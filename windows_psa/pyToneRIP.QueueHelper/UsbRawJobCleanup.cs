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

        private static void Cancel(string jobFolder)
        {
            try
            {
                var path = Path.Combine(jobFolder, "physical-print-job.json");
                if (!File.Exists(path)) return;
                PhysicalJob job;
                using (var stream = File.OpenRead(path))
                    job = (PhysicalJob)new DataContractJsonSerializer(typeof(PhysicalJob)).ReadObject(stream);
                Guid token;
                if (job == null || job.Schema != "pytone-rip.physical-print-job" || job.Version != 1
                    || (job.State != "ACTIVE" && job.State != "ABORT_FAILED") || job.JobId == 0
                    || job.DocumentName == null || !job.DocumentName.StartsWith("pyToneRIP ", StringComparison.Ordinal)
                    || !Guid.TryParseExact(job.DocumentName.Substring(10), "N", out token)
                    || job.PortName == null || !Regex.IsMatch(job.PortName, @"^USB\d+$", RegexOptions.IgnoreCase)) return;
                var queue = PrinterApi.ListLocalPrinters().FirstOrDefault(item => item.Name == job.QueueName
                    && string.Equals(item.PortName, job.PortName, StringComparison.OrdinalIgnoreCase)
                    && !item.Name.StartsWith("pyToneRIP", StringComparison.OrdinalIgnoreCase)
                    && item.DriverName != "Microsoft Virtual Print Class Driver");
                if (queue == null) return;
                IntPtr handle;
                if (!OpenPrinterW(queue.Name, out handle, IntPtr.Zero)) return;
                try
                {
                    uint needed;
                    GetJobW(handle, job.JobId, 1, IntPtr.Zero, 0, out needed);
                    if (needed == 0 || needed > 1024 * 1024) return;
                    var buffer = Marshal.AllocHGlobal((int)needed);
                    try
                    {
                        if (!GetJobW(handle, job.JobId, 1, buffer, needed, out needed)) return;
                        var actual = (JobInfoPrefix)Marshal.PtrToStructure(buffer, typeof(JobInfoPrefix));
                        if (actual.Id == job.JobId && actual.Document == job.DocumentName)
                            SetJobW(handle, job.JobId, 0, IntPtr.Zero, 5);
                    }
                    finally { Marshal.FreeHGlobal(buffer); }
                }
                finally { ClosePrinter(handle); }
            }
            catch (Exception error)
            {
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
