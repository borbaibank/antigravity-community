# Zip Antigravity Pro for Gumroad / buyer delivery.
# Usage: .\scripts\package_pro.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ProDir = Join-Path $Root "antigravity_pro"

if (-not (Test-Path $ProDir)) {
    Write-Error "antigravity_pro folder not found at $ProDir"
}

$Date = Get-Date -Format "yyyyMMdd"
$ZipName = "antigravity-pro-$Date.zip"
$DistDir = Join-Path $Root "dist"
if (-not (Test-Path $DistDir)) { New-Item -ItemType Directory -Path $DistDir | Out-Null }
$ZipPath = Join-Path $DistDir $ZipName

if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }

# Zip contents: antigravity_pro/ at archive root (buyer extracts into bot folder)
$Staging = Join-Path $DistDir "_pro_staging"
if (Test-Path $Staging) { Remove-Item $Staging -Recurse -Force }
New-Item -ItemType Directory -Path $Staging | Out-Null
Copy-Item -Path $ProDir -Destination (Join-Path $Staging "antigravity_pro") -Recurse

Compress-Archive -Path (Join-Path $Staging "*") -DestinationPath $ZipPath -CompressionLevel Optimal
Remove-Item $Staging -Recurse -Force

Write-Host ""
Write-Host "Pro package ready: $ZipPath"
Write-Host "Upload to Gumroad as the paid download."
Write-Host "Buyers also need Community: run publish_community.ps1 or link to GitHub."
