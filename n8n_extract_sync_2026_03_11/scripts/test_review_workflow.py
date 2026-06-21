#!/usr/bin/env python3
"""Tests for review_workflow quality gate."""

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
    sys.modules.pop("review_workflow", None)
    return importlib.import_module("review_workflow")


def workflow_root(tmp: Path) -> Path:
    (tmp / "workflows" / "primary" / "sample_wf").mkdir(parents=True, exist_ok=True)
    (tmp / ".n8n_sync").mkdir(exist_ok=True)
    return tmp / "workflows" / "primary" / "sample_wf" / "workflow.json"


class ReviewWorkflowTests(unittest.TestCase):
    def test_quality_gate_fails_on_process_env(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            wf_path = workflow_root(root)
            wf_path.write_text(
                json.dumps(
                    {
                        "id": "wf1",
                        "name": "Env Reader",
                        "nodes": [
                            {
                                "id": "n1",
                                "name": "Code",
                                "type": "n8n-nodes-base.code",
                                "parameters": {
                                    "jsCode": "return [{ json: { key: process.env.API_KEY } }];"
                                },
                            }
                        ],
                        "connections": {},
                    }
                ),
                encoding="utf-8",
            )

            exit_code = module.main(
                [
                    "--workspace-root",
                    str(root),
                    "--quality-gate",
                    str(wf_path),
                ]
            )

            context = json.loads((root / ".n8n_sync" / "review_context.json").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 2)
            self.assertEqual(context["qualityGateStatus"], "fail")
            self.assertEqual(context["summaries"][0]["findings"][0]["code"], "code-process-env")

    def test_crypto_usage_is_ignored(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            wf_path = workflow_root(root)
            wf_path.write_text(
                json.dumps(
                    {
                        "id": "wf1",
                        "name": "Crypto Helper",
                        "nodes": [
                            {
                                "id": "n1",
                                "name": "Code",
                                "type": "n8n-nodes-base.code",
                                "parameters": {
                                    "jsCode": "const crypto = require('crypto'); return [{ json: {} }];"
                                },
                            }
                        ],
                        "connections": {},
                    }
                ),
                encoding="utf-8",
            )

            exit_code = module.main(
                [
                    "--workspace-root",
                    str(root),
                    "--quality-gate",
                    str(wf_path),
                ]
            )

            context = json.loads((root / ".n8n_sync" / "review_context.json").read_text(encoding="utf-8"))
            codes = {finding["code"] for finding in context["summaries"][0]["findings"]}
            self.assertEqual(exit_code, 0)
            self.assertEqual(context["qualityGateStatus"], "pass")
            self.assertEqual(codes, set())

    def test_http_request_without_credentials_is_ignored(self) -> None:
        module = load_module()
        payload = {
            "id": "wf1",
            "name": "HTTP Workflow",
            "nodes": [
                {
                    "id": "n1",
                    "name": "Request",
                    "type": "n8n-nodes-base.httpRequest",
                    "parameters": {
                        "url": "https://api.example.com/data",
                        "authentication": "predefinedCredentialType",
                    },
                }
            ],
            "connections": {},
        }

        summary = module.summarize_workflow(Path("workflow.json"), payload, Path.cwd(), module.parse_args(["workflow.json"]))
        self.assertEqual(summary["findings"], [])
        self.assertEqual(summary["qualityGateStatus"], "pass")

    def test_process_env_in_comment_does_not_fail(self) -> None:
        module = load_module()
        payload = {
            "id": "wf1",
            "name": "Comment Only",
            "nodes": [
                {
                    "id": "n1",
                    "name": "Code",
                    "type": "n8n-nodes-base.code",
                    "parameters": {
                        "jsCode": "// do not use process.env here\nreturn [{ json: {} }];"
                    },
                }
            ],
            "connections": {},
        }

        summary = module.summarize_workflow(Path("workflow.json"), payload, Path.cwd(), module.parse_args(["workflow.json"]))
        codes = {finding["code"] for finding in summary["findings"]}
        self.assertNotIn("code-process-env", codes)

    def test_orphaned_node_is_ignored(self) -> None:
        module = load_module()
        payload = {
            "id": "wf1",
            "name": "Orphan Workflow",
            "nodes": [
                {
                    "id": "n1",
                    "name": "Lonely",
                    "type": "n8n-nodes-base.set",
                    "parameters": {},
                }
            ],
            "connections": {},
        }

        summary = module.summarize_workflow(Path("workflow.json"), payload, Path.cwd(), module.parse_args(["workflow.json"]))
        self.assertEqual(summary["findings"], [])
        self.assertEqual(summary["qualityGateStatus"], "pass")

    def test_missing_node_reference_in_expression_errors(self) -> None:
        module = load_module()
        payload = {
            "id": "wf1",
            "name": "Broken Expression Workflow",
            "nodes": [
                {
                    "id": "n1",
                    "name": "Prepare",
                    "type": "n8n-nodes-base.set",
                    "parameters": {
                        "value": "={{ $(\"prep_rescuetime\").item.json.total }}",
                    },
                }
            ],
            "connections": {},
        }

        summary = module.summarize_workflow(Path("workflow.json"), payload, Path.cwd(), module.parse_args(["workflow.json"]))
        codes = {finding["code"] for finding in summary["findings"]}
        severities = {finding["severity"] for finding in summary["findings"] if finding["code"] == "stale-node-reference"}
        self.assertIn("stale-node-reference", codes)
        self.assertIn("error", severities)
        self.assertEqual(summary["qualityGateStatus"], "fail")

    def test_stale_connection_to_deleted_node_errors(self) -> None:
        module = load_module()
        payload = {
            "id": "wf1",
            "name": "Broken Connection Workflow",
            "nodes": [
                {
                    "id": "n1",
                    "name": "Trigger",
                    "type": "n8n-nodes-base.manualTrigger",
                    "parameters": {},
                },
                {
                    "id": "n2",
                    "name": "Set",
                    "type": "n8n-nodes-base.set",
                    "parameters": {},
                },
            ],
            "connections": {
                "Trigger": {
                    "main": [[{"node": "Deleted Node", "type": "main", "index": 0}]]
                }
            },
        }

        summary = module.summarize_workflow(Path("workflow.json"), payload, Path.cwd(), module.parse_args(["workflow.json"]))
        codes = {finding["code"] for finding in summary["findings"]}
        severities = {finding["severity"] for finding in summary["findings"] if finding["code"] == "stale-connection-node-reference"}
        self.assertIn("stale-connection-node-reference", codes)
        self.assertIn("error", severities)
        self.assertEqual(summary["qualityGateStatus"], "fail")

    def test_clean_simple_workflow_passes(self) -> None:
        module = load_module()
        payload = {
            "id": "wf1",
            "name": "Clean Workflow",
            "nodes": [
                {
                    "id": "n1",
                    "name": "Trigger",
                    "type": "n8n-nodes-base.manualTrigger",
                    "parameters": {},
                },
                {
                    "id": "n2",
                    "name": "Set",
                    "type": "n8n-nodes-base.set",
                    "parameters": {},
                },
            ],
            "connections": {
                "Trigger": {
                    "main": [[{"node": "Set", "type": "main", "index": 0}]]
                }
            },
        }

        summary = module.summarize_workflow(Path("workflow.json"), payload, Path.cwd(), module.parse_args(["workflow.json"]))
        self.assertEqual(summary["qualityGateStatus"], "pass")
        self.assertEqual(summary["findings"], [])

    def test_changed_only_allows_reference_to_unchanged_node(self) -> None:
        module = load_module()
        payload = {
            "id": "wf1",
            "name": "Changed Only Workflow",
            "nodes": [
                {
                    "id": "n1",
                    "name": "Start",
                    "type": "n8n-nodes-base.set",
                    "parameters": {},
                },
                {
                    "id": "n2",
                    "name": "Transform",
                    "type": "n8n-nodes-base.set",
                    "parameters": {
                        "value": "={{ $(\"Start\").item.json.total }}",
                    },
                },
            ],
            "connections": {
                "Start": {
                    "main": [[{"node": "Transform", "type": "main", "index": 0}]]
                }
            },
        }
        remote_payload = {
            "id": "wf1",
            "name": "Changed Only Workflow",
            "nodes": [
                {
                    "id": "n1",
                    "name": "Start",
                    "type": "n8n-nodes-base.set",
                    "parameters": {},
                },
                {
                    "id": "n2",
                    "name": "Transform",
                    "type": "n8n-nodes-base.set",
                    "parameters": {
                        "value": "={{ $(\"Start\").item.json.previous }}",
                    },
                },
            ],
            "connections": {
                "Start": {
                    "main": [[{"node": "Transform", "type": "main", "index": 0}]]
                }
            },
        }

        original_remote_context = module._remote_context
        try:
            module._remote_context = lambda workspace_root, args, payload: (remote_payload, "wf1", None)
            summary = module.summarize_workflow(
                Path("workflow.json"),
                payload,
                Path.cwd(),
                module.parse_args(["workflow.json", "--changed-only"]),
            )
        finally:
            module._remote_context = original_remote_context

        self.assertEqual(summary["reviewScope"], "changed-only")
        self.assertEqual(summary["changedNodeNames"], ["Transform"])
        self.assertEqual(summary["qualityGateStatus"], "pass")
        self.assertEqual(summary["findings"], [])

    def test_changed_only_with_no_diffs_does_not_fall_back_to_full_review(self) -> None:
        module = load_module()
        payload = {
            "id": "wf1",
            "name": "No Diff Workflow",
            "nodes": [
                {
                    "id": "n1",
                    "name": "Code",
                    "type": "n8n-nodes-base.code",
                    "parameters": {
                        "jsCode": "return [{ json: { key: process.env.API_KEY } }];"
                    },
                }
            ],
            "connections": {},
        }

        original_remote_context = module._remote_context
        try:
            module._remote_context = lambda workspace_root, args, payload: (payload, "wf1", None)
            summary = module.summarize_workflow(
                Path("workflow.json"),
                payload,
                Path.cwd(),
                module.parse_args(["workflow.json", "--changed-only"]),
            )
        finally:
            module._remote_context = original_remote_context

        self.assertEqual(summary["reviewScope"], "changed-only")
        self.assertEqual(summary["changedNodeNames"], [])
        self.assertEqual(summary["qualityGateStatus"], "pass")
        self.assertEqual(summary["findings"], [])


if __name__ == "__main__":
    unittest.main()
