[CmdletBinding()]
param([string]$LogPath)

# Run from an elevated PowerShell window. A clean uninstall intentionally
# deletes retained jobs from pyToneRIP virtual queues before removing the MSIX.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $LogPath) { $LogPath = Join-Path $PSScriptRoot 'uninstall-cleanup.log' }
Start-Transcript -LiteralPath $LogPath -Force | Out-Null
$uninstallSucceeded = $false
try {

function Get-PyToneRipQueue {
    @(Get-Printer -ErrorAction SilentlyContinue | Where-Object {
        $_.PortName -match '(?i)pytone-rip:' -or
        $_.Name -like 'pyToneRIP:*'
    })
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from PowerShell opened with Run as administrator.'
}
$printStateCleanup = Join-Path $PSScriptRoot 'Clear-PyToneRipPrintState.ps1'
if (-not (Test-Path -LiteralPath $printStateCleanup -PathType Leaf)) {
    throw 'The print-state cleanup script is missing; uninstall stopped before removing packages.'
}
$formPlanPath = Join-Path $PSScriptRoot 'pyToneRIP-print-form-cleanup.json'

$queues = @(Get-PyToneRipQueue)
foreach ($queue in $queues) {
    Write-Host "Purging pyToneRIP jobs: $($queue.Name)"
    @(Get-PrintJob -PrinterName $queue.Name -ErrorAction SilentlyContinue) |
        Remove-PrintJob -ErrorAction SilentlyContinue
}

# A virtual-printer job can remain at Size=0 before DataAvailable and ignore
# Remove-PrintJob.  Do not remove/update the owning MSIX while such a job still
# exists: that is how Windows can leave the queue in PendingDeletion.  Wait for
# normal cleanup first, then remove only the exact SHD/SPL files belonging to
# still-live pyToneRIP job IDs while Spooler is stopped.
$jobDeadline = [DateTime]::UtcNow.AddSeconds(10)
do {
    $remainingJobs = @(
        foreach ($queue in $queues) {
            Get-PrintJob -PrinterName $queue.Name -ErrorAction SilentlyContinue
        }
    )
    if ($remainingJobs.Count -eq 0) { break }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $jobDeadline)

if ($remainingJobs.Count -gt 0) {
    $windowsRoot = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::Windows)
    $spoolRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $windowsRoot 'System32\spool\PRINTERS')).TrimEnd('\')

    Write-Host 'Stopping Print Spooler to clear only stuck pyToneRIP jobs...'
    Stop-Service -Name Spooler -Force -ErrorAction Stop
    try {
        foreach ($job in $remainingJobs) {
            $jobBase = '{0:D5}' -f [int]$job.ID
            foreach ($extension in @('.SHD', '.SPL')) {
                $target = [System.IO.Path]::GetFullPath(
                    (Join-Path $spoolRoot ($jobBase + $extension)))
                if (-not $target.StartsWith(
                    $spoolRoot + '\',
                    [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw "Unsafe spool job path: $target"
                }
                if (Test-Path -LiteralPath $target -PathType Leaf) {
                    Write-Host "Removing stuck pyToneRIP spool file: $target"
                    Remove-Item -LiteralPath $target -Force
                }
            }
        }
    } finally {
        Start-Service -Name Spooler -ErrorAction Stop
    }
    (Get-Service -Name Spooler).WaitForStatus(
        [System.ServiceProcess.ServiceControllerStatus]::Running,
        [TimeSpan]::FromSeconds(15))
}

$packages = @(Get-AppxPackage -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq 'pyToneRIP.PSA' -or
    $_.Name -eq 'pyToneRIP.Manager' -or
    $_.Name -like 'pyToneRIP.Printer.*'
} | Sort-Object @{ Expression = {
    if ($_.Name -like 'pyToneRIP.Printer.*') { 0 }
    elseif ($_.Name -eq 'pyToneRIP.PSA') { 1 }
    else { 2 }
} })
# Capture the private media IDs while their PDC files are still installed.
# Keep the plan until all cleanup succeeds so an interrupted uninstall can
# resume even after Windows has already removed the MSIX files.
$pdcFiles = @(
    foreach ($package in $packages) {
        foreach ($relativeRoot in @('Assets\PdcModels', 'Assets\Model')) {
            $pdcRoot = Join-Path $package.InstallLocation $relativeRoot
            if (Test-Path -LiteralPath $pdcRoot -PathType Container) {
                Get-ChildItem -LiteralPath $pdcRoot -Filter '*.pdc.xml' -File |
                    ForEach-Object FullName
            }
        }
        $legacyPdc = Join-Path $package.InstallLocation 'Assets\pyToneRIP.pdc.xml'
        if (Test-Path -LiteralPath $legacyPdc -PathType Leaf) { $legacyPdc }
    }
)
$formCleanupPlan = @(& $printStateCleanup -PdcFiles $pdcFiles -CaptureOnly)
if (Test-Path -LiteralPath $formPlanPath -PathType Leaf) {
    $savedPlan = Get-Content -LiteralPath $formPlanPath -Encoding UTF8 -Raw | ConvertFrom-Json
    $formCleanupPlan = @(@($formCleanupPlan) + @($savedPlan) |
        Group-Object Name,Keyword,W,H | ForEach-Object { $_.Group[0] })
}
if ($formCleanupPlan.Count) {
    ConvertTo-Json -InputObject @($formCleanupPlan) -Depth 4 |
        Set-Content -LiteralPath $formPlanPath -Encoding UTF8
}
foreach ($package in $packages) {
    Write-Host "Removing package: $($package.PackageFullName)"
    Remove-AppxPackage -Package $package.PackageFullName
}

$startMenuShortcut = Join-Path ([Environment]::GetFolderPath(
    [Environment+SpecialFolder]::CommonPrograms)) 'pyToneRIP Manager.lnk'
if (Test-Path -LiteralPath $startMenuShortcut -PathType Leaf) {
    Remove-Item -LiteralPath $startMenuShortcut -Force
    Write-Host "Removed pyToneRIP Start Menu shortcut: $startMenuShortcut"
}

# The installer owns this exact ProgramData cache. Removing it prevents stale
# model binaries from affecting a later clean ModuleSet installation.
$programDataRoot = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath(
    [Environment+SpecialFolder]::CommonApplicationData)).TrimEnd('\')
$deploymentRoot = [System.IO.Path]::GetFullPath((Join-Path $programDataRoot 'pyToneRIP'))
if ([System.IO.Path]::GetDirectoryName($deploymentRoot) -ne $programDataRoot -or
    [System.IO.Path]::GetFileName($deploymentRoot) -ne 'pyToneRIP') {
    throw 'Unsafe pyToneRIP deployment repository path.'
}
if (Test-Path -LiteralPath $deploymentRoot -PathType Container) {
    Remove-Item -LiteralPath $deploymentRoot -Recurse -Force
    Write-Host "Removed pyToneRIP deployment repository: $deploymentRoot"
}

# Normally package removal deletes its queues after the jobs are gone. Remove
# only exact pyToneRIP virtual endpoints if the spooler still exposes them.
foreach ($attempt in 1..30) {
    $remaining = @(Get-PyToneRipQueue)
    if ($remaining.Count -eq 0) {
        break
    }
    foreach ($queue in $remaining) {
        Remove-Printer -Name $queue.Name -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 200
}

$remaining = @(Get-PyToneRipQueue)
if ($remaining.Count) {
    $names = $remaining.Name -join ', '
    throw "Windows still holds pyToneRIP queues: $names. Restart Windows before installing another clean copy."
}
$cleanup = & $printStateCleanup -Forms $formCleanupPlan
if (Test-Path -LiteralPath $formPlanPath -PathType Leaf) {
    Remove-Item -LiteralPath $formPlanPath -Force
}
Write-Host "Removed private forms: $($cleanup.RemovedForms); removed orphan ports: $($cleanup.RemovedPorts)."
Write-Host 'pyToneRIP packages, queues, jobs, private forms and unused ports were removed.'
if ((Split-Path -Leaf $PSScriptRoot) -eq 'Payload') {
    $installerRoot = Split-Path -Parent $PSScriptRoot
    foreach ($file in @('install-msix.log', 'language-setup.log')) {
        $ownedLog = Join-Path $installerRoot $file
        if (Test-Path -LiteralPath $ownedLog -PathType Leaf) { Remove-Item -LiteralPath $ownedLog -Force }
    }
}
$uninstallSucceeded = $true
} finally {
    Stop-Transcript | Out-Null
    if ($uninstallSucceeded -and (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
        Remove-Item -LiteralPath $LogPath -Force
    }
}
