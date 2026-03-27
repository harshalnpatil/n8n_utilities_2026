# n8n Extract Sync — command cheatsheet

Run commands from **your project root** (the directory containing `workflows/` and `.n8n_sync/`). In this setup, the sync utilities live in a sibling folder, so commands look like `python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_sync.py ...`. See [REFERENCE.md](REFERENCE.md) for environment variables, `--dotenv`, and behavior notes.

## n8n_sync.py

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_sync.py --mode backup --instance all --dotenv ./secrets/.env.n8n
```

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_sync.py --mode backup --instance all --dry-run --dotenv ./secrets/.env.n8n
```

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_sync.py --mode status --instance all --dotenv ./secrets/.env.n8n
```

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_sync.py --mode push --instance all --dry-run --dotenv ./secrets/.env.n8n
```

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_sync.py --mode sync-two-way --instance all --dry-run --dotenv ./secrets/.env.n8n
```

Primary-only example:

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_sync.py --mode backup --instance primary --dotenv ./secrets/.env.n8n
```

Single-workflow push (after diff approval):

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_sync.py --mode push --instance <instance> --workflow-id <id> --dotenv ./secrets/.env.n8n
```

## n8n_cred_copy.py

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_cred_copy.py --source secondary --target primary --dry-run
```

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_cred_copy.py --source secondary --target primary --output-report-path cred_copy_report.json
```

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_cred_copy.py --source tertiary --target primary --output-report-path cred_copy_tertiary_report.json
```

## workflow_diff_server.py (localhost diff UI)

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\workflow_diff_server.py --instance primary --workflow-id <WORKFLOW_ID>
```

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\workflow_diff_server.py --instance primary --local-path workflows/primary/<slug>/workflow.json
```

Open: `http://127.0.0.1:8765`

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

## review_workflow.py

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\review_workflow.py --workflow workflows/primary/<workflow_slug>/workflow.json --question "What should I improve?"
```

Outputs: `.n8n_sync/review_context.json`, `.n8n_sync/review_report.md`

## Windows Task Scheduler

Register the 8-hour scheduled backup task from Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File <UtilityRoot>\scripts\scheduler\2026_03_27_register_scheduled_sync_task.ps1
```

Machine-specific defaults come from `scripts/scheduler/2026_03_27_scheduled_sync.config.psd1`.
