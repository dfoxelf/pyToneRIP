[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ModuleSetRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath($ModuleSetRoot).TrimEnd('\') + '\'

function Resolve-SetFile {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    if ([System.IO.Path]::IsPathRooted($RelativePath) -or
        $RelativePath -split '[\\/]' -contains '..') {
        throw "Unsafe catalog path: $RelativePath"
    }
    $path = [System.IO.Path]::GetFullPath((Join-Path $root $RelativePath))
    if (-not $path.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing or escaped catalog file: $RelativePath"
    }
    return $path
}

function Assert-Hash {
    param([string]$Path, [string]$Expected)
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Expected) {
        throw "SHA-256 mismatch: $Path"
    }
}

$catalogPath = Resolve-SetFile 'catalog.json'
$signaturePath = Resolve-SetFile 'catalog.sig'
$certificatePath = Resolve-SetFile 'pyToneRIP-TestSigning.cer'
$readyMarkerPath = Resolve-SetFile 'PACKAGE-READY.json'
$catalogBytes = [System.IO.File]::ReadAllBytes($catalogPath)
$catalog = [System.Text.Encoding]::UTF8.GetString($catalogBytes).TrimStart([char]0xFEFF) |
    ConvertFrom-Json
$readyMarker = Get-Content -LiteralPath $readyMarkerPath -Raw | ConvertFrom-Json
if ($readyMarker.schema -ne 'pytone-rip.package-ready' -or
    [string]$readyMarker.catalog_sha256 -ne
        (Get-FileHash -LiteralPath $catalogPath -Algorithm SHA256).Hash) {
    throw 'ModuleSet is incomplete or its readiness marker is invalid.'
}
if ($catalog.schema -ne 'pytone-rip.windows-printer-catalog' -or
    [int]$catalog.version -ne 2 -or [int]$catalog.core_contract -ne 2) {
    throw 'Unsupported pyToneRIP printer catalog contract.'
}

$certificateBytes = [System.IO.File]::ReadAllBytes($certificatePath)
$certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $certificateBytes)
try {
    $rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPublicKey(
        $certificate)
    try {
        $signature = [Convert]::FromBase64String(
            [System.IO.File]::ReadAllText($signaturePath).Trim())
        if (-not $rsa.VerifyData(
            $catalogBytes, $signature,
            [System.Security.Cryptography.HashAlgorithmName]::SHA256,
            [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)) {
            throw 'Catalog RSA signature is invalid.'
        }
    } finally { $rsa.Dispose() }
} finally { $certificate.Dispose() }

$managerPath = Resolve-SetFile ([string]$catalog.core.package_file)
Assert-Hash $managerPath ([string]$catalog.core.package_sha256)
if ([string]$readyMarker.manager_sha256 -ne [string]$catalog.core.package_sha256) {
    throw 'Readiness marker does not match the Manager MSIX.'
}
foreach ($dependency in @($catalog.core.dependencies)) {
    $dependencyPath = Resolve-SetFile ([string]$dependency.package_file)
    if ($dependencyPath -notmatch '(?i)[\\/]Dependencies[\\/]x64[\\/]') {
        throw 'Manager has a non-x64 dependency path.'
    }
    Assert-Hash $dependencyPath ([string]$dependency.package_sha256)
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($managerPath)
try {
    $manifestEntry = $archive.GetEntry('AppxManifest.xml')
    if (-not $manifestEntry) { throw 'Manager has no manifest.' }
    $reader = [System.IO.StreamReader]::new($manifestEntry.Open())
    try { $manifestText = $reader.ReadToEnd() } finally { $reader.Dispose() }
    [xml]$manifest = $manifestText
    if ([string]$manifest.Package.Identity.Name -ne
        [string]$catalog.core.package_identity) {
        throw 'Manager package identity mismatch.'
    }
    $workflowRegistrations = [regex]::Matches(
        $manifestText, 'Category="windows\.printSupportVirtualPrinterWorkflow"')
    $bootstrapEndpoints = [regex]::Matches(
        $manifestText, '<[^>]*PrintSupportVirtualPrinter\s')
    if ($workflowRegistrations.Count -ne 1 -or $bootstrapEndpoints.Count -ne 1 -or
        $manifestText -notmatch 'EntryPoint="pyToneRIP\.Tasks\.VirtualPrinterBackgroundTask"' -or
        $manifestText -notmatch 'PrinterUri="pytone-rip:workflow-bootstrap"' -or
        $manifestText -notmatch 'PdcFile="Assets\\PdcModels\\model\.[^"\\]+\.pdc\.xml"') {
        throw 'Manager workflow bootstrap registration is missing or invalid.'
    }
    if ($manifestText -notmatch 'windows.printSupportExtension' -or
        $manifestText -notmatch 'windows.printSupportSettingsUI') {
        throw 'Manager has no Print Support extension/settings registration.'
    }
    if (-not $archive.GetEntry('Runtime/pyToneRIP.Runtime.exe') -or
        -not $archive.GetEntry('pyToneRIP.QueueHelper.exe')) {
        throw 'Manager package role boundary is invalid.'
    }

    $ids = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase)
    $uris = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase)
    foreach ($model in @($catalog.modules)) {
        if ($model.schema -ne 'pytone-rip.windows-printer-model' -or
            [int]$model.version -ne 2 -or [int]$model.core_contract -ne 2 -or
            -not $ids.Add([string]$model.model_id) -or
            -not $uris.Add([string]$model.printer_uri_root)) {
            throw 'Printer catalog contains an invalid or duplicate model.'
        }
        $pdcPath = ([string]$model.pdc_file).Replace('\', '/')
        if (-not $pdcPath.StartsWith('Assets/PdcModels/',
            [System.StringComparison]::Ordinal) -or $pdcPath.Contains('..')) {
            throw "Unsafe model PDC path: $pdcPath"
        }
        $pdcEntry = $archive.GetEntry($pdcPath)
        if (-not $pdcEntry) { throw "Manager has no model PDC: $pdcPath" }
        $memory = [System.IO.MemoryStream]::new()
        try {
            $stream = $pdcEntry.Open()
            try { $stream.CopyTo($memory) } finally { $stream.Dispose() }
            $sha = [System.Security.Cryptography.SHA256]::Create()
            try {
                $actual = [BitConverter]::ToString(
                    $sha.ComputeHash($memory.ToArray())).Replace('-', '')
            } finally { $sha.Dispose() }
            if ($actual -ne [string]$model.pdc_sha256) {
                throw "Model PDC hash mismatch: $pdcPath"
            }
        } finally { $memory.Dispose() }
    }
} finally { $archive.Dispose() }

$unexpectedPackages = @(Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.msix' |
    Where-Object { $_.FullName -ne $managerPath })
if ($unexpectedPackages.Count -ne 0) {
    throw "Unexpected model MSIX packages: $($unexpectedPackages.FullName -join ', ')"
}
$unexpectedDependencies = @(Get-ChildItem -LiteralPath $root -Recurse -Directory |
    Where-Object { $_.Parent.Name -eq 'Dependencies' -and $_.Name -ne 'x64' })
if ($unexpectedDependencies.Count -ne 0) {
    throw "Unexpected dependency architectures: $($unexpectedDependencies.FullName -join ', ')"
}
Write-Host "Manager/PDC catalog audit PASS: $root"
