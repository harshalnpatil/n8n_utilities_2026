# n8n Extract Sync (Python)

Python tooling in this repo to **back up, compare, and push** n8n workflow JSON exports (multi-instance: primary / secondary / tertiary).

## Quick links

- **[CHEATSHEET.md](CHEATSHEET.md)** — copy-paste commands (`n8n_sync.py`, cred copy, diff server, Playwright, review).
- **[REFERENCE.md](REFERENCE.md)** — environment variables, dotenv paths, pruning behavior, workflow ID casing, credential migration limits, troubleshooting notes.
- **[2026_03_27_windows_task_scheduler_setup.md](2026_03_27_windows_task_scheduler_setup.md)** — Windows Task Scheduler setup for isolated mirror sync plus Supabase telemetry.

## Setup

1. Copy `secrets/.env.n8n.example` to `secrets/.env.n8n` and add API keys (see REFERENCE for variable names and examples).
2. Use **Python 3.10+**.
3. Run scripts from **your project root** (the directory containing `workflows/` and `.n8n_sync/`), not from inside this folder.
4. Point `--dotenv` to wherever your `.env.n8n` lives (default: `secrets/.env.n8n` relative to your project root); details in REFERENCE.

## Common command

Back up all workflows from all configured instances:

```bash
python ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_sync.py --mode backup --instance all --dotenv ./secrets/.env.n8n
```

For dry-run, push, status, diff UI, and tests, use the cheatsheet.
