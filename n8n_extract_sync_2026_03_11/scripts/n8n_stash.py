#!/usr/bin/env python3
"""n8n stash: locally move workflow export files into an ignored .stash/ tree.

Local-only tool. No network, no dotenv, no credentials, no telemetry, and it
never reads or writes .n8n_sync/state.json.

Safety model:
  - Moves use an atomic no-replace primitive (exclusive hard link + source
    unlink verified by inode identity). A racing writer's bytes are never
    overwritten or deleted; filesystems without hard links fail safely.
  - Rollback/preflight reconcile the ACTUAL filesystem state, never trusting
    recorded progress states; an entry is deleted only after every original
    is verified restored and no unique payload bytes remain.
  - Symlinks/junctions/reparse points are rejected on .stash, entries,
    manifests, payload ancestors, and destinations, before reads/mutations.
  - Locks are never stolen automatically and release removes only the exact
    lock file this instance created.

Commands:
  stash --workflow-id <id> [--instance primary] [--message TEXT] [--dry-run]
  stash --all             [--instance primary] [--message TEXT] [--dry-run]
  stash list
  stash show [selector]
  stash drop <selector> [--dry-run]
  pop   [selector] [--dry-run]

Layout:
  <workspace>/.stash/<entry-id>/manifest.json          transaction manifest
  <workspace>/.stash/<entry-id>/workflows/...          stashed files, mirrored
                                                       original relative paths

Entry IDs are immutable UTC stamps with a random suffix, e.g.
20261112T103000.123456Z-ab12cd34. A selector is `stash@{N}`, a plain index `N`,
or an entry ID. Bare pop/show target the newest complete entry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from n8n_common import resolve_workspace_root, utc_now_iso  # local helpers only


class StashError(RuntimeError):
    """Raised for expected stash/pop failures with user-facing messages."""


# ── constants ────────────────────────────────────────────────────────────
STASH_DIRNAME = ".stash"
MANIFEST_FILENAME = "manifest.json"
LOCK_FILENAME = ".lock"
MANIFEST_VERSION = 1
WORKFLOW_FILE = "workflow.json"
METADATA_FILE = "metadata.json"

ENTRY_ID_RE = re.compile(r"^\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{8}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SELECTOR_INDEX_RE = re.compile(r"^stash@\{(\d+)\}$|^(\d+)$")

ALLOWED_FILE_NAMES = (WORKFLOW_FILE, METADATA_FILE)
ITEM_FILE_STATES = ("pending", "capturing", "stashed", "restored", "rolled-back", "rollback-blocked")
CAPTURE_DIRNAME = ".captured"
STASH_PHASE_STATES = ("committing", "incomplete-rollback")
RESTORE_PHASE_STATES = ("restoring", "incomplete-restore")
COMPLETE_STATE = "committed"

LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.1
IGNORE_LINE = "/.stash/"


def _fail(message: str) -> "StashError":
    return StashError(message)


# ── small filesystem helpers ─────────────────────────────────────────────
def _is_reparse_point(path: Path) -> bool:
    """True for symlinks, junctions, and any Windows reparse point."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    st_file_attributes = getattr(st, "st_file_attributes", 0)
    if st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    return os.path.islink(path)


def _is_regular_file(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, data)


def _exclusive_copy(src: Path, dst: Path, expected_sha: str) -> None:
    """Fallback publish when hard links are unsupported: exclusively create dst
    (O_CREAT|O_EXCL is atomic no-replace on every platform), copy the verified
    source bytes, fsync, and verify the copy's checksum. Never overwrites; a
    checksum mismatch removes our incomplete creation and keeps src."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(str(dst), flags)
    except FileExistsError:
        raise _fail(
            f"Refusing to overwrite: a file appeared at {dst} during the move "
            "(racing writer); its bytes are untouched."
        )
    except OSError as exc:
        raise _fail(f"Cannot create destination exclusively: {dst}: {exc}")
    try:
        with open(src, "rb") as src_fh, os.fdopen(fd, "wb") as dst_fh:
            for chunk in iter(lambda: src_fh.read(1024 * 1024), b""):
                dst_fh.write(chunk)
            dst_fh.flush()
            os.fsync(dst_fh.fileno())
    except OSError as exc:
        try:
            os.unlink(dst)  # our incomplete exclusive creation
        except OSError:
            pass
        raise _fail(f"Copy failed during fallback publish ({src} -> {dst}): {exc}")
    if _sha256_file(dst) != expected_sha:
        try:
            os.unlink(dst)  # our verified-corrupt incomplete creation
        except OSError:
            pass
        raise _fail(
            f"Fallback copy verification failed for {dst}; incomplete copy "
            f"removed, source bytes kept at {src}."
        )


def _move_no_overwrite(src: Path, dst: Path) -> None:
    """Move src to dst using an atomic no-replace primitive.

    Primary path: an exclusive hard link (link(2)/CreateHardLink both fail if
    the destination exists) followed by a source unlink that is only performed
    after the source still verifies as the SAME inode as the destination. The
    destination link is never destroyed on a mismatch: it may hold the only
    copy of the payload. A racing writer that replaced the source leaves its
    bytes untouched and the move fails closed.

    Fallback when hard links are unsupported: exclusive-create (O_CREAT|O_EXCL,
    atomic no-replace) + checksum-verified copy; the source is unlinked only
    after the copy verifies. Callers only ever pass operation-owned source
    names (stash payloads / quarantine captures), never user-facing paths.
    """
    if os.path.lexists(dst):
        raise _fail(f"Refusing to overwrite existing file: {dst}")
    if _is_reparse_point(src):
        raise _fail(f"Refusing to move a symlink/junction source: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(str(src), str(dst))
    except FileExistsError:
        raise _fail(
            f"Refusing to overwrite: a file appeared at {dst} during the move "
            "(racing writer); its bytes are untouched."
        )
    except OSError:
        # Hard links unsupported here: fall back to exclusive-create + verified
        # copy (still atomic no-replace on the destination). src is an
        # operation-owned name in all callers of this helper.
        _exclusive_copy(src, dst, _sha256_file(src))
        os.unlink(str(src))
        return
    try:
        dst_st = os.lstat(dst)
        if not stat.S_ISREG(dst_st.st_mode):
            # Our exclusive creation is not a usable regular file; remove it.
            try:
                os.unlink(dst)
            except OSError:
                pass
            raise _fail(f"Move produced a non-regular destination: {dst}")
        try:
            src_st = os.lstat(src)
        except FileNotFoundError:
            # The source name is already gone; the payload lives at dst.
            return
        if (
            not stat.S_ISREG(src_st.st_mode)
            or (src_st.st_ino, src_st.st_dev) != (dst_st.st_ino, dst_st.st_dev)
        ):
            # The source was replaced by a racing writer mid-move. The payload
            # is safe at dst; the racer's bytes at src stay untouched.
            raise _fail(
                f"Source was replaced during the move; payload retained at "
                f"{dst} and the current bytes at {src} were not deleted."
            )
        os.unlink(str(src))
    except StashError:
        raise
    except OSError as exc:
        raise _fail(
            f"Failed to complete move {src} -> {dst} (the destination link "
            f"holds the payload bytes and was kept): {exc}"
        )


def _capture_source(src: Path, quarantine: Path, expected_sha: str) -> None:
    """Atomically take the current source inode into the operation-owned
    quarantine path and verify the captured bytes.

    The capture is an atomic rename, so the mutable current source pathname is
    NEVER unlinked: whatever it points to at capture time is moved (not
    deleted) into quarantine. If the captured bytes do not match the expected
    checksum, a racing writer's file was captured instead; a copy is published
    back through the exclusive non-destructive publish helper and the
    unexpected capture is ALWAYS retained at its quarantine path — the stash
    fails closed either way.
    """
    try:
        os.rename(str(src), str(quarantine))
    except FileExistsError:
        raise _fail(f"Capture path already exists (unexpected): {quarantine}")
    except FileNotFoundError:
        raise _fail(f"Source disappeared before capture: {src}")
    except OSError as exc:
        raise _fail(f"Cannot capture source safely ({src} -> {quarantine}): {exc}")
    actual_sha = _sha256_file(quarantine)
    if actual_sha == expected_sha:
        return
    # Captured a racing writer's replacement, not our payload. Put a copy of
    # it back ONLY through the non-destructive exclusive publish helper: a
    # replace-capable rename onto the mutable original path could destroy a
    # file a re-occupier wrote in the meantime, and even the verified-copy
    # MOVE would unlink the quarantine name after publishing — a racing
    # replacement landing on the restored copy right then would leave the
    # unexpected captured bytes nowhere. The unexpected capture is therefore
    # ALWAYS retained at its quarantine path (even when a copy initially
    # verified at the original path), so both versions survive anywhere.
    publish_error: Optional[str] = None
    try:
        _publish_captured(quarantine, src, actual_sha)
    except StashError as exc:
        publish_error = str(exc)
    if publish_error is None:
        raise _fail(
            f"Source changed since selection (racing writer): its bytes were "
            f"published back to {src} (a later racing writer may have replaced "
            f"them) and the unexpected captured bytes are ALSO retained at "
            f"{quarantine} for manual review; stash aborted."
        )
    raise _fail(
        f"Source changed since selection (racing writer) and its path is "
        f"occupied; the unexpected captured bytes are retained at "
        f"{quarantine} (nothing was overwritten or deleted): {publish_error}"
    )


def _publish_captured(quarantine: Path, dst: Path, expected_sha: str) -> None:
    """Publish the verified captured payload to dst with exclusive no-replace
    semantics. The quarantine name is kept on any failure (it may hold the
    only copy); the caller unlinks it only after verified success."""
    if os.path.lexists(dst):
        raise _fail(f"Refusing to overwrite existing file: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(str(quarantine), str(dst))
    except FileExistsError:
        raise _fail(
            f"Refusing to overwrite: a file appeared at {dst} during publish "
            "(racing writer); its bytes are untouched."
        )
    except OSError:
        # Hard links unsupported here: exclusive-create + verified copy
        # fallback (still atomic no-replace on the destination). The copy is
        # checksum-verified inside; the quarantine name is then ours to keep
        # or remove.
        _exclusive_copy(quarantine, dst, expected_sha)
        return
    q_st = os.lstat(quarantine)
    d_st = os.lstat(dst)
    if not stat.S_ISREG(d_st.st_mode) or (q_st.st_ino, q_st.st_dev) != (d_st.st_ino, d_st.st_dev):
        # Keep the quarantine name: it may hold the only copy.
        raise _fail(
            f"Published payload at {dst} does not match the captured inode; "
            f"captured bytes retained at {quarantine}."
        )


def _assert_no_reparse_between(root: Path, target: Path) -> None:
    """Reject symlinks/junctions on the path from root down to target.

    Uses lexical components and os.lstat; links are rejected regardless of
    where they resolve (inside or outside the root). Non-existent components
    are skipped, so this is safe to call before creating a path.
    """
    try:
        rel = target.relative_to(root)
    except ValueError:
        raise _fail(f"Path {target} is not inside {root}")
    cur = root
    if _is_reparse_point(cur):
        raise _fail(f"Refusing unsafe path: root itself is a symlink/junction: {cur}")
    for part in rel.parts:
        cur = cur / part
        if _is_reparse_point(cur):
            raise _fail(
                f"Refusing unsafe path: symlink/junction/reparse point in path: {cur}"
            )


def _assert_stash_tree_safe(workspace: Path, entry_dir: Optional[Path] = None) -> None:
    """Reject reparse points on .stash root / entry dir before any reads or
    mutations. Call before touching the stash tree (including before the lock
    and before reading any manifest)."""
    stash = stash_root(workspace)
    if stash.exists() or stash.is_symlink():
        _assert_no_reparse_between(workspace, stash)
    if entry_dir is not None:
        _assert_no_reparse_between(stash, entry_dir)


def _known_entry_relpaths(manifest: Dict[str, Any], files: List[Dict[str, Any]]) -> Set[str]:
    """Relative paths inside an entry that the operation owns (casefolded)."""
    known = {MANIFEST_FILENAME.casefold()}
    for item in manifest["items"]:
        for f in item["files"]:
            known.add(f["relpath"].casefold())
            if f.get("capture_path"):
                known.add(f["capture_path"].casefold())
    for f in files:
        if f.get("capture_relpath"):
            known.add(f["capture_relpath"].casefold())
    return known


def _scan_unknown_entry_paths(entry_dir: Path, known_casefold: Set[str]) -> List[str]:
    """Relative paths inside the entry that the operation does not own.

    Directories are descended (never through reparse points); a reparse point
    or a file not in the known set is reported as unknown. Atomic-write temp
    names (manifest.json.tmp-*) prove nothing: only the exact temp path a
    live atomic write creates is owned by that write, and it is renamed away
    (or unlinked by that same invocation on failure). Any temp file found
    later is unowned and must fail closed, never be cleaned up.
    """
    unknown: List[str] = []

    def walk(dir_path: Path, rel_prefix: str) -> None:
        try:
            children = sorted(os.scandir(dir_path), key=lambda e: e.name)
        except OSError:
            unknown.append(rel_prefix or ".")
            return
        for child in children:
            rel = f"{rel_prefix}{child.name}"
            if child.is_symlink() or _is_reparse_point(Path(child.path)):
                unknown.append(rel)
                continue
            if child.is_dir(follow_symlinks=False):
                walk(Path(child.path), rel + "/")
            elif rel.casefold() not in known_casefold:
                unknown.append(rel)

    walk(entry_dir, "")
    return unknown


def _cleanup_verified_entry(
    entry_dir: Path, manifest: Dict[str, Any], files: List[Dict[str, Any]]
) -> List[str]:
    """After a fully verified restore, remove ONLY operation-owned, verified
    names (known payload/capture files matching the manifest checksum, and the
    manifest last) and rmdir ONLY empty directories. Atomic-write temp files
    are never cleaned here: a temp name found on disk is unowned (only the
    exact temp of a live atomic write belongs to that write, which handles it
    itself).

    Returns the list of unknown/leftover paths that prevented full cleanup
    (empty when the entry directory is verifiably gone). The caller must then
    KEEP the entry, re-persist the in-memory manifest, and report nonzero —
    unverified bytes are never discarded and success is never claimed for an
    incomplete removal.
    """
    known = _known_entry_relpaths(manifest, files)
    unknown = _scan_unknown_entry_paths(entry_dir, known)
    if unknown:
        return unknown
    # Verified known files only: payload/capture leftovers must hash-match.
    for f in files:
        for rel in filter(None, [f.get("capture_relpath"), f["relpath"]]):
            path = entry_dir.joinpath(*_parse_relpath(rel, "relpath"))
            if not os.path.lexists(path):
                continue
            if (
                not _is_regular_file(path)
                or _is_reparse_point(path)
                or _sha256_file(path) != f["sha256"]
            ):
                return [rel]
            try:
                os.unlink(path)
            except OSError:
                return [rel]
    manifest_path = entry_dir / MANIFEST_FILENAME
    try:
        manifest_path.unlink()
    except OSError:
        return [MANIFEST_FILENAME]
    # Remove now-empty directories, deepest first; a dir that gained content
    # in the meantime simply fails to rmdir and is left in place.
    for dirpath, dirnames, filenames in os.walk(str(entry_dir), topdown=False):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
    # Removal must be verified, never assumed: if the directory still exists,
    # something unknown appeared (e.g. raced in between the pre-scan and the
    # manifest unlink). The manifest is already gone, so anything left is not
    # operation-owned; report it so the caller re-persists the manifest and
    # keeps the entry.
    if os.path.lexists(entry_dir):
        leftovers = _scan_unknown_entry_paths(entry_dir, known)
        return leftovers or ["."]
    return []


def _discard_entry_dir_authorized(entry_dir: Path, workspace: Path) -> None:
    """`stash drop` semantics: the user explicitly authorized discarding the
    whole entry, so unmanifested extras may go too — but never through or over
    a symlink/junction/reparse point, and never outside .stash/."""
    if not ENTRY_ID_RE.match(entry_dir.name):
        raise _fail(f"Refusing to remove entry with unexpected name: {entry_dir}")
    stash = stash_root(workspace)
    if _is_reparse_point(stash):
        raise _fail(f"Refusing to remove: .stash root is a symlink/junction: {stash}")
    if _is_reparse_point(entry_dir):
        raise _fail(f"Refusing to remove: entry is a symlink/junction: {entry_dir}")
    stash_resolved = stash.resolve()
    resolved = entry_dir.resolve()
    if resolved == stash_resolved or stash_resolved not in resolved.parents:
        raise _fail(f"Refusing to remove path outside .stash/: {resolved}")
    # Scan for reparse points inside; rmtree must never traverse one.
    for dirpath, dirnames, filenames in os.walk(str(entry_dir)):
        for name in dirnames + filenames:
            if _is_reparse_point(Path(dirpath) / name):
                raise _fail(
                    f"Refusing to drop: symlink/junction inside entry: "
                    f"{Path(dirpath) / name}"
                )
    shutil.rmtree(resolved)


# ── relative-path parsing / containment ──────────────────────────────────
def _parse_relpath(relpath: str, what: str = "path") -> List[str]:
    """Validate a stored relative path and return its parts."""
    if not isinstance(relpath, str) or not relpath.strip():
        raise _fail(f"Invalid {what}: empty value")
    if "\\" in relpath:
        raise _fail(f"Invalid {what} (backslash not allowed; use forward slashes): {relpath!r}")
    if relpath.startswith("/"):
        raise _fail(f"Invalid {what} (absolute path not allowed): {relpath!r}")
    if re.match(r"^[A-Za-z]:($|/)", relpath):
        raise _fail(f"Invalid {what} (drive-qualified path not allowed): {relpath!r}")
    parts = relpath.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise _fail(f"Invalid {what} (empty, '.', or '..' component): {relpath!r}")
        if ":" in part:
            raise _fail(f"Invalid {what} (':' component not allowed): {relpath!r}")
    return parts


def _check_case_collisions(relpaths: List[str], what: str) -> None:
    seen: Dict[str, str] = {}
    for relpath in relpaths:
        key = relpath.casefold()
        if key in seen and seen[key] != relpath:
            raise _fail(
                f"Case-colliding {what} entries would be unsafe on Windows: "
                f"{seen[key]!r} vs {relpath!r}"
            )
        seen[key] = relpath


# ── gitignore handling ───────────────────────────────────────────────────
def _utility_gitignore_path() -> Path:
    # Override hook for test isolation (wrapper roundtrip tests must never
    # touch the real utility repo's .gitignore).
    override = os.environ.get("N8N_STASH_UTILITY_GITIGNORE", "").strip()
    if override:
        return Path(override)
    return SCRIPT_DIR.parent / ".gitignore"


def _ignore_line_present(data: bytes) -> bool:
    for line in data.splitlines():
        if line.strip() in (b"/.stash/", b"/.stash"):
            return True
    return False


def _ensure_ignore_file(path: Path, label: str, dry_run: bool) -> str:
    """Ensure `/.stash/` is ignored in one .gitignore, appending once."""
    if path.is_symlink() or _is_reparse_point(path):
        raise _fail(f"Refusing unsafe {label}: .gitignore is a symlink/junction: {path}")
    if path.exists():
        if not _is_regular_file(path):
            raise _fail(f"Refusing unsafe {label}: .gitignore is not a regular file: {path}")
        data = path.read_bytes()
        if _ignore_line_present(data):
            return f"{label}: already ignored ({path})"
    else:
        data = b""
    if dry_run:
        return f"{label}: would append {IGNORE_LINE!r} to {path}"
    new_data = data
    if new_data and not new_data.endswith(b"\n"):
        new_data += b"\n"
    new_data += IGNORE_LINE.encode("ascii") + b"\n"
    _atomic_write_bytes(path, new_data)
    return f"{label}: appended {IGNORE_LINE!r} to {path}"


def ensure_gitignores(workspace: Path, dry_run: bool) -> List[str]:
    messages: List[str] = []
    workspace_gi = workspace / ".gitignore"
    utility_gi = _utility_gitignore_path()
    messages.append(_ensure_ignore_file(workspace_gi, "consuming workspace", dry_run))
    if utility_gi.resolve() != workspace_gi.resolve():
        messages.append(_ensure_ignore_file(utility_gi, "utility repo", dry_run))
    return messages


# ── exclusive operation lock ─────────────────────────────────────────────
class ExclusiveLock:
    """Exclusive lock via O_CREAT|O_EXCL with a bounded wait.

    Locks are never stolen automatically (an old mtime does NOT authorize a
    break): a contending run fails closed with the holder's details and
    recovery guidance. Release only removes the exact lock file this instance
    created (verified by inode/file ID), never a replacement written by
    someone else.
    """

    def __init__(self, path: Path, timeout: Optional[float] = None) -> None:
        self.path = path
        self.timeout = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
        self._held = False
        self._inode: Optional[int] = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    try:
                        holder = self.path.read_text(encoding="utf-8", errors="replace").strip()
                    except OSError:
                        holder = "<unreadable>"
                    raise _fail(
                        "Another stash/pop operation appears to be in progress "
                        f"(lock held: {self.path}; holder: {holder}). Stash does "
                        "not steal locks automatically, even old ones. If you are "
                        "certain no stash/pop is running (for example after a "
                        "crash), remove the lock file and retry."
                    )
                time.sleep(LOCK_POLL_SECONDS)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "pid": os.getpid(),
                    "utc": utc_now_iso(),
                    "command": " ".join(sys.argv[1:]),
                }))
            try:
                self._inode = os.stat(self.path).st_ino
            except OSError:
                self._inode = None
            self._held = True
            return

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            current_inode = os.stat(self.path).st_ino
        except OSError:
            return  # lock file already gone; nothing to remove
        if self._inode is not None and current_inode != self._inode:
            print(
                f"WARNING: stash lock at {self.path} was replaced by another "
                "process while it was held; not removing the replacement.",
                file=sys.stderr,
            )
            return
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "ExclusiveLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


# ── workspace + entry discovery ──────────────────────────────────────────
def resolve_workspace(explicit: Optional[str]) -> Path:
    if explicit:
        ws = Path(explicit).resolve()
    else:
        ws = resolve_workspace_root(None, SCRIPT_DIR).resolve()
    if not (ws / "workflows").is_dir():
        raise _fail(
            f"Workspace root {ws} does not contain a workflows/ directory. "
            "Run from the workflows workspace or pass --workspace."
        )
    if _is_reparse_point(ws):
        raise _fail(f"Refusing unsafe workspace root (symlink/junction): {ws}")
    return ws


def stash_root(workspace: Path) -> Path:
    return workspace / STASH_DIRNAME


def list_entry_dirs(workspace: Path) -> List[Path]:
    """Entry dirs including ones with broken/missing manifests, newest first."""
    root = stash_root(workspace)
    if not root.is_dir():
        return []
    out = [c for c in root.iterdir() if c.is_dir() and ENTRY_ID_RE.match(c.name)]
    return sorted(out, key=lambda p: p.name, reverse=True)


def read_manifest_raw(entry_dir: Path) -> Optional[Dict[str, Any]]:
    path = entry_dir / MANIFEST_FILENAME
    # Never read through a symlink/junction: an unsafe entry or manifest is
    # reported as unreadable rather than followed.
    if _is_reparse_point(entry_dir) or _is_reparse_point(path):
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _raw_entry_state(entry: Path) -> Optional[str]:
    raw = read_manifest_raw(entry)
    if raw and isinstance(raw, dict):
        tx = raw.get("transaction") or {}
        state = tx.get("state")
        return state if isinstance(state, str) else None
    return None


def resolve_entry_dir(workspace: Path, selector: Optional[str]) -> Path:
    """Resolve a selector to an entry directory.

    Bare selector = newest complete entry, falling back to the newest
    interrupted-restore entry (pop preflights make resuming it safe).
    """
    entries = list_entry_dirs(workspace)
    if not entries:
        raise _fail("No stash entries found.")
    if selector is None or selector == "":
        for entry in entries:
            if _raw_entry_state(entry) == COMPLETE_STATE:
                return entry
        for entry in entries:
            if _raw_entry_state(entry) in RESTORE_PHASE_STATES:
                return entry
        incomplete = ", ".join(e.name for e in entries)
        raise _fail(
            "No complete stash entries to restore. "
            f"Entries present (may be incomplete): {incomplete}"
        )
    match = SELECTOR_INDEX_RE.match(selector)
    if match:
        index = int(match.group(1) or match.group(2))
        if index >= len(entries):
            raise _fail(
                f"Selector {selector!r} is out of range: {len(entries)} entr"
                f"{'y' if len(entries) == 1 else 'ies'} exist."
            )
        return entries[index]
    if not ENTRY_ID_RE.match(selector):
        raise _fail(
            f"Invalid selector {selector!r}. Use an index (e.g. 'stash@{{0}}' or '0') "
            "or an entry ID (e.g. 20261112T103000.123456Z-ab12cd34)."
        )
    for entry in entries:
        if entry.name == selector:
            return entry
    raise _fail(f"No stash entry with ID {selector!r}.")


# ── manifest validation ──────────────────────────────────────────────────
def _validate_transaction_state(manifest: Dict[str, Any]) -> str:
    tx = manifest.get("transaction")
    if not isinstance(tx, dict):
        raise _fail("Invalid manifest: missing 'transaction' object.")
    state = tx.get("state")
    known = (COMPLETE_STATE,) + STASH_PHASE_STATES + RESTORE_PHASE_STATES
    if state not in known:
        raise _fail(f"Invalid manifest: unknown transaction state {state!r}.")
    return state


def load_and_validate_manifest(
    workspace: Path, entry_dir: Path
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Strictly validate a manifest and return (manifest, normalized files).

    Normalized file entries:
      {item, name, relpath_parts, original_path, stashed_path, size, sha256, state}
    """
    manifest = read_manifest_raw(entry_dir)
    if manifest is None:
        raise _fail(
            f"Invalid or unreadable manifest: {entry_dir / MANIFEST_FILENAME}"
        )
    if not isinstance(manifest, dict):
        raise _fail("Invalid manifest: top-level JSON is not an object.")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise _fail(
            f"Invalid manifest: unsupported manifest_version "
            f"{manifest.get('manifest_version')!r}."
        )
    if manifest.get("entry_id") != entry_dir.name:
        raise _fail(
            "Invalid manifest: entry_id "
            f"{manifest.get('entry_id')!r} does not match directory {entry_dir.name!r}."
        )
    instance = manifest.get("instance")
    if not isinstance(instance, str) or not INSTANCE_RE.match(instance):
        raise _fail(f"Invalid manifest: bad instance {instance!r}.")
    _validate_transaction_state(manifest)

    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise _fail("Invalid manifest: 'items' must be a non-empty list.")

    normalized: List[Dict[str, Any]] = []
    relpaths: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise _fail("Invalid manifest: item is not an object.")
        wdir_parts = _parse_relpath(item.get("workflow_dir", ""), "workflow_dir")
        if len(wdir_parts) != 3 or wdir_parts[0] != "workflows" or wdir_parts[1] != instance:
            raise _fail(
                "Invalid manifest: workflow_dir must be "
                f"'workflows/{instance}/<directory>', got {item.get('workflow_dir')!r}."
            )
        files = item.get("files")
        if not isinstance(files, list) or not files:
            raise _fail(f"Invalid manifest: item {wdir_parts[2]!r} has no files.")
        for f in files:
            if not isinstance(f, dict):
                raise _fail("Invalid manifest: file entry is not an object.")
            name = f.get("name")
            if name not in ALLOWED_FILE_NAMES:
                raise _fail(f"Invalid manifest: unexpected file name {name!r}.")
            parts = _parse_relpath(f.get("relpath", ""), "relpath")
            if parts != wdir_parts + [name]:
                raise _fail(
                    f"Invalid manifest: relpath {f.get('relpath')!r} does not match "
                    f"workflow_dir and file name {name!r}."
                )
            size = f.get("size")
            sha = f.get("sha256")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise _fail(f"Invalid manifest: bad size for {f.get('relpath')!r}.")
            if not isinstance(sha, str) or not SHA256_RE.match(sha):
                raise _fail(f"Invalid manifest: bad sha256 for {f.get('relpath')!r}.")
            fstate = f.get("state")
            if fstate not in ITEM_FILE_STATES:
                raise _fail(f"Invalid manifest: bad file state {fstate!r}.")
            capture_rel = f.get("capture_path")
            capture_parts: Optional[List[str]] = None
            if capture_rel is not None:
                capture_parts = _parse_relpath(capture_rel, "capture_path")
                if capture_parts[0] != CAPTURE_DIRNAME:
                    raise _fail(
                        f"Invalid manifest: capture_path must live under "
                        f"{CAPTURE_DIRNAME}/, got {capture_rel!r}."
                    )
            relpath = "/".join(parts)
            relpaths.append(relpath)
            if capture_parts:
                relpaths.append("/".join(capture_parts))
            normalized.append({
                "workflow_id": item.get("workflow_id"),
                "name": name,
                "relpath": relpath,
                "relpath_parts": parts,
                "original_path": workspace.joinpath(*parts),
                "stashed_path": entry_dir.joinpath(*parts),
                "capture_path": (
                    entry_dir.joinpath(*capture_parts) if capture_parts else None
                ),
                "capture_relpath": "/".join(capture_parts) if capture_parts else None,
                "size": size,
                "sha256": sha,
                "state": fstate,
            })

    _check_case_collisions(relpaths, "manifest file")

    # Belt-and-braces containment: nothing may resolve outside the workspace.
    ws_resolved = str(workspace.resolve())
    for entry in normalized:
        paths = [entry["original_path"], entry["stashed_path"]]
        if entry["capture_path"] is not None:
            paths.append(entry["capture_path"])
        for path in paths:
            try:
                resolved = str(path.resolve())
            except OSError as exc:
                raise _fail(f"Cannot resolve path {path}: {exc}")
            if resolved != ws_resolved and not resolved.startswith(ws_resolved + os.sep):
                raise _fail(f"Manifest path escapes the workspace: {entry['relpath']!r}")
    return manifest, normalized


def entry_state_of(manifest: Dict[str, Any]) -> str:
    return (manifest.get("transaction") or {}).get("state", "<unknown>")


# ── candidate scanning (stash) ───────────────────────────────────────────
def scan_candidates(
    workspace: Path, instance: str, workflow_id: Optional[str], want_all: bool
) -> List[Dict[str, Any]]:
    """Find workflow export dirs under workflows/<instance>/<dir>/."""
    inst_dir = workspace / "workflows" / instance
    if not inst_dir.is_dir():
        raise _fail(
            f"Instance directory not found: workflows/{instance} (workspace {workspace})"
        )
    _assert_no_reparse_between(workspace, inst_dir)

    matches: List[Dict[str, Any]] = []
    for child in sorted(inst_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if _is_reparse_point(child):
            print(f"NOTE: skipping symlink/junction workflow directory: {child}")
            continue
        wf = child / WORKFLOW_FILE
        if not wf.is_file():
            continue
        if _is_reparse_point(wf):
            print(f"NOTE: skipping symlink/junction workflow file: {wf}")
            continue
        if not want_all:
            try:
                payload = json.loads(wf.read_text(encoding="utf-8"))
                wf_id = str(payload.get("id", "")).casefold()
            except (OSError, ValueError):
                continue
            if wf_id != workflow_id.casefold():
                continue
        files = [wf]
        meta = child / METADATA_FILE
        if meta.is_file() and not _is_reparse_point(meta):
            files.append(meta)
        matches.append({"dir": child, "files": files})

    if not want_all:
        if not matches:
            raise _fail(
                f"No local workflow export with id {workflow_id!r} found under "
                f"workflows/{instance}/. Note: stash matches local export files only; "
                "the workflow does not need to be registered."
            )
        if len(matches) > 1:
            dirs = ", ".join(str(m["dir"]) for m in matches)
            raise _fail(
                f"Workflow id {workflow_id!r} matches more than one local export "
                f"directory: {dirs}. Stash each directory explicitly via a unique "
                "workflow id, or remove the duplicate export."
            )

    items: List[Dict[str, Any]] = []
    relpaths: List[str] = []
    for m in matches:
        rel_dir = PurePosixPath("workflows") / instance / m["dir"].name
        file_entries = []
        for path in m["files"]:
            relpath = f"{rel_dir.as_posix()}/{path.name}"
            relpaths.append(relpath)
            file_entries.append({
                "name": path.name,
                "relpath": relpath,
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
                "state": "pending",
            })
        items.append({
            "workflow_id": _read_workflow_id(m["dir"] / WORKFLOW_FILE),
            "workflow_dir": rel_dir.as_posix(),
            "files": file_entries,
        })

    _check_case_collisions(relpaths, "selected workflow export file")
    return items


def _read_workflow_id(path: Path) -> Optional[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload.get("id", "")) or None
    except (OSError, ValueError):
        return None


def _new_entry_id() -> str:
    # Microsecond UTC stamp: entry IDs sort lexically in true chronological
    # order, even for entries created within the same second.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
    return f"{stamp}Z-{uuid.uuid4().hex[:8]}"


# ── manifest mutation helpers ────────────────────────────────────────────
def _set_transaction(
    manifest: Dict[str, Any], state: str, operation: Optional[str] = None,
    last_error: Any = None,
) -> None:
    manifest["transaction"] = {
        "state": state,
        "operation": operation or manifest.get("transaction", {}).get("operation", "stash"),
        "updated_utc": utc_now_iso(),
        "last_error": last_error,
    }


def _persist_manifest(entry_dir: Path, manifest: Dict[str, Any]) -> None:
    _atomic_write_json(entry_dir / MANIFEST_FILENAME, manifest)


def _find_file_entry(manifest: Dict[str, Any], relpath: str) -> Dict[str, Any]:
    for item in manifest["items"]:
        for f in item["files"]:
            if f["relpath"] == relpath:
                return f
    raise _fail(f"Internal error: relpath {relpath!r} not in manifest.")


# ── commands ─────────────────────────────────────────────────────────────
def cmd_stash(args: argparse.Namespace) -> int:
    if bool(args.all) == bool(args.workflow_id):
        raise _fail(
            "Refusing broad capture: choose exactly one of --workflow-id <id> "
            "(stash one workflow export) or --all (stash every export for the "
            "instance)."
        )
    if args.workflow_id is not None and not str(args.workflow_id).strip():
        raise _fail("--workflow-id must not be empty.")
    workspace = resolve_workspace(args.workspace)
    if not INSTANCE_RE.match(args.instance):
        raise _fail(f"Invalid --instance value: {args.instance!r}")

    items = scan_candidates(workspace, args.instance, args.workflow_id, bool(args.all))
    file_count = sum(len(item["files"]) for item in items)
    print(
        f"Stash plan: {len(items)} workflow export(s), {file_count} file(s) from "
        f"workflows/{args.instance}/ into {stash_root(workspace)}/"
    )
    for item in items:
        for f in item["files"]:
            print(f"  move  {f['relpath']}")
    if args.dry_run:
        for msg in ensure_gitignores(workspace, dry_run=True):
            print(f"  {msg}")
        print("Dry run: no files were moved and nothing was written.")
        return 0

    entry_id = _new_entry_id()
    entry_dir = stash_root(workspace) / entry_id
    lock = ExclusiveLock(stash_root(workspace) / LOCK_FILENAME)
    lock.acquire()
    try:
        # Guard the stash tree BEFORE creating anything: a junctioned .stash
        # would otherwise receive the payload outside the workspace.
        _assert_stash_tree_safe(workspace)
        ensure_gitignores(workspace, dry_run=False)
        entry_dir.mkdir(parents=True)
        # Re-verify: a racing .stash replacement could redirect entry creation.
        _assert_stash_tree_safe(workspace, entry_dir)
        manifest: Dict[str, Any] = {
            "manifest_version": MANIFEST_VERSION,
            "entry_id": entry_id,
            "created_utc": utc_now_iso(),
            "message": args.message or "",
            "instance": args.instance,
            "items": items,
            "transaction": {
                "state": "committing",
                "operation": "stash",
                "updated_utc": utc_now_iso(),
                "last_error": None,
            },
        }
        # Manifest is written atomically BEFORE any file moves.
        _persist_manifest(entry_dir, manifest)

        try:
            capture_dir = entry_dir / CAPTURE_DIRNAME
            capture_dir.mkdir(exist_ok=True)
            for item in manifest["items"]:
                for f in item["files"]:
                    parts = _parse_relpath(f["relpath"], "relpath")
                    src = workspace.joinpath(*parts)
                    dst = entry_dir.joinpath(*parts)
                    # Recheck immediately before capturing; never overwrite a
                    # racing file, and never traverse a reparse point.
                    _assert_stash_tree_safe(workspace, entry_dir)
                    _assert_no_reparse_between(workspace, src)
                    if not _is_regular_file(src):
                        raise _fail(
                            f"Source changed or disappeared before move: {src}"
                        )
                    if os.path.lexists(dst):
                        raise _fail(f"Stash destination already exists: {dst}")
                    if _sha256_file(src) != f["sha256"]:
                        raise _fail(
                            f"Source file changed since selection (racing writer?): "
                            f"{src}"
                        )
                    # Capture first: atomically take the source inode into an
                    # operation-owned quarantine name, verify its bytes, then
                    # publish with exclusive no-replace semantics. The mutable
                    # current source pathname is NEVER unlinked.
                    capture_rel = (
                        f"{CAPTURE_DIRNAME}/captured-{uuid.uuid4().hex[:12]}-{f['name']}"
                    )
                    quarantine = entry_dir.joinpath(*_parse_relpath(capture_rel))
                    f["state"] = "capturing"
                    f["capture_path"] = capture_rel
                    _persist_manifest(entry_dir, manifest)
                    _capture_source(src, quarantine, f["sha256"])
                    _publish_captured(quarantine, dst, f["sha256"])
                    os.unlink(str(quarantine))  # verified operation-owned name
                    f["state"] = "stashed"
                    f["capture_path"] = None
                    _persist_manifest(entry_dir, manifest)
            _set_transaction(manifest, COMPLETE_STATE)
            _persist_manifest(entry_dir, manifest)
        except BaseException as exc:
            # Covers injected failures as well as Ctrl-C mid-run.
            _rollback_stash(workspace, entry_dir, manifest, exc)
            raise
        print(
            f"Stashed {file_count} file(s) into .stash/{entry_id} "
            f"({len(items)} workflow export(s))."
        )
        print(f"Restore with: n8n pop {entry_id}")
        return 0
    finally:
        lock.release()


def _rollback_stash(
    workspace: Path, entry_dir: Path, manifest: Dict[str, Any], cause: BaseException
) -> None:
    """Reconcile the filesystem after a failed stash, never trusting the
    in-memory per-file states: a capture/publish may have completed before the
    injected failure (or a KeyboardInterrupt) hit, so every file is located by
    what is actually on disk — payload path, quarantine capture path, and
    original path. The entry is deleted only when every original path is
    verified restored (matching the manifest hash) and no unique payload bytes
    remain inside the entry; unmanifested extras are never discarded.
    """
    all_items = [f for item in manifest["items"] for f in item["files"]]
    blocked: List[Dict[str, Any]] = []
    lost: List[Dict[str, Any]] = []
    for f in reversed(all_items):
        parts = _parse_relpath(f["relpath"], "relpath")
        original = workspace.joinpath(*parts)
        try:
            payload: Optional[Path] = entry_dir.joinpath(*parts)
            if not (os.path.lexists(payload) and not _is_reparse_point(payload)
                    and _is_regular_file(payload)
                    and _sha256_file(payload) == f["sha256"]):
                payload = None
        except OSError:
            payload = None
        capture: Optional[Path] = None
        if f.get("capture_path"):
            capture = entry_dir.joinpath(
                *_parse_relpath(f["capture_path"], "capture_path")
            )
            try:
                if not (os.path.lexists(capture) and not _is_reparse_point(capture)
                        and _is_regular_file(capture)
                        and _sha256_file(capture) == f["sha256"]):
                    capture = None
            except OSError:
                capture = None
        orig_ok = (
            os.path.lexists(original)
            and not _is_reparse_point(original)
            and _is_regular_file(original)
        )
        try:
            if orig_ok and _sha256_file(original) == f["sha256"]:
                # Original already holds the verified payload; drop verified
                # operation-owned duplicates (payload/capture hard links).
                for dup in filter(None, [payload, capture]):
                    os.unlink(str(dup))  # verified duplicate at a validated path
                f["state"] = "rolled-back"
            elif payload is not None and not os.path.lexists(original):
                # Unique payload at the stash path: move it back (no-replace).
                _move_no_overwrite(payload, original)
                if _sha256_file(original) != f["sha256"]:
                    raise _fail(
                        f"restored bytes do not match the manifest for {f['relpath']}"
                    )
                if capture is not None:
                    os.unlink(str(capture))  # verified duplicate
                f["state"] = "rolled-back"
            elif capture is not None and not os.path.lexists(original):
                # Unique payload still in quarantine: restore it (no-replace).
                _move_no_overwrite(capture, original)
                if _sha256_file(original) != f["sha256"]:
                    raise _fail(
                        f"restored bytes do not match the manifest for {f['relpath']}"
                    )
                f["state"] = "rolled-back"
            elif not orig_ok and not os.path.lexists(original) and payload is None and capture is None:
                # Neither copy exists: the payload is gone. Keep the entry and
                # report loudly; never silently delete the manifest record.
                f["state"] = "rollback-blocked"
                blocked.append(f)
                lost.append(f)
            else:
                # Original exists with different bytes (racing writer) or an
                # unexpected payload state: retain everything for review.
                f["state"] = "rollback-blocked"
                blocked.append(f)
        except (StashError, OSError):
            f["state"] = "rollback-blocked"
            blocked.append(f)

    if not blocked:
        # Every original verified restored; discard only verified owned names.
        unknown = _cleanup_verified_entry(entry_dir, manifest, all_items)
        if not unknown and not entry_dir.exists():
            print(
                f"Stash failed ({cause}); every file was verified back at its "
                "original path; entry removed.",
                file=sys.stderr,
            )
            return
        if unknown:
            blocked = all_items  # fall through: entry retained, extras preserved

    _set_transaction(manifest, "incomplete-rollback", last_error=str(cause))
    _persist_manifest(entry_dir, manifest)
    print(
        f"Stash failed and rollback is incomplete. Entry {entry_dir.name} was "
        "kept with all remaining bytes. Recovery guidance:",
        file=sys.stderr,
    )
    for f in all_items:
        parts = _parse_relpath(f["relpath"], "relpath")
        stashed = entry_dir.joinpath(*parts)
        original = workspace.joinpath(*parts)
        capture = (
            entry_dir.joinpath(*_parse_relpath(f["capture_path"], "capture_path"))
            if f.get("capture_path") else None
        )
        if f in lost:
            print(
                f"  WARNING: no copy of {f['relpath']} exists at either "
                f"{stashed} or {original}; restore from the server with "
                "`n8n backup` or from git.",
                file=sys.stderr,
            )
        elif f["state"] == "rollback-blocked":
            locations = f"  inspect {original}"
            if os.path.lexists(stashed):
                locations += f", {stashed}"
            if capture is not None and os.path.lexists(capture):
                locations += f", {capture}"
            print(locations + " (rollback blocked: content mismatch or a racing file)",
                  file=sys.stderr)
        elif f["state"] == "stashed":
            print(
                f"  move {stashed}  ->  {original}   (stashed, not yet rolled back)",
                file=sys.stderr,
            )
        else:
            print(f"  untouched at original path: {original}", file=sys.stderr)
    if entry_dir.exists():
        raise _fail(
            f"Stash failed ({cause}); entry {entry_dir.name} retained as "
            "incomplete-rollback with exact recovery paths above."
        )


def cmd_list(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.workspace)
    entries = list_entry_dirs(workspace)
    if not entries:
        print("No stash entries.")
        return 0
    print(f"Stash entries in {stash_root(workspace)} (newest first):")
    _assert_stash_tree_safe(workspace)
    for index, entry in enumerate(entries):
        if _is_reparse_point(entry):
            print(
                f"  {index}  UNSAFE (symlink/junction)  {entry.name}  "
                "(not read; inspect manually before deleting)"
            )
            continue
        raw = read_manifest_raw(entry)
        if raw is None or not isinstance(raw, dict):
            print(
                f"  {index}  <broken-manifest>  {entry.name}  "
                f"(manifest unreadable; inspect {entry / MANIFEST_FILENAME})"
            )
            continue
        state = entry_state_of(raw)
        tag = "complete" if state == COMPLETE_STATE else f"INCOMPLETE ({state})"
        message = raw.get("message") or ""
        items = raw.get("items") or []
        files = sum(len(i.get("files") or []) for i in items if isinstance(i, dict))
        print(
            f"  {index}  {tag:<28} {entry.name}  "
            f"instance={raw.get('instance', '?')} files={files}"
            + (f"  message={message!r}" if message else "")
        )
    print("Restore with: n8n pop [stash@{N}|entry-id]")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.workspace)
    entry_dir = resolve_entry_dir(workspace, args.selector)
    _assert_stash_tree_safe(workspace, entry_dir)
    _show_entry(workspace, entry_dir)
    return 0


def _show_entry(workspace: Path, entry_dir: Path) -> None:
    manifest, files = load_and_validate_manifest(workspace, entry_dir)
    print(f"Entry:   {manifest['entry_id']}")
    print(f"State:   {entry_state_of(manifest)}")
    print(f"Created: {manifest.get('created_utc', '?')}")
    print(f"Instance: {manifest.get('instance', '?')}")
    if manifest.get("message"):
        print(f"Message: {manifest['message']}")
    print(f"Files ({len(files)}):")
    for f in files:
        print(f"  [{f['state']:<10}] {f['relpath']}")
        print(f"               size={f['size']} sha256={f['sha256']}")
        print(f"               stashed as: {f['stashed_path']}")


def _verify_stash_payload(entry_dir: Path, files: List[Dict[str, Any]]) -> None:
    """Strict payload verification for drop: every file must be present in the
    entry and match the manifest. Never reads through a reparse point."""
    for f in files:
        stashed: Path = f["stashed_path"]
        _assert_no_reparse_between(entry_dir, stashed)
        if not _is_regular_file(stashed):
            raise _fail(
                f"Stash payload path exists but is not a regular file (it may "
                f"contain unique bytes; inspect manually): {stashed}"
            )
        actual_size = stashed.stat().st_size
        if actual_size != f["size"]:
            raise _fail(
                f"Stash payload size mismatch for {f['relpath']}: manifest says "
                f"{f['size']}, on disk {actual_size} ({stashed})"
            )
        actual_sha = _sha256_file(stashed)
        if actual_sha != f["sha256"]:
            raise _fail(
                f"Stash payload was modified since stash (sha256 mismatch) for "
                f"{f['relpath']}: manifest says {f['sha256']}, on disk {actual_sha} "
                f"({stashed})"
            )


def _preflight_restore(
    workspace: Path, entry_dir: Path, files: List[Dict[str, Any]]
) -> Tuple[List[Tuple[Dict[str, Any], str]], List[str]]:
    """Reconcile BOTH locations for every file before any mutation.

    A file whose stashed copy is gone (moved during an interrupted restore,
    before the progress manifest was persisted) counts as restored ONLY after
    the destination validates against the manifest hash and size; otherwise it
    is a conflict. Returns (plan, conflicts); plan actions are 'move' and
    'already-restored'.
    """
    plan: List[Tuple[Dict[str, Any], str]] = []
    conflicts: List[str] = []
    # Any path inside the entry that the operation does not own must refuse
    # the whole restore BEFORE mutation: successful cleanup never discards
    # unverified bytes.
    known = {MANIFEST_FILENAME.casefold()}
    for f in files:
        known.add(f["relpath"].casefold())
        if f.get("capture_relpath"):
            known.add(f["capture_relpath"].casefold())
    unknown = _scan_unknown_entry_paths(entry_dir, known)
    for rel in unknown:
        conflicts.append(
            f"CONFLICT in stash entry {entry_dir.name}: unknown path {rel!r} is "
            "not part of the manifest. Inspect it manually; the restore is "
            "refused so the bytes are never discarded."
        )
    for f in files:
        original: Path = f["original_path"]
        stashed: Path = f["stashed_path"]
        # Reject reparse points on payload ancestors and destinations before
        # any reads or moves.
        _assert_no_reparse_between(entry_dir, stashed)
        _assert_no_reparse_between(workspace, original)
        stashed_exists = os.path.lexists(stashed)
        if stashed_exists and (_is_reparse_point(stashed) or not _is_regular_file(stashed)):
            # A non-regular payload (e.g. a directory) is NOT "missing": it may
            # hold unique bytes that cleanup would destroy. Refuse entirely.
            conflicts.append(
                f"CONFLICT at {stashed}: stash payload path exists but is not a "
                "regular file (e.g. a directory). It may contain unique bytes; "
                "inspect it manually — the restore is refused."
            )
            continue
        stashed_ok = stashed_exists
        orig_exists = os.path.lexists(original)
        if stashed_ok:
            # A modified stash payload must fail BEFORE any mutation.
            actual_size = stashed.stat().st_size
            if actual_size != f["size"]:
                conflicts.append(
                    f"Stash payload size mismatch for {f['relpath']}: manifest "
                    f"says {f['size']}, on disk {actual_size} ({stashed})"
                )
                continue
            actual_sha = _sha256_file(stashed)
            if actual_sha != f["sha256"]:
                conflicts.append(
                    f"Stash payload was modified since stash (sha256 mismatch) "
                    f"for {f['relpath']}: manifest says {f['sha256']}, on disk "
                    f"{actual_sha} ({stashed})"
                )
                continue
        if not stashed_ok and not orig_exists:
            conflicts.append(
                f"Stash payload file is missing and the destination is absent: "
                f"{stashed}"
            )
            continue
        if not stashed_ok:
            # Stashed copy already moved (stale progress manifest). The
            # destination must validate as the restored copy.
            if _is_regular_file(original) and _sha256_file(original) == f["sha256"]:
                plan.append((f, "already-restored"))
            else:
                conflicts.append(
                    f"CONFLICT at {original}: the stashed copy is gone and the "
                    "destination does not match the manifest (content or type "
                    "mismatch). Do not delete the entry; inspect both locations."
                )
            continue
        if orig_exists:
            if _is_reparse_point(original) or not _is_regular_file(original):
                conflicts.append(
                    f"CONFLICT at {original}: destination is a symlink/junction "
                    f"or not a regular file (stashed file: {stashed})"
                )
            elif _sha256_file(original) == f["sha256"]:
                plan.append((f, "already-restored"))
            else:
                conflicts.append(
                    f"CONFLICT at {original}: destination content differs from "
                    f"stashed copy {stashed}. Merge manually first; the stash "
                    "entry is retained until you resolve or drop it."
                )
        else:
            plan.append((f, "move"))
    return plan, conflicts


def cmd_pop(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.workspace)
    entry_dir = resolve_entry_dir(workspace, args.selector)
    _assert_stash_tree_safe(workspace, entry_dir)
    lock = ExclusiveLock(stash_root(workspace) / LOCK_FILENAME)
    with lock:
        manifest, files = load_and_validate_manifest(workspace, entry_dir)
        state = entry_state_of(manifest)

        if state in STASH_PHASE_STATES:
            lines = [
                f"Entry {entry_dir.name} is in an interrupted stash state "
                f"({state}). It is kept with all bytes; inspect it with "
                f"'n8n stash show {entry_dir.name}', then either recover the "
                "files manually using the paths below or delete the entry "
                "directory once reviewed:"
            ]
            for f in files:
                lines.append(
                    f"  move {f['stashed_path']}  ->  {f['original_path']}"
                )
                if f.get("capture_path") is not None and os.path.lexists(f["capture_path"]):
                    lines.append(
                        f"    (captured bytes pending publish: {f['capture_path']} "
                        "-> same destination as above)"
                    )
            raise _fail("\n".join(lines))
        if state in RESTORE_PHASE_STATES:
            print(
                f"NOTE: resuming interrupted restore of {entry_dir.name} "
                "(already-restored files are detected as identical; remaining "
                "files are moved)."
            )

        # Preflight the WHOLE entry (reconciling both locations) before any
        # mutation.
        plan, conflicts = _preflight_restore(workspace, entry_dir, files)
        if conflicts:
            for line in conflicts:
                print(line, file=sys.stderr)
            raise _fail(
                f"Restore aborted with {len(conflicts)} conflict(s); the stash "
                f"entry {entry_dir.name} is unchanged and no destination file "
                "was modified."
            )

        moves = [f for f, action in plan if action == "move"]
        already = [f for f, action in plan if action == "already-restored"]
        if args.dry_run:
            for f in moves:
                print(f"  would move {f['stashed_path']}  ->  {f['original_path']}")
            for f in already:
                print(f"  already restored (identical): {f['original_path']}")
            print("Dry run: nothing was changed.")
            return 0

        _set_transaction(manifest, "restoring", operation="restore")
        _persist_manifest(entry_dir, manifest)
        try:
            for f in moves:
                # Recheck immediately before moving; never overwrite a racing file.
                if os.path.lexists(f["original_path"]):
                    raise _fail(
                        f"A file appeared at {f['original_path']} just before "
                        "restore; aborting without touching it. Re-run "
                        f"'n8n pop {entry_dir.name}' to re-evaluate."
                    )
                _move_no_overwrite(f["stashed_path"], f["original_path"])
                f["state"] = "restored"
                _find_file_entry(manifest, f["relpath"])["state"] = "restored"
                _persist_manifest(entry_dir, manifest)
            for f in already:
                f["state"] = "restored"
                _find_file_entry(manifest, f["relpath"])["state"] = "restored"
                _persist_manifest(entry_dir, manifest)

            # Verify every restored byte before removing the entry.
            for f in files:
                if not _is_regular_file(f["original_path"]):
                    raise _fail(f"Restored file missing after move: {f['original_path']}")
                if _sha256_file(f["original_path"]) != f["sha256"]:
                    raise _fail(
                        f"Restored file checksum mismatch: {f['original_path']}"
                    )
        except StashError as exc:
            _set_transaction(manifest, "incomplete-restore", operation="restore",
                             last_error=str(exc))
            _persist_manifest(entry_dir, manifest)
            print(
                f"Restore was interrupted. Entry {entry_dir.name} retained with all "
                "remaining bytes. Recovery: re-run 'n8n pop "
                f"{entry_dir.name}' (already-restored files are detected as "
                "identical and the remaining files are moved), or move files "
                "manually:",
                file=sys.stderr,
            )
            for f in files:
                marker = "restored" if f["state"] == "restored" else "STILL STASHED"
                print(f"  [{marker}] {f['original_path']}", file=sys.stderr)
                if f["state"] != "restored":
                    print(f"      from: {f['stashed_path']}", file=sys.stderr)
            raise

        # Cleanup: remove only verified, operation-owned names; any unknown
        # path (including one that appeared after the preflight) keeps the
        # entry alive so unverified bytes are never discarded.
        unknown = _cleanup_verified_entry(entry_dir, manifest, files)
        if unknown:
            # The restore itself succeeded, but the entry could not be fully
            # removed. Cleanup may have unlinked the manifest before hitting
            # the leftover, so re-persist the full in-memory manifest to keep
            # the entry inspectable and recoverable — never recursively clean
            # unverified bytes as a fallback.
            _set_transaction(
                manifest, "incomplete-restore", operation="restore",
                last_error=f"entry not fully removed; leftover path(s): {unknown}",
            )
            _persist_manifest(entry_dir, manifest)
            print(
                f"WARNING: restore succeeded, but the stash entry "
                f"{entry_dir.name} could NOT be fully removed; it was KEPT "
                "with its manifest and nothing unverified was deleted. "
                "Inspect and remove manually after review:",
                file=sys.stderr,
            )
            for rel in unknown:
                print(f"  {entry_dir / rel}", file=sys.stderr)
            print(
                f"Restored {len(moves)} file(s) "
                f"({len(already)} already restored) from .stash/{entry_dir.name}; "
                f"entry {entry_dir.name} retained (cleanup incomplete).",
                file=sys.stderr,
            )
            return 1
        print(
            f"Restored {len(moves)} file(s) "
            f"({len(already)} already restored) from .stash/{entry_dir.name}; "
            "entry removed."
        )
        return 0


def cmd_drop(args: argparse.Namespace) -> int:
    if not args.selector:
        raise _fail(
            "stash drop requires an explicit selector (index such as 'stash@{0}' "
            "or an entry ID); bare drop is refused."
        )
    workspace = resolve_workspace(args.workspace)
    entry_dir = resolve_entry_dir(workspace, args.selector)
    _assert_stash_tree_safe(workspace, entry_dir)
    lock = ExclusiveLock(stash_root(workspace) / LOCK_FILENAME)
    with lock:
        manifest, files = load_and_validate_manifest(workspace, entry_dir)
        state = entry_state_of(manifest)
        if state != COMPLETE_STATE:
            raise _fail(
                f"Refusing to drop entry {entry_dir.name}: it is not complete "
                f"(state: {state}). Review it with 'n8n stash show "
                f"{entry_dir.name}', recover files manually if needed, and remove "
                f"the entry directory only after you are sure: {entry_dir}"
            )
        # Never recursively remove unverified paths: verify payload first.
        _verify_stash_payload(entry_dir, files)
        if args.dry_run:
            print(f"Would drop stash entry {entry_dir.name} ({len(files)} files).")
            print("Dry run: nothing was changed.")
            return 0
        # Drop semantics: the user explicitly authorized discarding this whole
        # entry (including unmanifested extras), but never through a link.
        _discard_entry_dir_authorized(entry_dir, workspace)
        print(f"Dropped stash entry {entry_dir.name}.")
        return 0


# ── CLI ──────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        # The wrappers expose this as `n8n stash ...` / `n8n pop ...`; prog is
        # "n8n" so the usage line reads `n8n {stash,list,show,pop,drop}` and
        # never doubles the verb (no "n8n stash stash" / "n8n stash pop").
        prog="n8n",
        description="Locally stash (move) workflow export files into an ignored "
        ".stash/ tree and restore them later (`n8n stash --workflow-id ...`, "
        "`n8n stash list|show|drop`, `n8n pop [selector]`). Local-only: no "
        "network, no credentials, no .n8n_sync/state.json access.",
    )
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{stash,list,show,pop,drop}")

    p_stash = sub.add_parser("stash", help="Move selected workflow export files into .stash/")
    p_stash.add_argument("--workflow-id", help="Workflow ID of the local export to stash")
    p_stash.add_argument("--all", action="store_true",
                         help="Stash every workflow export for the instance")
    p_stash.add_argument("--instance", default="primary",
                         help="Instance folder under workflows/ (default: primary)")
    p_stash.add_argument("--message", default="", help="Optional note stored in the manifest")
    p_stash.add_argument("--dry-run", action="store_true",
                         help="Show the plan; no writes at all (including .gitignore)")
    p_stash.add_argument("--workspace", default=None,
                         help="Workspace root (default: auto-detect workflows/ + .n8n_sync/)")

    p_list = sub.add_parser("list", help="List stash entries, newest first")
    p_list.add_argument("--workspace", default=None)

    p_show = sub.add_parser("show", help="Show one stash entry (default: newest complete)")
    p_show.add_argument("selector", nargs="?", default=None,
                        help="stash@{N}, N, or entry ID")
    p_show.add_argument("--workspace", default=None)

    p_pop = sub.add_parser("pop", help="Restore a stash entry (default: newest complete)")
    p_pop.add_argument("selector", nargs="?", default=None,
                       help="stash@{N}, N, or entry ID")
    p_pop.add_argument("--dry-run", action="store_true")
    p_pop.add_argument("--workspace", default=None)

    p_drop = sub.add_parser("drop", help="Drop one stash entry (explicit selector required)")
    p_drop.add_argument("selector", nargs="?", default=None,
                        help="stash@{N}, N, or entry ID")
    p_drop.add_argument("--dry-run", action="store_true")
    p_drop.add_argument("--workspace", default=None)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    # The wrappers dispatch `n8n stash list|show|drop ...` as
    # ['stash', 'list'|'show'|'drop', ...]; drop the redundant leading 'stash'.
    if len(argv) >= 2 and argv[0] == "stash" and argv[1] in ("list", "show", "drop"):
        argv = argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "stash": cmd_stash,
        "list": cmd_list,
        "show": cmd_show,
        "pop": cmd_pop,
        "drop": cmd_drop,
    }
    try:
        return handlers[args.command](args)
    except StashError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: filesystem error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
