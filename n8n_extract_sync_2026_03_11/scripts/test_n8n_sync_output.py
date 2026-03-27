#!/usr/bin/env python3
"""Tests for console-safe n8n_sync output formatting."""

from __future__ import annotations

import importlib
import io
import os
import sys
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


class FakeStdout:
    def __init__(self, encoding: str | None) -> None:
        self.encoding = encoding

    def isatty(self) -> bool:
        return False

    def write(self, _: str) -> int:
        return 0

    def flush(self) -> None:
        return None


class FakeEncodedCapture(FakeStdout):
    def __init__(self, encoding: str | None) -> None:
        super().__init__(encoding)
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        self.parts.append(text)
        return len(text)

    def getvalue(self) -> str:
        return "".join(self.parts)


class N8nSyncOutputTests(unittest.TestCase):
    def test_stream_supports_unicode_for_utf8(self) -> None:
        module = load_n8n_sync()
        self.assertTrue(module._stream_supports_unicode(FakeStdout("utf-8")))

    def test_stream_supports_unicode_rejects_cp1252(self) -> None:
        module = load_n8n_sync()
        self.assertFalse(module._stream_supports_unicode(FakeStdout("cp1252")))

    def test_ascii_override_disables_unicode(self) -> None:
        previous = os.environ.get("N8N_SYNC_ASCII")
        os.environ["N8N_SYNC_ASCII"] = "1"
        try:
            module = load_n8n_sync()
            self.assertFalse(module._USE_UNICODE)
        finally:
            if previous is None:
                os.environ.pop("N8N_SYNC_ASCII", None)
            else:
                os.environ["N8N_SYNC_ASCII"] = previous

    def test_print_instance_status_falls_back_to_ascii(self) -> None:
        module = load_n8n_sync()
        previous = module._USE_UNICODE
        module._USE_UNICODE = False
        try:
            capture = io.StringIO()
            with redirect_stdout(capture):
                module.print_instance_status("primary", True, "ready")
                module.print_instance_status("secondary", False, "blocked")
        finally:
            module._USE_UNICODE = previous

        output = capture.getvalue()
        self.assertIn("OK", output)
        self.assertIn("X", output)
        self.assertNotIn("✓", output)
        self.assertNotIn("✗", output)

    def test_safe_text_replaces_unencodable_characters(self) -> None:
        module = load_n8n_sync()
        self.assertEqual(module._safe_text("Idea 💡", FakeStdout("cp1252")), "Idea ?")

    def test_print_workflow_line_handles_cp1252_workflow_names(self) -> None:
        module = load_n8n_sync()
        previous_unicode = module._USE_UNICODE
        capture = FakeEncodedCapture("cp1252")
        module._USE_UNICODE = False
        try:
            with patch.object(module.sys, "stdout", capture):
                module._print_workflow_line(
                    "PUSHED",
                    "Idea 💡",
                    True,
                    "2026-03-11T17:48:00.546Z",
                    "123456789abc",
                    "workflows/primary/example/workflow.json",
                    "<-",
                )
        finally:
            module._USE_UNICODE = previous_unicode

        output = capture.getvalue()
        self.assertIn("Idea ?", output)
        self.assertNotIn("💡", output)


if __name__ == "__main__":
    unittest.main()
