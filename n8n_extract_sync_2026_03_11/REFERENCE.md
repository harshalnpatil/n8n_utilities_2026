# n8n Extract Sync — reference

Behavior notes, configuration, and edge cases. **Commands:** [CHEATSHEET.md](CHEATSHEET.md).

## Configuration and environment

Copy `secrets/.env.n8n.example` to `secrets/.env.n8n` and add API keys. Use Python 3.10+.

The scripts read configuration from `secrets/.env.n8n` (dotenv file) and/or environment variables. **Environment variables take precedence** over the dotenv file.

### Required variables per instance

Only the instances you want to sync need to be configured. An instance is enabled when both its base URL and API key are present.

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

### Removing an instance from backup

To stop backing up `secondary` or `tertiary`, remove (or comment out) both of that instance's URL and API key variables in `secrets/.env.n8n`. Also remove any matching `N8N_*` environment variables set in Windows or the scheduled task environment, because environment variables override the dotenv file.

Disabling an instance does **not** delete its existing `workflows/<instance>/` backup or its sync state. Do not delete the local workflow folder merely to disable an endpoint. Local workflow deletion only occurs when an enabled, reachable n8n instance reports that a previously tracked workflow was deleted or archived.

With `--mode backup --instance all`, unreachable configured instances are reported as a partial failure (exit code 3), but reachable instances are still backed up and their sync state is saved. The scheduled runner commits and pushes those successful backups before reporting the partial failure.

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

## Execution logs

Query execution logs from the n8n REST API via the CLI. Requires either `--workflow-id` or `--execution-id`.

| Flag | Default | Description |
|------|---------|-------------|
| `--workflow-id <id>` | — | Filter executions by workflow ID |
| `--execution-id <id>` | — | Fetch a single execution by ID |
| `--status <status>` | all | Filter by status: `error`, `success`, `waiting` |
| `--limit <N>` | 10 | Max executions to return |
| `--include-data` | off | Include full execution data (only with `--execution-id`) |
| `--format <fmt>` | text | Output format: `text` (condensed table) or `json` (raw API response) |

When using `--format text`, the table shows: execution ID, status, workflow name, start time, and duration.

## Activate / Deactivate

Activate or deactivate a workflow on the n8n instance. Requires `--workflow-id`.

These replace the old `curl.exe` one-liners. The API key is read from the same `.env.n8n` file as the rest of the CLI.

| Flag | Default | Description |
|------|---------|-------------|
| `--workflow-id <id>` | — | Workflow ID to activate or deactivate |

The command prints the workflow name, ID, and resulting active status.

## Diff review before push (localhost)

The diff UI loads **before** from the live remote and local `workflow.json` as **after**. Review in side-by-side `n8n-demo` diff mode, then use **Approve & Push** to run the push command (see cheatsheet).

- **Remote-drift guard:** approval is blocked if the remote workflow changed after the diff loaded; reload and review again.
- You can target by `--workflow-id` or `--local-path` to a tracked workflow file (see cheatsheet).

## Playwright (WSL + PowerShell)

- In WSL, browser launch may require Linux system packages; use `sudo npx playwright install-deps chromium` if prompted.
- Optional env overrides for the real-server test: `DIFF_REAL_INSTANCE`, `DIFF_REAL_WORKFLOW_ID`, `DIFF_REAL_PORT` (see cheatsheet).

## Remote-deletion pruning

When a workflow is deleted on the n8n instance, or marked archived by the n8n API (`archived`, `isArchived`, or `archivedAt`), the sync tool treats it as absent from the local mirror and can clean up the local copy:

- **backup** and **sync-two-way:** workflows in local state but missing on the remote are detected. In a real run the local workflow directory and state record are **hard-deleted**; with `--dry-run` the planned deletion is shown (tagged `DELETE`) without mutating files or state.
- **status:** reports stale/deleted-remote records (tagged `STALE`) but never mutates files or state.
- With **`--workflow-id`**, only that workflow is considered for pruning; unrelated workflows are not affected.

## Workflow ID casing

n8n workflow IDs are mixed-case strings (e.g. `AqMMz1UVLUKiblon`), but **local folder names are always lowercased** by `slugify()`. For example, folder `zoho_crm_agent_aqmmz1uvlukiblon` stores workflow ID `AqMMz1UVLUKiblon`. The canonical (original-case) ID is preserved in `state.json` and `metadata.json`.

All CLI lookups (`--workflow-id`) are **case-insensitive** (`aqmmz1uvlukiblon` and `AqMMz1UVLUKiblon` resolve to the same record).

## Codex skills

- **Workflow review skill:** keep the private skill files in `C:\Users\harsh\Documents\n8n_workflows_2026_01_25\skills\n8n-workflow-review\` rather than vendoring them in this repo. Use that external folder for workflow Q&A, comparisons, or improvement suggestions.

## Folders / Unfiled / Move

Folder-management commands backed by `scripts/n8n_folders.py`. The API key is read from the same `.env.n8n` file as the rest of the CLI.

### `folders`

Lists every folder in the instance project. Tries `GET /api/v1/folders` first; if that 404s or errors, falls back to the internal `GET /rest/folders`. The first endpoint that returns a list wins, and the working endpoint is printed (and included in `--format json` as `endpoint`).

| Flag | Default | Description |
|------|---------|-------------|
| `--format <fmt>` | text | `text` (table: ID, Name, ParentFolderId) or `json` (raw payload + endpoint) |

### `unfiled`

Lists workflows whose `folderId` is null/empty, using `list_workflows` from `n8n_common`.

| Flag | Default | Description |
|------|---------|-------------|
| `--format <fmt>` | text | `text` (table: ID, Name) or `json` (raw workflow summaries) |

### `move`

Moves a workflow into a folder. `--folder` resolves by exact folder ID, then case-insensitive exact name, then case-insensitive partial name; an ambiguous partial match errors with the candidate folders (pass the folder ID to disambiguate).

To move, the command GETs the workflow, sets `folderId`, and PUTs the full payload (`name`, `nodes`, `connections`, `settings`, `staticData`, `folderId`) back via the public `PUT /api/v1/workflows/{id}`. If the public PUT rejects `folderId`, it falls back to `PATCH /rest/workflows/{id}` with `{"folderId": "<id>"}`, then `POST /rest/workflows/{id}/move` with the same body. The method that succeeded is printed.

| Flag | Default | Description |
|------|---------|-------------|
| `--workflow-id <id>` | — | Workflow ID to move (required) |
| `--folder <nameOrId>` | — | Target folder ID or name (required) |
| `--dry-run` | off | Print the planned move (source folder / target folder) without calling the API |

### Folders feature availability

Folders are part of n8n projects, which is a license-gated feature. On instances where the feature is disabled (e.g. a community/development build without a projects license):

- `n8n folders` errors because neither `/api/v1/folders` nor `/rest/folders` is registered (both return 404).
- `n8n unfiled` lists every workflow, because the public `/api/v1/workflows` response omits `folderId` entirely when folders are unavailable.
- `n8n move` cannot run because there are no folders to resolve `--folder` against.

The commands still work on instances where the folders feature is enabled.

## Decisions and open questions

_Add reverse-chronological notes here as needed (lightweight decisions, no ceremony)._

- **2026-08-15 — Folder endpoints on the primary instance:** The primary self-hosted instance (n8n 2.33.7, development build) does not expose a folders endpoint. `GET /api/v1/folders` and `GET /rest/folders` both return 404, and `/api/v1/workflows` summaries omit `folderId`. Folders/projects are a license-gated feature on this instance, so the real-move smoke test could not be performed. The `folders`/`unfiled`/`move` commands are unit-tested with mocked HTTP and wired into the CLI; they will work on any instance where the folders feature is enabled.
