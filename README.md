# n8n Utilities

Small private repo for n8n helper scripts and supporting docs.

The main thing in here is tooling to:

- back up workflows from one or more n8n instances
- compare local vs remote workflow JSON
- review diffs in a small local UI
- migrate/copy credentials
- run scheduled sync jobs and a few test helpers

## Repo layout

- `n8n_extract_sync_2026_03_11/` - main project folder with the actual scripts
- `n8n_extract_sync_2026_03_11/CHEATSHEET.md` - quick commands
- `n8n_extract_sync_2026_03_11/REFERENCE.md` - env vars and behavior notes
- `docs/changelog.md` - lightweight repo notes

## Basic setup

1. Use Python 3.10+.
2. Use Node 18+ if you want to run the Playwright-based diff tests.
3. Copy `n8n_extract_sync_2026_03_11/secrets/.env.n8n.example` to your local `.env.n8n` file and fill in the n8n credentials.

## Notes

- This is a personal/private utility repo, so the structure is intentionally simple.
- Most real usage details live in the subproject README and cheatsheet.

## Start here

- [Main project README](n8n_extract_sync_2026_03_11/README.md)
- [Cheatsheet](n8n_extract_sync_2026_03_11/CHEATSHEET.md)
