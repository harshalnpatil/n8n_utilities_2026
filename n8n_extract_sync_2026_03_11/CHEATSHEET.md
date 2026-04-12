# n8n Extract Sync — command cheatsheet

Run commands from **your workflows repo** (`n8n_workflows_2026_01_25`). Defaults: `--instance primary --dotenv ./secrets/.env.n8n`. Override any default by passing the flag explicitly. See [REFERENCE.md](REFERENCE.md) for environment variables and behavior notes.

```powershell
# Alias (add to your PowerShell profile for convenience):
# Set-Alias n8n "$HOME\Documents\n8n_utilities_2026\n8n_extract_sync_2026_03_11\n8n.ps1"

..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\n8n.ps1 help          # list all subcommands
..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\n8n.ps1 backup --help # per-command flag reference
```

## Backup

```powershell
.\n8n backup
.\n8n backup --dry-run
```

## Status

```powershell
.\n8n status
```

## Push

```powershell
.\n8n push --dry-run
.\n8n push --workflow-id <id>           # single-workflow push (after diff approval)
```

## Two-way sync

```powershell
.\n8n sync --dry-run
```

## Diff viewer

```powershell
.\n8n diff --workflow-id <WORKFLOW_ID>
.\n8n diff --local-path workflows/primary/<slug>/workflow.json
```

Open: `http://127.0.0.1:8765`

## Review

```powershell
.\n8n review --workflow workflows/primary/<workflow_slug>/workflow.json --question "What should I improve?"
```

Outputs: `.n8n_sync/review_context.json`, `.n8n_sync/review_report.md`

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

## Credentials copy

```powershell
.\n8n creds --source secondary --target primary --dry-run
.\n8n creds --source secondary --target primary --output-report-path cred_copy_report.json
.\n8n creds --source tertiary --target primary --output-report-path cred_copy_tertiary_report.json
```
