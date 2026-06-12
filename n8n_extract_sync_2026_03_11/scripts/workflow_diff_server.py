#!/usr/bin/env python3
"""Local diff-review server for n8n workflows.

Renders before/after workflows in n8n-demo diff mode and provides an approval
endpoint that triggers the existing n8n_sync push flow.
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from n8n_common import (
    SyncError,
    canonical_json_dumps,
    canonicalize_workflow_payload,
    find_state_record,
    get_instances,
    get_workflow,
    load_config,
    load_json,
    load_state,
    local_workflow_hash as _local_workflow_hash,
    make_record_key,
    repo_root_from_script,
    resolve_workspace_root,
    sha256_text,
    verify_instance,
)


def _auto_resolve_workflow_id(repo_root: Path, instance_alias: str) -> str:
    """Auto-detect a single locally-changed workflow from state (no API calls).

    Returns the workflow ID if exactly one locally-changed workflow is found.
    If multiple are found and stdin is a TTY, presents an interactive menu.
    Raises SyncError if zero are found or stdin is not interactive.
    """
    state = load_state(repo_root)
    records = state.get("records", {})
    candidates: list[tuple[str, str, str]] = []  # (workflowId, workflowName, localPath)

    for rec in records.values():
        if rec.get("instance") != instance_alias:
            continue
        wid = str(rec.get("workflowId", ""))
        local_path = repo_root / rec.get("localPath", "").replace("\\", "/")
        current_hash = _local_workflow_hash(local_path)
        baseline_hash = rec.get("lastLocalHash", "")
        if current_hash and baseline_hash and current_hash != baseline_hash:
            candidates.append((wid, rec.get("workflowName", "?"), rec.get("localPath", "?")))

    if len(candidates) == 0:
        raise SyncError(
            "No locally-changed workflows found. "
            "Either specify --workflow-id / --local-path, or make a local change first."
        )
    if len(candidates) == 1:
        wid, name, _ = candidates[0]
        print(f"Auto-detected single locally-changed workflow: {name} (id={wid})", file=sys.stderr)
        return wid

    # Multiple candidates — offer interactive selection if stdin is a TTY
    if sys.stdin.isatty():
        print(f"\n  Multiple locally-changed workflows found ({len(candidates)}):", file=sys.stderr)
        for idx, (wid, name, _path) in enumerate(candidates, 1):
            print(f"    {idx}. {name}  (id={wid})", file=sys.stderr)
        print(file=sys.stderr)
        while True:
            try:
                raw = input("  Select [1-{}]: ".format(len(candidates)))
            except (EOFError, KeyboardInterrupt):
                raise SyncError("Aborted.")
            raw = raw.strip()
            if not raw:
                continue
            try:
                choice = int(raw)
            except ValueError:
                print(f"  Please enter a number 1-{len(candidates)}.", file=sys.stderr)
                continue
            if 1 <= choice <= len(candidates):
                wid, name, _ = candidates[choice - 1]
                print(f"  Selected: {name} (id={wid})", file=sys.stderr)
                return wid
            print(f"  Please enter a number 1-{len(candidates)}.", file=sys.stderr)

    lines = ["Multiple locally-changed workflows found; specify one with --workflow-id:"]
    for wid, name, path in candidates:
        lines.append(f"  {name}  id={wid}  ({path})")
    raise SyncError("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a localhost diff-review utility before pushing workflow changes."
    )
    parser.add_argument(
        "-i",
        "--instance",
        choices=[
            "primary",
            "secondary",
            "tertiary",
        ],
        default="primary",
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("-wid", "--workflow-id", help="Workflow ID to review (auto-detected if exactly one locally-changed workflow)")
    group.add_argument(
        "-lp",
        "--local-path",
        help="Local path to workflow.json (used to resolve matching state record)",
    )
    parser.add_argument("-H", "--host", default="127.0.0.1")
    parser.add_argument("-p", "--port", type=int, default=8765)
    parser.add_argument("-d", "--dotenv", default="secrets/.env.n8n")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="",
        help="Repo root directory for workflows/state (default: n8n_extract_sync_2026_03_11)",
    )
    parser.add_argument(
        "--print",
        dest="print_mode",
        action="store_true",
        help="Print a semantic, three-way-classified diff report to stdout and exit (no web UI).",
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format when --print is set (default: json).",
    )
    parser.add_argument(
        "--include-raw",
        action="store_true",
        help="Include raw line-level JSON diff in --print output (off by default to save tokens).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the web browser.",
    )
    return parser.parse_args()


def payload_hash(payload: Dict[str, Any]) -> str:
    canonical = canonical_json_dumps(canonicalize_workflow_payload(payload))
    return sha256_text(canonical)


def normalize_rel_path(path_value: str) -> str:
    return path_value.replace("\\", "/").strip()


def equivalent_instance_aliases(alias: str) -> list[str]:
    return [alias]


def isoformat_file_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")


def pretty_json_text(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def build_json_diff(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    before_text = pretty_json_text(before)
    after_text = pretty_json_text(after)
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()

    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines)
    rows: list[Dict[str, Any]] = []

    def add_row(
        kind: str,
        left_number: Optional[int],
        left_text: str,
        right_number: Optional[int],
        right_text: str,
    ) -> None:
        rows.append(
            {
                "kind": kind,
                "leftNumber": left_number,
                "leftText": left_text,
                "rightNumber": right_number,
                "rightText": right_text,
            }
        )

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                add_row(
                    "context",
                    i1 + offset + 1,
                    before_lines[i1 + offset],
                    j1 + offset + 1,
                    after_lines[j1 + offset],
                )
            continue

        if tag == "replace":
            span = max(i2 - i1, j2 - j1)
            for offset in range(span):
                has_left = i1 + offset < i2
                has_right = j1 + offset < j2
                add_row(
                    "replace",
                    i1 + offset + 1 if has_left else None,
                    before_lines[i1 + offset] if has_left else "",
                    j1 + offset + 1 if has_right else None,
                    after_lines[j1 + offset] if has_right else "",
                )
            continue

        if tag == "delete":
            for offset in range(i2 - i1):
                add_row("delete", i1 + offset + 1, before_lines[i1 + offset], None, "")
            continue

        if tag == "insert":
            for offset in range(j2 - j1):
                add_row("insert", None, "", j1 + offset + 1, after_lines[j1 + offset])

    changed_indexes = [index for index, row in enumerate(rows) if row["kind"] != "context"]
    hunk_ranges: list[tuple[int, int]] = []
    if changed_indexes:
        context_lines = 3
        group_start = changed_indexes[0]
        group_end = changed_indexes[0]
        for index in changed_indexes[1:]:
            if index <= group_end + (context_lines * 2):
                group_end = index
                continue
            hunk_ranges.append((max(0, group_start - context_lines), min(len(rows) - 1, group_end + context_lines)))
            group_start = index
            group_end = index
        hunk_ranges.append((max(0, group_start - context_lines), min(len(rows) - 1, group_end + context_lines)))

    hunks: list[Dict[str, Any]] = []
    for start_row, end_row in hunk_ranges:
        start_line = rows[start_row]["leftNumber"] or rows[start_row]["rightNumber"] or 1
        end_line = rows[end_row]["leftNumber"] or rows[end_row]["rightNumber"] or start_line
        hunks.append(
            {
                "startRow": start_row,
                "endRow": end_row,
                "label": f"Lines {start_line}-{end_line}",
            }
        )

    return {
        "beforePretty": before_text,
        "afterPretty": after_text,
        "rows": rows,
        "hunks": hunks,
        "changeCount": len(changed_indexes),
    }


# ── semantic diff (LLM-friendly) ─────────────────────────────────────────


def _index_nodes_by_id(nodes: Any) -> Dict[str, Dict[str, Any]]:
    """Index n8n nodes by their stable `id` field (falls back to `name`)."""
    indexed: Dict[str, Dict[str, Any]] = {}
    if not isinstance(nodes, list):
        return indexed
    for node in nodes:
        if not isinstance(node, dict):
            continue
        key = str(node.get("id") or node.get("name") or "")
        if not key:
            continue
        indexed[key] = node
    return indexed


def _flatten(value: Any, prefix: str = "") -> Dict[str, Any]:
    """Flatten a nested dict/list into a JSON-pointer-like {path: leaf} map."""
    flat: Dict[str, Any] = {}
    if isinstance(value, dict):
        if not value:
            flat[prefix or "/"] = {}
            return flat
        for k, v in value.items():
            child = f"{prefix}/{k}" if prefix else f"/{k}"
            flat.update(_flatten(v, child))
        return flat
    if isinstance(value, list):
        if not value:
            flat[prefix or "/"] = []
            return flat
        for i, v in enumerate(value):
            child = f"{prefix}/{i}" if prefix else f"/{i}"
            flat.update(_flatten(v, child))
        return flat
    flat[prefix or "/"] = value
    return flat


def _diff_flat(before: Dict[str, Any], after: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Compute per-path changes between two flattened maps."""
    changes: list[Dict[str, Any]] = []
    keys = sorted(set(before.keys()) | set(after.keys()))
    for k in keys:
        if k not in before:
            changes.append({"path": k, "op": "add", "after": after[k]})
        elif k not in after:
            changes.append({"path": k, "op": "remove", "before": before[k]})
        elif before[k] != after[k]:
            changes.append({"path": k, "op": "change", "before": before[k], "after": after[k]})
    return changes


_NODE_TRIVIAL_KEYS = {"id", "name", "type", "typeVersion", "position"}


def _diff_node(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Compute a semantic diff between two versions of a single node."""
    before_params = before.get("parameters") if isinstance(before.get("parameters"), dict) else {}
    after_params = after.get("parameters") if isinstance(after.get("parameters"), dict) else {}
    param_changes = _diff_flat(_flatten(before_params), _flatten(after_params))

    before_creds = before.get("credentials") if isinstance(before.get("credentials"), dict) else {}
    after_creds = after.get("credentials") if isinstance(after.get("credentials"), dict) else {}
    credential_changes = _diff_flat(_flatten(before_creds), _flatten(after_creds))

    other_changes: list[Dict[str, Any]] = []
    # `name` is surfaced via the `renamed` field; `type` via `typeChanged`; skip to avoid duplication.
    for key in sorted(set(before.keys()) | set(after.keys())):
        if key in {"parameters", "credentials", "position", "name", "type"}:
            continue
        if before.get(key) != after.get(key):
            other_changes.append(
                {"path": f"/{key}", "op": "change", "before": before.get(key), "after": after.get(key)}
            )

    position_changed = before.get("position") != after.get("position")
    has_real_change = bool(param_changes or credential_changes or other_changes)

    return {
        "id": after.get("id") or before.get("id"),
        "name": after.get("name") or before.get("name"),
        "type": after.get("type") or before.get("type"),
        "renamed": (
            {"from": before.get("name"), "to": after.get("name")}
            if before.get("name") != after.get("name")
            else None
        ),
        "typeChanged": (
            {"from": before.get("type"), "to": after.get("type")}
            if before.get("type") != after.get("type")
            else None
        ),
        "positionOnly": position_changed and not has_real_change,
        "positionChanged": position_changed,
        "paramChanges": param_changes,
        "credentialChanges": credential_changes,
        "otherChanges": other_changes,
    }


def _connection_edges(connections: Any) -> list[tuple]:
    """Flatten n8n's nested connections object into a sorted list of edge tuples.

    Shape: { source_name: { output_type: [ [ {node,type,index}, ... ], ... ] } }
    Edge:  (source, source_output_type, source_index, target, target_input_type, target_index)
    """
    edges: list[tuple] = []
    if not isinstance(connections, dict):
        return edges
    for source, by_type in connections.items():
        if not isinstance(by_type, dict):
            continue
        for output_type, slots in by_type.items():
            if not isinstance(slots, list):
                continue
            for source_index, targets in enumerate(slots):
                if not isinstance(targets, list):
                    continue
                for t in targets:
                    if not isinstance(t, dict):
                        continue
                    edges.append(
                        (
                            str(source),
                            str(output_type),
                            int(source_index),
                            str(t.get("node", "")),
                            str(t.get("type", "")),
                            int(t.get("index", 0)),
                        )
                    )
    return sorted(set(edges))


def _edge_to_dict(edge: tuple) -> Dict[str, Any]:
    return {
        "from": {"node": edge[0], "outputType": edge[1], "outputIndex": edge[2]},
        "to": {"node": edge[3], "inputType": edge[4], "inputIndex": edge[5]},
    }


def build_semantic_diff(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Build an n8n-aware structured diff between two canonicalized workflow payloads."""
    before_nodes = _index_nodes_by_id(before.get("nodes"))
    after_nodes = _index_nodes_by_id(after.get("nodes"))

    added_keys = sorted(set(after_nodes) - set(before_nodes))
    removed_keys = sorted(set(before_nodes) - set(after_nodes))
    common_keys = sorted(set(before_nodes) & set(after_nodes))

    added = [
        {"id": after_nodes[k].get("id"), "name": after_nodes[k].get("name"), "type": after_nodes[k].get("type")}
        for k in added_keys
    ]
    removed = [
        {"id": before_nodes[k].get("id"), "name": before_nodes[k].get("name"), "type": before_nodes[k].get("type")}
        for k in removed_keys
    ]

    modified: list[Dict[str, Any]] = []
    renamed: list[Dict[str, Any]] = []
    position_only_moves: list[Dict[str, Any]] = []
    for k in common_keys:
        node_diff = _diff_node(before_nodes[k], after_nodes[k])
        if node_diff["renamed"]:
            renamed.append({"id": k, **node_diff["renamed"]})
        has_real = bool(
            node_diff["paramChanges"]
            or node_diff["credentialChanges"]
            or node_diff["otherChanges"]
            or node_diff["typeChanged"]
        )
        if has_real:
            modified.append(node_diff)
        elif node_diff["positionOnly"]:
            position_only_moves.append(
                {"id": k, "name": node_diff["name"], "type": node_diff["type"]}
            )

    before_edges = set(_connection_edges(before.get("connections")))
    after_edges = set(_connection_edges(after.get("connections")))
    edges_added = sorted(after_edges - before_edges)
    edges_removed = sorted(before_edges - after_edges)

    top_level_changes: list[Dict[str, Any]] = []
    for key in ("name", "active", "settings", "tags", "pinData", "staticData", "meta"):
        if before.get(key) != after.get(key):
            top_level_changes.append(
                {"path": f"/{key}", "op": "change", "before": before.get(key), "after": after.get(key)}
            )

    return {
        "summary": {
            "nodesAdded": len(added),
            "nodesRemoved": len(removed),
            "nodesModified": len(modified),
            "nodesRenamed": len(renamed),
            "nodesPositionOnly": len(position_only_moves),
            "connectionsAdded": len(edges_added),
            "connectionsRemoved": len(edges_removed),
            "topLevelChanges": len(top_level_changes),
        },
        "nodes": {
            "added": added,
            "removed": removed,
            "renamed": renamed,
            "modified": modified,
            "positionOnlyMoves": position_only_moves,
        },
        "connections": {
            "added": [_edge_to_dict(e) for e in edges_added],
            "removed": [_edge_to_dict(e) for e in edges_removed],
        },
        "topLevelChanges": top_level_changes,
    }


def classify_drift(
    remote_hash: str,
    local_hash: str,
    baseline_remote_hash: str,
    baseline_local_hash: str,
) -> Dict[str, Any]:
    """Three-way classification: where has the workflow drifted?

    Baseline = last-synced hashes recorded in .n8n_sync/state.json.
    """
    if remote_hash == local_hash:
        verdict = "in-sync"
    else:
        local_changed = baseline_local_hash and local_hash != baseline_local_hash
        remote_changed = baseline_remote_hash and remote_hash != baseline_remote_hash
        if local_changed and remote_changed:
            verdict = "conflict"
        elif local_changed and not remote_changed:
            verdict = "local-ahead"
        elif remote_changed and not local_changed:
            verdict = "server-ahead"
        elif not baseline_remote_hash and not baseline_local_hash:
            verdict = "no-baseline"
        else:
            verdict = "diverged"

    return {
        "verdict": verdict,
        "remoteHash": remote_hash,
        "localHash": local_hash,
        "baselineRemoteHash": baseline_remote_hash,
        "baselineLocalHash": baseline_local_hash,
        "localChangedSinceSync": bool(baseline_local_hash) and local_hash != baseline_local_hash,
        "remoteChangedSinceSync": bool(baseline_remote_hash) and remote_hash != baseline_remote_hash,
    }


def render_report_text(report: Dict[str, Any]) -> str:
    """Render the JSON report as compact human/LLM readable text."""
    out: list[str] = []
    drift = report["drift"]
    summary = report["semanticDiff"]["summary"]
    out.append(f"workflow: {report['workflowName']} ({report['workflowId']}) @ instance={report['instance']}")
    out.append(f"localPath: {report['localPath']}")
    out.append(f"drift: {drift['verdict']}  (local_changed={drift['localChangedSinceSync']}, remote_changed={drift['remoteChangedSinceSync']})")
    out.append(
        "summary: "
        f"+{summary['nodesAdded']} nodes / -{summary['nodesRemoved']} / ~{summary['nodesModified']} "
        f"(renamed={summary['nodesRenamed']}, moves={summary['nodesPositionOnly']})  "
        f"edges +{summary['connectionsAdded']} -{summary['connectionsRemoved']}  "
        f"top-level={summary['topLevelChanges']}"
    )
    nodes = report["semanticDiff"]["nodes"]
    if nodes["added"]:
        out.append("added nodes:")
        for n in nodes["added"]:
            out.append(f"  + {n.get('name')} [{n.get('type')}]")
    if nodes["removed"]:
        out.append("removed nodes:")
        for n in nodes["removed"]:
            out.append(f"  - {n.get('name')} [{n.get('type')}]")
    if nodes["renamed"]:
        out.append("renamed nodes:")
        for n in nodes["renamed"]:
            out.append(f"  ~ {n.get('from')} -> {n.get('to')}")
    if nodes["modified"]:
        out.append("modified nodes:")
        for n in nodes["modified"]:
            out.append(f"  ~ {n.get('name')} [{n.get('type')}]")
            for c in n.get("paramChanges", []):
                out.append(f"      params {c['op']} {c['path']}")
            for c in n.get("credentialChanges", []):
                out.append(f"      creds  {c['op']} {c['path']}")
            for c in n.get("otherChanges", []):
                out.append(f"      other  {c['op']} {c['path']}")
            if n.get("typeChanged"):
                out.append(f"      type   {n['typeChanged']['from']} -> {n['typeChanged']['to']}")
    conns = report["semanticDiff"]["connections"]
    if conns["added"] or conns["removed"]:
        out.append("connection edges:")
        for e in conns["added"]:
            out.append(f"  + {e['from']['node']}[{e['from']['outputIndex']}] -> {e['to']['node']}[{e['to']['inputIndex']}]")
        for e in conns["removed"]:
            out.append(f"  - {e['from']['node']}[{e['from']['outputIndex']}] -> {e['to']['node']}[{e['to']['inputIndex']}]")
    for c in report["semanticDiff"]["topLevelChanges"]:
        out.append(f"top-level {c['op']} {c['path']}: {c.get('before')!r} -> {c.get('after')!r}")
    return "\n".join(out) + "\n"


class DiffReviewApp:
    def __init__(
        self,
        repo_root: Path,
        instance_alias: str,
        workflow_id: Optional[str],
        local_path: Optional[str],
        dotenv_path: str,
    ) -> None:
        self.repo_root = repo_root
        self.tool_root = repo_root_from_script(Path(__file__))
        self.instance_alias = instance_alias
        self.workflow_id = workflow_id
        if local_path:
            candidate = (repo_root / local_path).resolve()
            if candidate.is_dir():
                wf_file = candidate / "workflow.json"
                if wf_file.is_file():
                    local_path = str(wf_file.relative_to(repo_root))
        self.local_path = local_path
        self.dotenv_path = dotenv_path
        self.web_root = self.tool_root / "web"
        if not self.web_root.exists():
            self.web_root = repo_root / "web"

        config = load_config(repo_root, dotenv_relpath=dotenv_path)
        instances = get_instances(config)
        if instance_alias not in instances:
            raise SyncError(f"Instance '{instance_alias}' is not configured.")
        self.instance = instances[instance_alias]
        ok, msg = verify_instance(self.instance)
        if not ok:
            raise SyncError(f"Instance check failed for '{instance_alias}': {msg}")

    def _resolve_record(self) -> Dict[str, Any]:
        state = load_state(self.repo_root)
        records = state.get("records", {})
        if not isinstance(records, dict):
            raise SyncError("Invalid .n8n_sync/state.json: 'records' is not an object.")

        aliases = equivalent_instance_aliases(self.instance_alias)

        if self.workflow_id:
            rec = None
            for alias in aliases:
                rec = find_state_record(records, alias, self.workflow_id)
                if rec:
                    break
            if not rec:
                raise SyncError(
                    f"No state record found for '{self.instance_alias}:{self.workflow_id}'. "
                    "Run backup first to track this workflow."
                )
            return rec

        if not self.local_path:
            raise SyncError("Either --workflow-id or --local-path is required.")

        target = (self.repo_root / self.local_path).resolve()
        for rec in records.values():
            if rec.get("instance") not in aliases:
                continue
            rel = normalize_rel_path(str(rec.get("localPath", "")))
            candidate = (self.repo_root / rel).resolve()
            if candidate == target:
                return rec

        raise SyncError(
            f"No state record found for local path '{self.local_path}' on instance '{self.instance_alias}'."
        )

    def _resolve_local_file(self, rec: Dict[str, Any]) -> Path:
        rel = normalize_rel_path(str(rec.get("localPath", "")))
        if not rel:
            raise SyncError("State record has no localPath.")
        local_file = (self.repo_root / rel).resolve()
        if not local_file.exists():
            raise SyncError(f"Local workflow file not found: {local_file}")
        return local_file

    def context(self) -> Dict[str, Any]:
        rec = self._resolve_record()
        workflow_id = str(rec.get("workflowId"))
        local_file = self._resolve_local_file(rec)

        before = get_workflow(self.instance, workflow_id)
        after = load_json(local_file, fallback={})
        if not isinstance(after, dict) or not after:
            raise SyncError(f"Invalid local JSON payload: {local_file}")

        before_hash = payload_hash(before)
        after_hash = payload_hash(after)

        return {
            "instance": self.instance_alias,
            "workflowId": workflow_id,
            "workflowName": after.get("name") or before.get("name") or rec.get("workflowName"),
            "localPath": str(local_file.relative_to(self.repo_root)).replace("\\", "/"),
            "remoteUpdatedAt": before.get("updatedAt"),
            "localUpdatedAt": isoformat_file_mtime(local_file),
            "remoteWorkflowUrl": f"{self.instance.base_url}/workflow/{workflow_id}",
            "beforeHash": before_hash,
            "afterHash": after_hash,
            "isDifferent": before_hash != after_hash,
            "before": before,
            "after": after,
            "jsonDiff": build_json_diff(before, after),
        }

    def report(self, include_raw: bool = False) -> Dict[str, Any]:
        """Build a semantic, three-way-classified diff report suitable for LLM consumption."""
        rec = self._resolve_record()
        workflow_id = str(rec.get("workflowId"))
        local_file = self._resolve_local_file(rec)

        remote = get_workflow(self.instance, workflow_id)
        local = load_json(local_file, fallback={})
        if not isinstance(local, dict) or not local:
            raise SyncError(f"Invalid local JSON payload: {local_file}")

        remote_canon = canonicalize_workflow_payload(remote)
        local_canon = canonicalize_workflow_payload(local)
        remote_hash = sha256_text(canonical_json_dumps(remote_canon))
        local_hash = sha256_text(canonical_json_dumps(local_canon))

        baseline_remote = str(rec.get("lastRemoteHash", "") or "")
        baseline_local = str(rec.get("lastLocalHash", "") or "")

        drift = classify_drift(remote_hash, local_hash, baseline_remote, baseline_local)
        semantic = build_semantic_diff(remote_canon, local_canon)

        report: Dict[str, Any] = {
            "ok": True,
            "instance": self.instance_alias,
            "workflowId": workflow_id,
            "workflowName": local.get("name") or remote.get("name") or rec.get("workflowName"),
            "localPath": str(local_file.relative_to(self.repo_root)).replace("\\", "/"),
            "remoteWorkflowUrl": f"{self.instance.base_url}/workflow/{workflow_id}",
            "remoteUpdatedAt": remote.get("updatedAt"),
            "localUpdatedAt": isoformat_file_mtime(local_file),
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "drift": drift,
            "semanticDiff": semantic,
        }
        if include_raw:
            report["rawJsonDiff"] = build_json_diff(remote, local)
        return report

    def approve(self, expected_remote_hash: str, force: bool = False) -> Dict[str, Any]:
        rec = self._resolve_record()
        workflow_id = str(rec.get("workflowId"))
        fresh_remote = get_workflow(self.instance, workflow_id)
        fresh_remote_hash = payload_hash(fresh_remote)
        if expected_remote_hash and fresh_remote_hash != expected_remote_hash and not force:
            raise SyncError(
                "Remote workflow changed after review load. Reload and review again before approving."
            )

        cmd = [
            sys.executable,
            str(self.tool_root / "scripts" / "n8n_sync.py"),
            "--mode",
            "push",
            "--instance",
            self.instance_alias,
            "--workflow-id",
            workflow_id,
            *(["--force"] if force else []),
            "--output-dir",
            str(self.repo_root),
            "--dotenv",
            self.dotenv_path,
        ]
        result = subprocess.run(
            cmd,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise SyncError(
                "Push command failed.\n"
                f"stdout:\n{result.stdout.strip() or '(empty)'}\n\n"
                f"stderr:\n{result.stderr.strip() or '(empty)'}"
            )

        return {
            "ok": True,
            "command": " ".join(cmd),
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }


class RequestHandler(BaseHTTPRequestHandler):
    app: DiffReviewApp

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _write_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _write_file(self, code: int, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._write_file(200, self.app.web_root / "diff_review.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/api/context":
            try:
                self._write_json(200, self.app.context())
            except Exception as exc:  # pragma: no cover
                self._write_json(500, {"ok": False, "error": str(exc)})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/approve":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._write_json(400, {"ok": False, "error": "Invalid JSON body."})
            return

        expected_remote_hash = str(payload.get("expectedRemoteHash", "")).strip()
        force = bool(payload.get("force", False))
        try:
            result = self.app.approve(expected_remote_hash=expected_remote_hash, force=force)
        except SyncError as exc:
            message = str(exc)
            if "changed after review load" in message:
                self._write_json(409, {"ok": False, "error": message})
            else:
                self._write_json(500, {"ok": False, "error": message})
            return
        except Exception as exc:  # pragma: no cover
            self._write_json(500, {"ok": False, "error": str(exc)})
            return

        self._write_json(200, result)


def main() -> int:
    args = parse_args()
    repo_root = resolve_workspace_root(args.output_dir or None, script_path=Path(__file__))
    print(f"workspace root: {repo_root}", file=sys.stderr)

    # Auto-resolve workflow ID when neither --workflow-id nor --local-path is given
    workflow_id = args.workflow_id
    local_path = args.local_path
    if not workflow_id and not local_path:
        workflow_id = _auto_resolve_workflow_id(repo_root, args.instance)

    app = DiffReviewApp(
        repo_root=repo_root,
        instance_alias=args.instance,
        workflow_id=workflow_id,
        local_path=local_path,
        dotenv_path=args.dotenv,
    )

    if args.print_mode:
        report = app.report(include_raw=args.include_raw)
        if args.format == "text":
            sys.stdout.write(render_report_text(report))
        else:
            sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        return 0

    RequestHandler.app = app
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    url = f"http://{args.host}:{args.port}"
    print(
        f"Diff review server running at {url} "
        f"(instance={args.instance}, workflow={workflow_id or local_path})"
    )

    if not args.no_browser:
        browser_host = args.host if args.host != "0.0.0.0" else "127.0.0.1"
        webbrowser.open(f"http://{browser_host}:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
