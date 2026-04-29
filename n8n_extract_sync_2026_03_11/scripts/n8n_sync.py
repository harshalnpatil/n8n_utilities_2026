#!/usr/bin/env python3
"""n8n workflow sync CLI (Python-only)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from n8n_common import (
    SyncError,
    canonical_json_dumps,
    canonicalize_workflow_payload,
    ensure_dirs,
    get_instances,
    get_workflow,
    load_config,
    load_json,
    load_state,
    make_record_key,
    resolve_workspace_root,
    save_state,
    sha256_text,
    slugify,

    utc_now_iso,
    verify_instance,
    write_json,
    list_workflows,
    update_workflow,
    create_workflow,
)


# ANSI formatting
_USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _stream_supports_unicode(stream: Any) -> bool:
    if os.environ.get("N8N_SYNC_ASCII") == "1":
        return False

    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False

    try:
        "✓✗▸●○─│".encode(encoding)
    except (LookupError, TypeError, UnicodeEncodeError):
        return False
    return True


_USE_UNICODE = _stream_supports_unicode(sys.stdout)


def _sgr(code: str) -> str:
    return f"\033[{code}m" if _USE_COLOR else ""


_RESET = _sgr("0")
_BOLD = _sgr("1")
_DIM = _sgr("2")
_GREEN = _sgr("32")
_YELLOW = _sgr("33")
_CYAN = _sgr("36")
_RED = _sgr("31")
_WHITE = _sgr("37")

_TAG_STYLES: Dict[str, str] = {
    "NEW": _sgr("1;32"),        # bold green
    "CHANGED": _sgr("1;33"),    # bold yellow
    "UNCHANGED": _sgr("2"),     # dim
    "DRY-RUN": _sgr("1;36"),   # bold cyan
    "PUSHED": _sgr("1;32"),    # bold green
    "CONFLICT": _sgr("1;31"),  # bold red
    "SKIP": _sgr("2"),         # dim
    "PULL": _sgr("1;36"),      # bold cyan
    "PUSH": _sgr("1;33"),      # bold yellow
    "CLEAN": _sgr("2"),        # dim
    "DELETE": _sgr("1;31"),    # bold red
    "STALE": _sgr("1;31"),     # bold red
    "ARCHIVED": _sgr("1;31"),  # bold red
    "LOCAL_CHANGED": _sgr("1;33"),   # bold yellow (local ahead)
    "REMOTE_CHANGED": _sgr("1;36"),  # bold cyan (remote ahead)
}


def _tag(label: str) -> str:
    style = _TAG_STYLES.get(label, "")
    return f"  {style}{label:>9}{_RESET}"


def _safe_text(text: Any, stream: Any | None = None) -> str:
    value = str(text)
    stream = sys.stdout if stream is None else stream
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return value
    try:
        value.encode(encoding)
    except (LookupError, TypeError, UnicodeEncodeError):
        return value.encode(encoding, errors="replace").decode(encoding)
    return value


def _dim(text: str) -> str:
    return f"{_DIM}{_safe_text(text)}{_RESET}"


def _bold(text: str) -> str:
    return f"{_BOLD}{_safe_text(text)}{_RESET}"


def _glyph(unicode_text: str, ascii_text: str) -> str:
    return unicode_text if _USE_UNICODE else ascii_text


def _active_dot(active: bool) -> str:
    if active:
        return f"{_GREEN}{_glyph('●', '*')}{_RESET}"
    return f"{_DIM}{_glyph('○', 'o')}{_RESET}"


def _short_date(iso: str) -> str:
    """'2026-03-11T17:48:00.546Z' -> '2026-03-11 17:48'"""
    if not iso or iso == "?":
        return "?"
    return iso.replace("T", " ")[:16]


def _print_instance_header(alias: str, count: int, mode_label: str) -> None:
    header_arrow = _glyph("▸", ">")
    separator = _glyph("│", "|")
    rule = _glyph("─", "-") * 56
    print(f"\n{_BOLD}{header_arrow} {alias}{_RESET}  {_DIM}{separator}{_RESET}  {count} workflows  {_DIM}{separator}{_RESET}  {mode_label}")
    print(f"  {_DIM}{rule}{_RESET}")


def _print_workflow_line(
    tag_label: str,
    name: str,
    active: bool,
    updated_at: str,
    path_str: str,
    direction: str = "->",
    workflow_id: str = "",
) -> None:
    """Print a compact 2-line workflow entry."""
    dot = _active_dot(active)
    date = _short_date(updated_at)
    safe_direction = _safe_text(direction)
    wid_part = f"  {_dim('id=' + workflow_id)}" if workflow_id else ""
    print(f"{_tag(tag_label)}  {dot} {_bold(name)}{wid_part}")
    print(f"{'':>13}{_dim(date)}  {safe_direction} {_dim(path_str)}")


def _format_summary_parts(counters: Dict[str, int]) -> List[str]:
    parts = []
    for label in ("NEW", "CHANGED", "LOCAL_CHANGED", "REMOTE_CHANGED", "UNCHANGED", "PUSHED", "PULL", "PUSH", "CONFLICT", "ARCHIVED", "SKIP", "CLEAN", "DELETE", "STALE"):
        n = counters.get(label, 0)
        if n:
            style = _TAG_STYLES.get(label, "")
            parts.append(f"{style}{n} {label.lower()}{_RESET}")
    return parts


def _print_instance_summary(alias: str, counters: Dict[str, int], dry_run: bool) -> None:
    parts = _format_summary_parts(counters)
    if not parts:
        return
    prefix = f"{_DIM}(dry-run){_RESET} " if dry_run else ""
    print(f"\n  {prefix}{_bold(alias)}: {', '.join(parts)}")


def _print_summary(counters: Dict[str, int], dry_run: bool) -> None:
    parts = _format_summary_parts(counters)
    if not parts:
        return
    prefix = f"{_DIM}(dry-run){_RESET} " if dry_run else ""
    print(f"\n  {prefix}{', '.join(parts)}")


def _merge_counters(total: Dict[str, int], instance_counters: Dict[str, int]) -> None:
    for k, v in instance_counters.items():
        total[k] = total.get(k, 0) + v


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync n8n workflows to/from local repo.")
    parser.add_argument("--mode", choices=["backup", "status", "push", "sync-two-way"], default="backup")
    parser.add_argument("--instance", choices=["primary", "secondary", "tertiary", "all"], default="all")
    parser.add_argument("--workflow-id", help="Optional workflow id for targeted sync")
    parser.add_argument("--dry-run", action="store_true", help="Show planned writes without mutating local/remote")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Push mode only: overwrite the remote even if it changed since the last local sync (skips conflict guard)",
    )
    parser.add_argument("--dotenv", default="secrets/.env.n8n", help="Path to dotenv file relative to repo root")
    parser.add_argument(
        "--output-dir",
        default="",
        help="Workspace root containing workflows/ and .n8n_sync/ (default: auto-detect)",
    )
    return parser.parse_args()


def selected_aliases(instance_arg: str, available: Iterable[str]) -> List[str]:
    aliases = sorted(list(available))
    if instance_arg == "all":
        return aliases
    if instance_arg not in aliases:
        raise SyncError(f"Instance '{instance_arg}' is not configured; available: {', '.join(aliases)}")
    return [instance_arg]


def _normalize_path(p: str) -> str:
    """Normalise Windows backslash paths so they resolve on Linux/WSL."""
    return p.replace("\\", "/")


def _find_existing_dir_for_id(parent: Path, id_slug: str) -> Path | None:
    """Find an existing workflow folder that ends with _<id_slug>."""
    if not parent.is_dir():
        return None
    suffix = f"_{id_slug}"
    for entry in parent.iterdir():
        if entry.is_dir() and entry.name.endswith(suffix):
            return entry
    return None


def workflow_dir(repo_root: Path, alias: str, workflow_name: str, workflow_id: str) -> Path:
    slug = f"{slugify(workflow_name)}_{slugify(workflow_id)}"
    return repo_root / "workflows" / alias / slug


def _resolve_workflow_dir(repo_root: Path, alias: str, workflow_name: str, workflow_id: str, dry_run: bool) -> Path:
    """Return the workflow directory, renaming any existing folder if the workflow was renamed."""
    id_slug = slugify(workflow_id)
    target = workflow_dir(repo_root, alias, workflow_name, workflow_id)
    if target.exists():
        return target

    parent = repo_root / "workflows" / alias
    existing = _find_existing_dir_for_id(parent, id_slug)
    if existing and existing != target:
        if not dry_run:
            existing.rename(target)
        else:
            print(f"  (would rename {existing.name} -> {target.name})")
    return target


def local_workflow_hash(local_path: Path) -> str:
    if not local_path.exists():
        return ""
    local_data = load_json(local_path, fallback={})
    canonical = canonical_json_dumps(canonicalize_workflow_payload(local_data))
    return sha256_text(canonical)


def remote_workflow_hash(payload: Dict[str, Any]) -> str:
    canonical = canonical_json_dumps(canonicalize_workflow_payload(payload))
    return sha256_text(canonical)


def store_local_workflow(
    repo_root: Path,
    alias: str,
    workflow_payload: Dict[str, Any],
    dry_run: bool,
) -> Tuple[Path, str]:
    workflow_id = str(workflow_payload.get("id", "unknown"))
    workflow_name = str(workflow_payload.get("name", "workflow"))
    target_dir = _resolve_workflow_dir(repo_root, alias, workflow_name, workflow_id, dry_run)
    workflow_path = target_dir / "workflow.json"
    metadata_path = target_dir / "metadata.json"

    metadata = {
        "id": workflow_id,
        "name": workflow_name,
        "versionId": workflow_payload.get("versionId"),
        "active": workflow_payload.get("active"),
        "activeVersionId": workflow_payload.get("activeVersionId"),
        "createdAt": workflow_payload.get("createdAt"),
        "updatedAt": workflow_payload.get("updatedAt"),
        "tags": workflow_payload.get("tags", []),
        "projectId": workflow_payload.get("projectId"),
        "syncedAtUtc": utc_now_iso(),
        "instance": alias,
    }

    if not dry_run:
        write_json(workflow_path, workflow_payload)
        write_json(metadata_path, metadata)

    return workflow_path, remote_workflow_hash(workflow_payload)


def print_instance_status(alias: str, ok: bool, message: str) -> None:
    if ok:
        icon = f"{_GREEN}{_glyph('✓', 'OK')}{_RESET}"
    else:
        icon = f"{_RED}{_glyph('✗', 'X')}{_RESET}"
    print(f"  {icon} {_BOLD}{_safe_text(alias)}{_RESET}  {_DIM}{_safe_text(message)}{_RESET}")


def verify_selected_instances(instances: Dict[str, Any], aliases: List[str]) -> None:
    any_error = False
    for alias in aliases:
        ok, msg = verify_instance(instances[alias])
        print_instance_status(alias, ok, msg)
        if not ok:
            any_error = True
    if any_error:
        raise SyncError("One or more instance checks failed.")


def backup_mode(
    repo_root: Path,
    instances: Dict[str, Any],
    aliases: List[str],
    workflow_id: str | None,
    dry_run: bool,
    state: Dict[str, Any],
) -> None:
    records = state.setdefault("records", {})
    total_counters: Dict[str, int] = {}
    for alias in aliases:
        counters: Dict[str, int] = {}
        summaries = list_workflows(instances[alias])
        if workflow_id:
            summaries = [item for item in summaries if str(item.get("id")) == workflow_id]
        mode_label = f"{'dry-run ' if dry_run else ''}backup"
        _print_instance_header(alias, len(summaries), mode_label)
        for summary in summaries:
            wid = str(summary.get("id"))
            payload = get_workflow(instances[alias], wid)
            workflow_path, remote_hash = store_local_workflow(repo_root, alias, payload, dry_run=dry_run)
            key = make_record_key(alias, wid)
            local_hash = remote_hash

            updated_at = payload.get("updatedAt", "?")
            active = payload.get("active", False)
            prev = records.get(key)
            change_tag = "NEW" if prev is None else (
                "CHANGED" if prev.get("lastRemoteHash") != remote_hash else "UNCHANGED"
            )
            counters[change_tag] = counters.get(change_tag, 0) + 1

            record = {
                "instance": alias,
                "workflowId": wid,
                "workflowName": payload.get("name"),
                "localPath": str(workflow_path.relative_to(repo_root)),
                "versionId": payload.get("versionId", "?"),
                "updatedAt": updated_at,
                "lastRemoteHash": remote_hash,
                "lastLocalHash": local_hash,
                "lastSyncAtUtc": utc_now_iso(),
                "lastDirection": "remote_to_local",
            }
            if not dry_run:
                records[key] = record

            _print_workflow_line(
                change_tag,
                payload.get("name", "?"),
                active,
                updated_at,
                record["localPath"],
                workflow_id=wid,
            )
        remote_ids = {str(s.get("id")) for s in summaries}
        prune_deleted_remote(repo_root, alias, remote_ids, records, workflow_id, dry_run, counters)
        _print_instance_summary(alias, counters, dry_run)
        _merge_counters(total_counters, counters)
    if len(aliases) > 1:
        _print_summary(total_counters, dry_run)


def status_mode(repo_root: Path, instances: Dict[str, Any], aliases: List[str], state: Dict[str, Any]) -> int:
    records = state.get("records", {})
    exit_code = 0
    total_counters: Dict[str, int] = {}
    for alias in aliases:
        counters: Dict[str, int] = {}
        summaries = list_workflows(instances[alias])
        remote_by_id = {str(item.get("id")): item for item in summaries}

        relevant = [
            rec for rec in records.values() if rec.get("instance") == alias and str(rec.get("workflowId")) in remote_by_id
        ]
        _print_instance_header(alias, len(relevant), "status")
        for rec in relevant:
            wid = str(rec["workflowId"])
            local_path = repo_root / _normalize_path(rec["localPath"])
            remote_payload = get_workflow(instances[alias], wid)
            current_remote_hash = remote_workflow_hash(remote_payload)
            current_local_hash = local_workflow_hash(local_path)

            remote_changed = current_remote_hash != rec.get("lastRemoteHash", "")
            local_changed = current_local_hash != rec.get("lastLocalHash", "")
            if remote_changed and local_changed:
                tag = "CONFLICT"
                exit_code = 2
            elif remote_changed:
                tag = "REMOTE_CHANGED"
                exit_code = max(exit_code, 1)
            elif local_changed:
                tag = "LOCAL_CHANGED"
                exit_code = max(exit_code, 1)
            else:
                tag = "CLEAN"
            counters[tag] = counters.get(tag, 0) + 1

            _print_workflow_line(
                tag,
                rec.get("workflowName", "?"),
                rec.get("active", False),
                rec.get("updatedAt", "?"),
                rec.get("localPath", "?"),
                workflow_id=wid,
            )
        # Report workflows deleted on remote (informational, no mutation)
        all_alias_recs = [
            rec for rec in records.values() if rec.get("instance") == alias
        ]
        for rec in all_alias_recs:
            wid = str(rec.get("workflowId"))
            if wid not in remote_by_id:
                counters["STALE"] = counters.get("STALE", 0) + 1
                exit_code = max(exit_code, 1)
                _print_workflow_line(
                    "STALE",
                    rec.get("workflowName", "?"),
                    rec.get("active", False),
                    rec.get("updatedAt", "?"),
                    rec.get("localPath", "?"),
                    workflow_id=wid,
                )
        _print_instance_summary(alias, counters, dry_run=False)
        _merge_counters(total_counters, counters)
    if len(aliases) > 1:
        _print_summary(total_counters, dry_run=False)
    return exit_code


def build_upsert_payload(local_data: Dict[str, Any]) -> Dict[str, Any]:
    """Build a payload containing only the fields accepted by the n8n API."""
    ALLOWED_TOP_KEYS = ("name", "nodes", "connections", "settings", "staticData")
    ALLOWED_SETTINGS_KEYS = (
        "saveExecutionProgress", "saveManualExecutions",
        "saveDataErrorExecution", "saveDataSuccessExecution",
        "executionTimeout", "errorWorkflow", "timezone",
        "executionOrder", "callerPolicy", "callerIds",
        "timeSavedPerExecution", "availableInMCP",
    )
    payload = {k: json.loads(json.dumps(v)) for k, v in local_data.items() if k in ALLOWED_TOP_KEYS}
    if "settings" in payload and isinstance(payload["settings"], dict):
        payload["settings"] = {k: v for k, v in payload["settings"].items() if k in ALLOWED_SETTINGS_KEYS}
    return payload


def is_archived_workflow(payload: Dict[str, Any]) -> bool:
    """Return True when an n8n workflow payload/summary is marked archived."""
    return any(
        bool(payload.get(key))
        for key in ("archived", "isArchived", "archivedAt")
    )


def push_mode(
    repo_root: Path,
    instances: Dict[str, Any],
    aliases: List[str],
    workflow_id: str | None,
    dry_run: bool,
    state: Dict[str, Any],
    force: bool = False,
) -> None:
    records = state.setdefault("records", {})
    total_counters: Dict[str, int] = {}
    for alias in aliases:
        counters: Dict[str, int] = {}
        alias_recs = [(k, r) for k, r in records.items() if r.get("instance") == alias]
        if workflow_id:
            alias_recs = [(k, r) for k, r in alias_recs if str(r.get("workflowId")) == workflow_id]
        mode_label = f"{'dry-run ' if dry_run else ''}push{' (force)' if force else ''}"
        _print_instance_header(alias, len(alias_recs), mode_label)
        for key, rec in alias_recs:
            original_key = key
            wid = str(rec.get("workflowId"))
            name = rec.get("workflowName", "?")

            local_path = repo_root / _normalize_path(rec["localPath"])
            if not local_path.exists():
                counters["SKIP"] = counters.get("SKIP", 0) + 1
                _print_workflow_line("SKIP", name, False, "?", rec["localPath"], "<-", workflow_id=wid)
                continue

            local_data = load_json(local_path, fallback={})
            payload = build_upsert_payload(local_data)
            local_hash = local_workflow_hash(local_path)
            local_updated_at = local_data.get("updatedAt", "?")
            local_active = bool(local_data.get("active", rec.get("active", False)))
            previous_local_hash = rec.get("lastLocalHash", "")
            previous_remote_hash = rec.get("lastRemoteHash", "")
            local_changed = local_hash != previous_local_hash

            if not local_changed:
                counters["CLEAN"] = counters.get("CLEAN", 0) + 1
                _print_workflow_line(
                    "CLEAN", name,
                    local_active, local_updated_at,
                    rec["localPath"], "<-",
                    workflow_id=wid,
                )
                continue

            if dry_run:
                try:
                    remote_payload = get_workflow(instances[alias], wid)
                    if is_archived_workflow(remote_payload):
                        tag = "ARCHIVED"
                    else:
                        remote_hash = remote_workflow_hash(remote_payload)
                        if remote_hash == local_hash:
                            tag = "CLEAN"
                        elif previous_remote_hash and remote_hash != previous_remote_hash and not force:
                            tag = "CONFLICT"
                        else:
                            tag = "PUSH"
                except SyncError as exc:
                    if "HTTP 404" in str(exc):
                        tag = "NEW"
                    else:
                        raise
                counters[tag] = counters.get(tag, 0) + 1
                _print_workflow_line(
                    tag, name,
                    local_active, local_updated_at,
                    rec["localPath"], "<-",
                    workflow_id=wid,
                )
                continue

            remote_payload: Dict[str, Any] | None = None
            try:
                remote_payload = get_workflow(instances[alias], wid)
            except SyncError as exc:
                if "HTTP 404" in str(exc):
                    remote_created = create_workflow(instances[alias], payload)
                    resulting_id = str(remote_created.get("id"))
                    wid = resulting_id
                    key = make_record_key(alias, wid)
                else:
                    raise SyncError(
                        f"Failed to inspect workflow '{name}' (id={wid}) "
                        f"from {rec['localPath']}: {exc}"
                    ) from exc
            else:
                if is_archived_workflow(remote_payload):
                    counters["ARCHIVED"] = counters.get("ARCHIVED", 0) + 1
                    _print_workflow_line(
                        "ARCHIVED", name,
                        local_active, local_updated_at,
                        rec["localPath"], "<-",
                        workflow_id=wid,
                    )
                    continue

                remote_hash = remote_workflow_hash(remote_payload)
                if remote_hash == local_hash:
                    rec["lastLocalHash"] = local_hash
                    rec["lastRemoteHash"] = local_hash
                    rec["lastSyncAtUtc"] = utc_now_iso()
                    records[key] = rec
                    counters["CLEAN"] = counters.get("CLEAN", 0) + 1
                    _print_workflow_line(
                        "CLEAN", name,
                        local_active, local_updated_at,
                        rec["localPath"], "<-",
                        workflow_id=wid,
                    )
                    continue

                if previous_remote_hash and remote_hash != previous_remote_hash and not force:
                    counters["CONFLICT"] = counters.get("CONFLICT", 0) + 1
                    _print_workflow_line(
                        "CONFLICT", name,
                        local_active, local_updated_at,
                        rec["localPath"], "<-",
                        workflow_id=wid,
                    )
                    raise SyncError(
                        f"Refusing to push workflow '{name}' (id={wid}) because the remote "
                        "changed since the last local sync. Run status/diff, then backup or "
                        "resolve the conflict before pushing (or rerun with --force)."
                    )

                try:
                    remote_updated = update_workflow(instances[alias], wid, payload)
                    resulting_id = str(remote_updated.get("id", wid))
                except SyncError as exc:
                    raise SyncError(
                        f"Failed to push workflow '{name}' (id={wid}) "
                        f"from {rec['localPath']}: {exc}"
                    ) from exc

            rec["workflowId"] = wid
            rec["versionId"] = local_data.get("versionId", "?")
            rec["updatedAt"] = local_updated_at
            rec["lastLocalHash"] = local_hash
            rec["lastRemoteHash"] = local_hash
            rec["lastSyncAtUtc"] = utc_now_iso()
            rec["lastDirection"] = "local_to_remote"
            if key != original_key:
                records.pop(original_key, None)
            records[key] = rec
            counters["PUSHED"] = counters.get("PUSHED", 0) + 1
            _print_workflow_line(
                "PUSHED", rec.get("workflowName", "?"),
                rec.get("active", False), local_updated_at,
                rec["localPath"], "<-",
                workflow_id=wid,
            )
        _print_instance_summary(alias, counters, dry_run)
        _merge_counters(total_counters, counters)
    if len(aliases) > 1:
        _print_summary(total_counters, dry_run)


def sync_two_way_mode(
    repo_root: Path,
    instances: Dict[str, Any],
    aliases: List[str],
    workflow_id: str | None,
    dry_run: bool,
    state: Dict[str, Any],
) -> int:
    records = state.setdefault("records", {})
    conflicts = 0
    total_counters: Dict[str, int] = {}

    for alias in aliases:
        counters: Dict[str, int] = {}
        summaries = list_workflows(instances[alias])
        remote_ids = {str(s.get("id")) for s in summaries}

        alias_recs = [r for r in records.values() if r.get("instance") == alias]
        if workflow_id:
            alias_recs = [r for r in alias_recs if str(r.get("workflowId")) == workflow_id]
        # Filter to only records still present on remote (pruning handles the rest)
        alias_recs = [r for r in alias_recs if str(r.get("workflowId")) in remote_ids]
        mode_label = f"{'dry-run ' if dry_run else ''}sync"
        _print_instance_header(alias, len(alias_recs), mode_label)

        for rec in alias_recs:
            wid = str(rec.get("workflowId"))
            name = rec.get("workflowName", "?")
            active = rec.get("active", False)
            local_path = repo_root / _normalize_path(rec["localPath"])

            if not local_path.exists():
                counters["SKIP"] = counters.get("SKIP", 0) + 1
                _print_workflow_line("SKIP", name, False, "?", rec["localPath"], workflow_id=wid)
                continue

            remote_payload = get_workflow(instances[alias], wid)
            current_remote_hash = remote_workflow_hash(remote_payload)
            current_local_hash = local_workflow_hash(local_path)
            prev_remote_hash = rec.get("lastRemoteHash", "")
            prev_local_hash = rec.get("lastLocalHash", "")

            remote_changed = current_remote_hash != prev_remote_hash
            local_changed = current_local_hash != prev_local_hash

            if remote_changed and local_changed:
                conflicts += 1
                counters["CONFLICT"] = counters.get("CONFLICT", 0) + 1
                _print_workflow_line("CONFLICT", name, active, rec.get("updatedAt", "?"), rec["localPath"], workflow_id=wid)
                continue

            if remote_changed and not local_changed:
                tag = "PULL"
                if not dry_run:
                    local_payload = get_workflow(instances[alias], wid)
                    new_path, remote_hash = store_local_workflow(repo_root, alias, local_payload, dry_run=False)
                    rec["localPath"] = str(new_path.relative_to(repo_root))
                    rec["workflowName"] = local_payload.get("name", name)
                    rec["lastRemoteHash"] = remote_hash
                    rec["lastLocalHash"] = remote_hash
                    rec["lastDirection"] = "remote_to_local"
                    rec["lastSyncAtUtc"] = utc_now_iso()
                counters[tag] = counters.get(tag, 0) + 1
                _print_workflow_line(tag, rec.get("workflowName", name), active, remote_payload.get("updatedAt", "?"), rec["localPath"], "->", workflow_id=wid)
                continue

            if local_changed and not remote_changed:
                tag = "PUSH"
                if not dry_run:
                    local_payload = load_json(local_path, fallback={})
                    upsert_payload = build_upsert_payload(local_payload)
                    update_workflow(instances[alias], wid, upsert_payload)
                    rec["lastRemoteHash"] = current_local_hash
                    rec["lastLocalHash"] = current_local_hash
                    rec["lastDirection"] = "local_to_remote"
                    rec["lastSyncAtUtc"] = utc_now_iso()
                counters[tag] = counters.get(tag, 0) + 1
                _print_workflow_line(tag, name, active, rec.get("updatedAt", "?"), rec["localPath"], "<-", workflow_id=wid)
                continue

            counters["CLEAN"] = counters.get("CLEAN", 0) + 1
            _print_workflow_line("CLEAN", name, active, rec.get("updatedAt", "?"), rec["localPath"], workflow_id=wid)

        prune_deleted_remote(repo_root, alias, remote_ids, records, workflow_id, dry_run, counters)
        _print_instance_summary(alias, counters, dry_run)
        _merge_counters(total_counters, counters)

    if len(aliases) > 1:
        _print_summary(total_counters, dry_run)
    return conflicts


def prune_deleted_remote(
    repo_root: Path,
    alias: str,
    remote_ids: set[str],
    records: Dict[str, Any],
    workflow_id: str | None,
    dry_run: bool,
    counters: Dict[str, int],
    tag_label: str = "DELETE",
) -> List[str]:
    """Remove local state+files for workflows deleted on the remote.

    Returns list of record keys that were pruned (or would be pruned in dry-run).
    """
    pruned_keys: List[str] = []
    alias_keys = [
        (k, r) for k, r in list(records.items())
        if r.get("instance") == alias
    ]
    if workflow_id:
        alias_keys = [(k, r) for k, r in alias_keys if str(r.get("workflowId")) == workflow_id]

    for key, rec in alias_keys:
        wid = str(rec.get("workflowId"))
        if wid in remote_ids:
            continue

        name = rec.get("workflowName", "?")
        local_rel = rec.get("localPath", "?")
        _print_workflow_line(
            tag_label,
            name,
            rec.get("active", False),
            rec.get("updatedAt", "?"),
            local_rel,
            workflow_id=wid,
        )
        counters[tag_label] = counters.get(tag_label, 0) + 1

        if not dry_run:
            local_dir = (repo_root / local_rel).parent if local_rel != "?" else None
            if local_dir and local_dir.exists():
                shutil.rmtree(local_dir, onexc=_rmtree_handle_permission_error)
            records.pop(key, None)

        pruned_keys.append(key)
    return pruned_keys


def _rmtree_handle_permission_error(func: Any, path: str, exc: BaseException) -> None:
    if not isinstance(exc, PermissionError):
        raise exc
    os.chmod(path, stat.S_IWRITE)
    func(path)


def main() -> int:
    args = parse_args()
    repo_root = resolve_workspace_root(args.output_dir or None, script_path=Path(__file__))
    ensure_dirs(repo_root)
    print(f"workspace root: {repo_root}", file=sys.stderr)

    config = load_config(repo_root, dotenv_relpath=args.dotenv)
    instances = get_instances(config)
    aliases = selected_aliases(args.instance, instances.keys())
    verify_selected_instances(instances, aliases)

    state = load_state(repo_root)

    if args.mode == "backup":
        backup_mode(repo_root, instances, aliases, args.workflow_id, args.dry_run, state)
    elif args.mode == "status":
        code = status_mode(repo_root, instances, aliases, state)
        if not args.dry_run:
            save_state(repo_root, state)
        return code
    elif args.mode == "push":
        push_mode(repo_root, instances, aliases, args.workflow_id, args.dry_run, state, force=args.force)
    elif args.mode == "sync-two-way":
        conflicts = sync_two_way_mode(repo_root, instances, aliases, args.workflow_id, args.dry_run, state)
        if conflicts:
            print(f"conflicts={conflicts}")
            if not args.dry_run:
                save_state(repo_root, state)
            return 2
    else:
        raise SyncError(f"Unsupported mode: {args.mode}")

    if not args.dry_run:
        save_state(repo_root, state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
