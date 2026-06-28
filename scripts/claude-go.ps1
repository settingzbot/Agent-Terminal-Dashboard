# claude-go.ps1 -- terminal-mode launcher with ALL permission prompts disabled.
# Runs `claude --dangerously-skip-permissions` so every tool call is auto-approved
# for THIS SESSION ONLY. Nothing is written to settings.json; the next session
# (or any cron / autonomous run) gets its normal gate back.
#
# Use this for hands-on ops sessions you are personally orchestrating.

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ClaudeArgs
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path $PSScriptRoot -Parent
Push-Location $repoRoot
Pop-Location

Write-Host "[claude-go] WARNING: permission prompts are OFF for this session." -ForegroundColor Yellow

& claude --dangerously-skip-permissions @ClaudeArgs
