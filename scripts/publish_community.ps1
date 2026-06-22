# Build and zip Antigravity Community edition for public release.
# Usage: .\scripts\publish_community.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

Write-Host "Building community edition..."
& $Python scripts/build_community.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Date = Get-Date -Format "yyyyMMdd"
$ZipName = "antigravity-community-$Date.zip"
$ZipPath = Join-Path (Join-Path $Root "dist") $ZipName
$Source = Join-Path $Root "dist\community"

if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }

Compress-Archive -Path (Join-Path $Source "*") -DestinationPath $ZipPath -CompressionLevel Optimal

Write-Host ""
Write-Host "Ready: $ZipPath"
Write-Host "Upload to GitHub release or Gumroad as the free/community download."
