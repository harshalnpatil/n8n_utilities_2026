# 2026-04-23 — n8n-workflow-sync dual telemetry

## Goal

Repair and verify dual telemetry for `n8n-workflow-sync` so the scheduled sync writes both:

- direct Supabase rows to `n8n_sync_runs` and `n8n_sync_run_conflicts`
- webhook telemetry to the shared n8n flow that lands in `localhost_cron_runs`

## Notes

- The checked-in SQL schema for `n8n_sync_runs` does not have a top-level `dirty_files_reset` column. That value belongs inside the `summary` JSON.
- The webhook payload is meant to stay generic, with sync-specific fields inside `metadata`.
- The scheduled runner loads `.env.n8n` into an overlay env for `n8n_sync.py`, but the webhook resolution path was originally reading `os.environ` directly instead of the parsed `.env.n8n` values.
- The live scheduler config at `n8n_extract_sync_2026_03_11/scripts/scheduler/2026_03_27_scheduled_sync.config.psd1` had `WebhookUrl = ''`, so the task was relying on fallback lookup rather than an explicit `--webhook-url` argument.

## Work log

- Read the scheduler runner, unit tests, schema DDL, scheduler docs, PowerShell entrypoint, and task registration script.
- Confirmed the direct telemetry schema mismatch: `dirty_files_reset` was being sent as a top-level field in the run row even though the DDL only supports it inside `summary`.
- Refactored the Python runner to build `summary`, `run_row`, and webhook payloads through explicit helpers so payload shape is unit-testable.
- Changed HTTP error handling so non-2xx responses raise runtime errors that include status code and response body.
- Split telemetry handling into separate Supabase and webhook warnings so one failure does not mask the other.
- Added unit tests for:
  - `dirty_files_reset` staying in `summary` only for `n8n_sync_runs`
  - `dirty_files_reset` appearing in webhook `metadata`
  - separate warning capture for Supabase and webhook failures
- Investigated the earlier webhook theory by checking real task logs and live config.
- Found that the generic `HTTP 400` seen in historical logs could not have been the webhook path for the scheduled task because the task was not passing a webhook URL and the runner was not reading webhook settings from `.env.n8n`.
- Verified `.env.n8n` does contain `N8N_WEBHOOK_TELEMETRY_URL` and `N8N_WEBHOOK_TELEMETRY_AUTH_TOKEN`.
- Fixed webhook resolution so the runner now uses this precedence:
  1. explicit `--webhook-url`
  2. values from parsed `.env.n8n`
  3. process environment
- Added unit tests for webhook setting resolution order.
- Ran a manual scheduled-sync invocation via the PowerShell runner and then queried Supabase REST directly to verify both telemetry destinations.

## Verification

- `python3 -m unittest n8n_extract_sync_2026_03_11/scripts/test_2026_03_27_scheduled_sync.py`
- `python3 -m py_compile n8n_extract_sync_2026_03_11/scripts/scheduler/2026_03_27_scheduled_sync.py`
- Manual PowerShell run created a clean log at `C:\Users\harsh\AppData\Local\n8n_workflow_sync\logs\2026_04_23_10_20_54.log` with no telemetry warnings.
- Verified a new `n8n_sync_runs` row for `task_name = n8n-workflow-sync` at `2026-04-23T09:20:55Z` with `status = success`.
- Verified a matching `localhost_cron_runs` row for `job_name = n8n-workflow-sync` at `2026-04-23T09:20:55Z` with `status = success`.
- Verified the latest webhook row includes `dirty_files_reset` in `metadata`, confirming the webhook path is now using the updated payload shape.

## Outcome

- Root cause of the earlier `HTTP 400` was the direct Supabase payload mismatch, not the webhook endpoint.
- Root cause of the missing webhook path in scheduled runs was configuration plus lookup behavior:
  - task config left `WebhookUrl` empty
  - runner ignored webhook values present in `.env.n8n`
- After the fixes, both telemetry paths succeeded on a manual production-like run.
