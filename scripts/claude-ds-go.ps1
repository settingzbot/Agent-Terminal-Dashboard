# claude-ds-go.ps1 -- DeepSeek-routed Claude Code with ALL permission prompts disabled.
# Combines the DeepSeek API-key/env-var setup from claude-ds.ps1 with the
# --dangerously-skip-permissions flag from claude-go.ps1.
#
# Use this for hands-on ops sessions you are personally orchestrating when you
# want DeepSeek as the model provider.

[CmdletBinding()]
param(
    [string]$Model = "deepseek-v4-pro",
    [string]$SubagentModel = "deepseek-v4-flash",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ClaudeArgs
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path $PSScriptRoot -Parent
Push-Location $repoRoot
try {
    $key = & python -c "from shared.secrets import get_deepseek_key; print(get_deepseek_key() or '')"
} catch {
    Pop-Location
    Write-Host "[claude-ds-go] ERROR: failed to read keyring." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
Pop-Location

if ($null -ne $key) { $key = $key.Trim() }

if ([string]::IsNullOrWhiteSpace($key)) {
    $key = $env:DEEPSEEK_KEY
    if ($key) {
        Write-Host "[claude-ds-go] used DEEPSEEK_KEY env var (keyring unavailable)" -ForegroundColor DarkYellow
    }
}

if ([string]::IsNullOrWhiteSpace($key)) {
    Write-Host "[claude-ds-go] ERROR: DeepSeek key not set." -ForegroundColor Red
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

Write-Host "[claude-ds-go] model=$Model  permission prompts OFF" -ForegroundColor Cyan

& claude --dangerously-skip-permissions @ClaudeArgs
