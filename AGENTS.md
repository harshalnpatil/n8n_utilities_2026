# Cursor Project Guidance: n8n Utilities

## Windows PowerShell path note for AI agents

When an AI agent is running commands in Windows PowerShell, do not use WSL-style drive paths such as `/c/Users/...`; PowerShell treats that as `C:\c\Users\...` and the command fails. Use normal Windows drive paths instead.

Wrong in PowerShell:

```powershell
cat /c/Users/harsh/Documents/building_cursor_2026_02_06/PS_AGENTS.md
```

Right in PowerShell:

```powershell
cat C:/Users/harsh/Documents/building_cursor_2026_02_06/PS_AGENTS.md
```

## Workflow-mirror Git coordination

Manual `n8n backup` and `n8n pull` can write the same workflow-mirror files as the scheduled backup. Before running either command in a workflow-mirror clone, read that clone's README and confirm its `.gitattributes` and `keep-ours` merge driver are configured.

`workflows/**/metadata.json` changes on every sync because `syncedAtUtc` is refreshed. The merge driver preserves the local metadata copy so real `workflow.json` conflicts still require review. Do not resolve metadata conflicts by committing conflict markers, and do not add mirror-specific Git configuration to this utilities repository.

