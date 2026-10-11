param(
    [string]$LocalStatePath,
    [string]$InstallerRoot,
    [string]$ManagerLogPath,
    [string]$OutputDirectory,
    [string]$ResultPath,
    [ValidateRange(1, 30)][int]$LookbackDays = 1
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
        @{ Pattern = 'PrintTicket validation|Default PrintTicket validation'; Text = 'PrintTicket validation' },
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
        '(?i)\b(page|readback|orientation|raster|status|model|stage|dpi|resolution|media|input|launch|delivered)\s*=\s*([A-Za-z0-9_.:+-]{1,96})(?=[;\s]|$)')) {
        $labels.Add($field.Groups[1].Value + '=' + $field.Groups[2].Value)
    }
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
        'ResultCode', 'Status', 'EventName', 'PackageFullName')) {
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
        $entry = $archive.CreateEntry($source.Entry, [System.IO.Compression.CompressionLevel]::Optimal)
        $entryWriter = [System.IO.StreamWriter]::new($entry.Open(), [System.Text.UTF8Encoding]::new($false))
        try {
            if ([string]::IsNullOrWhiteSpace($source.Path) -or -not [System.IO.File]::Exists($source.Path)) {
                $entryWriter.WriteLine('Source log is not present. This is not evidence that no error occurred.')
            } else {
                try { $entryWriter.Write((Get-TechnicalSummary $source.Path)) }
                catch {
                    $cause = $_.Exception.GetBaseException()
                    $entryWriter.WriteLine('Source log could not be read; exception={0}; HRESULT=0x{1:X8}', $cause.GetType().Name, $cause.HResult)
                }
            }
        }
        finally { $entryWriter.Dispose() }
        $included.Add($source.Entry)
    }
    $eventText = [System.Text.StringBuilder]::new()
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
        'Microsoft-Windows-TWinUI/Operational',
        'Microsoft-Windows-PrintService/Admin',
        'Microsoft-Windows-PrintService/Operational'
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
    $defaultsEntry = $archive.CreateEntry('printer-defaults.txt', [System.IO.Compression.CompressionLevel]::Optimal)
    $defaultsWriter = [System.IO.StreamWriter]::new($defaultsEntry.Open(), [System.Text.UTF8Encoding]::new($false))
    try { $defaultsWriter.Write((Get-PrinterDefaultsSummary)) }
    finally { $defaultsWriter.Dispose() }
    $included.Add('printer-defaults.txt')
    $manifest = $archive.CreateEntry('README.txt')
    $writer = [System.IO.StreamWriter]::new($manifest.Open(), [System.Text.UTF8Encoding]::new($false))
    try {
        $writer.WriteLine('pyToneRIP technical diagnostics. No files are sent by the application.')
        $writer.WriteLine('Original log lines and Windows event messages are excluded.')
        $writer.WriteLine('Application logs contain technical error summaries and a settings timeline. Missing or unreadable logs are reported explicitly.')
        $writer.WriteLine('Windows reports cover crashes and hangs of any application, plus printing and system events. Look in windows-events.txt for the time, application, fault module and exception code.')
        $writer.WriteLine('Document contents, personal paths, IP addresses and arbitrary event/log text are excluded.')
        $writer.WriteLine('Created: ' + (Get-Date).ToString('O'))
        $writer.WriteLine('Windows version: ' + [Environment]::OSVersion.Version.ToString())
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
