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
                with redirect_stdout(io.StringIO()) as output:
                    module.push_mode(repo_root, {"primary": object()}, ["primary"], None, False, state)

            get_workflow.assert_not_called()
            update_workflow.assert_not_called()
            self.assertNotRegex(output.getvalue(), r"(?m)^\s+CLEAN\s")

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
            self.assertNotRegex(output.getvalue(), r"(?m)^\s+CLEAN\s")

    def test_dry_run_verbose_reports_unchanged_local_workflow(self) -> None:
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
                    module.push_mode(repo_root, {"primary": object()}, ["primary"], None, True, state, verbose=True)

            get_workflow.assert_not_called()
            update_workflow.assert_not_called()
            self.assertRegex(output.getvalue(), r"(?m)^\s+CLEAN\s")

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

            created_remote = {"id": "wf2", "name": "Example edited", "active": False, "nodes": [], "connections": {}}
            with patch.object(
                module,
                "get_workflow",
                # First call (inspect) 404s; second call fetches the canonical
                # workflow that was just created.
                side_effect=[module.SyncError("HTTP 404 missing"), created_remote],
            ), patch.object(
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

    def test_backup_skips_archived_workflow_summaries(self) -> None:
        module = load_n8n_sync()
        instance = object()
        archived_summary = {"id": "wf-archived", "name": "Archived", "archived": True}
        active_summary = {"id": "wf1", "name": "Active", "active": False}
        active_payload = {**active_summary, "nodes": [], "connections": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            state = {"records": {}}

            with patch.object(module, "list_workflows", return_value=[archived_summary, active_summary]), patch.object(
                module,
                "get_workflow",
                return_value=active_payload,
            ) as get_workflow:
                with redirect_stdout(io.StringIO()):
                    module.backup_mode(repo_root, {"primary": instance}, ["primary"], None, False, state)

            get_workflow.assert_called_once_with(instance, "wf1")
            self.assertIn("primary:wf1", state["records"])
            self.assertNotIn("primary:wf-archived", state["records"])

    def test_backup_hides_unchanged_workflows_by_default(self) -> None:
        module = load_n8n_sync()
        instance = object()
        summary = {"id": "wf1", "name": "Active", "active": False}
        payload = {**summary, "nodes": [], "connections": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "list_workflows", return_value=[summary]), patch.object(
                module,
                "get_workflow",
                return_value=payload,
            ):
                with redirect_stdout(io.StringIO()) as output:
                    module.backup_mode(repo_root, {"primary": instance}, ["primary"], None, False, state)

            self.assertNotRegex(output.getvalue(), r"(?m)^\s+UNCHANGED\s")

    def test_backup_verbose_reports_unchanged_workflows(self) -> None:
        module = load_n8n_sync()
        instance = object()
        summary = {"id": "wf1", "name": "Active", "active": False}
        payload = {**summary, "nodes": [], "connections": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "list_workflows", return_value=[summary]), patch.object(
                module,
                "get_workflow",
                return_value=payload,
            ):
                with redirect_stdout(io.StringIO()) as output:
                    module.backup_mode(
                        repo_root,
                        {"primary": instance},
                        ["primary"],
                        None,
                        False,
                        state,
                        verbose=True,
                    )

            self.assertRegex(output.getvalue(), r"(?m)^\s+UNCHANGED\s")

    def test_status_hides_clean_workflows_by_default(self) -> None:
        module = load_n8n_sync()
        payload = {"id": "wf1", "name": "Active", "active": False, "nodes": [], "connections": {}}
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "list_workflows", return_value=[{"id": "wf1", "name": "Active", "active": False}]), patch.object(
                module,
                "get_workflow",
                return_value=payload,
            ):
                with redirect_stdout(io.StringIO()) as output:
                    exit_code = module.status_mode(repo_root, {"primary": object()}, ["primary"], state)

        self.assertEqual(0, exit_code)
        self.assertNotRegex(output.getvalue(), r"(?m)^\s+CLEAN\s")

    def test_status_verbose_reports_clean_workflows(self) -> None:
        module = load_n8n_sync()
        payload = {"id": "wf1", "name": "Active", "active": False, "nodes": [], "connections": {}}
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "list_workflows", return_value=[{"id": "wf1", "name": "Active", "active": False}]), patch.object(
                module,
                "get_workflow",
                return_value=payload,
            ):
                with redirect_stdout(io.StringIO()) as output:
                    exit_code = module.status_mode(repo_root, {"primary": object()}, ["primary"], state, verbose=True)

        self.assertEqual(0, exit_code)
        self.assertRegex(output.getvalue(), r"(?m)^\s+CLEAN\s")

    def test_backup_prunes_existing_archived_workflow_from_local_mirror(self) -> None:
        module = load_n8n_sync()
        instance = object()
        archived_payload = {"id": "wf1", "name": "Archived", "active": False, "nodes": [], "connections": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, archived_payload)
            state = self._state_for(module, repo_root, workflow_path, archived_payload)

            with patch.object(module, "list_workflows", return_value=[{"id": "wf-archived", "archived": True}]), patch.object(
                module,
                "get_workflow",
            ) as get_workflow:
                with redirect_stdout(io.StringIO()) as output:
                    module.backup_mode(repo_root, {"primary": instance}, ["primary"], None, False, state)

            get_workflow.assert_not_called()
            self.assertNotIn("primary:wf1", state["records"])
            self.assertFalse(workflow_path.parent.exists())
            self.assertIn("DELETE", output.getvalue())

    def test_sync_two_way_imports_remote_only_workflow(self) -> None:
        module = load_n8n_sync()
        instance = object()
        remote_summary = {"id": "wf1", "name": "Remote only", "active": True}
        remote_payload = {
            "id": "wf1",
            "name": "Remote only",
            "active": True,
            "nodes": [],
            "connections": {},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            state = {"records": {}}

            with patch.object(module, "list_workflows", return_value=[remote_summary]), patch.object(
                module,
                "get_workflow",
                return_value=remote_payload,
            ) as get_workflow, patch.object(module, "update_workflow") as update_workflow:
                with redirect_stdout(io.StringIO()) as output:
                    conflicts = module.sync_two_way_mode(
                        repo_root,
                        {"primary": instance},
                        ["primary"],
                        None,
                        False,
                        state,
                    )

            self.assertEqual(0, conflicts)
            get_workflow.assert_called_once_with(instance, "wf1")
            update_workflow.assert_not_called()
            self.assertIn("NEW", output.getvalue())

            record = state["records"]["primary:wf1"]
            self.assertEqual("primary", record["instance"])
            self.assertEqual("wf1", record["workflowId"])
            self.assertEqual("Remote only", record["workflowName"])
            self.assertEqual("remote_to_local", record["lastDirection"])
            self.assertTrue((repo_root / record["localPath"]).exists())

    def test_sync_two_way_hides_clean_workflows_by_default(self) -> None:
        module = load_n8n_sync()
        payload = {"id": "wf1", "name": "Example", "active": False, "nodes": [], "connections": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "list_workflows", return_value=[{"id": "wf1", "name": "Example", "active": False}]), patch.object(
                module,
                "get_workflow",
                return_value=payload,
            ):
                with redirect_stdout(io.StringIO()) as output:
                    conflicts = module.sync_two_way_mode(
                        repo_root,
                        {"primary": object()},
                        ["primary"],
                        None,
                        False,
                        state,
                    )

        self.assertEqual(0, conflicts)
        self.assertNotRegex(output.getvalue(), r"(?m)^\s+CLEAN\s")

    def test_backup_fast_path_skips_get_workflow_when_updated_at_matches(self) -> None:
        module = load_n8n_sync()
        instance = object()
        payload = {
            "id": "wf1",
            "name": "Example",
            "active": False,
            "updatedAt": "2026-06-13T10:00:00.000Z",
            "nodes": [],
            "connections": {},
        }
        summary = {"id": "wf1", "name": "Example", "active": False, "updatedAt": payload["updatedAt"]}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "list_workflows", return_value=[summary]), patch.object(
                module, "get_workflow"
            ) as get_workflow:
                with redirect_stdout(io.StringIO()) as output:
                    module.backup_mode(repo_root, {"primary": instance}, ["primary"], None, False, state, verbose=True)

            get_workflow.assert_not_called()
            self.assertRegex(output.getvalue(), r"(?m)^\s+UNCHANGED\s")

    def test_backup_force_check_bypasses_fast_path(self) -> None:
        module = load_n8n_sync()
        instance = object()
        payload = {
            "id": "wf1",
            "name": "Example",
            "active": False,
            "updatedAt": "2026-06-13T10:00:00.000Z",
            "nodes": [],
            "connections": {},
        }
        summary = {"id": "wf1", "name": "Example", "active": False, "updatedAt": payload["updatedAt"]}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "list_workflows", return_value=[summary]), patch.object(
                module, "get_workflow", return_value=payload
            ) as get_workflow:
                with redirect_stdout(io.StringIO()):
                    module.backup_mode(
                        repo_root, {"primary": instance}, ["primary"], None, False, state, force_check=True
                    )

            get_workflow.assert_called_once_with(instance, "wf1")

    def test_backup_fast_path_refetches_when_remote_updated_at_changed(self) -> None:
        module = load_n8n_sync()
        instance = object()
        payload = {
            "id": "wf1",
            "name": "Example",
            "active": False,
            "updatedAt": "2026-06-13T10:00:00.000Z",
            "nodes": [],
            "connections": {},
        }
        newer = {**payload, "updatedAt": "2026-06-13T12:00:00.000Z"}
        summary = {"id": "wf1", "name": "Example", "active": False, "updatedAt": newer["updatedAt"]}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "list_workflows", return_value=[summary]), patch.object(
                module, "get_workflow", return_value=newer
            ) as get_workflow:
                with redirect_stdout(io.StringIO()):
                    module.backup_mode(repo_root, {"primary": instance}, ["primary"], None, False, state)

            get_workflow.assert_called_once_with(instance, "wf1")

    def test_sync_two_way_fast_path_skips_get_workflow_when_updated_at_matches(self) -> None:
        module = load_n8n_sync()
        instance = object()
        payload = {
            "id": "wf1",
            "name": "Example",
            "active": False,
            "updatedAt": "2026-06-13T10:00:00.000Z",
            "nodes": [],
            "connections": {},
        }
        summary = {"id": "wf1", "name": "Example", "active": False, "updatedAt": payload["updatedAt"]}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "list_workflows", return_value=[summary]), patch.object(
                module, "get_workflow"
            ) as get_workflow, patch.object(module, "update_workflow") as update_workflow:
                with redirect_stdout(io.StringIO()) as output:
                    conflicts = module.sync_two_way_mode(
                        repo_root, {"primary": instance}, ["primary"], None, False, state, verbose=True
                    )

            self.assertEqual(0, conflicts)
            get_workflow.assert_not_called()
            update_workflow.assert_not_called()
            self.assertRegex(output.getvalue(), r"(?m)^\s+CLEAN\s")

    def test_sync_two_way_force_check_bypasses_fast_path(self) -> None:
        module = load_n8n_sync()
        instance = object()
        payload = {
            "id": "wf1",
            "name": "Example",
            "active": False,
            "updatedAt": "2026-06-13T10:00:00.000Z",
            "nodes": [],
            "connections": {},
        }
        summary = {"id": "wf1", "name": "Example", "active": False, "updatedAt": payload["updatedAt"]}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, payload)
            state = self._state_for(module, repo_root, workflow_path, payload)

            with patch.object(module, "list_workflows", return_value=[summary]), patch.object(
                module, "get_workflow", return_value=payload
            ) as get_workflow, patch.object(module, "update_workflow"):
                with redirect_stdout(io.StringIO()):
                    module.sync_two_way_mode(
                        repo_root, {"primary": instance}, ["primary"], None, False, state, force_check=True
                    )

            get_workflow.assert_called_once_with(instance, "wf1")

    def test_sync_two_way_push_records_normalized_remote_hash(self) -> None:
        """After a push, n8n normalizes the workflow server-side. The state must
        record the true remote hash (not the local pre-normalization hash) and
        mirror the canonical remote locally, so a later --force-check stays clean.
        """
        module = load_n8n_sync()
        instance = object()

        # Local edit the user wants to push.
        local_payload = {
            "id": "wf1",
            "name": "Example",
            "active": False,
            "updatedAt": "2026-06-13T10:00:00.000Z",
            "nodes": [{"name": "A", "type": "n8n-nodes-base.noOp"}],
            "connections": {},
        }
        # The remote as it was at last sync (unchanged since) -> no conflict.
        remote_before = dict(local_payload, nodes=[], updatedAt="2026-06-12T09:00:00.000Z")
        # What n8n actually stores after the push: normalized (extra defaults) and
        # a new updatedAt. Its hash differs from the local file's hash.
        normalized_remote = {
            "id": "wf1",
            "name": "Example",
            "active": False,
            "updatedAt": "2026-06-15T06:24:00.000Z",
            "nodes": [{"name": "A", "type": "n8n-nodes-base.noOp", "typeVersion": 1, "position": [0, 0]}],
            "connections": {},
        }
        summary = {"id": "wf1", "name": "Example", "active": False, "updatedAt": remote_before["updatedAt"]}

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            workflow_path = self._write_workflow(module, repo_root, local_payload)
            # Seed state so local is changed (ahead of remote) and remote matches
            # remote_before -> classified as a PUSH, not a conflict.
            state = {
                "records": {
                    "primary:wf1": {
                        "instance": "primary",
                        "workflowId": "wf1",
                        "workflowName": "Example",
                        "localPath": str(workflow_path.relative_to(repo_root)),
                        "versionId": "?",
                        "updatedAt": remote_before["updatedAt"],
                        "lastRemoteHash": module.remote_workflow_hash(remote_before),
                        "lastLocalHash": module.remote_workflow_hash(remote_before),
                        "lastSyncAtUtc": "2026-06-12T09:00:00Z",
                        "lastDirection": "remote_to_local",
                    }
                }
            }

            # First get_workflow = inspect (remote_before); second = post-push refresh.
            with patch.object(module, "list_workflows", return_value=[summary]), patch.object(
                module, "get_workflow", side_effect=[remote_before, normalized_remote]
            ), patch.object(module, "update_workflow", return_value={"id": "wf1"}) as update_workflow:
                with redirect_stdout(io.StringIO()) as output:
                    module.sync_two_way_mode(
                        repo_root, {"primary": instance}, ["primary"], None, False, state, verbose=True
                    )

            update_workflow.assert_called_once()
            self.assertRegex(output.getvalue(), r"(?m)^\s+PUSH\s")

            rec = state["records"]["primary:wf1"]
            normalized_hash = module.remote_workflow_hash(normalized_remote)
            # State must reflect the actual normalized remote, not the local hash.
            self.assertEqual(normalized_hash, rec["lastRemoteHash"])
            self.assertEqual(normalized_hash, rec["lastLocalHash"])
            self.assertEqual(normalized_remote["updatedAt"], rec["updatedAt"])
            # Local mirror must now equal the canonical remote.
            local_path = repo_root / module._normalize_path(rec["localPath"])
            self.assertEqual(normalized_hash, module.local_workflow_hash(local_path))


if __name__ == "__main__":
    unittest.main()
