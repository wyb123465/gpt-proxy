$ErrorActionPreference = "SilentlyContinue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$escapedDir = [regex]::Escape($scriptDir)

$procs = Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -match "uvicorn" -and
        $_.CommandLine -match "main:app" -and
        $_.CommandLine -match $escapedDir
    }

$portProcs = Get-NetTCPConnection -LocalPort 8000 -State Listen |
    ForEach-Object {
        Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)"
    } |
    Where-Object {
        $_.CommandLine -match "uvicorn" -and
        $_.CommandLine -match "main:app"
    }

$procs = @($procs + $portProcs) |
    Where-Object { $_ -and $_.ProcessId } |
    Sort-Object ProcessId -Unique

if (-not $procs) {
    Write-Host "No running GPT Proxy process found."
    exit 0
}

$procs | ForEach-Object {
    Write-Host "Stopping PID $($_.ProcessId) ($($_.Name)) ..."
    Stop-Process -Id $_.ProcessId -Force
}

Write-Host "GPT Proxy stopped."
