[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$ModuleSetRoot,
    [string]$ISCCPath = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Version -notmatch '^\d+\.\d+\.\d+\.\d+$') {
    throw 'Version must have four numeric parts, for example 1.0.0.174.'
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$artifactRoot = Join-Path $scriptRoot 'artifacts'
if ([string]::IsNullOrWhiteSpace($ModuleSetRoot)) {
    $ModuleSetRoot = Join-Path $artifactRoot "pyToneRIP-ModuleSet-$Version"
}
$moduleRoot = (Resolve-Path -LiteralPath $ModuleSetRoot -ErrorAction Stop).ProviderPath
foreach ($cleanupFile in @('Uninstall-pyToneRIP.ps1', 'Clear-PyToneRipPrintState.ps1')) {
    if (-not (Test-Path -LiteralPath (Join-Path $scriptRoot $cleanupFile) -PathType Leaf)) {
        throw "The installer cleanup source is missing: $cleanupFile."
    }
}
$compiler = (Resolve-Path -LiteralPath $ISCCPath -ErrorAction Stop).ProviderPath
$readyPath = Join-Path $moduleRoot 'PACKAGE-READY.json'
$ready = Get-Content -LiteralPath $readyPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($ready.schema -ne 'pytone-rip.package-ready' -or $ready.version -ne $Version) {
    throw 'The requested version does not match PACKAGE-READY.json.'
}

# This is a read-only audit. The MSIX/runtime package is built separately.
& (Join-Path $scriptRoot 'Test-ModulePackageSet.ps1') -ModuleSetRoot $moduleRoot

$catalog = Get-Content -LiteralPath (Join-Path $moduleRoot 'catalog.json') `
    -Raw -Encoding UTF8 | ConvertFrom-Json
if ($catalog.core.package_identity -ne 'pyToneRIP.Manager' -or
    $catalog.package_version -ne $Version) {
    throw 'Unexpected package identity or catalog version.'
}

# The shortcut AUMID in pyToneRIP.iss is tied to this Publisher. Fail
# explicitly if signing identity changes rather than shipping a dead link.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$managerPath = Join-Path $moduleRoot ([string]$catalog.core.package_file)
$archive = [System.IO.Compression.ZipFile]::OpenRead($managerPath)
try {
    $entry = $archive.GetEntry('AppxManifest.xml')
    if ($null -eq $entry) { throw 'Manager MSIX has no AppxManifest.xml.' }
    $reader = [System.IO.StreamReader]::new($entry.Open())
    try { [xml]$manifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
    if ($manifest.Package.Identity.Name -ne 'pyToneRIP.Manager' -or
        $manifest.Package.Identity.Publisher -ne 'CN=pyToneRIP' -or
        @($manifest.Package.Applications.Application).Count -ne 1 -or
        $manifest.Package.Applications.Application.Id -ne 'App') {
        throw 'Manager identity changed; update the installer AUMID before compiling.'
    }
} finally { $archive.Dispose() }

$setupPath = Join-Path $artifactRoot "pyToneRIP-Setup-$Version-Beta.exe"
if (Test-Path -LiteralPath $setupPath) {
    throw "Setup EXE already exists; refusing to overwrite it: $setupPath"
}

# An ICO can contain PNG-compressed images. Generate it from the existing
# package logos so Setup and the optional desktop shortcut use the same art.
$buildDir = Join-Path $scriptRoot 'installer\.build'
New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
$iconPath = Join-Path $buildDir 'pyToneRIP.ico'
$licenseSource = Join-Path (Split-Path -Parent $scriptRoot) 'LICENSE'
$licensePath = Join-Path $buildDir 'LICENSE.txt'
Copy-Item -LiteralPath $licenseSource -Destination $licensePath -Force
$iconSources = @(
    (Join-Path $scriptRoot 'pyToneRIP\Assets\Square44x44Logo.png'),
    (Join-Path $scriptRoot 'pyToneRIP\Assets\Square150x150Logo.png')
)
Add-Type -AssemblyName System.Drawing
$iconImages = @(
    foreach ($source in $iconSources) {
        $bitmap = [System.Drawing.Bitmap]::FromFile($source)
        try {
            if ($bitmap.Width -ne $bitmap.Height -or $bitmap.Width -gt 256) {
                throw "Invalid square application logo: $source"
            }
            [pscustomobject]@{
                Width = $bitmap.Width
                Bytes = [System.IO.File]::ReadAllBytes($source)
            }
        } finally { $bitmap.Dispose() }
    }
)
$stream = [System.IO.File]::Create($iconPath)
try {
    $writer = [System.IO.BinaryWriter]::new($stream)
    try {
        $writer.Write([uint16]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]$iconImages.Count)
        $offset = 6 + 16 * $iconImages.Count
        foreach ($item in $iconImages) {
            $writer.Write([byte]($item.Width % 256))
            $writer.Write([byte]($item.Width % 256))
            $writer.Write([byte]0)
            $writer.Write([byte]0)
            $writer.Write([uint16]1)
            $writer.Write([uint16]32)
            $writer.Write([uint32]$item.Bytes.Length)
            $writer.Write([uint32]$offset)
            $offset += $item.Bytes.Length
        }
        foreach ($item in $iconImages) { $writer.Write([byte[]]$item.Bytes) }
    } finally { $writer.Dispose() }
} finally { $stream.Dispose() }

$issPath = Join-Path $scriptRoot 'installer\pyToneRIP.iss'
$arguments = @(
    "/DPackageVersion=$Version",
    "/DModuleSetDir=$moduleRoot",
    "/DInstallerIcon=$iconPath",
    "/DInstallerLicense=$licensePath",
    "/O$artifactRoot",
    $issPath
)
& $compiler @arguments
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $setupPath -PathType Leaf)) {
    throw "Inno Setup compiler failed with exit code $LASTEXITCODE."
}
$hash = (Get-FileHash -LiteralPath $setupPath -Algorithm SHA256).Hash
Write-Host "Setup ready: $setupPath"
Write-Host "SHA-256: $hash"
if ((Get-AuthenticodeSignature -LiteralPath $setupPath).Status -ne 'Valid') {
    Write-Warning 'The bootstrap EXE has no trusted Authenticode signature. Sign it before public distribution.'
}
