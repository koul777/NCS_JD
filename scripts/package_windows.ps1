[CmdletBinding()]
param(
    [string]$ReleaseDirectory = "release",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $ReleaseDirectory))
$packageRoot = Join-Path $releaseRoot "NCS_JD"
if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot "build_windows.ps1") -OutputDirectory $ReleaseDirectory
    if ($LASTEXITCODE -ne 0) { throw "Windows build failed." }
}
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "NCS_JD.exe") -PathType Leaf)) {
    throw "Build output is missing: $packageRoot"
}

$archive = Join-Path $releaseRoot "NCS_JD-windows-x64.zip"
if (Test-Path -LiteralPath $archive) {
    Remove-Item -LiteralPath $archive -Force
}
Compress-Archive -LiteralPath $packageRoot -DestinationPath $archive -CompressionLevel Optimal
$hash = Get-FileHash -LiteralPath $archive -Algorithm SHA256
Set-Content -LiteralPath "$archive.sha256" -Encoding ascii -NoNewline -Value "$($hash.Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($archive))"
Write-Host "Package: $archive"
Write-Host ("Archive size: {0:N1} MiB" -f ((Get-Item $archive).Length / 1MB))
Write-Host "SHA256: $($hash.Hash.ToLowerInvariant())"
