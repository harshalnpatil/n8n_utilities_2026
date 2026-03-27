# n8n Extract Sync — reference

Behavior notes, configuration, and edge cases. **Commands:** [CHEATSHEET.md](CHEATSHEET.md).

## Configuration and environment

Copy `secrets/.env.n8n.example` to `secrets/.env.n8n` and add API keys. Use Python 3.10+.

The scripts read configuration from `secrets/.env.n8n` (dotenv file) and/or environment variables. **Environment variables take precedence** over the dotenv file.

### Required variables per instance

| Instance | Base URL Variable | API Key Variable |
|----------|------------------|------------------|
| `primary` | `N8N_PRIMARY_BASE_URL` | `N8N_PRIMARY_API_KEY` |
| `secondary` | `N8N_SECONDARY_BASE_URL` | `N8N_SECONDARY_API_KEY` |
| `tertiary` | `N8N_TERTIARY_BASE_URL` | `N8N_TERTIARY_API_KEY` |

### Example `.env.n8n` file

```
N8N_PRIMARY_BASE_URL=https://your-primary.example.com
N8N_PRIMARY_API_KEY=your_api_key_here
N8N_SECONDARY_BASE_URL=https://your-secondary.app.n8n.cloud
N8N_SECONDARY_API_KEY=your_api_key_here
N8N_TERTIARY_BASE_URL=https://your-tertiary.app.n8n.cloud
N8N_TERTIARY_API_KEY=your_api_key_here
```

### PowerShell (inline env)

```powershell
$env:N8N_PRIMARY_BASE_URL = "https://your-primary.example.com"
$env:N8N_PRIMARY_API_KEY = "your_api_key_here"
```

### Windows console encoding

The sync CLI now falls back to ASCII status symbols automatically when Python stdout cannot encode Unicode, which avoids `UnicodeEncodeError` in legacy Windows PowerShell/code page setups.

If you still want UTF-8 symbols on Windows, use one of these before running the scripts:

```powershell
$env:PYTHONUTF8 = "1"
```

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

To force plain ASCII output even on UTF-8 terminals:

```powershell
$env:N8N_SYNC_ASCII = "1"
```

### Dotenv path (`--dotenv`)

Pass `--dotenv` with the path to your `.env.n8n` file (absolute or relative to your project root). For example `--dotenv secrets/.env.n8n`. You can also skip the dotenv file and set environment variables directly.

## Credential migration

Copy credential placeholders from secondary/tertiary instances to the primary instance so workflows can reference them after migration. **Actual credential values** must be filled in manually.

- Only placeholders are created (name + type matching). OAuth credentials need re-authorization in the UI.
- Credentials already on the target (same name + type) are skipped.
- Some credential types (e.g. custom community credentials) may require manual creation if schema is unavailable.

## Diff review before push (localhost)

The diff UI loads **before** from the live remote and local `workflow.json` as **after**. Review in side-by-side `n8n-demo` diff mode, then use **Approve & Push** to run the push command (see cheatsheet).

- **Remote-drift guard:** approval is blocked if the remote workflow changed after the diff loaded; reload and review again.
- You can target by `--workflow-id` or `--local-path` to a tracked workflow file (see cheatsheet).

## Playwright (WSL + PowerShell)

- In WSL, browser launch may require Linux system packages; use `sudo npx playwright install-deps chromium` if prompted.
- Optional env overrides for the real-server test: `DIFF_REAL_INSTANCE`, `DIFF_REAL_WORKFLOW_ID`, `DIFF_REAL_PORT` (see cheatsheet).

## Remote-deletion pruning

When a workflow is deleted on the n8n instance, the sync tool can clean up the local copy:

- **backup** and **sync-two-way:** workflows in local state but missing on the remote are detected. In a real run the local workflow directory and state record are **hard-deleted**; with `--dry-run` the planned deletion is shown (tagged `DELETE`) without mutating files or state.
- **status:** reports stale/deleted-remote records (tagged `STALE`) but never mutates files or state.
- With **`--workflow-id`**, only that workflow is considered for pruning; unrelated workflows are not affected.

## Workflow ID casing

n8n workflow IDs are mixed-case strings (e.g. `AqMMz1UVLUKiblon`), but **local folder names are always lowercased** by `slugify()`. For example, folder `zoho_crm_agent_aqmmz1uvlukiblon` stores workflow ID `AqMMz1UVLUKiblon`. The canonical (original-case) ID is preserved in `state.json` and `metadata.json`.

All CLI lookups (`--workflow-id`) are **case-insensitive** (`aqmmz1uvlukiblon` and `AqMMz1UVLUKiblon` resolve to the same record).

## Codex skills

- **Workflow review skill:** `skills/n8n-workflow-review/` — use for workflow Q&A, comparisons, or improvement suggestions.

## Decisions and open questions

_Add reverse-chronological notes here as needed (lightweight decisions, no ceremony)._
