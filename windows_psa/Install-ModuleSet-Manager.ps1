[CmdletBinding()]
param(
    [string]$ModuleSetRoot,
    [string]$LogPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
    $logDirectory = [System.IO.Path]::GetDirectoryName(
        [System.IO.Path]::GetFullPath($LogPath))
    [System.IO.Directory]::CreateDirectory($logDirectory) | Out-Null
    Start-Transcript -LiteralPath $LogPath -Force | Out-Null
}

$principal = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this installer from PowerShell opened as Administrator.'
}

if ([string]::IsNullOrWhiteSpace($ModuleSetRoot)) {
    $ModuleSetRoot = $PSScriptRoot
}
$root = [System.IO.Path]::GetFullPath(
    (Resolve-Path -LiteralPath $ModuleSetRoot -ErrorAction Stop).ProviderPath
).TrimEnd('\') + '\'

function Resolve-PackageFile {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    if ([System.IO.Path]::IsPathRooted($RelativePath) -or
        $RelativePath -split '[\\/]' -contains '..') {
        throw "Unsafe package path: $RelativePath"
    }
    $path = [System.IO.Path]::GetFullPath((Join-Path $root $RelativePath))
    if (-not $path.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Package file is missing or outside the package set: $RelativePath"
    }
    return $path
}

function Assert-FileHash {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Expected
    )
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Expected) {
        throw "SHA-256 mismatch: $Path"
    }
}

$catalogPath = Resolve-PackageFile 'catalog.json'
$signaturePath = Resolve-PackageFile 'catalog.sig'
$certificatePath = Resolve-PackageFile 'pyToneRIP-TestSigning.cer'
$readyMarkerPath = Resolve-PackageFile 'PACKAGE-READY.json'
$catalogBytes = [System.IO.File]::ReadAllBytes($catalogPath)
$catalog = [System.Text.Encoding]::UTF8.GetString($catalogBytes).TrimStart([char]0xFEFF) |
    ConvertFrom-Json
$readyMarker = Get-Content -LiteralPath $readyMarkerPath -Raw | ConvertFrom-Json
if ($readyMarker.schema -ne 'pytone-rip.package-ready' -or
    [string]$readyMarker.catalog_sha256 -ne
        (Get-FileHash -LiteralPath $catalogPath -Algorithm SHA256).Hash) {
    throw 'This ModuleSet is incomplete. Use only an artifact containing a valid PACKAGE-READY.json.'
}
if ($catalog.schema -ne 'pytone-rip.windows-printer-catalog' -or
    [int]$catalog.version -ne 2 -or [int]$catalog.core_contract -ne 2 -or
    $catalog.core.package_identity -ne 'pyToneRIP.Manager') {
    throw 'This package set is not compatible with the dynamic pyToneRIP Manager.'
}

$certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    [System.IO.File]::ReadAllBytes($certificatePath))
try {
    if ($certificate.GetNameInfo(
        [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
        $false) -ne 'pyToneRIP') {
        throw 'Unexpected package certificate subject.'
    }
    $rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPublicKey(
        $certificate)
    try {
        $signature = [Convert]::FromBase64String(
            [System.IO.File]::ReadAllText($signaturePath).Trim())
        if (-not $rsa.VerifyData(
            $catalogBytes, $signature,
            [System.Security.Cryptography.HashAlgorithmName]::SHA256,
            [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)) {
            throw 'Package catalog signature is invalid.'
        }
    } finally { $rsa.Dispose() }
} finally { $certificate.Dispose() }

$legacyModules = @(Get-AppxPackage -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'pyToneRIP.Printer.*' })
if ($legacyModules.Count -ne 0) {
    throw 'Legacy 1.0.0.78 model packages are installed. Run Uninstall-pyToneRIP.ps1, restart Windows if requested, and then install 1.0.0.79.'
}

$managerPath = Resolve-PackageFile ([string]$catalog.core.package_file)
Assert-FileHash $managerPath ([string]$catalog.core.package_sha256)
if ([string]$readyMarker.manager_sha256 -ne [string]$catalog.core.package_sha256) {
    throw 'PACKAGE-READY.json does not match the Manager MSIX.'
}
function Get-AppxIdentityFromFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entry = $archive.GetEntry('AppxManifest.xml')
        if ($null -eq $entry) {
            throw "Dependency package has no AppxManifest.xml: $Path"
        }
        $reader = [System.IO.StreamReader]::new($entry.Open())
        try {
            [xml]$manifest = $reader.ReadToEnd()
        } finally { $reader.Dispose() }
        $identity = $manifest.SelectSingleNode(
            "/*[local-name()='Package']/*[local-name()='Identity']")
        if ($null -eq $identity) {
            throw "Dependency package has no identity: $Path"
        }
        return $identity
    } finally { $archive.Dispose() }
}

$dependencies = @()
foreach ($dependency in @($catalog.core.dependencies)) {
    $path = Resolve-PackageFile ([string]$dependency.package_file)
    Assert-FileHash $path ([string]$dependency.package_sha256)
    $identity = Get-AppxIdentityFromFile $path
    $name = [string]$identity.GetAttribute('Name')
    $publisher = [string]$identity.GetAttribute('Publisher')
    $architecture = [string]$identity.GetAttribute('ProcessorArchitecture')
    $version = [version]$identity.GetAttribute('Version')
    $installed = @(Get-AppxPackage -Name $name -PackageTypeFilter Framework |
        Where-Object {
            $_.Publisher -eq $publisher -and
            $_.Architecture.ToString() -eq $architecture -and
            [version]$_.Version -ge $version
        })
    if ($installed.Count -gt 0) {
        Write-Host "Using installed dependency: $name $($installed[0].Version) $architecture"
        continue
    }
    $dependencies += $path
    Write-Host "Adding missing dependency: $name $version $architecture"
}

Import-Certificate -FilePath $certificatePath `
    -CertStoreLocation 'Cert:\LocalMachine\TrustedPeople' | Out-Null
Write-Host 'Installing the single pyToneRIP Manager/Core package...'
$installParams = @{
    Path = $managerPath
    ForceUpdateFromAnyVersion = $true
}
if ($dependencies.Count -gt 0) {
    $installParams.DependencyPath = $dependencies
}
Add-AppxPackage @installParams

# Do not create a .lnk here. Windows must own the Start registration for the
# MSIX application so that a fresh install/update is listed normally in Start's
# Recommended/Recently added area and uses the package's native identity.
Write-Host 'pyToneRIP installation PASS. Windows registered the MSIX application in Start.'
if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
    Stop-Transcript | Out-Null
}
