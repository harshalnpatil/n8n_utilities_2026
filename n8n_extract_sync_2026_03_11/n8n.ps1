<#
.SYNOPSIS
    CLI wrapper for n8n extract/sync utilities.
.DESCRIPTION
    Thin wrapper that bakes in defaults (--instance primary, --dotenv ./secrets/.env.n8n)
    and shortens invocations. Run  .\n8n help  to see all subcommands.
.EXAMPLE
    .\n8n backup
    .\n8n status
    .\n8n push --dry-run
    .\n8n diff --workflow-id abc123
    .\n8n review workflows/primary/my_wf/workflow.json
#>

param(
    [Parameter(Position = 0)]
    [string]$Command,

    [Parameter(ValueFromRemainingArguments)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'

# ── paths ────────────────────────────────────────────────────────────────
$ScriptsDir = Join-Path $PSScriptRoot 'scripts'
$SyncScript = Join-Path $ScriptsDir 'n8n_sync.py'
$DiffScript = Join-Path $ScriptsDir 'workflow_diff_server.py'
$CredScript = Join-Path $ScriptsDir 'n8n_cred_copy.py'
$ReviewScript = Join-Path $ScriptsDir 'review_workflow.py'

# ── defaults ─────────────────────────────────────────────────────────────
$DefaultInstance = 'primary'
$DefaultDotenv   = './secrets/.env.n8n'

# ── helpers ──────────────────────────────────────────────────────────────
function Has-Flag([string]$flag, [string[]]$args) {
    foreach ($a in $args) { if ($a -eq $flag) { return $true } }
    return $false
}

function Inject-Defaults([string[]]$extra) {
    $out = @()
    $hasInstance = Has-Flag '--instance' $extra
    $hasDotenv   = Has-Flag '--dotenv'   $extra
    if (-not $hasInstance) { $out += '--instance', $DefaultInstance }
    if (-not $hasDotenv)   { $out += '--dotenv',   $DefaultDotenv }
    $out += $extra
    return $out
}

# ── subcommands ──────────────────────────────────────────────────────────
function Show-Help {
    Write-Host ''
    Write-Host '  n8n CLI wrapper — subcommands' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '    backup   [flags]       Pull all workflows from server to local repo'
    Write-Host '    status   [flags]       Show drift between local and server'
    Write-Host '    push     [flags]       Push local changes to server'
    Write-Host '    sync     [flags]       Two-way sync (sync-two-way mode)'
    Write-Host '    diff     [flags]       Launch localhost diff viewer'
    Write-Host '    review   <path> [flags]  Generate review context for a workflow'
    Write-Host '    creds    [flags]       Copy credentials between instances'
    Write-Host '    help                   Show this message'
    Write-Host ''
    Write-Host '  Defaults: --instance primary --dotenv ./secrets/.env.n8n' -ForegroundColor DarkGray
    Write-Host '  Override: .\n8n backup --instance secondary' -ForegroundColor DarkGray
    Write-Host '  Pass-through: any extra flags are forwarded to the underlying script.' -ForegroundColor DarkGray
    Write-Host '  Per-command help: .\n8n backup --help' -ForegroundColor DarkGray
    Write-Host ''
}

switch ($Command) {

    { $_ -in 'backup', 'status', 'push' } {
        $args2 = Inject-Defaults $Rest
        python $SyncScript --mode $Command @args2
    }

    'sync' {
        $args2 = Inject-Defaults $Rest
        python $SyncScript --mode sync-two-way @args2
    }

    'diff' {
        $args2 = Inject-Defaults $Rest
        python $DiffScript @args2
    }

    'review' {
        # First positional arg in $Rest is the workflow path; pass the rest through.
        python $ReviewScript @Rest
    }

    'creds' {
        # Creds script has its own --source/--target; only inject --dotenv.
        $out = @()
        if (-not (Has-Flag '--dotenv' $Rest)) { $out += '--dotenv', $DefaultDotenv }
        $out += $Rest
        python $CredScript @out
    }

    { $_ -in 'help', '--help', '-h', '', $null } {
        Show-Help
    }

    default {
        Write-Host "Unknown command: $Command" -ForegroundColor Red
        Show-Help
        exit 1
    }
}
