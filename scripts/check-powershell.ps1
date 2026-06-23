$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$ignoredPathPattern = "[\\/](\.venv|\.uv-cache)[\\/]"

$ignorePatternSelfCheck = @(
    "C:\repo\.venv\Scripts\Activate.ps1",
    "/home/runner/work/repo/.venv/bin/Activate.ps1",
    "C:\repo\.uv-cache\bad.ps1",
    "/home/runner/work/repo/.uv-cache/bad.ps1"
)
foreach ($samplePath in $ignorePatternSelfCheck) {
    if ($samplePath -notmatch $ignoredPathPattern) {
        throw "PowerShell ignore pattern does not match path: $samplePath"
    }
}

$scriptFiles = Get-ChildItem -Path $repoRoot -Recurse -Filter "*.ps1" |
    Where-Object {
        $_.FullName -notmatch $ignoredPathPattern
    }

foreach ($script in $scriptFiles) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($script.FullName, [ref]$tokens, [ref]$parseErrors) | Out-Null
    if ($parseErrors.Count -gt 0) {
        $messages = $parseErrors | ForEach-Object { "$($script.FullName):$($_.Extent.StartLineNumber): $($_.Message)" }
        throw "PowerShell parse errors found:`n$($messages -join "`n")"
    }
}

$tempEnv = New-TemporaryFile
try {
    Set-Content -LiteralPath $tempEnv -Encoding UTF8 -Value @(
        "# ignored comment",
        "GPT_PROXY_DOTENV_TEST=from-file",
        "GPT_PROXY_QUOTED_TEST=`"quoted value`"",
        "GPT_PROXY_EXISTING_TEST=from-file"
    )

    [Environment]::SetEnvironmentVariable("GPT_PROXY_DOTENV_TEST", $null, "Process")
    [Environment]::SetEnvironmentVariable("GPT_PROXY_QUOTED_TEST", $null, "Process")
    [Environment]::SetEnvironmentVariable("GPT_PROXY_EXISTING_TEST", "from-process", "Process")

    . (Join-Path $PSScriptRoot "load-env.ps1") -Path $tempEnv

    if ($env:GPT_PROXY_DOTENV_TEST -ne "from-file") {
        throw "load-env.ps1 did not load a simple key/value entry"
    }
    if ($env:GPT_PROXY_QUOTED_TEST -ne "quoted value") {
        throw "load-env.ps1 did not trim surrounding quotes"
    }
    if ($env:GPT_PROXY_EXISTING_TEST -ne "from-process") {
        throw "load-env.ps1 overwrote an existing process environment variable"
    }

    . (Join-Path $PSScriptRoot "load-env.ps1") -Path $tempEnv -Override
    if ($env:GPT_PROXY_EXISTING_TEST -ne "from-file") {
        throw "load-env.ps1 -Override did not replace an existing process environment variable"
    }
}
finally {
    Remove-Item -LiteralPath $tempEnv -Force -ErrorAction SilentlyContinue
}

Write-Host "PowerShell scripts parsed successfully and load-env.ps1 behavior checks passed."
