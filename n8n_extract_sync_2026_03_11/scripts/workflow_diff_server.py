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
    repo_root_from_script,
    resolve_workspace_root,
    sha256_text,
    verify_instance,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a localhost diff-review utility before pushing workflow changes."
    )
    parser.add_argument(
        "--instance",
        choices=[
            "primary",
            "secondary",
            "tertiary",
        ],
        default="primary",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--workflow-id", help="Workflow ID to review")
    group.add_argument(
        "--local-path",
        help="Local path to workflow.json (used to resolve matching state record)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dotenv", default="secrets/.env.n8n")
    parser.add_argument(
        "--output-dir",
        default="",
        help="Repo root directory for workflows/state (default: n8n_extract_sync_2026_03_11)",
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

    def approve(self, expected_remote_hash: str) -> Dict[str, Any]:
        rec = self._resolve_record()
        workflow_id = str(rec.get("workflowId"))
        fresh_remote = get_workflow(self.instance, workflow_id)
        fresh_remote_hash = payload_hash(fresh_remote)
        if expected_remote_hash and fresh_remote_hash != expected_remote_hash:
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
        try:
            result = self.app.approve(expected_remote_hash=expected_remote_hash)
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

    app = DiffReviewApp(
        repo_root=repo_root,
        instance_alias=args.instance,
        workflow_id=args.workflow_id,
        local_path=args.local_path,
        dotenv_path=args.dotenv,
    )

    RequestHandler.app = app
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    print(
        f"Diff review server running at http://{args.host}:{args.port} "
        f"(instance={args.instance}, workflow={args.workflow_id or app.local_path})"
    )
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
