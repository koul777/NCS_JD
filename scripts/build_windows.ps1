[CmdletBinding()]
param(
    [string]$OutputDirectory = "release",
    [string]$PythonExecutable = "",
    [string]$NodeExecutable = "",
    [switch]$WithoutBundledNode,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$resolvedOutput = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
$buildRoot = Join-Path $projectRoot "build\pyinstaller"
$licenseRoot = Join-Path $projectRoot "build\windows-runtime"

if (-not $PythonExecutable) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "Python 3.12+ is required to build the Windows package."
    }
    $PythonExecutable = $pythonCommand.Source
}
$PythonExecutable = [System.IO.Path]::GetFullPath($PythonExecutable)
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Python executable was not found: $PythonExecutable"
}
$pythonVersion = (& $PythonExecutable -c "import platform; print(platform.python_version())").Trim()
if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]"3.12.0") {
    throw "Python 3.12+ is required; found $pythonVersion"
}

if (-not $NodeExecutable) {
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if ($null -eq $nodeCommand) {
        throw "Node.js 18+ is required to build the Windows package."
    }
    $NodeExecutable = $nodeCommand.Source
}
$NodeExecutable = [System.IO.Path]::GetFullPath($NodeExecutable)
if (-not (Test-Path -LiteralPath $NodeExecutable -PathType Leaf)) {
    throw "node.exe was not found: $NodeExecutable"
}

$nodeVersion = (& $NodeExecutable -p "process.versions.node").Trim()
if ($LASTEXITCODE -ne 0 -or [version]$nodeVersion -lt [version]"18.0.0") {
    throw "Node.js 18+ is required; found $nodeVersion"
}
if (-not $WithoutBundledNode) {
    Push-Location $projectRoot
    try {
        $kordocSelfCheck = "const k=await import('kordoc');if(k.VERSION!=='4.2.9')process.exit(2)"
        & $NodeExecutable --input-type=module --eval $kordocSelfCheck
        if ($LASTEXITCODE -ne 0) {
            throw "Kordoc 4.2.9 is missing. Run npm ci first."
        }
    }
    finally {
        Pop-Location
    }
}

New-Item -ItemType Directory -Force -Path $licenseRoot | Out-Null
$nodeLicense = Join-Path $licenseRoot "LICENSE.node.txt"
if (-not (Test-Path -LiteralPath $nodeLicense -PathType Leaf)) {
    $licenseUrl = "https://raw.githubusercontent.com/nodejs/node/v$nodeVersion/LICENSE"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $licenseUrl -OutFile $nodeLicense
    }
    catch {
        throw "Could not obtain the Node.js $nodeVersion license from $licenseUrl"
    }
}

if ($Clean) {
    foreach ($target in @($buildRoot, (Join-Path $resolvedOutput "NCS_JD"))) {
        if (Test-Path -LiteralPath $target) {
            $resolvedTarget = [System.IO.Path]::GetFullPath($target)
            if (-not $resolvedTarget.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to clean a path outside the project: $resolvedTarget"
            }
            Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
        }
    }
}

New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null
$env:NCS_JD_PACKAGE_NODE_EXE = $NodeExecutable
$env:NCS_JD_PACKAGE_WITHOUT_NODE = if ($WithoutBundledNode) { "1" } else { "0" }
try {
    & $PythonExecutable -m PyInstaller `
        --noconfirm `
        --distpath $resolvedOutput `
        --workpath $buildRoot `
        (Join-Path $projectRoot "packaging\NCS_JD.spec")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
}
finally {
    Remove-Item Env:NCS_JD_PACKAGE_NODE_EXE -ErrorAction SilentlyContinue
    Remove-Item Env:NCS_JD_PACKAGE_WITHOUT_NODE -ErrorAction SilentlyContinue
}

$exePath = Join-Path $resolvedOutput "NCS_JD\NCS_JD.exe"
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "PyInstaller completed without producing $exePath"
}
$size = (Get-ChildItem (Split-Path -Parent $exePath) -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host "Built: $exePath"
Write-Host ("Onedir size: {0:N1} MiB" -f ($size / 1MB))
