#!/usr/bin/env python3
"""Migrate credential placeholders from workflow JSON to target n8n instance."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from n8n_common import (
    SyncError,
    InstanceConfig,
    get_instances,
    http_json_request,
    join_url,
    load_config,
    write_json,
)


@dataclass(frozen=True)
class CredentialRef:
    """Credential type and name pair."""
    cred_type: str
    cred_name: str

    def __hash__(self):
        return hash((self.cred_type, self.cred_name))

    def __eq__(self, other):
        if not isinstance(other, CredentialRef):
            return False
        return self.cred_type == other.cred_type and self.cred_name == other.cred_name


def parse_args():
    parser = argparse.ArgumentParser(description="Migrate credential placeholders to target n8n instance.")
    parser.add_argument(
        "--workflows-path",
        required=True,
        help="Path to folder containing workflow JSON files",
    )
    parser.add_argument(
        "--target",
        default="primary",
        help="Target instance alias (default: primary)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned creations without mutating remote",
    )
    parser.add_argument(
        "--overwrite-strategy",
        choices=["skip-existing", "error-on-existing"],
        default="skip-existing",
        help="How to handle existing credentials",
    )
    parser.add_argument(
        "--output-report-path",
        help="Path to save JSON report",
    )
    parser.add_argument(
        "--dotenv",
        default="secrets/.env.n8n",
        help="Path to dotenv file relative to repo root",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repo root directory for config",
    )
    return parser.parse_args()


def extract_credentials_from_workflow(workflow_json: Dict[str, Any]) -> Set[CredentialRef]:
    """Extract all credential pairs from a workflow."""
    refs = set()
    nodes = workflow_json.get("nodes", [])
    for node in nodes:
        credentials = node.get("credentials", {})
        for cred_type, cred_info in credentials.items():
            if isinstance(cred_info, dict):
                cred_name = cred_info.get("name")
                if cred_name:
                    refs.add(CredentialRef(cred_type=cred_type, cred_name=cred_name))
    return refs


def load_workflow_files(workflows_path: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    """Load all workflow JSON files from directory."""
    workflows = []
    if not workflows_path.exists():
        raise SyncError(f"Workflows path does not exist: {workflows_path}")

    for json_file in workflows_path.rglob("*.json"):
        try:
            content = json.loads(json_file.read_text(encoding="utf-8"))
            workflows.append((json_file, content))
        except (json.JSONDecodeError, IOError) as exc:
            print(f"warning: skipped {json_file}: {exc}")
    return workflows


def list_target_credentials(instance: InstanceConfig) -> Dict[str, Any]:
    """Get all existing credentials on target instance."""
    url = join_url(instance.base_url, "/api/v1/credentials")
    response = http_json_request("GET", url, instance.api_key)
    if isinstance(response.get("data"), list):
        return {cred["name"]: cred for cred in response["data"]}
    return {}


def get_credential_schema(instance: InstanceConfig, cred_type: str) -> Dict[str, Any]:
    """Get schema for a credential type."""
    url = join_url(instance.base_url, f"/api/v1/credentials/schema/{cred_type}")
    return http_json_request("GET", url, instance.api_key)


def build_minimal_credential_data(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Build minimal credential data object that satisfies schema."""
    data = {}
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    for prop_name, prop_schema in properties.items():
        if prop_name in required:
            default = prop_schema.get("default")
            if default is not None:
                data[prop_name] = default
            elif prop_schema.get("type") == "string":
                data[prop_name] = f"__placeholder_{prop_name}__"
            elif prop_schema.get("type") == "number":
                data[prop_name] = 0
            elif prop_schema.get("type") == "boolean":
                data[prop_name] = False
            else:
                data[prop_name] = None

    return data


def create_credential(
    instance: InstanceConfig,
    cred_type: str,
    cred_name: str,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    """Create a credential on the target instance."""
    url = join_url(instance.base_url, "/api/v1/credentials")
    payload = {
        "name": cred_name,
        "type": cred_type,
        "data": data,
    }
    return http_json_request("POST", url, instance.api_key, payload=payload)


def main():
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    config = load_config(repo_root, dotenv_relpath=args.dotenv)
    instances = get_instances(config)

    if args.target not in instances:
        raise SyncError(f"Target instance {repr(args.target)} is not configured.")
    target = instances[args.target]

    workflows_path = Path(args.workflows_path).resolve()
    print(f"Loading workflows from: {workflows_path}")
    workflow_files = load_workflow_files(workflows_path)
    print(f"[+] Loaded {len(workflow_files)} workflow file(s)")

    all_refs = set()
    for _, workflow_json in workflow_files:
        refs = extract_credentials_from_workflow(workflow_json)
        all_refs.update(refs)

    print(f"\nFound {len(all_refs)} unique credential reference(s):")
    for ref in sorted(all_refs, key=lambda r: (r.cred_type, r.cred_name)):
        print(f"  • {ref.cred_type}: {ref.cred_name}")

    print(f"\nQuerying target instance for existing credentials...")
    try:
        existing = list_target_credentials(target)
    except SyncError as exc:
        raise SyncError(f"Failed to list credentials on target: {exc}")

    print(f"Found {len(existing)} existing credential(s) on target")

    created = []
    skipped = []
    failed = []
    manual_action = []

    for ref in sorted(all_refs, key=lambda r: (r.cred_type, r.cred_name)):
        if ref.cred_name in existing:
            print(f"  [-] skip    {ref.cred_type}: {ref.cred_name} (already exists)")
            skipped.append({"credentialType": ref.cred_type, "credentialName": ref.cred_name})
            continue

        print(f"  [+] create  {ref.cred_type}: {ref.cred_name}")

        try:
            schema = get_credential_schema(target, ref.cred_type)
        except SyncError as exc:
            error_msg = f"Could not fetch schema for {ref.cred_type}: {exc}"
            print(f"    [!] {error_msg}")
            manual_action.append({
                "credentialType": ref.cred_type,
                "credentialName": ref.cred_name,
                "reason": error_msg,
            })
            continue

        try:
            data = build_minimal_credential_data(schema)
        except Exception as exc:
            error_msg = f"Could not generate placeholder data: {exc}"
            print(f"    [!] {error_msg}")
            manual_action.append({
                "credentialType": ref.cred_type,
                "credentialName": ref.cred_name,
                "reason": error_msg,
            })
            continue

        if not args.dry_run:
            try:
                result = create_credential(target, ref.cred_type, ref.cred_name, data)
                created.append({
                    "credentialType": ref.cred_type,
                    "credentialName": ref.cred_name,
                    "id": result.get("id"),
                })
                print(f"    [+] created id={result.get('id')}")
            except SyncError as exc:
                error_msg = f"Failed to create: {exc}"
                print(f"    [!] {error_msg}")
                failed.append({
                    "credentialType": ref.cred_type,
                    "credentialName": ref.cred_name,
                    "error": error_msg,
                })
        else:
            created.append({
                "credentialType": ref.cred_type,
                "credentialName": ref.cred_name,
                "id": None,
            })
            print(f"    (dry-run: would create)")

    report = {
        "workflowFilesScanned": len(workflow_files),
        "credentialReferencesFound": len(all_refs),
        "uniqueCredentialsRequired": len(all_refs),
        "existingOnTarget": len(existing),
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "manualActionRequired": manual_action,
    }

    if args.output_report_path:
        report_path = Path(args.output_report_path)
        write_json(report_path, report)
        print(f"\n[+] Report saved to: {report_path}")

    print("\n=== Summary ===")
    print(f"Workflow files scanned:      {report['workflowFilesScanned']}")
    print(f"Credential refs found:       {report['credentialReferencesFound']}")
    print(f"Already existing on target:  {report['existingOnTarget']}")
    print(f"Created:                     {len(created)}")
    print(f"Skipped:                     {len(skipped)}")
    print(f"Failed:                      {len(failed)}")
    print(f"Manual action required:      {len(manual_action)}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        raise SystemExit(130)
