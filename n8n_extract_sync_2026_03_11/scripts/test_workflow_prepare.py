#!/usr/bin/env python3
"""Tests for workflow_prepare helper."""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    sys.modules.pop("workflow_prepare", None)
    return importlib.import_module("workflow_prepare")


class WorkflowPrepareTests(unittest.TestCase):
    def test_mirror_active_version_updates_nodes_and_connections(self) -> None:
        module = load_module()
        payload = {
            "id": "wf1",
            "name": "Example",
            "nodes": [{"id": "n1", "name": "Node A"}],
            "connections": {"Node A": {"main": []}},
            "settings": {"executionOrder": "v1"},
            "activeVersion": {
                "nodes": [{"id": "stale", "name": "Old"}],
                "connections": {},
                "settings": {"executionOrder": "v0"},
            },
        }

        changed = module.mirror_active_version(payload)

        self.assertEqual(
            changed,
            ["activeVersion.nodes", "activeVersion.connections"],
        )
        self.assertEqual(payload["activeVersion"]["nodes"], payload["nodes"])
        self.assertEqual(payload["activeVersion"]["connections"], payload["connections"])

    def test_resolve_workflow_path_uses_state_record(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workflows").mkdir()
            (root / ".n8n_sync").mkdir()
            state = {
                "records": {
                    "primary:wf1": {
                        "instance": "primary",
                        "workflowId": "wf1",
                        "localPath": "workflows\\primary\\example_wf1\\workflow.json",
                    }
                }
            }
            (root / ".n8n_sync" / "state.json").write_text(json.dumps(state), encoding="utf-8")
            args = module.parse_args(["--workflow-id", "wf1"])

            resolved = module.resolve_workflow_path(args, root)

            self.assertEqual(
                resolved,
                (root / "workflows" / "primary" / "example_wf1" / "workflow.json").resolve(),
            )

    def test_load_json_with_context_reports_line_and_column(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workflow.json"
            path.write_text('{\n  "nodes": [\n    {}\n  ],\n  "connections": {}\n  "activeVersion": {}\n}\n', encoding="utf-8")

            with self.assertRaises(module.SyncError) as ctx:
                module.load_json_with_context(path)

            message = str(ctx.exception)
            self.assertIn("line 6, column 3", message)
            self.assertIn("Invalid JSON", message)


if __name__ == "__main__":
    unittest.main()
