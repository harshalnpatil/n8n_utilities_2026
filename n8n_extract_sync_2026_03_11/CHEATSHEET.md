# n8n Extract Sync — command cheatsheet

Run commands from **your workflows repo** (`n8n_workflows_2026_01_25`). Defaults: `--instance primary --dotenv ./secrets/.env.n8n`. Override any default by passing the flag explicitly. See [REFERENCE.md](REFERENCE.md) for environment variables and behavior notes.

## Setup

### macOS / POSIX

```bash
# Alias (add to your ~/.bashrc, ~/.zshrc, or ~/.bash_profile):
alias n8n="$HOME/Documents/n8n_utilities_2026/n8n_extract_sync_2026_03_11/n8n"
```

### Windows PowerShell

```powershell
# Alias (add to your PowerShell profile for convenience):
# Set-Alias n8n "$HOME\Documents\n8n_utilities_2026\n8n_extract_sync_2026_03_11\n8n.ps1"
```

## Help

```bash
# macOS / POSIX
n8n help                    # list all subcommands
n8n backup --help           # per-command flag reference
```

```powershell
# Windows
..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\n8n.ps1 help
..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\n8n.ps1 backup --help
```

## Backup

```bash
# macOS / POSIX
n8n backup
n8n backup --dry-run
n8n backup --verbose        # show unchanged workflows too
```

```powershell
# Windows
.\n8n backup
.\n8n backup --dry-run
.\n8n backup --verbose
```

## Status

```bash
# macOS / POSIX
n8n status
n8n status --verbose
```

```powershell
# Windows
.\n8n status
.\n8n status --verbose
```

## Push

```bash
# macOS / POSIX
n8n push --dry-run
n8n push --workflow-id <id>                   # single-workflow push (after diff approval)
n8n push --workflow-id <id> --force           # overwrite remote even if it changed since last sync
n8n push --workflow-id <id> --force --dry-run # preview a force push (shows PUSH instead of CONFLICT)
n8n push --verbose                            # show unchanged workflows too
```

```powershell
# Windows
.\n8n push --dry-run
.\n8n push --workflow-id <id>
.\n8n push --workflow-id <id> --force
.\n8n push --workflow-id <id> --force --dry-run
.\n8n push --verbose
```

## Two-way sync

```bash
# macOS / POSIX
n8n sync --dry-run
n8n sync --verbose
```

```powershell
# Windows
.\n8n sync --dry-run
.\n8n sync --verbose
```

## Diff viewer

```bash
# macOS / POSIX
n8n diff --workflow-id <WORKFLOW_ID>
n8n diff --local-path workflows/primary/<slug>/workflow.json
```

```powershell
# Windows
.\n8n diff --workflow-id <WORKFLOW_ID>
.\n8n diff --local-path workflows/primary/<slug>/workflow.json
```

Automatically opens `http://127.0.0.1:8765` in your browser. Use `--no-browser` to disable.

## Prepare workflow JSON after edits

```bash
# macOS / POSIX
n8n prepare --workflow-id <WORKFLOW_ID>
n8n prepare --local-path workflows/primary/<slug>/workflow.json
n8n prepare --workflow-id <WORKFLOW_ID> --check
```

```powershell
# Windows
.\n8n prepare --workflow-id <WORKFLOW_ID>
.\n8n prepare --local-path workflows/primary/<slug>/workflow.json
.\n8n prepare --workflow-id <WORKFLOW_ID> --check
```

Mirrors top-level workflow fields into `activeVersion` and validates JSON before `diff` or `push`.

## Review

```bash
# macOS / POSIX
n8n review workflows/primary/<workflow_slug>/workflow.json --quality-gate
n8n review workflows/primary/<workflow_slug>/workflow.json --quality-gate --changed-only
n8n review workflows/primary/<workflow_slug>/workflow.json --quality-gate --include-executions --workflow-id <id>
```

```powershell
# Windows
.\n8n review workflows/primary/<workflow_slug>/workflow.json --quality-gate
.\n8n review workflows/primary/<workflow_slug>/workflow.json --quality-gate --changed-only
.\n8n review workflows/primary/<workflow_slug>/workflow.json --quality-gate --include-executions --workflow-id <id>
```

Outputs: `.n8n_sync/review_context.json`, `.n8n_sync/review_report.md`

`--quality-gate` returns a nonzero exit code only for error-level findings. Advisory findings stay visible in the report, but they do not block the command.

## Credentials copy

```bash
# macOS / POSIX
n8n creds --source secondary --target primary --dry-run
n8n creds --source secondary --target primary --output-report-path cred_copy_report.json
n8n creds --source tertiary --target primary --output-report-path cred_copy_tertiary_report.json
```

```powershell
# Windows
.\n8n creds --source secondary --target primary --dry-run
.\n8n creds --source secondary --target primary --output-report-path cred_copy_report.json
.\n8n creds --source tertiary --target primary --output-report-path cred_copy_tertiary_report.json
```

## Execution logs

```bash
# macOS / POSIX
n8n executions --workflow-id <id>                          # last 10 executions (text table)
n8n executions --workflow-id <id> --limit 20               # more results
n8n executions --workflow-id <id> --status error           # filter to errors only
n8n executions --workflow-id <id> --format json            # raw JSON output
n8n executions --execution-id <execId>                     # single execution detail
n8n executions --execution-id <execId> --include-data      # include full execution data
```

```powershell
# Windows
.\n8n executions --workflow-id <id>
.\n8n executions --workflow-id <id> --limit 20
.\n8n executions --workflow-id <id> --status error
.\n8n executions --workflow-id <id> --format json
.\n8n executions --execution-id <execId>
.\n8n executions --execution-id <execId> --include-data
```

## Activate / Deactivate

```bash
# macOS / POSIX
n8n activate --workflow-id <id>        # activate a workflow on the n8n instance
n8n deactivate --workflow-id <id>      # deactivate a workflow on the n8n instance
```

```powershell
# Windows
.\n8n activate --workflow-id <id>
.\n8n deactivate --workflow-id <id>
```

These replace the old `curl` one-liners for activate/deactivate. The API key is read from `--dotenv` (default: `./secrets/.env.n8n`).

## Retry / Stop execution

```bash
# macOS / POSIX
n8n retry --execution-id <id>                 # retry with latest saved workflow (loadWorkflow=true, default)
n8n retry --execution-id <id> --dry-run       # preview the POST URL + payload without calling the API
n8n retry --execution-id <id> --no-load-workflow  # retry with the workflow version captured at execution time
n8n stop  --execution-id <id>                 # stop a running execution
```

```powershell
# Windows
.\n8n retry --execution-id <id>
.\n8n retry --execution-id <id> --dry-run
.\n8n retry --execution-id <id> --no-load-workflow
.\n8n stop  --execution-id <id>
```

`retry` replays the ORIGINAL trigger data from the failed execution. With `loadWorkflow=true` (the default), it runs against the CURRENTLY SAVED workflow on the server, not the version captured when the execution first ran. This is the "report error, fix workflow, push, re-run" closed loop:

1. Inspect the failed run: `n8n executions --execution-id <id> --include-data` (POSIX) or `.\n8n executions --execution-id <id> --include-data` (Windows).
2. Fix the workflow JSON locally, then run the validation gates (`prepare`, `diff`, `review`).
3. Get explicit user approval, then `n8n push --workflow-id <wf-id>` so the fix is live on the server.
4. `n8n retry --execution-id <id>` to re-run with the same input data against the fixed workflow.

**Safety:** retry executes real workflow logic with real data and can cause real side effects (messages, CRM writes, etc.). Only retry when the user explicitly asks.

## Playwright (npm scripts)

```bash
npm run pw:install:wsl
```

```bash
npm run pw:install:windows
```

```bash
npm run pw:test:diff
```

```bash
npm run pw:test:diff:real
```

```bash
npm run pw:test:diff:real:api
```

Real-server test with env overrides (Unix-style):

```bash
DIFF_REAL_INSTANCE=primary DIFF_REAL_WORKFLOW_ID=<id> DIFF_REAL_PORT=8765 npm run pw:test:diff:real
```

WSL browser deps (if prompted):

```bash
sudo npx playwright install-deps chromium
```

## Windows Task Scheduler

Register the 8-hour scheduled backup task from Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File <UtilityRoot>\scripts\scheduler\2026_03_27_register_scheduled_sync_task.ps1
```

Machine-specific defaults come from `scripts/scheduler/2026_03_27_scheduled_sync.config.psd1`.
