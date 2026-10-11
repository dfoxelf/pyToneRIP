[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$setup = Join-Path $projectRoot "cython_setup.py"
$temporary = Join-Path $projectRoot "tmp\cython-build"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python environment was not found: $python"
}
if (-not (Test-Path -LiteralPath $setup)) {
    throw "Cython build recipe was not found: $setup"
}

Write-Host "Building AOT x64 Cython kernels (QTR + halftoning + Weave + PackBits)..."
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $vswhere)) {
    throw "Visual Studio locator was not found: $vswhere"
}
$visualStudio = & $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath
if ([string]::IsNullOrWhiteSpace($visualStudio)) {
    throw "Visual Studio x64 C++ Build Tools are required for Cython."
}
$developerCommand = Join-Path $visualStudio "Common7\Tools\VsDevCmd.bat"
$windowsSdkBin = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Directory |
    Sort-Object Name -Descending |
    ForEach-Object { Join-Path $_.FullName "x64" } |
    Where-Object { Test-Path -LiteralPath (Join-Path $_ "rc.exe") } |
    Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($windowsSdkBin)) {
    throw "Windows SDK x64 resource compiler was not found."
}
$buildTemp = Join-Path $temporary "temp"
$buildLib = Join-Path $temporary "lib"
$command = 'set "PATH=' + $windowsSdkBin + ';%PATH%" && call "' +
    $developerCommand + '" -no_logo -arch=amd64 -host_arch=amd64 && ' +
    '"' + $python + '" "' + $setup + '" build_ext --inplace --build-temp "' +
    $buildTemp + '" --build-lib "' + $buildLib + '"'
Push-Location -LiteralPath $projectRoot
try {
    & $env:ComSpec /d /s /c $command
    if ($LASTEXITCODE -ne 0) {
        throw "Cython extension build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

$extensions = @(Get-ChildItem -LiteralPath (Join-Path $projectRoot "src\xp750_rip") `
    -Filter "_cython_kernels*.pyd" -File)
if ($extensions.Count -ne 1) {
    throw "Expected exactly one compiled Cython extension, found $($extensions.Count)."
}

$bytes = [System.IO.File]::ReadAllBytes($extensions[0].FullName)
$peOffset = [BitConverter]::ToInt32($bytes, 0x3c)
$machine = [BitConverter]::ToUInt16($bytes, $peOffset + 4)
if ($machine -ne 0x8664) {
    throw ("Cython extension is not x64 (PE machine 0x{0:X4})." -f $machine)
}
Write-Host "Cython x64 extension: $($extensions[0].FullName)"
