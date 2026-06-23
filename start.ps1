$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $scriptDir
. (Join-Path $scriptDir "scripts\load-env.ps1")
$env:UV_CACHE_DIR = Join-Path $scriptDir ".uv-cache"
Write-Host "Starting GPT Proxy on http://127.0.0.1:8000 ..."
uv run uvicorn main:app --host 127.0.0.1 --port 8000
