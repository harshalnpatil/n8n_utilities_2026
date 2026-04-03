#!/usr/bin/env python3
"""One-time cleanup: remove stale workflow folders left behind by renames.

For each workflow ID that has multiple folders (same ID suffix, different name prefix),
keeps only the folder referenced in state.json and removes the rest.

Run with --dry-run first to preview what would be deleted.
"""

import json
import os
import shutil
import stat
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "n8n_workflows_2026_01_25"
STATE_PATH = REPO_ROOT / ".n8n_sync" / "state.json"
WORKFLOWS_DIR = REPO_ROOT / "workflows"


def _rmtree_onexc(func, path, exc):
    if not isinstance(exc, PermissionError):
        raise exc
    os.chmod(path, stat.S_IWRITE)
    func(path)


def main():
    dry_run = "--dry-run" in sys.argv

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    records = state.get("records", {})

    # Build set of active local paths from state
    active_paths = set()
    for rec in records.values():
        local_path = rec.get("localPath", "")
        # localPath points to workflow.json; we want the parent folder
        folder = (REPO_ROOT / local_path.replace("\\", "/")).parent
        active_paths.add(folder.resolve())

    # Scan all workflow folders and group by ID suffix
    removed = 0
    for alias_dir in sorted(WORKFLOWS_DIR.iterdir()):
        if not alias_dir.is_dir():
            continue

        # Group folders by their ID suffix (last segment after final _)
        by_id = defaultdict(list)
        for folder in sorted(alias_dir.iterdir()):
            if not folder.is_dir():
                continue
            parts = folder.name.rsplit("_", 1)
            if len(parts) == 2:
                by_id[parts[1]].append(folder)

        for id_suffix, folders in sorted(by_id.items()):
            if len(folders) <= 1:
                continue

            # Multiple folders for same ID — keep the one in state, remove the rest
            for folder in folders:
                resolved = folder.resolve()
                if resolved in active_paths:
                    print(f"  KEEP   {folder.relative_to(REPO_ROOT)}")
                else:
                    print(f"  DELETE {folder.relative_to(REPO_ROOT)}")
                    if not dry_run:
                        shutil.rmtree(folder, onexc=_rmtree_onexc)
                    removed += 1

    label = "would remove" if dry_run else "removed"
    print(f"\n{label} {removed} stale folder(s)")


if __name__ == "__main__":
    main()
