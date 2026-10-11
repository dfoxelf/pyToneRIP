[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $scriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$spec = Join-Path $scriptRoot "runtime\pyToneRIP.Runtime.spec"
$work = Join-Path $scriptRoot "runtime\build"
$dist = Join-Path $scriptRoot "runtime\dist"
$runtime = Join-Path $dist "pyToneRIP.Runtime"
$executable = Join-Path $runtime "pyToneRIP.Runtime.exe"
$pyInstallerConfig = Join-Path $projectRoot ".pyinstaller"
$env:UV_CACHE_DIR = Join-Path $projectRoot ".uv-cache"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python environment was not found: $python"
}

$cythonBuild = Join-Path $scriptRoot "Build-CythonExtensions.ps1"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $cythonBuild
if ($LASTEXITCODE -ne 0) {
    throw "Cython extension build failed with exit code $LASTEXITCODE."
}

Write-Host "Building self-contained x64 Python RIP runtime (PyInstaller one-folder)..."
$env:PYINSTALLER_CONFIG_DIR = $pyInstallerConfig
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --workpath $work `
    --distpath $dist `
    $spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Frozen runtime executable was not created: $executable"
}

$bytes = [System.IO.File]::ReadAllBytes($executable)
$peOffset = [BitConverter]::ToInt32($bytes, 0x3c)
$machine = [BitConverter]::ToUInt16($bytes, $peOffset + 4)
if ($machine -ne 0x8664) {
    throw ("Frozen runtime is not x64 (PE machine 0x{0:X4})." -f $machine)
}

$files = @(Get-ChildItem -LiteralPath $runtime -Recurse -File)
if ($files.Count -lt 10) {
    throw "Frozen runtime content inventory is unexpectedly small."
}
$size = ($files | Measure-Object -Property Length -Sum).Sum
Write-Host "Frozen runtime: $executable"
Write-Host "Runtime files: $($files.Count); bytes: $size"
