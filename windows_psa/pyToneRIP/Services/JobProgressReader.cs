using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Threading.Tasks;
using pyToneRIP.Shared;
using Windows.Storage;

namespace pyToneRIP.Services
{
    internal sealed class PrinterJobActivity
    {
        public string JobFolderName { get; set; }
        public string QueueName { get; set; }
        public string Host { get; set; }
        public int Port { get; set; }
        public string State { get; set; }
    }

    internal static class JobProgressReader
    {
        [DataContract]
        private sealed class TransportActivity
        {
            [DataMember(Name = "schema")] public string Schema { get; set; }
            [DataMember(Name = "version")] public int Version { get; set; }
            [DataMember(Name = "job_id")] public string JobId { get; set; }
            [DataMember(Name = "host")] public string Host { get; set; }
            [DataMember(Name = "active")] public bool Active { get; set; }
            [DataMember(Name = "updated_utc")] public string UpdatedUtc { get; set; }
        }

        [DataContract]
        private sealed class JobConfigurationConnection
        {
            [DataMember(Name = "kind")] public string Kind { get; set; }
            [DataMember(Name = "host")] public string Host { get; set; }
            [DataMember(Name = "port")] public int Port { get; set; }
        }

        [DataContract]
        private sealed class JobConfiguration
        {
            [DataMember(Name = "schema")] public string Schema { get; set; }
            [DataMember(Name = "version")] public int Version { get; set; }
            [DataMember(Name = "connection")] public JobConfigurationConnection Connection { get; set; }
        }

        private static readonly Dictionary<string, int> HistoryLineCounts =
            new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);

        public static async Task<JobProgressSnapshot> ReadLatestAsync()
        {
            StorageFolder jobs;
            try
            {
                jobs = await pyToneRIP.Shared.SharedApplicationStorage.Root.GetFolderAsync("jobs");
            }
            catch (FileNotFoundException)
            {
                return null;
            }

            JobProgressSnapshot latest = null;
            DateTimeOffset latestTime = DateTimeOffset.MinValue;
            foreach (var folder in await jobs.GetFoldersAsync())
            {
                try
                {
                    var file = await folder.GetFileAsync("progress.json");
                    JobProgressSnapshot value;
                    using (var stream = await file.OpenStreamForReadAsync())
                        value = (JobProgressSnapshot)new DataContractJsonSerializer(
                            typeof(JobProgressSnapshot)).ReadObject(stream);
                    DateTimeOffset updated;
                    if (value == null ||
                        value.Schema != "pytone-rip.job-progress" ||
                        value.Version != 1 ||
                        !DateTimeOffset.TryParse(value.UpdatedUtc, out updated))
                        continue;
                    if (updated > latestTime)
                    {
                        latest = value;
                        latestTime = updated;
                    }
                }
                catch (FileNotFoundException) { }
                catch (IOException) { }
                catch (System.Runtime.Serialization.SerializationException) { }
            }
            return latest;
        }

        public static async Task<IReadOnlyCollection<string>> ReadActiveTransmissionsAsync()
        {
            var active = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            StorageFolder jobs;
            try
            {
                jobs = await pyToneRIP.Shared.SharedApplicationStorage.Root.GetFolderAsync("jobs");
            }
            catch (FileNotFoundException) { return active; }

            foreach (var folder in await jobs.GetFoldersAsync())
            {
                try
                {
                    var file = await folder.GetFileAsync("transport-activity.json");
                    TransportActivity value;
                    using (var stream = await file.OpenStreamForReadAsync())
                        value = (TransportActivity)new DataContractJsonSerializer(
                            typeof(TransportActivity)).ReadObject(stream);
                    DateTimeOffset updated;
                    if (value?.Schema != "pytone-rip.transport-activity" ||
                        value.Version != 1 || !value.Active ||
                        string.IsNullOrWhiteSpace(value.Host) ||
                        !DateTimeOffset.TryParse(value.UpdatedUtc, out updated) ||
                        updated > DateTimeOffset.UtcNow.AddMinutes(1) ||
                        updated < DateTimeOffset.UtcNow.AddMinutes(-6))
                        continue;
                    active.Add(value.Host.Trim());
                }
                catch (FileNotFoundException) { }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
                catch (SerializationException) { }
            }
            return active;
        }

        public static async Task<IReadOnlyCollection<PrinterJobActivity>> ReadActiveJobActivitiesAsync()
        {
            var activities = new List<PrinterJobActivity>();
            StorageFolder jobs;
            try
            {
                jobs = await SharedApplicationStorage.Root.GetFolderAsync("jobs");
            }
            catch (FileNotFoundException) { return activities; }

            var now = DateTimeOffset.UtcNow;
            foreach (var folder in await jobs.GetFoldersAsync())
            {
                try
                {
                    var progressFile = await folder.GetFileAsync("progress.json");
                    JobProgressSnapshot progress;
                    using (var stream = await progressFile.OpenStreamForReadAsync())
                        progress = (JobProgressSnapshot)new DataContractJsonSerializer(
                            typeof(JobProgressSnapshot)).ReadObject(stream);
                    DateTimeOffset updated;
                    var queued = string.Equals(progress?.Stage, "RUNTIME_QUEUED",
                        StringComparison.OrdinalIgnoreCase);
                    if (progress?.Schema != "pytone-rip.job-progress"
                        || progress.Version != 1 || progress.Terminal
                        || !string.Equals(progress.Status, "RUNNING",
                            StringComparison.OrdinalIgnoreCase)
                        || string.IsNullOrWhiteSpace(progress.Stage)
                        || !DateTimeOffset.TryParse(progress.UpdatedUtc, out updated)
                        || updated > now.AddMinutes(1)
                        || (!queued && updated < now.AddMinutes(-35)))
                        continue;

                    var activity = new PrinterJobActivity
                    {
                        JobFolderName = folder.Name,
                        // Queue waiting has no processing deadline and does not
                        // write progress heartbeats while the printer is busy.
                        State = queued ? "Queued"
                            : progress.Percent >= 32 ? "Processing" : "Preparing",
                    };
                    // USBPRINT has no network host. Read the actual transport
                    // activity for this job instead of inferring transmission
                    // from a RIP percentage or a network address.
                    try
                    {
                        var transportFile = await folder.GetFileAsync("transport-activity.json");
                        TransportActivity transport;
                        using (var stream = await transportFile.OpenStreamForReadAsync())
                            transport = (TransportActivity)new DataContractJsonSerializer(
                                typeof(TransportActivity)).ReadObject(stream);
                        DateTimeOffset transportUpdated;
                        if (!queued && transport?.Schema == "pytone-rip.transport-activity"
                            && transport.Version == 1 && transport.Active
                            && string.Equals(transport.JobId, progress.JobId, StringComparison.Ordinal)
                            && DateTimeOffset.TryParse(transport.UpdatedUtc, out transportUpdated)
                            && transportUpdated <= now.AddMinutes(1)
                            && transportUpdated >= now.AddMinutes(-6))
                            activity.State = "Printing";
                    }
                    catch (FileNotFoundException) { }
                    catch (IOException) { }
                    catch (UnauthorizedAccessException) { }
                    catch (SerializationException) { }
                    try
                    {
                        var queueFile = await folder.GetFileAsync("job-queue.txt");
                        using (var stream = await queueFile.OpenStreamForReadAsync())
                        using (var reader = new StreamReader(stream))
                            activity.QueueName = (await reader.ReadToEndAsync()).Trim();
                    }
                    catch (FileNotFoundException) { }

                    try
                    {
                        var configurationFile = await folder.GetFileAsync("configuration.json");
                        JobConfiguration configuration;
                        using (var stream = await configurationFile.OpenStreamForReadAsync())
                            configuration = (JobConfiguration)new DataContractJsonSerializer(
                                typeof(JobConfiguration)).ReadObject(stream);
                        if (configuration?.Schema == "pytone-rip.configuration"
                            && configuration.Version == 1
                            && string.Equals(configuration.Connection?.Kind, "network",
                                StringComparison.OrdinalIgnoreCase)
                            && !string.IsNullOrWhiteSpace(configuration.Connection.Host)
                            && configuration.Connection.Port >= 1
                            && configuration.Connection.Port <= 65535)
                        {
                            activity.Host = configuration.Connection.Host.Trim();
                            activity.Port = configuration.Connection.Port;
                        }
                    }
                    catch (FileNotFoundException) { }
                    catch (IOException) { }
                    catch (SerializationException) { }

                    if (!string.IsNullOrWhiteSpace(activity.Host)
                        || !string.IsNullOrWhiteSpace(activity.QueueName))
                        activities.Add(activity);
                }
                catch (FileNotFoundException) { }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
                catch (SerializationException) { }
            }
            return activities;
        }

        public static async Task<IReadOnlyList<string>> ReadNewStageLinesAsync(
            string jobId)
        {
            var result = new List<string>();
            if (string.IsNullOrWhiteSpace(jobId))
                return result;
            try
            {
                var jobs = await pyToneRIP.Shared.SharedApplicationStorage.Root.GetFolderAsync(
                    "jobs");
                var folder = await jobs.GetFolderAsync(jobId);
                var history = await folder.GetFileAsync("progress-history.log");
                var lines = await FileIO.ReadLinesAsync(history);
                int consumed;
                if (!HistoryLineCounts.TryGetValue(jobId, out consumed)
                    || consumed > lines.Count)
                    consumed = 0;
                for (var index = consumed; index < lines.Count; index++)
                {
                    if (!string.IsNullOrWhiteSpace(lines[index]))
                        result.Add("PRINT | Job " + jobId + " | " + lines[index]);
                }
                HistoryLineCounts[jobId] = lines.Count;
            }
            catch (FileNotFoundException) { }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
            return result;
        }
    }
}
