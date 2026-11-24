using System;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;

namespace pyToneRIP.QueueHelper
{
    // A blocking desktop WritePrinter can outlive the RIP cancellation marker.
    // The supervisor can delete only the exact job recorded before the first write.
    internal static class UsbRawJobCleanup
    {
        internal static void TryCancel(string jobFolder)
        {
            // Windows printing calls can block. Cleanup must not keep the PSA
            // result waiting indefinitely after the worker has been terminated.
            Task.Run(() => Cancel(jobFolder)).Wait(TimeSpan.FromSeconds(3));
        }

        internal static void CancelDelivered(string jobFolder, string queue, string device, string driver = null)
        {
            Cancel(jobFolder, true, queue, device, driver);
        }

        internal static bool HasActiveJob(string jobFolder, string queue, string device, string driver = null)
        {
            var path = Path.Combine(jobFolder, "physical-print-job.json");
            if (!File.Exists(path) || new FileInfo(path).Length > 16384
                || (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) return false;
            PhysicalJob job;
            try
            {
                using (var stream = File.OpenRead(path))
                    job = (PhysicalJob)new DataContractJsonSerializer(typeof(PhysicalJob)).ReadObject(stream);
            }
            catch (SerializationException) { return false; }
            Guid token;
            if (job == null || job.Schema != "pytone-rip.physical-print-job" || job.Version != 1
                || job.JobId == 0 || job.QueueName != queue || job.DeviceId != device || job.DriverName != driver
                || (job.State != "ACTIVE" && job.State != "SPOOLED" && job.State != "ABORT_FAILED")
                || job.DocumentName == null || !job.DocumentName.StartsWith("pyToneRIP ", StringComparison.Ordinal)
                || !Guid.TryParseExact(job.DocumentName.Substring(10), "N", out token)
                || FindDestination(job) == null) return false;
            return PrintQueueCancellationMonitor.HasMatchingJob(queue, job.JobId, job.DocumentName);
        }

        private static QueueDescriptor FindDestination(PhysicalJob job)
        {
            if (job.DriverName != null)
                return PrinterApi.ListPrinters().FirstOrDefault(item => item.IsEpsonRawDestination
                    && item.Name == job.QueueName
                    && string.Equals(item.DriverName, job.DriverName, StringComparison.OrdinalIgnoreCase));
            if (job.PortName == null || !Regex.IsMatch(job.PortName, @"^USB\d+$", RegexOptions.IgnoreCase))
                return null;
            return PrinterApi.ListLocalPrinters().FirstOrDefault(item => item.Name == job.QueueName
                && string.Equals(item.PortName, job.PortName, StringComparison.OrdinalIgnoreCase)
                && !item.Name.StartsWith("pyToneRIP", StringComparison.OrdinalIgnoreCase)
                && item.DriverName != "Microsoft Virtual Print Class Driver");
        }

        private static void Cancel(string jobFolder, bool strict = false, string expectedQueue = null,
            string expectedDevice = null, string expectedDriver = null)
        {
            try
            {
                var path = Path.Combine(jobFolder, "physical-print-job.json");
                if (!File.Exists(path)) { if (strict) throw new InvalidDataException("Missing physical job identity."); return; }
                PhysicalJob job;
                using (var stream = File.OpenRead(path))
                    job = (PhysicalJob)new DataContractJsonSerializer(typeof(PhysicalJob)).ReadObject(stream);
                Guid token;
                if (job == null || job.Schema != "pytone-rip.physical-print-job" || job.Version != 1
                    || (job.State != "ACTIVE" && job.State != "ABORT_FAILED" && !(strict && job.State == "SPOOLED")) || job.JobId == 0
                    || job.DocumentName == null || !job.DocumentName.StartsWith("pyToneRIP ", StringComparison.Ordinal)
                    || !Guid.TryParseExact(job.DocumentName.Substring(10), "N", out token))
                { if (strict) throw new InvalidDataException("Invalid physical job identity."); return; }
                if (strict && (job.QueueName != expectedQueue || job.DeviceId != expectedDevice || job.DriverName != expectedDriver))
                    throw new InvalidDataException("Physical job target changed.");
                var queue = FindDestination(job);
                if (queue == null) { if (strict) throw new InvalidDataException("Physical queue target changed."); return; }
                IntPtr handle;
                if (!OpenPrinterW(queue.Name, out handle, IntPtr.Zero)) { if (strict) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error()); return; }
                try
                {
                    uint needed;
                    GetJobW(handle, job.JobId, 1, IntPtr.Zero, 0, out needed);
                    if (needed == 0)
                    {
                        int error = Marshal.GetLastWin32Error();
                        if (strict && error != 87 && error != 2 && error != 259)
                            throw new System.ComponentModel.Win32Exception(error);
                        if (strict && job.DriverName != null)
                            throw new InvalidOperationException("This print job is no longer in the Windows queue.");
                        return; // An already delivered spool job can be gone.
                    }
                    if (needed > 1024 * 1024) throw new InvalidDataException("Invalid physical job buffer size.");
                    var buffer = Marshal.AllocHGlobal((int)needed);
                    try
                    {
                        if (!GetJobW(handle, job.JobId, 1, buffer, needed, out needed)) { if (strict) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error()); return; }
                        var actual = (JobInfoPrefix)Marshal.PtrToStructure(buffer, typeof(JobInfoPrefix));
                        if (actual.Id == job.JobId && actual.Document == job.DocumentName)
                        {
                            var deleted = SetJobW(handle, job.JobId, 0, IntPtr.Zero, 5);
                            if (!deleted && strict)
                                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                            // Supervisor fallback after a blocked RIP is terminated.
                            // Stop the verified spool job before resetting its interface.
                            if (deleted && !strict && job.DriverName == null && File.Exists(Path.Combine(jobFolder, "cancel.requested")))
                                UsbPrinterStatusReader.CancelPrint(job.DeviceId);
                        }
                        else if (strict) throw new InvalidDataException("Physical job ID was reused by another document.");
                    }
                    finally { Marshal.FreeHGlobal(buffer); }
                }
                finally { ClosePrinter(handle); }
            }
            catch (Exception error)
            {
                if (strict) throw;
                System.Diagnostics.Trace.WriteLine("USB job cleanup failed: " + error.Message);
            }
        }

        [DataContract]
        private sealed class PhysicalJob
        {
            [DataMember(Name = "schema")] public string Schema { get; set; }
            [DataMember(Name = "version")] public int Version { get; set; }
            [DataMember(Name = "queue_name")] public string QueueName { get; set; }
            [DataMember(Name = "job_id")] public uint JobId { get; set; }
            [DataMember(Name = "document_name")] public string DocumentName { get; set; }
            [DataMember(Name = "port_name")] public string PortName { get; set; }
            [DataMember(Name = "state")] public string State { get; set; }
            [DataMember(Name = "device_id")] public string DeviceId { get; set; }
            [DataMember(Name = "driver_name")] public string DriverName { get; set; }
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct JobInfoPrefix
        {
            public uint Id;
            [MarshalAs(UnmanagedType.LPWStr)] public string Printer;
            [MarshalAs(UnmanagedType.LPWStr)] public string Machine;
            [MarshalAs(UnmanagedType.LPWStr)] public string User;
            [MarshalAs(UnmanagedType.LPWStr)] public string Document;
        }
        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool OpenPrinterW(string name, out IntPtr handle, IntPtr defaults);
        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool GetJobW(IntPtr handle, uint job, uint level, IntPtr buffer, uint size, out uint needed);
        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool SetJobW(IntPtr handle, uint job, uint level, IntPtr buffer, uint command);
        [DllImport("winspool.drv", SetLastError = true)]
        private static extern bool ClosePrinter(IntPtr handle);
    }
}
