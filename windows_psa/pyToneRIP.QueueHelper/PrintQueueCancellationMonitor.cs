using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;

namespace pyToneRIP.QueueHelper
{
    // The virtual-printer workflow status does not necessarily follow a user's
    // Cancel command in the Windows queue. Observe that queue from the full-
    // trust worker, where Winspool APIs are available, while the RIP is live.
    internal sealed class PrintQueueCancellationMonitor
    {
        private const uint JobStatusPrinting = 0x00000010;
        private const uint JobStatusDeleting = 0x00000004;
        private const uint JobStatusDeleted = 0x00000100;
        private const uint JobStatusComplete = 0x00001000;
        private const uint JobControlDelete = 5;
        private const int ErrorInsufficientBuffer = 122;
        private readonly string queueName;
        private readonly long pdlSize;
        private readonly DateTime createdUtc;
        private readonly string jobId;
        private readonly string cancellationPath;
        private readonly string localState;
        private uint? windowsJobId;
        private DateTime nextPollUtc;

        private PrintQueueCancellationMonitor(
            string queueName, long pdlSize, DateTime createdUtc,
            string jobId, string cancellationPath, string localState)
        {
            this.queueName = queueName;
            this.pdlSize = pdlSize;
            this.createdUtc = createdUtc;
            this.jobId = jobId;
            this.cancellationPath = cancellationPath;
            this.localState = localState;
        }

        public static PrintQueueCancellationMonitor Open(
            string jobRecordPath, string cancellationPath, string localState)
        {
            using (var stream = File.OpenRead(jobRecordPath))
            {
                var record = (QueueJobRecord)new DataContractJsonSerializer(
                    typeof(QueueJobRecord)).ReadObject(stream);
                DateTime created;
                if (record == null || record.Metadata == null || record.Artifacts == null
                    || record.Artifacts.Pdl == null
                    || string.IsNullOrWhiteSpace(record.Metadata.QueueName)
                    || string.IsNullOrWhiteSpace(record.JobId)
                    || record.Artifacts.Pdl.SizeBytes <= 0
                    || !DateTime.TryParse(
                        record.CreatedUtc, System.Globalization.CultureInfo.InvariantCulture,
                        System.Globalization.DateTimeStyles.RoundtripKind, out created))
                    throw new InvalidDataException("Print queue cancellation identity is incomplete.");
                return new PrintQueueCancellationMonitor(
                    record.Metadata.QueueName, record.Artifacts.Pdl.SizeBytes,
                    created.ToUniversalTime(), record.JobId, cancellationPath, localState);
            }
        }

        public void Poll()
        {
            if (DateTime.UtcNow < nextPollUtc || File.Exists(cancellationPath))
                return;
            nextPollUtc = DateTime.UtcNow.AddMilliseconds(500);
            try
            {
                var jobs = ReadJobs(queueName);
                if (!windowsJobId.HasValue)
                {
                    // SessionId is NOT the spooler job ID (the captured job
                    // had SessionId 00000002 and Windows job ID 3). Match on
                    // queue, exact captured PDL byte count and submission time.
                    // Consecutive copies of the same document have the same size:
                    // choose the closest submission only when it is clearly
                    // separated from the next candidate.
                    var candidates = new List<QueueJob>();
                    foreach (var job in jobs)
                        if (job.Size == pdlSize
                            && Math.Abs((job.SubmittedUtc - createdUtc).TotalMinutes) <= 2)
                            candidates.Add(job);
                    candidates.Sort((left, right) =>
                        Math.Abs((left.SubmittedUtc - createdUtc).TotalSeconds).CompareTo(
                            Math.Abs((right.SubmittedUtc - createdUtc).TotalSeconds)));
                    if (candidates.Count == 0)
                        return;
                    var nearestSeconds = Math.Abs(
                        (candidates[0].SubmittedUtc - createdUtc).TotalSeconds);
                    if (candidates.Count > 1
                        && Math.Abs((candidates[1].SubmittedUtc - createdUtc).TotalSeconds)
                            - nearestSeconds < 10)
                        return;
                    windowsJobId = candidates[0].Id;
                    Program.Log(localState, "RIP job " + jobId + " matched Windows queue job "
                        + windowsJobId + " on " + queueName + ".");
                }

                foreach (var job in jobs)
                {
                    if (job.Id != windowsJobId.Value)
                        continue;
                    if ((job.Status & (JobStatusDeleting | JobStatusDeleted)) == 0)
                        return;
                    File.WriteAllText(cancellationPath,
                        DateTimeOffset.UtcNow.ToString("O") + Environment.NewLine
                        + "Windows queue job " + job.Id + " is deleting." + Environment.NewLine);
                    Program.Log(localState, "RIP cancellation requested for " + jobId
                        + ": Windows queue job " + job.Id + " status=" + job.Status + ".");
                    return;
                }
            }
            catch (Exception error)
            {
                // A spooler query failure must not turn a valid print into a
                // cancellation. The workflow status/marker path still applies.
                Program.Log(localState, "Windows queue cancellation monitor for "
                    + jobId + " could not poll: " + error.Message);
                nextPollUtc = DateTime.UtcNow.AddSeconds(5);
            }
        }

        public static uint? CancelCurrentJob(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName))
                throw new InvalidDataException("A printer queue name is required.");
            // Query the spooler again at click time because the job detected
            // when the menu opened may have finished meanwhile.
            var current = FindCurrentJob(queueName);
            if (current == null)
                return null;
            IntPtr printer;
            if (!OpenPrinterW(queueName, out printer, IntPtr.Zero))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not open print queue.");
            try
            {
                if (!SetJobW(printer, current.Id, 0, IntPtr.Zero, JobControlDelete))
                    throw new Win32Exception(Marshal.GetLastWin32Error(),
                        "Windows could not cancel the current print job.");
                return current.Id;
            }
            finally { ClosePrinter(printer); }
        }

        public static bool HasCurrentJob(string queueName) => FindCurrentJob(queueName) != null;

        private static QueueJob FindCurrentJob(string queueName)
        {
            var jobs = ReadJobs(queueName);
            QueueJob current = null;
            foreach (var job in jobs)
            {
                if ((job.Status & (JobStatusDeleting | JobStatusDeleted | JobStatusComplete)) != 0)
                    continue;
                if (current == null ||
                    ((job.Status & JobStatusPrinting) != 0 &&
                     (current.Status & JobStatusPrinting) == 0) ||
                    ((job.Status & JobStatusPrinting) == (current.Status & JobStatusPrinting)
                     && job.Position < current.Position))
                    current = job;
            }
            return current;
        }

        private static List<QueueJob> ReadJobs(string queueName)
        {
            IntPtr printer;
            if (!OpenPrinterW(queueName, out printer, IntPtr.Zero))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not open print queue.");
            try
            {
                uint needed = 0;
                uint returned = 0;
                EnumJobsW(printer, 0, uint.MaxValue, 2, IntPtr.Zero, 0,
                    out needed, out returned);
                if (needed == 0)
                    return new List<QueueJob>();
                if (Marshal.GetLastWin32Error() != ErrorInsufficientBuffer)
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not size print jobs.");
                var buffer = Marshal.AllocHGlobal((int)needed);
                try
                {
                    if (!EnumJobsW(printer, 0, uint.MaxValue, 2, buffer, needed,
                        out needed, out returned))
                        throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not enumerate print jobs.");
                    var jobs = new List<QueueJob>((int)returned);
                    var size = Marshal.SizeOf(typeof(JobInfo2));
                    for (var index = 0; index < returned; index++)
                    {
                        var raw = (JobInfo2)Marshal.PtrToStructure(
                            IntPtr.Add(buffer, (int)index * size), typeof(JobInfo2));
                        jobs.Add(new QueueJob
                        {
                            Id = raw.JobId,
                            Status = raw.Status,
                            Position = raw.Position,
                            Size = raw.Size,
                            SubmittedUtc = raw.Submitted.ToUtc()
                        });
                    }
                    return jobs;
                }
                finally { Marshal.FreeHGlobal(buffer); }
            }
            finally { ClosePrinter(printer); }
        }

        private sealed class QueueJob
        {
            public uint Id;
            public uint Status;
            public uint Position;
            public long Size;
            public DateTime SubmittedUtc;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SystemTime
        {
            public ushort Year, Month, DayOfWeek, Day, Hour, Minute, Second, Milliseconds;
            public DateTime ToUtc() => Year == 0
                ? DateTime.MinValue
                : new DateTime(Year, Month, Day, Hour, Minute, Second,
                    Milliseconds, DateTimeKind.Utc);
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JobInfo2
        {
            public uint JobId;
            public IntPtr PrinterName, MachineName, UserName, Document, NotifyName;
            public IntPtr Datatype, PrintProcessor, Parameters, DriverName, DevMode;
            public IntPtr StatusText, SecurityDescriptor;
            public uint Status, Priority, Position, StartTime, UntilTime, TotalPages, Size;
            public SystemTime Submitted;
            public uint Time, PagesPrinted;
        }

        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool OpenPrinterW(string printerName, out IntPtr printer, IntPtr defaults);

        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool EnumJobsW(
            IntPtr printer, uint firstJob, uint noJobs, uint level, IntPtr buffer,
            uint bufferSize, out uint needed, out uint returned);

        [DllImport("winspool.drv", SetLastError = true)]
        private static extern bool ClosePrinter(IntPtr printer);

        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool SetJobW(
            IntPtr printer, uint jobId, uint level, IntPtr job, uint command);

        [DataContract]
        private sealed class QueueJobRecord
        {
            [DataMember(Name = "job_id")] public string JobId { get; set; }
            [DataMember(Name = "created_utc")] public string CreatedUtc { get; set; }
            [DataMember(Name = "metadata")] public QueueJobMetadata Metadata { get; set; }
            [DataMember(Name = "artifacts")] public QueueJobArtifacts Artifacts { get; set; }
        }

        [DataContract]
        private sealed class QueueJobMetadata
        {
            [DataMember(Name = "queue_name")] public string QueueName { get; set; }
        }

        [DataContract]
        private sealed class QueueJobArtifacts
        {
            [DataMember(Name = "pdl")] public QueuePdlArtifact Pdl { get; set; }
        }

        [DataContract]
        private sealed class QueuePdlArtifact
        {
            [DataMember(Name = "size_bytes")] public long SizeBytes { get; set; }
        }
    }
}
