<#
 Starts the Channel-Agnostic Agent locally (Slack Socket Mode + Teams/web API on :8000).
 Usage:  .\start.ps1            # server only
         .\start.ps1 -Tunnel    # also open a public HTTPS tunnel for Teams (needs cloudflared)
         .\start.ps1 -Ingest    # (re)index data\docs first
#>
param(
    [switch]$Tunnel,
    [switch]$Ingest,
    [int]$Port = 8000,
    [string]$Venv = "$env:USERPROFILE\rag-venv"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Virtualenv not found at $Venv. Create it: py -3.11 -m venv --without-pip $Venv; py -3.11 -m pip --python $py install -e ." }
if (-not (Test-Path ".env")) { throw ".env missing. Copy .env.example to .env and fill it in." }

if ($Ingest) { & $py -m app.ingest }

$tunnelProc = $null
if ($Tunnel) {
    $cf = Get-Command cloudflared -ErrorAction SilentlyContinue
    if (-not $cf) { throw "cloudflared not found. Install: winget install Cloudflare.cloudflared" }
    $log = Join-Path $env:TEMP "rag-tunnel.log"
    Remove-Item $log -ErrorAction SilentlyContinue
    $tunnelProc = Start-Process cloudflared -ArgumentList "tunnel","--url","http://localhost:$Port" `
        -RedirectStandardError $log -WindowStyle Hidden -PassThru
    $url = $null
    for ($i = 0; $i -lt 30 -and -not $url; $i++) {
        Start-Sleep 1
        if (Test-Path $log) { $m = Select-String -Path $log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1; if ($m) { $url = $m.Matches[0].Value } }
    }
    if (-not $url) { Write-Warning "Tunnel URL not found; see $log" }
    else {
        Write-Host ""
        Write-Host "Teams messaging endpoint (paste into Azure Bot > Configuration):" -ForegroundColor Green
        Write-Host "  $url/channels/teams/messages" -ForegroundColor Green
        Write-Host "(quick-tunnel URLs change every run; update the Azure Bot each time)"
        Write-Host ""
    }
}

try {
    & $py -m uvicorn app.api:app --host 127.0.0.1 --port $Port
} finally {
    if ($tunnelProc -and -not $tunnelProc.HasExited) { Stop-Process -Id $tunnelProc.Id -Force }
}
