#!/usr/bin/env python3
"""Shared helpers for n8n workflow sync tooling."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class SyncError(RuntimeError):
    """Raised for expected sync and configuration failures."""


@dataclass(frozen=True)
class InstanceConfig:
    alias: str
    base_url: str
    api_key: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repo_root_from_script(script_path: Path) -> Path:
    return script_path.resolve().parent.parent


def resolve_workspace_root(explicit: Optional[str], script_path: Path) -> Path:
    """Return the workspace root that contains workflows/ and .n8n_sync/.

    Priority:
      1. Explicit --output-dir value (if non-empty).
      2. Walk up from cwd looking for the marker pair.
      3. Walk up from the script's directory.
      4. Fall back to cwd.
    """
    if explicit:
        return Path(explicit).resolve()

    markers = ("workflows", ".n8n_sync")

    def _has_markers(d: Path) -> bool:
        return all((d / m).is_dir() for m in markers)

    # Walk up from cwd
    candidate = Path.cwd().resolve()
    for _ in range(10):
        if _has_markers(candidate):
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent

    # Walk up from script location
    candidate = script_path.resolve().parent
    for _ in range(10):
        if _has_markers(candidate):
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent

    return Path.cwd().resolve()


def load_dotenv(dotenv_path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not dotenv_path.exists():
        return values

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def load_config(repo_root: Path, dotenv_relpath: str = "secrets/.env.n8n") -> Dict[str, str]:
    config: Dict[str, str] = {}
    config.update(load_dotenv(repo_root / dotenv_relpath))
    config.update({k: v for k, v in os.environ.items() if k.startswith("N8N_") or k.startswith("OPENAI_")})
    return config


def get_instances(config: Dict[str, str]) -> Dict[str, InstanceConfig]:
    instances: Dict[str, InstanceConfig] = {}

    def pick_credentials(prefixes: List[str]) -> Tuple[str, str]:
        for prefix in prefixes:
            base_url = config.get(f"{prefix}_BASE_URL", "").strip().rstrip("/")
            api_key = config.get(f"{prefix}_API_KEY", "").strip()
            if base_url and api_key:
                return base_url, api_key
        return "", ""

    # Support both legacy and cloud-prefixed env naming.
    base_url, api_key = pick_credentials(["N8N_PRIMARY"])
    if base_url and api_key:
        instances["primary"] = InstanceConfig(alias="primary", base_url=base_url, api_key=api_key)

    base_url, api_key = pick_credentials(["N8N_SECONDARY", "N8N_CLOUD_SECONDARY"])
    if base_url and api_key:
        instances["secondary"] = InstanceConfig(alias="secondary", base_url=base_url, api_key=api_key)

    base_url, api_key = pick_credentials(["N8N_TERTIARY", "N8N_CLOUD_TERTIARY"])
    if base_url and api_key:
        instances["tertiary"] = InstanceConfig(
            alias="tertiary", base_url=base_url, api_key=api_key
        )

    if not instances:
        raise SyncError(
            "No n8n instances configured. Set N8N_PRIMARY_BASE_URL/API_KEY and either "
            "N8N_SECONDARY_BASE_URL/API_KEY or N8N_CLOUD_SECONDARY_BASE_URL/API_KEY "
            "(optional: N8N_TERTIARY_BASE_URL/API_KEY or N8N_CLOUD_TERTIARY_BASE_URL/API_KEY) "
            "in secrets/.env.n8n or env vars."
        )
    return instances


def ensure_dirs(repo_root: Path) -> None:
    (repo_root / "workflows").mkdir(parents=True, exist_ok=True)
    (repo_root / ".n8n_sync").mkdir(parents=True, exist_ok=True)


def slugify(value: str) -> str:
    lowered = value.lower().strip()
    lowered = re.sub(r"[^a-z0-9]+", "_", lowered)
    lowered = lowered.strip("_")
    return lowered or "workflow"



def canonicalize_workflow_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Keep importable structure while removing volatile metadata from hash computation.
    cleaned = json.loads(json.dumps(payload))
    for key in ("updatedAt", "createdAt", "versionId", "staticData",
                "triggerCount", "versionCounter",
                "scopes", "checksum", "ownedBy", "sharedWith", "shared",
                "activeVersion"):
        cleaned.pop(key, None)
    cleaned.pop("meta", None)
    return cleaned


def canonical_json_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def load_state(repo_root: Path) -> Dict[str, Any]:
    state_path = repo_root / ".n8n_sync" / "state.json"
    return load_json(state_path, fallback={"records": {}})


def save_state(repo_root: Path, state: Dict[str, Any]) -> None:
    state_path = repo_root / ".n8n_sync" / "state.json"
    write_json(state_path, state)


def make_record_key(instance_alias: str, workflow_id: str) -> str:
    return f"{instance_alias}:{workflow_id}"


def find_state_record(
    records: Dict[str, Any], alias: str, workflow_id: str
) -> Optional[Dict[str, Any]]:
    """Lookup a state record by alias+workflow_id, with case-insensitive fallback."""
    exact = records.get(make_record_key(alias, workflow_id))
    if exact:
        return exact
    wanted = str(workflow_id).casefold()
    for rec in records.values():
        if rec.get("instance") != alias:
            continue
        if str(rec.get("workflowId", "")).casefold() == wanted:
            return rec
    return None


def http_json_request(
    method: str,
    url: str,
    api_key: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    data = None
    headers = {
        "X-N8N-API-KEY": api_key,
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, method=method.upper(), data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            if not body:
                return {}
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SyncError(f"HTTP {exc.code} calling {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"Network error calling {url}: {exc}") from exc


def join_url(base_url: str, path: str, query: Optional[Dict[str, Any]] = None) -> str:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if query:
        encoded = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        if encoded:
            url = f"{url}?{encoded}"
    return url


def extract_items(response_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(response_json.get("data"), list):
        return response_json["data"]
    if isinstance(response_json.get("items"), list):
        return response_json["items"]
    if isinstance(response_json, list):
        return response_json
    return []


def list_workflows(instance: InstanceConfig, *, page_limit: int = 250) -> List[Dict[str, Any]]:
    all_items: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        query: Dict[str, Any] = {"limit": page_limit}
        if cursor:
            query["cursor"] = cursor
        url = join_url(instance.base_url, "/api/v1/workflows", query=query)
        response = http_json_request("GET", url, instance.api_key)
        items = extract_items(response)
        if not items and isinstance(response, dict) and "id" in response:
            items = [response]
        all_items.extend(items)
        next_cursor = response.get("nextCursor") if isinstance(response, dict) else None
        if not next_cursor:
            break
        cursor = next_cursor
    return all_items


def get_workflow(instance: InstanceConfig, workflow_id: str) -> Dict[str, Any]:
    url = join_url(instance.base_url, f"/api/v1/workflows/{workflow_id}")
    return http_json_request("GET", url, instance.api_key)


def update_workflow(instance: InstanceConfig, workflow_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = join_url(instance.base_url, f"/api/v1/workflows/{workflow_id}")
    return http_json_request("PUT", url, instance.api_key, payload=payload)


def create_workflow(instance: InstanceConfig, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = join_url(instance.base_url, "/api/v1/workflows")
    return http_json_request("POST", url, instance.api_key, payload=payload)


def verify_instance(instance: InstanceConfig) -> Tuple[bool, str]:
    try:
        _ = list_workflows(instance)
        return True, "ok"
    except SyncError as exc:
        return False, str(exc)
