param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$Token = "",
    [switch]$IncludeUpstream
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$loadEnvPath = Join-Path $PSScriptRoot "load-env.ps1"
if (Test-Path -LiteralPath $loadEnvPath) {
    . $loadEnvPath
}

if ([string]::IsNullOrWhiteSpace($Token)) {
    $Token = $env:GPT_PROXY_ACCESS_TOKEN
}

$BaseUrl = $BaseUrl.TrimEnd("/")
$headers = @{}
if (-not [string]::IsNullOrWhiteSpace($Token)) {
    $headers["Authorization"] = "Bearer $Token"
}

function Get-SmokeStatusCode {
    param([object]$ErrorRecord)

    if ($null -eq $ErrorRecord.Exception.Response) {
        return $null
    }

    try {
        return [int]$ErrorRecord.Exception.Response.StatusCode
    }
    catch {
        return $null
    }
}

function Invoke-SmokeGet {
    param(
        [string]$Name,
        [string]$Path,
        [switch]$Authenticated
    )

    $uri = "$BaseUrl$Path"
    $parameters = @{
        Method = "GET"
        Uri = $uri
        TimeoutSec = 20
    }

    if ($Authenticated -and $headers.Count -gt 0) {
        $parameters["Headers"] = $headers
    }

    try {
        $response = Invoke-RestMethod @parameters
        Write-Host "[ok] $Name"
        return $response
    }
    catch {
        $statusCode = Get-SmokeStatusCode $_
        if ($Authenticated -and $statusCode -eq 401 -and [string]::IsNullOrWhiteSpace($Token)) {
            throw "$Name returned 401. Pass -Token or set GPT_PROXY_ACCESS_TOKEN in .env/current shell."
        }

        if ($null -ne $statusCode) {
            throw "$Name failed with HTTP $statusCode at $uri. $($_.Exception.Message)"
        }
        throw "$Name failed at $uri. $($_.Exception.Message)"
    }
}

function Invoke-SmokeWeb {
    param(
        [string]$Name,
        [string]$Path
    )

    $uri = "$BaseUrl$Path"
    $parameters = @{
        Method = "GET"
        Uri = $uri
        TimeoutSec = 20
        UseBasicParsing = $true
    }

    try {
        $response = Invoke-WebRequest @parameters
        if ([int]$response.StatusCode -lt 200 -or [int]$response.StatusCode -ge 300) {
            throw "HTTP $($response.StatusCode)"
        }
        Write-Host "[ok] $Name"
        return [string]$response.Content
    }
    catch {
        $statusCode = Get-SmokeStatusCode $_
        if ($null -ne $statusCode) {
            throw "$Name failed with HTTP $statusCode at $uri. $($_.Exception.Message)"
        }
        throw "$Name failed at $uri. $($_.Exception.Message)"
    }
}

function Assert-SmokeProperty {
    param(
        [string]$Name,
        [object]$Value,
        [string]$Property
    )

    if ($null -eq $Value -or -not ($Value.PSObject.Properties.Name -contains $Property)) {
        throw "$Name response is missing '$Property'."
    }
}

function Assert-SmokeContains {
    param(
        [string]$Name,
        [string]$Content,
        [string]$Expected
    )

    if ($Content.IndexOf($Expected, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "$Name response does not contain '$Expected'."
    }
}

Set-Location $repoRoot

Write-Host "Smoke checking $BaseUrl ..."

$dashboard = Invoke-SmokeWeb -Name "dashboard page" -Path "/"
Assert-SmokeContains -Name "dashboard page" -Content $dashboard -Expected "GPT Proxy Console"
Assert-SmokeContains -Name "dashboard page" -Content $dashboard -Expected "/static/app.js"
Assert-SmokeContains -Name "dashboard page" -Content $dashboard -Expected "/static/styles.css"

$appJs = Invoke-SmokeWeb -Name "dashboard JavaScript" -Path "/static/app.js"
Assert-SmokeContains -Name "dashboard JavaScript" -Content $appJs -Expected "providerList"

$styles = Invoke-SmokeWeb -Name "dashboard stylesheet" -Path "/static/styles.css"
Assert-SmokeContains -Name "dashboard stylesheet" -Content $styles -Expected ":root"

$health = Invoke-SmokeGet -Name "health" -Path "/health"
if ($health.status -ne "ok") {
    throw "health returned unexpected status '$($health.status)'."
}

$detailedHealth = Invoke-SmokeGet -Name "detailed health" -Path "/health/detailed"
Assert-SmokeProperty -Name "detailed health" -Value $detailedHealth -Property "protocols"
Assert-SmokeProperty -Name "detailed health" -Value $detailedHealth -Property "endpoints"

$config = Invoke-SmokeGet -Name "dashboard config" -Path "/api/config" -Authenticated
Assert-SmokeProperty -Name "dashboard config" -Value $config -Property "providers"
Assert-SmokeProperty -Name "dashboard config" -Value $config -Property "security"

$routingPreview = Invoke-SmokeGet -Name "routing preview" -Path "/api/routing/preview?target=chat" -Authenticated
Assert-SmokeProperty -Name "routing preview" -Value $routingPreview -Property "status"
Assert-SmokeProperty -Name "routing preview" -Value $routingPreview -Property "candidates"

if ($routingPreview.status -ne "ready" -and $routingPreview.status -ne "empty") {
    throw "routing preview returned unexpected status '$($routingPreview.status)'."
}

if ($IncludeUpstream) {
    $models = Invoke-SmokeGet -Name "v1 models" -Path "/v1/models" -Authenticated
    if ($models.object -ne "list") {
        throw "v1 models returned unexpected object '$($models.object)'."
    }

    $coverage = Invoke-SmokeGet -Name "model coverage" -Path "/api/model-coverage" -Authenticated
    Assert-SmokeProperty -Name "model coverage" -Value $coverage -Property "models"
    Assert-SmokeProperty -Name "model coverage" -Value $coverage -Property "providers"
}
else {
    Write-Host "[skip] upstream model fetches; pass -IncludeUpstream to check /v1/models and /api/model-coverage."
}

Write-Host "Smoke check passed."
