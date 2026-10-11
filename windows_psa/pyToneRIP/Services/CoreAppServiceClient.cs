using System;
using System.IO;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Threading.Tasks;
using pyToneRIP.Shared;
using Windows.ApplicationModel;
using Windows.ApplicationModel.AppService;
using Windows.Foundation.Collections;
using Windows.Storage;

namespace pyToneRIP.Tasks.Services
{
    internal static class CoreAppServiceClient
    {
        public const string ServiceName = "pytone-rip.core.v1";
        public const string ManagerPackageFamilyName =
            "pyToneRIP.Manager_qg4mjaqw0qctt";

        public static bool IsModelPackage => Package.Current.Id.Name.StartsWith(
            "pyToneRIP.Printer.", StringComparison.OrdinalIgnoreCase);

        public static async Task<RipRuntimeResponse> ExecuteAsync(StorageFile jobRecord)
        {
            if (!IsModelPackage)
                return await new RipRuntimeClient().ExecuteAsync(jobRecord);
            var jobFolder = await jobRecord.GetParentAsync();
            using (var connection = new AppServiceConnection
            {
                AppServiceName = ServiceName,
                PackageFamilyName = ManagerPackageFamilyName,
            })
            {
                var opened = await connection.OpenAsync();
                if (opened != AppServiceConnectionStatus.Success)
                    throw new InvalidOperationException(
                        "pyToneRIP Core is unavailable: " + opened + ".");
                var response = await connection.SendMessageAsync(new ValueSet
                {
                    ["schema"] = "pytone-rip.core-request",
                    ["version"] = 1,
                    ["operation"] = "render-job",
                    ["job_id"] = jobFolder.Name,
                });
                if (response.Status != AppServiceResponseStatus.Success)
                {
                    // Windows can close a synchronous AppService request after
                    // roughly 90 seconds even though the full-trust RIP process
                    // is still making progress. The runtime writes atomically to
                    // the publisher-shared job folder, so recover that legitimate
                    // long job by following its terminal progress record.
                    if (response.Status == AppServiceResponseStatus.Failure)
                    {
                        var recovered = await WaitForSharedTerminalResultAsync(
                            jobFolder, jobFolder.Name);
                        if (recovered != null) return recovered;
                    }
                    throw new InvalidOperationException(
                        "pyToneRIP Core request failed: " + response.Status + ".");
                }
                var values = response.Message;
                if (values.ContainsKey("status")
                    && string.Equals(values["status"] as string, "CANCELLED",
                    StringComparison.OrdinalIgnoreCase))
                    throw new OperationCanceledException(
                        values["error"] as string ?? "Windows print job was canceled.");
                if (!(values["success"] is bool success) || !success)
                    throw new InvalidOperationException(
                        "pyToneRIP Core failed: " + (values["error"] as string ?? "unknown error"));
                return new RipRuntimeResponse
                {
                    Schema = "pytone-rip.runtime-response",
                    Version = 1,
                    JobId = jobFolder.Name,
                    Success = true,
                    Status = values["status"] as string,
                };
            }
        }

        private static async Task<RipRuntimeResponse> WaitForSharedTerminalResultAsync(
            StorageFolder jobFolder,
            string jobId)
        {
            var runtimeObserved = false;
            DateTimeOffset? activeDeadline = null;
            var missingProgressAttempts = 0;
            // Waiting behind another job has no processing deadline.  Once this
            // job reaches RUNTIME_START, QueueHelper gives it its own 30 minutes;
            // the client keeps one additional minute for response publication.
            while (true)
            {
                JobProgressSnapshot progress = null;
                try
                {
                    var file = await jobFolder.GetFileAsync("progress.json");
                    using (var stream = await file.OpenStreamForReadAsync())
                        progress = (JobProgressSnapshot)new DataContractJsonSerializer(
                            typeof(JobProgressSnapshot)).ReadObject(stream);
                }
                catch (FileNotFoundException) { }
                catch (IOException) { }
                catch (SerializationException) { }

                if (progress != null
                    && progress.Schema == "pytone-rip.job-progress"
                    && progress.Version == 1
                    && string.Equals(progress.JobId, jobId, StringComparison.OrdinalIgnoreCase))
                {
                    runtimeObserved = runtimeObserved || progress.Percent >= 32
                        || string.Equals(progress.Stage, "RUNTIME_START",
                            StringComparison.OrdinalIgnoreCase);
                    if (runtimeObserved && !activeDeadline.HasValue)
                        activeDeadline = DateTimeOffset.UtcNow.AddMinutes(31);
                    if (progress.Terminal)
                    {
                        if (string.Equals(progress.Status, "CANCELLED",
                            StringComparison.OrdinalIgnoreCase))
                            throw new OperationCanceledException(
                                progress.Message ?? "Windows print job was canceled.");
                        if (string.Equals(progress.Status, "SENT",
                            StringComparison.OrdinalIgnoreCase))
                            return new RipRuntimeResponse
                            {
                                Schema = "pytone-rip.runtime-response",
                                Version = 1,
                                JobId = jobId,
                                Success = true,
                                Status = progress.Status,
                            };
                        throw new InvalidOperationException(
                            "pyToneRIP Core failed: "
                            + (progress.Message ?? progress.Status ?? "unknown error"));
                    }
                }

                missingProgressAttempts = progress == null
                    ? missingProgressAttempts + 1
                    : 0;

                if (File.Exists(Path.Combine(jobFolder.Path, "cancel.requested")))
                    throw new OperationCanceledException(
                        "Windows print job was canceled while waiting for its printer.");

                // If no runtime process is visible shortly after the AppService
                // failure, this was not the recoverable 90-second disconnect.
                // A legitimately queued job remains at RUNTIME_QUEUED (30%), so
                // the existence of that valid snapshot is also sufficient proof
                // that this request belongs to the durable broker.
                if (!runtimeObserved && missingProgressAttempts >= 25) return null;
                if (activeDeadline.HasValue
                    && DateTimeOffset.UtcNow >= activeDeadline.Value)
                    throw new TimeoutException(
                        "Packaged Python RIP did not finish after its own active timeout.");
                await Task.Delay(200);
            }
        }
    }
}
