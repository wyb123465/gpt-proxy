$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $scriptDir

. (Join-Path $scriptDir "scripts\load-env.ps1")
$env:UV_CACHE_DIR = Join-Path $scriptDir ".uv-cache"
$url = "http://127.0.0.1:8000/"

$running = Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -match "uvicorn main:app" -and
        $_.CommandLine -match "127\.0\.0\.1" -and
        $_.CommandLine -match "8000"
    }

if (-not $running) {
    Start-Process -FilePath "uv" `
        -ArgumentList "run uvicorn main:app --host 127.0.0.1 --port 8000" `
        -WorkingDirectory $scriptDir `
        -WindowStyle Hidden
}

for ($i = 0; $i -lt 20; $i++) {
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
        Start-Process $url
        Write-Host "GPT Proxy is running at $url"
        exit 0
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

Write-Host "GPT Proxy may still be starting. Open $url manually in a few seconds."
