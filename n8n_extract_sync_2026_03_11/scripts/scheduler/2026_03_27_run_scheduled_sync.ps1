[CmdletBinding()]
param(
    [string]$UtilityRoot = "",
    [string]$MirrorRoot = "",
    [string]$Instance = "all",
    [string]$Branch = "main",
    [string]$TaskName = "n8n-workflow-sync",
    [string]$WebhookUrl = "",
    [string]$ConflictRoot = "",
    [string]$PythonCommand = "py -3",
    [string]$GitOriginUrl = "https://github.com/harshalnpatil/n8n_workflows.git"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$resolvedUtilityRoot = if ($UtilityRoot) { $UtilityRoot } else { Split-Path -Parent $scriptDir }
$utilityRoot = [System.IO.Path]::GetFullPath($resolvedUtilityRoot)
$runnerPath = Join-Path $scriptDir "2026_03_27_scheduled_sync.py"

if (-not (Test-Path -LiteralPath $utilityRoot)) {
    throw "UtilityRoot does not exist: $utilityRoot"
}
if (-not (Test-Path -LiteralPath $runnerPath)) {
    throw "Scheduled sync runner not found at $runnerPath"
}
if (-not $MirrorRoot) {
    throw "MirrorRoot must not be empty."
}

$mirrorParent = Split-Path -Parent $MirrorRoot
if (-not $mirrorParent) {
    throw "MirrorRoot must include a parent directory: $MirrorRoot"
}
if (-not (Test-Path -LiteralPath $mirrorParent)) {
    throw "MirrorRoot parent directory does not exist: $mirrorParent"
}

$resolvedConflictRoot = if ($ConflictRoot) { $ConflictRoot } else { Join-Path $mirrorParent "n8n_sync_conflicts" }
$conflictParent = Split-Path -Parent $resolvedConflictRoot
if (-not $conflictParent) {
    throw "ConflictRoot must include a parent directory: $resolvedConflictRoot"
}
if (-not (Test-Path -LiteralPath $conflictParent)) {
    throw "ConflictRoot parent directory does not exist: $conflictParent"
}
if (-not $PythonCommand.Trim()) {
    throw "PythonCommand must not be empty."
}

$logRoot = Join-Path $env:LOCALAPPDATA "n8n_workflow_sync\logs"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$timestamp = Get-Date -Format "yyyy_MM_dd_HH_mm_ss"
$logPath = Join-Path $logRoot "$timestamp.log"

$argList = @(
    "`"$runnerPath`"",
    "--utility-root", "`"$utilityRoot`"",
    "--mirror-root", "`"$MirrorRoot`"",
    "--instance", "`"$Instance`"",
    "--branch", "`"$Branch`"",
    "--task-name", "`"$TaskName`"",
    "--git-origin-url", "`"$GitOriginUrl`""
)

if ($WebhookUrl) {
    $argList += @("--webhook-url", "`"$WebhookUrl`"")
}

$argList += @("--conflict-root", "`"$resolvedConflictRoot`"")

$pythonParts = $PythonCommand.Split(" ", [System.StringSplitOptions]::RemoveEmptyEntries)
if ($pythonParts.Count -lt 1) {
    throw "PythonCommand must not be empty."
}

$exe = $pythonParts[0]
$exeArgs = @()
if ($pythonParts.Count -gt 1) {
    $exeArgs = $pythonParts[1..($pythonParts.Count - 1)]
}

$finalArgs = @()
$finalArgs += $exeArgs
$finalArgs += $argList

Push-Location $utilityRoot
try {
    & $exe @finalArgs *>> $logPath
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
