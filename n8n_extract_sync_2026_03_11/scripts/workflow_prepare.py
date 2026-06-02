#!/usr/bin/env python3
"""Mirror top-level workflow structure into activeVersion and validate JSON."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from n8n_common import SyncError, find_state_record, load_state, resolve_workspace_root


MIRRORED_KEYS = ("nodes", "connections")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a local workflow JSON file and mirror top-level workflow "
            "structure into activeVersion."
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-wid", "--workflow-id", help="Workflow ID tracked in .n8n_sync/state.json")
    group.add_argument("--local-path", help="Path to workflow.json, relative to workspace root or absolute")
    parser.add_argument("--instance", default="primary", help="Instance alias for --workflow-id lookup")
    parser.add_argument(
        "--workspace-root",
        help="Explicit workspace root. Defaults to the nearest parent with workflows/ and .n8n_sync/.",
    )
    parser.add_argument(
        "--dotenv",
        default="./secrets/.env.n8n",
        help="Accepted for wrapper compatibility. Not used by this local-only command.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and report pending mirror changes without writing the file.",
    )
    return parser.parse_args(argv)


def load_json_with_context(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        lines = text.splitlines()
        start = max(exc.lineno - 2, 1)
        end = min(exc.lineno + 1, len(lines))
        snippet = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1))
        raise SyncError(
            f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}\n{snippet}"
        ) from exc
    if not isinstance(payload, dict):
        raise SyncError(f"Workflow file must contain a top-level JSON object: {path}")
    return payload


def resolve_workflow_path(args: argparse.Namespace, workspace_root: Path) -> Path:
    if args.local_path:
        path = Path(args.local_path)
        if not path.is_absolute():
            path = (workspace_root / path).resolve()
        return path

    state = load_state(workspace_root)
    records = state.get("records", {})
    if not isinstance(records, dict):
        raise SyncError("Invalid .n8n_sync/state.json: 'records' is not an object.")
    record = find_state_record(records, args.instance, str(args.workflow_id))
    if not record:
        raise SyncError(
            f"Workflow ID {args.workflow_id!r} not found for instance {args.instance!r} in .n8n_sync/state.json."
        )
    local_path = record.get("localPath")
    if not local_path:
        raise SyncError(f"State record for workflow {args.workflow_id!r} has no localPath.")
    return (workspace_root / local_path).resolve()


def summarize_workflow(payload: Dict[str, Any], path: Path, workflow_id: Optional[str]) -> str:
    name = payload.get("name") or path.parent.name
    wid = payload.get("id") or workflow_id or "unknown-id"
    return f"{name} ({wid})"


def ensure_active_version(payload: Dict[str, Any]) -> Dict[str, Any]:
    active_version = payload.get("activeVersion")
    if active_version is None:
        active_version = {}
        payload["activeVersion"] = active_version
    if not isinstance(active_version, dict):
        raise SyncError("workflow.activeVersion must be an object when present.")
    return active_version


def validate_top_level_shape(payload: Dict[str, Any]) -> None:
    if not isinstance(payload.get("nodes"), list):
        raise SyncError("workflow.nodes must be an array.")
    if not isinstance(payload.get("connections"), dict):
        raise SyncError("workflow.connections must be an object.")


def mirror_active_version(payload: Dict[str, Any]) -> List[str]:
    validate_top_level_shape(payload)
    active_version = ensure_active_version(payload)
    changed: List[str] = []
    for key in MIRRORED_KEYS:
        if key not in payload:
            continue
        new_value = copy.deepcopy(payload[key])
        if active_version.get(key) != new_value:
            active_version[key] = new_value
            changed.append(f"activeVersion.{key}")
    return changed


def write_payload(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    workspace_root = resolve_workspace_root(args.workspace_root, Path(__file__))
    workflow_path = resolve_workflow_path(args, workspace_root)
    if not workflow_path.exists():
        raise SyncError(f"Workflow file does not exist: {workflow_path}")

    payload = load_json_with_context(workflow_path)
    summary = summarize_workflow(payload, workflow_path, args.workflow_id)
    changed = mirror_active_version(payload)

    print(f"workflow: {summary}")
    print(f"localPath: {workflow_path.relative_to(workspace_root)}")

    if args.check:
        if changed:
            print("result: mirror-needed")
            print("changes:")
            for entry in changed:
                print(f"  - {entry}")
            return 2
        print("result: already-in-sync")
        return 0

    if changed:
        write_payload(workflow_path, payload)
        print("result: updated")
        print("mirrored:")
        for entry in changed:
            print(f"  - {entry}")
    else:
        print("result: already-in-sync")
    print("validation: ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
