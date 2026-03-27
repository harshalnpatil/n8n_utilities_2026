#!/usr/bin/env python3
"""Run unattended n8n workflow backup in an isolated mirror checkout."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
PARENT_SCRIPTS_DIR = SCRIPT_DIR.parent
if str(PARENT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_SCRIPTS_DIR))

from n8n_common import (
    canonicalize_workflow_payload,
    canonical_json_dumps,
    ensure_dirs,
    get_instances,
    get_workflow,
    load_config,
    load_json,
    load_state,
    sha256_text,
)


RUN_STATUSES = {"success", "partial_conflict", "failed"}


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class ConflictRecord:
    instance: str
    workflow_id: str
    workflow_name: str
    local_path: str
    conflict_reason: str
    local_hash: str
    remote_hash: str
    baseline_local_hash: str
    baseline_remote_hash: str
    artifact_dir: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_key_value_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def build_env(base_env: Dict[str, str], overlays: Iterable[Dict[str, str]]) -> Dict[str, str]:
    merged = dict(base_env)
    for overlay in overlays:
        for key, value in overlay.items():
            if value:
                merged[key] = value
    return merged


def run_command(args: List[str], cwd: Path, env: Dict[str, str]) -> CommandResult:
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def require_success(result: CommandResult, step: str) -> None:
    if result.returncode == 0:
        return
    raise RuntimeError(f"{step} failed ({result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")


def ensure_mirror_checkout(mirror_root: Path, origin_url: str, branch: str, env: Dict[str, str]) -> None:
    git_dir = mirror_root / ".git"
    if git_dir.exists():
        return
    mirror_root.parent.mkdir(parents=True, exist_ok=True)
    result = run_command(["git", "clone", "--branch", branch, origin_url, str(mirror_root)], cwd=mirror_root.parent, env=env)
    require_success(result, "git clone")


def git_output(mirror_root: Path, env: Dict[str, str], *args: str) -> str:
    result = run_command(["git", *args], cwd=mirror_root, env=env)
    require_success(result, f"git {' '.join(args)}")
    return result.stdout.strip()


def git_status_porcelain(mirror_root: Path, env: Dict[str, str]) -> List[str]:
    out = git_output(mirror_root, env, "status", "--porcelain")
    return [line for line in out.splitlines() if line.strip()]


def changed_workflow_dirs_from_status(lines: Iterable[str]) -> List[str]:
    workflow_dirs = set()
    for line in lines:
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        normalized = path.replace("\\", "/")
        parts = normalized.split("/")
        if len(parts) >= 4 and parts[0] == "workflows":
            workflow_dirs.add("/".join(parts[:3]))
    return sorted(workflow_dirs)


def relpath_for(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")


def local_hash_for_workflow(path: Path) -> str:
    if not path.exists():
        return ""
    payload = load_json(path, fallback={})
    return sha256_text(canonical_json_dumps(canonicalize_workflow_payload(payload)))


def remote_hash_for_workflow(payload: Dict[str, Any]) -> str:
    return sha256_text(canonical_json_dumps(canonicalize_workflow_payload(payload)))


def normalize_path(value: str) -> str:
    return value.replace("\\", "/")


def read_state_record_by_local_dir(state: Dict[str, Any], workflow_dir: str) -> Optional[Dict[str, Any]]:
    workflow_rel = f"{workflow_dir}/workflow.json"
    for rec in state.get("records", {}).values():
        if normalize_path(str(rec.get("localPath", ""))) == workflow_rel:
            return rec
    return None


def capture_dirty_conflicts(
    mirror_root: Path,
    workflow_dirs: Iterable[str],
    conflict_root: Path,
    n8n_env: Dict[str, str],
) -> List[ConflictRecord]:
    conflicts: List[ConflictRecord] = []
    state = load_state(mirror_root)
    config = load_config(mirror_root, dotenv_relpath="does_not_matter.env")
    config.update({k: v for k, v in n8n_env.items() if k.startswith("N8N_")})
    instances = get_instances(config)

    for workflow_dir in workflow_dirs:
        record = read_state_record_by_local_dir(state, workflow_dir)
        local_dir = mirror_root / workflow_dir
        workflow_path = local_dir / "workflow.json"
        metadata = load_json(local_dir / "metadata.json", fallback={})
        workflow_name = str(metadata.get("name") or (record or {}).get("workflowName") or local_dir.name)
        instance = str(metadata.get("instance") or (record or {}).get("instance") or workflow_dir.split("/")[1])
        workflow_id = str(metadata.get("id") or (record or {}).get("workflowId") or "")

        artifact_dir = conflict_root / workflow_dir.replace("/", "__")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if workflow_path.exists():
            shutil.copy2(workflow_path, artifact_dir / "local_workflow.json")
        if (local_dir / "metadata.json").exists():
            shutil.copy2(local_dir / "metadata.json", artifact_dir / "local_metadata.json")

        remote_hash = ""
        if workflow_id and instance in instances:
            try:
                remote_payload = get_workflow(instances[instance], workflow_id)
                remote_hash = remote_hash_for_workflow(remote_payload)
                (artifact_dir / "remote_workflow.json").write_text(
                    json.dumps(remote_payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                (artifact_dir / "remote_fetch_error.txt").write_text(str(exc) + "\n", encoding="utf-8")

        summary = {
            "workflow_dir": workflow_dir,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "instance": instance,
            "conflict_reason": "mirror_dirty_before_run",
            "baseline_local_hash": (record or {}).get("lastLocalHash", ""),
            "baseline_remote_hash": (record or {}).get("lastRemoteHash", ""),
            "captured_at": utc_now_iso(),
        }
        (artifact_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

        conflicts.append(
            ConflictRecord(
                instance=instance,
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                local_path=f"{workflow_dir}/workflow.json",
                conflict_reason="mirror_dirty_before_run",
                local_hash=local_hash_for_workflow(workflow_path),
                remote_hash=remote_hash,
                baseline_local_hash=(record or {}).get("lastLocalHash", ""),
                baseline_remote_hash=(record or {}).get("lastRemoteHash", ""),
                artifact_dir=str(artifact_dir),
            )
        )
    return conflicts


def post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(
        url=url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def supabase_rest_url(project_url: str, table_name: str) -> str:
    encoded = urllib.parse.quote(table_name, safe="")
    return f"{project_url.rstrip('/')}/rest/v1/{encoded}"


def insert_supabase_row(project_url: str, secret_key: str, table_name: str, row: Dict[str, Any]) -> Dict[str, Any]:
    headers = {
        "apikey": secret_key,
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
        "X-Client-Info": "python-script",
    }
    data = post_json(supabase_rest_url(project_url, table_name), headers, row)
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    raise RuntimeError(f"Unexpected Supabase response for {table_name}: {data!r}")


def emit_telemetry(supabase_env: Dict[str, str], run_row: Dict[str, Any], conflicts: List[ConflictRecord]) -> Optional[str]:
    project_url = supabase_env.get("SUPABASE_PROJECT_URL", "")
    secret_key = supabase_env.get("SUPABASE_SECRET_KEY", "")
    if not project_url or not secret_key:
        return None

    inserted_run = insert_supabase_row(project_url, secret_key, "n8n_sync_runs", run_row)
    run_id = str(inserted_run.get("id", ""))
    if not run_id:
        return None

    for conflict in conflicts:
        insert_supabase_row(
            project_url,
            secret_key,
            "n8n_sync_run_conflicts",
            {
                "run_id": run_id,
                "instance": conflict.instance,
                "workflow_id": conflict.workflow_id,
                "workflow_name": conflict.workflow_name,
                "local_path": conflict.local_path,
                "conflict_reason": conflict.conflict_reason,
                "local_hash": conflict.local_hash,
                "remote_hash": conflict.remote_hash,
                "baseline_local_hash": conflict.baseline_local_hash,
                "baseline_remote_hash": conflict.baseline_remote_hash,
                "artifact_dir": conflict.artifact_dir,
            },
        )
    return run_id


def send_webhook(url: str, payload: Dict[str, Any]) -> None:
    headers = {"Content-Type": "application/json"}
    post_json(url, headers, payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scheduled n8n backup runner for Windows Task Scheduler.")
    parser.add_argument("--utility-root", default="", help="Path to this utilities package root.")
    parser.add_argument("--repo-root", default="", help="Backward-compatible alias for --utility-root.")
    parser.add_argument("--mirror-root", required=True, help="Path to an isolated mirror checkout used by automation.")
    parser.add_argument("--instance", default="all", help="n8n instance selector passed to n8n_sync.py")
    parser.add_argument("--branch", default="main", help="Git branch to sync in the mirror checkout.")
    parser.add_argument("--task-name", default="n8n-workflow-sync", help="Task Scheduler task name for telemetry.")
    parser.add_argument("--webhook-url", default="", help="Optional webhook URL for run summaries.")
    parser.add_argument("--conflict-root", default="", help="Optional directory for conflict artifacts.")
    parser.add_argument("--n8n-env-file", default="", help="Optional override for secrets/.env.n8n")
    parser.add_argument("--supabase-env-file", default="", help="Optional override for secrets/supabase_env")
    parser.add_argument("--git-origin-url", default="", help="Optional override for git origin URL.")
    return parser.parse_args()


def resolve_origin_url(utility_root: Path, base_env: Dict[str, str], explicit_origin_url: str) -> str:
    if explicit_origin_url:
        return explicit_origin_url

    git_dir = utility_root / ".git"
    if git_dir.exists():
        return git_output(utility_root, base_env, "config", "--get", "remote.origin.url")

    env_origin = base_env.get("N8N_SYNC_GIT_ORIGIN_URL", "").strip()
    if env_origin:
        return env_origin

    raise RuntimeError(
        "Missing git origin URL. Pass --git-origin-url or set N8N_SYNC_GIT_ORIGIN_URL for the scheduled sync runner."
    )


def main() -> int:
    args = parse_args()
    started_at = utc_now()
    utility_root_arg = args.utility_root or args.repo_root
    if not utility_root_arg:
        raise RuntimeError("Missing --utility-root (or legacy --repo-root).")
    utility_root = Path(utility_root_arg).resolve()
    script_path = Path(__file__).resolve()
    mirror_root = Path(args.mirror_root).resolve()
    conflict_root = Path(args.conflict_root).resolve() if args.conflict_root else (mirror_root.parent / "n8n_sync_conflicts")
    conflict_root.mkdir(parents=True, exist_ok=True)

    base_env = dict(os.environ)
    n8n_env = load_key_value_file(Path(args.n8n_env_file).resolve() if args.n8n_env_file else utility_root / "secrets" / ".env.n8n")
    supabase_env = load_key_value_file(Path(args.supabase_env_file).resolve() if args.supabase_env_file else utility_root / "secrets" / "supabase_env")
    env = build_env(base_env, [n8n_env])

    origin_url = resolve_origin_url(utility_root, base_env, args.git_origin_url)
    commit_before = ""
    commit_after = ""
    run_status = "failed"
    commit_created = False
    commit_sha = ""
    push_succeeded: Optional[bool] = None
    error_message = ""
    conflict_records: List[ConflictRecord] = []
    changed_workflow_dirs: List[str] = []
    pruned_count = 0

    try:
        ensure_mirror_checkout(mirror_root, origin_url, args.branch, env)
        ensure_dirs(mirror_root)

        dirty_before = git_status_porcelain(mirror_root, env)
        if dirty_before:
            changed_workflow_dirs = changed_workflow_dirs_from_status(dirty_before)
            conflict_records = capture_dirty_conflicts(mirror_root, changed_workflow_dirs, conflict_root / started_at.strftime("%Y%m%dT%H%M%SZ"), n8n_env)
            raise RuntimeError("Mirror checkout is dirty before scheduled run; refusing to continue.")

        git_output(mirror_root, env, "fetch", "origin", args.branch)
        git_output(mirror_root, env, "checkout", args.branch)
        commit_before = git_output(mirror_root, env, "rev-parse", "HEAD")
        git_output(mirror_root, env, "pull", "--ff-only", "origin", args.branch)

        sync_script = script_path.parent / "n8n_sync.py"
        sync_result = run_command(
            [sys.executable, str(sync_script), "--mode", "backup", "--instance", args.instance, "--output-dir", str(mirror_root)],
            cwd=utility_root,
            env=env,
        )
        require_success(sync_result, "n8n_sync backup")

        status_lines = git_status_porcelain(mirror_root, env)
        changed_workflow_dirs = changed_workflow_dirs_from_status(status_lines)
        pruned_count = sum(1 for line in status_lines if line.startswith(" D ") or line.startswith("D "))

        if status_lines:
            run_command(["git", "add", "-A", "workflows"], cwd=mirror_root, env=env)
            staged = git_output(mirror_root, env, "diff", "--cached", "--name-only")
            if staged.strip():
                commit_message = f"chore(sync): backup n8n workflows {started_at.replace(microsecond=0).isoformat().replace('+00:00', 'Z')}"
                commit_result = run_command(["git", "commit", "-m", commit_message], cwd=mirror_root, env=env)
                require_success(commit_result, "git commit")
                commit_created = True
                commit_sha = git_output(mirror_root, env, "rev-parse", "HEAD")
                push_result = run_command(["git", "push", "origin", args.branch], cwd=mirror_root, env=env)
                require_success(push_result, "git push")
                push_succeeded = True
            else:
                push_succeeded = None
        commit_after = git_output(mirror_root, env, "rev-parse", "HEAD")
        run_status = "success"
    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)
        if conflict_records:
            run_status = "partial_conflict"
        else:
            run_status = "failed"

    if run_status not in RUN_STATUSES:
        run_status = "failed"

    finished_at = utc_now()
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    remote_changed_count = len(changed_workflow_dirs)
    staged_change_count = remote_changed_count if commit_created else 0
    summary = {
        "utility_root": str(utility_root),
        "mirror_root": str(mirror_root),
        "python_executable": sys.executable,
        "branch": args.branch,
        "origin_url": origin_url,
        "conflict_root": str(conflict_root),
        "changed_workflow_dirs": changed_workflow_dirs,
        "hostname": socket.gethostname(),
    }

    run_row = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "status": run_status,
        "host_name": socket.gethostname(),
        "instance": args.instance,
        "mirror_root": str(mirror_root),
        "git_branch": args.branch,
        "git_commit_before": commit_before or None,
        "git_commit_after": commit_after or None,
        "commit_created": commit_created,
        "commit_sha": commit_sha or None,
        "push_succeeded": push_succeeded,
        "task_name": args.task_name,
        "duration_ms": duration_ms,
        "remote_changed_count": remote_changed_count,
        "staged_change_count": staged_change_count,
        "conflict_count": len(conflict_records),
        "pruned_count": pruned_count,
        "error_message": error_message or None,
        "summary": summary,
    }

    telemetry_error = ""
    try:
        run_id = emit_telemetry(supabase_env, run_row, conflict_records)
        if run_id:
            summary["run_id"] = run_id
    except Exception as exc:  # noqa: BLE001
        telemetry_error = str(exc)

    webhook_url = args.webhook_url.strip()
    if webhook_url:
        webhook_payload = {
            "task_name": args.task_name,
            "status": run_status,
            "instance": args.instance,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "mirror_root": str(mirror_root),
            "commit_created": commit_created,
            "commit_sha": commit_sha or None,
            "remote_changed_count": remote_changed_count,
            "conflict_count": len(conflict_records),
            "error_message": error_message or None,
            "telemetry_error": telemetry_error or None,
        }
        try:
            send_webhook(webhook_url, webhook_payload)
        except Exception as exc:  # noqa: BLE001
            if not telemetry_error:
                telemetry_error = str(exc)

    if telemetry_error:
        print(f"telemetry_warning: {telemetry_error}", file=sys.stderr)

    if run_status == "success":
        return 0
    if run_status == "partial_conflict":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
