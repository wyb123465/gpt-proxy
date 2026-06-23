param(
    [string]$Path = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [switch]$Override
)

if (-not (Test-Path -LiteralPath $Path)) {
    return
}

Get-Content -LiteralPath $Path | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) {
        return
    }

    $separatorIndex = $line.IndexOf("=")
    if ($separatorIndex -le 0) {
        return
    }

    $name = $line.Substring(0, $separatorIndex).Trim()
    $value = $line.Substring($separatorIndex + 1).Trim()
    if (-not $name) {
        return
    }

    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    $existing = [Environment]::GetEnvironmentVariable($name, "Process")
    if ($Override -or [string]::IsNullOrEmpty($existing)) {
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}
