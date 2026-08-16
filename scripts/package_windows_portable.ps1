[CmdletBinding()]
param(
    [string]$Version = "0.1.4",
    [string]$NcsMcpRoot = "C:\workspace\NCS_MCP",
    [string]$PythonExecutable = "",
    [string]$ReleaseDirectory = "release",
    [switch]$SkipAppBuild,
    [switch]$SkipSidecarBuild,
    [switch]$SkipDatabaseExport
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$NcsMcpRoot = [System.IO.Path]::GetFullPath($NcsMcpRoot)
$releaseRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $ReleaseDirectory))
if (-not $releaseRoot.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "ReleaseDirectory must stay inside the project: $releaseRoot"
}
if (-not (Test-Path -LiteralPath (Join-Path $NcsMcpRoot "src\ncs_mcp\server.py") -PathType Leaf)) {
    throw "NCS_MCP source checkout was not found: $NcsMcpRoot"
}

if (-not $PythonExecutable) {
    $PythonExecutable = Join-Path $projectRoot ".venv-build313\Scripts\python.exe"
}
$PythonExecutable = [System.IO.Path]::GetFullPath($PythonExecutable)
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Build Python was not found: $PythonExecutable"
}

$appExe = Join-Path $projectRoot "NCS_JD.exe"
if (-not $SkipAppBuild) {
    & (Join-Path $PSScriptRoot "build_windows_onefile.ps1") -PythonExecutable $PythonExecutable
    if ($LASTEXITCODE -ne 0) { throw "NCS_JD.exe build failed." }
}
if (-not (Test-Path -LiteralPath $appExe -PathType Leaf)) {
    throw "NCS_JD.exe is missing: $appExe"
}

$sidecarDistRoot = Join-Path $projectRoot "build\ncs-mcp-sidecar-dist"
$sidecarWorkRoot = Join-Path $projectRoot "build\ncs-mcp-sidecar-work"
$sidecarRoot = Join-Path $sidecarDistRoot "NCS_MCP"
if (-not $SkipSidecarBuild) {
    $env:NCS_MCP_SOURCE_ROOT = $NcsMcpRoot
    try {
        & $PythonExecutable -m PyInstaller `
            --noconfirm `
            --distpath $sidecarDistRoot `
            --workpath $sidecarWorkRoot `
            (Join-Path $projectRoot "packaging\NCS_MCP_sidecar.spec")
        if ($LASTEXITCODE -ne 0) { throw "NCS MCP sidecar build failed." }
    }
    finally {
        Remove-Item Env:NCS_MCP_SOURCE_ROOT -ErrorAction SilentlyContinue
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $sidecarRoot "NCS_MCP.exe") -PathType Leaf)) {
    throw "NCS MCP sidecar output is missing: $sidecarRoot"
}

$databaseBuildRoot = Join-Path $projectRoot "build\portable-data"
$servingDatabase = Join-Path $databaseBuildRoot "ncs_jd_serving.db"
$servingReport = Join-Path $databaseBuildRoot "ncs_jd_serving.report.json"
if (-not $SkipDatabaseExport) {
    New-Item -ItemType Directory -Force -Path $databaseBuildRoot | Out-Null
    $sourceDatabase = Join-Path $NcsMcpRoot "data\processed\ncs.db"
    $ncsPython = Join-Path $NcsMcpRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $sourceDatabase -PathType Leaf)) {
        throw "Canonical NCS database is missing: $sourceDatabase"
    }
    if (-not (Test-Path -LiteralPath $ncsPython -PathType Leaf)) {
        throw "NCS MCP Python is missing: $ncsPython"
    }
    & $ncsPython (Join-Path $NcsMcpRoot "scripts\export_interview_serving_db.py") `
        --source $sourceDatabase `
        --destination $servingDatabase `
        --report $servingReport
    if ($LASTEXITCODE -ne 0) { throw "NCS serving database export failed." }
}
if (-not (Test-Path -LiteralPath $servingDatabase -PathType Leaf)) {
    throw "NCS serving database is missing: $servingDatabase"
}

$packageName = "NCS_JD-windows-x64-v$Version"
$packageRoot = Join-Path $releaseRoot $packageName
if (Test-Path -LiteralPath $packageRoot) {
    $resolvedPackageRoot = [System.IO.Path]::GetFullPath($packageRoot)
    if (-not $resolvedPackageRoot.StartsWith($releaseRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace a package outside the release directory: $resolvedPackageRoot"
    }
    Remove-Item -LiteralPath $resolvedPackageRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null
$packageMcpRoot = Join-Path $packageRoot "NCS_MCP"
Copy-Item -LiteralPath $appExe -Destination (Join-Path $packageRoot "NCS_JD.exe")
Copy-Item -LiteralPath $sidecarRoot -Destination $packageMcpRoot -Recurse
New-Item -ItemType Directory -Force -Path (Join-Path $packageMcpRoot "data") | Out-Null
Copy-Item -LiteralPath $servingDatabase -Destination (Join-Path $packageMcpRoot "data\ncs_jd_serving.db")
if (Test-Path -LiteralPath $servingReport -PathType Leaf) {
    Copy-Item -LiteralPath $servingReport -Destination (Join-Path $packageMcpRoot "data\ncs_jd_serving.report.json")
}
Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\PORTABLE_README.txt") -Destination (Join-Path $packageRoot "README_FIRST.txt")
$examplesRoot = Join-Path $packageRoot "examples"
New-Item -ItemType Directory -Force -Path $examplesRoot | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot "reports\samples\administrative-support-announcement.txt") -Destination $examplesRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "reports\samples\ncs-jd-supported-template.hwpx") -Destination $examplesRoot

$archive = Join-Path $releaseRoot "$packageName.zip"
if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
Compress-Archive -LiteralPath $packageRoot -DestinationPath $archive -CompressionLevel Optimal
$hash = Get-FileHash -LiteralPath $archive -Algorithm SHA256
$hashPath = "$archive.sha256"
Set-Content -LiteralPath $hashPath -Encoding ascii -NoNewline -Value "$($hash.Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($archive))"

Write-Host "Portable package: $archive"
Write-Host ("Archive size: {0:N1} MiB" -f ((Get-Item $archive).Length / 1MB))
Write-Host "SHA256: $($hash.Hash.ToLowerInvariant())"
