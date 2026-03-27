#!/usr/bin/env python3
"""One-time migration from cloud instances into the new primary."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

from n8n_common import (
    SyncError,
    create_workflow,
    get_instances,
    get_workflow,
    list_workflows,
    load_config,
    update_workflow,
    verify_instance,
)
from n8n_sync import submit_workflow_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate workflows into the new primary instance.")
    parser.add_argument(
        "--source",
        choices=["cloud-secondary", "cloud-tertiary", "both"],
        default="both",
    )
    parser.add_argument("--target", default="primary", help="Target instance alias (default: primary)")
    parser.add_argument("--dry-run", action="store_true", help="Show planned writes without mutating remote")
    parser.add_argument("--dotenv", default="secrets/.env.n8n", help="Path to dotenv file relative to repo root")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Repo root directory for config (default: current working directory)",
    )
    return parser.parse_args()


def resolve_sources(instance_arg: str) -> List[str]:
    if instance_arg == "both":
        return ["cloud-secondary", "cloud-tertiary"]
    return [instance_arg]


def build_target_name_index(items: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    name_map: Dict[str, List[str]] = {}
    for item in items:
        name = str(item.get("name", "")).strip()
        workflow_id = item.get("id")
        if name and workflow_id is not None:
            name_map.setdefault(name, []).append(str(workflow_id))
    return name_map


def get_unique_target_id(name_index: Dict[str, List[str]], workflow_name: str) -> str | None:
    matches = name_index.get(workflow_name, [])
    if len(matches) > 1:
        raise SyncError(
            f"Target already contains multiple workflows named '{workflow_name}' ({', '.join(matches)}). "
            "Aborting to avoid creating or updating duplicates."
        )
    return matches[0] if matches else None


def main() -> int:
    args = parse_args()
    repo_root = Path(args.output_dir).resolve()
    config = load_config(repo_root, dotenv_relpath=args.dotenv)
    instances = get_instances(config)

    if args.target not in instances:
        raise SyncError(f"Target instance '{args.target}' is not configured.")
    target = instances[args.target]
    ok, msg = verify_instance(target)
    if not ok:
        raise SyncError(f"Instance check failed for '{args.target}': {msg}")

    source_aliases = resolve_sources(args.source)
    for alias in source_aliases:
        if alias not in instances:
            raise SyncError(f"Source instance '{alias}' is not configured.")
        ok, msg = verify_instance(instances[alias])
        if not ok:
            raise SyncError(f"Instance check failed for '{alias}': {msg}")

    target_name_index = build_target_name_index(list_workflows(target))
    counters: Dict[str, int] = {"CREATE": 0, "UPDATE": 0}

    for alias in source_aliases:
        source = instances[alias]
        summaries = list_workflows(source)
        print(f"\n▸ {alias} → {args.target}  ({len(summaries)} workflows)")
        for summary in summaries:
            workflow_id = str(summary.get("id"))
            payload = get_workflow(source, workflow_id)
            name = str(payload.get("name", "workflow")).strip() or f"workflow-{workflow_id}"

            target_id = get_unique_target_id(target_name_index, name)
            action = "UPDATE" if target_id else "CREATE"

            if not args.dry_run:
                if not target_id:
                    target_name_index = build_target_name_index(list_workflows(target))
                    target_id = get_unique_target_id(target_name_index, name)
                    if target_id:
                        action = "UPDATE"

                if action == "UPDATE" and target_id:
                    submit_workflow_payload(
                        lambda upsert_payload: update_workflow(target, target_id, upsert_payload),
                        payload,
                    )
                else:
                    created = submit_workflow_payload(
                        lambda upsert_payload: create_workflow(target, upsert_payload),
                        payload,
                    )
                    new_id = created.get("id")
                    if new_id is not None:
                        target_name_index[name] = [str(new_id)]

            counters[action] = counters.get(action, 0) + 1
            suffix = "(dry-run)" if args.dry_run else ""
            print(f"  {action:>6}  {name} {suffix}".rstrip())

    total = sum(counters.values())
    prefix = "(dry-run) " if args.dry_run else ""
    print(f"\n{prefix}Summary: {counters.get('CREATE', 0)} created, {counters.get('UPDATE', 0)} updated, {total} total")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
