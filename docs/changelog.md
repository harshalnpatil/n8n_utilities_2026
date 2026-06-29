# Changelog

## 2026-06-29

### Highlights
- Added `n8n pull` as an alias for `n8n backup` in the Python CLI and the PowerShell wrapper.

## 2026-06-13

### Highlights
- Extended the `updatedAt` fast-path (previously `n8n status` only) to `n8n backup`/`sync` and the scheduled backup. Unchanged workflows are now skipped without a per-workflow `get_workflow()` API call when the list summary's `updatedAt` matches the state record and the local hash is unchanged. Previously only `status` benefited, so scheduled `--mode backup` runs still fetched every workflow.
- `n8n sync` now refreshes each record's `updatedAt` after a PULL/PUSH so the fast-path stays effective on subsequent runs.
- `--force-check` now applies to `status`, `backup`, and `sync` (was status-only) to bypass the fast-path and fetch every workflow.

## 2026-06-12

### Highlights
- `n8n diff` now auto-resolves the workflow ID when exactly one locally-changed workflow exists — no need to pass `--workflow-id` or `--local-path`. When multiple locally-changed workflows are found, an interactive numbered menu lets you pick one (falls back to an error listing when stdin is not a TTY).
- `n8n status` fast-path: workflows are marked CLEAN without an API call when the list summary's `updatedAt` matches the state record and the local hash is unchanged. This reduces status from ~11s to ~3s for 71 workflows (70 clean, 1 changed).
- Added `--force-check` flag to `n8n status` to bypass the fast-path and fetch every workflow from the API (useful for auditing or when `updatedAt` may be unreliable).
- Moved `local_workflow_hash()` from `n8n_sync.py` to `n8n_common.py` so it can be shared between the diff server and sync scripts.

## 2026-06-08

### Highlights
- Added `executions`, `activate`, and `deactivate` subcommands to the n8n CLI wrapper (`n8n.ps1`), backed by a new `scripts/n8n_executions.py` Python script.
- `n8n executions` queries execution logs from the n8n REST API with `--workflow-id`, `--execution-id`, `--status`, `--limit`, `--include-data`, and `--format text|json` flags. Text format shows a condensed table with execution ID, status, workflow name, start time, and duration.
- `n8n activate` and `n8n deactivate` activate/deactivate a workflow by `--workflow-id`, replacing the old `curl.exe` one-liners that required manually extracting the API key.
- All three subcommands reuse `n8n_common.py` helpers (`load_config`, `get_instances`, `http_json_request`) and read the API key from the same `.env.n8n` file as the rest of the CLI.
- Updated `CHEATSHEET.md` and `REFERENCE.md` with documentation for the new subcommands.
- Updated `n8n_workflows_2026_01_25/AGENTS.md` to replace curl recipes with CLI references, remove the MCP-based execution log guidance, and add a note that the n8n MCP server is unreliable due to frequent OAuth re-auth.
- Created `n8n_workflows_2026_01_25/workflow_context/workspace_lessons.md` documenting the n8n MCP auth issue.

## 2026-05-04

### Highlights
- Made `backup`, `status`, `push`, and `sync` concise by hiding unchanged workflows by default, with `--verbose` to show the full row list again.
- Fixed `sync-two-way` so remote-only workflows are now imported into the local mirror as `NEW` records instead of being ignored until a separate backup run.
- Added an `Ignore positional differences` checkbox to the JSON diff view in `n8n_extract_sync_2026_03_11/web/diff_review.html`, so `position` changes can be excluded from the structured compare when reviewing workflow nodes.
- Reworked approve/push failures in the diff reviewer into a proper modal instead of a toast, with readable error text, a close button, a back button, and a red `Force push` action for conflict cases.
- Wired the modal force-push action through `workflow_diff_server.py` and `n8n_sync.py` so the backend can retry with `--force` when the user explicitly chooses to override a conflict.

## 2026-04-15

### Highlights
- Fixed scheduled mirror sync divergence in `n8n_extract_sync_2026_03_11/scripts/scheduler/2026_03_27_scheduled_sync.py` by replacing `git pull --ff-only` with mirror-safe alignment (`fetch` + `reset --hard origin/<branch>` + `clean -fd`).
- Added explicit inline note that the scheduler mirror checkout is disposable and should always be forced to remote tip before each sync run.
- Performed one-time recovery on `C:\Users\harsh\Documents\backup_n8n_workflows_mirror`: created safety branch `backup/diverged-mirror-20260415_102520`, then reset mirror `main` to `origin/main` to clear ahead/behind divergence.
- Verified scheduled sync path after recovery: manual run exited successfully for git sync flow (`origin/main...HEAD` returned to `0 0`); only non-fatal telemetry warning remains (`HTTP 400`).

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
