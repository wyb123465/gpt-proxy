$ErrorActionPreference = "SilentlyContinue"
$procs = Get-Process -Name uvicorn,uv,python -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "main:app" -or $_.CommandLine -match "uvicorn" }
if ($procs) {
    $procs | ForEach-Object {
        Write-Host "Stopping PID $($_.Id) ($($_.ProcessName)) ..."
        Stop-Process $_ -Force
    }
    Write-Host "GPT Proxy stopped."
} else {
    Write-Host "No running GPT Proxy process found."
}
