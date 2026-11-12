#!/usr/bin/env python3
"""Tests for n8n_stash: stash/pop of local workflow exports in temp workspaces.

All tests run against synthetic temporary workspaces — no real workflow
exports, no network, no credentials, no state.json writes.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_n8n_stash():
    sys.modules.pop("n8n_stash", None)
    sys.modules.pop("n8n_common", None)
    return importlib.import_module("n8n_stash")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


WF_ALPHA = json.dumps({"id": "111", "name": "alpha", "nodes": []}, indent=2)
WF_BETA = json.dumps({"id": "222", "name": "beta", "nodes": []}, indent=2)
WF_GAMMA = json.dumps({"id": "333", "name": "gamma", "nodes": []}, indent=2)
WF_UNREG = json.dumps({"id": "999", "name": "unregistered", "nodes": []}, indent=2)
META_ALPHA = json.dumps({"nodeCount": 0}, indent=2)


def make_workspace(root: Path) -> Path:
    """Synthetic workspace: workflows/ + .n8n_sync/ markers plus sample exports."""
    ws = root / "ws"
    (ws / ".n8n_sync").mkdir(parents=True)
    (ws / ".n8n_sync" / "state.json").write_text('{"records": []}', encoding="utf-8")
    for rel, content in [
        ("workflows/primary/alpha/workflow.json", WF_ALPHA),
        ("workflows/primary/alpha/metadata.json", META_ALPHA),
        ("workflows/primary/alpha/notes.txt", "unrelated file"),
        ("workflows/primary/beta/workflow.json", WF_BETA),
        ("workflows/primary/unregistered/workflow.json", WF_UNREG),
        ("workflows/secondary/gamma/workflow.json", WF_GAMMA),
    ]:
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))  # byte-exact, no newline translation
    return ws


class StashTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_n8n_stash()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.ws = make_workspace(root)
        self.utility_gitignore = root / "utility_gitignore"
        self._original_utility_gi = self.mod._utility_gitignore_path
        self.mod._utility_gitignore_path = lambda: self.utility_gitignore
        self._original_lock_timeout = self.mod.LOCK_TIMEOUT_SECONDS
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        self.mod._utility_gitignore_path = self._original_utility_gi
        self.mod.LOCK_TIMEOUT_SECONDS = self._original_lock_timeout

    def run_cli(self, argv: list) -> "tuple[int, str, str]":
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = self.mod.main(argv)
        except SystemExit as exc:  # argparse-level errors
            code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def stash_argv(self, extra: list) -> list:
        return ["stash", "--workspace", str(self.ws)] + extra

    def pop_argv(self, extra: list) -> list:
        return ["pop", "--workspace", str(self.ws)] + extra

    def snapshot(self) -> "dict[str, bytes]":
        files: "dict[str, bytes]" = {}
        for path in sorted(self.ws.rglob("*")):
            if path.is_file():
                files[path.relative_to(self.ws).as_posix()] = path.read_bytes()
        return files

    def entry_dirs(self) -> "list[Path]":
        root = self.ws / ".stash"
        if not root.is_dir():
            return []
        return sorted(
            [p for p in root.iterdir() if p.is_dir()],
            key=lambda p: p.name,
            reverse=True,
        )

    def do_stash(self, extra: "list | None" = None) -> "tuple[int, str, str]":
        extra = list(extra or [])
        if "--all" not in extra and "--workflow-id" not in extra:
            extra = ["--workflow-id", "111"] + extra
        return self.run_cli(self.stash_argv(extra))


class StashCoreTests(StashTestBase):
    def test_stash_moves_files_byte_exact_with_manifest(self) -> None:
        code, out, err = self.do_stash(["--message", "wip experiment"])
        self.assertEqual(0, code, err)
        alpha = self.ws / "workflows" / "primary" / "alpha"
        self.assertFalse((alpha / "workflow.json").exists())
        self.assertFalse((alpha / "metadata.json").exists())
        # Unrelated files stay put.
        self.assertEqual("unrelated file", (alpha / "notes.txt").read_text())
        entries = self.entry_dirs()
        self.assertEqual(1, len(entries))
        stashed_wf = entries[0] / "workflows" / "primary" / "alpha" / "workflow.json"
        stashed_meta = entries[0] / "workflows" / "primary" / "alpha" / "metadata.json"
        self.assertEqual(WF_ALPHA.encode(), stashed_wf.read_bytes())
        self.assertEqual(META_ALPHA.encode(), stashed_meta.read_bytes())
        manifest = json.loads((entries[0] / "manifest.json").read_text())
        self.assertEqual(1, manifest["manifest_version"])
        self.assertEqual(entries[0].name, manifest["entry_id"])
        self.assertEqual("primary", manifest["instance"])
        self.assertEqual("wip experiment", manifest["message"])
        self.assertEqual("committed", manifest["transaction"]["state"])
        wf_entry = manifest["items"][0]["files"][0]
        self.assertEqual("workflows/primary/alpha/workflow.json", wf_entry["relpath"])
        self.assertEqual(len(WF_ALPHA.encode()), wf_entry["size"])
        self.assertEqual(sha256_bytes(WF_ALPHA.encode()), wf_entry["sha256"])

    def test_stash_list_and_show(self) -> None:
        code, out, err = self.do_stash()
        self.assertEqual(0, code, err)
        code, out, err = self.run_cli(["list", "--workspace", str(self.ws)])
        self.assertEqual(0, code, err)
        self.assertIn("complete", out)
        self.assertIn("instance=primary", out)
        code, out, err = self.run_cli(["show", "--workspace", str(self.ws)])
        self.assertEqual(0, code, err)
        self.assertIn("workflows/primary/alpha/workflow.json", out)
        self.assertIn("committed", out)

    def test_unregistered_workflow_can_be_stashed(self) -> None:
        # 'unregistered' is not present in .n8n_sync/state.json; stash must not care.
        code, out, err = self.run_cli(
            self.stash_argv(["--workflow-id", "999"])
        )
        self.assertEqual(0, code, err)
        self.assertFalse(
            (self.ws / "workflows" / "primary" / "unregistered" / "workflow.json").exists()
        )

    def test_all_is_export_only_and_scoped_to_instance(self) -> None:
        before_secondary = (self.ws / "workflows" / "secondary" / "gamma" / "workflow.json").read_bytes()
        code, out, err = self.run_cli(self.stash_argv(["--all"]))
        self.assertEqual(0, code, err)
        for slug in ("alpha", "beta", "unregistered"):
            self.assertFalse(
                (self.ws / "workflows" / "primary" / slug / "workflow.json").exists(),
                slug,
            )
        # Export-only: notes.txt preserved; secondary instance untouched.
        self.assertTrue((self.ws / "workflows" / "primary" / "alpha" / "notes.txt").exists())
        self.assertEqual(
            before_secondary,
            (self.ws / "workflows" / "secondary" / "gamma" / "workflow.json").read_bytes(),
        )
        entries = self.entry_dirs()
        self.assertEqual(1, len(entries))
        manifest = json.loads((entries[0] / "manifest.json").read_text())
        self.assertEqual(3, len(manifest["items"]))

    def test_bare_stash_refused_helpfully(self) -> None:
        code, out, err = self.run_cli(["stash", "--workspace", str(self.ws)])
        self.assertEqual(1, code)
        self.assertIn("Refusing broad capture", err)
        self.assertIn("--workflow-id", err)
        self.assertIn("--all", err)

    def test_both_selector_flags_refused(self) -> None:
        code, out, err = self.run_cli(
            self.stash_argv(["--workflow-id", "111", "--all"])
        )
        self.assertEqual(1, code)
        self.assertIn("Refusing broad capture", err)

    def test_unknown_workflow_id_fails_helpfully(self) -> None:
        code, out, err = self.run_cli(self.stash_argv(["--workflow-id", "nope"]))
        self.assertEqual(1, code)
        self.assertIn("No local workflow export with id 'nope'", err)

    def test_dry_run_changes_nothing_including_ignore(self) -> None:
        before = self.snapshot()
        code, out, err = self.run_cli(self.stash_argv(["--all", "--dry-run"]))
        self.assertEqual(0, code, err)
        self.assertIn("Dry run", out)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.ws / ".gitignore").exists())
        self.assertFalse(self.utility_gitignore.exists())
        self.assertFalse((self.ws / ".stash").exists())

    def test_gitignore_ensured_and_preserved(self) -> None:
        gi = self.ws / ".gitignore"
        gi.write_bytes(b"secrets/\nnode_modules")  # no trailing newline
        code, out, err = self.do_stash()
        self.assertEqual(0, code, err)
        data = gi.read_bytes()
        self.assertTrue(data.startswith(b"secrets/\nnode_modules\n"))
        self.assertTrue(data.endswith(b"/.stash/\n"))
        self.assertEqual(1, data.count(b"/.stash/"))
        # Utility gitignore (patched to a temp path) also gains the ignore line.
        self.assertTrue(self.utility_gitignore.read_bytes().endswith(b"/.stash/\n"))

    def test_sync_state_byte_identical_through_stash_and_pop(self) -> None:
        state = self.ws / ".n8n_sync" / "state.json"
        before = state.read_bytes()
        self.assertEqual(0, self.do_stash()[0])
        self.assertEqual(before, state.read_bytes())
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(0, code, err)
        self.assertEqual(before, state.read_bytes())

    def test_instance_isolation_on_stash(self) -> None:
        code, out, err = self.do_stash()
        self.assertEqual(0, code, err)
        self.assertTrue(
            (self.ws / "workflows" / "secondary" / "gamma" / "workflow.json").exists()
        )


class PopTests(StashTestBase):
    def test_pop_restores_bytes_and_removes_entry(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(0, code, err)
        alpha = self.ws / "workflows" / "primary" / "alpha"
        self.assertEqual(WF_ALPHA.encode(), (alpha / "workflow.json").read_bytes())
        self.assertEqual(META_ALPHA.encode(), (alpha / "metadata.json").read_bytes())
        self.assertEqual([], self.entry_dirs())
        self.assertFalse((self.ws / ".stash" / ".lock").exists())

    def test_two_entry_stack_bare_pop_newest_first(self) -> None:
        self.run_cli(self.stash_argv(["--workflow-id", "111"]))
        self.run_cli(self.stash_argv(["--workflow-id", "222"]))
        entries = self.entry_dirs()
        self.assertEqual(2, len(entries))
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(0, code, err)
        # Newest entry (beta) restored first.
        self.assertTrue((self.ws / "workflows" / "primary" / "beta" / "workflow.json").exists())
        self.assertFalse((self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists())
        self.assertEqual(1, len(self.entry_dirs()))
        # Next bare pop restores the older entry.
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(0, code, err)
        self.assertTrue((self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists())
        self.assertEqual([], self.entry_dirs())

    def test_pop_older_entry_by_index(self) -> None:
        self.run_cli(self.stash_argv(["--workflow-id", "111"]))  # older -> index 1
        self.run_cli(self.stash_argv(["--workflow-id", "222"]))  # newer -> index 0
        code, out, err = self.run_cli(self.pop_argv(["stash@{1}"]))
        self.assertEqual(0, code, err)
        self.assertTrue((self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists())
        self.assertFalse((self.ws / "workflows" / "primary" / "beta" / "workflow.json").exists())

    def test_pop_by_entry_id(self) -> None:
        self.run_cli(self.stash_argv(["--workflow-id", "111"]))
        entry_id = self.entry_dirs()[0].name
        self.run_cli(self.stash_argv(["--workflow-id", "222"]))
        code, out, err = self.run_cli(self.pop_argv([entry_id]))
        self.assertEqual(0, code, err)
        self.assertTrue((self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists())

    def test_pop_conflict_retains_both_sides(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        # Simulate a pull that rewrote the local export while it was stashed.
        pulled = json.dumps({"id": "111", "name": "alpha", "nodes": [{"pulled": True}]})
        target = self.ws / "workflows" / "primary" / "alpha" / "workflow.json"
        target.write_bytes(pulled.encode("utf-8"))
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("CONFLICT", err)
        self.assertIn(str(target), err)
        entry = self.entry_dirs()[0]
        self.assertTrue((entry / "workflows" / "primary" / "alpha" / "workflow.json").exists())
        self.assertEqual(pulled.encode(), target.read_bytes())
        # stash list still shows the retained entry.
        code, out, err = self.run_cli(["list", "--workspace", str(self.ws)])
        self.assertIn("complete", out)

    def test_one_file_conflict_prevents_partial_restore(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        alpha = self.ws / "workflows" / "primary" / "alpha"
        # Only metadata.json conflicts; workflow.json must NOT be restored either.
        (alpha / "metadata.json").write_bytes(b'{"changed": true}')
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertFalse((alpha / "workflow.json").exists())
        self.assertEqual(b'{"changed": true}', (alpha / "metadata.json").read_bytes())
        self.assertEqual(1, len(self.entry_dirs()))

    def test_identical_destination_is_safe_already_restored(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        alpha = self.ws / "workflows" / "primary" / "alpha"
        alpha.mkdir(parents=True, exist_ok=True)
        # User already copied the stashed bytes back by hand.
        shutil.copy2(entry / "workflows" / "primary" / "alpha" / "workflow.json",
                     alpha / "workflow.json")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(0, code, err)
        self.assertEqual(WF_ALPHA.encode(), (alpha / "workflow.json").read_bytes())
        self.assertEqual([], self.entry_dirs())

    def test_pop_dry_run_changes_nothing(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        before = self.snapshot()
        code, out, err = self.run_cli(self.pop_argv(["--dry-run"]))
        self.assertEqual(0, code, err)
        self.assertIn("Dry run", out)
        self.assertEqual(before, self.snapshot())

    def test_pop_no_entries_errors(self) -> None:
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("No stash entries", err)

    def test_pop_invalid_and_out_of_range_selectors(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        code, out, err = self.run_cli(self.pop_argv(["stash@{5}"]))
        self.assertEqual(1, code)
        self.assertIn("out of range", err)
        code, out, err = self.run_cli(self.pop_argv(["../evil"]))
        self.assertEqual(1, code)
        self.assertIn("Invalid selector", err)

    def test_resume_interrupted_restore(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        manifest_path = entry / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["transaction"]["state"] = "incomplete-restore"
        manifest["items"][0]["files"][0]["state"] = "restored"
        manifest["items"][0]["files"][1]["state"] = "stashed"
        # Simulate a crash after workflow.json was restored but before metadata.
        alpha = self.ws / "workflows" / "primary" / "alpha"
        alpha.mkdir(parents=True, exist_ok=True)
        os.replace(entry / "workflows" / "primary" / "alpha" / "workflow.json",
                   alpha / "workflow.json")
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(0, code, err)
        self.assertEqual(META_ALPHA.encode(), (alpha / "metadata.json").read_bytes())
        self.assertEqual([], self.entry_dirs())

    def test_stale_manifest_after_real_move_reconciles(self) -> None:
        """A restore move completed but the progress manifest was never
        persisted: pop must reconcile both locations and succeed."""
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        alpha = self.ws / "workflows" / "primary" / "alpha"
        alpha.mkdir(parents=True, exist_ok=True)
        # The move really happened, but the manifest still says "stashed".
        os.replace(entry / "workflows" / "primary" / "alpha" / "workflow.json",
                   alpha / "workflow.json")
        manifest_path = entry / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["transaction"]["state"] = "restoring"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(0, code, err)
        self.assertEqual(WF_ALPHA.encode(), (alpha / "workflow.json").read_bytes())
        self.assertEqual(META_ALPHA.encode(), (alpha / "metadata.json").read_bytes())
        self.assertEqual([], self.entry_dirs())

    def test_mismatching_restored_destination_fails(self) -> None:
        """Stashed copy already moved, but the destination does not match the
        manifest: refuse, retain the entry, do not touch the destination."""
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        alpha = self.ws / "workflows" / "primary" / "alpha"
        alpha.mkdir(parents=True, exist_ok=True)
        os.replace(entry / "workflows" / "primary" / "alpha" / "workflow.json",
                   alpha / "workflow.json")
        different = b'{"id": "111", "mismatched": true}'
        (alpha / "workflow.json").write_bytes(different)
        manifest_path = entry / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["transaction"]["state"] = "restoring"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("does not match the manifest", err)
        self.assertEqual(different, (alpha / "workflow.json").read_bytes())
        self.assertEqual(1, len(self.entry_dirs()))

    def test_racing_destination_survives_on_pop(self) -> None:
        """Simulated racing destination: a file that appears at the restore
        target during the move must survive byte-for-byte and abort the pop."""
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        target = self.ws / "workflows" / "primary" / "alpha" / "workflow.json"
        racing_bytes = b'{"id": "111", "racing": true}'
        original_link = self.mod.os.link
        calls = {"n": 0}

        def racing_link(src, dst, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1 and Path(dst) == target:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(racing_bytes)  # racer wins the race
            return original_link(src, dst, *a, **kw)

        self.mod.os.link = racing_link
        try:
            code, out, err = self.run_cli(self.pop_argv([]))
        finally:
            self.mod.os.link = original_link
        self.assertEqual(1, code)
        # The racing writer's bytes survived untouched.
        self.assertEqual(racing_bytes, target.read_bytes())
        # The entry is retained (incomplete-restore), payload intact.
        self.assertEqual(1, len(self.entry_dirs()))
        stashed = entry / "workflows" / "primary" / "alpha" / "workflow.json"
        self.assertEqual(WF_ALPHA.encode(), stashed.read_bytes())

    def test_hard_link_unsupported_falls_back_to_exclusive_copy(self) -> None:
        """Without hard-link support the move falls back to exclusive-create +
        verified copy: still no-overwrite, no bytes lost."""
        original_link = self.mod.os.link

        def unsupported_link(src, dst, *a, **kw):
            raise OSError(1, "hard links not supported here")

        self.mod.os.link = unsupported_link
        try:
            code, out, err = self.do_stash()
        finally:
            self.mod.os.link = original_link
        self.assertEqual(0, code, err)
        alpha = self.ws / "workflows" / "primary" / "alpha"
        self.assertFalse((alpha / "workflow.json").exists())
        entry = self.entry_dirs()[0]
        self.assertEqual(
            WF_ALPHA.encode(),
            (entry / "workflows" / "primary" / "alpha" / "workflow.json").read_bytes(),
        )
        # Restore works through the fallback too.
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(0, code, err)
        self.assertEqual(WF_ALPHA.encode(), (alpha / "workflow.json").read_bytes())
        self.assertEqual([], self.entry_dirs())


class DropTests(StashTestBase):
    def test_drop_requires_explicit_selector(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        code, out, err = self.run_cli(["drop", "--workspace", str(self.ws)])
        self.assertEqual(1, code)
        self.assertIn("explicit selector", err)

    def test_drop_removes_verified_complete_entry(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        code, out, err = self.run_cli(["drop", "--workspace", str(self.ws), entry.name])
        self.assertEqual(0, code, err)
        self.assertEqual([], self.entry_dirs())

    def test_drop_dry_run_changes_nothing(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        before = self.snapshot()
        code, out, err = self.run_cli(
            ["drop", "--workspace", str(self.ws), entry.name, "--dry-run"]
        )
        self.assertEqual(0, code, err)
        self.assertEqual(before, self.snapshot())

    def test_drop_refuses_modified_payload(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        target = entry / "workflows" / "primary" / "alpha" / "workflow.json"
        target.write_text('{"tampered": true}', encoding="utf-8")
        code, out, err = self.run_cli(["drop", "--workspace", str(self.ws), entry.name])
        self.assertEqual(1, code)
        self.assertRegex(err, "modified|mismatch")
        self.assertTrue(entry.exists())

    def test_drop_refuses_incomplete_entry(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        manifest_path = entry / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["transaction"]["state"] = "incomplete-rollback"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        code, out, err = self.run_cli(["drop", "--workspace", str(self.ws), entry.name])
        self.assertEqual(1, code)
        self.assertIn("not complete", err)


class IntegritySecurityTests(StashTestBase):
    def do_committed_stash(self) -> Path:
        self.assertEqual(0, self.do_stash()[0])
        return self.entry_dirs()[0]

    def _make_junction(self, link: Path, target: Path) -> None:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            self.skipTest(f"could not create junction: {result.stderr.strip()}")
        self.addCleanup(self._remove_junction, link)

    @staticmethod
    def _remove_junction(link: Path) -> None:
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)

    def test_stash_root_junction_outside_rejected(self) -> None:
        if os.name != "nt":
            self.skipTest("junction test requires Windows")
        outside = self.ws.parent / "stash-escape-outside"
        outside.mkdir(exist_ok=True)
        self._make_junction(self.ws / ".stash", outside)
        before = (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").read_bytes()
        code, out, err = self.do_stash()
        self.assertEqual(1, code)
        self.assertIn("symlink/junction", err)
        self.assertEqual(
            before,
            (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").read_bytes(),
        )
        # Nothing was written through the junction to the outside directory.
        self.assertEqual([], list(outside.rglob("workflows")))
        self.assertEqual([], list(outside.rglob("*.json")))

    def test_stash_root_junction_inside_root_rejected(self) -> None:
        if os.name != "nt":
            self.skipTest("junction test requires Windows")
        inside = self.ws / "elsewhere"
        inside.mkdir(exist_ok=True)
        self._make_junction(self.ws / ".stash", inside)
        code, out, err = self.do_stash()
        self.assertEqual(1, code)
        self.assertIn("symlink/junction", err)
        self.assertTrue(
            (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists()
        )

    def test_entry_dir_junction_rejected_on_pop_and_drop(self) -> None:
        if os.name != "nt":
            self.skipTest("junction test requires Windows")
        entry = self.do_committed_stash()
        outside = self.ws.parent / "entry-escape-outside"
        outside.mkdir(exist_ok=True)
        backup = entry.with_name(entry.name + ".bak")
        os.rename(entry, backup)
        self.addCleanup(shutil.rmtree, backup, ignore_errors=True)
        self._make_junction(entry, outside)
        code, out, err = self.run_cli(self.pop_argv([entry.name]))
        self.assertEqual(1, code)
        self.assertIn("symlink/junction", err)
        code, out, err = self.run_cli(["drop", "--workspace", str(self.ws), entry.name])
        self.assertEqual(1, code)
        # The outside directory received nothing.
        self.assertEqual([], list(outside.rglob("*")))

    def test_entry_dir_junction_inside_root_rejected_on_pop(self) -> None:
        if os.name != "nt":
            self.skipTest("junction test requires Windows")
        entry = self.do_committed_stash()
        backup = entry.with_name(entry.name + ".bak")
        os.rename(entry, backup)
        self.addCleanup(shutil.rmtree, backup, ignore_errors=True)
        self._make_junction(entry, self.ws / "workflows" / "primary")
        code, out, err = self.run_cli(self.pop_argv([entry.name]))
        self.assertEqual(1, code)
        self.assertIn("symlink/junction", err)

    def test_payload_ancestor_junction_rejected_on_pop(self) -> None:
        if os.name != "nt":
            self.skipTest("junction test requires Windows")
        entry = self.do_committed_stash()
        outside = self.ws.parent / "payload-escape-outside"
        outside.mkdir(exist_ok=True)
        wf_dir = entry / "workflows"
        wf_dir.rename(entry / "workflows.bak")
        self.addCleanup(shutil.rmtree, entry / "workflows.bak", ignore_errors=True)
        self._make_junction(wf_dir, outside)
        code, out, err = self.run_cli(self.pop_argv([entry.name]))
        self.assertEqual(1, code)
        # Either the reparse guard or the containment check rejects the link;
        # both refuse before any mutation.
        self.assertRegex(err, "symlink/junction|escapes the workspace")
        self.assertEqual([], list(outside.rglob("*")))
        # Destination untouched.
        self.assertFalse(
            (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists()
        )

    def test_stash_root_symlink_rejected(self) -> None:
        if os.name != "posix":
            self.skipTest("symlink test requires POSIX")
        outside = self.ws.parent / "stash-escape-outside"
        outside.mkdir(exist_ok=True)
        os.symlink(outside, self.ws / ".stash")
        code, out, err = self.do_stash()
        self.assertEqual(1, code)
        self.assertIn("symlink", err)
        self.assertTrue(
            (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists()
        )
        self.assertEqual([], list(outside.rglob("*")))

    def test_entry_dir_symlink_rejected_on_pop(self) -> None:
        if os.name != "posix":
            self.skipTest("symlink test requires POSIX")
        entry = self.do_committed_stash()
        outside = self.ws.parent / "entry-escape-outside"
        outside.mkdir(exist_ok=True)
        backup = entry.with_name(entry.name + ".bak")
        os.rename(entry, backup)
        self.addCleanup(shutil.rmtree, backup, ignore_errors=True)
        os.symlink(outside, entry)
        code, out, err = self.run_cli(self.pop_argv([entry.name]))
        self.assertEqual(1, code)
        self.assertIn("symlink", err)
        self.assertEqual([], list(outside.rglob("*")))

    def test_payload_ancestor_symlink_rejected_on_pop(self) -> None:
        if os.name != "posix":
            self.skipTest("symlink test requires POSIX")
        entry = self.do_committed_stash()
        outside = self.ws.parent / "payload-escape-outside"
        outside.mkdir(exist_ok=True)
        wf_dir = entry / "workflows"
        wf_dir.rename(entry / "workflows.bak")
        self.addCleanup(shutil.rmtree, entry / "workflows.bak", ignore_errors=True)
        os.symlink(outside, wf_dir)
        code, out, err = self.run_cli(self.pop_argv([entry.name]))
        self.assertEqual(1, code)
        self.assertRegex(err, "symlink|escapes the workspace")
        self.assertEqual([], list(outside.rglob("*")))

    def test_malformed_manifest_detected_before_mutation(self) -> None:
        entry = self.do_committed_stash()
        (entry / "manifest.json").write_text("{not json", encoding="utf-8")
        code, out, err = self.run_cli(["list", "--workspace", str(self.ws)])
        self.assertEqual(0, code)
        self.assertIn("broken-manifest", out)
        code, out, err = self.run_cli(self.pop_argv([entry.name]))
        self.assertEqual(1, code)
        self.assertIn("Invalid or unreadable manifest", err)
        # Local files were not touched (alpha still empty of workflow.json).
        self.assertFalse(
            (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists()
        )

    def test_manifest_traversal_rejected(self) -> None:
        entry = self.do_committed_stash()
        manifest_path = entry / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["items"][0]["workflow_dir"] = "workflows/../../evil"
        manifest["items"][0]["files"][0]["relpath"] = "workflows/../../evil/workflow.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("Invalid", err)

    def test_manifest_absolute_path_rejected(self) -> None:
        entry = self.do_committed_stash()
        manifest_path = entry / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["items"][0]["files"][0]["relpath"] = "C:/evil/workflow.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("Invalid", err)

    def test_manifest_case_collision_rejected(self) -> None:
        entry = self.do_committed_stash()
        manifest_path = entry / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        item = json.loads(json.dumps(manifest["items"][0]))  # deep copy
        item["workflow_dir"] = "workflows/primary/Alpha"
        for f in item["files"]:
            f["relpath"] = f"workflows/primary/Alpha/{f['name']}"
        manifest["items"].append(item)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("Case-colliding", err)

    def test_nonregular_payload_directory_blocks_pop(self) -> None:
        """A stash payload replaced by a DIRECTORY holding unique bytes must
        fail the whole preflight — it is not 'missing' and must never be
        recursively deleted by a successful restore."""
        entry = self.do_committed_stash()
        stashed = entry / "workflows" / "primary" / "alpha" / "workflow.json"
        stashed.unlink()
        stashed.mkdir(parents=True)
        unique = stashed / "unique.txt"
        unique.write_bytes(b"unique payload bytes")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("not a regular file", err)
        # Nothing mutated: unique bytes intact, destination still absent.
        self.assertEqual(b"unique payload bytes", unique.read_bytes())
        self.assertFalse(
            (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists()
        )

    def test_nonregular_payload_with_identical_destination_blocks_pop(self) -> None:
        """Even when the destination already matches the manifest, a
        directory-shaped payload must refuse the restore (the old bug: pop
        returned 0 and rmtree deleted unique.txt)."""
        entry = self.do_committed_stash()
        alpha = self.ws / "workflows" / "primary" / "alpha"
        alpha.mkdir(parents=True, exist_ok=True)
        (alpha / "workflow.json").write_bytes(WF_ALPHA.encode())
        stashed = entry / "workflows" / "primary" / "alpha" / "workflow.json"
        stashed.unlink()
        stashed.mkdir(parents=True)
        unique = stashed / "unique.txt"
        unique.write_bytes(b"unique payload bytes")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("not a regular file", err)
        self.assertEqual(b"unique payload bytes", unique.read_bytes())
        self.assertEqual(WF_ALPHA.encode(), (alpha / "workflow.json").read_bytes())
        self.assertEqual(1, len(self.entry_dirs()))

    def test_drop_refuses_nonregular_payload(self) -> None:
        entry = self.do_committed_stash()
        stashed = entry / "workflows" / "primary" / "alpha" / "workflow.json"
        stashed.unlink()
        stashed.mkdir(parents=True)
        (stashed / "unique.txt").write_bytes(b"unique")
        code, out, err = self.run_cli(["drop", "--workspace", str(self.ws), entry.name])
        self.assertEqual(1, code)
        self.assertIn("not a regular file", err)
        self.assertTrue((stashed / "unique.txt").exists())

    def test_unknown_extra_file_blocks_pop_before_mutation(self) -> None:
        """An unmanifested file inside the entry must refuse the whole pop;
        successful cleanup never discards unverified bytes."""
        entry = self.do_committed_stash()
        extra = entry / "unmanifested.txt"
        extra.write_bytes(b"unverified extra bytes")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("unknown path", err)
        self.assertEqual(b"unverified extra bytes", extra.read_bytes())
        self.assertFalse(
            (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists()
        )

    def test_unknown_file_appearing_at_cleanup_survives(self) -> None:
        """A file raced into the entry between the preflight and the cleanup
        must survive: cleanup removes only verified known names."""
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        raced = entry / "raced-in.txt"
        raced_bytes = b"raced in during restore"
        original_move = self.mod._move_no_overwrite
        calls = {"n": 0}

        def planting_move(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raced.write_bytes(raced_bytes)  # appear after the preflight
            return original_move(src, dst)

        self.mod._move_no_overwrite = planting_move
        try:
            code, out, err = self.run_cli(self.pop_argv([]))
        finally:
            self.mod._move_no_overwrite = original_move
        # Restore succeeded but cleanup could not finish: nonzero, never a
        # false "entry removed".
        self.assertEqual(1, code, err)
        self.assertNotIn("entry removed", err)
        # The raced-in bytes survive and the entry stays recoverable.
        self.assertEqual(raced_bytes, raced.read_bytes())
        self.assertTrue(entry.exists())
        self.assertTrue((entry / "manifest.json").exists())
        self.assertIn("KEPT", err)

    def test_manifest_temp_file_with_unique_bytes_blocks_pop(self) -> None:
        """Exact Sol repro: an unregistered manifest.json.tmp-* file holding
        UNIQUE bytes must be treated as unknown — the temp prefix never
        proves ownership, so pop fails closed and the bytes survive."""
        entry = self.do_committed_stash()
        tmp = entry / "manifest.json.tmp-user"
        tmp_bytes = b'{"unique": "temp bytes"}'
        tmp.write_bytes(tmp_bytes)
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("unknown path", err)
        # The unique bytes survive and nothing was restored.
        self.assertEqual(tmp_bytes, tmp.read_bytes())
        self.assertFalse(
            (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists()
        )
        # The entry is still intact and listable.
        self.assertEqual(1, len(self.entry_dirs()))

    def test_unknown_file_at_manifest_unlink_keeps_manifest_and_fails(self) -> None:
        """Exact Sol repro: an unknown file raced in exactly at the manifest
        unlink must NOT yield a manifest-less entry with a false success.
        Cleanup detects the incomplete removal, re-persists the in-memory
        manifest, and returns nonzero."""
        entry = self.do_committed_stash()
        raced = entry / "sol-race.txt"
        raced_bytes = b"raced in at manifest unlink"
        original_unlink = self.mod.os.unlink
        manifest_path = entry / "manifest.json"

        def patched_unlink(path, *a, **kw):
            if str(path) == str(manifest_path):
                raced.write_bytes(raced_bytes)  # appear right at the unlink
            return original_unlink(path, *a, **kw)

        self.mod.os.unlink = patched_unlink
        try:
            code, out, err = self.run_cli(self.pop_argv([]))
        finally:
            self.mod.os.unlink = original_unlink
        # Nonzero and never a false "entry removed".
        self.assertEqual(1, code, err)
        self.assertNotIn("entry removed", err)
        self.assertIn("could NOT be fully removed", err)
        # The entry is recoverable: manifest re-persisted, unknown bytes kept.
        self.assertTrue(entry.exists())
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("incomplete-restore", manifest["transaction"]["state"])
        self.assertEqual(raced_bytes, raced.read_bytes())
        # The restore itself did succeed.
        self.assertEqual(
            WF_ALPHA.encode(),
            (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").read_bytes(),
        )

    def test_recovery_guidance_commands_parse_and_execute(self) -> None:
        """Every recovery command printed by an interrupted stash must parse
        against the real CLI grammar (no 'n8n stash pop' — the wrapper only
        dispatches 'n8n pop'), and executing it must reach the command
        handler rather than die in argparse."""
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        manifest_path = entry / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["transaction"]["state"] = "committing"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        code, out, err = self.run_cli(self.pop_argv([entry.name]))
        self.assertEqual(1, code)
        self.assertIn("interrupted stash state", err)
        self.assertNotIn("n8n stash pop", err)
        self.assertNotIn("n8n stash stash", err)

        commands = re.findall(r"'(n8n [^']+)'", err)
        self.assertTrue(commands)
        for command in commands:
            self.assertNotIn("stash pop", command)
            self.assertNotIn("stash stash", command)
            script_argv = command.split()[1:]  # drop the leading "n8n"
            out2, err2 = io.StringIO(), io.StringIO()
            with redirect_stdout(out2), redirect_stderr(err2):
                try:
                    self.mod.main(script_argv + ["--workspace", str(self.ws)])
                except SystemExit as exc:  # argparse rejection = grammar bug
                    self.fail(f"guidance command {command!r} does not parse: {exc}")

    def test_help_usage_avoids_doubled_verbs(self) -> None:
        """The usage line must never read 'n8n stash stash' or
        'n8n stash pop'."""
        parser = self.mod.build_parser()
        self.assertEqual("n8n", parser.prog)
        help_text = parser.format_help()
        self.assertNotIn("n8n stash stash", help_text)
        self.assertNotIn("n8n stash pop", help_text)
        self.assertIn("{stash,list,show,pop,drop}", help_text)

    def test_drop_discards_unmanifested_extras_authorized(self) -> None:
        """Drop is an explicit user-authorized discard of the whole entry, so
        unmanifested extras go too — but never through links."""
        entry = self.do_committed_stash()
        extra = entry / "unmanifested.txt"
        extra.write_bytes(b"extra")
        code, out, err = self.run_cli(["drop", "--workspace", str(self.ws), entry.name])
        self.assertEqual(0, code, err)
        self.assertEqual([], self.entry_dirs())

    def test_modified_stash_payload_rejected_before_mutation(self) -> None:
        entry = self.do_committed_stash()
        target = entry / "workflows" / "primary" / "alpha" / "workflow.json"
        # Same-length tamper so the size check passes and sha256 catches it.
        tampered = WF_ALPHA.replace('"name": "alpha"', '"name": "alphb"')
        self.assertEqual(len(WF_ALPHA), len(tampered))
        target.write_bytes(tampered.encode("utf-8"))
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("modified since stash", err)
        self.assertFalse(
            (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists()
        )

    def test_missing_stash_payload_rejected(self) -> None:
        entry = self.do_committed_stash()
        (entry / "workflows" / "primary" / "alpha" / "workflow.json").unlink()
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("missing", err)

    def test_symlinked_workflow_dir_is_not_stashed(self) -> None:
        if os.name != "posix":
            self.skipTest("symlink test requires POSIX")
        alpha = self.ws / "workflows" / "primary" / "alpha"
        link = self.ws / "workflows" / "primary" / "alphalink"
        os.symlink(alpha, link)
        code, out, err = self.run_cli(self.stash_argv(["--workflow-id", "111"]))
        # The symlinked dir is skipped; the real dir still matches by id.
        self.assertEqual(0, code, err)
        self.assertTrue(link.is_symlink())

    def test_symlink_destination_ancestor_rejected_on_pop(self) -> None:
        if os.name != "posix":
            self.skipTest("symlink test requires POSIX")
        entry = self.do_committed_stash()
        outside = self.ws / "outside"
        outside.mkdir()
        alpha = self.ws / "workflows" / "primary" / "alpha"
        # alpha still exists after stash (unrelated notes.txt stayed behind).
        alpha.mkdir(exist_ok=True)
        os.remove(alpha / "notes.txt")
        os.symlink(outside, alpha / "workflow.json")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("symlink", err)
        self.assertTrue(entry.exists())

    def test_junction_destination_ancestor_rejected_on_pop(self) -> None:
        if os.name != "nt":
            self.skipTest("junction test requires Windows")
        entry = self.do_committed_stash()
        outside = self.ws / "outside"
        outside.mkdir()
        alpha = self.ws / "workflows" / "primary" / "alpha"
        # alpha still exists after stash (unrelated notes.txt stayed behind).
        alpha.mkdir(exist_ok=True)
        os.remove(alpha / "notes.txt")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alpha / "workflow.json"), str(outside)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            self.skipTest(f"could not create junction: {result.stderr}")
        code, out, err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("symlink/junction", err)
        self.assertTrue(entry.exists())
        # Cleanup the junction so the temp dir can be removed.
        subprocess.run(["cmd", "/c", "rmdir", str(alpha / "workflow.json")],
                       capture_output=True)


class InterruptionTests(StashTestBase):
    def _patch_link(self, behavior):
        """Patch mod.os.link with behavior(publish_n, original_link, src, dst);
        publish_n counts only publish-phase links (dst inside .stash).
        Returns a restore function."""
        original_link = self.mod.os.link
        state = {"publish": 0}

        def patched(src, dst, *a, **kw):
            publish = ".stash" in str(dst)
            if publish:
                state["publish"] += 1
            return behavior(state["publish"], original_link, src, dst, *a, **kw)

        self.mod.os.link = patched

        def restore():
            self.mod.os.link = original_link

        return restore

    def _patch_link_and_copy(self, link_behavior, copy_behavior):
        """Patch os.link and _exclusive_copy together (the fallback must fail
        too for an injected mid-stash failure). Returns restore func."""
        original_link = self.mod.os.link
        original_copy = self.mod._exclusive_copy
        self.mod.os.link = link_behavior
        self.mod._exclusive_copy = copy_behavior

        def restore():
            self.mod.os.link = original_link
            self.mod._exclusive_copy = original_copy

        return restore

    def test_injected_move_failure_rolls_back_completely(self) -> None:
        real_link = self.mod.os.link
        real_copy = self.mod._exclusive_copy
        publish_state = {"n": 0}

        def link_behavior(src, dst, *a, **kw):
            if ".stash" in str(dst):
                publish_state["n"] += 1
                if publish_state["n"] == 2:
                    raise OSError("injected link failure")
            return real_link(src, dst, *a, **kw)

        def copy_behavior(src, dst, expected_sha):
            # Only reached when the link primitive failed for this publish.
            if ".stash" in str(dst) and publish_state["n"] == 2:
                raise OSError("injected copy failure")
            return real_copy(src, dst, expected_sha)

        restore = self._patch_link_and_copy(link_behavior, copy_behavior)
        try:
            code, out, err = self.do_stash()
        finally:
            restore()
        self.assertEqual(1, code)
        self.assertRegex(err, "injected (link|copy) failure")
        # Everything rolled back: files back in place, no entry left behind.
        alpha = self.ws / "workflows" / "primary" / "alpha"
        self.assertEqual(WF_ALPHA.encode(), (alpha / "workflow.json").read_bytes())
        self.assertEqual(META_ALPHA.encode(), (alpha / "metadata.json").read_bytes())
        self.assertEqual([], self.entry_dirs())

    def test_real_move_then_keyboard_interrupt_rolls_back(self) -> None:
        """A move that COMPLETES and then raises must not lose the only copy:
        rollback reconciles actual filesystem state, not manifest state."""
        alpha = self.ws / "workflows" / "primary" / "alpha"

        def behavior(publish_n, original_link, src, dst, *a, **kw):
            if ".stash" in str(dst) and publish_n == 2:
                # second file: complete the real publish, then die
                original_link(src, dst, *a, **kw)
                os.unlink(src)  # quarantine unlink (operation-owned) happened
                raise KeyboardInterrupt
            return original_link(src, dst, *a, **kw)

        restore = self._patch_link(behavior)
        try:
            code, out, err = self.do_stash()
        finally:
            restore()
        self.assertEqual(130, code)
        self.assertEqual(WF_ALPHA.encode(), (alpha / "workflow.json").read_bytes())
        self.assertEqual(META_ALPHA.encode(), (alpha / "metadata.json").read_bytes())
        self.assertEqual([], self.entry_dirs())

    def test_keyboard_interrupt_at_manifest_persist_boundary_rolls_back(self) -> None:
        """Interruption exactly at a manifest persistence boundary: the on-disk
        manifest is authoritative only as a hint; bytes must still reconcile."""
        original_persist = self.mod._persist_manifest
        calls = {"n": 0}

        def failing_persist(path, payload):
            calls["n"] += 1
            original_persist(path, payload)
            # Persist sequence: 1 initial, 2 moving f1, 3 stashed f1,
            # 4 moving f2. Die right after the 4th real write.
            if calls["n"] == 4:
                raise KeyboardInterrupt
            return None

        self.mod._persist_manifest = failing_persist
        try:
            code, out, err = self.do_stash()
        finally:
            self.mod._persist_manifest = original_persist
        self.assertEqual(130, code)
        alpha = self.ws / "workflows" / "primary" / "alpha"
        self.assertEqual(WF_ALPHA.encode(), (alpha / "workflow.json").read_bytes())
        self.assertEqual(META_ALPHA.encode(), (alpha / "metadata.json").read_bytes())
        self.assertEqual([], self.entry_dirs())

    def test_injected_failure_with_racing_file_keeps_entry_and_guidance(self) -> None:
        alpha = self.ws / "workflows" / "primary" / "alpha"
        real_link = self.mod.os.link
        real_copy = self.mod._exclusive_copy
        publish_state = {"n": 0}

        def link_behavior(src, dst, *a, **kw):
            if ".stash" in str(dst):
                publish_state["n"] += 1
                if publish_state["n"] == 1:
                    result = real_link(src, dst, *a, **kw)
                    # Racing writer recreates the first file at its original
                    # path with DIFFERENT content (a sha-matching duplicate
                    # would be safely reconciled with a full rollback instead).
                    (alpha / "workflow.json").write_bytes(b'{"id": "111", "raced": true}')
                    return result
                if publish_state["n"] == 2:
                    raise OSError("injected link failure")
            return real_link(src, dst, *a, **kw)

        def copy_behavior(src, dst, expected_sha):
            # Only reached when the link primitive failed for this publish.
            if ".stash" in str(dst) and publish_state["n"] == 2:
                raise OSError("injected copy failure")
            return real_copy(src, dst, expected_sha)

        restore = self._patch_link_and_copy(link_behavior, copy_behavior)
        try:
            code, out, err = self.do_stash()
        finally:
            restore()
        self.assertEqual(1, code)
        self.assertIn("incomplete-rollback", err)
        self.assertIn("Recovery guidance", err)
        entries = self.entry_dirs()
        self.assertEqual(1, len(entries))
        stashed = entries[0] / "workflows" / "primary" / "alpha" / "workflow.json"
        self.assertTrue(stashed.exists())
        self.assertEqual(b'{"id": "111", "raced": true}',
                         (alpha / "workflow.json").read_bytes())
        # Metadata is still at its original path (never moved).
        self.assertEqual(META_ALPHA.encode(), (alpha / "metadata.json").read_bytes())

    def test_replacement_source_bytes_survive(self) -> None:
        """Regression for the source-side unlink race: the current source
        pathname is NEVER unlinked, so a racing replacement written at the
        source path during the move survives byte-for-byte."""
        alpha = self.ws / "workflows" / "primary" / "alpha"
        original_rename = self.mod.os.rename
        new_current = b'{"id": "111", "new-current": true}'

        def racing_rename(src, dst, *a, **kw):
            result = original_rename(src, dst, *a, **kw)
            # Racing writer replaces the (now vacant) source path right after
            # the capture; stash must never delete or displace these bytes.
            if "workflows" in str(src) and ".stash" not in str(src):
                alpha.mkdir(parents=True, exist_ok=True)
                (alpha / "workflow.json").write_bytes(new_current)
            return result

        self.mod.os.rename = racing_rename
        try:
            code, out, err = self.do_stash()
        finally:
            self.mod.os.rename = original_rename
        self.assertEqual(0, code, err)
        # The racer's current bytes survived untouched at the source path.
        self.assertEqual(new_current, (alpha / "workflow.json").read_bytes())
        # Our verified payload is in the stash, byte-exact.
        entry = self.entry_dirs()[0]
        self.assertEqual(
            WF_ALPHA.encode(),
            (entry / "workflows" / "primary" / "alpha" / "workflow.json").read_bytes(),
        )

    def test_source_path_is_never_unlinked(self) -> None:
        """Direct regression for the Sol scenario: patch unlink to atomically
        replace the source with NEW CURRENT bytes right before unlinking. The
        stash must never call unlink on the current source pathname."""
        alpha = self.ws / "workflows" / "primary" / "alpha"
        original_unlink = self.mod.os.unlink
        source_str = str(alpha / "workflow.json")
        unlinks_of_source = {"n": 0}

        def patched_unlink(path, *a, **kw):
            if str(path) == source_str:
                # Simulate the race: replacement appears right before unlink.
                (alpha / "workflow.json").write_bytes(b'{"new": "current"}')
                unlinks_of_source["n"] += 1
            return original_unlink(path, *a, **kw)

        self.mod.os.unlink = patched_unlink
        try:
            code, out, err = self.do_stash()
        finally:
            self.mod.os.unlink = original_unlink
        self.assertEqual(0, code, err)
        self.assertEqual(0, unlinks_of_source["n"])

    def test_capture_mismatch_restores_racer_bytes_and_fails(self) -> None:
        """If the atomic capture grabs a racing writer's file instead of our
        payload, a copy is published back through the exclusive non-destructive
        helper, the quarantine capture is ALWAYS retained, and the stash
        fails closed."""
        src = self.ws / "workflows" / "primary" / "alpha" / "workflow.json"
        racer_bytes = b'{"racing": "writer"}'
        src.write_bytes(racer_bytes)  # a racer already replaced the source
        quarantine = self.ws / ".stash" / "q" / "captured-test-workflow.json"
        quarantine.parent.mkdir(parents=True)
        with self.assertRaises(self.mod.StashError) as ctx:
            self.mod._capture_source(src, quarantine, sha256_bytes(WF_ALPHA.encode()))
        self.assertIn("racing writer", str(ctx.exception))
        self.assertIn(str(quarantine), str(ctx.exception))
        # Both survive: the copy at the vacant source path AND the retained
        # quarantine capture.
        self.assertEqual(racer_bytes, src.read_bytes())
        self.assertEqual(racer_bytes, quarantine.read_bytes())

    def test_capture_mismatch_with_reoccupied_source_keeps_capture(self) -> None:
        """If the source path is re-occupied before the captured racer bytes
        can be restored, the captured bytes are retained at a discoverable
        quarantine path and nothing is deleted."""
        src = self.ws / "workflows" / "primary" / "alpha" / "workflow.json"
        racer_bytes = b'{"racing": "writer"}'
        occupier_bytes = b'{"occupier": true}'
        src.write_bytes(racer_bytes)
        quarantine = self.ws / ".stash" / "q" / "captured-test-workflow.json"
        quarantine.parent.mkdir(parents=True)
        original_rename = self.mod.os.rename

        def racing_rename(s, d, *a, **kw):
            result = original_rename(s, d, *a, **kw)
            src.write_bytes(occupier_bytes)  # another writer re-occupies src
            return result

        self.mod.os.rename = racing_rename
        try:
            with self.assertRaises(self.mod.StashError) as ctx:
                self.mod._capture_source(src, quarantine, sha256_bytes(WF_ALPHA.encode()))
        finally:
            self.mod.os.rename = original_rename
        self.assertIn("occupied", str(ctx.exception))
        self.assertIn("retained", str(ctx.exception))
        # Nothing was deleted: occupier at src, racer bytes in quarantine.
        self.assertEqual(occupier_bytes, src.read_bytes())
        self.assertEqual(racer_bytes, quarantine.read_bytes())

    def test_recovery_copy_fallback_replacement_keeps_quarantine_and_original(self) -> None:
        """Exact Sol race on a link-less filesystem: the recovery publishes a
        verified copy at the vacant source path, and a NEW CURRENT writer
        replaces it immediately afterwards. The capture must NOT be deleted
        anywhere: quarantine captured bytes and the newest original bytes both
        survive, with an accurate error naming the retained quarantine path."""
        src = self.ws / "workflows" / "primary" / "alpha" / "workflow.json"
        racer_bytes = b'{"racing": "writer-1"}'
        newest_bytes = b'{"newest": "current"}'
        src.write_bytes(racer_bytes)
        quarantine = self.ws / ".stash" / "q" / "captured-test-workflow.json"
        quarantine.parent.mkdir(parents=True)
        original_link = self.mod.os.link
        original_copy = self.mod._exclusive_copy

        def no_links(s, d, *a, **kw):
            raise OSError("hard links unsupported (simulated)")

        def racing_copy(s, d, expected_sha):
            result = original_copy(s, d, expected_sha)
            # The newest writer replaces the just-published copy immediately
            # after it verified (previously this window preceded the unlink
            # that destroyed the captured bytes).
            src.write_bytes(newest_bytes)
            return result

        self.mod.os.link = no_links
        self.mod._exclusive_copy = racing_copy
        try:
            with self.assertRaises(self.mod.StashError) as ctx:
                self.mod._capture_source(
                    src, quarantine, sha256_bytes(WF_ALPHA.encode())
                )
        finally:
            self.mod.os.link = original_link
            self.mod._exclusive_copy = original_copy
        message = str(ctx.exception)
        self.assertIn("racing writer", message)
        self.assertIn(str(quarantine), message)
        # Both versions survive: the retained quarantine capture AND the
        # newest bytes at the original path.
        self.assertEqual(racer_bytes, quarantine.read_bytes())
        self.assertEqual(newest_bytes, src.read_bytes())

    def test_recovery_of_mismatched_capture_never_renames_onto_source(self) -> None:
        """Exact Sol race: a NEW re-occupier appears just before the recovery
        transfer of the mismatched capture. The captured bytes may only be
        restored through the exclusive no-replace helper — never a
        replace-capable rename onto the mutable original path — so the
        re-occupier's bytes AND the quarantined prior bytes both survive."""
        src = self.ws / "workflows" / "primary" / "alpha" / "workflow.json"
        racer_bytes = b'{"racing": "writer-1"}'
        reoccupier_bytes = b'{"re-occupier": "newer"}'
        src.write_bytes(racer_bytes)
        quarantine = self.ws / ".stash" / "q" / "captured-test-workflow.json"
        quarantine.parent.mkdir(parents=True)
        original_rename = self.mod.os.rename
        original_link = self.mod.os.link
        rename_calls = {"to_src": 0}

        def recording_rename(s, d, *a, **kw):
            if str(d) == str(src):
                rename_calls["to_src"] += 1
            return original_rename(s, d, *a, **kw)

        def racing_link(s, d, *a, **kw):
            if str(d) == str(src):
                # The re-occupier wins the race, arriving just before the
                # recovery transfer.
                src.write_bytes(reoccupier_bytes)
            return original_link(s, d, *a, **kw)

        self.mod.os.rename = recording_rename
        self.mod.os.link = racing_link
        try:
            with self.assertRaises(self.mod.StashError) as ctx:
                self.mod._capture_source(src, quarantine, sha256_bytes(WF_ALPHA.encode()))
        finally:
            self.mod.os.rename = original_rename
            self.mod.os.link = original_link
        self.assertIn("occupied", str(ctx.exception))
        # Both versions survive; no replace-capable rename ever targeted the
        # original path.
        self.assertEqual(0, rename_calls["to_src"])
        self.assertEqual(reoccupier_bytes, src.read_bytes())
        self.assertEqual(racer_bytes, quarantine.read_bytes())

    def test_keyboard_interrupt_after_capture_before_publish(self) -> None:
        """Crash window: capture done, publish not started. Rollback must
        restore the captured bytes to the vacant original path."""
        alpha = self.ws / "workflows" / "primary" / "alpha"

        def behavior(publish_n, original_link, src, dst, *a, **kw):
            if ".stash" in str(dst) and publish_n == 1:
                raise KeyboardInterrupt
            return original_link(src, dst, *a, **kw)

        restore = self._patch_link(behavior)
        try:
            code, out, err = self.do_stash()
        finally:
            restore()
        self.assertEqual(130, code)
        self.assertEqual(WF_ALPHA.encode(), (alpha / "workflow.json").read_bytes())
        self.assertEqual(META_ALPHA.encode(), (alpha / "metadata.json").read_bytes())
        self.assertEqual([], self.entry_dirs())

    def test_keyboard_interrupt_after_publish_before_capture_unlink(self) -> None:
        """Crash window: payload published, quarantine duplicate not yet
        removed. Rollback must restore the original and drop only the
        verified duplicate."""
        alpha = self.ws / "workflows" / "primary" / "alpha"
        original_unlink = self.mod.os.unlink
        fired = {"x": False}

        def patched_unlink(path, *a, **kw):
            # Crash once, at the stash command's quarantine unlink; the
            # rollback's own duplicate unlink must still be able to run.
            if ".captured" in str(path) and not fired["x"]:
                fired["x"] = True
                raise KeyboardInterrupt
            return original_unlink(path, *a, **kw)

        self.mod.os.unlink = patched_unlink
        try:
            code, out, err = self.do_stash()
        finally:
            self.mod.os.unlink = original_unlink
        self.assertEqual(130, code)
        self.assertEqual(WF_ALPHA.encode(), (alpha / "workflow.json").read_bytes())
        self.assertEqual(META_ALPHA.encode(), (alpha / "metadata.json").read_bytes())
        self.assertEqual([], self.entry_dirs())

    def test_keyboard_interrupt_after_capture_unlink_before_progress_persist(self) -> None:
        """Crash window: capture fully done and quarantine removed, but the
        'stashed' progress was never persisted (manifest still says
        'capturing'). Rollback must reconcile by filesystem state."""
        original_persist = self.mod._persist_manifest
        calls = {"n": 0}

        def failing_persist(path, payload):
            calls["n"] += 1
            original_persist(path, payload)
            # Persist sequence: 1 initial, 2 capturing f1, 3 stashed f1.
            if calls["n"] == 3:
                raise KeyboardInterrupt
            return None

        self.mod._persist_manifest = failing_persist
        try:
            code, out, err = self.do_stash()
        finally:
            self.mod._persist_manifest = original_persist
        self.assertEqual(130, code)
        alpha = self.ws / "workflows" / "primary" / "alpha"
        self.assertEqual(WF_ALPHA.encode(), (alpha / "workflow.json").read_bytes())
        self.assertEqual(META_ALPHA.encode(), (alpha / "metadata.json").read_bytes())
        self.assertEqual([], self.entry_dirs())

    def test_interrupted_stash_visible_and_pop_refused(self) -> None:
        self.assertEqual(0, self.do_stash()[0])
        entry = self.entry_dirs()[0]
        manifest_path = entry / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["transaction"]["state"] = "committing"
        for item in manifest["items"]:
            for f in item["files"]:
                f["state"] = "pending"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        # Stash list shows it as incomplete.
        code, out, err = self.run_cli(["list", "--workspace", str(self.ws)])
        self.assertIn("INCOMPLETE", out)
        # Pop refuses and retains all bytes: bare pop (no complete entry) and
        # explicit selection of the interrupted entry both refuse.
        code, out, bare_err = self.run_cli(self.pop_argv([]))
        self.assertEqual(1, code)
        self.assertIn("No complete stash entries", bare_err)
        code, out, err = self.run_cli(self.pop_argv([entry.name]))
        self.assertEqual(1, code)
        self.assertIn("interrupted stash state", err)
        self.assertIn("recover", err)
        self.assertIn("workflows\\primary\\alpha\\workflow.json" if os.name == "nt"
                      else "workflows/primary/alpha/workflow.json", err)
        stashed = entry / "workflows" / "primary" / "alpha" / "workflow.json"
        self.assertTrue(stashed.exists())
        self.assertFalse(
            (self.ws / "workflows" / "primary" / "alpha" / "workflow.json").exists()
        )


class LockTests(StashTestBase):
    def test_lock_contention_fails_fast_with_message(self) -> None:
        self.mod.LOCK_TIMEOUT_SECONDS = 0.5
        lock_path = self.ws / ".stash" / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text('{"pid": 1, "utc": "now"}', encoding="utf-8")
        code, out, err = self.do_stash()
        self.assertEqual(1, code)
        self.assertIn("Another stash/pop operation", err)
        # The contending run must not have removed the other holder's lock.
        self.assertTrue(lock_path.exists())

    def test_old_active_lock_is_not_stolen(self) -> None:
        """An old mtime does NOT authorize breaking the lock: fail closed."""
        self.mod.LOCK_TIMEOUT_SECONDS = 0.5
        lock_path = self.ws / ".stash" / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder_bytes = b'{"pid": 424242, "utc": "2020-01-01T00:00:00Z"}'
        lock_path.write_bytes(holder_bytes)
        twenty_minutes_ago = time.time() - 1200
        os.utime(lock_path, (twenty_minutes_ago, twenty_minutes_ago))
        code, out, err = self.do_stash()
        self.assertEqual(1, code)
        self.assertIn("does not steal", err)
        # The lock file and its owner info survived untouched.
        self.assertEqual(holder_bytes, lock_path.read_bytes())

    def test_release_never_removes_a_replacement_lock(self) -> None:
        lock_path = self.ws / ".stash" / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.mod.ExclusiveLock(lock_path, timeout=1.0)
        lock.acquire()
        # Another process replaces the lock file with its own (new inode).
        replacement = lock_path.with_name(".lock.new")
        replacement.write_bytes(b'{"pid": 999, "utc": "later"}')
        os.replace(replacement, lock_path)
        lock.release()
        # Our release must not delete the replacement.
        self.assertTrue(lock_path.exists())
        self.assertEqual(b'{"pid": 999, "utc": "later"}', lock_path.read_bytes())
        # Cleanup: the replacement holder removes it itself.
        lock_path.unlink()

    def test_list_and_show_do_not_take_lock_or_create_stash(self) -> None:
        code, out, err = self.run_cli(["list", "--workspace", str(self.ws)])
        self.assertEqual(0, code)
        self.assertFalse((self.ws / ".stash").exists())


REPO_GUARDED_PATHS = (
    REPO_ROOT / ".gitignore",
    REPO_ROOT / ".n8n_sync" / "state.json",
)


def snapshot_repo_guarded_files() -> dict:
    return {
        str(p): (p.read_bytes() if p.exists() else None)
        for p in REPO_GUARDED_PATHS
    }


def assert_repo_untouched(snapshot: dict) -> None:
    for path_str, before in snapshot.items():
        path = Path(path_str)
        after = path.read_bytes() if path.exists() else None
        assert before == after, f"repo file changed during tests: {path}"


class WrapperIsolationMixin:
    """Never let wrapper tests mutate the real utility repo: point the
    utility-gitignore override at a temp file and snapshot the repo files."""

    @classmethod
    def isolate_repo(cls) -> None:
        cls._repo_snapshot = snapshot_repo_guarded_files()
        cls._isolated_gitignore = Path(tempfile.mkdtemp(prefix="stash_util_gi_")) / ".gitignore"
        cls._old_env = os.environ.get("N8N_STASH_UTILITY_GITIGNORE")
        os.environ["N8N_STASH_UTILITY_GITIGNORE"] = str(cls._isolated_gitignore)
        assert not (REPO_ROOT / ".stash").exists(), "real repo unexpectedly has .stash/"

    @classmethod
    def unisolate_repo(cls) -> None:
        if getattr(cls, "_old_env", None) is None:
            os.environ.pop("N8N_STASH_UTILITY_GITIGNORE", None)
        else:
            os.environ["N8N_STASH_UTILITY_GITIGNORE"] = cls._old_env
        assert_repo_untouched(cls._repo_snapshot)
        assert not (REPO_ROOT / ".stash").exists(), "tests created .stash/ in the real repo"


class WrapperParityTests(WrapperIsolationMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.isolate_repo()
        cls.pwsh = shutil.which("pwsh") or shutil.which("powershell")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.unisolate_repo()

    def run_wrapper(self, args: list, cwd: "Path | None" = None) -> "tuple[int, str, str]":
        result = subprocess.run(
            [self.pwsh, "-NoProfile", "-File", str(REPO_ROOT / "n8n.ps1")] + args,
            capture_output=True, text=True, cwd=str(cwd) if cwd else None,
        )
        return result.returncode, result.stdout, result.stderr

    @unittest.skipUnless(os.name == "nt", "PowerShell wrapper test")
    def test_help_lists_stash_and_pop(self) -> None:
        if not self.pwsh:
            self.skipTest("no PowerShell available")
        code, out, err = self.run_wrapper(["help"])
        self.assertEqual(0, code)
        self.assertIn("stash", out)
        self.assertIn("pop", out)

    @unittest.skipUnless(os.name == "nt", "PowerShell wrapper test")
    def test_unknown_command_exits_1(self) -> None:
        if not self.pwsh:
            self.skipTest("no PowerShell available")
        code, out, err = self.run_wrapper(["definitely-not-a-command"])
        self.assertEqual(1, code)

    @unittest.skipUnless(os.name == "nt", "PowerShell wrapper test")
    def test_wrapper_stash_list_and_pop_roundtrip(self) -> None:
        if not self.pwsh:
            self.skipTest("no PowerShell available")
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_workspace(Path(tmp))
            code, out, err = self.run_wrapper(["stash", "--workflow-id", "111"], cwd=ws)
            self.assertEqual(0, code, out + err)
            self.assertFalse((ws / "workflows" / "primary" / "alpha" / "workflow.json").exists())
            code, out, err = self.run_wrapper(["stash", "list"], cwd=ws)
            self.assertEqual(0, code, out + err)
            self.assertIn("complete", out)
            code, out, err = self.run_wrapper(["pop"], cwd=ws)
            self.assertEqual(0, code, out + err)
            self.assertEqual(
                WF_ALPHA.encode(),
                (ws / "workflows" / "primary" / "alpha" / "workflow.json").read_bytes(),
            )
            # The isolated override file, not the real repo gitignore, got the line.
            self.assertTrue(self._isolated_gitignore.read_bytes().endswith(b"/.stash/\n"))

    def test_posix_wrapper_dispatch_defined(self) -> None:
        # Static check: the POSIX wrapper routes stash/pop like n8n.ps1.
        text = (REPO_ROOT / "n8n").read_text(encoding="utf-8")
        self.assertIn("stash|pop)", text)
        self.assertIn("n8n_stash.py", text)


WSL_DISTRO = "Ubuntu"
GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
)


def _wsl_available() -> bool:
    if os.name != "nt" or not shutil.which("wsl.exe"):
        return False
    try:
        result = subprocess.run(
            ["wsl.exe", "-d", WSL_DISTRO, "--", "true"],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _to_wsl_path(win_path: Path) -> str:
    # Forward slashes survive wsl.exe argument mangling; backslashes do not.
    result = subprocess.run(
        ["wsl.exe", "-d", WSL_DISTRO, "--", "wslpath", "-u", win_path.as_posix()],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"wslpath failed: {result.stderr}")
    return result.stdout.strip()


def _find_git_bash() -> "str | None":
    for candidate in GIT_BASH_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("bash")


class POSIXWrapperTests(WrapperIsolationMixin, unittest.TestCase):
    """Real Bash roundtrip tests through WSL Ubuntu (and a Git Bash syntax
    check when available). Temporary workspaces only; the real utility repo's
    .gitignore is redirected away via N8N_STASH_UTILITY_GITIGNORE."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.isolate_repo()
        cls.wsl = _wsl_available()
        if cls.wsl:
            cls.repo_linux = _to_wsl_path(REPO_ROOT)
            cls.gitignore_linux = _to_wsl_path(cls._isolated_gitignore)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.unisolate_repo()

    def run_posix_wrapper(self, args: str, ws: Path) -> "tuple[int, str, str]":
        ws_linux = _to_wsl_path(ws)
        script = (
            f'export N8N_STASH_UTILITY_GITIGNORE="{self.gitignore_linux}" && '
            f'cd "{ws_linux}" && bash "{self.repo_linux}/n8n" {args}'
        )
        result = subprocess.run(
            ["wsl.exe", "-d", WSL_DISTRO, "--", "bash", "-c", script],
            capture_output=True, text=True, timeout=180,
        )
        return result.returncode, result.stdout, result.stderr

    def test_posix_wrapper_help_lists_stash_and_pop(self) -> None:
        if not self.wsl:
            self.skipTest("WSL Ubuntu not available")
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_workspace(Path(tmp))
            code, out, err = self.run_posix_wrapper("help", ws)
            self.assertEqual(0, code, out + err)
            self.assertIn("stash", out)
            self.assertIn("pop", out)

    def test_posix_wrapper_stash_pop_roundtrip(self) -> None:
        if not self.wsl:
            self.skipTest("WSL Ubuntu not available")
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_workspace(Path(tmp))
            code, out, err = self.run_posix_wrapper("stash --workflow-id 111", ws)
            self.assertEqual(0, code, out + err)
            self.assertFalse(
                (ws / "workflows" / "primary" / "alpha" / "workflow.json").exists()
            )
            code, out, err = self.run_posix_wrapper("pop", ws)
            self.assertEqual(0, code, out + err)
            self.assertEqual(
                WF_ALPHA.encode(),
                (ws / "workflows" / "primary" / "alpha" / "workflow.json").read_bytes(),
            )

    def test_posix_wrapper_conflict_exits_nonzero(self) -> None:
        if not self.wsl:
            self.skipTest("WSL Ubuntu not available")
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_workspace(Path(tmp))
            code, out, err = self.run_posix_wrapper("stash --workflow-id 111", ws)
            self.assertEqual(0, code, out + err)
            target = ws / "workflows" / "primary" / "alpha" / "workflow.json"
            target.write_bytes(b'{"id": "111", "pulled": true}')
            code, out, err = self.run_posix_wrapper("pop", ws)
            self.assertNotEqual(0, code)
            self.assertIn("CONFLICT", err)
            self.assertEqual(b'{"id": "111", "pulled": true}', target.read_bytes())
            # The stash entry is retained.
            self.assertEqual(1, len(list((ws / ".stash").iterdir())))

    def test_posix_wrapper_bare_stash_exits_1(self) -> None:
        if not self.wsl:
            self.skipTest("WSL Ubuntu not available")
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_workspace(Path(tmp))
            code, out, err = self.run_posix_wrapper("stash", ws)
            self.assertEqual(1, code)
            self.assertIn("Refusing broad capture", err)

    def test_posix_wrapper_syntax_check(self) -> None:
        bash = _find_git_bash()
        if not bash:
            self.skipTest("no Git Bash available for syntax check")
        result = subprocess.run(
            [bash, "-n", str(REPO_ROOT / "n8n")], capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_posix_wrapper_has_lf_endings(self) -> None:
        """The POSIX wrapper must never regress to CRLF (a Windows autocrlf
        checkout breaks it on Linux/macOS; .gitattributes pins eol=lf)."""
        data = (REPO_ROOT / "n8n").read_bytes()
        self.assertNotIn(b"\r\n", data)
        self.assertTrue(data.endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
