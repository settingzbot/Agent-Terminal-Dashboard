<#
    launch_dashboard.ps1 — one-click launcher for the Agent Terminal Dashboard.

    Starts the FastAPI server (which lazy-spawns the PTY + Agent daemons on first
    request) and opens the dashboard in the default browser once it is healthy.
    Close this window to stop the server.
#>

$ErrorActionPreference = 'Stop'

# Repo root = parent of this scripts/ folder
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# Prefer the project virtualenv; fall back to whatever python is on PATH
$venvPy = Join-Path $repo '.venv\Scripts\python.exe'
$py = if (Test-Path $venvPy) { $venvPy } else { 'python' }

# Build the frontend if the production bundle is missing
$indexHtml = Join-Path $repo 'web\dist\index.html'
if (-not (Test-Path $indexHtml)) {
    Write-Host 'web/dist not found — building frontend...' -ForegroundColor Yellow
    Push-Location (Join-Path $repo 'web')
    try {
        if (-not (Test-Path 'node_modules')) { npm install }
        npm run build
    } finally {
        Pop-Location
    }
}

$host_  = '127.0.0.1'
$port   = 8080
$url    = "http://localhost:$port"
$health = "http://${host_}:${port}/api/health"

# Open the browser once the server answers /api/health (runs in the background
# while uvicorn holds the foreground of this window)
Start-Job -ArgumentList $url, $health -ScriptBlock {
    param($url, $health)
    for ($i = 0; $i -lt 60; $i++) {
        try {
            if ((Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) {
                Start-Process $url
                return
            }
        } catch { Start-Sleep -Milliseconds 500 }
    }
} | Out-Null

Write-Host "Starting Agent Terminal Dashboard on $url" -ForegroundColor Cyan
Write-Host 'Close this window to stop the server.' -ForegroundColor DarkGray
Write-Host ''

# Run uvicorn in a relaunch loop. The in-app Settings -> "Restart Dashboard"
# button exits the server with code 42 (RESTART_EXIT_CODE in server/app.py); we
# catch that and relaunch in THIS same window so the logs and "close to stop"
# affordance stay put — letting a rebuilt frontend / edited backend take effect
# without leaving the terminal. Any other exit code (Ctrl-C, window close, a
# real crash) falls through and ends the loop.
while ($true) {
    & $py -m uvicorn server.app:create_app --factory --host $host_ --port $port
    if ($LASTEXITCODE -ne 42) { break }
    Write-Host ''
    Write-Host 'Restarting dashboard (picking up rebuild)...' -ForegroundColor Cyan
    Write-Host ''
}
