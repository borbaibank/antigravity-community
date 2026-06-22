# Create/update Antigravity Pro on Gumroad via API.
# Requires GUMROAD_ACCESS_TOKEN in .env or environment (Settings -> Advanced on Gumroad).
#
# Usage:
#   .\scripts\publish_gumroad.ps1
#   .\scripts\publish_gumroad.ps1 -ZipPath dist\antigravity-pro-20260622.zip

param(
    [string]$ZipPath = "",
    [string]$Slug = "binance-multistrategy",
    [string]$ProductName = "Antigravity Pro - Tab11-Tab18 Strategy Pack",
    [int]$PriceCents = 7900,
    [string]$AccessToken = $env:GUMROAD_ACCESS_TOKEN
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$EnvFile = Join-Path $Root ".env"

if (-not $AccessToken -and (Test-Path $EnvFile)) {
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match '^\s*GUMROAD_ACCESS_TOKEN\s*=\s*(.+)\s*$') {
            $AccessToken = $matches[1].Trim().Trim('"').Trim("'")
            break
        }
    }
}

if (-not $AccessToken) {
    Write-Error @"
GUMROAD_ACCESS_TOKEN not set.

1. Open https://app.gumroad.com/settings/advanced
2. Create an access token (edit_products scope)
3. Add to .env:  GUMROAD_ACCESS_TOKEN=your_token_here
4. Re-run: .\scripts\publish_gumroad.ps1
"@
}

function Invoke-Gumroad {
    param(
        [string]$Method,
        [string]$Path,
        [hashtable]$Form = @{}
    )
    $uri = "https://api.gumroad.com/v2$Path"
    if ($Method -eq "Get") {
        $qs = "access_token=$([uri]::EscapeDataString($AccessToken))"
        foreach ($k in $Form.Keys) {
            $qs += "&$([uri]::EscapeDataString($k))=$([uri]::EscapeDataString([string]$Form[$k]))"
        }
        $resp = Invoke-RestMethod -Uri "$uri`?$qs" -Method Get
    } else {
        $body = @{ access_token = $AccessToken }
        foreach ($k in $Form.Keys) { $body[$k] = $Form[$k] }
        $resp = Invoke-RestMethod -Uri $uri -Method $Method -Body $body
    }
    if (-not $resp.success) {
        $msg = if ($resp.message) { $resp.message } else { ($resp | ConvertTo-Json -Compress) }
        throw "Gumroad API error on $Path : $msg"
    }
    return $resp
}

function Upload-GumroadFile {
    param([string]$FilePath)
    $file = Get-Item $FilePath
    $presign = Invoke-Gumroad -Method Post -Path "/files/presign" -Form @{
        filename  = $file.Name
        file_size = $file.Length
    }
    $part = $presign.parts[0]
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $put = Invoke-WebRequest -Uri $part.presigned_url -Method Put -Body $bytes -UseBasicParsing
    $etag = $put.Headers.ETag
    if ($etag -match '^"(.*)"$') { $etag = $matches[1] }
    $complete = Invoke-Gumroad -Method Post -Path "/files/complete" -Form @{
        upload_id = $presign.upload_id
        key       = $presign.key
        "parts[][part_number]" = "1"
        "parts[][etag]"        = $etag
    }
    return @{
        url          = $presign.file_url
        display_name = $file.Name
    }
}

if (-not $ZipPath) {
    $latest = Get-ChildItem (Join-Path $Root "dist\antigravity-pro-*.zip") -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest) {
        $ZipPath = $latest.FullName
    } else {
        Write-Host "Packaging Pro zip..."
        & (Join-Path $Root "scripts\package_pro.ps1")
        $latest = Get-ChildItem (Join-Path $Root "dist\antigravity-pro-*.zip") | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        $ZipPath = $latest.FullName
    }
}
if (-not (Test-Path $ZipPath)) { Write-Error "Zip not found: $ZipPath" }

Write-Host "Gumroad user check..."
$user = Invoke-Gumroad -Method Get -Path "/user"
Write-Host "  Seller: $($user.user.name) ($($user.user.url))"

$description = @"
<p><strong>Antigravity Pro</strong> is the paid add-on for <a href="https://github.com/borbaibank/antigravity-community">Antigravity Community</a> (free).</p>
<p>Unlocks <strong>8 strategies (Tab11–Tab18)</strong> — volume / momentum on Binance USDT-M Futures.</p>
<ul>
<li>Tab11 — Volume Pressure Proxy (1H)</li>
<li>Tab12 — Volume Spike Breakout (1H)</li>
<li>Tab13–16 — Tab9–12 logic on 4H</li>
<li>Tab17 — Momentum Vol Pressure (1H, top 50 universe)</li>
<li>Tab18 — Vol ultimate (1H)</li>
</ul>
<p>Self-hosted Python source. Install with <code>pip install -e ./antigravity_pro</code> on top of Community.</p>
<p><strong>Requires:</strong> <a href="https://github.com/borbaibank/antigravity-community">Community edition</a>, Python 3.11+, Binance USDT-M Futures.</p>
<p><em>No profit guarantee. Test on paper/testnet before live.</em></p>
"@

$receipt = @"
Thank you for buying Antigravity Pro!

1. Install Antigravity Community (free):
   https://github.com/borbaibank/antigravity-community

2. Extract the zip and place the antigravity_pro folder next to server.py

3. Run:
   pip install -e ./antigravity_pro
   python server.py

See INSTALL.md inside the zip.
Support: https://github.com/borbaibank/antigravity-community/issues
"@

Write-Host "Uploading $($ZipPath | Split-Path -Leaf)..."
$fileMeta = Upload-GumroadFile -FilePath $ZipPath

$existingId = $null
$list = Invoke-Gumroad -Method Get -Path "/products"
foreach ($p in $list.products) {
    if ($p.custom_permalink -eq $Slug) {
        $existingId = $p.id
        break
    }
}

$form = @{
    name              = $ProductName
    description       = $description
    custom_permalink  = $Slug
    price             = $PriceCents
    native_type       = "digital"
    custom_receipt    = $receipt
    "files[][url]"          = $fileMeta.url
    "files[][display_name]" = $fileMeta.display_name
}

if ($existingId) {
    Write-Host "Updating product $existingId (slug: $Slug)..."
    $result = Invoke-Gumroad -Method Put -Path "/products/$existingId" -Form $form
    $productId = $existingId
} else {
    Write-Host "Creating product (slug: $Slug)..."
    $result = Invoke-Gumroad -Method Post -Path "/products" -Form $form
    $productId = $result.product.id
}

if (-not $result.product.published) {
    Write-Host "Publishing..."
    $result = Invoke-Gumroad -Method Put -Path "/products/$productId/enable"
}

$short = $result.product.short_url
if (-not $short) { $short = "https://tkbanker.gumroad.com/l/$Slug" }
Write-Host ""
Write-Host "Done: $short"
Write-Host "Profile: https://tkbanker.gumroad.com"
