#!/usr/bin/env python3
"""Lightweight REST wrapper for n8n execution logs, activate, deactivate, retry, and stop.

Uses n8n_common helpers (load_config, get_instances, http_json_request, join_url)
so it reads the API key from the same .env.n8n file as the rest of the CLI.

Modes:
  executions   Query execution logs for a workflow or a single execution
  activate     Activate a workflow on the n8n instance
  deactivate   Deactivate a workflow on the n8n instance
  retry        Retry a failed execution (optionally with the latest saved workflow)
  stop         Stop a running execution
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from n8n_common import (
    InstanceConfig,
    SyncError,
    get_instances,
    http_json_request,
    join_url,
    load_config,
    resolve_workspace_root,
)


# ── execution helpers ────────────────────────────────────────────────────

def list_executions(
    instance: InstanceConfig,
    *,
    workflow_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"limit": limit}
    if workflow_id:
        query["workflowId"] = workflow_id
    if status:
        query["status"] = status
    url = join_url(instance.base_url, "/api/v1/executions", query=query)
    response = http_json_request("GET", url, instance.api_key)
    items = response.get("data", [])
    if isinstance(response, list):
        items = response
    return items


def get_execution(
    instance: InstanceConfig,
    execution_id: str,
    *,
    include_data: bool = False,
) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if include_data:
        query["includeData"] = "true"
    url = join_url(instance.base_url, f"/api/v1/executions/{execution_id}", query=query or None)
    return http_json_request("GET", url, instance.api_key)


# ── activate / deactivate helpers ────────────────────────────────────────

def activate_workflow(instance: InstanceConfig, workflow_id: str) -> Dict[str, Any]:
    url = join_url(instance.base_url, f"/api/v1/workflows/{workflow_id}/activate")
    return http_json_request("POST", url, instance.api_key)


def deactivate_workflow(instance: InstanceConfig, workflow_id: str) -> Dict[str, Any]:
    url = join_url(instance.base_url, f"/api/v1/workflows/{workflow_id}/deactivate")
    return http_json_request("POST", url, instance.api_key)


# ── retry / stop helpers ──────────────────────────────────────────────────

def retry_execution(
    instance: InstanceConfig,
    execution_id: str,
    *,
    load_workflow: bool = True,
) -> Dict[str, Any]:
    """Retry an execution, optionally with the latest saved workflow version.

    load_workflow=True (default) replays the original trigger data against the
    CURRENTLY SAVED workflow, which is what the fix-and-rerun loop needs.
    """
    url = join_url(instance.base_url, f"/api/v1/executions/{execution_id}/retry")
    return http_json_request("POST", url, instance.api_key, payload={"loadWorkflow": load_workflow})


def stop_execution(instance: InstanceConfig, execution_id: str) -> Dict[str, Any]:
    url = join_url(instance.base_url, f"/api/v1/executions/{execution_id}/stop")
    return http_json_request("POST", url, instance.api_key)


# ── formatters ────────────────────────────────────────────────────────────

def format_executions_table(executions: List[Dict[str, Any]]) -> str:
    if not executions:
        return "No executions found."
    rows = []
    for ex in executions:
        ex_id = str(ex.get("id") or "?")
        status = str(ex.get("status") or "?")
        wf_name = "?"
        wf_data = ex.get("workflowData") or ex.get("workflow") or {}
        if isinstance(wf_data, dict):
            wf_name = str(wf_data.get("name") or wf_data.get("id") or "?")
        started_raw = ex.get("startedAt") or ex.get("createdAt")
        started = str(started_raw) if started_raw else "?"
        stopped = ex.get("stoppedAt")
        duration = ""
        if started_raw and stopped:
            try:
                from datetime import datetime as _dt
                s = _dt.fromisoformat(str(started_raw).replace("Z", "+00:00"))
                e = _dt.fromisoformat(str(stopped).replace("Z", "+00:00"))
                delta = e - s
                duration = f"{delta.total_seconds():.1f}s"
            except Exception:
                duration = "?"
        rows.append(f"  {ex_id:<12} {status:<10} {wf_name:<40} {started:<26} {duration}")
    header = f"  {'ID':<12} {'Status':<10} {'Workflow':<40} {'Started':<26} {'Duration'}"
    return header + "\n  " + "-" * len(header) + "\n" + "\n".join(rows)


# ── CLI entry points ─────────────────────────────────────────────────────

def cmd_executions(args: argparse.Namespace) -> None:
    config = load_config(resolve_workspace_root(None, Path(__file__).parent.parent), args.dotenv)
    instances = get_instances(config)
    inst = instances.get(args.instance)
    if not inst:
        raise SyncError(f"Instance '{args.instance}' not found. Available: {', '.join(instances)}")

    if args.execution_id:
        result = get_execution(inst, args.execution_id, include_data=args.include_data)
        if args.format == "json":
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if not args.workflow_id:
            raise SyncError("Either --workflow-id or --execution-id is required.")
        results = list_executions(
            inst,
            workflow_id=args.workflow_id,
            status=args.status,
            limit=args.limit,
        )
        if args.format == "json":
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            print(format_executions_table(results))


def cmd_activate(args: argparse.Namespace) -> None:
    config = load_config(resolve_workspace_root(None, Path(__file__).parent.parent), args.dotenv)
    instances = get_instances(config)
    inst = instances.get(args.instance)
    if not inst:
        raise SyncError(f"Instance '{args.instance}' not found. Available: {', '.join(instances)}")
    if not args.workflow_id:
        raise SyncError("--workflow-id is required.")
    result = activate_workflow(inst, args.workflow_id)
    name = result.get("name", args.workflow_id)
    active = result.get("active", False)
    status = "ACTIVE" if active else "INACTIVE"
    print(f"Workflow '{name}' ({args.workflow_id}) is now {status}")


def cmd_deactivate(args: argparse.Namespace) -> None:
    config = load_config(resolve_workspace_root(None, Path(__file__).parent.parent), args.dotenv)
    instances = get_instances(config)
    inst = instances.get(args.instance)
    if not inst:
        raise SyncError(f"Instance '{args.instance}' not found. Available: {', '.join(instances)}")
    if not args.workflow_id:
        raise SyncError("--workflow-id is required.")
    result = deactivate_workflow(inst, args.workflow_id)
    name = result.get("name", args.workflow_id)
    active = result.get("active", False)
    status = "ACTIVE" if active else "INACTIVE"
    print(f"Workflow '{name}' ({args.workflow_id}) is now {status}")


def cmd_retry(args: argparse.Namespace) -> None:
    config = load_config(resolve_workspace_root(None, Path(__file__).parent.parent), args.dotenv)
    instances = get_instances(config)
    inst = instances.get(args.instance)
    if not inst:
        raise SyncError(f"Instance '{args.instance}' not found. Available: {', '.join(instances)}")
    if not args.execution_id:
        raise SyncError("--execution-id is required.")

    load_wf = args.load_workflow
    url = join_url(inst.base_url, f"/api/v1/executions/{args.execution_id}/retry")
    payload = {"loadWorkflow": load_wf}

    if args.dry_run:
        print(f"[dry-run] POST {url}")
        print(f"[dry-run] payload: {json.dumps(payload)}")
        print(f"[dry-run] loadWorkflow={load_wf} (latest saved workflow)" if load_wf
              else f"[dry-run] loadWorkflow={load_wf} (execution-time workflow)")
        return

    result = retry_execution(inst, args.execution_id, load_workflow=load_wf)
    new_id = result.get("id", "?")
    status = result.get("status", "?")
    finished = result.get("finished", "?")
    mode = "latest saved workflow" if load_wf else "execution-time workflow"
    print(f"Retried execution {args.execution_id} -> new execution {new_id}  "
          f"status={status}  finished={finished}  loadWorkflow={load_wf} ({mode})")


def cmd_stop(args: argparse.Namespace) -> None:
    config = load_config(resolve_workspace_root(None, Path(__file__).parent.parent), args.dotenv)
    instances = get_instances(config)
    inst = instances.get(args.instance)
    if not inst:
        raise SyncError(f"Instance '{args.instance}' not found. Available: {', '.join(instances)}")
    if not args.execution_id:
        raise SyncError("--execution-id is required.")

    result = stop_execution(inst, args.execution_id)
    status = result.get("status", "?")
    print(f"Stopped execution {args.execution_id}  status={status}")


# ── arg parser ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="n8n executions / activate / deactivate / retry / stop")
    parser.add_argument("--mode", required=True,
                        choices=["executions", "activate", "deactivate", "retry", "stop"])
    parser.add_argument("--instance", default="primary", help="Instance alias (default: primary)")
    parser.add_argument("--dotenv", default="./secrets/.env.n8n", help="Path to .env.n8n file")

    # executions flags
    parser.add_argument("--workflow-id", help="Workflow ID to filter executions by")
    parser.add_argument("--execution-id", help="Single execution ID to fetch / retry / stop")
    parser.add_argument("--status", choices=["error", "success", "waiting"], help="Filter by status")
    parser.add_argument("--limit", type=int, default=10, help="Max executions to return (default: 10)")
    parser.add_argument("--include-data", action="store_true", help="Include full execution data (for --execution-id)")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format (default: text)")

    # retry flags
    parser.add_argument("--dry-run", action="store_true", help="(retry) Print the request without calling the API")
    parser.add_argument("--load-workflow", dest="load_workflow", action="store_true", default=True,
                        help="(retry) Use the latest saved workflow version (default: True)")
    parser.add_argument("--no-load-workflow", dest="load_workflow", action="store_false",
                        help="(retry) Use the workflow version captured at execution time")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "executions": cmd_executions,
        "activate": cmd_activate,
        "deactivate": cmd_deactivate,
        "retry": cmd_retry,
        "stop": cmd_stop,
    }
    try:
        dispatch[args.mode](args)
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
