# n8n Utilities 2026

Python + Node tooling to **back up, compare, diff-review, and push** n8n workflow JSON exports across multiple instances (primary / secondary / tertiary).

## What's inside

| Path | Description |
|------|-------------|
| [`n8n_extract_sync_2026_03_11/`](n8n_extract_sync_2026_03_11/) | Core sync scripts, diff UI, credential migration, Playwright tests, and scheduler |
| [`n8n_extract_sync_2026_03_11/CHEATSHEET.md`](n8n_extract_sync_2026_03_11/CHEATSHEET.md) | Copy-paste commands for every tool |
| [`n8n_extract_sync_2026_03_11/REFERENCE.md`](n8n_extract_sync_2026_03_11/REFERENCE.md) | Environment variables, dotenv paths, behavior notes |

## Quick start

1. Copy `n8n_extract_sync_2026_03_11/secrets/.env.n8n.example` → `secrets/.env.n8n` and fill in your API keys.
2. Use **Python 3.10+** and **Node 18+** (for Playwright tests).
3. See the [CHEATSHEET](n8n_extract_sync_2026_03_11/CHEATSHEET.md) for usage.

## License

ISC
