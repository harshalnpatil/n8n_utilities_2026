# Changelog

## 2026-04-03

### Highlights
- Fixed workflow rename duplication bug in `n8n_sync.py` — when a workflow is renamed in n8n, the sync now renames the existing local folder instead of creating a duplicate.
- Added `_find_existing_dir_for_id()` and `_resolve_workflow_dir()` helpers to detect and handle folder renames by matching on workflow ID suffix.
- Updated `sync-two-way` PULL path to refresh `localPath` and `workflowName` in state after pulling a renamed workflow.
- Added one-time cleanup script `onetime_scripts/cleanup_renamed_duplicates.py` — removed 8 stale duplicate folders from `workflows/`.
- `workflow_diff_server.py`: `--local-path` now accepts a workflow directory (auto-resolves `workflow.json` inside it); startup message shows the resolved file path.
- Fixed diff reviewer layout so the tab bar and JSON diff navigation buttons (Previous/Next diff) stay frozen at the top while only the diff content scrolls — body set to fixed viewport height with `overflow: hidden`, table thead sticky top corrected to `0`.

## 2026-04-02

### Summary
- `3 files changed, 52 insertions(+), 5 deletions(-)` (across 2 commits + 1 uncommitted change).

### Highlights
- Added view state persistence to diff reviewer — selected tab (graph / side-by-side / json-diff) is saved in the URL and restored on reload.
- Added toast notification UI with animated show/hide for push success/error feedback in diff reviewer.
- Added `Cache-Control: no-store` header to `workflow_diff_server.py` to prevent stale diff responses.
- Fixed diff reviewer layout so the tab bar (Graph diff / Side-by-side / JSON diff) stays pinned and visible when scrolling — replaced brittle `height: calc(...)` with a flex column layout on `body`.

## 2026-03-31

### Summary
- `7 files changed, 127 insertions(+), 36 deletions(-)` (across 4 commits).

### Highlights
- Updated root `README.md` with focused overview, clearer repo layout, and simplified setup steps.
- Added `.factory/` IDE configuration to `.gitignore`.
- Enhanced scheduler with automatic dirty-file recovery (`git reset --hard` + `clean -fd`) and `dirty_files_reset` metric tracking.
- Added cursor-based pagination to `list_workflows()` in `n8n_sync.py` with configurable page limit.
- Added `_rmtree_handle_permission_error()` for Windows readonly attribute cleanup.
- Refactored sync summary printing with per-instance counters and merged totals.

## 2026-03-29

### Summary
- `9 files changed, 147 insertions(+), 27 deletions(-)` (across 2 commits).

### Highlights
- Added `docs/changelog.md` and redacted workflow diff viewer screenshots for documentation.
- Enhanced scheduler PowerShell scripts with `N8nEnvFile` / `SupabaseEnvFile` parameters and configurable `TriggerTimes`.
- Refactored `run_scheduled_sync.ps1` with `Resolve-PythonInvocation` function and comprehensive logging.
- Improved `scheduled_sync.py` file I/O error handling and exception reporting.
- Fixed `n8n_sync.py` path resolution to use `PARENT_SCRIPTS_DIR`.

## 2026-03-27

### Summary
- `5 files changed, 119 insertions(+), 24 deletions(-)` (from current `git diff --stat` on active project files).

### Highlights
- Updated `n8n_extract_sync_2026_03_11/README.md` with `status` usage, example output, local diff viewer instructions, and a redacted screenshot reference.
- Updated `n8n_extract_sync_2026_03_11/scripts/n8n_sync.py` output formatting to show workflow IDs inline and remove hash display from workflow status lines.
- Updated `n8n_extract_sync_2026_03_11/scripts/review_workflow.py` to resolve best-practices references with repo-local and external fallback paths.
- Updated `n8n_extract_sync_2026_03_11/scripts/scheduler/2026_03_27_register_scheduled_sync_task.ps1` to use three explicit daily triggers (8-hour spacing) for broader compatibility.
- Updated `n8n_extract_sync_2026_03_11/REFERENCE.md` with revised guidance for where to keep private workflow review assets.
