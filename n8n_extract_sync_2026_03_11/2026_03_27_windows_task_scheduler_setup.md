# Windows Task Scheduler setup for scheduled n8n backup

## Files

- `scripts/scheduler/2026_03_27_scheduled_sync.py` runs the isolated mirror backup, commits changes, pushes, saves conflict artifacts, and writes Supabase telemetry.
- `scripts/scheduler/2026_03_27_run_scheduled_sync.ps1` is the Task Scheduler entrypoint.
- `scripts/scheduler/2026_03_27_register_scheduled_sync_task.ps1` creates a repeating 8-hour scheduled task.
- `2026_03_27_n8n_sync_telemetry_schema.sql` is the DDL used to create the telemetry tables.

## Register the task

The registration script now reads machine-specific values from `scripts/scheduler/2026_03_27_scheduled_sync.config.psd1`, including:

- `UtilityRoot = C:\path\to\n8n_utilities_2026\n8n_extract_sync_2026_03_11`
- `MirrorRoot = C:\path\to\n8n_workflows_mirror`
- `ConflictRoot = C:\path\to\n8n_sync_conflicts`
- `TaskName = n8n-workflow-sync`
- `Instance = all`
- `Branch = main`
- `StartTime = 00:00`

Run from any Windows PowerShell prompt:

```powershell
powershell -ExecutionPolicy Bypass -File <UtilityRoot>\scripts\scheduler\2026_03_27_register_scheduled_sync_task.ps1
```

Optional overrides remain available if you need to temporarily bypass the config:

- `-ConfigPath "C:\path\to\2026_03_27_scheduled_sync.config.psd1"`
- `-UtilityRoot "C:\path\to\n8n_utilities_2026\n8n_extract_sync_2026_03_11"`
- `-MirrorRoot "C:\path\to\n8n_workflows_mirror"`
- `-ConflictRoot "C:\path\to\n8n_sync_conflicts"`
- `-TaskName "n8n-workflow-sync"`
- `-Instance "all"`
- `-Branch "main"`
- `-StartTime "00:00"`
- `-WebhookUrl "<url>"`
- `-PythonCommand "py -3"`
- `-GitOriginUrl "https://github.com/harshalnpatil/n8n_workflows.git"`

Manual run example from your workflow repo:

```powershell
cd C:\path\to\n8n_workflows_2026_01_25
py -3 ..\n8n_utilities_2026\n8n_extract_sync_2026_03_11\scripts\n8n_sync.py --mode backup --instance primary --dotenv .\secrets\.env.n8n
```

## Behavior

- The task uses a separate mirror checkout so your active repo is never overwritten.
- If the mirror is dirty before a run, the job stops, stores conflict artifacts, and records telemetry.
- Logs are written under `%LOCALAPPDATA%\n8n_workflow_sync\logs`.
- The register script prints the fully resolved settings before it writes the scheduled task.
- The utilities folder does not need to be a git repo; the mirror clone source is provided via `-GitOriginUrl`.
