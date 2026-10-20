using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using pyToneRIP.Shared;
using Windows.Data.Json;
using Windows.Graphics.Printing.Workflow;
using Windows.Storage;

namespace pyToneRIP.Tasks.Services
{
    internal sealed class JobRecordWriter
    {
        public const string CancellationFileName = "cancel.requested";
        public const string BoundaryStatus = "WINDOWS_TIFF_READY_PYTHON_PENDING";
        public StorageFolder JobFolder { get; private set; }
        public string JobId { get; private set; }
        private int cancellationRequested;
        private string cancellationReason = "Windows print job was canceled.";
        private string acceptedUtc;

        public async Task BeginAsync(string queueName)
        {
            // This runs as the first action after Windows delivers print data.
            // A virtual-printer session must be started synchronously; awaiting
            // storage work before session.Start() makes the Windows print
            // processor cancel the job. Keep progress creation here instead.
            if (JobFolder != null && !string.IsNullOrWhiteSpace(JobId))
                return;

            // Capture queue order before any PDL copy or XpsToTiff work.  Two
            // jobs can rasterize at different speeds; ordering them by the time
            // capture finishes would allow the later job to overtake the first.
            acceptedUtc = DateTimeOffset.UtcNow.ToString("O");
            var jobId = Guid.NewGuid().ToString("D");
            var jobs = await SharedApplicationStorage.Root.CreateFolderAsync(
                "jobs", CreationCollisionOption.OpenIfExists);
            var folder = await jobs.CreateFolderAsync(jobId, CreationCollisionOption.FailIfExists);
            JobFolder = folder;
            JobId = jobId;
            PublishCancellationMarkerIfNeeded();
            ThrowIfCancellationRequested();
            // Configuration is captured only after Windows renders the page.
            // Keep the queue name beside the first progress snapshot so Manager
            // can identify the printer throughout that earlier preparation.
            var queueFile = await folder.CreateFileAsync(
                "job-queue.txt", CreationCollisionOption.FailIfExists);
            await FileIO.WriteTextAsync(queueFile, queueName ?? string.Empty);
            await JobProgressWriter.WriteAsync(
                folder,
                jobId,
                1,
                "WINDOWS_JOB_ACCEPTED",
                "Windows print job accepted; preparing RIP job");
            pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage(
                "JobRecordWriter progress initialized for "
                + (string.IsNullOrWhiteSpace(queueName) ? "unknown queue" : queueName)
                + ": " + jobId);
        }

        public async Task<StorageFile> CaptureAsync(
            PrintWorkflowVirtualPrinterSession session,
            PrintWorkflowVirtualPrinterDataAvailableEventArgs args)
        {
            pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage(
                "JobRecordWriter.CaptureAsync entered");
            if (JobFolder == null || string.IsNullOrWhiteSpace(JobId))
                await BeginAsync(session.Printer.PrinterName);
            ThrowIfCancellationRequested();

            var jobId = JobId;
            var folder = JobFolder;
            await JobProgressWriter.WriteAsync(
                folder, jobId, 2, "WINDOWS_DATA_AVAILABLE", "Windows print data received");
            var contentType = args.SourceContent.ContentType;
            var pdlName = contentType == "application/oxps" ? "payload.oxps" : "payload.pdl";
            var pdlFile = await folder.CreateFileAsync(pdlName, CreationCollisionOption.FailIfExists);
            var pdlArtifact = await CopyAndHashAsync(args.SourceContent.GetInputStream(), pdlFile);
            ThrowIfCancellationRequested();
            pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage(
                "JobRecordWriter PDL captured: " + pdlArtifact.SizeBytes + " bytes");
            await JobProgressWriter.WriteAsync(
                folder, jobId, 10, "PDL_CAPTURED", "Windows page description captured");

            var ticket = args.GetJobPrintTicket();
            const string printKeywords =
                "http://schemas.microsoft.com/windows/2003/08/printing/printschemakeywords";
            var copies = ticket.GetParameterInitializer(
                "JobCopiesAllDocuments", printKeywords)?.Value?.GetValueAsInteger() ?? 1;
            if (copies < 1 || copies > 99)
                throw new InvalidDataException(
                    "JobCopiesAllDocuments must be between 1 and 99.");
            var ticketText = ticket.XmlNode.GetXml();
            var ticketArtifact = await WriteTextAndHashAsync(folder, "print-ticket.xml", ticketText);
            var adapterTiffFile = await folder.CreateFileAsync(
                "windows-converter-output.tiff",
                CreationCollisionOption.FailIfExists);
            var converter = args.GetPdlConverter(PrintWorkflowPdlConversionType.XpsToTiff);
            pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage(
                "JobRecordWriter XpsToTiff conversion begin");
            await JobProgressWriter.WriteAsync(
                folder, jobId, 15, "WINDOWS_RENDER", "Windows is rasterizing the page");
            using (var source = await pdlFile.OpenAsync(FileAccessMode.Read))
            using (var target = await adapterTiffFile.OpenAsync(FileAccessMode.ReadWrite))
            {
                // The RIP repeats the completed native job for each copy.
                // Exclude host copy expansion so XpsToTiff supplies each page once.
                // Python orders completed page streams according to DocumentCollate.
                var hostProcessing =
                    PdlConversionHostBasedProcessingOperations.PageRotation |
                    PdlConversionHostBasedProcessingOperations.PageOrdering |
                    PdlConversionHostBasedProcessingOperations.BlankPageInsertion;
                await converter.ConvertPdlAsync(ticket, source, target, hostProcessing);
                await target.FlushAsync();
            }
            ThrowIfCancellationRequested();
            var adapterTiffArtifact = await HashFileAsync(adapterTiffFile);
            pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage(
                "JobRecordWriter XpsToTiff conversion complete: "
                + adapterTiffArtifact.SizeBytes + " bytes");
            await JobProgressWriter.WriteAsync(
                folder, jobId, 25, "WINDOWS_TIFF_READY", "Windows page raster is ready");
            var configurationJson = await ReadAndValidateConfigurationAsync(ticket, session);
            ThrowIfCancellationRequested();
            var sourceCurvePaths = ReadExternalCurvePaths(configurationJson);
            var configurationArtifact = await WriteTextAndHashAsync(
                folder, "configuration.json", configurationJson);
            await JobProgressWriter.WriteAsync(
                folder, jobId, 28, "CONFIGURATION_READY", "RIP configuration captured");

            pdlArtifact.ContentType = contentType;
            var record = new JobRecord
            {
                Schema = "pytone-rip.job-record",
                Version = 1,
                JobId = jobId,
                CreatedUtc = acceptedUtc,
                Status = BoundaryStatus,
                Metadata = new JobMetadata
                {
                    JobTitle = args.Configuration.JobTitle,
                    SourceApplication = args.Configuration.SourceAppDisplayName,
                    SessionId = args.Configuration.SessionId,
                    QueueName = session.Printer.PrinterName,
                    PrinterUri = session.Printer.PrinterUri?.ToString(),
                    ConfigurationKey = QueueIdentity.StableKey(
                        session.Printer.PrinterUri?.ToString(),
                        session.Printer.PrinterName),
                    PdlContentType = contentType,
                    Copies = copies,
                    SourceCurvePaths = sourceCurvePaths
                },
                Artifacts = new JobArtifacts
                {
                    Pdl = pdlArtifact,
                    PrintTicket = ticketArtifact,
                    Configuration = configurationArtifact,
                    AdapterTiff = adapterTiffArtifact
                }
            };
            var recordFile = await folder.CreateFileAsync(
                "job-record.json", CreationCollisionOption.FailIfExists);
            using (var stream = await recordFile.OpenStreamForWriteAsync())
            {
                var serializer = new DataContractJsonSerializer(typeof(JobRecord));
                serializer.WriteObject(stream, record);
            }
            pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage(
                "JobRecordWriter job record written: " + jobId);
            await JobProgressWriter.WriteAsync(
                folder, jobId, 30, "RUNTIME_QUEUED", "Packaged Python RIP is queued");
            return recordFile;
        }

        public void RequestCancellation(string reason)
        {
            if (!string.IsNullOrWhiteSpace(reason))
                cancellationReason = reason;
            Interlocked.Exchange(ref cancellationRequested, 1);
            PublishCancellationMarkerIfNeeded();
        }

        public Task WriteCancelledAsync(string reason)
        {
            RequestCancellation(reason);
            if (JobFolder == null || string.IsNullOrWhiteSpace(JobId))
                return Task.CompletedTask;
            return JobProgressWriter.WriteAsync(
                JobFolder,
                JobId,
                100,
                "CANCELLED",
                cancellationReason,
                "CANCELLED",
                true);
        }

        private void ThrowIfCancellationRequested()
        {
            if (Volatile.Read(ref cancellationRequested) != 0 ||
                (JobFolder != null && File.Exists(
                    Path.Combine(JobFolder.Path, CancellationFileName))))
                throw new OperationCanceledException(cancellationReason);
        }

        private void PublishCancellationMarkerIfNeeded()
        {
            var folder = JobFolder;
            if (Volatile.Read(ref cancellationRequested) == 0 || folder == null)
                return;
            try
            {
                File.WriteAllText(
                    Path.Combine(folder.Path, CancellationFileName),
                    DateTimeOffset.UtcNow.ToString("O") + Environment.NewLine
                    + cancellationReason + Environment.NewLine,
                    Encoding.UTF8);
            }
            catch (IOException)
            {
                // A simultaneous caller may already have published the marker.
                if (!File.Exists(Path.Combine(folder.Path, CancellationFileName)))
                    throw;
            }
        }

        public Task WriteFailureAsync(Exception error)
        {
            if (JobFolder == null || string.IsNullOrWhiteSpace(JobId))
                return Task.CompletedTask;
            return JobProgressWriter.WriteAsync(
                JobFolder,
                JobId,
                100,
                "FAILED",
                error.GetType().Name + ": " + error.Message,
                "FAILED",
                true);
        }

        private static async Task<string> ReadAndValidateConfigurationAsync(
            Windows.Graphics.Printing.PrintTicket.WorkflowPrintTicket ticket,
            PrintWorkflowVirtualPrinterSession session)
        {
            const string parameterName = "ConfigurationV1";
            const string parameterNamespace =
                "https://pytone-rip.local/printticket/2026/phase12";
            var initializer = ticket.GetParameterInitializer(parameterName, parameterNamespace);
            var json = initializer?.Value?.GetValueAsString();
            if (string.IsNullOrWhiteSpace(json) || json == "{}")
            {
                json = await QueueConfigurationStorage.ReadAsync(
                    session.Printer.PrinterUri?.ToString(),
                    session.Printer.PrinterName);
                if (string.IsNullOrWhiteSpace(json))
                    throw new InvalidDataException(
                        "pyToneRIP configuration is missing for queue "
                        + session.Printer.PrinterName + ".");
            }

            JsonObject configuration;
            if (!JsonObject.TryParse(json, out configuration) ||
                configuration.GetNamedString("schema", "") != "pytone-rip.configuration" ||
                configuration.GetNamedNumber("version", 0) != 1)
            {
                throw new InvalidDataException("pyToneRIP configuration schema is invalid.");
            }
            var capabilityFile = await StorageFile.GetFileFromApplicationUriAsync(
                new Uri("ms-appx:///Assets/capabilities.generated.json"));
            var capabilityJson = await FileIO.ReadTextAsync(capabilityFile);
            return PrintTicketSynchronization.OverlayConfiguration(
                ticket, json, capabilityJson);
        }

        private static List<string> ReadExternalCurvePaths(string configurationJson)
        {
            JsonObject configuration;
            if (!JsonObject.TryParse(configurationJson, out configuration))
                throw new InvalidDataException("pyToneRIP configuration JSON is invalid.");
            if (configuration.ContainsKey("calibration")
                && configuration.GetNamedObject("calibration").GetNamedBoolean("enabled", false))
            {
                // RAW calibration deliberately bypasses external QTR curves.
                // An empty source-curve list is valid only for this mode.
                return new List<string>();
            }
            var profiles = configuration.GetNamedObject("profiles");
            var curves = profiles.GetNamedArray("curves");
            if (curves.Count < 1 || curves.Count > 3)
                throw new InvalidDataException("pyToneRIP requires one to three external curves.");

            var sourcePaths = new List<string>();
            foreach (var value in curves)
            {
                var sourcePath = Path.GetFullPath(value.GetString());
                if (!string.Equals(
                    Path.GetExtension(sourcePath), ".quad", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Only external .quad curve files are supported.");
                sourcePaths.Add(sourcePath);
            }
            return sourcePaths;
        }

        private static async Task<JobArtifact> WriteTextAndHashAsync(
            StorageFolder folder, string name, string text)
        {
            var bytes = Encoding.UTF8.GetBytes(text);
            var file = await folder.CreateFileAsync(name, CreationCollisionOption.FailIfExists);
            await FileIO.WriteBytesAsync(file, bytes);
            return new JobArtifact
            {
                File = name,
                SizeBytes = bytes.LongLength,
                Sha256 = Sha256(bytes)
            };
        }

        private static async Task<JobArtifact> CopyAndHashAsync(
            Windows.Storage.Streams.IInputStream source,
            StorageFile target)
        {
            long total = 0;
            using (var sha = SHA256.Create())
            using (var input = source.AsStreamForRead())
            using (var output = await target.OpenStreamForWriteAsync())
            {
                var buffer = new byte[1024 * 1024];
                int count;
                while ((count = await input.ReadAsync(buffer, 0, buffer.Length)) > 0)
                {
                    await output.WriteAsync(buffer, 0, count);
                    sha.TransformBlock(buffer, 0, count, null, 0);
                    total += count;
                }
                sha.TransformFinalBlock(Array.Empty<byte>(), 0, 0);
                await output.FlushAsync();
                return new JobArtifact
                {
                    File = target.Name,
                    SizeBytes = total,
                    Sha256 = Hex(sha.Hash)
                };
            }
        }

        private static async Task<JobArtifact> HashFileAsync(StorageFile file)
        {
            using (var sha = SHA256.Create())
            using (var input = await file.OpenStreamForReadAsync())
            {
                var hash = sha.ComputeHash(input);
                return new JobArtifact
                {
                    File = file.Name,
                    SizeBytes = input.Length,
                    Sha256 = Hex(hash)
                };
            }
        }

        private static string Sha256(byte[] value)
        {
            using (var sha = SHA256.Create()) return Hex(sha.ComputeHash(value));
        }

        private static string Hex(IEnumerable<byte> bytes)
        {
            var result = new StringBuilder(64);
            foreach (var value in bytes) result.Append(value.ToString("x2"));
            return result.ToString();
        }
    }

    [DataContract]
    internal sealed class JobRecord
    {
        [DataMember(Name = "schema", Order = 1)] public string Schema { get; set; }
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; }
        [DataMember(Name = "job_id", Order = 3)] public string JobId { get; set; }
        [DataMember(Name = "created_utc", Order = 4)] public string CreatedUtc { get; set; }
        [DataMember(Name = "status", Order = 5)] public string Status { get; set; }
        [DataMember(Name = "metadata", Order = 6)] public JobMetadata Metadata { get; set; }
        [DataMember(Name = "artifacts", Order = 7)] public JobArtifacts Artifacts { get; set; }
    }

    [DataContract]
    internal sealed class JobMetadata
    {
        [DataMember(Name = "job_title", Order = 1)] public string JobTitle { get; set; }
        [DataMember(Name = "source_application", Order = 2)] public string SourceApplication { get; set; }
        [DataMember(Name = "session_id", Order = 3)] public string SessionId { get; set; }
        [DataMember(Name = "queue_name", Order = 4)] public string QueueName { get; set; }
        [DataMember(Name = "printer_uri", Order = 5)] public string PrinterUri { get; set; }
        [DataMember(Name = "configuration_key", Order = 6)] public string ConfigurationKey { get; set; }
        [DataMember(Name = "pdl_content_type", Order = 7)] public string PdlContentType { get; set; }
        [DataMember(Name = "source_curve_paths", Order = 8)] public List<string> SourceCurvePaths { get; set; }
        [DataMember(Name = "copies", Order = 9)] public int Copies { get; set; }
    }

    [DataContract]
    internal sealed class JobArtifacts
    {
        [DataMember(Name = "pdl", Order = 1)] public JobArtifact Pdl { get; set; }
        [DataMember(Name = "print_ticket", Order = 2)] public JobArtifact PrintTicket { get; set; }
        [DataMember(Name = "configuration", Order = 3)] public JobArtifact Configuration { get; set; }
        [DataMember(Name = "adapter_tiff", Order = 4)] public JobArtifact AdapterTiff { get; set; }
    }

    [DataContract]
    internal sealed class JobArtifact
    {
        [DataMember(Name = "file", Order = 1)] public string File { get; set; }
        [DataMember(Name = "size_bytes", Order = 2)] public long SizeBytes { get; set; }
        [DataMember(Name = "sha256", Order = 3)] public string Sha256 { get; set; }
        [DataMember(Name = "content_type", Order = 4, EmitDefaultValue = false)] public string ContentType { get; set; }
    }
}
