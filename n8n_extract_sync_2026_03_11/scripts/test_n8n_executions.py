#!/usr/bin/env python3
"""Regression tests for n8n execution retry behavior."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_n8n_executions():
    sys.modules.pop("n8n_executions", None)
    sys.modules.pop("n8n_common", None)
    return importlib.import_module("n8n_executions")


class RetryExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_n8n_executions()
        self.instance = self.module.InstanceConfig(
            alias="primary",
            base_url="https://n8n.example.com",
            api_key="key",
        )

    def test_retry_waits_up_to_workflow_execution_timeout(self) -> None:
        with patch.object(self.module, "http_json_request", return_value={"id": "new"}) as http:
            result = self.module.retry_execution(
                self.instance,
                "failed-execution",
                load_workflow=True,
            )

        self.assertEqual({"id": "new"}, result)
        http.assert_called_once_with(
            "POST",
            "https://n8n.example.com/api/v1/executions/failed-execution/retry",
            "key",
            payload={"loadWorkflow": True},
            timeout=300,
        )


if __name__ == "__main__":
    unittest.main()
