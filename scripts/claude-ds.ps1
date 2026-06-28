# claude-ds.ps1 -- terminal-mode DeepSeek launcher.
# Reads the DeepSeek API key from the OS keyring, sets ANTHROPIC_* env vars
# in the current scope, then runs `claude @args` interactively in the same
# terminal. Use this when you want to start a Claude Code session pointed at
# DeepSeek without launching a separate VS Code window (e.g. from the
# dashboard Terminal tab or any PowerShell prompt).
#
# Install: runs once via install_hybrid_shortcut.ps1, which adds a
# `function claude-ds { ... }` to your PowerShell profile so this script is
# transparent -- just type `claude-ds` anywhere.

[CmdletBinding()]
param(
    [string]$Model = "deepseek-v4-pro",
    [string]$SubagentModel = "deepseek-v4-flash",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ClaudeArgs
)

$ErrorActionPreference = "Stop"

# Derive the repo root from this script's own location (scripts\..), so the
# launcher works on any host (laptop, PC, server) without a hardcoded path.
$repoRoot = Split-Path $PSScriptRoot -Parent
Push-Location $repoRoot
try {
    $key = & python -c "from shared.secrets import get_deepseek_key; print(get_deepseek_key() or '')"
} catch {
    Pop-Location
    Write-Host "[claude-ds] ERROR: failed to read keyring." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
Pop-Location

if ($null -ne $key) { $key = $key.Trim() }

# Fallback: if the keyring is empty (common over SSH — Windows Credential
# Manager requires an interactive desktop session), try the DEEPSEEK_KEY
# environment variable.
if ([string]::IsNullOrWhiteSpace($key)) {
    $key = $env:DEEPSEEK_KEY
    if ($key) {
        Write-Host "[claude-ds] used DEEPSEEK_KEY env var (keyring unavailable)" -ForegroundColor DarkYellow
    }
}

if ([string]::IsNullOrWhiteSpace($key)) {
    Write-Host "[claude-ds] ERROR: DeepSeek key not set." -ForegroundColor Red
    Write-Host "Set via dashboard Settings tab, or run: set_deepseek_key.py" -ForegroundColor Yellow
    Write-Host "For SSH: setx DEEPSEEK_KEY <key> (machine-level env var)" -ForegroundColor Yellow
    exit 2
}

$env:ANTHROPIC_BASE_URL            = "https://api.deepseek.com/anthropic"
$env:ANTHROPIC_AUTH_TOKEN          = $key
$env:ANTHROPIC_MODEL               = $Model
$env:ANTHROPIC_DEFAULT_OPUS_MODEL  = $Model
$env:ANTHROPIC_DEFAULT_SONNET_MODEL= "deepseek-v4-pro"
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = "deepseek-v4-flash"
$env:CLAUDE_CODE_SUBAGENT_MODEL    = $SubagentModel
$env:CLAUDE_CODE_EFFORT_LEVEL      = "max"

Write-Host "[claude-ds] model=$Model" -ForegroundColor Cyan

# Run claude interactively in the foreground. Passthrough any extra args
# the user typed (e.g. `claude-ds --resume` or `claude-ds -p "hello"`).
& claude @ClaudeArgs
