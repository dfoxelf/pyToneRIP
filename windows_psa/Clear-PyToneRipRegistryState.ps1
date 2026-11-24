[CmdletBinding()]
param(
    [string[]]$QueueNames = @(),
    [switch]$CaptureOnly,
    [string]$UserSid,
    [string[]]$ProtectedQueueNames = @(),
    [switch]$ClearUnscopedSystemCache
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
    param([Microsoft.Win32.RegistryKey]$UserRoot, [string[]]$QueueNames = @(),
        [string]$UserSid, [string[]]$ProtectedQueueNames = @(),
        [switch]$SystemPrintCache, [switch]$ClearUnscopedSystemCache)
    $plan = @()
    $convertPath = 'Printers\ConvertUserDevModesCount'
    $paths = @($convertPath)
    if (-not $SystemPrintCache) {
        $paths += @('Printers\DevModePerUser', 'Printers\DevModes2', 'Printers\Settings')
    }
    if (-not $SystemPrintCache -or $ClearUnscopedSystemCache) {
        $paths += @('Software\Microsoft\Windows NT\CurrentVersion\Devices',
            'Software\Microsoft\Windows NT\CurrentVersion\PrinterPorts')
    }
    foreach ($path in $paths) {
        $key = $UserRoot.OpenSubKey($path)
        if ($null -eq $key) { continue }
        try {
            foreach ($name in $key.GetValueNames()) {
                $queueName = $name
                $ownedEndpoint = $false
                if ($path -eq $convertPath) {
                    if ($key.GetValueKind($name) -ne [Microsoft.Win32.RegistryValueKind]::DWord) { continue }
                    if ($name -match '^(S-1-\d+(?:-\d+)+):(.+)$') {
                        if ($Matches[1] -ne $UserSid) { continue }
                        $queueName = $Matches[2]
                    } else {
                        # The spooler may truncate the SID suffix in this counter name.
                        # Match the exact publisher and private model/instance identifier.
                        if ($name -match '^pyToneRIP\.(?:Manager|PSA|Printer\.[^_\\]+)_\d+(?:\.\d+){3}_(?:x64|x86|arm64|neutral)__qg4mjaqw0qctt_pytone-rip:[a-z0-9._-]+/[a-f0-9]{32}(?:_(S-1-\d+(?:-\d+)*-?))?$') {
                            if ($Matches.ContainsKey(1) -and $Matches[1] -ne $UserSid) {
                                if (($SystemPrintCache -and -not $ClearUnscopedSystemCache) -or
                                    -not $UserSid -or -not $UserSid.StartsWith($Matches[1], [StringComparison]::Ordinal)) { continue }
                            }
                            $ownedEndpoint = $true
                        }
                        if ($SystemPrintCache -and -not $ClearUnscopedSystemCache -and
                            (-not $ownedEndpoint -or -not $Matches.ContainsKey(1))) { continue }
                    }
                }
                if ($queueName -in $ProtectedQueueNames) { continue }
                if ($ownedEndpoint -or (Test-PyToneRipRegistryQueueName $queueName $QueueNames)) {
                    $plan += [pscustomobject]@{ Kind='Value'; Path=$path; Name=$name }
                }
            }
        } finally { $key.Dispose() }
    }
    if ($SystemPrintCache) { return $plan }
    $wizardPath = 'Software\Microsoft\Windows\CurrentVersion\Explorer\PhotoPrintingWizard'
    $wizard = $UserRoot.OpenSubKey($wizardPath)
    if ($null -ne $wizard) {
        try {
            foreach ($name in $wizard.GetSubKeyNames()) {
                if ($name -in $ProtectedQueueNames -or -not (Test-PyToneRipRegistryQueueName $name $QueueNames)) { continue }
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
            if ($defaultPrinter -notin $ProtectedQueueNames -and (Test-PyToneRipRegistryQueueName $defaultPrinter $QueueNames)) {
                $plan += [pscustomobject]@{ Kind='Value'; Path=$wizardPath; Name='DefaultPrinter' }
            }
        } finally { $wizard.Dispose() }
    }
    $windowsPath = 'Software\Microsoft\Windows NT\CurrentVersion\Windows'
    $windows = $UserRoot.OpenSubKey($windowsPath)
    if ($null -ne $windows) {
        try {
            $device = [string]$windows.GetValue('Device', '')
            $printer = $device -replace ',winspool,.*$', ''
            if ($printer -ne $device -and $printer -notin $ProtectedQueueNames -and
                (Test-PyToneRipRegistryQueueName $printer $QueueNames)) {
                $plan += [pscustomobject]@{ Kind='Value'; Path=$windowsPath; Name='Device' }
            }
        } finally { $windows.Dispose() }
    }
    return $plan
}

function Clear-PyToneRipUserRegistryState {
    param([Microsoft.Win32.RegistryKey]$UserRoot, [string[]]$QueueNames = @(), [switch]$CaptureOnly,
        [string]$UserSid, [string[]]$ProtectedQueueNames = @(),
        [switch]$SystemPrintCache, [switch]$ClearUnscopedSystemCache)
    $scope = @{ UserRoot=$UserRoot; QueueNames=$QueueNames; UserSid=$UserSid; ProtectedQueueNames=$ProtectedQueueNames
        SystemPrintCache=$SystemPrintCache; ClearUnscopedSystemCache=$ClearUnscopedSystemCache }
    $plan = @(Get-PyToneRipUserRegistryCleanupPlan @scope)
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
    $remaining = @(Get-PyToneRipUserRegistryCleanupPlan @scope)
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
            $scope = @{ QueueNames=$QueueNames; UserSid=$UserSid; ProtectedQueueNames=$ProtectedQueueNames
                CaptureOnly=$CaptureOnly }
            $userResult = Clear-PyToneRipUserRegistryState -UserRoot $userRoot @scope
            $systemRoot = [Microsoft.Win32.Registry]::Users.OpenSubKey('.DEFAULT', -not $CaptureOnly)
            if ($null -eq $systemRoot) { throw 'The system print cache hive is unavailable.' }
            try {
                $systemResult = Clear-PyToneRipUserRegistryState -UserRoot $systemRoot @scope `
                    -SystemPrintCache -ClearUnscopedSystemCache:$ClearUnscopedSystemCache
            } finally { $systemRoot.Dispose() }
            if ($CaptureOnly) { $userResult; $systemResult }
            else {
                [pscustomobject]@{
                    RemovedRegistryKeys=$userResult.RemovedRegistryKeys + $systemResult.RemovedRegistryKeys
                    RemovedRegistryValues=$userResult.RemovedRegistryValues + $systemResult.RemovedRegistryValues
                }
            }
        } finally { $userRoot.Dispose() }
    } else {
        $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        Clear-PyToneRipUserRegistryState -UserRoot ([Microsoft.Win32.Registry]::CurrentUser) `
            -QueueNames $QueueNames -CaptureOnly:$CaptureOnly -UserSid $currentSid `
            -ProtectedQueueNames $ProtectedQueueNames
    }
}
