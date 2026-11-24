// Copyright (C) 2026 Alexander Eichhorn
// SPDX-License-Identifier: GPL-3.0-only
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Threading;
using pyToneRIP.Cancellation;

namespace pyToneRIP.QueueHelper
{
    // Virtual workflow completion records delivery, not physical page completion.
    // Keep that history intact; record a physical cancellation acknowledgement separately.
    internal static class DeliveredPrintCancellation
    {
        internal static bool HasJob(string localState, string queue)
        {
            var job = Latest(ReadJobs(localState).Where(item => Same(item.Queue, queue)));
            return IsCancelable(job);
        }

        private static bool IsCancelable(Job job)
        {
            if (job == null || !HasPhysicalData(job) || Acknowledged(job)) return false;
            if (job.Config.Connection.Kind == "network")
                return EpsonNetworkCancellation.IsPrinting(job.Config.Connection.Host);
            return UsesQueue(job.Config.Connection) && UsbRawJobCleanup.HasActiveJob(
                job.Folder, job.Config.Connection.PrinterQueue, job.Config.Connection.UsbDeviceId,
                job.Config.Connection.PrinterQueueDriver);
        }

        internal static bool CancelQueue(string localState, string queue)
        {
            var job = Latest(ReadJobs(localState).Where(item => Same(item.Queue, queue)));
            if (!IsCancelable(job)) return false;
            Request(localState, job.Id);
            return true;
        }

        internal static void Request(string localState, string id)
        {
            var matches = ReadJobs(localState).Where(item => item.Id == id).ToList();
            if (matches.Count != 1) throw new InvalidDataException("Print job identity is missing or ambiguous.");
            var job = matches[0];
            if (Acknowledged(job)) return;
            var snapshot = Read<Transport>(Path.Combine(job.Folder, "transport-result.json"));
            if (snapshot != null && snapshot.Bytes > 0 && !HasPhysicalData(job))
                throw new InvalidDataException("Delivered print target failed validation.");
            if (!HasPhysicalData(job))
            {
                File.WriteAllText(Path.Combine(job.Folder, "cancel.requested"), "Print cancellation requested.\r\n");
                // If delivery finishes during this write, the worker or this
                // second read must observe the cancellation before returning.
                if (!HasPhysicalData(job)) return;
            }
            // Use the scheduler's existing device lock, before the cancellation
            // lock. This also permits the owning worker's final cancellation.
            var key = RipRequestProcessor.ReadPhysicalPrinterKey(job.Folder);
            using (var printer = new Mutex(false, RipRequestProcessor.PhysicalPrinterMutexName(key)))
            {
                bool owns;
                try { owns = printer.WaitOne(TimeSpan.FromSeconds(8)); }
                catch (AbandonedMutexException) { owns = true; }
                if (!owns) throw new TimeoutException("Printer delivery is still finishing; try cancellation again.");
                try { CancelDelivered(localState, id); }
                finally { printer.ReleaseMutex(); }
            }
        }

        private static void CancelDelivered(string localState, string id)
        {
            if (string.IsNullOrWhiteSpace(id) || id != Path.GetFileName(id) || id.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                throw new InvalidDataException("Invalid print job identity.");
            using (var gate = new Mutex(false, "Local\\pyToneRIP.PhysicalCancellation"))
            {
                bool owns;
                try { owns = gate.WaitOne(TimeSpan.FromSeconds(8)); }
                catch (AbandonedMutexException) { owns = true; }
                if (!owns) throw new TimeoutException("Another physical cancellation is still running.");
                try
                {
                    var jobs = ReadJobs(localState).ToList();
                    var matches = jobs.Where(item => item.Id == id).ToList();
                    if (matches.Count != 1) throw new InvalidDataException("Print job identity is missing or ambiguous.");
                    var job = matches[0];
                    if (Acknowledged(job)) return;
                    // A reset is interface-wide. Never use an old preview to reset
                    // a newer job, including jobs sent through another virtual queue.
                    var related = jobs.Where(item => Same(item.Queue, job.Queue) || SameTarget(item, job));
                    if (Latest(related)?.Id != id)
                        throw new InvalidOperationException("A newer job exists for this printer; cancel that job instead.");
                    File.WriteAllText(Path.Combine(job.Folder, "cancel.requested"), "Physical cancellation requested.\r\n");
                    if (!HasPhysicalData(job)) return; // Running worker observes the durable marker.
                    try
                    {
                        if (job.Config.Connection.Kind == "network")
                            EpsonNetworkCancellation.Cancel(job.Config.Connection.Host);
                        else if (job.Config.Connection.Kind == "windows_queue")
                            UsbRawJobCleanup.CancelDelivered(job.Folder, job.Config.Connection.PrinterQueue,
                                null, job.Config.Connection.PrinterQueueDriver);
                        else
                        {
                            if (UsesQueue(job.Config.Connection))
                                UsbRawJobCleanup.CancelDelivered(job.Folder, job.Config.Connection.PrinterQueue, job.Config.Connection.UsbDeviceId);
                            UsbPrinterStatusReader.CancelPrint(job.Config.Connection.UsbDeviceId);
                        }
                        WriteResult(job, true, null);
                    }
                    catch (Exception error)
                    {
                        WriteResult(job, false, error.Message);
                        throw;
                    }
                }
                finally { gate.ReleaseMutex(); }
            }
        }

        internal static void CompletePending(string localState, string folder)
        {
            if (!File.Exists(Path.Combine(folder, "cancel.requested"))) return;
            // A request can arrive as the sender writes its final result.
            // The runtime owns any abort it already attempted.
            var transport = Read<Transport>(Path.Combine(folder, "transport-result.json"));
            if (transport == null || transport.Bytes <= 0 || transport.AbortAttempted) return;
            try { Request(localState, Path.GetFileName(folder)); }
            catch (Exception error) { Program.Log(localState, "Physical cancellation was not acknowledged: " + error.Message); }
        }

        private static bool HasPhysicalData(Job job)
        {
            var result = Read<Transport>(Path.Combine(job.Folder, "transport-result.json"));
            // A cancelled/failed sender may already have filled the printer's
            // buffer. Cancelling RIP alone does not stop those physical bytes.
            return result != null && result.Schema == "pytone-rip.transport-result" && result.Version == 1
                && result.JobId == job.Id && (result.Status == "SENT" || result.Status == "CANCELLED"
                    || result.Status == "FAILED") && result.Bytes > 0
                && result.Destination == Destination(job.Config.Connection);
        }

        private static bool UsesQueue(Connection value) => value.Kind == "windows_queue" || value.UsbMode == "windows_queue"
            || value.UsbMode == null && !string.IsNullOrEmpty(value.PrinterQueue);
        private static string Destination(Connection value) => value.Kind == "network"
            ? "raw_tcp://" + value.Host + ":" + value.Port
            : UsesQueue(value) ? "windows_queue://" + value.PrinterQueue
            : "usbprint://" + value.UsbDeviceId;
        private static bool Same(string left, string right) => !string.IsNullOrEmpty(left)
            && string.Equals(left, right, StringComparison.OrdinalIgnoreCase);
        private static bool SameTarget(Job left, Job right) => Same(Destination(left.Config.Connection), Destination(right.Config.Connection))
            || Same(left.Config.Connection.UsbDeviceId, right.Config.Connection.UsbDeviceId);
        private static Job Latest(IEnumerable<Job> jobs) => jobs.OrderByDescending(item => item.Created).ThenByDescending(item => item.Id, StringComparer.Ordinal).FirstOrDefault();

        private static IEnumerable<Job> ReadJobs(string localState)
        {
            var shared = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Publishers", "qg4mjaqw0qctt", "pyToneRIP.Shared", "jobs");
            foreach (var root in new[] {shared, Path.Combine(localState, "jobs")}.Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (!Directory.Exists(root) || Reparse(root)) continue;
                foreach (var folder in Directory.EnumerateDirectories(root))
                {
                    if (Reparse(folder)) continue;
                    var record = Read<Record>(Path.Combine(folder, "job-record.json"));
                    var config = Read<Configuration>(Path.Combine(folder, "configuration.json"));
                    DateTimeOffset created;
                    if (record == null || record.Schema != "pytone-rip.job-record" || record.Version != 1
                        || record.Id != Path.GetFileName(folder) || record.Metadata == null
                        || string.IsNullOrWhiteSpace(record.Metadata.Queue)
                        || !DateTimeOffset.TryParse(record.Created, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out created)
                        || config == null || config.Schema != "pytone-rip.rip-configuration" || config.Version != 1
                        || config.Connection == null || (config.Connection.Kind != "network"
                            && config.Connection.Kind != "usb" && config.Connection.Kind != "windows_queue")) continue;
                    if (config.Connection.Kind == "network" && (string.IsNullOrWhiteSpace(config.Connection.Host)
                        || config.Connection.Port < 1 || config.Connection.Port > 65535)) continue;
                    if (config.Connection.Kind == "usb" && (config.Connection.UsbDeviceId == null
                        || !config.Connection.UsbDeviceId.StartsWith("USBPRINT\\", StringComparison.OrdinalIgnoreCase))) continue;
                    if (config.Connection.Kind == "windows_queue" && (string.IsNullOrWhiteSpace(config.Connection.PrinterQueue)
                        || string.IsNullOrWhiteSpace(config.Connection.PrinterQueueDriver))) continue;
                    yield return new Job {Id=record.Id, Queue=record.Metadata.Queue, Created=created, Folder=folder, Config=config};
                }
            }
        }

        private static bool Reparse(string path) => (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0;
        private static T Read<T>(string path) where T : class
        {
            try
            {
                if (!File.Exists(path) || Reparse(path) || new FileInfo(path).Length > 1024 * 1024) return null;
                using (var stream = File.OpenRead(path)) return (T)new DataContractJsonSerializer(typeof(T)).ReadObject(stream);
            }
            catch (IOException) { return null; }
            catch (SerializationException) { return null; }
            catch (UnauthorizedAccessException) { return null; }
        }
        private static bool Acknowledged(Job job)
        {
            var result = Read<CancelResult>(Path.Combine(job.Folder, "physical-cancel-result.json"));
            if (result != null && result.Schema == "pytone-rip.physical-cancel-result" && result.Version == 1
                && result.JobId == job.Id && result.Acknowledged) return true;
            var transport = Read<Transport>(Path.Combine(job.Folder, "transport-result.json"));
            return transport != null && transport.AbortAttempted && transport.AbortSucceeded
                && HasPhysicalData(job);
        }
        private static void WriteResult(Job job, bool acknowledged, string error)
        {
            var path = Path.Combine(job.Folder, "physical-cancel-result.json");
            var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            using (var stream = File.Create(temporary)) new DataContractJsonSerializer(typeof(CancelResult)).WriteObject(stream,
                new CancelResult {Schema="pytone-rip.physical-cancel-result", Version=1, JobId=job.Id,
                    Acknowledged=acknowledged, Error=error, Completed=DateTimeOffset.UtcNow.ToString("O")});
            if (File.Exists(path)) File.Replace(temporary, path, null); else File.Move(temporary, path);
        }
        private sealed class Job {internal string Id, Queue, Folder; internal DateTimeOffset Created; internal Configuration Config;}
        [DataContract] private sealed class Record
        {
            [DataMember(Name="schema")] public string Schema { get; set; }
            [DataMember(Name="version")] public int Version { get; set; }
            [DataMember(Name="job_id")] public string Id { get; set; }
            [DataMember(Name="created_utc")] public string Created { get; set; }
            [DataMember(Name="metadata")] public Metadata Metadata { get; set; }
        }
        [DataContract] private sealed class Metadata {[DataMember(Name="queue_name")] public string Queue { get; set; }}
        [DataContract] private sealed class Configuration
        {
            [DataMember(Name="schema")] public string Schema { get; set; }
            [DataMember(Name="version")] public int Version { get; set; }
            [DataMember(Name="connection")] public Connection Connection { get; set; }
        }
        [DataContract] private sealed class Connection
        {
            [DataMember(Name="kind")] public string Kind { get; set; }
            [DataMember(Name="host")] public string Host { get; set; }
            [DataMember(Name="port")] public int Port { get; set; }
            [DataMember(Name="usb_device_id")] public string UsbDeviceId { get; set; }
            [DataMember(Name="printer_queue")] public string PrinterQueue { get; set; }
            [DataMember(Name="printer_queue_driver")] public string PrinterQueueDriver { get; set; }
            [DataMember(Name="usb_transport_mode")] public string UsbMode { get; set; }
        }
        [DataContract] private sealed class Transport
        {
            [DataMember(Name="schema")] public string Schema { get; set; }
            [DataMember(Name="version")] public int Version { get; set; }
            [DataMember(Name="job_id")] public string JobId { get; set; }
            [DataMember(Name="status")] public string Status { get; set; }
            [DataMember(Name="abort_attempted")] public bool AbortAttempted { get; set; }
            [DataMember(Name="abort_succeeded")] public bool AbortSucceeded { get; set; }
            [DataMember(Name="destination")] public string Destination { get; set; }
            [DataMember(Name="bytes_sent")] public long Bytes { get; set; }
        }
        [DataContract] private sealed class CancelResult
        {
            [DataMember(Name="schema")] public string Schema { get; set; }
            [DataMember(Name="version")] public int Version { get; set; }
            [DataMember(Name="job_id")] public string JobId { get; set; }
            [DataMember(Name="acknowledged")] public bool Acknowledged { get; set; }
            [DataMember(Name="error")] public string Error { get; set; }
            [DataMember(Name="completed_utc")] public string Completed { get; set; }
        }
    }
}
