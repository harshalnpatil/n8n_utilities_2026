#!/usr/bin/env python3
"""Tests for n8n_sync push workflow selection."""

from __future__ import annotations

import importlib
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_n8n_sync():
    sys.modules.pop("n8n_sync", None)
    return importlib.import_module("n8n_sync")


class N8nSyncPushTests(unittest.TestCase):
    def _write_workflow(self, module, repo_root: Path, payload: dict) -> Path:
        workflow_path = repo_root / "workflows" / "primary" / "example_wf1" / "workflow.json"
        module.write_json(workflow_path, payload)
        return workflow_path

    def _state_for(self, module, repo_root: Path, workflow_path: Path, payload: dict) -> dict:
        workflow_hash = module.local_workflow_hash(workflow_path)
        return {
            "records": {
                "primary:wf1": {
                    "instance": "primary",
                    "workflowId": "wf1",
                    "workflowName": payload["name"],
                    "localPath": str(workflow_path.relative_to(repo_root)),
                    "versionId": payload.get("versionId", "?"),
                    "updatedAt": payload.get("updatedAt", "?"),
                    "lastRemoteHash": workflow_hash,
                    "lastLocalHash": workflow_hash,
                    "lastSyncAtUtc": "2026-04-15T00:00:00Z",
                    "lastDirection": "remote_to_local",
                }
            }
        }

    def test_push_skips_unchanged_local_workflow_without_remote_call(self) -> None:
        module = load_n8n_sync()
        payload = {"id": "wf1", "name": "Example", "active": False, "nodes": [], "connections": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "get_workflow") as get_workflow, patch.object(module, "update_workflow") as update_workflow:
                with redirect_stdout(io.StringIO()):
                    module.push_mode(repo_root, {"primary": object()}, ["primary"], None, False, state)

            get_workflow.assert_not_called()
            update_workflow.assert_not_called()

    def test_push_updates_only_when_local_hash_changed(self) -> None:
        module = load_n8n_sync()
        original = {"id": "wf1", "name": "Example", "active": False, "nodes": [], "connections": {}}
        edited = {"id": "wf1", "name": "Example edited", "active": False, "nodes": [], "connections": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, original)
            state = self._state_for(module, repo_root, workflow_path, original)
            module.write_json(workflow_path, edited)

            with patch.object(module, "get_workflow", return_value=original), patch.object(
                module,
                "update_workflow",
                return_value={"id": "wf1"},
            ) as update_workflow:
                with redirect_stdout(io.StringIO()):
                    module.push_mode(repo_root, {"primary": object()}, ["primary"], None, False, state)

            update_workflow.assert_called_once()

    def test_push_skips_archived_remote_workflow(self) -> None:
        module = load_n8n_sync()
        original = {"id": "wf1", "name": "Example", "active": False, "nodes": [], "connections": {}}
        edited = {"id": "wf1", "name": "Example edited", "active": False, "nodes": [], "connections": {}}
        archived_remote = {**original, "archived": True}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, original)
            state = self._state_for(module, repo_root, workflow_path, original)
            module.write_json(workflow_path, edited)

            with patch.object(module, "get_workflow", return_value=archived_remote), patch.object(module, "update_workflow") as update_workflow:
                with redirect_stdout(io.StringIO()) as output:
                    module.push_mode(repo_root, {"primary": object()}, ["primary"], None, False, state)

            update_workflow.assert_not_called()
            self.assertIn("ARCHIVED", output.getvalue())

    def test_push_treats_archived_at_as_archived_marker(self) -> None:
        module = load_n8n_sync()
        original = {"id": "wf1", "name": "Example", "active": False, "nodes": [], "connections": {}}
        edited = {"id": "wf1", "name": "Example edited", "active": False, "nodes": [], "connections": {}}
        archived_remote = {**original, "archivedAt": "2026-04-15T06:00:00.000Z"}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, original)
            state = self._state_for(module, repo_root, workflow_path, original)
            module.write_json(workflow_path, edited)

            with patch.object(module, "get_workflow", return_value=archived_remote), patch.object(
                module,
                "update_workflow",
            ) as update_workflow:
                with redirect_stdout(io.StringIO()) as output:
                    module.push_mode(repo_root, {"primary": object()}, ["primary"], None, False, state)

            update_workflow.assert_not_called()
            self.assertIn("ARCHIVED", output.getvalue())

    def test_dry_run_skips_unchanged_local_workflow_without_remote_call(self) -> None:
        module = load_n8n_sync()
        payload = {"id": "wf1", "name": "Example", "active": False, "nodes": [], "connections": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "get_workflow") as get_workflow, patch.object(
                module,
                "update_workflow",
            ) as update_workflow:
                with redirect_stdout(io.StringIO()) as output:
                    module.push_mode(repo_root, {"primary": object()}, ["primary"], None, True, state)

            get_workflow.assert_not_called()
            update_workflow.assert_not_called()
            self.assertIn("CLEAN", output.getvalue())

    def test_dry_run_reports_push_for_changed_local_clean_remote(self) -> None:
        module = load_n8n_sync()
        original = {"id": "wf1", "name": "Example", "active": False, "nodes": [], "connections": {}}
        edited = {"id": "wf1", "name": "Example edited", "active": False, "nodes": [], "connections": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, original)
            state = self._state_for(module, repo_root, workflow_path, original)
            module.write_json(workflow_path, edited)
            before_state = dict(state["records"]["primary:wf1"])

            with patch.object(module, "get_workflow", return_value=original), patch.object(
                module,
                "update_workflow",
            ) as update_workflow:
                with redirect_stdout(io.StringIO()) as output:
                    module.push_mode(repo_root, {"primary": object()}, ["primary"], None, True, state)

            update_workflow.assert_not_called()
            self.assertIn("PUSH", output.getvalue())
            self.assertEqual(before_state, state["records"]["primary:wf1"])

    def test_push_refuses_remote_drift_before_update(self) -> None:
        module = load_n8n_sync()
        original = {"id": "wf1", "name": "Example", "active": False, "nodes": [], "connections": {}}
        edited = {"id": "wf1", "name": "Example edited", "active": False, "nodes": [], "connections": {}}
        remote_changed = {"id": "wf1", "name": "Remote edited", "active": False, "nodes": [], "connections": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, original)
            state = self._state_for(module, repo_root, workflow_path, original)
            module.write_json(workflow_path, edited)

            with patch.object(module, "get_workflow", return_value=remote_changed), patch.object(
                module,
                "update_workflow",
            ) as update_workflow:
                with redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(module.SyncError):
                        module.push_mode(repo_root, {"primary": object()}, ["primary"], None, False, state)

            update_workflow.assert_not_called()
            self.assertIn("CONFLICT", output.getvalue())

    def test_push_creates_workflow_when_remote_is_missing(self) -> None:
        module = load_n8n_sync()
        original = {"id": "wf1", "name": "Example", "active": False, "nodes": [], "connections": {}}
        edited = {"id": "wf1", "name": "Example edited", "active": False, "nodes": [], "connections": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, original)
            state = self._state_for(module, repo_root, workflow_path, original)
            module.write_json(workflow_path, edited)

            with patch.object(module, "get_workflow", side_effect=module.SyncError("HTTP 404 missing")), patch.object(
                module,
                "create_workflow",
                return_value={"id": "wf2"},
            ) as create_workflow, patch.object(module, "update_workflow") as update_workflow:
                with redirect_stdout(io.StringIO()) as output:
                    module.push_mode(repo_root, {"primary": object()}, ["primary"], None, False, state)

            create_workflow.assert_called_once()
            update_workflow.assert_not_called()
            self.assertIn("PUSHED", output.getvalue())
            self.assertIn("primary:wf2", state["records"])
            self.assertNotIn("primary:wf1", state["records"])
            self.assertEqual("wf2", state["records"]["primary:wf2"]["workflowId"])


if __name__ == "__main__":
    unittest.main()
