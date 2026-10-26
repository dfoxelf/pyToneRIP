[CmdletBinding()]
param(
    [string[]]$QueueNames = @(),
    [switch]$CaptureOnly,
    [string]$UserSid
)
$ErrorActionPreference = 'Stop'

function Test-PyToneRipRegistryQueueName {
    param([string]$Name, [string[]]$QueueNames = @())
    if ([string]::IsNullOrWhiteSpace($Name) -or $Name.Contains('\')) { return $false }
    if ($Name -in $QueueNames) { return $true }
    # Exact namespaces used by released queues and the former diagnostic queues.
    return $Name -match '^pyToneRIP(?::\s+| TEST\s+| WinRT watcher probe\s+)\S'
}

function Get-PyToneRipUserRegistryCleanupPlan {
    param([Microsoft.Win32.RegistryKey]$UserRoot, [string[]]$QueueNames = @())
    $plan = @()
    foreach ($path in @(
        'Printers\DevModePerUser', 'Printers\DevModes2', 'Printers\Settings',
        'Software\Microsoft\Windows NT\CurrentVersion\Devices',
        'Software\Microsoft\Windows NT\CurrentVersion\PrinterPorts'
    )) {
        $key = $UserRoot.OpenSubKey($path)
        if ($null -eq $key) { continue }
        try {
            foreach ($name in $key.GetValueNames()) {
                if (Test-PyToneRipRegistryQueueName $name $QueueNames) {
                    $plan += [pscustomobject]@{ Kind='Value'; Path=$path; Name=$name }
                }
            }
        } finally { $key.Dispose() }
    }
    $wizardPath = 'Software\Microsoft\Windows\CurrentVersion\Explorer\PhotoPrintingWizard'
    $wizard = $UserRoot.OpenSubKey($wizardPath)
    if ($null -ne $wizard) {
        try {
            foreach ($name in $wizard.GetSubKeyNames()) {
                if (-not (Test-PyToneRipRegistryQueueName $name $QueueNames)) { continue }
                $child = $wizard.OpenSubKey($name)
                if ($null -eq $child) { continue }
                try {
                    $driver = [string]$child.GetValue('DriverName', '')
                    if ($name -notin $QueueNames -and
                        $driver -ne 'Microsoft Virtual Print Class Driver' -and
                        $driver -notmatch '^pyToneRIP(?:\.|$)') { continue }
                    $plan += [pscustomobject]@{ Kind='Key'; Path=$wizardPath; Name=$name }
                } finally { $child.Dispose() }
            }
            $defaultPrinter = [string]$wizard.GetValue('DefaultPrinter', '')
            if (Test-PyToneRipRegistryQueueName $defaultPrinter $QueueNames) {
                $plan += [pscustomobject]@{ Kind='Value'; Path=$wizardPath; Name='DefaultPrinter' }
            }
        } finally { $wizard.Dispose() }
    }
    $windowsPath = 'Software\Microsoft\Windows NT\CurrentVersion\Windows'
    $windows = $UserRoot.OpenSubKey($windowsPath)
    if ($null -ne $windows) {
        try {
            $device = [string]$windows.GetValue('Device', '')
            $comma = $device.IndexOf(',')
            if ($comma -gt 0 -and (Test-PyToneRipRegistryQueueName $device.Substring(0, $comma) $QueueNames)) {
                $plan += [pscustomobject]@{ Kind='Value'; Path=$windowsPath; Name='Device' }
            }
        } finally { $windows.Dispose() }
    }
    return $plan
}

function Clear-PyToneRipUserRegistryState {
    param([Microsoft.Win32.RegistryKey]$UserRoot, [string[]]$QueueNames = @(), [switch]$CaptureOnly)
    $plan = @(Get-PyToneRipUserRegistryCleanupPlan $UserRoot $QueueNames)
    if ($CaptureOnly) { return $plan }
    $removedValues = 0
    $removedKeys = 0
    foreach ($entry in $plan) {
        $key = $UserRoot.OpenSubKey($entry.Path, $true)
        if ($null -eq $key) { continue }
        try {
            if ($entry.Kind -eq 'Key') {
                # The plan contains only verified per-printer wizard children.
                $key.DeleteSubKeyTree($entry.Name, $false)
                $removedKeys++
            } else {
                $key.DeleteValue($entry.Name, $false)
                $removedValues++
            }
            Write-Host ('Removed print preference: ' + $UserRoot.Name + '\' + $entry.Path + '\' + $entry.Name)
        } finally { $key.Dispose() }
    }
    $remaining = @(Get-PyToneRipUserRegistryCleanupPlan $UserRoot $QueueNames)
    if ($remaining.Count) { throw 'pyToneRIP print preferences remain in the user registry after cleanup.' }
    [pscustomobject]@{ RemovedRegistryKeys=$removedKeys; RemovedRegistryValues=$removedValues }
}

# Importable for isolated registry tests. The uninstall invokes the fixed
# target-user root; Windows owns MSIX registration and spooler-owned keys.
if ($MyInvocation.InvocationName -ne '.') {
    if ($UserSid) {
        $UserSid = [Security.Principal.SecurityIdentifier]::new($UserSid).Value
        $userRoot = [Microsoft.Win32.Registry]::Users.OpenSubKey($UserSid, -not $CaptureOnly)
        if ($null -eq $userRoot) { throw "The target user's registry hive is not loaded: $UserSid" }
        try {
            Clear-PyToneRipUserRegistryState -UserRoot $userRoot -QueueNames $QueueNames -CaptureOnly:$CaptureOnly
        } finally { $userRoot.Dispose() }
    } else {
        Clear-PyToneRipUserRegistryState -UserRoot ([Microsoft.Win32.Registry]::CurrentUser) `
            -QueueNames $QueueNames -CaptureOnly:$CaptureOnly
    }
}
