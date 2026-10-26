[CmdletBinding()]
param(
    [string[]]$PdcFiles = @(),
    [object[]]$Forms = @(),
    [switch]$CaptureOnly,
    [string]$PortBackupPath
)
$ErrorActionPreference = 'Stop'
function Test-PyToneRipStalePortError {
    param($Port, $ErrorRecord)
    $Port.PortMonitor -eq 'Virtual Monitor' -and
        $ErrorRecord.FullyQualifiedErrorId -match '^HRESULT 0x8007(0002|00aa|0709),Remove-PrinterPort$'
}
if (-not ('PyToneRipPrintState' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class PyToneRipPrintState {
    [StructLayout(LayoutKind.Sequential)] struct Size { public int W,H; }
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int L,T,R,B; }
    [StructLayout(LayoutKind.Sequential)] struct Info {
        public uint Flags; public IntPtr Name; public Size Size; public Rect Area;
        public IntPtr Keyword; public uint StringType; public IntPtr Mui;
        public uint ResourceId; public IntPtr Display; public ushort Lang;
    }
    [StructLayout(LayoutKind.Sequential)] struct Defaults { public IntPtr Type,Mode; public uint Access; }
    public sealed class Form { public uint Flags; public string Name,Keyword; public int W,H; }
    [DllImport("winspool.drv",EntryPoint="OpenPrinterW",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern bool Open(string name,out IntPtr h,ref Defaults defaults);
    [DllImport("winspool.drv",EntryPoint="EnumFormsW",SetLastError=true)]
    static extern bool Enum(IntPtr h,uint level,IntPtr buffer,uint bytes,out uint needed,out uint count);
    [DllImport("winspool.drv",EntryPoint="DeleteFormW",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern bool Delete(IntPtr h,string name);
    [DllImport("winspool.drv",EntryPoint="ClosePrinter")] static extern bool Close(IntPtr h);
    static IntPtr OpenServer(uint access) {
        var d=new Defaults { Access=access }; IntPtr h;
        if(!Open(null,out h,ref d)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return h;
    }
    public static Form[] Read() {
        IntPtr h=OpenServer(2);
        try { uint needed,count; bool ok=Enum(h,2,IntPtr.Zero,0,out needed,out count);
            int error=Marshal.GetLastWin32Error();
            if(!ok && error!=122) throw new Win32Exception(error);
            if(needed==0) return new Form[0];
            IntPtr buffer=Marshal.AllocHGlobal(checked((int)needed));
            try { if(!Enum(h,2,buffer,needed,out needed,out count)) throw new Win32Exception(Marshal.GetLastWin32Error());
                var forms=new List<Form>(); int stride=Marshal.SizeOf(typeof(Info));
                for(int i=0;i<count;i++) { var f=(Info)Marshal.PtrToStructure(IntPtr.Add(buffer,i*stride),typeof(Info));
                    forms.Add(new Form { Flags=f.Flags,Name=Marshal.PtrToStringUni(f.Name),Keyword=Marshal.PtrToStringAnsi(f.Keyword),W=f.Size.W,H=f.Size.H }); }
                return forms.ToArray();
            } finally { Marshal.FreeHGlobal(buffer); }
        } finally { Close(h); }
    }
    public static bool Remove(string name,string keyword,int width,int height) {
        bool legacy=(String.IsNullOrEmpty(keyword) && name=="JIS B5 (182 x 257 mm)" && width==182000 && height==257000);
        if(!legacy && (keyword==null || !keyword.StartsWith("Page_",StringComparison.Ordinal)
            || !keyword.EndsWith(":Microsoft*",StringComparison.Ordinal))) throw new InvalidOperationException("Unapproved form keyword.");
        bool found=false;
        foreach(var f in Read()) if(f.Name==name) {
            if((f.Keyword ?? "")!=(keyword ?? "") || f.Flags!=2 || f.W!=width || f.H!=height)
                throw new InvalidOperationException("Form fingerprint changed: "+name);
            found=true;
        }
        if(!found) return false;
        IntPtr h=OpenServer(3);
        try { if(!Delete(h,name)) throw new Win32Exception(Marshal.GetLastWin32Error()); return true; }
        finally { Close(h); }
    }
}
'@
}

if ($CaptureOnly) {
    $known = @{}
    foreach ($path in $PdcFiles) {
        [xml]$pdc = Get-Content -LiteralPath $path -Encoding UTF8 -Raw
        foreach ($option in $pdc.SelectNodes("//*[local-name()='PageMediaSize']/*")) {
            if ($option.NamespaceURI -ne 'https://pytone-rip.local/printticket/2026/phase12' -or
                $option.LocalName -notlike 'Page_*') { continue }
            $width = $option.SelectSingleNode("*[local-name()='MediaSizeWidth']")
            $height = $option.SelectSingleNode("*[local-name()='MediaSizeHeight']")
            if ($null -eq $width -or $null -eq $height) { continue }
            $keyword = $option.LocalName + ':Microsoft*'
            if (-not $known.ContainsKey($keyword)) { $known[$keyword] = @{} }
            $size = @([int]$width.InnerText, [int]$height.InnerText)
            $known[$keyword][($size -join ',')] = $size
        }
    }
    foreach ($form in [PyToneRipPrintState]::Read()) {
        # Exact name/dimensions of the temporary B5 label introduced in our
        # 1.0.0.183 experiment. Its keyword is empty; ordinary B5 stays intact.
        if ($form.Flags -eq 2 -and -not $form.Keyword -and $form.Name -ceq 'JIS B5 (182 x 257 mm)' -and
            $form.W -eq 182000 -and $form.H -eq 257000) { $form; continue }
        if ($form.Flags -ne 2 -or -not $form.Keyword -or -not $known.ContainsKey($form.Keyword)) { continue }
        foreach ($size in $known[$form.Keyword].Values) {
            if ([Math]::Abs($form.W - $size[0]) -le 2 -and [Math]::Abs($form.H - $size[1]) -le 2) {
                $form
                break
            }
        }
    }
    return
}

$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run print-state cleanup as Administrator.' }
if (@(Get-AppxPackage -AllUsers -ErrorAction Stop | Where-Object {
    ($_.Name -eq 'pyToneRIP.Manager' -or $_.Name -eq 'pyToneRIP.PSA' -or $_.Name -like 'pyToneRIP.Printer.*') -and
    @($_.PackageUserInformation | Where-Object { $_.InstallState -eq 'Installed' }).Count -gt 0
}).Count) {
    Write-Host 'pyToneRIP is registered for another Windows user; shared print state was kept.'
    [pscustomobject]@{ RemovedForms=0; RemovedPorts=0; SpoolerRestarted=$false; SharedStateKept=$true }
    return
}
$queues = @(Get-Printer)
if (@($queues | Where-Object { $_.Name -like 'pyToneRIP*' -or $_.PortName -match 'pytone-rip:' }).Count) {
    throw 'Remove pyToneRIP queues before clearing their print state.'
}

$removedForms = 0
foreach ($form in $Forms) {
    if ($form.Flags -ne 2) { throw 'A protected form was included in the cleanup plan.' }
    if ([PyToneRipPrintState]::Remove($form.Name, $form.Keyword, $form.W, $form.H)) { $removedForms++ }
}
$portPattern = '^pyToneRIP\.(Manager|PSA|Printer\.[^_]+)_\d+\.\d+\.\d+\.\d+_'
$ports = @(Get-PrinterPort | Where-Object { $_.Name -match $portPattern -and $_.Name -notin @($queues.PortName) })
$fallback = @()
$removedPorts = 0
$temporaryBackup = -not $PortBackupPath
$staleVirtualMonitor = $false
foreach ($port in $ports) {
    if ($staleVirtualMonitor -and $port.PortMonitor -eq 'Virtual Monitor') { $fallback += $port; continue }
    try { Remove-PrinterPort -Name $port.Name; $removedPorts++ }
    catch {
        # Confirmed on unregistered MSIX ports: the virtual monitor tries to
        # access an old package (ERROR_FILE_NOT_FOUND), keeps a reference
        # after queue deletion (ERROR_BUSY), or reports an invalid printer name
        # for an orphaned port (ERROR_INVALID_PRINTER_NAME). Live queues are excluded above;
        # no printing jobs may exist before the registry/cache cleanup below.
        if (-not (Test-PyToneRipStalePortError $port $_)) { throw }
        $staleVirtualMonitor = $true
        $fallback += $port
    }
}
$restarted = $false
if ($fallback.Count) {
    foreach ($queue in $queues) {
        if (@(Get-PrintJob -PrinterName $queue.Name).Count) { throw 'Print jobs exist; orphan-port cleanup postponed.' }
    }
    $registryPath = 'SYSTEM\CurrentControlSet\Control\Print\Monitors\Virtual Port Monitor\Ports'
    $root = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($registryPath, $true)
    if ($null -eq $root) { throw 'The virtual-port registry root is unavailable.' }
    try {
        $backup = @()
        foreach ($port in $fallback) {
            if ($port.Name -notmatch $portPattern -or $port.Name.Contains('\')) { throw 'Unsafe virtual-port name.' }
            $key = $root.OpenSubKey($port.Name)
            if ($null -eq $key) { continue }
            try {
                if ($key.SubKeyCount -ne 0) { throw 'Unexpected virtual-port subkeys.' }
                $backup += [pscustomobject]@{ Name=$port.Name; PortData=$key.GetValue('PortData'); Kind=$key.GetValueKind('PortData').ToString() }
            } finally { $key.Dispose() }
        }
        if (-not $PortBackupPath) { $PortBackupPath = Join-Path $env:TEMP ('pyToneRIP-orphan-ports-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json') }
        ConvertTo-Json -InputObject @($backup) -Depth 4 | Set-Content -LiteralPath $PortBackupPath -Encoding UTF8
        $wasRunning = (Get-Service Spooler).Status -eq 'Running'
        try {
            if ($wasRunning) { Stop-Service Spooler -Force; $restarted = $true }
            foreach ($entry in $backup) {
                $key = $root.OpenSubKey($entry.Name)
                if ($null -eq $key) { continue }
                try {
                    if ($key.SubKeyCount -ne 0 -or $key.GetValue('PortData') -cne $entry.PortData) { throw 'Virtual-port data changed during cleanup.' }
                } finally { $key.Dispose() }
                # Delete exactly one verified leaf under the fixed monitor root.
                $root.DeleteSubKey($entry.Name, $false)
                $removedPorts++
            }
        } finally {
            if ($wasRunning) {
                Start-Service Spooler
                (Get-Service Spooler).WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Running, [TimeSpan]::FromSeconds(15))
            }
        }
    } finally { $root.Dispose() }
}
if (@(Get-PrinterPort | Where-Object { $_.Name -match $portPattern -and $_.Name -notin @($queues.PortName) }).Count) {
    throw 'Unused pyToneRIP ports remain after cleanup.'
}
if ($temporaryBackup -and $PortBackupPath -and (Test-Path -LiteralPath $PortBackupPath -PathType Leaf)) {
    Remove-Item -LiteralPath $PortBackupPath -Force
}
[pscustomobject]@{ RemovedForms=$removedForms; RemovedPorts=$removedPorts; SpoolerRestarted=$restarted }
