param(
    [string]$LocalStatePath,
    [string]$InstallerRoot,
    [string]$ManagerLogPath,
    [string]$OutputDirectory,
    [string]$ResultPath,
    [ValidateRange(1, 30)][int]$LookbackDays = 1,
    [string]$PrintProbeQueueName,
    [string]$PrintProbeOutputPath
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.IO.Compression
$reportStartTime = (Get-Date).AddDays(-$LookbackDays)

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = [Environment]::GetFolderPath('DesktopDirectory')
}
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    throw 'The current user desktop directory could not be found.'
}

# The installer copy of this script can still run when the MSIX was removed.
if ([string]::IsNullOrWhiteSpace($InstallerRoot)) {
    $candidateRoots = @(
        (Join-Path $env:LOCALAPPDATA 'Programs/pyToneRIP Setup'),
        (Join-Path $env:ProgramFiles 'pyToneRIP Setup'),
        (Split-Path -Parent $PSScriptRoot)
    )
    $InstallerRoot = $candidateRoots | Where-Object { Test-Path -LiteralPath (Join-Path $_ 'Payload') } |
        Select-Object -First 1
}
if ([string]::IsNullOrWhiteSpace($LocalStatePath)) {
    $packageRoot = Join-Path $env:LOCALAPPDATA 'Packages'
    $packageFolder = Get-ChildItem -LiteralPath $packageRoot -Directory -Filter 'pyToneRIP.Manager_*' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $packageFolder) {
        $LocalStatePath = Join-Path $packageFolder.FullName 'LocalState'
    }
}

function Join-ExistingRoot([string]$root, [string]$relative) {
    if ([string]::IsNullOrWhiteSpace($root)) { return $null }
    return Join-Path $root $relative
}

function Get-ErrorCodes([string]$line) {
    return @([regex]::Matches($line, '(?i)\b0x[0-9a-f]{8}\b') |
        ForEach-Object { $_.Value.ToUpperInvariant() } | Select-Object -Unique)
}

function Get-CodeDescription([string]$code) {
    switch ($code.ToUpperInvariant()) {
        '0XC0000005' { return 'Access violation' }
        '0XC0000094' { return 'Integer division by zero' }
        '0XC000041D' { return 'Unhandled exception in a UI callback' }
        '0XC000027B' { return 'Stowed application exception' }
        '0X80070005' { return 'Access denied' }
        '0X80070002' { return 'File not found' }
        '0X80070057' { return 'Invalid argument' }
        '0X80004005' { return 'Unspecified failure' }
        '0X800705B4' { return 'Operation timed out' }
        '0X8000FFFF' { return 'Unexpected failure' }
        default { return $null }
    }
}

function Get-TechnicalContext([string]$line) {
    # Export only fixed diagnostic labels and constrained setting values.
    # Arbitrary exception text can contain document titles and private paths.
    $labels = [System.Collections.Generic.List[string]]::new()
    foreach ($item in @(
        @{ Pattern = 'PDC callback|PDC/PDR callback|PrintSupportExtension'; Text = 'Print capabilities callback' },
        @{ Pattern = 'PrintSupportExtension Run entered'; Text = 'Print extension activation entered' },
        @{ Pattern = 'PrintSupportExtension session started'; Text = 'Print extension session started' },
        @{ Pattern = 'PrintSupportExtension session is unavailable'; Text = 'Print extension session unavailable' },
        @{ Pattern = 'PrintSupportExtension cancellation requested'; Text = 'Print extension cancelled' },
        @{ Pattern = 'PDC callback entered'; Text = 'PDC callback entered' },
        @{ Pattern = 'PDC callback completed'; Text = 'PDC callback completed' },
        @{ Pattern = 'PrintTicket validation|Default PrintTicket validation'; Text = 'PrintTicket validation' },
        @{ Pattern = 'PrintTicket validation begin'; Text = 'PrintTicket validation request begin' },
        @{ Pattern = 'PrintTicket validation returned'; Text = 'PrintTicket validation request returned' },
        @{ Pattern = 'PrintTicket validation threw'; Text = 'PrintTicket validation request threw' },
        @{ Pattern = 'PrintTicket validation completed:\s*Resolved'; Text = 'PrintTicket callback completed: Resolved' },
        @{ Pattern = 'User defaults|DEVMODE|SetUserDefaults|VerifyUserDefaults'; Text = 'Windows printer defaults / DEVMODE' },
        @{ Pattern = 'Print settings session'; Text = 'Print settings activation context' },
        @{ Pattern = 'WM_DEVMODECHANGE'; Text = 'Printer default settings notification' },
        @{ Pattern = 'VirtualPrinterDataAvailable|VirtualPrinterBackgroundTask'; Text = 'Virtual printer workflow' },
        @{ Pattern = 'XpsToTiff'; Text = 'Windows XPS to TIFF conversion' },
        @{ Pattern = 'Could not read the current DEVMODE'; Text = 'Could not read the current DEVMODE' },
        @{ Pattern = 'printer driver rejected the DEVMODE'; Text = 'Printer driver rejected the DEVMODE' },
        @{ Pattern = 'did not persist|changed the requested'; Text = 'Requested printer settings were not preserved' },
        @{ Pattern = 'RAW TCP|printer disconnected|printer closed|stopped accepting'; Text = 'Printer data transmission failure' },
        @{ Pattern = 'RIP worker|Packaged Python RIP'; Text = 'RIP worker' }
    )) {
        if ([regex]::IsMatch($line, $item.Pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)) {
            $labels.Add($item.Text)
        }
    }
    foreach ($field in [regex]::Matches($line,
        '(?i)\b(page|readback|orientation|raster|status|model|stage|dpi|resolution|media|input|launch|delivered|validated|elapsedMs|refreshPolicy)\s*=\s*([A-Za-z0-9_.:+-]{1,96})(?=[;\s]|$)')) {
        $labels.Add($field.Groups[1].Value + '=' + $field.Groups[2].Value)
    }
    $cancellation = [regex]::Match($line,
        '(?i)PrintSupportExtension cancellation requested:\s*([A-Za-z]{1,48})\b')
    if ($cancellation.Success) { $labels.Add('cancellationReason=' + $cancellation.Groups[1].Value) }
    $stackMethod = [regex]::Match($line, '\bat\s+((?:pyToneRIP|Windows|System)\.[A-Za-z0-9_.+`]+)\s*\(')
    if ($stackMethod.Success) { $labels.Add('method=' + $stackMethod.Groups[1].Value) }
    return $labels -join '; '
}

function Get-TechnicalSummary([string]$path) {
    if ([System.IO.File]::GetLastWriteTime($path) -lt $reportStartTime) {
        return 'Source log was not modified during the reporting period.'
    }
    $lines = 0
    $selectedLines = 0
    $errors = 0
    $warnings = 0
    $records = [System.Collections.Generic.Queue[string]]::new()
    $timeline = [System.Collections.Generic.Queue[string]]::new()
    $knownExceptions = @(
        'COMException', 'Win32Exception', 'IOException', 'FileNotFoundException',
        'UnauthorizedAccessException', 'TimeoutException', 'InvalidOperationException',
        'OperationCanceledException', 'TaskCanceledException', 'AggregateException',
        'InvalidDataException', 'ArgumentException', 'FormatException', 'NotSupportedException',
        'NullReferenceException', 'IndexOutOfRangeException', 'AccessViolationException',
        'DivideByZeroException', 'ObjectDisposedException'
    )
    $inputStream = [System.IO.File]::Open($path, [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
        $truncated = $inputStream.Length -gt 8MB
        if ($truncated) { [void]$inputStream.Seek(-8MB, [System.IO.SeekOrigin]::End) }
        $reader = [System.IO.StreamReader]::new($inputStream, [System.Text.Encoding]::UTF8,
            $true, 4096, $true)
        try {
            if ($truncated) { [void]$reader.ReadLine() }
            $includeRecord = $true
            while ($null -ne ($line = $reader.ReadLine())) {
                $lines++
                $time = [regex]::Match($line, '\b\d{4}-\d{2}-\d{2}T[\d:.+-]+').Value
                $parsedTime = [DateTimeOffset]::MinValue
                if ($time -and [DateTimeOffset]::TryParse($time, [ref]$parsedTime)) {
                    $includeRecord = $parsedTime.LocalDateTime -ge $reportStartTime
                }
                if (-not $includeRecord) { continue }
                $selectedLines++
                $category = 'Other'
                if ($line -match '(?i)\b(error|failed|failure|exception|fatal|abort|cannot|could not)\b') {
                    $category = 'Error'
                    $errors++
                }
                elseif ($line -match '(?i)\b(warning|warn)\b') {
                    $category = 'Warning'
                    $warnings++
                }
                $codes = @(Get-ErrorCodes $line)
                $types = @($knownExceptions | Where-Object { $line -match ('(?i)\b' + $_ + '\b') })
                $context = Get-TechnicalContext $line
                $descriptions = @($codes | ForEach-Object { Get-CodeDescription $_ })
                if ($category -ne 'Other' -or $codes.Count -gt 0 -or $types.Count -gt 0 -or $context) {
                    $record = 'time={0}; line={1}; category={2}; codes={3}; types={4}; context={5}; meaning={6}' -f
                        $time, $lines, $category, ($codes -join ','), ($types -join ','), $context, ($descriptions -join ',')
                    $timeline.Enqueue($record)
                    if ($timeline.Count -gt 100) { [void]$timeline.Dequeue() }
                }
                if ($category -ne 'Other' -or $types.Count -gt 0 -or $context -like '*method=*') {
                    $records.Enqueue($record)
                    if ($records.Count -gt 200) { [void]$records.Dequeue() }
                }
            }
        }
        finally { $reader.Dispose() }
    }
    finally { $inputStream.Dispose() }
    $summary = [System.Text.StringBuilder]::new()
    [void]$summary.AppendLine('Technical summary only. Original log text is excluded.')
    [void]$summary.AppendLine("Scanned lines: $lines; selected lines: $selectedLines; error lines: $errors; warning lines: $warnings; last records: $($records.Count)")
    [void]$summary.AppendLine('Undated lines follow the preceding dated record; undated logs use their last modification time.')
    [void]$summary.AppendLine("Only the last 8 MiB were scanned: $truncated")
    [void]$summary.AppendLine('ERRORS AND WARNINGS:')
    if ($records.Count -eq 0) { [void]$summary.AppendLine('No error or warning was detected in this application log. Check windows-events.txt for external application crashes.') }
    foreach ($record in $records) { [void]$summary.AppendLine($record) }
    [void]$summary.AppendLine('TECHNICAL TIMELINE:')
    foreach ($record in $timeline) { [void]$summary.AppendLine($record) }
    return $summary.ToString()
}

function Protect-DiagnosticText([string]$text) {
    # Keep exception messages and stack frames, replacing private values in place.
    # Version numbers, HRESULTs, method names and source line numbers are diagnostic data.
    $text = [regex]::Replace($text, '(?i)(["''])(?:[a-z]:[\\/]|\\\\)[^\r\n]*?\1(?=\s|[),;:]|$)', '<path>')
    $text = [regex]::Replace($text,
        '(?im)(?:[a-z]:[\\/]|\\\\)[^\r\n]*?(?=:line\s+\d+|\s+(?:HRESULT|error|elapsedMs|validated)\s*=|$)', '<path>')
    $text = [regex]::Replace($text, '(?i)\b(?:https?|ipp|ipps|socket|tcp)://[^\s<>"'']+', '<endpoint>')
    $text = [regex]::Replace($text, '(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', {
        param($match)
        $address = $null
        $prefix = $text.Substring([Math]::Max(0, $match.Index - 48), [Math]::Min(48, $match.Index))
        if ($prefix -match '(?i)\b(?:version|build|release|CLR|assembly)\s*[:=]?\s*$') { return $match.Value }
        if ([System.Net.IPAddress]::TryParse($match.Value, [ref]$address)) { return '<ip>' }
        return $match.Value
    })
    $text = [regex]::Replace($text, '(?i)(?<![\w:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:.]*(?:%[\w.-]+)?(?![\w:])', {
        param($match)
        $address = $null
        if ([System.Net.IPAddress]::TryParse($match.Value, [ref]$address) -and
            $address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) { return '<ip>' }
        return $match.Value
    })
    $text = [regex]::Replace($text, '(?i)(["''])[^\r\n]*?\.(?:tiff?|jpe?g|png|quad|pdf|psd|psb|bmp|o?xps)\1', '<document>')
    $text = [regex]::Replace($text, '(?i)(?<![\w.])[^\s<>"''=|]+\.(?:tiff?|jpe?g|png|quad|pdf|psd|psb|bmp|o?xps)\b', '<document>')
    return $text
}

function Get-ErrorDetails([string]$path) {
    if ([System.IO.File]::GetLastWriteTime($path) -lt $reportStartTime) {
        return 'Source log was not modified during the reporting period.'
    }
    $records = [System.Collections.Generic.Queue[string]]::new()
    $block = [System.Text.StringBuilder]::new()
    $includeRecord = $true
    $selectedErrors = 0
    $inputStream = [System.IO.File]::Open($path, [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
        $truncated = $inputStream.Length -gt 8MB
        if ($truncated) { [void]$inputStream.Seek(-8MB, [System.IO.SeekOrigin]::End) }
        $reader = [System.IO.StreamReader]::new($inputStream, [System.Text.Encoding]::UTF8, $true, 4096, $true)
        try {
            if ($truncated) { [void]$reader.ReadLine() }
            # A null sentinel flushes the last multiline exception as one record.
            do {
                $line = $reader.ReadLine()
                $recordStart = $null -ne $line -and ($line -match '^\s*\d{4}-\d{2}-\d{2}T' -or $line -match '^[^|]+\s\|\s')
                if ($recordStart -or $null -eq $line) {
                    $record = $block.ToString()
                    if ($includeRecord -and $record -match '(?i)\b(error|failed|failure|fatal|abort|cannot|could not|timed? out|timeout|[\w.]*Exception)\b|\b0x(?!00000000\b)[0-9a-f]{8}\b') {
                        $selectedErrors++
                        $records.Enqueue((Protect-DiagnosticText $record))
                        if ($records.Count -gt 200) { [void]$records.Dequeue() }
                    }
                    [void]$block.Clear()
                    $time = [regex]::Match([string]$line, '^\s*\d{4}-\d{2}-\d{2}T[\d:.+Z-]+').Value
                    $parsedTime = [DateTimeOffset]::MinValue
                    if ($time -and [DateTimeOffset]::TryParse($time, [ref]$parsedTime)) {
                        $includeRecord = $parsedTime.LocalDateTime -ge $reportStartTime
                    }
                }
                if ($null -ne $line) { [void]$block.AppendLine($line) }
            } while ($null -ne $line)
        }
        finally { $reader.Dispose() }
    }
    finally { $inputStream.Dispose() }
    $details = [System.Text.StringBuilder]::new()
    [void]$details.AppendLine('Error records with original messages, HRESULTs, inner exceptions and stack traces when present in the source log.')
    [void]$details.AppendLine('Paths, document/profile filenames and IP/URL endpoints are replaced; messages are not reduced to fixed labels.')
    [void]$details.AppendLine("Only the last 8 MiB were scanned: $truncated; matching records: $selectedErrors; exported last records: $($records.Count); limit: 200.")
    [void]$details.AppendLine('Undated lines follow the preceding dated record; undated logs use their last modification time.')
    if ($records.Count -eq 0) { [void]$details.AppendLine('No matching error records were found in this source log.') }
    foreach ($record in $records) {
        [void]$details.AppendLine('--- ERROR RECORD ---')
        [void]$details.Append($record)
    }
    return $details.ToString()
}

function Get-PrintApiEnvironment {
    $summary = [System.Text.StringBuilder]::new()
    [void]$summary.AppendLine('Read-only OS and print API snapshot. API presence does not prove that an operation succeeds.')
    try {
        $version = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop
        foreach ($field in @('EditionID', 'DisplayVersion', 'CurrentBuild', 'UBR')) {
            $value = [string]$version.$field
            if ($value -match '^[A-Za-z0-9_. -]{1,64}$') {
                [void]$summary.AppendLine($field + '=' + $value)
            }
        }
        if ([string]$version.CurrentBuild -match '^\d{4,6}$' -and [string]$version.UBR -match '^\d{1,8}$') {
            [void]$summary.AppendLine('Full OS build=' + $version.CurrentBuild + '.' + $version.UBR)
        }
    }
    catch { [void]$summary.AppendLine('OS registry version unavailable; exception=' + $_.Exception.GetBaseException().GetType().Name) }
    try {
        [void]$summary.AppendLine('OS architecture=' + [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture)
        [void]$summary.AppendLine('Process architecture=' + [System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture)
    }
    catch { [void]$summary.AppendLine('Architecture details unavailable; process64=' + [Environment]::Is64BitProcess) }
    foreach ($packageName in @('pyToneRIP.Manager', 'MicrosoftWindows.Client.CBS')) {
        try {
            foreach ($package in @(Get-AppxPackage -Name $packageName -ErrorAction Stop)) {
                [void]$summary.AppendLine('Package=' + $packageName + '; version=' + $package.Version + '; architecture=' + $package.Architecture)
            }
        }
        catch { [void]$summary.AppendLine('Package=' + $packageName + '; version unavailable; exception=' + $_.Exception.GetBaseException().GetType().Name) }
    }
    $systemDirectory = if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        Join-Path $env:SystemRoot 'Sysnative'
    } else { Join-Path $env:SystemRoot 'System32' }
    foreach ($fileName in @(
        'Print.PrintSupport.Source.dll', 'Print.Workflow.Source.dll', 'PrintWorkflowService.dll',
        'Windows.Graphics.Printing.Workflow.dll', 'Windows.Graphics.Printing.Workflow.Native.dll',
        'PrintPlatformConfig.dll', 'ModernPrintConfigHelper.dll', 'prntvpt.dll', 'spoolss.dll',
        'spoolsv.exe', 'winspool.drv'
    )) {
        try {
            $path = Join-Path $systemDirectory $fileName
            if ([System.IO.File]::Exists($path)) {
                $version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($path)
                [void]$summary.AppendLine(('Component={0}; version={1}.{2}.{3}.{4}' -f $fileName,
                    $version.FileMajorPart, $version.FileMinorPart, $version.FileBuildPart, $version.FilePrivatePart))
            } else { [void]$summary.AppendLine('Component=' + $fileName + '; not present') }
        }
        catch { [void]$summary.AppendLine('Component=' + $fileName + '; version unavailable; exception=' + $_.Exception.GetBaseException().GetType().Name) }
    }
    try {
        $api = [Windows.Foundation.Metadata.ApiInformation,Windows.Foundation,ContentType=WindowsRuntime]
        foreach ($major in @(19, 20)) {
            [void]$summary.AppendLine('UniversalApiContract.' + $major + '=' +
                $api::IsApiContractPresent('Windows.Foundation.UniversalApiContract', $major))
        }
        $checks = @(
            @{ Kind='Method'; Type='Windows.Devices.Printers.VirtualPrinterManager'; Name='InstallVirtualPrinterAsync'; Count=1 },
            @{ Kind='Method'; Type='Windows.Devices.Printers.VirtualPrinterManager'; Name='InstallVirtualPrinterAsync'; Count=2 },
            @{ Kind='Method'; Type='Windows.Devices.Printers.VirtualPrinterManager'; Name='FindAllVirtualPrinters'; Count=1 },
            @{ Kind='Method'; Type='Windows.Devices.Printers.VirtualPrinterManager'; Name='RemoveVirtualPrinterAsync'; Count=1 },
            @{ Kind='Method'; Type='Windows.Devices.Printers.IppPrintDevice'; Name='FromPrinterName'; Count=1 },
            @{ Kind='Property'; Type='Windows.Devices.Printers.IppPrintDevice'; Name='CanModifyUserDefaultPrintTicket' },
            @{ Kind='WriteableProperty'; Type='Windows.Devices.Printers.IppPrintDevice'; Name='UserDefaultPrintTicket' },
            @{ Kind='Method'; Type='Windows.Devices.Printers.IppPrintDevice'; Name='RefreshPrintDeviceCapabilities'; Count=0 },
            @{ Kind='Method'; Type='Windows.Graphics.Printing.PrintTicket.WorkflowPrintTicket'; Name='ValidateAsync'; Count=0 },
            @{ Kind='Method'; Type='Windows.Graphics.Printing.PrintSupport.PrintSupportSettingsUISession'; Name='UpdatePrintTicket'; Count=1 },
            @{ Kind='Method'; Type='Windows.Graphics.Printing.PrintSupport.PrintSupportExtensionSession'; Name='Start'; Count=0 },
            @{ Kind='Event'; Type='Windows.Graphics.Printing.PrintSupport.PrintSupportExtensionSession'; Name='PrintTicketValidationRequested' },
            @{ Kind='Event'; Type='Windows.Graphics.Printing.PrintSupport.PrintSupportExtensionSession'; Name='PrintDeviceCapabilitiesChanged' },
            @{ Kind='Method'; Type='Windows.Graphics.Printing.PrintSupport.PrintSupportPrintTicketValidationRequestedEventArgs'; Name='GetDeferral'; Count=0 },
            @{ Kind='Method'; Type='Windows.Graphics.Printing.PrintSupport.PrintSupportPrintTicketValidationRequestedEventArgs'; Name='SetPrintTicketValidationStatus'; Count=1 },
            @{ Kind='Method'; Type='Windows.Graphics.Printing.PrintSupport.PrintSupportPrintDeviceCapabilitiesChangedEventArgs'; Name='SetPrintDeviceCapabilitiesUpdatePolicy'; Count=1 },
            @{ Kind='Method'; Type='Windows.Graphics.Printing.PrintSupport.PrintSupportPrintDeviceCapabilitiesUpdatePolicy'; Name='CreatePeriodicRefresh'; Count=1 }
        )
        foreach ($check in $checks) {
            $present = switch ($check.Kind) {
                'Method' { $api::IsMethodPresent($check.Type, $check.Name, [uint32]$check.Count) }
                'Property' { $api::IsPropertyPresent($check.Type, $check.Name) }
                'WriteableProperty' { $api::IsWriteablePropertyPresent($check.Type, $check.Name) }
                'Event' { $api::IsEventPresent($check.Type, $check.Name) }
            }
            $arity = if ($check.Kind -eq 'Method') { '/' + $check.Count } else { '' }
            [void]$summary.AppendLine('API=' + $check.Type + '.' + $check.Name + $arity + '; present=' + $present)
        }
    }
    catch { [void]$summary.AppendLine('API metadata probe unavailable; exception=' + $_.Exception.GetBaseException().GetType().Name) }
    return $summary.ToString()
}

function Get-EventTechnicalDetails($eventRecord) {
    [xml]$document = $eventRecord.ToXml()
    $fields = @{}
    foreach ($node in @($document.Event.EventData.Data)) {
        if ($null -ne $node -and $node -is [System.Xml.XmlElement]) {
            $fields[$node.GetAttribute('Name')] = $node.InnerText
        }
    }
    $parts = [System.Collections.Generic.List[string]]::new()
    foreach ($name in @('AppName', 'ModuleName', 'AppVersion', 'ModuleVersion',
        'ExceptionCode', 'FaultingOffset', 'ProcessId', 'ErrorCode', 'HResult',
        'ResultCode', 'Status', 'EventName', 'PackageFullName', 'StatusCode',
        'RequestedSigningLevel', 'ValidatedSigningLevel', 'SigningScenario', 'PolicyId', 'RuleId')) {
        $value = [string]$fields[$name]
        if ($value -and $value -match '^[A-Za-z0-9_.+ -]{1,200}$') {
            $parts.Add($name + '=' + $value)
            if ($name -eq 'ExceptionCode') {
                $code = if ($value -match '^0x') { $value } else { '0x' + $value }
                $description = Get-CodeDescription $code
                if ($description) { $parts.Add('meaning=' + $description) }
            }
        }
    }
    # WER uses P1/P4/P7/P8 for native APPCRASH/BEX reports. Other report types
    # use these parameters differently and must not leak arbitrary string data.
    if ([string]$fields['EventName'] -match '^(APPCRASH|BEX|BEX64|AppHangB1)$') {
        $application = [string]$fields['P1']
        if ($application -match '^[A-Za-z0-9_. -]+\.exe$') { $parts.Add('Application=' + $application) }
    }
    if ([string]$fields['EventName'] -eq 'APPCRASH') {
        foreach ($pair in @(@('P1','Application'), @('P4','FaultModule'), @('P7','ExceptionCode'), @('P8','FaultOffset'))) {
            $value = [string]$fields[$pair[0]]
            if (($pair[0] -in @('P1','P4') -and $value -match '^[A-Za-z0-9_. -]+\.(exe|dll)$') -or
                ($pair[0] -in @('P7','P8') -and $value -match '^(0x)?[0-9a-fA-F]{1,16}$')) {
                $parts.Add($pair[1] + '=' + $value)
            }
        }
    }
    $eventText = [string]$document.Event.EventData.InnerText
    $application = [regex]::Match($eventText, '(?im)^Application:\s*([A-Za-z0-9_. -]+\.exe)\s*$')
    if ($application.Success) { $parts.Add('Application=' + $application.Groups[1].Value) }
    foreach ($exceptionType in [regex]::Matches($eventText, '\b(?:System|Windows|pyToneRIP)\.[A-Za-z0-9_.]+Exception\b')) {
        $parts.Add('ExceptionType=' + $exceptionType.Value)
    }
    $context = Get-TechnicalContext $eventText
    if ($context) { $parts.Add('context=' + $context) }
    return $parts -join '; '
}

function Format-ProbeError($failure) {
    $parts = [System.Collections.Generic.List[string]]::new()
    $errorObject = $failure.Exception
    while ($null -ne $errorObject) {
        $parts.Add(('{0}; HRESULT=0x{1:X8}; message={2}; stack={3}' -f
            $errorObject.GetType().FullName, $errorObject.HResult,
            (Protect-DiagnosticText $errorObject.Message), (Protect-DiagnosticText $errorObject.StackTrace)))
        if ($errorObject -is [System.ComponentModel.Win32Exception]) {
            $parts.Add('NativeErrorCode=' + $errorObject.NativeErrorCode)
        }
        $errorObject = $errorObject.InnerException
    }
    return $parts -join [Environment]::NewLine
}

function Get-PrintQueueProbe([string]$queueName) {
    $results = [ordered]@{}
    $results['context'] = 'Read-only Windows PowerShell probe; not the PSA package identity. CanModify=false here does not prove that the Manager lacks permission. XML is queue configuration, never job/document data.'
    $probeClock = [Diagnostics.Stopwatch]::StartNew()
    function Save-ProbeStage([string]$stage) {
        $results['trace.txt'] += (Get-Date).ToString('o') + '; elapsedMs=' + $probeClock.ElapsedMilliseconds + '; ' + $stage + [Environment]::NewLine
        if (-not [string]::IsNullOrWhiteSpace($PrintProbeOutputPath)) {
            $pending = $PrintProbeOutputPath + '.pending'
            [IO.File]::WriteAllText($pending, ($results | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
            if ([IO.File]::Exists($PrintProbeOutputPath)) { [IO.File]::Replace($pending, $PrintProbeOutputPath, [NullString]::Value) }
            else { [IO.File]::Move($pending, $PrintProbeOutputPath) }
        }
    }
    Save-ProbeStage 'probe begin'
    if (-not ('SupportReportDevModeProbe' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class SupportReportDevModeProbe {
    public static string TracePath;
    static void Trace(string text) {
        if(String.IsNullOrEmpty(TracePath)) return;
        try {
            using(var stream=new System.IO.FileStream(TracePath,System.IO.FileMode.Append,System.IO.FileAccess.Write,System.IO.FileShare.ReadWrite))
            using(var writer=new System.IO.StreamWriter(stream,new System.Text.UTF8Encoding(false)))
                writer.WriteLine(DateTimeOffset.Now.ToString("o")+"; "+text);
        } catch { }
    }
    public static void DisposeObject(object value) {
        IDisposable disposable=value as IDisposable;
        if(disposable!=null) disposable.Dispose();
    }
    [DllImport("winspool.drv", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool OpenPrinterW(string name,out IntPtr printer,IntPtr defaults);
    [DllImport("winspool.drv", SetLastError=true)]
    static extern bool GetPrinterW(IntPtr printer,uint level,IntPtr buffer,uint size,ref uint needed);
    [DllImport("winspool.drv", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern int DocumentPropertiesW(IntPtr window,IntPtr printer,string name,IntPtr output,IntPtr input,uint flags);
    [DllImport("winspool.drv",EntryPoint="EnumFormsW",SetLastError=true)]
    static extern bool EnumForms(IntPtr printer,uint level,IntPtr buffer,uint size,out uint needed,out uint count);
    [StructLayout(LayoutKind.Sequential)] struct ServerDefaults { public IntPtr DataType,Mode; public uint Access; }
    [DllImport("winspool.drv",EntryPoint="OpenPrinterW",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern bool OpenServer(string name,out IntPtr printer,ref ServerDefaults defaults);
    [DllImport("winspool.drv")] static extern bool ClosePrinter(IntPtr printer);
    [StructLayout(LayoutKind.Sequential)] struct Size { public int W,H; }
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int L,T,R,B; }
    [StructLayout(LayoutKind.Sequential)] struct FormInfo {
        public uint Flags; public IntPtr Name; public Size Size; public Rect Area;
        public IntPtr Keyword; public uint StringType; public IntPtr Mui;
        public uint ResourceId; public IntPtr Display; public ushort Language;
    }
    public sealed class Form {
        public string Name,Keyword,MuiFile,DisplayName; public uint Flags,StringType,ResourceId;
        public int WidthMicrons,HeightMicrons; public ushort Language;
    }
    public static Form[] Forms(string name) {
        IntPtr printer;
        bool opened;
        if(name==null) {var defaults=new ServerDefaults {Access=2};opened=OpenServer(null,out printer,ref defaults);}
        else opened=OpenPrinterW(name,out printer,IntPtr.Zero);
        if(!opened) throw new Win32Exception(Marshal.GetLastWin32Error(),"OpenPrinter for EnumForms");
        try {
            uint needed,count; Trace("EnumForms(size) begin");
            var clock=System.Diagnostics.Stopwatch.StartNew();
            bool ok=EnumForms(printer,2,IntPtr.Zero,0,out needed,out count);
            int error=Marshal.GetLastWin32Error();
            Trace("EnumForms(size) return="+ok+"; needed="+needed+"; lastError="+error+"; elapsedMs="+clock.ElapsedMilliseconds);
            if(!ok && error!=122) throw new Win32Exception(error,"EnumForms size");
            if(needed==0) return new Form[0];
            if(needed>16*1024*1024) throw new InvalidOperationException("EnumForms buffer exceeds diagnostic limit");
            IntPtr buffer=Marshal.AllocHGlobal((int)needed);
            try {
                Trace("EnumForms(data) begin");clock.Restart();
                ok=EnumForms(printer,2,buffer,needed,out needed,out count);error=Marshal.GetLastWin32Error();
                Trace("EnumForms(data) return="+ok+"; count="+count+"; lastError="+error+"; elapsedMs="+clock.ElapsedMilliseconds);
                if(!ok) throw new Win32Exception(error,"EnumForms data");
                int stride=Marshal.SizeOf(typeof(FormInfo));
                if((ulong)stride*count>needed) throw new InvalidOperationException("EnumForms count outside buffer");
                var forms=new Form[count];
                for(int i=0;i<count;i++) {
                    var f=(FormInfo)Marshal.PtrToStructure(IntPtr.Add(buffer,i*stride),typeof(FormInfo));
                    forms[i]=new Form {Name=Marshal.PtrToStringUni(f.Name),Keyword=Marshal.PtrToStringAnsi(f.Keyword),
                        MuiFile=f.Mui==IntPtr.Zero?null:System.IO.Path.GetFileName(Marshal.PtrToStringUni(f.Mui)),
                        DisplayName=Marshal.PtrToStringUni(f.Display),Flags=f.Flags,StringType=f.StringType,
                        ResourceId=f.ResourceId,Language=f.Language,WidthMicrons=f.Size.W,HeightMicrons=f.Size.H};
                }
                return forms;
            } finally {Marshal.FreeHGlobal(buffer);}
        } finally {ClosePrinter(printer);}
    }
    static byte[] Copy(IntPtr mode,int available) {
        if(mode==IntPtr.Zero) return null;
        if(available<72) throw new InvalidOperationException("DEVMODE buffer is too short");
        int length=(ushort)Marshal.ReadInt16(mode,68)+(ushort)Marshal.ReadInt16(mode,70);
        if(length<72 || length>available) throw new InvalidOperationException("DEVMODE size is invalid");
        byte[] data=new byte[length]; Marshal.Copy(mode,data,0,length); return data;
    }
    public static byte[] Read(string name,uint level) {
        IntPtr printer;
        if(!OpenPrinterW(name,out printer,IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error(),"OpenPrinter");
        try {
            uint needed=0; GetPrinterW(printer,level,IntPtr.Zero,0,ref needed);
            if(needed<IntPtr.Size) throw new Win32Exception(Marshal.GetLastWin32Error(),"GetPrinter size");
            IntPtr buffer=Marshal.AllocHGlobal((int)needed);
            try {
                if(!GetPrinterW(printer,level,buffer,needed,ref needed)) throw new Win32Exception(Marshal.GetLastWin32Error(),"GetPrinter");
                IntPtr mode=Marshal.ReadIntPtr(buffer);
                if(mode==IntPtr.Zero) return null;
                long offset=mode.ToInt64()-buffer.ToInt64();
                if(offset<0 || offset>needed-72) throw new InvalidOperationException("DEVMODE pointer outside GetPrinter buffer");
                return Copy(mode,(int)(needed-offset));
            } finally {Marshal.FreeHGlobal(buffer);}
        } finally {ClosePrinter(printer);}
    }
    public static byte[] Effective(string name) {
        IntPtr printer;
        if(!OpenPrinterW(name,out printer,IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error(),"OpenPrinter");
        try {
            Trace("DocumentProperties(size, flags=0) begin");
            var clock=System.Diagnostics.Stopwatch.StartNew();
            int size=DocumentPropertiesW(IntPtr.Zero,printer,name,IntPtr.Zero,IntPtr.Zero,0);
            int error=Marshal.GetLastWin32Error();
            Trace("DocumentProperties(size) rawReturn="+size+"; lastError="+error+"; elapsedMs="+clock.ElapsedMilliseconds);
            if(size<72) throw new Win32Exception(error,"DocumentProperties size; rawReturn="+size);
            IntPtr mode=Marshal.AllocHGlobal(size);
            try {
                Trace("DocumentProperties(data, flags=DM_OUT_BUFFER) begin");clock.Restart();
                int result=DocumentPropertiesW(IntPtr.Zero,printer,name,mode,IntPtr.Zero,2);
                error=Marshal.GetLastWin32Error();
                Trace("DocumentProperties(data) rawReturn="+result+"; lastError="+error+"; elapsedMs="+clock.ElapsedMilliseconds);
                if(result!=1) throw new Win32Exception(error,"DocumentProperties DM_OUT_BUFFER; rawReturn="+result);
                return Copy(mode,size);
            } finally {Marshal.FreeHGlobal(mode);}
        } finally {ClosePrinter(printer);}
    }
}
'@
    }
    $traceField = [SupportReportDevModeProbe].GetField('TracePath')
    if ($null -ne $traceField -and -not [string]::IsNullOrWhiteSpace($PrintProbeOutputPath)) {
        $traceField.SetValue($null, $PrintProbeOutputPath + '.native.txt')
    }
    $printingReady = $false
    try { Add-Type -AssemblyName System.Printing, ReachFramework; $printingReady = $true }
    catch { $results['system-printing-load.error.txt'] = Format-ProbeError $_ }
    # Keep global, per-user and application defaults independent: one broken
    # provider path must not prevent collecting the remaining sources.
    foreach ($source in @('global', 'user', 'effective')) {
        Save-ProbeStage "$source DEVMODE begin"
        try {
            $data = switch ($source) {
                'global' { [SupportReportDevModeProbe]::Read($queueName,8) }
                'user' { [SupportReportDevModeProbe]::Read($queueName,9) }
                'effective' { [SupportReportDevModeProbe]::Effective($queueName) }
            }
            if ($null -eq $data) { $results["$source-devmode.txt"] = 'DEVMODE absent'; Save-ProbeStage "$source DEVMODE absent"; continue }
            $data = [byte[]]$data
            $hash = [System.Security.Cryptography.SHA256]::Create()
            try { $digest = [BitConverter]::ToString($hash.ComputeHash($data)).Replace('-','') }
            finally { $hash.Dispose() }
            $results["$source-devmode.txt"] = 'bytes=' + $data.Length + '; publicBytes=' +
                [BitConverter]::ToUInt16($data,68) + '; privateBytes=' + [BitConverter]::ToUInt16($data,70) +
                '; specVersion=' + [BitConverter]::ToUInt16($data,64) + '; driverVersion=' +
                [BitConverter]::ToUInt16($data,66) + '; sha256=' + $digest
            $results["$source-devmode.base64.txt"] = [Convert]::ToBase64String($data)
            Save-ProbeStage "$source DEVMODE captured; ConvertDevModeToPrintTicket begin"
            if ($printingReady) {
                $converter = [System.Printing.Interop.PrintTicketConverter]::new($queueName,1)
                try {
                    $ticket = $converter.ConvertDevModeToPrintTicket($data)
                    try {
                        $stream = $ticket.GetXmlStream()
                        $reader = [System.IO.StreamReader]::new($stream)
                        try { $results["$source-devmode-printticket.xml"] = $reader.ReadToEnd() }
                        finally { $reader.Dispose() }
                    } finally { [SupportReportDevModeProbe]::DisposeObject($ticket) }
                } finally { $converter.Dispose() }
            }
        } catch { $results["$source-devmode.error.txt"] = Format-ProbeError $_ }
        Save-ProbeStage "$source DEVMODE completed"
    }
    if ($printingReady) {
        $server = $null; $queue = $null
        try {
            $server = [System.Printing.LocalPrintServer]::new()
            $queue = $server.GetPrintQueue($queueName)
            foreach ($property in @('DefaultPrintTicket','UserPrintTicket')) {
                Save-ProbeStage "System.Printing $property begin"
                try {
                    $ticket = $queue.$property
                    if ($null -eq $ticket) { $results["$property.txt"] = 'Ticket absent'; Save-ProbeStage "$property absent"; continue }
                    try {
                        $reader = [System.IO.StreamReader]::new($ticket.GetXmlStream())
                        try { $results["$property.xml"] = $reader.ReadToEnd() }
                        finally { $reader.Dispose() }
                    } finally { [SupportReportDevModeProbe]::DisposeObject($ticket) }
                } catch { $results["$property.error.txt"] = Format-ProbeError $_ }
                Save-ProbeStage "System.Printing $property completed"
            }
            Save-ProbeStage 'System.Printing PrintCapabilities begin'
            try {
                $reader = [System.IO.StreamReader]::new($queue.GetPrintCapabilitiesAsXml())
                try { $results['PrintCapabilities.xml'] = $reader.ReadToEnd() }
                finally { $reader.Dispose() }
            } catch { $results['PrintCapabilities.error.txt'] = Format-ProbeError $_ }
        } catch { $results['PrintQueue.error.txt'] = Format-ProbeError $_ }
        finally { if ($null -ne $queue) { $queue.Dispose() }; if ($null -ne $server) { $server.Dispose() } }
    }
    Save-ProbeStage 'Get-PrintConfiguration begin'
    try {
        $configuration = Get-PrintConfiguration -PrinterName $queueName -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($configuration.PrintTicketXML)) { throw 'Get-PrintConfiguration returned no PrintTicketXML' }
        $results['Get-PrintConfiguration.xml'] = [string]$configuration.PrintTicketXML
    } catch { $results['Get-PrintConfiguration.error.txt'] = Format-ProbeError $_ }
    Save-ProbeStage 'IppPrintDevice.FromPrinterName begin'
    try {
        $device = [Windows.Devices.Printers.IppPrintDevice, Windows.Devices.Printers, ContentType=WindowsRuntime]::FromPrinterName($queueName)
        if ($null -eq $device) { throw 'IppPrintDevice.FromPrinterName returned null' }
        foreach ($property in @('CanModifyUserDefaultPrintTicket','DeviceKind')) {
            try { $results["IppPrintDevice-$property.txt"] = [string]$device.$property }
            catch { $results["IppPrintDevice-$property.error.txt"] = Format-ProbeError $_ }
        }
        try {
            Save-ProbeStage 'IppPrintDevice.UserDefaultPrintTicket begin'
            $ticket = $device.UserDefaultPrintTicket
            if ($null -eq $ticket) { throw 'UserDefaultPrintTicket returned null' }
            $results['IppPrintDevice-UserDefaultPrintTicket.xml'] = $ticket.XmlNode.GetXml()
            Save-ProbeStage 'IppPrintDevice.UserDefaultPrintTicket captured; GetCapabilities begin'
            try { $results['IppPrintDevice-PrintCapabilities.xml'] = $ticket.GetCapabilities().XmlNode.GetXml() }
            catch { $results['IppPrintDevice-PrintCapabilities.error.txt'] = Format-ProbeError $_ }
        } catch { $results['IppPrintDevice-UserDefaultPrintTicket.error.txt'] = Format-ProbeError $_ }
    } catch { $results['IppPrintDevice-resolve.error.txt'] = Format-ProbeError $_ }
    Save-ProbeStage 'IppPrintDevice completed; EnumForms begin'
    if ($null -ne [SupportReportDevModeProbe].GetMethod('Forms')) {
        try { $results['forms.json'] = ConvertTo-Json -InputObject @([SupportReportDevModeProbe]::Forms($queueName)) -Depth 5 }
        catch { $results['forms.error.txt'] = Format-ProbeError $_ }
        Save-ProbeStage 'queue EnumForms completed'
        if ($queueName -match '^pyToneRIP(?::| TEST )') {
            try { $results['server-forms.json'] = ConvertTo-Json -InputObject @([SupportReportDevModeProbe]::Forms([NullString]::Value)) -Depth 5 }
            catch { $results['server-forms.error.txt'] = Format-ProbeError $_ }
        }
    }
    Save-ProbeStage 'probe completed'
    return $results
}

# Run provider calls in a disposable read-only child process. A provider hang
# must not freeze the Manager or prevent exporting the other diagnostic logs.
if (-not [string]::IsNullOrWhiteSpace($PrintProbeOutputPath)) {
    try { $probe = Get-PrintQueueProbe $PrintProbeQueueName }
    catch { $probe = @{ 'probe.error.txt' = (Format-ProbeError $_) } }
    [System.IO.File]::WriteAllText($PrintProbeOutputPath, ($probe | ConvertTo-Json -Depth 8), [System.Text.UTF8Encoding]::new($false))
    exit
}

function Add-ReportText($archive, $included, [string]$name, [string]$value) {
    $entry = $archive.CreateEntry($name, [System.IO.Compression.CompressionLevel]::Optimal)
    $writer = [System.IO.StreamWriter]::new($entry.Open(), [System.Text.UTF8Encoding]::new($false))
    try { $writer.Write($value) } finally { $writer.Dispose() }
    $included.Add($name)
}

function Get-UsbStreamingSummary {
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add('USB transport counters only; no document contents, job names or device identifiers.')
    $lines.Add('At most 100 recent job folders are inspected. Queue empty time is host-side waiting, not measured print-head stopping.')
    $jobs = Join-ExistingRoot $LocalStatePath 'jobs'
    if ([string]::IsNullOrWhiteSpace($jobs) -or -not [System.IO.Directory]::Exists($jobs)) {
        $lines.Add('USB streaming counters are not present.')
        return ($lines -join "`r`n")
    }
    $fields = @('queue_capacity_bytes','prefill_target_bytes','block_bytes','produced_bytes',
        'bytes_sent','queue_peak_bytes','prefill_seconds','producer_wait_seconds',
        'queue_empty_waits','queue_empty_seconds','native_write_calls','native_write_seconds',
        'max_native_write_seconds','native_cancel_requests','elapsed_seconds')
    $count = 0
    foreach ($folder in @(Get-ChildItem -LiteralPath $jobs -Directory -ErrorAction SilentlyContinue |
        Where-Object { -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) } |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 100)) {
        $path = Join-Path $folder.FullName 'usb-streaming.json'
        if (-not [System.IO.File]::Exists($path)) { continue }
        try {
            $info = Get-Item -LiteralPath $path -ErrorAction Stop
            if (($info.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or
                $info.Length -gt 16384 -or $info.LastWriteTimeUtc -lt $reportStartTime.ToUniversalTime()) { continue }
            $record = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($record.schema -ne 'pytone-rip.usb-streaming' -or $record.version -ne 1) { continue }
            $count++
            $parts = [System.Collections.Generic.List[string]]::new()
            $parts.Add('USB stream ' + $count + '; updated_utc=' + $info.LastWriteTimeUtc.ToString('O'))
            if ($record.completed -is [bool]) { $parts.Add('completed=' + $record.completed) }
            foreach ($name in $fields) {
                $value = $record.$name
                if (($value -is [int] -or $value -is [long] -or $value -is [double] -or $value -is [decimal]) -and
                    $value -ge 0 -and -not [double]::IsInfinity([double]$value) -and -not [double]::IsNaN([double]$value)) {
                    $parts.Add($name + '=' + [Convert]::ToString($value, [Globalization.CultureInfo]::InvariantCulture))
                }
            }
            $lines.Add(($parts -join '; '))
        } catch { $lines.Add('USB counter record unavailable; exception=' + $_.Exception.GetBaseException().GetType().Name) }
    }
    $lines.Add('USB counter records included: ' + $count)
    return ($lines -join "`r`n")
}

function Get-PrintSecuritySnapshot {
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add('Read-only security/policy snapshot. Missing values mean absent/not configured, not necessarily disabled. A configured protection is not evidence that it caused E_FAIL.')
    $lines.Add('Collector context is Windows PowerShell, not the packaged Manager. Registry state may require a reboot before becoming effective.')
    $selections = @(
        @{ Path='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'; Names=@('EnableLUA','ConsentPromptBehaviorAdmin','ConsentPromptBehaviorUser','PromptOnSecureDesktop','FilterAdministratorToken','EnableVirtualization','ValidateAdminCodeSignatures','EnableSecureUIAPaths') },
        @{ Path='HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Printers\WPP'; Names=@('WindowsProtectedPrintGroupPolicyState') },
        @{ Path='HKLM:\SYSTEM\CurrentControlSet\Control\Print'; Names=@('WindowsProtectedPrintMode','WindowsProtectedPrintGroupPolicyState','RpcAuthnLevelPrivacyEnabled','RestrictDriverInstallationToAdministrators') },
        @{ Path='HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Printers'; Names=@('DisableWebPrinting','DisableHTTPPrinting','DisableHTTPPrintingPolicy','RegisterSpoolerRemoteRpcEndPoint','PrintDriverIsolationExecutionPolicy','PrintDriverIsolationOverrideCompat','KMPrintersAreBlocked') },
        @{ Path='HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Printers\PointAndPrint'; Names=@('RestrictDriverInstallationToAdministrators','Restricted','TrustedServers','NoWarningNoElevationOnInstall','UpdatePromptSettings') },
        @{ Path='HKLM:\SOFTWARE\Policies\Microsoft\Windows\Appx'; Names=@('AllowAllTrustedApps','AllowDevelopmentWithoutDevLicense','BlockNonAdminUserInstall','RestrictAppToSystemVolume') },
        @{ Path='HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore'; Names=@('DisableStoreApps','RemoveWindowsStore') },
        @{ Path='HKCU:\SOFTWARE\Policies\Microsoft\Windows NT\Printers\PointAndPrint'; Names=@('Restricted','TrustedServers','NoWarningNoElevationOnInstall','UpdatePromptSettings') }
    )
    foreach ($selection in $selections) {
        try {
            if (-not (Test-Path -LiteralPath $selection.Path)) { $lines.Add($selection.Path + ': key absent'); continue }
            $values = Get-ItemProperty -LiteralPath $selection.Path -ErrorAction Stop
            foreach ($name in $selection.Names) {
                $property = $values.PSObject.Properties[$name]
                $value = if ($null -eq $property) { '<absent>' } else { [string]$property.Value }
                if ($value -notmatch '^(?:<absent>|[0-9A-Fa-fx -]{1,64})$') { $value = '<non-numeric value>' }
                $lines.Add($selection.Path + '; ' + $name + '=' + $value)
            }
        } catch { $lines.Add((Format-ProbeError $_)) }
    }
    foreach ($collection in @('Exe','Dll','Msi','Script','Appx')) {
        $path = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\SrpV2\' + $collection
        try {
            if (Test-Path -LiteralPath $path) {
                $value = Get-ItemProperty -LiteralPath $path -ErrorAction Stop
                $lines.Add('AppLocker collection=' + $collection + '; EnforcementMode=' + $value.EnforcementMode)
            } else { $lines.Add('AppLocker collection=' + $collection + '; registry policy absent') }
        } catch { $lines.Add((Format-ProbeError $_)) }
    }
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        try {
            $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
            $lines.Add('Collector token administrator=' + $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator))
            $lines.Add('Collector token system=' + $identity.IsSystem + '; impersonation=' + $identity.ImpersonationLevel)
        } finally { $identity.Dispose() }
    } catch { $lines.Add((Format-ProbeError $_)) }
    try {
        foreach ($service in @(Get-Service -Name Spooler,PrintWorkflowUserSvc*,AppXSvc,ClipSVC,StateRepository -ErrorAction Stop)) {
            $name = $service.Name -replace '_[0-9a-fA-F]+$','_<instance>'
            $lines.Add('Service=' + $name + '; status=' + $service.Status + '; startType=' + $service.StartType)
        }
    } catch { $lines.Add('Service snapshot: ' + (Format-ProbeError $_)) }
    try {
        $defender = Get-MpPreference -ErrorAction Stop
        foreach ($name in @('DisableRealtimeMonitoring','DisableBehaviorMonitoring','EnableControlledFolderAccess','PUAProtection','AttackSurfaceReductionRules_Ids','AttackSurfaceReductionRules_Actions')) {
            $lines.Add('Defender ' + $name + '=' + ($defender.$name -join ','))
        }
    } catch { $lines.Add('Defender snapshot: ' + (Format-ProbeError $_)) }
    try {
        foreach ($guard in @(Get-CimInstance -Namespace root\Microsoft\Windows\DeviceGuard -ClassName Win32_DeviceGuard -ErrorAction Stop)) {
            foreach ($name in @('VirtualizationBasedSecurityStatus','CodeIntegrityPolicyEnforcementStatus','UsermodeCodeIntegrityPolicyEnforcementStatus','SecurityServicesConfigured','SecurityServicesRunning')) {
                $lines.Add('DeviceGuard ' + $name + '=' + ($guard.$name -join ','))
            }
        }
    } catch { $lines.Add('DeviceGuard snapshot: ' + (Format-ProbeError $_)) }
    return $lines -join [Environment]::NewLine
}

function Add-PsaPackageDiagnostics($archive, $included, $printers) {
    $text = [Text.StringBuilder]::new()
    [void]$text.AppendLine('Current-user package registration and installed source assets. This does not prove background task activation. No registration, services or preferences are modified.')
    $models = @($printers | ForEach-Object {
        $match = [regex]::Match([string]$_.PortName, '(?i)(?:^|_)pytone-rip:([a-z0-9]+)/')
        if ($match.Success) { $match.Groups[1].Value }
    } | Select-Object -Unique)
    $packageNumber = 0
    try {
        $packages = @(Get-AppxPackage -ErrorAction Stop | Where-Object {
            $_.Name -in @('pyToneRIP.Manager','pyToneRIP.PSA') -or $_.Name -like 'pyToneRIP.Printer.*'
        })
        foreach ($package in $packages) {
            $packageNumber++
            $prefix = "printing/psa-package-$packageNumber"
            [void]$text.AppendLine("Package $packageNumber; name=$($package.Name); version=$($package.Version); status=$($package.Status); family=$($package.PackageFamilyName)")
            foreach ($contract in @('Windows.PrintSupportExtension','Windows.PrintSupportSettingsUI',
                'Windows.PrintSupportVirtualPrinterWorkflow','Windows.BackgroundTasks')) {
                $key = $null
                try {
                    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey(
                        'Software\Classes\Extensions\ContractId\' + $contract + '\PackageId\' + $package.PackageFullName)
                    [void]$text.AppendLine("Package $packageNumber; registryContract=$contract; present=$($null -ne $key)")
                } catch { [void]$text.AppendLine((Format-ProbeError $_)) }
                finally { if ($null -ne $key) { $key.Dispose() } }
            }
            foreach ($logName in @('pyToneRIP-pdc.log','pyToneRIP-workflow.log','pyToneRIP-system-defaults.log')) {
                $log = Join-Path $env:LOCALAPPDATA ('Packages\' + $package.PackageFamilyName + '\LocalState\' + $logName)
                if (Test-Path -LiteralPath $log -PathType Leaf) {
                    $info = Get-Item -LiteralPath $log
                    [void]$text.AppendLine("Package $packageNumber; log=$logName; bytes=$($info.Length); modifiedUtc=$($info.LastWriteTimeUtc.ToString('o'))")
                } else { [void]$text.AppendLine("Package $packageNumber; log=$logName; absent") }
            }
            if ([string]::IsNullOrWhiteSpace($package.InstallLocation)) { continue }
            $root = [IO.Path]::GetFullPath($package.InstallLocation).TrimEnd('\')
            $files = @()
            $manifest = Join-Path $root 'AppxManifest.xml'
            if (Test-Path -LiteralPath $manifest -PathType Leaf) { $files += Get-Item -LiteralPath $manifest }
            $assets = Join-Path $root 'Assets'
            if (Test-Path -LiteralPath $assets -PathType Container) {
                $files += @(Get-ChildItem -LiteralPath $assets -File -Recurse -ErrorAction Stop | Where-Object {
                    $fileName = $_.Name
                    $isModel = @($models | Where-Object { $fileName.IndexOf($_, [StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0
                    ($fileName -match '(?i)\.(pdc|pdr)\.xml$' -and
                        ($isModel -or $fileName -match '^pyToneRIP\.(pdc|pdr)\.xml$'))
                })
            }
            $total = 0
            foreach ($fileInfo in @($files | Sort-Object FullName | Select-Object -First 256)) {
                try {
                    $path = [IO.Path]::GetFullPath($fileInfo.FullName)
                    if (-not $path.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Package asset outside installed package root' }
                    $relative = $path.Substring($root.Length + 1).Replace('\','/')
                    if ($relative -notmatch '^[A-Za-z0-9_./-]+$') { continue }
                    if ($fileInfo.Length -gt 8MB -or $total + $fileInfo.Length -gt 16MB) {
                        [void]$text.AppendLine("Package $packageNumber; asset=$relative; skipped: diagnostic size limit")
                        continue
                    }
                    $bytes = [IO.File]::ReadAllBytes($path)
                    $hash = [Security.Cryptography.SHA256]::Create()
                    try { $digest = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-','') }
                    finally { $hash.Dispose() }
                    $entryName = "$prefix/$relative"
                    $entry = $archive.CreateEntry($entryName, [IO.Compression.CompressionLevel]::Optimal)
                    $stream = $entry.Open()
                    try { $stream.Write($bytes,0,$bytes.Length) } finally { $stream.Dispose() }
                    $included.Add($entryName)
                    $total += $bytes.Length
                    [void]$text.AppendLine("Package $packageNumber; asset=$relative; bytes=$($bytes.Length); sha256=$digest")
                } catch { [void]$text.AppendLine((Format-ProbeError $_)) }
            }
        }
        [void]$text.AppendLine("Packages found: $packageNumber; queue model IDs: $($models -join ',')")
    } catch { [void]$text.AppendLine((Format-ProbeError $_)) }
    Add-ReportText $archive $included 'printing/psa-registration.txt' $text.ToString()
}

function Get-PrintActivationEventDetails($record) {
    $channel = [string]$record.LogName
    $provider = [string]$record.ProviderName
    $id = [int]$record.Id
    $isPrintFailure = $provider -eq 'Microsoft-Windows-PrintService' -and $id -in @(215,370)
    $isServiceFailure = $provider -eq 'Service Control Manager' -and $id -in @(7000,7001,7009,7011,7023,7024,7031,7034)
    $isActivation = $channel -match '(?i)AppModel-Runtime|TWinUI|BackgroundTaskInfrastructure'
    if (-not ($isPrintFailure -or $isServiceFailure -or $isActivation)) { return $null }
    [xml]$document = $record.ToXml()
    if ($isActivation -and $document.DocumentElement.InnerText -notmatch '(?i)pyToneRIP|PrintSupport|PrintWorkflow') { return $null }
    $lines = [Collections.Generic.List[string]]::new()
    $lines.Add(('time={0}; channel={1}; provider={2}; event_id={3}' -f $record.TimeCreated.ToString('o'),$channel,$provider,$id))
    foreach ($node in @($document.SelectNodes('//*[local-name()="EventData"]/*|//*[local-name()="UserData"]//*[not(*)]'))) {
        $name = if ($node.HasAttribute('Name')) { $node.GetAttribute('Name') } else { $node.LocalName }
        # Driver-install and service-failure fields have no job contents.
        # For activation export only technical fields, never command lines.
        if ($isActivation -and $name -notmatch '(?i)^(Package.*|AppUserModelId|ApplicationId|Contract.*|EntryPoint|Task.*|Error.*|HResult|Status.*|Result.*|ProcessId|Service.*|ServerName|Phase)$') { continue }
        $value = Protect-DiagnosticText $node.InnerText
        $value = [regex]::Replace($value, '\bS-1-\d+(?:-\d+)+\b', '<sid>')
        $lines.Add($name + '=' + $value)
    }
    return $lines -join [Environment]::NewLine
}

function Add-PrintProviderDiagnostics($archive, $included) {
    $inventory = [System.Text.StringBuilder]::new()
    [void]$inventory.AppendLine('Read-only diagnostics. No preferences, registry values, media or queues are changed. Queue numbers are local to this report.')
    [void]$inventory.AppendLine('Original queue PrintTicket/PrintCapabilities XML may contain vendor private DEVMODE and printer identifiers. No document/job tickets are read. Software presence is not proof of causation.')
    $printers = @()
    try {
        $printers = @(Get-CimInstance Win32_Printer -ErrorAction Stop | Where-Object {
            $_.PortName -like 'pyToneRIP.Manager_*' -or $_.Name -like 'pyToneRIP*' -or
            $_.DriverName -match '(?i)EPSON' -or $_.Name -match '(?i)\bP900\b'
        } | Sort-Object @{Expression={if ($_.Name -like 'pyToneRIP*') {0} else {1}}}, DriverName, Name)
    } catch { [void]$inventory.AppendLine((Format-ProbeError $_)) }
    [void]$inventory.AppendLine('Relevant queues=' + $printers.Count + '; detailed provider probes limited to first 6 queues: 45 seconds for pyToneRIP, 15 seconds for other queues. Partial snapshots survive a timeout.')
    $number = 0
    foreach ($printer in $printers) {
        $number++
        [void]$inventory.AppendLine("Queue $number; driver=$($printer.DriverName); virtual=$($printer.PortName -like 'pyToneRIP.Manager_*'); default=$($printer.Default); attributes=$($printer.Attributes); status=$($printer.PrinterStatus); workOffline=$($printer.WorkOffline)")
        if ($number -gt 6) { continue }
        $probePath = Join-Path ([System.IO.Path]::GetTempPath()) ('pyToneRIP-print-probe-' + [guid]::NewGuid().ToString('N') + '.json')
        $process = $null
        $timeoutMs = if ($printer.PortName -like 'pyToneRIP.Manager_*' -or $printer.Name -like 'pyToneRIP*') { 45000 } else { 15000 }
        $timedOut = $false
        try {
            $command = "& '" + $PSCommandPath.Replace("'","''") + "' -PrintProbeQueueName '" +
                ([string]$printer.Name).Replace("'","''") + "' -PrintProbeOutputPath '" + $probePath.Replace("'","''") + "'"
            $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($command))
            $process = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -WindowStyle Hidden -PassThru -ArgumentList @(
                '-NoProfile','-NonInteractive','-Sta','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded)
            if (-not $process.WaitForExit($timeoutMs)) {
                $timedOut = $true
                $process.Kill()
                [void]$process.WaitForExit(2000)
                Add-ReportText $archive $included "printing/queue-$number/probe.error.txt" ('Provider probe exceeded ' + ($timeoutMs / 1000) + ' seconds; only the diagnostic child process was terminated. Partial results and native call trace follow when available.')
            }
            if (-not (Test-Path -LiteralPath $probePath) -and -not $timedOut) {
                Add-ReportText $archive $included "printing/queue-$number/probe.error.txt" ('Provider child returned no result; exitCode=' + $process.ExitCode)
            } elseif (Test-Path -LiteralPath $probePath) {
                $result = Get-Content -LiteralPath $probePath -Encoding UTF8 -Raw | ConvertFrom-Json
                foreach ($item in $result.PSObject.Properties) {
                    if ($item.Name -notmatch '^[A-Za-z0-9_.-]+$') { continue }
                    Add-ReportText $archive $included "printing/queue-$number/$($item.Name)" ([string]$item.Value)
                }
            }
        } catch { Add-ReportText $archive $included "printing/queue-$number/probe-export.error.txt" (Format-ProbeError $_) }
        finally {
            if ($null -ne $process) {
                if (-not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(2000) }
                $process.Dispose()
            }
            $nativeTrace = $probePath + '.native.txt'
            if (Test-Path -LiteralPath $nativeTrace) {
                Add-ReportText $archive $included "printing/queue-$number/native-calls.txt" ([IO.File]::ReadAllText($nativeTrace))
            }
            foreach ($temporary in @($probePath, ($probePath + '.pending'), $nativeTrace)) {
                if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -ErrorAction SilentlyContinue }
            }
        }
    }
    try {
        $driverNames = @($printers | ForEach-Object DriverName | Select-Object -Unique)
        foreach ($driver in @(Get-PrinterDriver -ErrorAction Stop | Where-Object { $_.Name -in $driverNames })) {
            [void]$inventory.AppendLine("Driver=$($driver.Name); manufacturer=$($driver.Manufacturer); version=$($driver.DriverVersion); major=$($driver.MajorVersion); environment=$($driver.PrinterEnvironment)")
            $driverFiles = @($driver.DriverPath, $driver.ConfigFile, $driver.DataFile, $driver.InfPath) + @($driver.DependentFiles)
            foreach ($path in @($driverFiles | Where-Object { $_ } | Select-Object -Unique)) {
                try {
                    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
                    $item = Get-Item -LiteralPath $path
                    $digest = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
                    [void]$inventory.AppendLine("DriverFile=$($item.Name); bytes=$($item.Length); version=$($item.VersionInfo.FileVersion); modifiedUtc=$($item.LastWriteTimeUtc.ToString('O')); sha256=$digest")
                } catch { [void]$inventory.AppendLine((Format-ProbeError $_)) }
            }
        }
    } catch { [void]$inventory.AppendLine('Driver inventory: ' + (Format-ProbeError $_)) }
    foreach ($root in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')) {
        try {
            foreach ($key in @(Get-ChildItem -LiteralPath $root -ErrorAction Stop)) {
                $software = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
                if ($software.DisplayName -match '(?i)EPSON|Media Installer') {
                    [void]$inventory.AppendLine("Software=$($software.DisplayName); version=$($software.DisplayVersion); publisher=$($software.Publisher); installDate=$($software.InstallDate)")
                }
            }
        } catch { [void]$inventory.AppendLine('Software inventory: ' + (Format-ProbeError $_)) }
    }
    Add-PsaPackageDiagnostics $archive $included $printers
    Add-ReportText $archive $included 'printing/inventory.txt' $inventory.ToString()
}

function Get-PrinterDefaultsSummary {
    $summary = [System.Text.StringBuilder]::new()
    [void]$summary.AppendLine('Current printer settings snapshot. Queue names and private DEVMODE data are excluded.')
    try {
        if (-not ('SupportReportPrinterDefaults' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class SupportReportPrinterDefaults {
    [DllImport("winspool.drv", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool OpenPrinterW(string name,out IntPtr printer,IntPtr defaults);
    [DllImport("winspool.drv", SetLastError=true)]
    static extern bool GetPrinterW(IntPtr printer,uint level,IntPtr buffer,uint size,ref uint needed);
    [DllImport("winspool.drv")] static extern bool ClosePrinter(IntPtr printer);
    public static string Read(string name,uint level) {
        IntPtr printer;
        if(!OpenPrinterW(name,out printer,IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error());
        try {
            uint needed=0; GetPrinterW(printer,level,IntPtr.Zero,0,ref needed);
            if(needed<IntPtr.Size || Marshal.GetLastWin32Error()!=122) throw new Win32Exception(Marshal.GetLastWin32Error());
            IntPtr buffer=Marshal.AllocHGlobal((int)needed);
            try {
                if(!GetPrinterW(printer,level,buffer,needed,ref needed)) throw new Win32Exception(Marshal.GetLastWin32Error());
                IntPtr mode=Marshal.ReadIntPtr(buffer);
                if(mode==IntPtr.Zero) return "DEVMODE absent";
                if((ushort)Marshal.ReadInt16(mode,68)<166) return "DEVMODE truncated";
                int flags=Marshal.ReadInt32(mode,72);
                string form=Marshal.PtrToStringUni(IntPtr.Add(mode,102),32).TrimEnd('\0');
                if(!System.Text.RegularExpressions.Regex.IsMatch(form,@"^(A[0-9]|B[0-9]|Letter|Legal|Tabloid|Executive|[0-9]+X[0-9]+(?:FULL)?|w[0-9]+h[0-9]+)$",
                    System.Text.RegularExpressions.RegexOptions.IgnoreCase)) form="<custom>";
                return string.Format("fields=0x{0:X8}; paper_id={1}; width_tenth_mm={2}; height_tenth_mm={3}; orientation={4}; dpi={5}x{6}; form={7}; form_name_active={8}; custom_width_active={9}; custom_height_active={10}",
                    flags,Marshal.ReadInt16(mode,78),Marshal.ReadInt16(mode,82),Marshal.ReadInt16(mode,80),
                    Marshal.ReadInt16(mode,76),Marshal.ReadInt16(mode,90),Marshal.ReadInt16(mode,96),form,(flags&0x10000)!=0,(flags&8)!=0,(flags&4)!=0);
            } finally {Marshal.FreeHGlobal(buffer);}
        } finally {ClosePrinter(printer);}
    }
}
'@
        }
        $printers = @(Get-CimInstance Win32_Printer -ErrorAction Stop)
        $defaultPrinter = $printers | Where-Object Default | Select-Object -First 1
        [void]$summary.AppendLine('System default is a pyToneRIP queue: ' +
            ([string]$defaultPrinter.PortName -like 'pyToneRIP.Manager_*' -or [string]$defaultPrinter.Name -like 'pyToneRIP*'))
        $queueNumber = 0
        foreach ($printer in @($printers | Where-Object { $_.PortName -like 'pyToneRIP.Manager_*' -or $_.Name -like 'pyToneRIP*' })) {
            $queueNumber++
            [void]$summary.AppendLine("Queue $queueNumber; is_system_default=$($printer.Default)")
            $model = [regex]::Match([string]$printer.PortName, '_pytone-rip:([a-z0-9]+)/')
            if ($model.Success) { [void]$summary.AppendLine('model=' + $model.Groups[1].Value) }
            foreach ($level in @(8,9)) {
                try { [void]$summary.AppendLine('DEVMODE level ' + $level + ': ' + [SupportReportPrinterDefaults]::Read($printer.Name,$level)) }
                catch { [void]$summary.AppendLine("DEVMODE level $level : unavailable; exception=$($_.Exception.GetBaseException().GetType().Name)") }
            }
        }
        [void]$summary.AppendLine("pyToneRIP queue count: $queueNumber")
        foreach ($package in @(Get-AppxPackage -Name 'pyToneRIP.Manager' -ErrorAction Stop)) {
            [void]$summary.AppendLine('Manager version: ' + $package.Version)
        }
    }
    catch { [void]$summary.AppendLine('Printer snapshot unavailable; exception=' + $_.Exception.GetBaseException().GetType().Name) }
    return $summary.ToString()
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$archivePath = Join-Path $OutputDirectory ("pyToneRIP-logs-$timestamp-$([guid]::NewGuid().ToString('N').Substring(0, 6)).zip")
$sources = @(
    @{ Path = (Join-ExistingRoot $LocalStatePath 'pyToneRIP-startup.log'); Entry = 'application/pyToneRIP-startup.diagnostics.txt' },
    @{ Path = (Join-ExistingRoot $LocalStatePath 'pyToneRIP-pdc.log'); Entry = 'application/pyToneRIP-pdc.diagnostics.txt' },
    @{ Path = (Join-ExistingRoot $LocalStatePath 'pyToneRIP-workflow.log'); Entry = 'application/pyToneRIP-workflow.diagnostics.txt' },
    @{ Path = (Join-ExistingRoot $LocalStatePath 'pyToneRIP-system-defaults.log'); Entry = 'application/pyToneRIP-system-defaults.diagnostics.txt' },
    @{ Path = (Join-ExistingRoot $LocalStatePath 'pyToneRIP-queue-helper.log'); Entry = 'application/pyToneRIP-queue-helper.diagnostics.txt' },
    @{ Path = $ManagerLogPath; Entry = 'application/manager-session.diagnostics.txt' },
    @{ Path = (Join-ExistingRoot $InstallerRoot 'install-msix.log'); Entry = 'installer/install-msix.diagnostics.txt' },
    @{ Path = (Join-ExistingRoot $InstallerRoot 'language-setup.log'); Entry = 'installer/language-setup.diagnostics.txt' },
    @{ Path = (Join-ExistingRoot $InstallerRoot 'Payload/uninstall-cleanup.log'); Entry = 'installer/uninstall-cleanup.diagnostics.txt' }
)

$file = $null
$archive = $null
try {
    $file = [System.IO.File]::Open($archivePath, [System.IO.FileMode]::CreateNew)
    $archive = [System.IO.Compression.ZipArchive]::new($file, [System.IO.Compression.ZipArchiveMode]::Create)
    $included = [System.Collections.Generic.List[string]]::new()
    foreach ($source in $sources) {
      foreach ($format in @('summary', 'details')) {
        $entryName = if ($format -eq 'summary') { $source.Entry } else { $source.Entry.Replace('.diagnostics.txt', '.errors.txt') }
        $entry = $archive.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
        $entryWriter = [System.IO.StreamWriter]::new($entry.Open(), [System.Text.UTF8Encoding]::new($false))
        try {
            if ([string]::IsNullOrWhiteSpace($source.Path) -or -not [System.IO.File]::Exists($source.Path)) {
                $entryWriter.WriteLine('Source log is not present. This is not evidence that no error occurred.')
            } else {
                try {
                    if ($format -eq 'summary') { $entryWriter.Write((Get-TechnicalSummary $source.Path)) }
                    else { $entryWriter.Write((Get-ErrorDetails $source.Path)) }
                }
                catch {
                    $cause = $_.Exception.GetBaseException()
                    $entryWriter.WriteLine('Source log could not be read; exception={0}; HRESULT=0x{1:X8}', $cause.GetType().Name, $cause.HResult)
                }
            }
        }
        finally { $entryWriter.Dispose() }
        $included.Add($entryName)
      }
    }
    $eventText = [System.Text.StringBuilder]::new()
    $activationEvents = [System.Text.StringBuilder]::new()
    [void]$activationEvents.AppendLine('Selected sanitized EventData for print-driver installation, service failures and PSA activation. Missing events do not prove that a task was never activated. Original rendered messages and print job contents are excluded.')
    [void]$eventText.AppendLine("Windows application crashes, hangs and errors, printing and system diagnostics from the last $LookbackDays days. Original event messages are excluded.")
    [void]$eventText.AppendLine('Applications are not restricted to a fixed list. At most 1000 recent records are queried and 100 exported per query.')
    $startTime = $reportStartTime
    $queries = @(
        @{ Channel = 'Application'; Ids = @(1000,1001,1002,1026) },
        @{ Channel = 'Application'; Levels = @(1,2,3) },
        @{ Channel = 'System'; Levels = @(1,2,3) }
    )
    foreach ($channel in @(
        'Microsoft-Windows-AppXDeploymentServer/Operational',
        'Microsoft-Windows-AppModel-Runtime/Admin',
        'Microsoft-Windows-AppModel-Runtime/Operational',
        'Microsoft-Windows-BackgroundTaskInfrastructure/Operational',
        'Microsoft-Windows-TWinUI/Operational',
        'Microsoft-Windows-PrintService/Admin',
        'Microsoft-Windows-PrintService/Operational',
        'Microsoft-Windows-CodeIntegrity/Operational',
        'Microsoft-Windows-AppLocker/EXE and DLL',
        'Microsoft-Windows-AppLocker/Packaged app-Execution',
        'Microsoft-Windows-AppLocker/Packaged app-Deployment',
        'Microsoft-Windows-Windows Defender/Operational'
    )) { $queries += @{ Channel = $channel } }
    $exportedRecords = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($query in $queries) {
        $channel = $query.Channel
        $filter = @{ LogName = $channel; StartTime = $startTime }
        if ($query.Ids) { $filter.Id = $query.Ids }
        if ($query.Levels) { $filter.Level = $query.Levels }
        try {
            $matchedEventCount = 0
            $queried = @(Get-WinEvent -FilterHashtable $filter -MaxEvents 1000 -ErrorAction Stop)
            foreach ($eventRecord in $queried) {
                if (-not $exportedRecords.Add($channel + ':' + $eventRecord.RecordId)) { continue }
                try {
                    $details = Get-EventTechnicalDetails $eventRecord
                    $eventKind = if ($channel -eq 'Application') { switch ([int]$eventRecord.Id) {
                        1000 { 'Application crash' }
                        1001 { 'Windows Error Reporting' }
                        1002 { 'Application hang' }
                        1026 { 'Unhandled .NET exception' }
                        default { 'Windows diagnostic event' }
                    } } else { 'Windows diagnostic event' }
                    [void]$eventText.AppendLine(('time={0}; channel={1}; provider={2}; event_id={3}; level={4}; kind={5}; {6}' -f
                        $eventRecord.TimeCreated.ToString('O'), $channel, $eventRecord.ProviderName,
                        [int]$eventRecord.Id, [int]$eventRecord.Level, $eventKind, $details))
                    try {
                        $activation = Get-PrintActivationEventDetails $eventRecord
                        if ($activation) { [void]$activationEvents.AppendLine($activation + [Environment]::NewLine) }
                    } catch { [void]$activationEvents.AppendLine((Format-ProbeError $_)) }
                    $matchedEventCount++
                    if ($matchedEventCount -ge 100) { break }
                }
                catch {
                    [void]$eventText.AppendLine(('channel={0}; event_id={1}; details unavailable; exception={2}' -f
                        $channel, [int]$eventRecord.Id, $_.Exception.GetBaseException().GetType().Name))
                }
            }
            [void]$eventText.AppendLine("$channel : queried=$($queried.Count); exported=$matchedEventCount; output limit reached=$($matchedEventCount -ge 100)")
        }
        catch {
            if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') {
                [void]$eventText.AppendLine("$channel : no matching recent events")
            } else {
                [void]$eventText.AppendLine(('{0} : collection unavailable; exception={1}; HRESULT=0x{2:X8}' -f
                    $channel, $_.Exception.GetBaseException().GetType().Name, $_.Exception.GetBaseException().HResult))
            }
        }
    }
    $eventEntry = $archive.CreateEntry('windows-events.txt', [System.IO.Compression.CompressionLevel]::Optimal)
    $eventWriter = [System.IO.StreamWriter]::new($eventEntry.Open(), [System.Text.UTF8Encoding]::new($false))
    try { $eventWriter.Write($eventText.ToString()) }
    finally { $eventWriter.Dispose() }
    $included.Add('windows-events.txt')
    Add-ReportText $archive $included 'windows-print-activation-events.txt' $activationEvents.ToString()
    $defaultsEntry = $archive.CreateEntry('printer-defaults.txt', [System.IO.Compression.CompressionLevel]::Optimal)
    $defaultsWriter = [System.IO.StreamWriter]::new($defaultsEntry.Open(), [System.Text.UTF8Encoding]::new($false))
    try { $defaultsWriter.Write((Get-PrinterDefaultsSummary)) }
    finally { $defaultsWriter.Dispose() }
    $included.Add('printer-defaults.txt')
    $environmentEntry = $archive.CreateEntry('print-api-environment.txt', [System.IO.Compression.CompressionLevel]::Optimal)
    $environmentWriter = [System.IO.StreamWriter]::new($environmentEntry.Open(), [System.Text.UTF8Encoding]::new($false))
    try { $environmentWriter.Write((Get-PrintApiEnvironment)) }
    finally { $environmentWriter.Dispose() }
    $included.Add('print-api-environment.txt')
    Add-PrintProviderDiagnostics $archive $included
    Add-ReportText $archive $included 'printing/usb-streaming.txt' (Get-UsbStreamingSummary)
    Add-ReportText $archive $included 'printing/security-policy.txt' (Get-PrintSecuritySnapshot)
    $manifest = $archive.CreateEntry('README.txt')
    $writer = [System.IO.StreamWriter]::new($manifest.Open(), [System.Text.UTF8Encoding]::new($false))
    try {
        $writer.WriteLine('pyToneRIP technical diagnostics. No files are sent by the application.')
        $writer.WriteLine('*.errors.txt contain detailed application/installer error records: original messages, HRESULTs, inner exceptions and stack traces when logged. Paths, document/profile filenames and IP/URL endpoints are replaced.')
        $writer.WriteLine('*.diagnostics.txt contain compact technical summaries and a settings timeline. Missing or unreadable logs are reported explicitly in both formats.')
        $writer.WriteLine('Windows reports cover crashes and hangs of any application, plus printing and system events. Look in windows-events.txt for the time, application, fault module and exception code.')
        $writer.WriteLine('Document contents and full raw log files are not collected. Original Windows event messages are excluded; their structured technical fields are in windows-events.txt.')
        $writer.WriteLine('printing/ contains original queue-level PrintTicket/PrintCapabilities XML, which may include vendor private DEVMODE and printer identifiers, plus driver/software versions and hashes. No print contents or document tickets are read. USB counters include only selected numeric transport fields. Provider probes run read-only outside the PSA package; compare their results with the packaged Manager error logs.')
        $writer.WriteLine('Provider diagnostics include exact queue DEVMODE in base64, per-stage timing, native DocumentProperties results/last-error values and EnumForms metadata. Last-error is only meaningful when the API documents it for the returned result. Partial results survive a provider timeout (45 seconds for pyToneRIP, 15 seconds for other queues).')
        $writer.WriteLine('printing/psa-registration.txt lists current-user package/contract registration and log presence. Installed manifests and PDC/PDR assets for queue models are copied byte-for-byte with SHA-256. Presence does not prove successful activation. windows-print-activation-events.txt retains selected sanitized EventData, including PrintService 215/370 and Service Control Manager failures; paths, document filenames, endpoints and SIDs are masked.')
        $writer.WriteLine('Created: ' + (Get-Date).ToString('O'))
        $writer.WriteLine('Windows version: ' + [Environment]::OSVersion.Version.ToString())
        $writer.WriteLine('Full OS build, installed print component versions and runtime APIs: print-api-environment.txt')
        $writer.WriteLine('64-bit operating system: ' + [Environment]::Is64BitOperatingSystem)
        $writer.WriteLine('PowerShell version: ' + $PSVersionTable.PSVersion.ToString())
        $writer.WriteLine('Lookback days: ' + $LookbackDays)
        $writer.WriteLine('Included summaries:')
        foreach ($name in $included) { $writer.WriteLine('  ' + $name) }
    }
    finally { $writer.Dispose() }
}
catch {
    if ($null -ne $archive) { $archive.Dispose(); $archive = $null }
    if ($null -ne $file) { $file.Dispose(); $file = $null }
    Remove-Item -LiteralPath $archivePath -ErrorAction SilentlyContinue
    throw
}
finally {
    if ($null -ne $archive) { $archive.Dispose() }
    if ($null -ne $file) { $file.Dispose() }
}

if (-not [string]::IsNullOrWhiteSpace($ResultPath)) {
    [System.IO.File]::WriteAllText($ResultPath, $archivePath, [System.Text.UTF8Encoding]::new($false))
}
Write-Output $archivePath
