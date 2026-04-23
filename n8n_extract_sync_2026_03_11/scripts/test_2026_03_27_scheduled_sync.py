#!/usr/bin/env python3
"""Unit tests for scheduled sync helper logic."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parent / "scheduler" / "2026_03_27_scheduled_sync.py"
sys.path.insert(0, str(MODULE_PATH.parent.parent))
SPEC = importlib.util.spec_from_file_location("scheduled_sync_2026_03_27", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ScheduledSyncTests(unittest.TestCase):
    def test_resolve_webhook_settings_prefers_explicit_url_and_env_file_token(self) -> None:
        webhook_url, auth_token = MODULE.resolve_webhook_settings(
            "https://explicit.example/webhook",
            {
                "N8N_WEBHOOK_TELEMETRY_URL": "https://env-file.example/webhook",
                "N8N_WEBHOOK_TELEMETRY_AUTH_TOKEN": "env-file-token",
            },
            {
                "N8N_WEBHOOK_TELEMETRY_URL": "https://base-env.example/webhook",
                "N8N_WEBHOOK_TELEMETRY_AUTH_TOKEN": "base-env-token",
            },
        )

        self.assertEqual(webhook_url, "https://explicit.example/webhook")
        self.assertEqual(auth_token, "env-file-token")

    def test_resolve_webhook_settings_uses_env_file_before_process_env(self) -> None:
        webhook_url, auth_token = MODULE.resolve_webhook_settings(
            "",
            {
                "N8N_WEBHOOK_TELEMETRY_URL": "https://env-file.example/webhook",
                "N8N_WEBHOOK_TELEMETRY_AUTH_TOKEN": "env-file-token",
            },
            {
                "N8N_WEBHOOK_TELEMETRY_URL": "https://base-env.example/webhook",
                "N8N_WEBHOOK_TELEMETRY_AUTH_TOKEN": "base-env-token",
            },
        )

        self.assertEqual(webhook_url, "https://env-file.example/webhook")
        self.assertEqual(auth_token, "env-file-token")

    def test_build_run_row_keeps_dirty_files_reset_in_summary_only(self) -> None:
        started_at = datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc)
        finished_at = datetime(2026, 3, 27, 12, 1, tzinfo=timezone.utc)
        summary = {"dirty_files_reset": 3, "hostname": "test-host"}

        run_row = MODULE.build_run_row(
            started_at=started_at,
            finished_at=finished_at,
            run_status="success",
            instance="all",
            mirror_root=Path("/tmp/mirror"),
            branch="main",
            commit_before="abc",
            commit_after="def",
            commit_created=True,
            commit_sha="def",
            push_succeeded=True,
            task_name="n8n-workflow-sync",
            duration_ms=60000,
            remote_changed_count=2,
            staged_change_count=2,
            conflict_count=0,
            pruned_count=1,
            error_message="",
            summary=summary,
        )

        self.assertNotIn("dirty_files_reset", run_row)
        self.assertEqual(run_row["summary"]["dirty_files_reset"], 3)

    def test_build_webhook_payload_puts_dirty_files_reset_in_metadata(self) -> None:
        started_at = datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc)
        finished_at = datetime(2026, 3, 27, 12, 1, tzinfo=timezone.utc)

        payload = MODULE.build_webhook_payload(
            job_name="n8n-workflow-sync",
            run_status="success",
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=60000,
            error_message="",
            commit_sha="def",
            commit_created=True,
            push_succeeded=True,
            remote_changed_count=2,
            conflict_count=0,
            pruned_count=1,
            branch="main",
            instance="all",
            dirty_files_reset=4,
        )

        self.assertEqual(payload["job_name"], "n8n-workflow-sync")
        self.assertNotIn("dirty_files_reset", payload)
        self.assertEqual(payload["metadata"]["dirty_files_reset"], 4)

    def test_emit_telemetry_destinations_reports_supabase_and_webhook_failures_separately(self) -> None:
        with (
            mock.patch.object(MODULE, "emit_telemetry", side_effect=RuntimeError("supabase failed")),
            mock.patch.object(MODULE, "send_webhook", side_effect=RuntimeError("webhook failed")),
        ):
            run_id, warnings = MODULE.emit_telemetry_destinations(
                supabase_env={"SUPABASE_PROJECT_URL": "https://example.supabase.co", "SUPABASE_SECRET_KEY": "secret"},
                run_row={"status": "success"},
                conflicts=[],
                webhook_url="https://example.test/webhook",
                webhook_payload={"job_name": "n8n-workflow-sync"},
                webhook_auth_token="token",
            )

        self.assertIsNone(run_id)
        self.assertEqual(
            warnings,
            [
                "supabase telemetry warning: supabase failed",
                "webhook telemetry warning: webhook failed",
            ],
        )

    def test_emit_telemetry_destinations_allows_webhook_success_after_supabase_failure(self) -> None:
        with (
            mock.patch.object(MODULE, "emit_telemetry", side_effect=RuntimeError("supabase failed")),
            mock.patch.object(MODULE, "send_webhook") as send_webhook,
        ):
            run_id, warnings = MODULE.emit_telemetry_destinations(
                supabase_env={"SUPABASE_PROJECT_URL": "https://example.supabase.co", "SUPABASE_SECRET_KEY": "secret"},
                run_row={"status": "success"},
                conflicts=[],
                webhook_url="https://example.test/webhook",
                webhook_payload={"job_name": "n8n-workflow-sync"},
                webhook_auth_token="token",
            )

        self.assertIsNone(run_id)
        self.assertEqual(warnings, ["supabase telemetry warning: supabase failed"])
        send_webhook.assert_called_once()

    def test_changed_workflow_dirs_from_status_handles_modify(self) -> None:
        lines = [
            " M workflows/primary/example_abc/workflow.json",
            " M workflows/primary/example_abc/metadata.json",
        ]
        self.assertEqual(
            MODULE.changed_workflow_dirs_from_status(lines),
            ["workflows/primary/example_abc"],
        )

    def test_changed_workflow_dirs_from_status_handles_rename(self) -> None:
        lines = [
            "R  workflows/primary/old_abc/workflow.json -> workflows/primary/new_abc/workflow.json",
        ]
        self.assertEqual(
            MODULE.changed_workflow_dirs_from_status(lines),
            ["workflows/primary/new_abc"],
        )

    def test_read_state_record_by_local_dir_matches_windows_paths(self) -> None:
        state = {
            "records": {
                "primary:abc": {
                    "localPath": "workflows\\primary\\example_abc\\workflow.json",
                    "workflowId": "abc",
                }
            }
        }
        rec = MODULE.read_state_record_by_local_dir(state, "workflows/primary/example_abc")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["workflowId"], "abc")

    def test_resolve_origin_url_prefers_explicit_value(self) -> None:
        origin = MODULE.resolve_origin_url(Path("/tmp/no-git"), {}, "https://example.com/repo.git")
        self.assertEqual(origin, "https://example.com/repo.git")

    def test_resolve_origin_url_uses_env_when_no_git_repo(self) -> None:
        origin = MODULE.resolve_origin_url(Path("/tmp/no-git"), {"N8N_SYNC_GIT_ORIGIN_URL": "https://example.com/env.git"}, "")
        self.assertEqual(origin, "https://example.com/env.git")

    def test_scheduler_points_to_parent_n8n_sync_script(self) -> None:
        self.assertTrue((MODULE.PARENT_SCRIPTS_DIR / "n8n_sync.py").exists())


if __name__ == "__main__":
    unittest.main()
