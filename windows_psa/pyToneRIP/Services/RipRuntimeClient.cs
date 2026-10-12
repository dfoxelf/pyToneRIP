using System;
using System.IO;
using System.Net;
using System.Runtime.Serialization.Json;
using System.Threading.Tasks;
using pyToneRIP.Shared;
using Windows.ApplicationModel;
using Windows.Storage;
using Windows.Data.Json;

namespace pyToneRIP.Tasks.Services
{
    internal sealed class RipRuntimeClient
    {
        private const string ParameterGroupId = "RipCommand";
        private const string RequestFolderName = "rip-runtime";

        public async Task<RipRuntimeResponse> ExecuteAsync(StorageFile jobRecord)
        {
            if (jobRecord == null)
                throw new ArgumentNullException(nameof(jobRecord));
            var jobFolder = await jobRecord.GetParentAsync();
            var jobId = jobFolder.Name;
            var requestId = Guid.NewGuid().ToString("N");
            var printerKey = await ReadPhysicalPrinterKeyAsync(jobFolder);
            var enqueuedUtc = await ReadJobAcceptedUtcAsync(jobRecord);
            var folder = await ApplicationData.Current.LocalFolder.CreateFolderAsync(
                RequestFolderName, CreationCollisionOption.OpenIfExists);
            var requestName = requestId + ".request.json";
            var responseName = requestId + ".response.json";
            var request = new RipRuntimeRequest
            {
                RequestId = requestId,
                JobId = jobId,
                JobRecord = jobRecord.Path,
                PrinterKey = printerKey,
                EnqueuedUtc = enqueuedUtc,
            };
            var temporary = await folder.CreateFileAsync(
                requestId + ".request.tmp", CreationCollisionOption.FailIfExists);
            using (var stream = await temporary.OpenStreamForWriteAsync())
                new DataContractJsonSerializer(typeof(RipRuntimeRequest)).WriteObject(stream, request);
            await temporary.RenameAsync(requestName, NameCollisionOption.FailIfExists);

            await FullTrustProcessLauncher.LaunchFullTrustProcessForCurrentAppAsync(
                ParameterGroupId);
            var responseFile = await WaitForResponseAsync(
                folder, responseName, jobFolder, jobId);
            try
            {
                RipRuntimeResponse response;
                using (var stream = await responseFile.OpenStreamForReadAsync())
                    response = (RipRuntimeResponse)new DataContractJsonSerializer(
                        typeof(RipRuntimeResponse)).ReadObject(stream);
                if (response == null ||
                    response.Schema != "pytone-rip.runtime-response" ||
                    response.Version != 1 ||
                    response.RequestId != requestId ||
                    response.JobId != jobId)
                    throw new InvalidDataException("Packaged RIP returned an invalid response.");
                if (string.Equals(response.Status, "CANCELLED", StringComparison.OrdinalIgnoreCase))
                    throw new OperationCanceledException(
                        response.Error ?? "Windows print job was canceled.");
                if (!response.Success || response.Status != "SENT")
                    throw new InvalidOperationException(
                        "Packaged RIP failed (exit " + response.ExitCode + "): "
                        + (response.Error ?? response.Status ?? "unknown error"));
                return response;
            }
            finally
            {
                await responseFile.DeleteAsync(StorageDeleteOption.PermanentDelete);
            }
        }

        private static async Task<StorageFile> WaitForResponseAsync(
            StorageFolder folder,
            string responseName,
            StorageFolder jobFolder,
            string jobId)
        {
            DateTimeOffset? activeDeadline = null;
            while (true)
            {
                try
                {
                    return await folder.GetFileAsync(responseName);
                }
                catch (FileNotFoundException)
                {
                }

                if (File.Exists(Path.Combine(jobFolder.Path, "cancel.requested")))
                    throw new OperationCanceledException(
                        "Windows print job was canceled while waiting for its printer.");

                // Queue waiting is intentionally not charged to the job's
                // processing timeout.  Start a separate deadline only after the
                // packaged Python process reports RUNTIME_START.  QueueHelper
                // enforces the exact 30-minute active deadline; this extra minute
                // merely lets it publish the terminal response atomically.
                if (!activeDeadline.HasValue
                    && await RuntimeHasStartedAsync(jobFolder, jobId))
                    activeDeadline = DateTimeOffset.UtcNow.AddMinutes(31);
                if (activeDeadline.HasValue
                    && DateTimeOffset.UtcNow >= activeDeadline.Value)
                    throw new TimeoutException(
                        "Packaged Python RIP did not respond after its own active timeout.");

                await Task.Delay(200);
            }
        }

        private static async Task<bool> RuntimeHasStartedAsync(
            StorageFolder jobFolder,
            string jobId)
        {
            try
            {
                var file = await jobFolder.GetFileAsync("progress.json");
                JobProgressSnapshot progress;
                using (var stream = await file.OpenStreamForReadAsync())
                    progress = (JobProgressSnapshot)new DataContractJsonSerializer(
                        typeof(JobProgressSnapshot)).ReadObject(stream);
                return progress != null
                    && progress.Schema == "pytone-rip.job-progress"
                    && progress.Version == 1
                    && string.Equals(
                        progress.JobId, jobId, StringComparison.OrdinalIgnoreCase)
                    && (progress.Percent >= 32
                        || string.Equals(
                            progress.Stage, "RUNTIME_START",
                            StringComparison.OrdinalIgnoreCase));
            }
            catch (FileNotFoundException) { return false; }
            catch (IOException) { return false; }
            catch (System.Runtime.Serialization.SerializationException) { return false; }
        }

        private static async Task<string> ReadPhysicalPrinterKeyAsync(
            StorageFolder jobFolder)
        {
            var file = await jobFolder.GetFileAsync("configuration.json");
            var json = await FileIO.ReadTextAsync(file);
            JsonObject root;
            if (!JsonObject.TryParse(json, out root))
                throw new InvalidDataException("pyToneRIP configuration JSON is invalid.");
            var connection = root.GetNamedObject("connection", null);
            if (connection == null)
                throw new InvalidDataException("pyToneRIP connection configuration is missing.");
            var kind = connection.GetNamedString("kind", "").Trim().ToLowerInvariant();
            if (kind == "network")
            {
                var hostText = connection.GetNamedString("host", "").Trim();
                IPAddress address;
                if (!IPAddress.TryParse(hostText, out address))
                    throw new InvalidDataException("pyToneRIP network address is invalid.");
                var portValue = connection.GetNamedNumber("port", 0);
                var port = (int)portValue;
                if (portValue != port || port < 1 || port > 65535)
                    throw new InvalidDataException("pyToneRIP network port is invalid.");
                return "network|" + address + "|" + port;
            }
            if (kind == "usb")
            {
                var deviceId = connection.GetNamedString("usb_device_id", "").Trim();
                if (string.IsNullOrWhiteSpace(deviceId))
                    throw new InvalidDataException("pyToneRIP USB device identity is missing.");
                return "usb|" + deviceId.ToUpperInvariant();
            }
            throw new InvalidDataException("pyToneRIP connection kind is invalid.");
        }

        private static async Task<string> ReadJobAcceptedUtcAsync(StorageFile jobRecord)
        {
            var json = await FileIO.ReadTextAsync(jobRecord);
            JsonObject root;
            if (!JsonObject.TryParse(json, out root))
                throw new InvalidDataException("pyToneRIP job record JSON is invalid.");
            var value = root.GetNamedString("created_utc", "");
            DateTimeOffset acceptedUtc;
            if (!DateTimeOffset.TryParse(value, out acceptedUtc))
                throw new InvalidDataException(
                    "pyToneRIP job record creation timestamp is invalid.");
            return acceptedUtc.ToUniversalTime().ToString("O");
        }
    }
}
