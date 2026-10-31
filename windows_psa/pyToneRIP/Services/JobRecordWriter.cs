using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using pyToneRIP.Shared;
using pyToneRIP.Models;
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
        public JsonObject PreviewOptions { get; private set; }
        private int cancellationRequested;
        private string cancellationReason = "Windows print job was canceled.";
        private string acceptedUtc;
        private JobRecord capturedRecord;
        private StorageFile capturedPdl;
        private StorageFile sourcePdl;
        private Func<Task<JobArtifact>> deferredRaster;
        private PrintWorkflowVirtualPrinterDataAvailableEventArgs capturedArgs;
        private readonly Dictionary<int, PreviewPhotoPlacement> photoPlacements = new Dictionary<int, PreviewPhotoPlacement>();
        private string lastEditId;

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
            PrintWorkflowVirtualPrinterDataAvailableEventArgs args,
            Func<Task> beginPreview = null)
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
            // Resolve the configuration and any stale borderless flag before
            // persisting/rendering the ticket. XpsToTiff and the RIP must see
            // the same supported paper selection.
            var configurationJson = await ReadAndValidateConfigurationAsync(ticket, session);
            if (PreviewOptions != null && beginPreview != null) await beginPreview();
            var ticketText = ticket.XmlNode.GetXml();
            var ticketArtifact = await WriteTextAndHashAsync(folder, "print-ticket.xml", ticketText);
            var precision = JsonObject.Parse(configurationJson)
                .GetNamedObject("adjustments").GetNamedNumber("processing_bits", 8);
            if (precision != 8 && precision != 16)
                throw new InvalidDataException("processing_bits must be 8 or 16.");
            JobArtifact adapterTiffArtifact = null;
            if (precision == 8)
            {
                if (PreviewOptions != null)
                    deferredRaster = () => RasterizeAsync(args, ticket, pdlFile);
                else
                    adapterTiffArtifact = await RasterizeAsync(args, ticket, pdlFile);
            }
            else
            {
                // Preserve XPS resources and precision. The existing full-trust
                // RIP process performs float32 rasterization before TIFF input.
                await JobProgressWriter.WriteAsync(
                    folder, jobId, 25, "WINDOWS_XPS_READY",
                    "Windows XPS is ready for high-precision rendering in the desktop RIP");
            }
            ThrowIfCancellationRequested();
            var sourceCurvePaths = ReadExternalCurvePaths(configurationJson);
            var configurationArtifact = await WriteTextAndHashAsync(
                folder, "configuration.json", configurationJson);
            await JobProgressWriter.WriteAsync(
                folder, jobId, 28, "CONFIGURATION_READY", "RIP configuration captured");

            pdlArtifact.ContentType = contentType;
            capturedArgs = args; capturedPdl = sourcePdl = pdlFile;
            if (PreviewOptions != null)
            {
                var previewPdf = await folder.CreateFileAsync("preview.pdf", CreationCollisionOption.FailIfExists);
                var converter = args.GetPdlConverter(PrintWorkflowPdlConversionType.XpsToPdf);
                using (var source = await pdlFile.OpenAsync(FileAccessMode.Read))
                using (var target = await previewPdf.OpenAsync(FileAccessMode.ReadWrite))
                {
                    var operations = PdlConversionHostBasedProcessingOperations.PageRotation
                        | PdlConversionHostBasedProcessingOperations.PageOrdering
                        | PdlConversionHostBasedProcessingOperations.BlankPageInsertion;
                    await converter.ConvertPdlAsync(ticket, source, target, operations);
                    await target.FlushAsync();
                }
            }
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
            capturedRecord = record;
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

        private async Task<JobArtifact> RasterizeAsync(
            PrintWorkflowVirtualPrinterDataAvailableEventArgs args,
            Windows.Graphics.Printing.PrintTicket.WorkflowPrintTicket ticket,
            StorageFile pdlFile)
        {
            ThrowIfCancellationRequested();
            var folder = JobFolder;
            var jobId = JobId;
                var adapterTiffFile = await folder.CreateFileAsync(
                    "windows-converter-output.tiff",
                    CreationCollisionOption.FailIfExists);
                var converter = args.GetPdlConverter(PrintWorkflowPdlConversionType.XpsToTiff);
                pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage(
                    "JobRecordWriter XpsToTiff conversion begin: job=" + jobId
                    + "; pdl=" + pdlFile.Name + "; ticket="
                    + (capturedRecord?.Artifacts?.PrintTicket?.File ?? "print-ticket.xml"));
                await JobProgressWriter.WriteAsync(
                    folder, jobId, 15, "WINDOWS_RENDER", "Windows is rasterizing the page");
                pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage("XpsToTiff OpenSource begin: " + jobId);
                using (var source = await pdlFile.OpenAsync(FileAccessMode.Read))
                {
                pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage("XpsToTiff OpenTarget begin: " + jobId);
                using (var target = await adapterTiffFile.OpenAsync(FileAccessMode.ReadWrite))
                {
                    // The RIP repeats the completed native job for each copy.
                    // Exclude host copy expansion so XpsToTiff supplies each page once.
                    // Python orders completed page streams according to DocumentCollate.
                    var hostProcessing =
                        PdlConversionHostBasedProcessingOperations.PageRotation |
                        PdlConversionHostBasedProcessingOperations.PageOrdering |
                        PdlConversionHostBasedProcessingOperations.BlankPageInsertion;
                    pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage("XpsToTiff ConvertPdlAsync begin: " + jobId);
                    await converter.ConvertPdlAsync(ticket, source, target, hostProcessing);
                    pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage("XpsToTiff ConvertPdlAsync complete; FlushAsync begin: " + jobId);
                    await target.FlushAsync();
                    pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage("XpsToTiff FlushAsync complete: " + jobId);
                }
                }
                ThrowIfCancellationRequested();
                var adapterTiffArtifact = await HashFileAsync(adapterTiffFile);
                pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage(
                    "JobRecordWriter XpsToTiff conversion complete: "
                    + adapterTiffArtifact.SizeBytes + " bytes");
                await JobProgressWriter.WriteAsync(
                    folder, jobId, 25, "WINDOWS_TIFF_READY", "Windows page raster is ready");
            return adapterTiffArtifact;
        }

        public async Task FinalizeCaptureAsync(StorageFile recordFile)
        {
            if (deferredRaster == null) return;
            recordFile = await JobFolder.GetFileAsync("job-record.json");
            ThrowIfCancellationRequested();
            var raster = await deferredRaster();
            ThrowIfCancellationRequested();
            capturedRecord.Artifacts.AdapterTiff = raster;
            var temporary = await JobFolder.CreateFileAsync("job-record-final.json", CreationCollisionOption.FailIfExists);
            using (var stream = await temporary.OpenStreamForWriteAsync())
                new DataContractJsonSerializer(typeof(JobRecord)).WriteObject(stream, capturedRecord);
            await temporary.MoveAndReplaceAsync(recordFile);
            deferredRaster = null;
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

        private async Task<string> ReadAndValidateConfigurationAsync(
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
            var preview = configuration.GetNamedObject("preview", null);
            var calibration = configuration.GetNamedObject("calibration", null);
            PreviewOptions = preview?.GetNamedBoolean("enabled", false) == true
                && calibration?.GetNamedBoolean("enabled", false) != true ? preview : null;
            var capabilityFile = await StorageFile.GetFileFromApplicationUriAsync(
                new Uri("ms-appx:///Assets/capabilities.generated.json"));
            var capabilityJson = await FileIO.ReadTextAsync(capabilityFile);
            var projected = PrintTicketSynchronization.OverlayConfiguration(
                ticket, json, capabilityJson,
                pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage);
            if (PreviewOptions != null)
                await WritePreviewDraftAsync(projected, PreviewOptions, ticket, "preview-configuration.json");
            return projected;
        }

        private async Task WritePreviewDraftAsync(string projected, JsonObject preview,
            Windows.Graphics.Printing.PrintTicket.WorkflowPrintTicket ticket, string name)
        {
            var draft = JsonObject.Parse(projected);
            draft["schema"] = JsonValue.CreateStringValue("pytone-rip.configuration");
            draft["preview"] = JsonObject.Parse(preview.Stringify());
            var resolution = PrintTicketSynchronization.Read(ticket);
            if (resolution.ResolutionX.HasValue) draft.GetNamedObject("printer")["raster_resolution_x"] = JsonValue.CreateNumberValue(resolution.ResolutionX.Value);
            if (resolution.ResolutionY.HasValue) draft.GetNamedObject("printer")["raster_resolution_y"] = JsonValue.CreateNumberValue(resolution.ResolutionY.Value);
            await PreviewWorkflowStore.WriteAtomicAsync(JobFolder, name, draft.Stringify());
        }

        public async Task ProcessPreviewEditAsync()
        {
            var requestPath = Path.Combine(JobFolder.Path, "preview-edit-request.json");
            if (!File.Exists(requestPath)) return;
            var request = JsonObject.Parse(File.ReadAllText(requestPath, Encoding.UTF8));
            var id = request.GetNamedString("id");
            if (!Guid.TryParse(id, out _) || id == lastEditId) return;
            lastEditId = id;
            var result = new JsonObject { ["id"] = JsonValue.CreateStringValue(id) };
            var commitStarted = false;
            var editStep = "ReadRequest";
            try
            {
                ThrowIfCancellationRequested();
                var draft = request.GetNamedObject("configuration");
                var oldDraft = JsonObject.Parse(await FileIO.ReadTextAsync(await JobFolder.GetFileAsync("preview-configuration.json")));
                var printer = draft.GetNamedObject("printer");
                if (printer.GetNamedString("model") != oldDraft.GetNamedObject("printer").GetNamedString("model")
                    || !SameDestination(draft.GetNamedObject("connection"), oldDraft.GetNamedObject("connection"))
                    || draft.GetNamedObject("calibration", null)?.GetNamedBoolean("enabled", false) == true)
                    throw new InvalidDataException("A captured job cannot change its physical printer or enable RAW calibration.");
                var capabilityFile = await StorageFile.GetFileFromApplicationUriAsync(new Uri("ms-appx:///Assets/capabilities.generated.json"));
                var capabilities = await FileIO.ReadTextAsync(capabilityFile);
                var model = JsonObject.Parse(capabilities).GetNamedArray("printers").Select(value => value.GetObject())
                    .Single(value => value.GetNamedString("id") == printer.GetNamedString("model"));
                var page = model.GetNamedArray("pages").Select(value => value.GetObject())
                    .Single(value => value.GetNamedString("id") == printer.GetNamedString("page") && value.GetNamedBoolean("enabled"));
                var oldPrinter = oldDraft.GetNamedObject("printer");
                var standardChanged = new[] { "page", "media", "input_source", "raster_resolution_x", "raster_resolution_y" }
                    .Any(key => printer.GetNamedValue(key).Stringify() != oldPrinter.GetNamedValue(key).Stringify())
                    || draft.GetNamedObject("layout").GetNamedString("orientation") != oldDraft.GetNamedObject("layout").GetNamedString("orientation");
                var geometryChanged = printer.GetNamedString("page") != oldPrinter.GetNamedString("page")
                    || draft.GetNamedObject("layout").GetNamedString("orientation") != oldDraft.GetNamedObject("layout").GetNamedString("orientation");
                pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage(
                    "Preview edit begin: job=" + JobId + "; edit=" + id + "; standardChanged=" + standardChanged + "; geometryChanged=" + geometryChanged);
                editStep = "OpenPrintTicket";
                var sourceTicket = await JobFolder.GetFileAsync(capturedRecord.Artifacts.PrintTicket.File);
                using (var input = await sourceTicket.OpenReadAsync())
                {
                    editStep = "ConstructWorkflowPrintTicket";
                    var ticket = new Windows.Graphics.Printing.PrintTicket.WorkflowPrintTicket(capturedRecord.Metadata.QueueName, input);
                    editStep = "ApplyPrintTicket";
                    if (standardChanged)
                    {
                        PrintTicketSynchronization.Apply(ticket, printer.GetNamedString("page"), page.GetNamedNumber("width_mm"), page.GetNamedNumber("height_mm"),
                            draft.GetNamedObject("layout").GetNamedString("orientation"), (int)printer.GetNamedNumber("raster_resolution_x"),
                            (int)printer.GetNamedNumber("raster_resolution_y"), printer.GetNamedString("media"), printer.GetNamedString("input_source"),
                            page.GetNamedBoolean("borderless"));
                        editStep = "ValidatePrintTicket";
                        var validation = await PrintTicketSynchronization.ValidateAsync(
                            ticket, pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage);
                        if (!validation.Validated) throw new InvalidDataException("PrintTicket validation failed: " + validation.ExtendedError);
                        if (!PrintTicketSynchronization.SelectionMatchesDefaults(PrintTicketSynchronization.Read(ticket),
                            printer.GetNamedString("page"), page.GetNamedNumber("width_mm"), page.GetNamedNumber("height_mm"),
                            draft.GetNamedObject("layout").GetNamedString("orientation"), (int)printer.GetNamedNumber("raster_resolution_x"),
                            (int)printer.GetNamedNumber("raster_resolution_y"), printer.GetNamedString("media"), printer.GetNamedString("input_source"), page.GetNamedBoolean("borderless")))
                            throw new InvalidDataException("Windows changed the requested print settings during validation.");
                    }
                    editStep = "PreparePlacement";
                    var pendingPlacements = photoPlacements.ToDictionary(item => item.Key, item => item.Value.Copy());
                    var requestedPlacements = new List<PreviewPhotoPlacement>();
                    var placementRequest = request.GetNamedArray("placements", null);
                    var previousLayout = JsonObject.Parse(draft.GetNamedObject("layout").Stringify());
                    if (placementRequest != null)
                    {
                        using (var bytes = new MemoryStream(Encoding.UTF8.GetBytes(placementRequest.Stringify())))
                            requestedPlacements = (List<PreviewPhotoPlacement>)new DataContractJsonSerializer(typeof(List<PreviewPhotoPlacement>)).ReadObject(bytes);
                        if (requestedPlacements.Count == 0 || requestedPlacements.Select(item => item.PageIndex).Distinct().Count() != requestedPlacements.Count)
                            throw new InvalidDataException("Invalid photo placement batch.");
                        draft.GetNamedObject("layout")["apply_rip_layout"] = JsonValue.CreateBooleanValue(false);
                    }
                    var projected = standardChanged ? PrintTicketSynchronization.OverlayConfiguration(ticket, draft.Stringify(), capabilities)
                        : RipConfigurationProjection.FromQueueJson(draft.Stringify()).Stringify();
                    var curves = ReadExternalCurvePaths(projected);
                    var precision = JsonObject.Parse(projected).GetNamedObject("adjustments").GetNamedNumber("processing_bits", 8);
                    if (precision != 8 && precision != 16) throw new InvalidDataException("processing_bits must be 8 or 16.");
                    var ticketArtifact = await WriteTextAndHashAsync(JobFolder, "ticket-" + id + ".xml", ticket.XmlNode.GetXml());
                    var configArtifact = await WriteTextAndHashAsync(JobFolder, "configuration-" + id + ".json", projected);
                    // Compose the captured vector page before both Windows
                    // converters. The printer backend receives its usual input.
                    var paperWidth = page.GetNamedNumber("width_mm"); var paperHeight = page.GetNamedNumber("height_mm");
                    if (draft.GetNamedObject("layout").GetNamedString("orientation") == "landscape")
                    { var savedWidth = paperWidth; paperWidth = paperHeight; paperHeight = savedWidth; }
                    if (placementRequest != null && previousLayout.GetNamedBoolean("apply_rip_layout", false))
                        await BakeAdditionalLayoutAsync(pendingPlacements, previousLayout, printer, page, paperWidth, paperHeight);
                    foreach (var placement in requestedPlacements) pendingPlacements[placement.PageIndex] = placement;
                    if (geometryChanged)
                    {
                        foreach (var placement in pendingPlacements.Values)
                        {
                            if (placement.Width > paperWidth || placement.Height > paperHeight)
                            {
                                var fit = Math.Min(paperWidth/placement.Width, paperHeight/placement.Height);
                                placement.Width *= fit; placement.Height *= fit;
                            }
                            placement.X = Math.Max(0, Math.Min(placement.X, paperWidth-placement.Width));
                            placement.Y = Math.Max(0, Math.Min(placement.Y, paperHeight-placement.Height));
                        }
                    }
                    var activePdl = capturedPdl;
                    var pdlArtifact = capturedRecord.Artifacts.Pdl;
                    if (geometryChanged || placementRequest != null)
                    {
                        editStep = "RewriteXps";
                        activePdl = await JobFolder.CreateFileAsync("layout-" + id + ".oxps", CreationCollisionOption.FailIfExists);
                        using (var source = await sourcePdl.OpenStreamForReadAsync())
                        using (var target = await activePdl.OpenStreamForWriteAsync())
                            PreviewPageTransform.Write(source, target, pendingPlacements, paperWidth, paperHeight);
                        pdlArtifact = await HashFileAsync(activePdl);
                        pdlArtifact.ContentType = capturedRecord.Artifacts.Pdl.ContentType;
                    }
                    editStep = "SavePreviewSettings";
                    // Render the immutable initial PDF with native transforms.
                    // Do not invoke the workflow PDF converter again for edits.
                    const string pdfName = "preview.pdf";
                    var placementsName = "placement-" + id + ".json";
                    using (var memory = new MemoryStream())
                    {
                        new DataContractJsonSerializer(typeof(List<PreviewPhotoPlacement>)).WriteObject(memory, pendingPlacements.Values.ToList());
                        await PreviewWorkflowStore.WriteAtomicAsync(JobFolder, placementsName, Encoding.UTF8.GetString(memory.ToArray()));
                    }
                    var settings = JsonObject.Parse(draft.GetNamedObject("preview").Stringify());
                    var iccPath = settings.GetNamedString("icc_path", "");
                    if (!string.IsNullOrWhiteSpace(iccPath))
                    {
                        var iccName = "printer-" + id + ".icc";
                        await (await ExternalIccAccessStore.OpenAsync(iccPath)).CopyAsync(JobFolder, iccName, NameCollisionOption.FailIfExists);
                        settings["icc_file"] = JsonValue.CreateStringValue(iccName);
                    }
                    var settingsName = "settings-" + id + ".json";
                    await PreviewWorkflowStore.WriteAtomicAsync(JobFolder, settingsName, settings.Stringify());
                    var draftName = "draft-" + id + ".json";
                    await WritePreviewDraftAsync(projected, settings, ticket, draftName);
                    ThrowIfCancellationRequested();
                    editStep = "CommitEdit";
                    commitStarted = true;
                    capturedRecord.Artifacts.Configuration = configArtifact;
                    capturedRecord.Artifacts.Pdl = pdlArtifact;
                    capturedRecord.Artifacts.PrintTicket = ticketArtifact;
                    capturedRecord.Artifacts.AdapterTiff = null;
                    capturedRecord.Metadata.SourceCurvePaths = curves;
                    capturedPdl = activePdl;
                    photoPlacements.Clear();
                    foreach (var item in pendingPlacements) photoPlacements[item.Key] = item.Value;
                    deferredRaster = precision == 8 ? (Func<Task<JobArtifact>>)(() => RasterizeAsync(capturedArgs, ticket, capturedPdl)) : null;

                    using (var memory = new MemoryStream())
                    {
                        new DataContractJsonSerializer(typeof(JobRecord)).WriteObject(memory, capturedRecord);
                        await PreviewWorkflowStore.WriteAtomicAsync(JobFolder, "job-record.json", Encoding.UTF8.GetString(memory.ToArray()));
                    }
                    var descriptor = new JsonObject
                    {
                        ["pdf"] = JsonValue.CreateStringValue(pdfName), ["configuration"] = JsonValue.CreateStringValue(configArtifact.File),
                        ["ticket"] = JsonValue.CreateStringValue(ticketArtifact.File), ["settings"] = JsonValue.CreateStringValue(settingsName),
                        ["draft"] = JsonValue.CreateStringValue(draftName),
                        ["placements"] = JsonValue.CreateStringValue(placementsName),
                    };
                    await PreviewWorkflowStore.WriteAtomicAsync(JobFolder, "preview-document.json", descriptor.Stringify());
                    // Keep the stable editor snapshot in sync for later edits.
                    await WritePreviewDraftAsync(projected, settings, ticket, "preview-configuration.json");
                    using (var memory = new MemoryStream())
                    {
                        new DataContractJsonSerializer(typeof(List<PreviewPhotoPlacement>)).WriteObject(memory, photoPlacements.Values.ToList());
                        await PreviewWorkflowStore.WriteAtomicAsync(JobFolder, "preview-placement.json", Encoding.UTF8.GetString(memory.ToArray()));
                    }
                    result["success"] = JsonValue.CreateBooleanValue(true);
                    pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage("Preview edit complete: job=" + JobId + "; edit=" + id);
                }
            }
            catch (Exception error)
            {
                result["success"] = JsonValue.CreateBooleanValue(false);
                result["fatal"] = JsonValue.CreateBooleanValue(commitStarted);
                result["error"] = JsonValue.CreateStringValue(error.Message);
                pyToneRIP.Tasks.VirtualPrinterBackgroundTask.WriteWorkflowStage("Preview edit failed: job=" + JobId + "; edit=" + id + "; step=" + editStep + "; HRESULT=0x" + error.HResult.ToString("X8") + "; " + error);
                if (commitStarted) await PreviewWorkflowStore.WriteAtomicAsync(JobFolder, "preview-failed.txt", error.ToString());
            }
            await PreviewWorkflowStore.WriteAtomicAsync(JobFolder, "preview-edit-result.json", result.Stringify());
        }

        private async Task BakeAdditionalLayoutAsync(Dictionary<int, PreviewPhotoPlacement> placements, JsonObject layout,
            JsonObject printer, JsonObject page, double paperWidth, double paperHeight)
        {
            // Removing the global extra-layout flag must preserve all the other
            // pages too, not just the photo the user is currently dragging.
            var points = page.GetNamedArray("preview_imageable_points").Select(value => value.GetNumber()).ToArray();
            foreach (var value in page.GetNamedArray("preview_regions", new JsonArray()))
            {
                var region = value.GetObject();
                if (region.GetNamedArray("modes").Any(item => item.GetString() == printer.GetNamedString("mode"))
                    && region.GetNamedArray("input_sources").Any(item => item.GetString() == printer.GetNamedString("input_source")))
                { points = region.GetNamedArray("points").Select(item => item.GetNumber()).ToArray(); break; }
            }
            var paperPoints = page.GetNamedArray("preview_paper_points");
            var ph = paperPoints[1].GetNumber();
            var left = points[0]*25.4/72; var top = (ph-points[3])*25.4/72;
            var areaWidth = (points[2]-points[0])*25.4/72; var areaHeight = (points[3]-points[1])*25.4/72;
            if (layout.GetNamedString("orientation") == "landscape")
            {
                var oldLeft = left; var oldWidth = areaWidth;
                left = ph*25.4/72-top-areaHeight; top = oldLeft; areaWidth = areaHeight; areaHeight = oldWidth;
            }
            var aspect = paperWidth/paperHeight;
            var requestedWidth = LayoutNumber(layout, "width_mm", 0); var requestedHeight = LayoutNumber(layout, "height_mm", 0);
            double width, height;
            if (requestedWidth > 0 || requestedHeight > 0)
            {
                width = requestedWidth > 0 ? (requestedHeight > 0 ? Math.Min(requestedWidth, requestedHeight*aspect) : requestedWidth)
                    : requestedHeight*aspect;
                height = width/aspect;
            }
            else if (layout.GetNamedString("sizing", "fit") != "fit")
            {
                var scale = LayoutNumber(layout, "scale_percent", 100)/100;
                width = paperWidth*scale; height = paperHeight*scale;
            }
            else
            {
                width = Math.Min(areaWidth, areaHeight*aspect); height = width/aspect;
            }
            var centered = layout.GetNamedString("placement", "center") == "center";
            var x = left+(centered ? (areaWidth-width)/2 : LayoutNumber(layout, "x_mm", 0));
            var y = top+(centered ? (areaHeight-height)/2 : LayoutNumber(layout, "y_mm", 0));
            List<double[]> sizes;
            using (var source = await sourcePdl.OpenStreamForReadAsync()) sizes = PreviewPageTransform.SourceSizes(source);
            for (var i = 0; i < sizes.Count; i++)
            {
                if (!placements.TryGetValue(i, out var placement))
                {
                    var fit = Math.Min(paperWidth/sizes[i][0], paperHeight/sizes[i][1]);
                    placement = new PreviewPhotoPlacement { PageIndex = i, Width = sizes[i][0]*fit, Height = sizes[i][1]*fit };
                    placement.X = (paperWidth-placement.Width)/2; placement.Y = (paperHeight-placement.Height)/2;
                    placements[i] = placement;
                }
                placement.ApplyPageBox(x, y, width, height, paperWidth, paperHeight);
            }
        }

        private static double LayoutNumber(JsonObject layout, string name, double fallback)
        {
            var value = layout.GetNamedValue(name, null);
            return value?.ValueType == JsonValueType.Number ? value.GetNumber() : fallback;
        }

        private static bool SameDestination(JsonObject first, JsonObject second)
        {
            string Value(JsonObject value, string name, string fallback = "")
            {
                var item = value.GetNamedValue(name, null);
                return item?.ValueType == JsonValueType.String ? item.GetString().Trim() : fallback;
            }
            bool Same(string name, string fallback = "") => string.Equals(Value(first, name, fallback), Value(second, name, fallback), StringComparison.OrdinalIgnoreCase);
            if (!Same("kind", "network")) return false;
            if (Value(first, "kind", "network") == "network")
                return Same("host") && LayoutNumber(first, "port", 9100) == LayoutNumber(second, "port", 9100);
            return Same("usb_device_id") && Same("usb_port") && Same("printer_queue") && Same("usb_transport_mode", "usbprint");
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
        [DataMember(Name = "adapter_tiff", Order = 4, EmitDefaultValue = false)] public JobArtifact AdapterTiff { get; set; }
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
