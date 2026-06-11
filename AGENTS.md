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

