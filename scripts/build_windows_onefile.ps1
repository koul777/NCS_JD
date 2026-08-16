[CmdletBinding()]
param(
    [string]$PythonExecutable = "",
    [string]$NodeExecutable = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$workRoot = Join-Path $projectRoot "build\pyinstaller-onefile"
$nodeRuntimeRoot = Join-Path $projectRoot "build\kordoc-runtime"
$nodeRuntimeModules = Join-Path $nodeRuntimeRoot "node_modules"
$exePath = Join-Path $projectRoot "NCS_JD.exe"

if (-not $PythonExecutable) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) { throw "Python 3.12+ is required." }
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
    if ($null -eq $nodeCommand) { throw "Node.js 18+ is required." }
    $NodeExecutable = $nodeCommand.Source
}
$NodeExecutable = [System.IO.Path]::GetFullPath($NodeExecutable)
$nodeVersion = (& $NodeExecutable -p "process.versions.node").Trim()
if ($LASTEXITCODE -ne 0 -or [version]$nodeVersion -lt [version]"18.0.0") {
    throw "Node.js 18+ is required; found $nodeVersion"
}

Push-Location $projectRoot
try {
    & $NodeExecutable --input-type=module --eval "const k=await import('kordoc');if(k.VERSION!=='4.2.9')process.exit(2)"
    if ($LASTEXITCODE -ne 0) { throw "Kordoc 4.2.9 is missing. Run npm ci first." }
}
finally {
    Pop-Location
}

if ($Clean -or -not (Test-Path -LiteralPath (Join-Path $nodeRuntimeModules "kordoc\package.json") -PathType Leaf)) {
    if (Test-Path -LiteralPath $nodeRuntimeRoot) {
        $resolvedRuntimeRoot = [System.IO.Path]::GetFullPath($nodeRuntimeRoot)
        if (-not $resolvedRuntimeRoot.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean a path outside the project: $resolvedRuntimeRoot"
        }
        Remove-Item -LiteralPath $resolvedRuntimeRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $nodeRuntimeRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\kordoc-runtime-package.json") -Destination (Join-Path $nodeRuntimeRoot "package.json")
    & npm install --prefix $nodeRuntimeRoot --omit=optional --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "Minimal Kordoc runtime install failed with exit code $LASTEXITCODE" }
}

Push-Location $nodeRuntimeRoot
try {
    & $NodeExecutable --input-type=module --eval "const k=await import('kordoc');if(k.VERSION!=='4.2.9')process.exit(2);await import('pdfjs-dist/legacy/build/pdf.mjs')"
    if ($LASTEXITCODE -ne 0) { throw "Minimal Kordoc runtime self-check failed." }
}
finally {
    Pop-Location
}

if ($Clean) {
    foreach ($target in @($workRoot, $exePath)) {
        if (Test-Path -LiteralPath $target) {
            $resolvedTarget = [System.IO.Path]::GetFullPath($target)
            if (-not $resolvedTarget.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to clean a path outside the project: $resolvedTarget"
            }
            Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
        }
    }
}

$env:NCS_JD_PACKAGE_NODE_EXE = $NodeExecutable
$env:NCS_JD_PACKAGE_NODE_MODULES = $nodeRuntimeModules
try {
    & $PythonExecutable -m PyInstaller `
        --noconfirm `
        --distpath $projectRoot `
        --workpath $workRoot `
        (Join-Path $projectRoot "packaging\NCS_JD_onefile.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller one-file build failed with exit code $LASTEXITCODE" }
}
finally {
    Remove-Item Env:NCS_JD_PACKAGE_NODE_EXE -ErrorAction SilentlyContinue
    Remove-Item Env:NCS_JD_PACKAGE_NODE_MODULES -ErrorAction SilentlyContinue
}

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "PyInstaller completed without producing $exePath"
}
Write-Host "Built: $exePath"
Write-Host ("One-file size: {0:N1} MiB" -f ((Get-Item $exePath).Length / 1MB))
