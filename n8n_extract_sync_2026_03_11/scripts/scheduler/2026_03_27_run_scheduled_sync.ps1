[CmdletBinding()]
param(
    [string]$UtilityRoot = "",
    [string]$MirrorRoot = "",
    [string]$Instance = "all",
    [string]$Branch = "main",
    [string]$TaskName = "n8n-workflow-sync",
    [string]$WebhookUrl = "",
    [string]$ConflictRoot = "",
    [string]$N8nEnvFile = "",
    [string]$SupabaseEnvFile = "",
    [string]$PythonCommand = "py -3",
    [string]$GitOriginUrl = "https://github.com/harshalnpatil/n8n_workflows.git"
)

$ErrorActionPreference = "Stop"

function Resolve-PythonInvocation {
    param(
        [string]$ConfiguredCommand,
        [string]$LogPath
    )

    $parts = $ConfiguredCommand.Split(" ", [System.StringSplitOptions]::RemoveEmptyEntries)
    if ($parts.Count -lt 1) {
        throw "PythonCommand must not be empty."
    }

    $candidateExe = $parts[0]
    $candidateArgs = @()
    if ($parts.Count -gt 1) {
        $candidateArgs = $parts[1..($parts.Count - 1)]
    }

    $resolvedExe = $candidateExe
    if (Test-Path -LiteralPath $candidateExe) {
        $resolvedExe = (Resolve-Path -LiteralPath $candidateExe).Path
    }
    else {
        $command = Get-Command $candidateExe -ErrorAction SilentlyContinue
        if ($command) {
            $resolvedExe = $command.Source
        }
    }

    $probeArgs = @()
    $probeArgs += $candidateArgs
    $probeArgs += @("-c", "import sys; print(sys.executable)")

    try {
        $probedExe = (& $resolvedExe @probeArgs 2>$null | Select-Object -First 1).Trim()
        if ($probedExe -and (Test-Path -LiteralPath $probedExe)) {
            Add-Content -LiteralPath $LogPath -Value ("Resolved Python interpreter: {0}" -f $probedExe)
            return @{
                Exe  = $probedExe
                Args = @()
            }
        }
    }
    catch {
        Add-Content -LiteralPath $LogPath -Value ("Python probe failed for '{0}': {1}" -f $ConfiguredCommand, $_.Exception.Message)
    }

    if (-not (Get-Command $resolvedExe -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $resolvedExe)) {
        throw "Python command '$ConfiguredCommand' could not be resolved. Configure PythonCommand to a full python.exe path."
    }

    Add-Content -LiteralPath $LogPath -Value ("Using configured Python command without probe resolution: {0}" -f $ConfiguredCommand)
    return @{
        Exe  = $resolvedExe
        Args = $candidateArgs
    }
}

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
Add-Content -LiteralPath $logPath -Value ("Started scheduled sync runner at {0}" -f (Get-Date -Format "s"))

$argList = @(
    $runnerPath,
    "--utility-root", $utilityRoot,
    "--mirror-root", $MirrorRoot,
    "--instance", $Instance,
    "--branch", $Branch,
    "--task-name", $TaskName,
    "--git-origin-url", $GitOriginUrl
)

if ($WebhookUrl) {
    $argList += @("--webhook-url", $WebhookUrl)
}

$resolvedN8nEnvFile = if ($N8nEnvFile) { [System.IO.Path]::GetFullPath($N8nEnvFile) } else { "" }
$resolvedSupabaseEnvFile = if ($SupabaseEnvFile) { [System.IO.Path]::GetFullPath($SupabaseEnvFile) } else { "" }

$argList += @("--conflict-root", $resolvedConflictRoot)

if ($resolvedN8nEnvFile) {
    if (-not (Test-Path -LiteralPath $resolvedN8nEnvFile)) {
        throw "N8nEnvFile does not exist: $resolvedN8nEnvFile"
    }
    $argList += @("--n8n-env-file", $resolvedN8nEnvFile)
}

if ($resolvedSupabaseEnvFile) {
    if (-not (Test-Path -LiteralPath $resolvedSupabaseEnvFile)) {
        throw "SupabaseEnvFile does not exist: $resolvedSupabaseEnvFile"
    }
    $argList += @("--supabase-env-file", $resolvedSupabaseEnvFile)
}

$pythonInvocation = Resolve-PythonInvocation -ConfiguredCommand $PythonCommand -LogPath $logPath
$exe = [string]$pythonInvocation.Exe
$exeArgs = @($pythonInvocation.Args)
$finalArgs = @($exeArgs + $argList)

Push-Location $utilityRoot
try {
    Add-Content -LiteralPath $logPath -Value ("Executing: {0} {1}" -f $exe, ($finalArgs -join " "))
    & $exe @finalArgs *>> $logPath
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
