#!/usr/bin/env python3
"""Unit tests for scheduled sync helper logic."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parent / "scheduler" / "2026_03_27_scheduled_sync.py"
sys.path.insert(0, str(MODULE_PATH.parent.parent))
SPEC = importlib.util.spec_from_file_location("scheduled_sync_2026_03_27", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ScheduledSyncTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
