[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [string]$NcsMcpRoot = "C:\workspace\NCS_MCP"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$mcpLauncher = Join-Path $NcsMcpRoot "run_ncs_mcp_http.cmd"
$healthUrl = "http://127.0.0.1:8766/health"
$parserBridge = Join-Path $projectRoot "scripts\kordoc_bridge.mjs"
$rendererBridge = Join-Path $projectRoot "scripts\kordoc_hwpx_bridge.mjs"
$selfCheck = Join-Path $projectRoot "scripts\kordoc_selfcheck.mjs"

$nodeCommand = Get-Command "node" -ErrorAction SilentlyContinue
if ($null -eq $nodeCommand) {
    throw "Node.js를 찾을 수 없습니다. Kordoc 4.2.9 실행 환경을 먼저 설치하세요."
}
foreach ($bridge in @($parserBridge, $rendererBridge, $selfCheck)) {
    if (-not (Test-Path -LiteralPath $bridge -PathType Leaf)) {
        throw "Kordoc 브리지 파일을 찾을 수 없습니다: $bridge"
    }
}

Push-Location $projectRoot
try {
    & $nodeCommand.Source $selfCheck *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Kordoc 4.2.9 자체 점검에 실패했습니다. npm ci 실행 결과를 확인하세요."
    }
}
finally {
    Pop-Location
}

function Get-NcsMcpHealth {
    try {
        return Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 3
    }
    catch {
        return $null
    }
}

$health = Get-NcsMcpHealth
if ($null -eq $health) {
    if (-not (Test-Path -LiteralPath $mcpLauncher -PathType Leaf)) {
        throw "NCS MCP 실행기를 찾을 수 없습니다: $mcpLauncher"
    }
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c", $mcpLauncher `
        -WorkingDirectory $NcsMcpRoot `
        -WindowStyle Hidden | Out-Null

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $health = Get-NcsMcpHealth
    } while ($null -eq $health -and [DateTimeOffset]::UtcNow -lt $deadline)
}

if ($null -eq $health) {
    throw "NCS MCP가 20초 안에 준비되지 않았습니다."
}
if ($health.runtime.read_only_mode -ne $true -or $health.tools.operator -ne 0) {
    throw "NCS MCP가 읽기 전용 공개 도구 모드가 아닙니다. 실행 설정을 확인하세요."
}

$env:PYTHONPATH = Join-Path $projectRoot "src"
Write-Host "NCS JD: http://${BindHost}:$Port"
Write-Host "NCS MCP: $healthUrl (read-only)"
& python -m uvicorn ncs_jd.web.app:create_connected_app `
    --factory `
    --host $BindHost `
    --port $Port
