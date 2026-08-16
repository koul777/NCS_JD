[CmdletBinding()]
param(
    [string]$Executable = "release\NCS_JD\NCS_JD.exe",
    [ValidateRange(1, 65535)]
    [int]$Port = 18080,
    [ValidateRange(1, 65535)]
    [int]$McpPort = 8766,
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$exePath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Executable))
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "Executable not found: $exePath"
}

$existingProcessIds = @(
    Get-Process -Name NCS_JD -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $exePath } |
        ForEach-Object { $_.Id }
)

& $exePath --diagnostics
if ($LASTEXITCODE -ne 0) { throw "Frozen diagnostics failed with exit code $LASTEXITCODE" }

$process = Start-Process -FilePath $exePath -ArgumentList "--port", "$Port", "--mcp-port", "$McpPort", "--no-browser" -PassThru -WindowStyle Hidden
try {
    $healthUrl = "http://127.0.0.1:$Port/health"
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $health = $null
    do {
        if ($process.HasExited) {
            throw "NCS_JD.exe exited before health succeeded (exit=$($process.ExitCode))."
        }
        Start-Sleep -Milliseconds 250
        try { $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2 } catch { $health = $null }
    } while ($null -eq $health -and [DateTimeOffset]::UtcNow -lt $deadline)
    if ($null -eq $health -or $health.service -ne "ncs-jd-web" -or $health.backend_connected -ne $true) {
        throw "Frozen health smoke failed at $healthUrl"
    }
    Write-Host "Frozen health smoke passed: $healthUrl"

    $uiContract = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/ui-contract" -TimeoutSec 30
    if ($uiContract.generation_mode -ne "deterministic_with_optional_template_mapping" -or $uiContract.external_ai_required -ne $false) {
        throw "Frozen deterministic generation contract failed"
    }
    if (
        @($uiContract.providers) -notcontains "off" -or
        @($uiContract.providers) -notcontains "claude" -or
        @($uiContract.providers) -notcontains "codex"
    ) {
        throw "Frozen run-mode providers are incomplete"
    }
    if (@($uiContract.secret_inputs).Count -ne 0) {
        throw "Frozen build asks a person for a credential"
    }
    Write-Host "Frozen deterministic generation with CLI-login AI contract passed"

    $workspaceHtml = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/" -TimeoutSec 30).Content
    $runModeIndex = $workspaceHtml.IndexOf('name="run_mode"')
    $pickerIndex = $workspaceHtml.IndexOf("data-engine-picker")
    if ($runModeIndex -lt 0 -or $pickerIndex -lt 0 -or $runModeIndex -ge $pickerIndex) {
        throw "Frozen run-mode choice does not precede the engine picker"
    }
    if (
        -not $workspaceHtml.Contains('data-login-provider="claude"') -or
        -not $workspaceHtml.Contains('data-login-provider="codex"')
    ) {
        throw "Frozen CLI login controls are incomplete"
    }
    if ($workspaceHtml.Contains('type="password"')) {
        throw "Frozen build still has a credential input"
    }
    $loginScript = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/static/provider-login.js" -TimeoutSec 30).Content
    if (-not $loginScript.Contains("/api/llm/providers") -or -not $loginScript.Contains("X-NCS-JD-Local-Action")) {
        throw "Frozen CLI login script is incomplete"
    }
    Write-Host "Frozen run-mode then Claude/Codex engine picker passed"

    $profile = Get-Content -LiteralPath (Join-Path $projectRoot "tests\fixtures\job_profile_v1.json") -Raw -Encoding utf8 | ConvertFrom-Json
    $body = @{ job_profile = $profile; filename = "smoke.hwpx" } | ConvertTo-Json -Depth 100 -Compress
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $hwpx = Invoke-WebRequest `
        -UseBasicParsing `
        -Uri "http://127.0.0.1:$Port/api/drafts/export/hwpx" `
        -Method Post `
        -ContentType "application/json" `
        -Body $bodyBytes `
        -TimeoutSec 120
    if ($hwpx.StatusCode -ne 200 -or -not $hwpx.Headers["X-Hwpx-Validation-Entries"]) {
        throw "Frozen HWPX generation readiness failed"
    }
    Write-Host "Frozen Kordoc HWPX generation passed"
}
finally {
    # A PyInstaller one-file launcher may replace its bootstrap process with a
    # child that has the same executable path. Stop only processes started by
    # this smoke run and preserve any instance that existed beforehand.
    Get-Process -Name NCS_JD -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $exePath -and $_.Id -notin $existingProcessIds } |
        Stop-Process -Force
    if (-not $process.HasExited) {
        $process.WaitForExit(10000) | Out-Null
    }
}
