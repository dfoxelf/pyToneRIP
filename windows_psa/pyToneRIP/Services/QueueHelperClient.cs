using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Threading.Tasks;
using pyToneRIP.Models;
using pyToneRIP.Shared;
using Windows.ApplicationModel;
using Windows.Storage;

namespace pyToneRIP.Services
{
    internal static class QueueHelperClient
    {
        private const string RequestFolderName = "queue-helper";
        private const string ParameterGroupId = "QueueCommand";
        private const int SetUserDefaultsWaitAttempts = 600; // 60 seconds

        public static async Task<IReadOnlyList<WindowsQueueDescriptor>> ListAsync()
        {
            var response = await ExecuteRawAsync(
                new QueueHelperRequest { Operation = "list" }, maxAttempts: 150);
            // A missing payload is not evidence that all queues were deleted.
            return response.Queues
                ?? throw new InvalidDataException("Queue helper returned no Windows printer list.");
        }

        public static async Task<IReadOnlyList<UsbPrinterDescriptor>> DiscoverUsbPrintersAsync()
        {
            var response = await ExecuteRawAsync(
                new QueueHelperRequest { Operation = "discover-usb" }, maxAttempts: 300);
            return response.UsbPrinters ?? new List<UsbPrinterDescriptor>();
        }

        public static async Task<IReadOnlyList<UsbInkLevel>> ReadUsbInkAsync(string deviceInstanceId)
        {
            if (string.IsNullOrWhiteSpace(deviceInstanceId))
                throw new ArgumentException("A USB printer device is required.", nameof(deviceInstanceId));
            var response = await ExecuteRawAsync(new QueueHelperRequest
            {
                Operation = "read-usb-ink",
                UsbDeviceId = deviceInstanceId,
            }, maxAttempts: 300);
            return response.UsbInkLevels ?? new List<UsbInkLevel>();
        }

        public static Task<QueueHelperResponse> ReadQueueInkAsync(WindowsQueueDescriptor queue) =>
            ExecuteRawAsync(new QueueHelperRequest
            {
                Operation = "read-queue-ink", CurrentName = queue.Name, DriverName = queue.DriverName,
            }, maxAttempts: 200);

        public static async Task<PrinterInformationReport> ReadUsbInformationAsync(string deviceInstanceId)
        {
            if (string.IsNullOrWhiteSpace(deviceInstanceId))
                throw new ArgumentException("A USB printer device is required.", nameof(deviceInstanceId));
            var response = await ExecuteRawAsync(new QueueHelperRequest
            {
                Operation = "read-usb-information", UsbDeviceId = deviceInstanceId,
            }, maxAttempts: 1200);
            return response.PrinterInformation
                ?? throw new InvalidDataException("The printer information report is missing.");
        }

        public static async Task<string> CollectSupportReportAsync(string managerLog)
        {
            const int maxManagerLogCharacters = 1_000_000;
            if (managerLog != null && managerLog.Length > maxManagerLogCharacters)
                managerLog = managerLog.Substring(managerLog.Length - maxManagerLogCharacters);
            var response = await ExecuteRawAsync(new QueueHelperRequest
            {
                Operation = "collect-support-report",
                ManagerLog = managerLog,
            }, maxAttempts: 1800);
            if (string.IsNullOrWhiteSpace(response.ReportPath))
                throw new InvalidDataException("Support report path was not returned.");
            return response.ReportPath;
        }

        public static Task<IReadOnlyList<WindowsQueueDescriptor>> RepairStaleAsync() =>
            ExecuteAsync(new QueueHelperRequest { Operation = "repair-stale" });

        public static async Task OpenJobsAsync(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName))
                throw new ArgumentException("A printer queue name is required.", nameof(queueName));
            await ExecuteAsync(new QueueHelperRequest
            {
                Operation = "open-jobs",
                CurrentName = queueName,
            });
        }

        public static async Task<bool> CancelCurrentJobAsync(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName))
                throw new ArgumentException("A printer queue name is required.", nameof(queueName));
            var result = await ExecuteAsync(new QueueHelperRequest
            {
                Operation = "cancel-current-job",
                CurrentName = queueName,
            });
            return result.Count == 1 && result[0].Jobs != 0;
        }

        public static async Task RequestPhysicalCancellationAsync(string jobId)
        {
            await ExecuteAsync(new QueueHelperRequest { Operation = "cancel-delivered-job", JobId = jobId });
        }

        public static async Task<bool> HasCancelableJobAsync(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName)) return false;
            var result = await ExecuteAsync(new QueueHelperRequest { Operation = "cancelable-job", CurrentName = queueName });
            return result.Count == 1 && result[0].Jobs != 0;
        }

        public static async Task<bool> HasCurrentJobAsync(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName))
                return false;
            var result = await ExecuteAsync(new QueueHelperRequest
            {
                Operation = "current-job",
                CurrentName = queueName,
            });
            return result.Count == 1 && result[0].Jobs != 0;
        }

        public static Task<IReadOnlyList<WindowsQueueDescriptor>> SetDefaultAsync(
            string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName))
                throw new ArgumentException("A printer queue name is required.", nameof(queueName));
            return ExecuteAsync(new QueueHelperRequest
            {
                Operation = "set-default",
                CurrentName = queueName,
            });
        }

        public static async Task ValidateRuntimeAsync() =>
            await ExecuteAsync(
                new QueueHelperRequest { Operation = "runtime-self-test" },
                maxAttempts: 1200);

        public static async Task<string> RunMaintenanceAsync(MaintenanceRuntimeRequest maintenance)
        {
            if (maintenance == null)
                throw new ArgumentNullException(nameof(maintenance));
            var settings = new DataContractJsonSerializerSettings
            {
                UseSimpleDictionaryFormat = true,
            };
            string json;
            using (var stream = new MemoryStream())
            {
                new DataContractJsonSerializer(
                    typeof(MaintenanceRuntimeRequest), settings).WriteObject(stream, maintenance);
                json = System.Text.Encoding.UTF8.GetString(stream.ToArray());
            }
            var result = await ExecuteRawAsync(
                new QueueHelperRequest
                {
                    Operation = "maintenance",
                    MaintenanceJson = json,
                },
                // The helper's hard limit is ten minutes. Keep the UI-side
                // response window slightly longer so a valid long cleaning
                // cycle is not reported as a timeout while the helper is
                // still waiting for the runtime. / Окно ожидания GUI должно
                // быть длиннее предела helper, иначе длительная очистка даст
                // ложный timeout в интерфейсе.
                maxAttempts: 7200);
            return result.MaintenanceTransport ?? maintenance.ConnectionKind;
        }

        public static async Task InstallModelPackageAsync(
            string packagePath,
            IEnumerable<string> dependencyPaths,
            string packageIdentity,
            string packageVersion)
        {
            if (string.IsNullOrWhiteSpace(packagePath))
                throw new ArgumentException("A model package path is required.", nameof(packagePath));
            await ExecuteAsync(new QueueHelperRequest
            {
                Operation = "install-model-package",
                ModulePackagePath = packagePath,
                ModuleDependencyPaths = dependencyPaths?.ToList() ?? new List<string>(),
                ModulePackageIdentity = packageIdentity,
                ModulePackageVersion = packageVersion,
            }, maxAttempts: 1200);
        }

        public static async Task RemoveModelPackageAsync(string packageIdentity)
        {
            if (string.IsNullOrWhiteSpace(packageIdentity))
                throw new ArgumentException("A model package identity is required.", nameof(packageIdentity));
            await ExecuteAsync(new QueueHelperRequest
            {
                Operation = "remove-model-package",
                ModulePackageIdentity = packageIdentity,
            }, maxAttempts: 1200);
        }

        public static async Task<WindowsQueueDescriptor> RenameAsync(
            string currentName,
            string portName,
            string desiredName)
        {
            var queues = await ExecuteAsync(new QueueHelperRequest
            {
                Operation = "rename",
                CurrentName = currentName,
                PortName = portName,
                DesiredName = desiredName,
            });
            if (queues.Count != 1)
                throw new InvalidDataException("Queue helper returned an invalid rename response.");
            return queues[0];
        }

        public static async Task SetUserDefaultsAsync(
            string queueName,
            string pageId,
            double pageWidthMm,
            double pageHeightMm,
            string orientation,
            int resolutionX,
            int resolutionY,
            bool? borderless = null)
        {
            try
            {
                // Spooler validation and DEVMODE writes can take longer on a new queue.
                await ExecuteAsync(new QueueHelperRequest
                {
                    Operation = "set-user-defaults",
                    CurrentName = queueName,
                    PageId = pageId,
                    PageWidthMm = pageWidthMm,
                    PageHeightMm = pageHeightMm,
                    Orientation = orientation,
                    ResolutionX = resolutionX,
                    ResolutionY = resolutionY,
                    Borderless = borderless,
                }, maxAttempts: SetUserDefaultsWaitAttempts);
            }
            catch (TimeoutException timeout)
            {
                // A write can complete after the reply deadline. Check both stored
                // DEVMODEs and the settings visible to classic apps before reporting failure.
                try
                {
                    await VerifyUserDefaultsAsync(queueName, pageId, pageWidthMm,
                        pageHeightMm, orientation, resolutionX, resolutionY, borderless);
                }
                catch (Exception verificationError)
                {
                    throw new TimeoutException(
                        "Windows printer defaults could not be confirmed after the write timed out.",
                        new AggregateException(timeout, verificationError));
                }
                ManagerLogger.Write("Windows printer defaults verified after write timeout: queue=" + queueName);
            }
        }

        public static async Task VerifyUserDefaultsAsync(
            string queueName, string pageId, double pageWidthMm, double pageHeightMm,
            string orientation, int resolutionX, int resolutionY, bool? borderless = null) =>
            await ExecuteAsync(new QueueHelperRequest
            {
                Operation = "verify-user-defaults",
                CurrentName = queueName, PageId = pageId,
                PageWidthMm = pageWidthMm, PageHeightMm = pageHeightMm,
                Orientation = orientation, ResolutionX = resolutionX, ResolutionY = resolutionY,
                Borderless = borderless,
            });

        public static async Task<LegacyPrinterDefaults> ReadLegacyUserDefaultsAsync(
            string queueName)
        {
            var response = await ExecuteRawAsync(new QueueHelperRequest
            {
                Operation = "read-user-defaults",
                CurrentName = queueName,
            }, maxAttempts: 150);
            return response.LegacyDefaults
                ?? throw new InvalidDataException("Queue helper returned no per-user DEVMODE selection.");
        }

        public static async Task NotifyUserDefaultsChangedAsync(string queueName) =>
            await ExecuteAsync(new QueueHelperRequest
            {
                Operation = "notify-user-defaults", CurrentName = queueName,
            });

        public static async Task<string> ReadConfigurationTicketXmlAsync(string queueName)
        {
            var response = await ExecuteRawAsync(new QueueHelperRequest
            {
                Operation = "read-configuration-ticket", CurrentName = queueName,
            }, maxAttempts: 300);
            if (string.IsNullOrWhiteSpace(response.ConfigurationTicketXml))
                throw new InvalidDataException("Queue helper returned no configuration PrintTicket XML.");
            return response.ConfigurationTicketXml;
        }

        private static async Task<IReadOnlyList<WindowsQueueDescriptor>> ExecuteAsync(
            QueueHelperRequest request,
            int maxAttempts = 150)
        {
            var response = await ExecuteRawAsync(request, maxAttempts);
            return response.Queues ?? new List<WindowsQueueDescriptor>();
        }

        private static async Task<QueueHelperResponse> ExecuteRawAsync(
            QueueHelperRequest request,
            int maxAttempts)
        {
            if (QueueAppServiceProtocol.UsesAppService(request.Operation))
            {
                var response = await QueueAppServiceClient.SendAsync(request, TimeSpan.FromMilliseconds(maxAttempts * 100));
                if (response != null)
                    return CheckResponse(response);
                ManagerLogger.Write("Queue AppService using file exchange for: " + request.Operation);
            }
            return await ExecuteFileAsync(request, maxAttempts);
        }

        private static async Task<QueueHelperResponse> ExecuteFileAsync(
            QueueHelperRequest request,
            int maxAttempts)
        {
            var folder = await ApplicationData.Current.LocalFolder.CreateFolderAsync(
                RequestFolderName,
                CreationCollisionOption.OpenIfExists);
            var token = Guid.NewGuid().ToString("N");
            var requestName = token + ".request.json";
            var responseName = token + ".response.json";
            var stagedName = token + ".request.tmp";
            var stagedFile = await folder.CreateFileAsync(
                stagedName,
                CreationCollisionOption.FailIfExists);
            try
            {
                using (var stream = await stagedFile.OpenStreamForWriteAsync())
                    new DataContractJsonSerializer(typeof(QueueHelperRequest))
                        .WriteObject(stream, request);
                // Other helper launches can scan the inbox before this launch.
                // Publish only after the writer has closed the complete request.
                await stagedFile.RenameAsync(requestName, NameCollisionOption.FailIfExists);

                await FullTrustProcessLauncher.LaunchFullTrustProcessForCurrentAppAsync(
                    ParameterGroupId);
                var responseFile = await WaitForResponseAsync(folder, responseName, maxAttempts);
                try
                {
                    QueueHelperResponse response;
                    using (var stream = await responseFile.OpenStreamForReadAsync())
                        response = (QueueHelperResponse)new DataContractJsonSerializer(
                            typeof(QueueHelperResponse)).ReadObject(stream);
                    return CheckResponse(response);
                }
                finally
                {
                    DeleteExchangeFile(responseFile.Path);
                }
            }
            finally
            {
                DeleteExchangeFile(Path.Combine(folder.Path, stagedName));
                DeleteExchangeFile(Path.Combine(folder.Path, requestName));
            }
        }

        private static QueueHelperResponse CheckResponse(QueueHelperResponse response)
        {
            if (response == null)
                throw new InvalidDataException("Queue helper returned an empty response.");
            if (!response.Success && response.ErrorMessage == EpsonRawQueueMatcher.RawDriverRequiredMessage)
                throw new InvalidOperationException(LocalizationService.Get("Queue.RawDriverRequired"));
            if (!response.Success)
                throw new InvalidOperationException(
                    "Windows queue operation failed (" + response.ErrorCode + "): " +
                    (response.ErrorMessage ?? "Unknown error."));
            return response;
        }

        private static void DeleteExchangeFile(string path)
        {
            try { File.Delete(path); }
            catch (Exception error) when (error is IOException || error is UnauthorizedAccessException)
            {
                ManagerLogger.WriteException("Cleaning up queue helper exchange file", error);
            }
        }

        private static async Task<StorageFile> WaitForResponseAsync(
            StorageFolder folder,
            string responseName,
            int maxAttempts)
        {
            for (var attempt = 0; attempt < maxAttempts; attempt++)
            {
                try
                {
                    return await folder.GetFileAsync(responseName);
                }
                catch (FileNotFoundException)
                {
                    await Task.Delay(100);
                }
            }
            throw new TimeoutException(
                "The Windows queue helper did not respond within "
                + (maxAttempts / 10) + " seconds.");
        }
    }

    [DataContract]
    internal sealed class QueueHelperRequest
    {
        [DataMember(Name = "operation", Order = 1)] public string Operation { get; set; }
        [DataMember(Name = "current_name", Order = 2, EmitDefaultValue = false)] public string CurrentName { get; set; }
        [DataMember(Name = "port_name", Order = 3, EmitDefaultValue = false)] public string PortName { get; set; }
        [DataMember(Name = "desired_name", Order = 4, EmitDefaultValue = false)] public string DesiredName { get; set; }
        [DataMember(Name = "page_id", Order = 5, EmitDefaultValue = false)] public string PageId { get; set; }
        [DataMember(Name = "page_width_mm", Order = 6)] public double PageWidthMm { get; set; }
        [DataMember(Name = "borderless", Order = 99)] public bool? Borderless { get; set; }
        [DataMember(Name = "page_height_mm", Order = 7)] public double PageHeightMm { get; set; }
        [DataMember(Name = "orientation", Order = 8, EmitDefaultValue = false)] public string Orientation { get; set; }
        [DataMember(Name = "resolution_x", Order = 9)] public int ResolutionX { get; set; }
        [DataMember(Name = "resolution_y", Order = 10)] public int ResolutionY { get; set; }
        [DataMember(Name = "module_package_path", Order = 11, EmitDefaultValue = false)] public string ModulePackagePath { get; set; }
        [DataMember(Name = "module_dependency_paths", Order = 12, EmitDefaultValue = false)] public List<string> ModuleDependencyPaths { get; set; }
        [DataMember(Name = "module_package_identity", Order = 13, EmitDefaultValue = false)] public string ModulePackageIdentity { get; set; }
        [DataMember(Name = "module_package_version", Order = 14, EmitDefaultValue = false)] public string ModulePackageVersion { get; set; }
        [DataMember(Name = "maintenance_json", Order = 15, EmitDefaultValue = false)] public string MaintenanceJson { get; set; }
        [DataMember(Name = "manager_log", Order = 16, EmitDefaultValue = false)] public string ManagerLog { get; set; }
        [DataMember(Name = "usb_device_id", Order = 17, EmitDefaultValue = false)] public string UsbDeviceId { get; set; }
        [DataMember(Name = "job_id", Order = 18)] public string JobId { get; set; }
        [DataMember(Name = "driver_name", Order = 19, EmitDefaultValue = false)] public string DriverName { get; set; }
    }

    [DataContract]
    internal sealed class QueueHelperResponse
    {
        [DataMember(Name = "success", Order = 1)] public bool Success { get; set; }
        [DataMember(Name = "error_code", Order = 2)] public int ErrorCode { get; set; }
        [DataMember(Name = "error_message", Order = 3, EmitDefaultValue = false)] public string ErrorMessage { get; set; }
        [DataMember(Name = "queues", Order = 4)] public List<WindowsQueueDescriptor> Queues { get; set; }
        [DataMember(Name = "report_path", Order = 5, EmitDefaultValue = false)] public string ReportPath { get; set; }
        [DataMember(Name = "usb_printers", Order = 6, EmitDefaultValue = false)] public List<UsbPrinterDescriptor> UsbPrinters { get; set; }
        [DataMember(Name = "legacy_defaults", Order = 7, EmitDefaultValue = false)] public LegacyPrinterDefaults LegacyDefaults { get; set; }
        [DataMember(Name = "usb_ink_levels", Order = 8, EmitDefaultValue = false)] public List<UsbInkLevel> UsbInkLevels { get; set; }
        [DataMember(Name = "configuration_ticket_xml", Order = 9, EmitDefaultValue = false)] public string ConfigurationTicketXml { get; set; }
        [DataMember(Name = "printer_information", Order = 10, EmitDefaultValue = false)] public PrinterInformationReport PrinterInformation { get; set; }
        [DataMember(Name = "maintenance_transport", Order = 11, EmitDefaultValue = false)] public string MaintenanceTransport { get; set; }
    }

    [DataContract]
    internal sealed class WindowsQueueDescriptor
    {
        [DataMember(Name = "name", Order = 1)] public string Name { get; set; }
        [DataMember(Name = "port_name", Order = 2)] public string PortName { get; set; }
        [DataMember(Name = "driver_name", Order = 3)] public string DriverName { get; set; }
        [DataMember(Name = "status", Order = 4)] public uint Status { get; set; }
        [DataMember(Name = "jobs", Order = 5)] public uint Jobs { get; set; }
        [DataMember(Name = "is_default", Order = 6)] public bool IsDefault { get; set; }
        [DataMember(Name = "raw_ready", Order = 7)] public bool RawReady { get; set; }

        public bool IsEpsonRawDestination => EpsonRawQueueMatcher.IsCompatible(Name, PortName, DriverName, RawReady, Status);
        public bool RequiresRawDriver => EpsonRawQueueMatcher.IsEpsonClassDriver(DriverName);
        public bool IsOffline => EpsonRawQueueMatcher.IsOffline(Status);
        public bool IsPrinting => EpsonRawQueueMatcher.IsPrinting(Status);
        public bool MatchesDestination(string queue, string driver) => IsEpsonRawDestination
            && string.Equals(Name, queue, StringComparison.OrdinalIgnoreCase)
            && string.Equals(DriverName, driver, StringComparison.OrdinalIgnoreCase);
        public bool MatchesModel(PrinterCapability model, bool allowHqFallback = true)
        {
            if (model == null) return false;
            var reportedModel = EpsonRawQueueMatcher.IsEpsonClassDriver(DriverName) ? Name : DriverName;
            return UsbPrinterModelMatcher.MatchesReportedModel(model.Id, model.DisplayName, reportedModel)
                || (allowHqFallback && model.Id.EndsWith("_HQ", StringComparison.Ordinal)
                    && UsbPrinterModelMatcher.MatchesReportedModel(model.Id.Substring(0, model.Id.Length - 3), null, reportedModel));
        }
        public override string ToString() => Name;
    }
}
