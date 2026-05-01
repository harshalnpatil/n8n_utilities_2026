[CmdletBinding()]
param(
    [string]$ConfigPath = "",
    [string]$UtilityRoot = "",
    [string]$TaskName = "",
    [string]$MirrorRoot = "",
    [string]$Instance = "",
    [string]$Branch = "",
    [string]$WebhookUrl = "",
    [string]$ConflictRoot = "",
    [string]$N8nEnvFile = "",
    [string]$SupabaseEnvFile = "",
    [string]$PythonCommand = "",
    [string]$GitOriginUrl = "",
    [string]$StartTime = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$defaultConfigPath = Join-Path $scriptDir "2026_03_27_scheduled_sync.config.psd1"
$resolvedConfigPath = if ($ConfigPath) { $ConfigPath } else { $defaultConfigPath }

if (-not (Test-Path -LiteralPath $resolvedConfigPath)) {
    throw "Scheduled sync config not found at $resolvedConfigPath"
}

$config = Import-PowerShellDataFile -Path $resolvedConfigPath

function Resolve-Setting {
    param(
        [string]$ExplicitValue,
        [string]$ConfigKey,
        [switch]$Required
    )

    if ($ExplicitValue) {
        return $ExplicitValue
    }

    $configValue = $config[$ConfigKey]
    if ($configValue) {
        return [string]$configValue
    }

    if ($Required) {
        throw "Missing required config value '$ConfigKey' in $resolvedConfigPath"
    }

    return ""
}

$resolvedUtilityRoot = Resolve-Setting -ExplicitValue $UtilityRoot -ConfigKey "UtilityRoot" -Required
$resolvedTaskName = Resolve-Setting -ExplicitValue $TaskName -ConfigKey "TaskName" -Required
$resolvedMirrorRoot = Resolve-Setting -ExplicitValue $MirrorRoot -ConfigKey "MirrorRoot" -Required
$resolvedInstance = Resolve-Setting -ExplicitValue $Instance -ConfigKey "Instance" -Required
$resolvedBranch = Resolve-Setting -ExplicitValue $Branch -ConfigKey "Branch" -Required
$resolvedWebhookUrl = Resolve-Setting -ExplicitValue $WebhookUrl -ConfigKey "WebhookUrl"
$resolvedConflictRoot = Resolve-Setting -ExplicitValue $ConflictRoot -ConfigKey "ConflictRoot" -Required
$resolvedN8nEnvFile = Resolve-Setting -ExplicitValue $N8nEnvFile -ConfigKey "N8nEnvFile"
$resolvedSupabaseEnvFile = Resolve-Setting -ExplicitValue $SupabaseEnvFile -ConfigKey "SupabaseEnvFile"
$resolvedPythonCommand = Resolve-Setting -ExplicitValue $PythonCommand -ConfigKey "PythonCommand" -Required
$resolvedGitOriginUrl = Resolve-Setting -ExplicitValue $GitOriginUrl -ConfigKey "GitOriginUrl" -Required
$resolvedStartTime = Resolve-Setting -ExplicitValue $StartTime -ConfigKey "StartTime" -Required

if (-not (Test-Path -LiteralPath $resolvedUtilityRoot)) {
    throw "UtilityRoot does not exist: $resolvedUtilityRoot"
}

$runnerPs1 = Join-Path $resolvedUtilityRoot "scripts\scheduler\2026_03_27_run_scheduled_sync.ps1"
if (-not (Test-Path -LiteralPath $runnerPs1)) {
    throw "Runner script not found at $runnerPs1"
}

$mirrorParent = Split-Path -Parent $resolvedMirrorRoot
if (-not $mirrorParent) {
    throw "MirrorRoot must include a parent directory: $resolvedMirrorRoot"
}
if (-not (Test-Path -LiteralPath $mirrorParent)) {
    throw "MirrorRoot parent directory does not exist: $mirrorParent"
}

$conflictParent = Split-Path -Parent $resolvedConflictRoot
if (-not $conflictParent) {
    throw "ConflictRoot must include a parent directory: $resolvedConflictRoot"
}
if (-not (Test-Path -LiteralPath $conflictParent)) {
    throw "ConflictRoot parent directory does not exist: $conflictParent"
}

if (-not $resolvedPythonCommand.Trim()) {
    throw "PythonCommand must not be empty."
}
if ($resolvedN8nEnvFile -and -not (Test-Path -LiteralPath $resolvedN8nEnvFile)) {
    throw "N8nEnvFile does not exist: $resolvedN8nEnvFile"
}
if ($resolvedSupabaseEnvFile -and -not (Test-Path -LiteralPath $resolvedSupabaseEnvFile)) {
    throw "SupabaseEnvFile does not exist: $resolvedSupabaseEnvFile"
}

$taskArgs = @(
    "-NoLogo",
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$runnerPs1`"",
    "-UtilityRoot", "`"$resolvedUtilityRoot`"",
    "-MirrorRoot", "`"$resolvedMirrorRoot`"",
    "-Instance", "`"$resolvedInstance`"",
    "-Branch", "`"$resolvedBranch`"",
    "-TaskName", "`"$resolvedTaskName`"",
    "-PythonCommand", "`"$resolvedPythonCommand`"",
    "-GitOriginUrl", "`"$resolvedGitOriginUrl`"",
    "-ConflictRoot", "`"$resolvedConflictRoot`""
)

if ($resolvedN8nEnvFile) {
    $taskArgs += @("-N8nEnvFile", "`"$resolvedN8nEnvFile`"")
}

if ($resolvedSupabaseEnvFile) {
    $taskArgs += @("-SupabaseEnvFile", "`"$resolvedSupabaseEnvFile`"")
}

if ($resolvedWebhookUrl) {
    $taskArgs += @("-WebhookUrl", "`"$resolvedWebhookUrl`"")
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($taskArgs -join " ")

try {
    $baseStartTime = [datetime]::Parse($resolvedStartTime, [System.Globalization.CultureInfo]::InvariantCulture)
}
catch {
    throw "StartTime must be parseable by PowerShell's datetime parser. Received: $resolvedStartTime"
}

# Use explicit daily triggers instead of task repetition so the script works on
# systems where New-ScheduledTaskRepetitionSettings is unavailable.
$configTriggerTimes = $config["TriggerTimes"]
if ($configTriggerTimes -and $configTriggerTimes.Count -gt 0) {
    $triggerTimes = $configTriggerTimes
} else {
    $triggerTimes = @(
        $baseStartTime.ToString("HH:mm")
        $baseStartTime.AddHours(8).ToString("HH:mm")
        $baseStartTime.AddHours(16).ToString("HH:mm")
    )
}

$triggers = foreach ($triggerTime in $triggerTimes) {
    New-ScheduledTaskTrigger -Daily -At $triggerTime
}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$resolvedSettings = [pscustomobject]@{
    ConfigPath = $resolvedConfigPath
    UtilityRoot = $resolvedUtilityRoot
    MirrorRoot = $resolvedMirrorRoot
    ConflictRoot = $resolvedConflictRoot
    N8nEnvFile = $resolvedN8nEnvFile
    SupabaseEnvFile = $resolvedSupabaseEnvFile
    TaskName = $resolvedTaskName
    Instance = $resolvedInstance
    Branch = $resolvedBranch
    StartTime = $resolvedStartTime
    PythonCommand = $resolvedPythonCommand
    GitOriginUrl = $resolvedGitOriginUrl
    RunnerScript = $runnerPs1
}
Register-ScheduledTask -TaskName $resolvedTaskName -Action $action -Trigger $triggers -Settings $settings -Force | Out-Null
