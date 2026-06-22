# Quick Tunnel — expose Antigravity dashboard via trycloudflare.com
# No Cloudflare account or domain required.
# Keep this window open; closing it stops the public URL.
#
# Usage (2 terminals):
#   1) cd repo && .\.venv\Scripts\python.exe server.py
#   2) .\tools\start-quick-tunnel.ps1
#
# URL appears in green in this window and is saved to:
#   tools\quick-tunnel-url.txt   (latest URL)
#   tools\quick-tunnel.log       (full cloudflared log + URL lines)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$cloudflared = Join-Path $PSScriptRoot "cloudflared.exe"

function Get-DashboardPort {
    param([string]$RepoRoot)
    $defaultPort = 8765
    $envFile = Join-Path $RepoRoot ".env"
    if (-not (Test-Path $envFile)) { return $defaultPort }
    $line = Get-Content $envFile -ErrorAction SilentlyContinue |
        Where-Object { $_ -match '^\s*DASHBOARD_PORT\s*=\s*(\d+)\s*(#.*)?$' } |
        Select-Object -First 1
    if ($line -and $line -match 'DASHBOARD_PORT\s*=\s*(\d+)') {
        return [int]$Matches[1]
    }
    return $defaultPort
}

if (-not (Test-Path $cloudflared)) {
    Write-Host "Downloading cloudflared..."
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    Invoke-WebRequest -Uri $url -OutFile $cloudflared -UseBasicParsing
}

function Test-PortListening([int]$Port) {
    $match = netstat -ano | Select-String ":$Port\s+.*LISTENING"
    return [bool]$match
}

$dashboardPort = Get-DashboardPort -RepoRoot $root
$localUrl = "http://127.0.0.1:$dashboardPort"

if (-not (Test-PortListening $dashboardPort)) {
    Write-Host ""
    Write-Host "Bot is not listening on port $dashboardPort." -ForegroundColor Yellow
    Write-Host "Start it first in another terminal:"
    Write-Host "  cd $root"
    Write-Host "  .\.venv\Scripts\python.exe server.py"
    Write-Host ""
    Write-Host "Tip: DASHBOARD_PORT in .env (default 8765)." -ForegroundColor DarkGray
    Write-Host ""
    exit 1
}

$urlFile = Join-Path $PSScriptRoot "quick-tunnel-url.txt"
$logFile = Join-Path $PSScriptRoot "quick-tunnel.log"
$startedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss") + " UTC"
Add-Content -Path $logFile -Value "=== Quick Tunnel started $startedAt -> $localUrl ===" -Encoding utf8

Write-Host ""
Write-Host "Starting Quick Tunnel -> $localUrl"
Write-Host "Log file: $logFile"
Write-Host "Press Ctrl+C to stop."
Write-Host ""

$urlPrinted = $false
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $cloudflared tunnel --url $localUrl 2>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { $_.ToString() }
        $loggedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss") + " UTC"
        Add-Content -Path $logFile -Value "$loggedAt  $line" -Encoding utf8
        Write-Host $line

        if ($line -match '(https://[a-z0-9-]+\.trycloudflare\.com)') {
            $tunnelUrl = $Matches[1]
            $entry = "$loggedAt  $tunnelUrl"
            Set-Content -Path $urlFile -Value $entry -Encoding utf8
            Add-Content -Path $logFile -Value ">>> PUBLIC URL: $tunnelUrl" -Encoding utf8

            if (-not $urlPrinted) {
                Write-Host ""
                Write-Host "========================================" -ForegroundColor Green
                Write-Host "  Quick Tunnel URL (public dashboard)" -ForegroundColor Green
                Write-Host "  $tunnelUrl" -ForegroundColor Cyan
                Write-Host "========================================" -ForegroundColor Green
                Write-Host "Saved: $urlFile" -ForegroundColor DarkGray
                Write-Host ""
                $urlPrinted = $true
            }
        }
    }
} finally {
    $ErrorActionPreference = $prevEap
}
