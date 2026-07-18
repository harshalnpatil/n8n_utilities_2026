# n8n Utilities

## Problem

n8n workflows are difficult to review safely when their live instance data, credentials, and execution history should remain private.

## What this toolkit does

These local helper scripts back up workflows from configured n8n instances, compare local and remote JSON, show a local diff view, support credential migration, inspect execution logs, and manage workflow activation.

## Public-safe scope

The repository publishes tooling and redacted visual evidence. It does not publish instance URLs, API keys, workflow exports, credentials, execution data, or production configuration. Copy the supplied environment-file template before using a local instance.

## How it works

1. Configure local credentials in an ignored `.env.n8n` file.
2. Export workflows into a local project root.
3. Check status, inspect a targeted diff, and review the JSON locally.
4. Push only after manual review, or use the utilities to query executions and change activation state.

The [main project README](n8n_extract_sync_2026_03_11/README.md) documents commands, state handling, and local workflows.

## Visual evidence

The diff-viewer image uses redacted example workflow names and content. It contains no live instance URL, credential, or workflow identifier.

![Synthetic local workflow diff viewer](docs/images/synthetic-workflow-diff-demo.png)

*Redacted example of the local diff viewer used to compare an exported workflow with a configured instance.*

## Repository layout

The main tooling lives in `n8n_extract_sync_2026_03_11/`:

- back up workflows from one or more n8n instances
- compare local and remote workflow JSON
- review diffs in a small local UI
- migrate or copy credentials
- query execution logs and activate or deactivate workflows
- run scheduled sync jobs and test helpers

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

- The repository is intentionally small and public-safe.
- Most usage details live in the subproject README and cheatsheet.

## Git hygiene when pairing manual `n8n backup` or `n8n pull` with the scheduled backup

The scheduled backup task (`scripts/scheduler/2026_03_27_register_scheduled_sync_task.ps1`)
runs `n8n backup` on a cadence and pushes a `chore(sync): backup n8n
workflows ...` commit to `main` in the workflows repo. Manual `n8n backup`
or `n8n pull` runs on the laptop and writes the same files. Every backup, scheduled or manual,
rewrites `workflows/**/metadata.json` with a fresh `syncedAtUtc` timestamp,
so when the two push to `main` out of order, a plain `git pull` would
otherwise produce dozens of textual conflicts on `metadata.json` files.
Those conflict markers then corrupt the JSON and crash the next
`n8n status` / `n8n backup` / `n8n pull` with `json.decoder.JSONDecodeError: Expecting
property name enclosed in double quotes`.

**Mitigation lives in the workflows repo, not here.** The
[n8n_workflows_2026_01_25](../n8n_workflows_2026_01_25) repo has a
`.gitattributes` rule that maps `workflows/**/metadata.json` to a
`keep-ours` merge driver. Each clone of that repo needs a one-time:

```powershell
git config merge.keep-ours.driver true
```

After that, `metadata.json` merge conflicts auto-resolve to the local copy
silently, while real `workflow.json` content conflicts still surface for
manual review. See the workflows repo's
[README.md](../n8n_workflows_2026_01_25/README.md) for the full explanation.

## Start here

- [Main project README](n8n_extract_sync_2026_03_11/README.md)
- [Cheatsheet](n8n_extract_sync_2026_03_11/CHEATSHEET.md)
