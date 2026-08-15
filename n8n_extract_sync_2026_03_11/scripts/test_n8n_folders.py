#!/usr/bin/env python3
"""Tests for n8n_folders: folder-name resolution and the dry-run move path.

Uses mocked HTTP (patching n8n_common.http_json_request and list_workflows) so
no network calls are made.
"""

from __future__ import annotations

import importlib
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_n8n_folders():
    sys.modules.pop("n8n_folders", None)
    sys.modules.pop("n8n_common", None)
    return importlib.import_module("n8n_folders")


FOLDERS_PAYLOAD = [
    {"id": "f1", "name": "Inbox", "parentFolderId": ""},
    {"id": "f2", "name": "Production", "parentFolderId": "f1"},
    {"id": "f3", "name": "production-backups", "parentFolderId": ""},
    {"id": "f4", "name": "Archive", "parentFolderId": ""},
]


class FolderResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_n8n_folders()

    def test_resolve_by_exact_id(self) -> None:
        folder = self.module.resolve_folder(FOLDERS_PAYLOAD, "f2")
        self.assertEqual("f2", self.module._folder_id_of(folder))
        self.assertEqual("Production", self.module._folder_name_of(folder))

    def test_resolve_by_case_insensitive_exact_name(self) -> None:
        folder = self.module.resolve_folder(FOLDERS_PAYLOAD, "production")
        self.assertEqual("f2", self.module._folder_id_of(folder))

    def test_resolve_by_partial_name_when_unique(self) -> None:
        folder = self.module.resolve_folder(FOLDERS_PAYLOAD, "arch")
        self.assertEqual("f4", self.module._folder_id_of(folder))

    def test_resolve_ambiguous_partial_name_errors_with_candidates(self) -> None:
        # "production" matches both "Production" (exact) — but "prod" is a
        # partial that matches "Production" and "production-backups".
        with self.assertRaises(self.module.SyncError) as ctx:
            self.module.resolve_folder(FOLDERS_PAYLOAD, "prod")
        message = str(ctx.exception)
        self.assertIn("Ambiguous", message)
        self.assertIn("Production", message)
        self.assertIn("production-backups", message)
        self.assertIn("f2", message)
        self.assertIn("f3", message)

    def test_resolve_no_match_errors(self) -> None:
        with self.assertRaises(self.module.SyncError) as ctx:
            self.module.resolve_folder(FOLDERS_PAYLOAD, "nonexistent-folder")
        self.assertIn("No folder matched", str(ctx.exception))

    def test_resolve_empty_target_errors(self) -> None:
        with self.assertRaises(self.module.SyncError):
            self.module.resolve_folder(FOLDERS_PAYLOAD, "")


class ListFoldersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_n8n_folders()

    def _fake_instance(self):
        return self.module.InstanceConfig(
            alias="primary", base_url="https://n8n.example.com", api_key="key"
        )

    def test_list_folders_uses_public_endpoint_when_it_returns_a_list(self) -> None:
        inst = self._fake_instance()
        with patch.object(self.module, "http_json_request") as http:
            http.return_value = FOLDERS_PAYLOAD
            folders, endpoint = self.module.list_folders(inst)
        self.assertEqual(FOLDERS_PAYLOAD, folders)
        self.assertIn("/api/v1/folders", endpoint)
        # Should not have tried the internal endpoint.
        self.assertEqual(1, http.call_count)

    def test_list_folders_falls_back_to_internal_endpoint_on_error(self) -> None:
        inst = self._fake_instance()
        with patch.object(self.module, "http_json_request") as http:
            http.side_effect = [
                self.module.SyncError("HTTP 404 not found"),
                FOLDERS_PAYLOAD,
            ]
            folders, endpoint = self.module.list_folders(inst)
        self.assertEqual(FOLDERS_PAYLOAD, folders)
        self.assertIn("/rest/folders", endpoint)
        self.assertEqual(2, http.call_count)

    def test_list_folders_raises_when_both_endpoints_fail(self) -> None:
        inst = self._fake_instance()
        with patch.object(self.module, "http_json_request") as http:
            http.side_effect = [
                self.module.SyncError("HTTP 404"),
                self.module.SyncError("HTTP 500"),
            ]
            with self.assertRaises(self.module.SyncError):
                self.module.list_folders(inst)
        self.assertEqual(2, http.call_count)


class MoveDryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_n8n_folders()

    def _fake_instance(self):
        return self.module.InstanceConfig(
            alias="primary", base_url="https://n8n.example.com", api_key="key"
        )

    def test_move_dry_run_does_not_call_api(self) -> None:
        inst = self._fake_instance()
        unfiled_workflow = {"id": "wf1", "name": "My Workflow", "folderId": None}

        args = self.module.build_parser().parse_args(
            [
                "--mode", "move",
                "--instance", "primary",
                "--dotenv", "./secrets/.env.n8n",
                "--workflow-id", "wf1",
                "--folder", "Inbox",
                "--dry-run",
            ]
        )

        with patch.object(self.module, "list_folders", return_value=(FOLDERS_PAYLOAD, "public /api/v1/folders")), \
             patch.object(self.module, "list_workflows", return_value=[unfiled_workflow]), \
             patch.object(self.module, "move_workflow") as move_workflow, \
             patch.object(self.module, "_load_instance", return_value=inst):
            with redirect_stdout(io.StringIO()) as output:
                self.module.cmd_move(args)

        move_workflow.assert_not_called()
        text = output.getvalue()
        self.assertIn("My Workflow", text)
        self.assertIn("wf1", text)
        self.assertIn("unfiled", text)
        self.assertIn("Inbox", text)
        self.assertIn("f1", text)
        self.assertIn("[dry-run]", text)

    def test_move_dry_run_shows_source_folder_when_already_filed(self) -> None:
        inst = self._fake_instance()
        filed_workflow = {"id": "wf1", "name": "My Workflow", "folderId": "f2"}

        args = self.module.build_parser().parse_args(
            [
                "--mode", "move",
                "--instance", "primary",
                "--dotenv", "./secrets/.env.n8n",
                "--workflow-id", "wf1",
                "--folder", "Archive",
                "--dry-run",
            ]
        )

        with patch.object(self.module, "list_folders", return_value=(FOLDERS_PAYLOAD, "public /api/v1/folders")), \
             patch.object(self.module, "list_workflows", return_value=[filed_workflow]), \
             patch.object(self.module, "move_workflow") as move_workflow, \
             patch.object(self.module, "_load_instance", return_value=inst):
            with redirect_stdout(io.StringIO()) as output:
                self.module.cmd_move(args)

        move_workflow.assert_not_called()
        text = output.getvalue()
        self.assertIn("Production (f2)", text)  # source label includes folder name + id
        self.assertIn("Archive", text)

    def test_move_errors_when_workflow_not_found(self) -> None:
        inst = self._fake_instance()
        args = self.module.build_parser().parse_args(
            [
                "--mode", "move",
                "--instance", "primary",
                "--dotenv", "./secrets/.env.n8n",
                "--workflow-id", "missing-wf",
                "--folder", "Inbox",
                "--dry-run",
            ]
        )

        with patch.object(self.module, "list_folders", return_value=(FOLDERS_PAYLOAD, "public /api/v1/folders")), \
             patch.object(self.module, "list_workflows", return_value=[]), \
             patch.object(self.module, "move_workflow") as move_workflow, \
             patch.object(self.module, "_load_instance", return_value=inst):
            with self.assertRaises(self.module.SyncError):
                self.module.cmd_move(args)

        move_workflow.assert_not_called()


class MoveExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_n8n_folders()

    def _fake_instance(self):
        return self.module.InstanceConfig(
            alias="primary", base_url="https://n8n.example.com", api_key="key"
        )

    def test_move_uses_public_put_first_and_reports_method(self) -> None:
        inst = self._fake_instance()
        unfiled_workflow = {"id": "wf1", "name": "My Workflow", "folderId": None}

        args = self.module.build_parser().parse_args(
            [
                "--mode", "move",
                "--instance", "primary",
                "--dotenv", "./secrets/.env.n8n",
                "--workflow-id", "wf1",
                "--folder", "Inbox",
            ]
        )

        with patch.object(self.module, "list_folders", return_value=(FOLDERS_PAYLOAD, "public /api/v1/folders")), \
             patch.object(self.module, "list_workflows", return_value=[unfiled_workflow]), \
             patch.object(self.module, "move_workflow", return_value=({"id": "wf1", "folderId": "f1"}, "public PUT /api/v1/workflows/{id}")) as move_workflow, \
             patch.object(self.module, "_load_instance", return_value=inst):
            with redirect_stdout(io.StringIO()) as output:
                self.module.cmd_move(args)

        move_workflow.assert_called_once()
        text = output.getvalue()
        self.assertIn("Moved via public PUT", text)
        self.assertIn("Confirmed folderId: f1", text)

    def test_move_workflow_falls_back_to_internal_patch(self) -> None:
        inst = self._fake_instance()
        workflow_payload = {"id": "wf1", "name": "My Workflow", "nodes": [], "connections": {}}

        with patch.object(self.module, "move_workflow_via_public_api", side_effect=self.module.SyncError("HTTP 400 folderId rejected")), \
             patch.object(self.module, "move_workflow_via_internal_patch", return_value={"id": "wf1", "folderId": "f1"}) as patch_call, \
             patch.object(self.module, "move_workflow_via_internal_move") as move_call:
            result, method = self.module.move_workflow(inst, "wf1", "f1", workflow=workflow_payload)

        self.assertEqual({"id": "wf1", "folderId": "f1"}, result)
        self.assertIn("internal PATCH", method)
        patch_call.assert_called_once()
        move_call.assert_not_called()

    def test_move_workflow_raises_when_all_methods_fail(self) -> None:
        inst = self._fake_instance()
        workflow_payload = {"id": "wf1", "name": "My Workflow", "nodes": [], "connections": {}}

        with patch.object(self.module, "move_workflow_via_public_api", side_effect=self.module.SyncError("pub fail")), \
             patch.object(self.module, "move_workflow_via_internal_patch", side_effect=self.module.SyncError("patch fail")), \
             patch.object(self.module, "move_workflow_via_internal_move", side_effect=self.module.SyncError("move fail")):
            with self.assertRaises(self.module.SyncError) as ctx:
                self.module.move_workflow(inst, "wf1", "f1", workflow=workflow_payload)

        message = str(ctx.exception)
        self.assertIn("All move methods failed", message)
        self.assertIn("pub fail", message)
        self.assertIn("patch fail", message)
        self.assertIn("move fail", message)


class UnfiledTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_n8n_folders()

    def _fake_instance(self):
        return self.module.InstanceConfig(
            alias="primary", base_url="https://n8n.example.com", api_key="key"
        )

    def test_unfiled_filters_workflows_with_null_folder_id(self) -> None:
        inst = self._fake_instance()
        workflows = [
            {"id": "wf1", "name": "Unfiled A", "folderId": None},
            {"id": "wf2", "name": "Filed B", "folderId": "f1"},
            {"id": "wf3", "name": "Unfiled C", "folderId": ""},
            {"id": "wf4", "name": "Filed D", "folderId": "f2"},
        ]
        args = self.module.build_parser().parse_args(
            ["--mode", "unfiled", "--instance", "primary", "--dotenv", "./secrets/.env.n8n"]
        )
        with patch.object(self.module, "list_workflows", return_value=workflows), \
             patch.object(self.module, "_load_instance", return_value=inst):
            with redirect_stdout(io.StringIO()) as output:
                self.module.cmd_unfiled(args)
        text = output.getvalue()
        self.assertIn("wf1", text)
        self.assertIn("Unfiled A", text)
        self.assertIn("wf3", text)
        self.assertIn("Unfiled C", text)
        self.assertNotIn("wf2", text)
        self.assertNotIn("wf4", text)


if __name__ == "__main__":
    unittest.main()
