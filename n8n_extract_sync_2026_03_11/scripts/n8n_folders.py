#!/usr/bin/env python3
"""Folder-management helpers for the n8n CLI.

Three modes:
  folders   List all folders in the instance project.
  unfiled   List workflows whose folderId is null/empty.
  move      Move a workflow into a folder (resolves folder by id or name).

Reuses n8n_common helpers so it reads the API key from the same .env.n8n file
as the rest of the CLI. No third-party deps — stdlib urllib/json only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from n8n_common import (
    InstanceConfig,
    SyncError,
    get_instances,
    http_json_request,
    join_url,
    list_workflows,
    load_config,
    resolve_workspace_root,
)


# ── folder helpers ──────────────────────────────────────────────────────

def list_folders(instance: InstanceConfig) -> Tuple[List[Dict[str, Any]], str]:
    """Return (folders, endpoint_label) trying the public API first, then the
    internal REST endpoint. The first endpoint that returns a list wins.
    """
    endpoints: List[Tuple[str, str]] = [
        (join_url(instance.base_url, "/api/v1/folders"), "public /api/v1/folders"),
        (join_url(instance.base_url, "/rest/folders"), "internal /rest/folders"),
    ]
    last_error: Optional[str] = None
    for url, label in endpoints:
        try:
            response = http_json_request("GET", url, instance.api_key)
        except SyncError as exc:
            last_error = str(exc)
            continue
        items = _extract_folder_items(response)
        if items is not None:
            return items, label
        # Response was non-list/empty-shaped; keep the last_error neutral and
        # try the next endpoint.
        last_error = last_error or f"{label} returned non-list response"
    raise SyncError(
        f"Could not list folders from either endpoint. Last error: {last_error}"
    )


def _extract_folder_items(response: Any) -> Optional[List[Dict[str, Any]]]:
    """Normalize a folders response into a list, or None if the shape is unknown."""
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        for key in ("data", "items", "folders"):
            value = response.get(key)
            if isinstance(value, list):
                return value
    return None


def _folder_id_of(folder: Dict[str, Any]) -> str:
    return str(folder.get("id") or folder.get("folderId") or "")


def _folder_name_of(folder: Dict[str, Any]) -> str:
    return str(folder.get("name") or "")


def _folder_parent_of(folder: Dict[str, Any]) -> str:
    parent = folder.get("parentFolderId")
    if parent is None or parent == "":
        return "-"
    return str(parent)


def resolve_folder(
    folders: List[Dict[str, Any]], name_or_id: str
) -> Dict[str, Any]:
    """Resolve a folder by exact id, case-insensitive exact name, then
    case-insensitive partial name. Raise SyncError on ambiguity or no match.
    """
    if not name_or_id:
        raise SyncError("--folder is required.")

    target = name_or_id.strip()
    target_cf = target.casefold()

    # 1. Exact id match.
    for folder in folders:
        if _folder_id_of(folder) == target:
            return folder

    # 2. Case-insensitive exact name match.
    exact_name_matches = [
        folder for folder in folders
        if _folder_name_of(folder).casefold() == target_cf
    ]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0]
    if len(exact_name_matches) > 1:
        _raise_ambiguous(target, exact_name_matches, "exact name")

    # 3. Case-insensitive partial name match (substring).
    partial_matches = [
        folder for folder in folders
        if target_cf in _folder_name_of(folder).casefold()
    ]
    if len(partial_matches) == 1:
        return partial_matches[0]
    if len(partial_matches) > 1:
        _raise_ambiguous(target, partial_matches, "partial name")

    raise SyncError(
        f"No folder matched '{name_or_id}'. Available folders: "
        + ", ".join(f"{_folder_name_of(f)} ({_folder_id_of(f)})" for f in folders)
        if folders
        else f"No folders found on the instance; cannot resolve '{name_or_id}'."
    )


def _raise_ambiguous(target: str, matches: List[Dict[str, Any]], kind: str) -> None:
    candidates = ", ".join(
        f"{_folder_name_of(f)} ({_folder_id_of(f)})" for f in matches
    )
    raise SyncError(
        f"Ambiguous folder {kind} '{target}' matched {len(matches)} folders: {candidates}. "
        "Pass the folder ID instead."
    )


# ── move helpers ─────────────────────────────────────────────────────────

def _build_move_payload(workflow: Dict[str, Any], folder_id: str) -> Dict[str, Any]:
    """Build a PUT payload with the fields n8n requires plus folderId."""
    payload: Dict[str, Any] = {}
    for key in ("name", "nodes", "connections", "settings", "staticData"):
        if key in workflow:
            payload[key] = json.loads(json.dumps(workflow[key]))
    payload["folderId"] = folder_id
    return payload


def move_workflow_via_public_api(
    instance: InstanceConfig, workflow_id: str, workflow: Dict[str, Any], folder_id: str
) -> Dict[str, Any]:
    """PUT /api/v1/workflows/{id} with the full payload + folderId."""
    from n8n_common import update_workflow

    payload = _build_move_payload(workflow, folder_id)
    return update_workflow(instance, workflow_id, payload)


def move_workflow_via_internal_patch(
    instance: InstanceConfig, workflow_id: str, folder_id: str
) -> Dict[str, Any]:
    """PATCH /rest/workflows/{id} with {"folderId": "<id>"}."""
    url = join_url(instance.base_url, f"/rest/workflows/{workflow_id}")
    return http_json_request("PATCH", url, instance.api_key, payload={"folderId": folder_id})


def move_workflow_via_internal_move(
    instance: InstanceConfig, workflow_id: str, folder_id: str
) -> Dict[str, Any]:
    """POST /rest/workflows/{id}/move with {"folderId": "<id>"}."""
    url = join_url(instance.base_url, f"/rest/workflows/{workflow_id}/move")
    return http_json_request("POST", url, instance.api_key, payload={"folderId": folder_id})


def move_workflow(
    instance: InstanceConfig,
    workflow_id: str,
    folder_id: str,
    *,
    workflow: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], str]:
    """Try the public PUT first, then internal PATCH, then internal move endpoint.

    Returns (response, method_label).
    """
    if workflow is None:
        from n8n_common import get_workflow
        workflow = get_workflow(instance, workflow_id)

    errors: List[str] = []
    try:
        result = move_workflow_via_public_api(instance, workflow_id, workflow, folder_id)
        return result, "public PUT /api/v1/workflows/{id}"
    except SyncError as exc:
        errors.append(f"public PUT: {exc}")

    try:
        result = move_workflow_via_internal_patch(instance, workflow_id, folder_id)
        return result, "internal PATCH /rest/workflows/{id}"
    except SyncError as exc:
        errors.append(f"internal PATCH: {exc}")

    try:
        result = move_workflow_via_internal_move(instance, workflow_id, folder_id)
        return result, "internal POST /rest/workflows/{id}/move"
    except SyncError as exc:
        errors.append(f"internal move: {exc}")

    raise SyncError(
        "All move methods failed:\n  - " + "\n  - ".join(errors)
    )


# ── formatters ───────────────────────────────────────────────────────────

def format_folders_table(folders: List[Dict[str, Any]]) -> str:
    if not folders:
        return "No folders found."
    rows = []
    for folder in folders:
        fid = _folder_id_of(folder)
        name = _folder_name_of(folder) or "?"
        parent = _folder_parent_of(folder)
        rows.append(f"  {fid:<24} {name:<40} {parent}")
    header = f"  {'ID':<24} {'Name':<40} {'ParentFolderId'}"
    return header + "\n  " + "-" * len(header) + "\n" + "\n".join(rows)


def format_unfiled_table(workflows: List[Dict[str, Any]]) -> str:
    if not workflows:
        return "No unfiled workflows found."
    rows = []
    for wf in workflows:
        wid = str(wf.get("id") or "?")
        name = str(wf.get("name") or "?")
        rows.append(f"  {wid:<24} {name}")
    header = f"  {'ID':<24} {'Name'}"
    return header + "\n  " + "-" * len(header) + "\n" + "\n".join(rows)


def _source_folder_label(
    folders: List[Dict[str, Any]], folder_id: Optional[str]
) -> str:
    if not folder_id:
        return "unfiled"
    folder_map = {_folder_id_of(f): f for f in folders}
    folder = folder_map.get(str(folder_id))
    if folder:
        return f"{_folder_name_of(folder)} ({_folder_id_of(folder)})"
    return str(folder_id)


# ── CLI entry points ─────────────────────────────────────────────────────

def _load_instance(args: argparse.Namespace) -> InstanceConfig:
    config = load_config(resolve_workspace_root(None, Path(__file__).parent.parent), args.dotenv)
    instances = get_instances(config)
    inst = instances.get(args.instance)
    if not inst:
        raise SyncError(
            f"Instance '{args.instance}' not found. Available: {', '.join(instances)}"
        )
    return inst


def cmd_folders(args: argparse.Namespace) -> None:
    inst = _load_instance(args)
    folders, endpoint = list_folders(inst)
    if args.format == "json":
        print(json.dumps({"endpoint": endpoint, "folders": folders}, indent=2, ensure_ascii=False))
        return
    print(f"Folders (via {endpoint}):")
    print(format_folders_table(folders))


def cmd_unfiled(args: argparse.Namespace) -> None:
    inst = _load_instance(args)
    workflows = list_workflows(inst)
    unfiled = [
        wf for wf in workflows
        if not wf.get("folderId")
    ]
    if args.format == "json":
        print(json.dumps(unfiled, indent=2, ensure_ascii=False))
        return
    print(f"Unfiled workflows ({len(unfiled)}):")
    print(format_unfiled_table(unfiled))


def cmd_move(args: argparse.Namespace) -> None:
    inst = _load_instance(args)
    if not args.workflow_id:
        raise SyncError("--workflow-id is required.")
    if not args.folder:
        raise SyncError("--folder is required.")

    folders, _ = list_folders(inst)
    target_folder = resolve_folder(folders, args.folder)
    target_folder_id = _folder_id_of(target_folder)
    target_folder_name = _folder_name_of(target_folder)

    workflows = list_workflows(inst)
    workflow_summary = next(
        (wf for wf in workflows if str(wf.get("id")) == str(args.workflow_id)),
        None,
    )
    if not workflow_summary:
        raise SyncError(f"Workflow '{args.workflow_id}' not found on instance '{args.instance}'.")

    workflow_name = str(workflow_summary.get("name") or "?")
    source_label = _source_folder_label(folders, workflow_summary.get("folderId"))

    print(f"Workflow : {workflow_name} ({args.workflow_id})")
    print(f"Source   : {source_label}")
    print(f"Target   : {target_folder_name} ({target_folder_id})")

    if args.dry_run:
        print("[dry-run] Would move the workflow into the target folder above. No API call made.")
        return

    result, method = move_workflow(inst, args.workflow_id, target_folder_id)
    print(f"Moved via {method}.")
    new_folder_id = result.get("folderId") if isinstance(result, dict) else None
    if new_folder_id:
        print(f"Confirmed folderId: {new_folder_id}")


# ── arg parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="n8n folder management: folders / unfiled / move"
    )
    parser.add_argument("--mode", required=True,
                        choices=["folders", "unfiled", "move"])
    parser.add_argument("--instance", default="primary", help="Instance alias (default: primary)")
    parser.add_argument("--dotenv", default="./secrets/.env.n8n", help="Path to .env.n8n file")
    parser.add_argument("--format", choices=["text", "json"], default="text",
                        help="Output format (default: text)")
    parser.add_argument("--workflow-id", help="(move) Workflow ID to move")
    parser.add_argument("--folder", help="(move) Target folder ID or name")
    parser.add_argument("--dry-run", action="store_true",
                        help="(move) Print the planned move without calling the API")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "folders": cmd_folders,
        "unfiled": cmd_unfiled,
        "move": cmd_move,
    }
    try:
        dispatch[args.mode](args)
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
