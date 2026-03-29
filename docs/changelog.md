# Changelog

## 2026-03-27

### Summary
- `5 files changed, 119 insertions(+), 24 deletions(-)` (from current `git diff --stat` on active project files).

### Highlights
- Updated `n8n_extract_sync_2026_03_11/README.md` with `status` usage, example output, local diff viewer instructions, and a redacted screenshot reference.
- Updated `n8n_extract_sync_2026_03_11/scripts/n8n_sync.py` output formatting to show workflow IDs inline and remove hash display from workflow status lines.
- Updated `n8n_extract_sync_2026_03_11/scripts/review_workflow.py` to resolve best-practices references with repo-local and external fallback paths.
- Updated `n8n_extract_sync_2026_03_11/scripts/scheduler/2026_03_27_register_scheduled_sync_task.ps1` to use three explicit daily triggers (8-hour spacing) for broader compatibility.
- Updated `n8n_extract_sync_2026_03_11/REFERENCE.md` with revised guidance for where to keep private workflow review assets.
