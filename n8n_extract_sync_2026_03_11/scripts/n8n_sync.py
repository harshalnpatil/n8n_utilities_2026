#!/usr/bin/env python3
"""n8n workflow sync CLI (Python-only)."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import socket
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
    insert_supabase_row,
    load_config,
    load_json,
    load_key_value_file,
    load_state,
    local_workflow_hash,
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


def _should_print_workflow_row(tag_label: str, verbose: bool) -> bool:
    if verbose:
        return True
    return tag_label not in {"CLEAN", "UNCHANGED"}


def _diff_friendly_text(payload: Dict[str, Any]) -> str:
    """Canonical workflow payload as pretty-printed JSON for line-level diffing."""
    cleaned = canonicalize_workflow_payload(payload)
    return json.dumps(cleaned, sort_keys=True, indent=2, ensure_ascii=False)


def _compute_diff_stats(before_text: str, after_text: str) -> Tuple[int, int]:
    """Count added/removed lines in a unified diff of two texts."""
    if not before_text and not after_text:
        return 0, 0
    before_lines = before_text.splitlines(keepends=True)
    after_lines = after_text.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(before_lines, after_lines, n=0))
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return added, removed


class _CustomHelpFormatter(argparse.HelpFormatter):
    """Custom formatter to show metavar only on the long option."""

    def _format_action_invocation(self, action: argparse.Action) -> str:
        if not action.option_strings or action.nargs == 0:
            return super()._format_action_invocation(action)

        default = self._get_default_metavar_for_optional(action)
        args_string = self._format_args(action, default)

        # If we have both short and long, only put the metavar on the last (long) one
        if len(action.option_strings) > 1:
            return ", ".join(action.option_strings[:-1]) + ", " + action.option_strings[-1] + " " + args_string
        return action.option_strings[0] + " " + args_string


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync n8n workflows to/from local repo.",
        formatter_class=_CustomHelpFormatter,
    )
    parser.add_argument("-m", "--mode", choices=["backup", "status", "push", "register", "sync-two-way"], default="backup", metavar="<mode>")
    parser.add_argument("-i", "--instance", choices=["primary", "secondary", "tertiary", "all"], default="all", metavar="<alias>")
    parser.add_argument("-wid", "--workflow-id", help="Optional workflow id for targeted sync", metavar="<id>")
    parser.add_argument("-dr", "--dry-run", action="store_true", help="Show planned writes without mutating local/remote")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show unchanged workflows in backup and push output",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Push mode only: overwrite the remote even if it changed since the last local sync (skips conflict guard)",
    )
    parser.add_argument(
        "--force-check",
        action="store_true",
        help="status/backup/sync modes: fetch every workflow from the API instead of using the updatedAt fast-path",
    )
    parser.add_argument("-d", "--dotenv", default="secrets/.env.n8n", help="Path to dotenv file relative to repo root", metavar="<path>")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="",
        help="Workspace root containing workflows/ and .n8n_sync/ (default: auto-detect)",
        metavar="<dir>",
    )
    parser.add_argument(
        "--supabase-env-file",
        default="",
        help="Path to Supabase env file (SUPABASE_PROJECT_URL/SUPABASE_SECRET_KEY). "
             "Default: secrets/supabase_env under workspace root. "
             "If the file does not exist, telemetry is silently skipped.",
        metavar="<path>",
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
    failures: List[str] = []
    for alias in aliases:
        ok, msg = verify_instance(instances[alias])
        print_instance_status(alias, ok, msg)
        if not ok:
            any_error = True
            failures.append(f"{alias}: {msg}")
    if any_error:
        if len(failures) == 1:
            raise SyncError(f"Instance check failed: {failures[0]}")
        raise SyncError(f"Instance checks failed: {'; '.join(failures)}")


def backup_mode(
    repo_root: Path,
    instances: Dict[str, Any],
    aliases: List[str],
    workflow_id: str | None,
    dry_run: bool,
    state: Dict[str, Any],
    verbose: bool = False,
    telemetry_events: List[Dict[str, Any]] | None = None,
    force_check: bool = False,
) -> None:
    _tel = telemetry_events if telemetry_events is not None else []
    records = state.setdefault("records", {})
    total_counters: Dict[str, int] = {}
    for alias in aliases:
        counters: Dict[str, int] = {}
        summaries = filter_unarchived_workflows(list_workflows(instances[alias]))
        if workflow_id:
            summaries = [item for item in summaries if str(item.get("id")) == workflow_id]
        mode_label = f"{'dry-run ' if dry_run else ''}backup"
        _print_instance_header(alias, len(summaries), mode_label)
        for summary in summaries:
            wid = str(summary.get("id"))
            key = make_record_key(alias, wid)
            prev = records.get(key)

            # Fast-path: skip get_workflow() when the remote updatedAt matches the
            # stored record and the local mirror copy is still intact. The list
            # endpoint already returns updatedAt, so this avoids one API call per
            # unchanged workflow.
            if not force_check and prev is not None:
                summary_updated_at = summary.get("updatedAt", "")
                local_path = repo_root / _normalize_path(prev["localPath"])
                current_local_hash = local_workflow_hash(local_path)
                remote_unchanged = summary_updated_at and summary_updated_at == prev.get("updatedAt", "")
                local_unchanged = current_local_hash and current_local_hash == prev.get("lastLocalHash", "")
                if remote_unchanged and local_unchanged:
                    counters["UNCHANGED"] = counters.get("UNCHANGED", 0) + 1
                    if _should_print_workflow_row("UNCHANGED", verbose):
                        _print_workflow_line(
                            "UNCHANGED",
                            prev.get("workflowName", "?"),
                            prev.get("active", False),
                            prev.get("updatedAt", "?"),
                            prev.get("localPath", "?"),
                            workflow_id=wid,
                        )
                    continue

            payload = get_workflow(instances[alias], wid)

            # Read old local content before store_local_workflow overwrites it
            parent = repo_root / "workflows" / alias
            existing_dir = _find_existing_dir_for_id(parent, slugify(wid))
            old_canonical = ""
            if existing_dir:
                old_wf_path = existing_dir / "workflow.json"
                if old_wf_path.exists():
                    old_local_data = load_json(old_wf_path, fallback={})
                    old_canonical = _diff_friendly_text(old_local_data)

            workflow_path, remote_hash = store_local_workflow(repo_root, alias, payload, dry_run=dry_run)
            local_hash = remote_hash

            updated_at = payload.get("updatedAt", "?")
            active = payload.get("active", False)
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

            # Telemetry for workflows that were actually transferred
            if change_tag in ("NEW", "CHANGED") and not dry_run:
                new_canonical = _diff_friendly_text(payload)
                lines_added, lines_removed = _compute_diff_stats(old_canonical, new_canonical)
                _tel.append({
                    "event_time": utc_now_iso(),
                    "host_name": socket.gethostname(),
                    "instance": alias,
                    "mode": "backup",
                    "dry_run": False,
                    "force_push": False,
                    "workflow_id": wid,
                    "workflow_name": payload.get("name"),
                    "event_type": change_tag,
                    "direction": "remote_to_local",
                    "local_path": record["localPath"],
                    "version_id": payload.get("versionId", ""),
                    "hash_before": prev.get("lastRemoteHash", "") if prev else "",
                    "hash_after": remote_hash,
                    "lines_added": lines_added,
                    "lines_removed": lines_removed,
                })

            if _should_print_workflow_row(change_tag, verbose):
                _print_workflow_line(
                    change_tag,
                    payload.get("name", "?"),
                    active,
                    updated_at,
                    record["localPath"],
                    workflow_id=wid,
                )
        remote_ids = {str(s.get("id")) for s in summaries}
        prune_deleted_remote(repo_root, alias, remote_ids, records, workflow_id, dry_run, counters, telemetry_events=_tel, telemetry_mode="backup")
        _print_instance_summary(alias, counters, dry_run)
        _merge_counters(total_counters, counters)
    if len(aliases) > 1:
        _print_summary(total_counters, dry_run)


def status_mode(
    repo_root: Path,
    instances: Dict[str, Any],
    aliases: List[str],
    state: Dict[str, Any],
    verbose: bool = False,
    force_check: bool = False,
) -> int:
    return status_mode_impl(repo_root, instances, aliases, state, verbose=verbose, force_check=force_check)


def status_mode_impl(
    repo_root: Path,
    instances: Dict[str, Any],
    aliases: List[str],
    state: Dict[str, Any],
    verbose: bool = False,
    force_check: bool = False,
) -> int:
    records = state.get("records", {})
    exit_code = 0
    total_counters: Dict[str, int] = {}
    for alias in aliases:
        counters: Dict[str, int] = {}
        summaries = filter_unarchived_workflows(list_workflows(instances[alias]))
        remote_by_id = {str(item.get("id")): item for item in summaries}

        relevant = [
            rec for rec in records.values() if rec.get("instance") == alias and str(rec.get("workflowId")) in remote_by_id
        ]
        _print_instance_header(alias, len(relevant), "status")
        for rec in relevant:
            wid = str(rec["workflowId"])
            local_path = repo_root / _normalize_path(rec["localPath"])
            current_local_hash = local_workflow_hash(local_path)

            # Fast-path: skip get_workflow() when both local and remote appear unchanged.
            # The list endpoint already returns updatedAt; if it matches the state
            # record and the local hash also matches, the workflow must be CLEAN.
            if not force_check:
                summary_updated_at = remote_by_id.get(wid, {}).get("updatedAt", "")
                state_updated_at = rec.get("updatedAt", "")
                local_unchanged = current_local_hash and current_local_hash == rec.get("lastLocalHash", "")
                remote_unchanged = summary_updated_at and summary_updated_at == state_updated_at
                if local_unchanged and remote_unchanged:
                    tag = "CLEAN"
                    counters[tag] = counters.get(tag, 0) + 1
                    if _should_print_workflow_row(tag, verbose):
                        _print_workflow_line(
                            tag,
                            rec.get("workflowName", "?"),
                            rec.get("active", False),
                            rec.get("updatedAt", "?"),
                            rec.get("localPath", "?"),
                            workflow_id=wid,
                        )
                    continue

            remote_payload = get_workflow(instances[alias], wid)
            current_remote_hash = remote_workflow_hash(remote_payload)

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

            if _should_print_workflow_row(tag, verbose):
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
                if _should_print_workflow_row("STALE", verbose):
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


def filter_unarchived_workflows(summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return only workflows that are not marked archived in the API payload."""
    return [summary for summary in summaries if not is_archived_workflow(summary)]


def push_mode(
    repo_root: Path,
    instances: Dict[str, Any],
    aliases: List[str],
    workflow_id: str | None,
    dry_run: bool,
    state: Dict[str, Any],
    force: bool = False,
    verbose: bool = False,
    telemetry_events: List[Dict[str, Any]] | None = None,
) -> None:
    _tel = telemetry_events if telemetry_events is not None else []
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
                if _should_print_workflow_row("CLEAN", verbose):
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
                if _should_print_workflow_row(tag, verbose):
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
                    if _should_print_workflow_row("CLEAN", verbose):
                        _print_workflow_line(
                            "CLEAN", name,
                            local_active, local_updated_at,
                            rec["localPath"], "<-",
                            workflow_id=wid,
                        )
                    continue

                if previous_remote_hash and remote_hash != previous_remote_hash and not force:
                    counters["CONFLICT"] = counters.get("CONFLICT", 0) + 1
                    if _should_print_workflow_row("CONFLICT", verbose):
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

            # Telemetry for pushed workflow
            before_canonical = _diff_friendly_text(remote_payload) if remote_payload else ""
            after_canonical = _diff_friendly_text(local_data)
            lines_added, lines_removed = _compute_diff_stats(before_canonical, after_canonical)
            _tel.append({
                "event_time": utc_now_iso(),
                "host_name": socket.gethostname(),
                "instance": alias,
                "mode": "push",
                "dry_run": False,
                "force_push": force,
                "workflow_id": wid,
                "workflow_name": rec.get("workflowName", name),
                "event_type": "PUSHED",
                "direction": "local_to_remote",
                "local_path": rec["localPath"],
                "version_id": local_data.get("versionId", ""),
                "hash_before": previous_remote_hash or "",
                "hash_after": local_hash,
                "lines_added": lines_added,
                "lines_removed": lines_removed,
            })

            counters["PUSHED"] = counters.get("PUSHED", 0) + 1
            if _should_print_workflow_row("PUSHED", verbose):
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
    verbose: bool = False,
    telemetry_events: List[Dict[str, Any]] | None = None,
    force_check: bool = False,
) -> int:
    _tel = telemetry_events if telemetry_events is not None else []
    records = state.setdefault("records", {})
    conflicts = 0
    total_counters: Dict[str, int] = {}

    for alias in aliases:
        counters: Dict[str, int] = {}
        summaries = filter_unarchived_workflows(list_workflows(instances[alias]))
        remote_ids = {str(s.get("id")) for s in summaries}
        remote_by_id = {str(s.get("id")): s for s in summaries}

        alias_recs = [r for r in records.values() if r.get("instance") == alias]
        if workflow_id:
            alias_recs = [r for r in alias_recs if str(r.get("workflowId")) == workflow_id]
        mode_label = f"{'dry-run ' if dry_run else ''}sync"
        tracked_ids = {str(r.get("workflowId")) for r in alias_recs}
        remote_only_summaries = [summary for summary in summaries if str(summary.get("id")) not in tracked_ids]
        if workflow_id:
            remote_only_summaries = [summary for summary in remote_only_summaries if str(summary.get("id")) == workflow_id]

        _print_instance_header(alias, len(alias_recs) + len(remote_only_summaries), mode_label)

        for summary in remote_only_summaries:
            wid = str(summary.get("id"))
            name = str(summary.get("name", "workflow"))
            active = bool(summary.get("active", False))

            remote_payload = get_workflow(instances[alias], wid)
            if is_archived_workflow(remote_payload):
                counters["ARCHIVED"] = counters.get("ARCHIVED", 0) + 1
                _print_workflow_line(
                    "ARCHIVED",
                    name,
                    active,
                    remote_payload.get("updatedAt", "?"),
                    "?",
                    "->",
                    workflow_id=wid,
                )
                continue

            workflow_path, remote_hash = store_local_workflow(repo_root, alias, remote_payload, dry_run=dry_run)
            key = make_record_key(alias, wid)
            record = {
                "instance": alias,
                "workflowId": wid,
                "workflowName": remote_payload.get("name", name),
                "localPath": str(workflow_path.relative_to(repo_root)),
                "versionId": remote_payload.get("versionId", "?"),
                "updatedAt": remote_payload.get("updatedAt", "?"),
                "lastRemoteHash": remote_hash,
                "lastLocalHash": remote_hash,
                "lastSyncAtUtc": utc_now_iso(),
                "lastDirection": "remote_to_local",
            }
            if not dry_run:
                records[key] = record
            counters["NEW"] = counters.get("NEW", 0) + 1
            if not dry_run:
                after_canonical = _diff_friendly_text(remote_payload)
                lines_added, lines_removed = _compute_diff_stats("", after_canonical)
                _tel.append({
                    "event_time": utc_now_iso(),
                    "host_name": socket.gethostname(),
                    "instance": alias,
                    "mode": "sync-two-way",
                    "dry_run": False,
                    "force_push": False,
                    "workflow_id": wid,
                    "workflow_name": record["workflowName"],
                    "event_type": "NEW",
                    "direction": "remote_to_local",
                    "local_path": record["localPath"],
                    "version_id": remote_payload.get("versionId", ""),
                    "hash_before": "",
                    "hash_after": remote_hash,
                    "lines_added": lines_added,
                    "lines_removed": lines_removed,
                })
            _print_workflow_line(
                "NEW",
                record["workflowName"],
                active,
                record["updatedAt"],
                record["localPath"],
                "->",
                workflow_id=wid,
            )

        for rec in alias_recs:
            wid = str(rec.get("workflowId"))
            name = rec.get("workflowName", "?")
            active = rec.get("active", False)
            local_path = repo_root / _normalize_path(rec["localPath"])

            if not local_path.exists():
                counters["SKIP"] = counters.get("SKIP", 0) + 1
                _print_workflow_line("SKIP", name, False, "?", rec["localPath"], workflow_id=wid)
                continue

            # Fast-path: skip get_workflow() when the remote updatedAt matches the
            # stored record and the local copy is unchanged -> guaranteed CLEAN.
            # The list endpoint already returns updatedAt, avoiding one API call
            # per unchanged workflow.
            if not force_check:
                summary_updated_at = remote_by_id.get(wid, {}).get("updatedAt", "")
                current_local_hash = local_workflow_hash(local_path)
                remote_unchanged = summary_updated_at and summary_updated_at == rec.get("updatedAt", "")
                local_unchanged = current_local_hash and current_local_hash == rec.get("lastLocalHash", "")
                if remote_unchanged and local_unchanged:
                    counters["CLEAN"] = counters.get("CLEAN", 0) + 1
                    if _should_print_workflow_row("CLEAN", verbose):
                        _print_workflow_line("CLEAN", name, active, rec.get("updatedAt", "?"), rec["localPath"], workflow_id=wid)
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
                if _should_print_workflow_row("CONFLICT", verbose):
                    _print_workflow_line(
                        "CONFLICT", name, active, rec.get("updatedAt", "?"), rec["localPath"], workflow_id=wid
                    )
                continue

            if remote_changed and not local_changed:
                tag = "PULL"
                if not dry_run:
                    old_local_data = load_json(local_path, fallback={})
                    old_canonical = _diff_friendly_text(old_local_data)
                    local_payload = get_workflow(instances[alias], wid)
                    new_path, remote_hash = store_local_workflow(repo_root, alias, local_payload, dry_run=False)
                    rec["localPath"] = str(new_path.relative_to(repo_root))
                    rec["workflowName"] = local_payload.get("name", name)
                    rec["updatedAt"] = local_payload.get("updatedAt", rec.get("updatedAt", "?"))
                    rec["lastRemoteHash"] = remote_hash
                    rec["lastLocalHash"] = remote_hash
                    rec["lastDirection"] = "remote_to_local"
                    rec["lastSyncAtUtc"] = utc_now_iso()
                    # Telemetry for pulled workflow
                    after_canonical = _diff_friendly_text(local_payload)
                    lines_added, lines_removed = _compute_diff_stats(old_canonical, after_canonical)
                    _tel.append({
                        "event_time": utc_now_iso(),
                        "host_name": socket.gethostname(),
                        "instance": alias,
                        "mode": "sync-two-way",
                        "dry_run": False,
                        "force_push": False,
                        "workflow_id": wid,
                        "workflow_name": rec.get("workflowName", name),
                        "event_type": "PULL",
                        "direction": "remote_to_local",
                        "local_path": rec["localPath"],
                        "version_id": local_payload.get("versionId", ""),
                        "hash_before": prev_remote_hash,
                        "hash_after": remote_hash,
                        "lines_added": lines_added,
                        "lines_removed": lines_removed,
                    })
                counters[tag] = counters.get(tag, 0) + 1
                if _should_print_workflow_row(tag, verbose):
                    _print_workflow_line(
                        tag,
                        rec.get("workflowName", name),
                        active,
                        remote_payload.get("updatedAt", "?"),
                        rec["localPath"],
                        "->",
                        workflow_id=wid,
                    )
                continue

            if local_changed and not remote_changed:
                tag = "PUSH"
                if not dry_run:
                    local_payload = load_json(local_path, fallback={})
                    upsert_payload = build_upsert_payload(local_payload)
                    # Telemetry for pushed workflow
                    before_canonical = _diff_friendly_text(remote_payload)
                    after_canonical = _diff_friendly_text(local_payload)
                    lines_added, lines_removed = _compute_diff_stats(before_canonical, after_canonical)
                    _tel.append({
                        "event_time": utc_now_iso(),
                        "host_name": socket.gethostname(),
                        "instance": alias,
                        "mode": "sync-two-way",
                        "dry_run": False,
                        "force_push": False,
                        "workflow_id": wid,
                        "workflow_name": name,
                        "event_type": "PUSH",
                        "direction": "local_to_remote",
                        "local_path": rec["localPath"],
                        "version_id": local_payload.get("versionId", ""),
                        "hash_before": prev_remote_hash,
                        "hash_after": current_local_hash,
                        "lines_added": lines_added,
                        "lines_removed": lines_removed,
                    })
                    update_response = update_workflow(instances[alias], wid, upsert_payload)
                    if isinstance(update_response, dict) and update_response.get("updatedAt"):
                        rec["updatedAt"] = update_response["updatedAt"]
                    rec["lastRemoteHash"] = current_local_hash
                    rec["lastLocalHash"] = current_local_hash
                    rec["lastDirection"] = "local_to_remote"
                    rec["lastSyncAtUtc"] = utc_now_iso()
                counters[tag] = counters.get(tag, 0) + 1
                if _should_print_workflow_row(tag, verbose):
                    _print_workflow_line(
                        tag, name, active, rec.get("updatedAt", "?"), rec["localPath"], "<-", workflow_id=wid
                    )
                continue

            counters["CLEAN"] = counters.get("CLEAN", 0) + 1
            if _should_print_workflow_row("CLEAN", verbose):
                _print_workflow_line("CLEAN", name, active, rec.get("updatedAt", "?"), rec["localPath"], workflow_id=wid)

        existing_remote_ids = remote_ids | {str(s.get("id")) for s in remote_only_summaries}
        prune_deleted_remote(repo_root, alias, existing_remote_ids, records, workflow_id, dry_run, counters, telemetry_events=_tel, telemetry_mode="sync-two-way")
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
    telemetry_events: List[Dict[str, Any]] | None = None,
    telemetry_mode: str = "backup",
) -> List[str]:
    """Remove local state+files for workflows deleted on the remote.

    Returns list of record keys that were pruned (or would be pruned in dry-run).
    """
    _tel = telemetry_events if telemetry_events is not None else []
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
            _tel.append({
                "event_time": utc_now_iso(),
                "host_name": socket.gethostname(),
                "instance": alias,
                "mode": telemetry_mode,
                "dry_run": False,
                "force_push": False,
                "workflow_id": wid,
                "workflow_name": name,
                "event_type": tag_label,
                "direction": "remote_to_local",
                "local_path": local_rel,
                "version_id": "",
                "hash_before": rec.get("lastRemoteHash", ""),
                "hash_after": "",
                "lines_added": 0,
                "lines_removed": 0,
            })
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


def register_mode(
    repo_root: Path,
    instances: Dict[str, Any],
    aliases: List[str],
    workflow_id: str | None,
    dry_run: bool,
    state: Dict[str, Any],
    verbose: bool = False,
) -> int:
    """Scan local workflow directories for files not tracked in state and add stub records.

    After registering, use ``push --workflow-id <id>`` to create the workflow on the
    remote server (push handles the 404 → create_workflow path).
    """
    records = state.setdefault("records", {})
    total_counters: Dict[str, int] = {}
    exit_code = 0

    for alias in aliases:
        counters: Dict[str, int] = {}
        workflows_dir = repo_root / "workflows" / alias
        if not workflows_dir.is_dir():
            _print_instance_header(alias, 0, f"{'dry-run ' if dry_run else ''}register")
            _print_instance_summary(alias, counters, dry_run)
            continue

        # Collect all local workflow.json files under workflows/<alias>/
        local_workflows: List[Tuple[Path, Dict[str, Any]]] = []
        for entry in sorted(workflows_dir.iterdir()):
            if not entry.is_dir():
                continue
            wf_path = entry / "workflow.json"
            if not wf_path.is_file():
                continue
            local_data = load_json(wf_path, fallback={})
            wid = str(local_data.get("id", ""))
            if not wid:
                continue
            local_workflows.append((wf_path, local_data))

        # Filter by --workflow-id if given
        if workflow_id:
            wanted = workflow_id.casefold()
            local_workflows = [
                (p, d) for p, d in local_workflows
                if str(d.get("id", "")).casefold() == wanted
            ]

        mode_label = f"{'dry-run ' if dry_run else ''}register"
        _print_instance_header(alias, len(local_workflows), mode_label)

        for wf_path, local_data in local_workflows:
            wid = str(local_data.get("id"))
            name = local_data.get("name", "?")
            active = bool(local_data.get("active", False))
            updated_at = local_data.get("updatedAt", "?")
            rel_path = str(wf_path.relative_to(repo_root))

            existing = records.get(make_record_key(alias, wid))
            if existing is not None:
                # Already tracked — skip
                counters["CLEAN"] = counters.get("CLEAN", 0) + 1
                if _should_print_workflow_row("CLEAN", verbose):
                    _print_workflow_line(
                        "CLEAN", name, active, updated_at, rel_path,
                        workflow_id=wid,
                    )
                continue

            local_hash = local_workflow_hash(wf_path)

            if dry_run:
                counters["NEW"] = counters.get("NEW", 0) + 1
                _print_workflow_line(
                    "NEW", name, active, updated_at, rel_path,
                    workflow_id=wid,
                )
                continue

            # Create stub state record
            key = make_record_key(alias, wid)
            record = {
                "instance": alias,
                "workflowId": wid,
                "workflowName": name,
                "localPath": _normalize_path(rel_path),
                "versionId": local_data.get("versionId", "?"),
                "updatedAt": updated_at,
                "lastRemoteHash": "",
                "lastLocalHash": local_hash,
                "lastSyncAtUtc": utc_now_iso(),
                "lastDirection": "local_to_remote",
            }
            records[key] = record
            counters["NEW"] = counters.get("NEW", 0) + 1
            _print_workflow_line(
                "NEW", name, active, updated_at, rel_path,
                workflow_id=wid,
            )

        _print_instance_summary(alias, counters, dry_run)
        _merge_counters(total_counters, counters)

    if len(aliases) > 1:
        _print_summary(total_counters, dry_run)

    if not dry_run and total_counters.get("NEW", 0) > 0:
        print(f"\n  {_BOLD}Next step:{_RESET} run  push --workflow-id <id>  to create each workflow on the server.")

    return exit_code


def emit_adhoc_telemetry(
    events: List[Dict[str, Any]],
    supabase_env: Dict[str, str],
) -> List[str]:
    """Emit ad-hoc sync telemetry events to Supabase. Returns warning messages."""
    warnings: List[str] = []
    project_url = supabase_env.get("SUPABASE_PROJECT_URL", "")
    secret_key = supabase_env.get("SUPABASE_SECRET_KEY", "")
    if not project_url or not secret_key:
        return warnings
    for event in events:
        try:
            insert_supabase_row(project_url, secret_key, "n8n_adhoc_sync_events", event)
        except Exception as exc:
            warnings.append(
                f"telemetry warning: failed to log {event.get('event_type')} "
                f"for workflow {event.get('workflow_id')}: {exc}"
            )
    return warnings


def main() -> int:
    args = parse_args()
    repo_root = resolve_workspace_root(args.output_dir or None, script_path=Path(__file__))
    ensure_dirs(repo_root)
    print(f"workspace root: {repo_root}", file=sys.stderr)

    config = load_config(repo_root, dotenv_relpath=args.dotenv)
    instances = get_instances(config)
    aliases = selected_aliases(args.instance, instances.keys())
    verify_selected_instances(instances, aliases)

    # Load Supabase env for telemetry (silently skip if unavailable)
    supabase_path = Path(args.supabase_env_file).resolve() if args.supabase_env_file else repo_root / "secrets" / "supabase_env"
    supabase_env = load_key_value_file(supabase_path)

    state = load_state(repo_root)
    telemetry_events: List[Dict[str, Any]] = []

    try:
        if args.mode == "backup":
            backup_mode(repo_root, instances, aliases, args.workflow_id, args.dry_run, state, verbose=args.verbose, telemetry_events=telemetry_events, force_check=args.force_check)
        elif args.mode == "status":
            code = status_mode(repo_root, instances, aliases, state, verbose=args.verbose, force_check=args.force_check)
            if not args.dry_run:
                save_state(repo_root, state)
            return code
        elif args.mode == "push":
            push_mode(
                repo_root,
                instances,
                aliases,
                args.workflow_id,
                args.dry_run,
                state,
                force=args.force,
                verbose=args.verbose,
                telemetry_events=telemetry_events,
            )
        elif args.mode == "register":
            register_mode(
                repo_root,
                instances,
                aliases,
                args.workflow_id,
                args.dry_run,
                state,
                verbose=args.verbose,
            )
        elif args.mode == "sync-two-way":
            conflicts = sync_two_way_mode(
                repo_root,
                instances,
                aliases,
                args.workflow_id,
                args.dry_run,
                state,
                verbose=args.verbose,
                telemetry_events=telemetry_events,
                force_check=args.force_check,
            )
            if conflicts:
                print(f"conflicts={conflicts}")
                if not args.dry_run:
                    save_state(repo_root, state)
                return 2
        else:
            raise SyncError(f"Unsupported mode: {args.mode}")

        if not args.dry_run:
            save_state(repo_root, state)
    finally:
        # Emit telemetry even if the mode partially failed
        if telemetry_events and not args.dry_run:
            try:
                warnings = emit_adhoc_telemetry(telemetry_events, supabase_env)
                for w in warnings:
                    print(w, file=sys.stderr)
            except Exception as exc:
                print(f"telemetry error: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
