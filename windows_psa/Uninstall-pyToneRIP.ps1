[CmdletBinding()]
param([string]$LogPath, [string]$UserSid, [switch]$ElevatedWorker, [string]$QueueNamesBase64)

# Capture the initiating user before UAC can switch to another administrator.
# Inno must invoke this script without elevation; only the worker elevates.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $LogPath) { $LogPath = Join-Path $PSScriptRoot 'uninstall-cleanup.log' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $UserSid) { $UserSid = $identity.User.Value }
$UserSid = [Security.Principal.SecurityIdentifier]::new($UserSid).Value
$userQueueNames = @()
if (-not $ElevatedWorker) {
    if ($UserSid -ne $identity.User.Value) { throw 'Start uninstall in the account whose installation is being removed.' }
    # The user-specific API must run BEFORE UAC changes the caller's identity.
    # Find by package ownership, including renamed queues; never delete another
    # profile's queues through the machine-wide Remove-Printer command.
    $families = @(Get-AppxPackage -ErrorAction Stop | Where-Object {
        $_.Name -eq 'pyToneRIP.PSA' -or $_.Name -eq 'pyToneRIP.Manager' -or $_.Name -like 'pyToneRIP.Printer.*'
    } | ForEach-Object PackageFamilyName | Select-Object -Unique)
    if ($families.Count) {
        $manager = [Windows.Devices.Printers.VirtualPrinterManager, Windows.Devices.Printers, ContentType = WindowsRuntime]
        Add-Type -AssemblyName System.Runtime.WindowsRuntime
        $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        } | Select-Object -First 1
        foreach ($family in $families) {
            $names = @($manager::FindAllVirtualPrinters($family))
            $userQueueNames += $names
            foreach ($name in $names) {
                $operation = $manager::RemoveVirtualPrinterAsync($name)
                $task = $asTask.MakeGenericMethod([bool]).Invoke($null, @($operation))
                if (-not $task.Wait(30000)) { throw "Timed out removing the user's virtual printer: $name" }
                $removed = $task.GetAwaiter().GetResult()
                if (-not $removed -and $name -in @($manager::FindAllVirtualPrinters($family))) {
                    throw "Windows did not remove the user's virtual printer: $name"
                }
            }
            if (@($manager::FindAllVirtualPrinters($family)).Count) {
                throw "Virtual printers remain for the user's package: $family"
            }
        }
    }
    $QueueNamesBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(
        (ConvertTo-Json -InputObject @($userQueueNames) -Compress)))
} elseif ($QueueNamesBase64) {
    $userQueueNames = @(ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($QueueNamesBase64))))
}
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $windowsRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
    $systemDirectory = if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        'Sysnative'
    } else { 'System32' }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = Join-Path $windowsRoot ($systemDirectory + '\WindowsPowerShell\v1.0\powershell.exe')
    $start.UseShellExecute = $true
    $start.Verb = 'runas'
    $start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $start.Arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' +
        $PSCommandPath + '" -ElevatedWorker -UserSid "' + $UserSid + '" -QueueNamesBase64 "' +
        $QueueNamesBase64 + '" -LogPath "' + $LogPath + '"'
    $worker = [Diagnostics.Process]::Start($start)
    try {
        $worker.WaitForExit()
        exit $worker.ExitCode
    } finally { $worker.Dispose() }
}
Start-Transcript -LiteralPath $LogPath -Force | Out-Null
$uninstallSucceeded = $false
$targetUserRoot = $null
try {

function Get-PyToneRipQueue {
    # Shared queues are owned by Windows package registration. Do not purge
    # another user's jobs or manually delete their endpoints.
    if ($sharedInstallation) { return }
    @(Get-Printer -ErrorAction Stop | Where-Object {
        $_.PortName -match '(?i)^(pytone-rip:|pyToneRIP\.(Manager|PSA|Printer\.[^_]+)_)' -or
        ($_.Name -like 'pyToneRIP:*' -and
            ($_.DriverName -eq 'Microsoft Virtual Print Class Driver' -or $_.DriverName -like 'pyToneRIP*'))
    })
}

Write-Host "Uninstall target SID: $UserSid; worker SID: $($identity.User.Value)"
$allPackages = @(Get-AppxPackage -AllUsers -ErrorAction Stop | Where-Object {
    $_.Name -eq 'pyToneRIP.PSA' -or $_.Name -eq 'pyToneRIP.Manager' -or $_.Name -like 'pyToneRIP.Printer.*'
})
$sharedInstallation = @($allPackages | ForEach-Object { $_.PackageUserInformation } | Where-Object {
    $_.InstallState -eq 'Installed' -and $_.UserSecurityId.ToString() -ne $UserSid
}).Count -gt 0
# Open only the initiating user's loaded hive; never fall back to the worker's HKCU.
$targetUserRoot = [Microsoft.Win32.Registry]::Users.OpenSubKey($UserSid, $true)
if ($null -eq $targetUserRoot) { throw "The uninstall target's registry hive is not loaded: $UserSid" }
$printStateCleanup = Join-Path $PSScriptRoot 'Clear-PyToneRipPrintState.ps1'
$registryStateCleanup = Join-Path $PSScriptRoot 'Clear-PyToneRipRegistryState.ps1'
if (-not (Test-Path -LiteralPath $printStateCleanup -PathType Leaf)) {
    throw 'The print-state cleanup script is missing; uninstall stopped before removing packages.'
}
if (-not (Test-Path -LiteralPath $registryStateCleanup -PathType Leaf)) {
    throw 'The registry cleanup script is missing; uninstall stopped before removing packages.'
}
$formPlanPath = Join-Path $PSScriptRoot 'pyToneRIP-print-form-cleanup.json'

$queues = @(Get-PyToneRipQueue)
$ownedQueueNames = @(@($queues | ForEach-Object Name) + $userQueueNames | Select-Object -Unique)
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

$packages = @(Get-AppxPackage -User $UserSid -ErrorAction Stop | Where-Object {
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
    Remove-AppxPackage -Package $package.PackageFullName -User $UserSid -ErrorAction Stop
}
if (@(Get-AppxPackage -User $UserSid -ErrorAction Stop | Where-Object {
    $_.Name -eq 'pyToneRIP.PSA' -or $_.Name -eq 'pyToneRIP.Manager' -or $_.Name -like 'pyToneRIP.Printer.*'
}).Count) { throw "pyToneRIP is still registered for the uninstall target: $UserSid" }

$startMenuShortcut = Join-Path ([Environment]::GetFolderPath(
    [Environment+SpecialFolder]::CommonPrograms)) 'pyToneRIP Manager.lnk'
if (-not $sharedInstallation -and (Test-Path -LiteralPath $startMenuShortcut -PathType Leaf)) {
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
if (-not $sharedInstallation -and (Test-Path -LiteralPath $deploymentRoot -PathType Container)) {
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
$registryCleanup = & $registryStateCleanup -QueueNames $ownedQueueNames -UserSid $UserSid
Write-Host "Removed print registry cache keys: $($registryCleanup.RemovedRegistryKeys); values: $($registryCleanup.RemovedRegistryValues)."
if (Test-Path -LiteralPath $formPlanPath -PathType Leaf) {
    Remove-Item -LiteralPath $formPlanPath -Force
}
Write-Host "Removed private forms: $($cleanup.RemovedForms); removed orphan ports: $($cleanup.RemovedPorts)."
Write-Host "pyToneRIP was removed for user $UserSid."
if ($sharedInstallation) { Write-Host 'Other users still have pyToneRIP; shared queues, forms and deployment files were kept.' }
if ((Split-Path -Leaf $PSScriptRoot) -eq 'Payload') {
    $installerRoot = Split-Path -Parent $PSScriptRoot
    foreach ($file in @('install-msix.log', 'language-setup.log')) {
        $ownedLog = Join-Path $installerRoot $file
        if (Test-Path -LiteralPath $ownedLog -PathType Leaf) { Remove-Item -LiteralPath $ownedLog -Force }
    }
}
$uninstallSucceeded = $true
} finally {
    if ($null -ne $targetUserRoot) { $targetUserRoot.Dispose() }
    Stop-Transcript | Out-Null
    if ($uninstallSucceeded -and (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
        Remove-Item -LiteralPath $LogPath -Force
    }
}
