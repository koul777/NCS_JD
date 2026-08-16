[CmdletBinding()]
param(
    [string]$PackageDirectory = "release\NCS_JD-windows-x64-v0.1.4",
    [ValidateRange(1, 65535)]
    [int]$Port = 18082,
    [ValidateRange(1, 65535)]
    [int]$McpPort = 18766,
    [int]$TimeoutSeconds = 150
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$packageRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $PackageDirectory))
$exePath = Join-Path $packageRoot "NCS_JD.exe"
$mcpExePath = Join-Path $packageRoot "NCS_MCP\NCS_MCP.exe"
$announcementPath = Join-Path $packageRoot "examples\administrative-support-announcement.txt"
$templatePath = Join-Path $packageRoot "examples\ncs-jd-supported-template.hwpx"
$outputRoot = Join-Path $projectRoot ".tmp\portable-e2e"
$outputPath = Join-Path $outputRoot "generated-from-template.hwpx"
$headerPath = Join-Path $outputRoot "response.headers.txt"

foreach ($required in @($exePath, $mcpExePath, $announcementPath, $templatePath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Portable E2E input is missing: $required"
    }
}
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null

$existingAppProcessIds = @(
    Get-Process -Name NCS_JD -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $exePath } |
        ForEach-Object { $_.Id }
)
$existingMcpProcessIds = @(
    Get-Process -Name NCS_MCP -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $mcpExePath } |
        ForEach-Object { $_.Id }
)

$process = Start-Process -FilePath $exePath -ArgumentList @(
    "--port", "$Port", "--mcp-port", "$McpPort", "--no-browser"
) -PassThru -WindowStyle Hidden
try {
    $healthUrl = "http://127.0.0.1:$Port/health"
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $health = $null
    do {
        if ($process.HasExited) {
            throw "Portable NCS_JD.exe exited before health succeeded (exit=$($process.ExitCode))."
        }
        Start-Sleep -Milliseconds 250
        try { $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2 } catch { $health = $null }
    } while ($null -eq $health -and [DateTimeOffset]::UtcNow -lt $deadline)
    if ($null -eq $health -or $health.backend_connected -ne $true) {
        throw "Portable app health did not become ready: $healthUrl"
    }

    $documentId = [guid]::NewGuid().ToString()
    $createdAt = [DateTimeOffset]::Now.ToString("yyyy-MM-ddTHH:mm:sszzz")
    $curlArguments = @(
        "--silent", "--show-error", "--fail-with-body",
        "--output", $outputPath,
        "--dump-header", $headerPath,
        "--form", "document_id=$documentId",
        "--form", "created_at=$createdAt",
        "--form", "provider=off",
        "--form", "announcement=@$announcementPath;type=text/plain",
        "--form", "template=@$templatePath;type=application/octet-stream",
        "http://127.0.0.1:$Port/api/generate-job-description"
    )
    & curl.exe @curlArguments
    if ($LASTEXITCODE -ne 0) { throw "Portable automatic generation request failed." }

    $headers = Get-Content -LiteralPath $headerPath -Raw -Encoding ascii
    if ($headers -notmatch "(?im)^X-HWPX-Template-Used:\s*true\s*$") {
        throw "Uploaded template was not applied by the portable application."
    }
    if ($headers -notmatch "(?im)^X-HWPX-Template-Mode:\s*hwpx-preserve\s*$") {
        throw "Portable template mode was not hwpx-preserve."
    }
    if ($headers -notmatch "(?im)^X-NCS-JD-AI-Provider:\s*off\s*$") {
        throw "Portable generation did not disable external AI."
    }
    if ($headers -notmatch "(?im)^X-NCS-JD-Generation-Mode:\s*deterministic\s*$") {
        throw "Portable generation was not deterministic."
    }
    if ((Get-Item -LiteralPath $outputPath).Length -lt 1024) {
        throw "Portable HWPX output is unexpectedly small."
    }
    Write-Host "Portable automatic/template E2E passed: $outputPath"
}
finally {
    Get-Process -Name NCS_JD -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $exePath -and $_.Id -notin $existingAppProcessIds } |
        Stop-Process -Force
    Get-Process -Name NCS_MCP -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $mcpExePath -and $_.Id -notin $existingMcpProcessIds } |
        Stop-Process -Force
}
