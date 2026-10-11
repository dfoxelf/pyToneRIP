using System;
using System.IO;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Threading.Tasks;
using Windows.Storage;

namespace pyToneRIP.Shared
{
    [DataContract]
    internal sealed class RipRuntimeRequest
    {
        [DataMember(Name = "schema", Order = 1)] public string Schema { get; set; } = "pytone-rip.runtime-request";
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; } = 2;
        [DataMember(Name = "request_id", Order = 3)] public string RequestId { get; set; }
        [DataMember(Name = "job_id", Order = 4)] public string JobId { get; set; }
        [DataMember(Name = "job_record", Order = 5)] public string JobRecord { get; set; }
        [DataMember(Name = "printer_key", Order = 6)] public string PrinterKey { get; set; }
        [DataMember(Name = "enqueued_utc", Order = 7)] public string EnqueuedUtc { get; set; }
    }

    [DataContract]
    internal sealed class RipRuntimeResponse
    {
        [DataMember(Name = "schema", Order = 1)] public string Schema { get; set; }
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; }
        [DataMember(Name = "request_id", Order = 3)] public string RequestId { get; set; }
        [DataMember(Name = "job_id", Order = 4)] public string JobId { get; set; }
        [DataMember(Name = "success", Order = 5)] public bool Success { get; set; }
        [DataMember(Name = "status", Order = 6)] public string Status { get; set; }
        [DataMember(Name = "exit_code", Order = 7)] public int ExitCode { get; set; }
        [DataMember(Name = "error", Order = 8, EmitDefaultValue = false)] public string Error { get; set; }
    }

    [DataContract]
    internal sealed class JobProgressSnapshot
    {
        [DataMember(Name = "schema", Order = 1)] public string Schema { get; set; } = "pytone-rip.job-progress";
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; } = 1;
        [DataMember(Name = "job_id", Order = 3)] public string JobId { get; set; }
        [DataMember(Name = "status", Order = 4)] public string Status { get; set; }
        [DataMember(Name = "stage", Order = 5)] public string Stage { get; set; }
        [DataMember(Name = "percent", Order = 6)] public int Percent { get; set; }
        [DataMember(Name = "message", Order = 7)] public string Message { get; set; }
        [DataMember(Name = "updated_utc", Order = 8)] public string UpdatedUtc { get; set; }
        [DataMember(Name = "terminal", Order = 9)] public bool Terminal { get; set; }
    }

    internal static class JobProgressWriter
    {
        private const string HistoryFileName = "progress-history.log";

        public static async Task WriteAsync(
            StorageFolder jobFolder,
            string jobId,
            int percent,
            string stage,
            string message,
            string status = "RUNNING",
            bool terminal = false)
        {
            var value = new JobProgressSnapshot
            {
                JobId = jobId,
                Status = status,
                Stage = stage,
                Percent = Math.Max(0, Math.Min(100, percent)),
                Message = message,
                UpdatedUtc = DateTimeOffset.UtcNow.ToString("O"),
                Terminal = terminal,
            };
            await AppendHistoryAsync(jobFolder, value);
            var temporary = await jobFolder.CreateFileAsync(
                "progress." + Guid.NewGuid().ToString("N") + ".tmp",
                CreationCollisionOption.FailIfExists);
            using (var stream = await temporary.OpenStreamForWriteAsync())
                new DataContractJsonSerializer(typeof(JobProgressSnapshot)).WriteObject(stream, value);
            var published = false;
            try
            {
                // Manager reads progress.json while the workflow updates it.  A reader can
                // briefly keep the destination open, in which case MoveAndReplaceAsync
                // returns E_ACCESSDENIED.  Progress is diagnostic/UI state and must never
                // abort raster capture or the RIP job, so retry the atomic publish briefly.
                for (var attempt = 0; attempt != 6; ++attempt)
                {
                    try
                    {
                        var target = await jobFolder.TryGetItemAsync("progress.json") as StorageFile;
                        if (target == null)
                            await temporary.RenameAsync(
                                "progress.json", NameCollisionOption.ReplaceExisting);
                        else
                            await temporary.MoveAndReplaceAsync(target);
                        // StorageFile follows the renamed/moved file. Deleting this
                        // object after publication would delete progress.json itself.
                        published = true;
                        return;
                    }
                    catch (UnauthorizedAccessException) when (attempt != 5)
                    {
                        await Task.Delay(40 * (attempt + 1));
                    }
                }
            }
            catch (UnauthorizedAccessException)
            {
                // The latest published snapshot remains usable.  Losing one UI-only
                // update is preferable to failing the actual print workflow.
            }
            finally
            {
                try
                {
                    if (!published)
                        await temporary.DeleteAsync(StorageDeleteOption.PermanentDelete);
                }
                catch (FileNotFoundException)
                {
                    // The successful rename/move consumed the temporary file.
                }
                catch (UnauthorizedAccessException)
                {
                    // A stale uniquely named .tmp file is harmless and can be collected
                    // later.  Never fail a print job because progress cleanup was blocked.
                }
            }
        }

        private static async Task AppendHistoryAsync(
            StorageFolder jobFolder, JobProgressSnapshot value)
        {
            try
            {
                var history = await jobFolder.CreateFileAsync(
                    HistoryFileName, CreationCollisionOption.OpenIfExists);
                var message = (value.Message ?? "")
                    .Replace("\r", " ")
                    .Replace("\n", " ");
                await FileIO.AppendTextAsync(
                    history,
                    value.UpdatedUtc + " | " + value.Percent + "% | "
                    + value.Stage + " | " + message + Environment.NewLine);
            }
            catch
            {
                // Stage history is for the optional Manager log. Failure to append
                // it must never fail or delay the actual print job.
            }
        }
    }
}
