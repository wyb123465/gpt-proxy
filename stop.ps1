$ErrorActionPreference = "SilentlyContinue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$escapedDir = [regex]::Escape($scriptDir)

$procs = Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -match "uvicorn main:app" -and
        $_.CommandLine -match $escapedDir
    }

if (-not $procs) {
    Write-Host "No running GPT Proxy process found."
    exit 0
}

$procs | ForEach-Object {
    Write-Host "Stopping PID $($_.ProcessId) ($($_.Name)) ..."
    Stop-Process -Id $_.ProcessId -Force
}

Write-Host "GPT Proxy stopped."
