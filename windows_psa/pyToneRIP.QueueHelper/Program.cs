using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading;
using System.Xml;
using Microsoft.Win32;
using pyToneRIP.Models;
using pyToneRIP.Services;

namespace pyToneRIP.QueueHelper
{
    internal static class Program
    {
        private const string RequestFolderName = "queue-helper";
        private const string RequestSuffix = ".request.json";
        private const int ErrorInsufficientBuffer = 122;
        private const int AppModelErrorNoPackage = 15700;
        private const string QueueRequestMutexName = "Local\\pyToneRIP.QueueHelper.Requests";

        private static int Main(string[] args)
        {
            string localState = null;
            try
            {
                localState = GetPackageLocalStatePath();
                if (args.Contains(QueueAppServiceProtocol.LaunchArgument, StringComparer.OrdinalIgnoreCase))
                {
                    new QueueAppServiceHost(localState).RunAsync().GetAwaiter().GetResult();
                    return 0;
                }
                if (args.Any(value => string.Equals(
                    value, "--process-rip-requests", StringComparison.OrdinalIgnoreCase)))
                {
                    RipRequestProcessor.ProcessPending(localState);
                    return 0;
                }
                using (var requestMutex = new Mutex(false, QueueRequestMutexName))
                {
                    var ownsMutex = false;
                    try
                    {
                        try
                        {
                            ownsMutex = requestMutex.WaitOne(TimeSpan.FromSeconds(30));
                        }
                        catch (AbandonedMutexException)
                        {
                            ownsMutex = true;
                        }
                        if (!ownsMutex)
                            throw new TimeoutException(
                                "Another queue helper instance did not finish in time.");

                        var requestFolder = Path.Combine(localState, RequestFolderName);
                        Directory.CreateDirectory(requestFolder);
                        var requests = Directory.GetFiles(requestFolder, "*" + RequestSuffix)
                            .OrderBy(path => path, StringComparer.OrdinalIgnoreCase)
                            .ToArray();
                        foreach (var requestPath in requests)
                            ProcessRequest(requestPath, localState);
                    }
                    finally
                    {
                        if (ownsMutex)
                            requestMutex.ReleaseMutex();
                    }
                }
                return 0;
            }
            catch (Exception error)
            {
                TryLog(localState, "Queue helper failed before processing requests: " + error);
                return 1;
            }
        }

        private static void ProcessRequest(string requestPath, string localState)
        {
            var claimedPath = requestPath + ".processing";
            try
            {
                // Claim the file before reading so client cleanup can withdraw
                // a pending request without touching a request already in use.
                File.Move(requestPath, claimedPath);
            }
            catch (FileNotFoundException)
            {
                return;
            }
            catch (IOException error)
            {
                TryLog(localState, "Could not claim queue request: " + error);
                return;
            }
            try
            {
                var response = ExecuteRequest(claimedPath, localState);
                var responsePath = requestPath.Substring(0, requestPath.Length - RequestSuffix.Length) + ".response.json";
                WriteJsonAtomically(responsePath, response);
            }
            finally
            {
                try { File.Delete(claimedPath); } catch (IOException) { } catch (UnauthorizedAccessException) { }
            }
        }

        private static QueueHelperResponse ExecuteRequest(string requestPath, string localState)
        {
            try
            {
                return ExecuteAppServiceRequest(ReadJson<QueueHelperRequest>(requestPath), localState);
            }
            catch (Exception error)
            {
                return RequestFailure(localState, Path.GetFileName(requestPath), error);
            }
        }

        internal static QueueHelperResponse ExecuteAppServiceRequest(QueueHelperRequest request, string localState)
        {
            // Keep queue edits mutually exclusive between Manager windows.
            // Live status, Open Jobs and cancellation bypass these locks.
            string group;
            switch (request?.Operation)
            {
                case "repair-stale":
                case "rename":
                case "set-default":
                case "set-user-defaults":
                case "notify-user-defaults":
                    group = "Settings";
                    break;
                case "read-usb-ink":
                case "read-usb-information":
                case "read-queue-ink":
                    group = "UsbStatus";
                    break;
                default:
                    return DispatchRequest(request, localState);
            }
            using (var mutex = new Mutex(false, QueueRequestMutexName + "." + group))
            {
                var acquired = false;
                try
                {
                    try { acquired = mutex.WaitOne(TimeSpan.FromSeconds(15)); }
                    catch (AbandonedMutexException) { acquired = true; }
                    if (!acquired)
                        throw new TimeoutException("Another queue operation did not release " + group + " in time.");
                    return DispatchRequest(request, localState);
                }
                catch (Exception error)
                {
                    return RequestFailure(localState, request.Operation, error);
                }
                finally
                {
                    if (acquired) mutex.ReleaseMutex();
                }
            }
        }

        private static List<UsbInkLevel> ReadQueueInk(QueueDescriptor queue, string localState)
        {
            try
            {
                var levels = WindowsQueueInkReader.Read(queue.Name);
                if (levels.Any(ink => ink.Percent >= 0)) return levels;
            }
            catch (Exception error) when (error is COMException || error is System.Xml.XmlException)
            {
                TryLog(localState, "Reading Windows queue ink via Bidi: " + error.Message);
            }

            if (!IsLocalUsbQueue(queue)) return null;
            try
            {
                using (var queueMutex = new Mutex(false, RipRequestProcessor.PhysicalPrinterMutexName(
                    "windows_queue|" + queue.Name.Trim().ToUpperInvariant())))
                {
                    if (!TryAcquire(queueMutex)) return null;
                    try
                    {
                        return WithIdleQueueUsb(queue, deviceId => UsbPrinterStatusReader.ReadInk(deviceId, exclusive: true));
                    }
                    finally { queueMutex.ReleaseMutex(); }
                }
            }
            catch (Exception error) when (error is Win32Exception || error is InvalidOperationException || error is TimeoutException)
            {
                TryLog(localState, "Reading Windows queue ink via USB: " + error.Message);
                return null;
            }
        }

        private static bool IsLocalUsbQueue(QueueDescriptor queue) => EpsonRawQueueMatcher.IsUsbPort(queue.PortName)
            && !queue.Name.StartsWith(@"\\", StringComparison.Ordinal);

        private static T WithIdleQueueUsb<T>(QueueDescriptor queue, Func<string, T> operation)
        {
            var devices = UsbPrinterDiscovery.Discover().Where(device =>
                string.Equals(device.PortName, queue.PortName, StringComparison.OrdinalIgnoreCase)
                && device.QueueNames.Contains(queue.Name, StringComparer.OrdinalIgnoreCase)
                && UsbPrinterModelMatcher.Matches(queue.DriverName, queue.Name, device)).ToList();
            if (devices.Count != 1)
                throw new InvalidOperationException("Could not identify the connected Epson device on " + queue.PortName + ".");

            var deviceId = devices[0].DeviceInstanceId;
            using (var usbMutex = new Mutex(false, RipRequestProcessor.PhysicalPrinterMutexName(
                "usb|" + deviceId.ToUpperInvariant())))
            {
                if (!TryAcquire(usbMutex)) throw new InvalidOperationException("The USB printer is busy.");
                try
                {
                    if (PrinterApi.ListLocalPrinters().Any(other =>
                        other.PortName.Split(',').Any(port =>
                            string.Equals(port.Trim(), queue.PortName, StringComparison.OrdinalIgnoreCase))
                        && (EpsonRawQueueMatcher.IsPrinting(other.Status)
                            || (other.Jobs > 0 && PrintQueueCancellationMonitor.HasCurrentJob(other.Name)))))
                        throw new InvalidOperationException("A Windows print job is using " + queue.PortName + ".");
                    return operation(deviceId);
                }
                finally { usbMutex.ReleaseMutex(); }
            }
        }

        internal static string RunMaintenanceTransport(MaintenanceBrokerRequest request, string json, Action<string> send)
        {
            var kind = request.ConnectionKind ?? "network";
            if (kind == "windows_queue")
            {
                var queue = FindWindowsQueue(request.PhysicalQueueName);
                if (EpsonRawQueueMatcher.IsEpsonClassDriver(queue?.DriverName))
                {
                    if (request.Action != "nozzle_check" || !IsLocalUsbQueue(queue))
                        throw new NotSupportedException(EpsonRawQueueMatcher.RawDriverRequiredMessage);
                    if (!queue.IsEpsonRawDestination || EpsonRawQueueMatcher.IsOffline(queue.Status)
                        || !string.Equals(queue.DriverName, request.PhysicalQueueDriver, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidOperationException("The selected Windows printer queue is unavailable.");
                    return WithIdleQueueUsb(queue, deviceId =>
                    {
                        var usbRequest = new MaintenanceBrokerRequest
                        {
                            Schema = request.Schema, Version = request.Version, Action = request.Action,
                            Model = request.Model, ConnectionKind = "usb", UsbDeviceId = deviceId, ExclusiveUsb = true,
                        };
                        send(QueueAppServiceProtocol.Serialize(usbRequest));
                        return usbRequest.ConnectionKind;
                    });
                }
            }
            send(json);
            return kind;
        }

        private static QueueDescriptor FindWindowsQueue(string name) => PrinterApi.ListPrinters()
            .FirstOrDefault(queue => string.Equals(queue.Name, name, StringComparison.OrdinalIgnoreCase));

        internal static void RequireRawQueueDriver(string name)
        {
            if (EpsonRawQueueMatcher.IsEpsonClassDriver(FindWindowsQueue(name)?.DriverName))
                throw new NotSupportedException(EpsonRawQueueMatcher.RawDriverRequiredMessage);
        }

        private static bool TryAcquire(Mutex mutex)
        {
            try { return mutex.WaitOne(0); }
            catch (AbandonedMutexException) { return true; }
        }

        private static QueueHelperResponse DispatchRequest(QueueHelperRequest request, string localState)
        {
            QueueHelperResponse response;
            try
            {
                if (request == null || string.IsNullOrWhiteSpace(request.Operation))
                    throw new InvalidDataException("Queue helper request has no operation.");

                if (request.Operation == "read-user-defaults" || request.Operation == "read-configuration-ticket"
                    || request.Operation == "set-user-defaults" || request.Operation == "verify-user-defaults")
                    TryLog(localState, Diagnostics.PrintSettingsContext.Describe("QueueHelper", request.CurrentName));

                if (string.Equals(request.Operation, "list", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(PrinterApi.ListPrinters());
                }
                else if (string.Equals(request.Operation, "discover-usb", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                    response.UsbPrinters = UsbPrinterDiscovery.Discover();
                }
                else if (string.Equals(request.Operation, "read-usb-ink", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                    response.UsbInkLevels = UsbPrinterStatusReader.ReadInk(request.UsbDeviceId);
                }
                else if (string.Equals(request.Operation, "read-queue-ink", StringComparison.OrdinalIgnoreCase))
                {
                    var queue = PrinterApi.ListPrinters().FirstOrDefault(item => item.IsEpsonRawDestination
                        && string.Equals(item.Name, request.CurrentName, StringComparison.OrdinalIgnoreCase)
                        && string.Equals(item.DriverName, request.DriverName, StringComparison.OrdinalIgnoreCase));
                    response = QueueHelperResponse.Pass(queue == null ? new List<QueueDescriptor>()
                        : new List<QueueDescriptor> { queue });
                    if (queue != null && !EpsonRawQueueMatcher.IsOffline(queue.Status))
                        response.UsbInkLevels = ReadQueueInk(queue, localState);
                }
                else if (string.Equals(request.Operation, "read-usb-information", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                    response.PrinterInformation = UsbPrinterStatusReader.ReadInformation(request.UsbDeviceId);
                }
                else if (string.Equals(request.Operation, "repair-stale", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(PrinterApi.RepairStaleDuplicates());
                }
                else if (string.Equals(request.Operation, "rename", StringComparison.OrdinalIgnoreCase))
                {
                    var queue = PrinterApi.RenamePrinter(
                        request.CurrentName,
                        request.PortName,
                        request.DesiredName);
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor> { queue });
                }
                else if (string.Equals(request.Operation, "open-jobs", StringComparison.OrdinalIgnoreCase))
                {
                    PrinterApi.OpenJobs(request.CurrentName);
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                }
                else if (string.Equals(request.Operation, "cancel-current-job", StringComparison.OrdinalIgnoreCase))
                {
                    var canceled = PrinterApi.CancelCurrentJob(request.CurrentName)
                        || DeliveredPrintCancellation.CancelQueue(localState, request.CurrentName);
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>
                    {
                        new QueueDescriptor { Name = request.CurrentName, Jobs = canceled ? 1u : 0u },
                    });
                }
                else if (string.Equals(request.Operation, "cancel-delivered-job", StringComparison.OrdinalIgnoreCase))
                {
                    DeliveredPrintCancellation.Request(localState, request.JobId);
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                }
                else if (string.Equals(request.Operation, "cancelable-job", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>
                    {
                        new QueueDescriptor { Name = request.CurrentName,
                            Jobs = PrinterApi.HasCurrentJob(request.CurrentName)
                                || DeliveredPrintCancellation.HasJob(localState, request.CurrentName) ? 1u : 0u },
                    });
                }
                else if (string.Equals(request.Operation, "current-job", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>
                    {
                        new QueueDescriptor
                        {
                            Name = request.CurrentName,
                            Jobs = PrinterApi.HasCurrentJob(request.CurrentName) ? 1u : 0u,
                        },
                    });
                }
                else if (string.Equals(request.Operation, "set-default", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(
                        PrinterApi.SetDefaultPrinter(request.CurrentName));
                }
                else if (string.Equals(
                    request.Operation, "set-user-defaults", StringComparison.OrdinalIgnoreCase))
                {
                    var elapsed = Stopwatch.StartNew();
                    PrinterApi.SetUserDefaults(
                        request.CurrentName,
                        request.PageId,
                        request.PageWidthMm,
                        request.PageHeightMm,
                        request.Orientation,
                        request.ResolutionX,
                        request.ResolutionY, request.Borderless);
                    TryLog(localState, "Legacy printer defaults written and verified: queue="
                        + request.CurrentName + "; elapsedMs=" + elapsed.ElapsedMilliseconds);
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                }
                else if (string.Equals(request.Operation, "verify-user-defaults", StringComparison.OrdinalIgnoreCase))
                {
                    PrinterApi.VerifyUserDefaults(
                        request.CurrentName, request.PageId,
                        request.PageWidthMm, request.PageHeightMm,
                        request.Orientation, request.ResolutionX, request.ResolutionY, request.Borderless);
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                }
                else if (string.Equals(request.Operation, "read-user-defaults", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                    response.LegacyDefaults = PrinterApi.ReadUserDefaults(request.CurrentName);
                }
                else if (string.Equals(request.Operation, "read-configuration-ticket", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                    response.ConfigurationTicketXml = PrinterApi.ReadConfigurationTicketXml(request.CurrentName);
                }
                else if (string.Equals(request.Operation, "notify-user-defaults", StringComparison.OrdinalIgnoreCase))
                {
                    var notified = PrinterApi.NotifyDefaultSettingsChanged(request.CurrentName);
                    TryLog(localState, "WM_DEVMODECHANGE notification returned: queue="
                        + request.CurrentName + "; delivered=" + notified);
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                }
                else if (string.Equals(request.Operation, "runtime-self-test", StringComparison.OrdinalIgnoreCase))
                {
                    RipRequestProcessor.ValidateRuntime(localState);
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                }
                else if (string.Equals(request.Operation, "collect-support-report", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                    response.ReportPath = CollectSupportReport(localState, request.ManagerLog);
                }
                else if (string.Equals(request.Operation, "maintenance", StringComparison.OrdinalIgnoreCase))
                {
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                    response.MaintenanceTransport = RipRequestProcessor.RunMaintenance(localState, request.MaintenanceJson);
                }
                else if (string.Equals(request.Operation, "install-model-package", StringComparison.OrdinalIgnoreCase))
                {
                    InstallModelPackage(
                        request.ModulePackagePath,
                        request.ModuleDependencyPaths,
                        request.ModulePackageIdentity,
                        request.ModulePackageVersion);
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                }
                else if (string.Equals(request.Operation, "remove-model-package", StringComparison.OrdinalIgnoreCase))
                {
                    RemoveModelPackage(request.ModulePackageIdentity);
                    response = QueueHelperResponse.Pass(new List<QueueDescriptor>());
                }
                else
                {
                    throw new InvalidDataException("Unsupported queue helper operation: " + request.Operation);
                }
            }
            catch (Exception error)
            {
                response = RequestFailure(localState, request?.Operation, error);
            }

            return response;
        }

        private static QueueHelperResponse RequestFailure(string localState, string request, Exception error)
        {
            var native = error as Win32Exception;
            TryLog(localState, "Request failed for " + request + ": " + error);
            return QueueHelperResponse.Fail(native?.NativeErrorCode ?? error.HResult, error.Message);
        }

        private static T ReadJson<T>(string path)
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                return (T)new DataContractJsonSerializer(typeof(T)).ReadObject(stream);
        }

        private static string CollectSupportReport(string localState, string managerLog)
        {
            var script = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "Collect-SupportReport.ps1");
            if (!File.Exists(script))
                throw new FileNotFoundException("Support report collector is missing.", script);
            var token = Guid.NewGuid().ToString("N");
            var sessionPath = Path.Combine(localState, token + ".manager-support.log");
            var resultPath = Path.Combine(localState, token + ".support-result.txt");
            try
            {
                File.WriteAllText(sessionPath, managerLog ?? string.Empty, new UTF8Encoding(false));
                var arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" + script +
                    "\" -LocalStatePath \"" + localState + "\" -ManagerLogPath \"" + sessionPath +
                    "\" -ResultPath \"" + resultPath + "\"";
                using (var process = Process.Start(new ProcessStartInfo
                {
                    FileName = "powershell.exe",
                    Arguments = arguments,
                    CreateNoWindow = true,
                    UseShellExecute = false,
                    WindowStyle = ProcessWindowStyle.Hidden,
                }))
                {
                    if (process == null)
                        throw new InvalidOperationException("Could not start the support report collector.");
                    if (!process.WaitForExit(120000))
                    {
                        process.Kill();
                        throw new TimeoutException("Support report collection timed out.");
                    }
                    if (process.ExitCode != 0 || !File.Exists(resultPath))
                        throw new InvalidOperationException("Support report collection failed (exit code " +
                            process.ExitCode + ").");
                }
                var archivePath = File.ReadAllText(resultPath, Encoding.UTF8).Trim();
                if (!File.Exists(archivePath))
                    throw new FileNotFoundException("Support report archive was not created.", archivePath);
                return archivePath;
            }
            finally
            {
                try { File.Delete(sessionPath); } catch { }
                try { File.Delete(resultPath); } catch { }
            }
        }

        private static void InstallModelPackage(
            string packageRelativePath,
            IEnumerable<string> dependencyRelativePaths,
            string packageIdentity,
            string packageVersion)
        {
            const string identityPrefix = "pyToneRIP.Printer.";
            if (string.IsNullOrWhiteSpace(packageIdentity)
                || !packageIdentity.StartsWith(identityPrefix, StringComparison.Ordinal)
                || packageIdentity.Substring(identityPrefix.Length).Any(
                    character => !char.IsLetterOrDigit(character)))
                throw new InvalidDataException("Invalid pyToneRIP model package identity.");
            if (!Version.TryParse(packageVersion, out Version parsedVersion)
                || parsedVersion.Revision < 0)
                throw new InvalidDataException("Invalid pyToneRIP model package version.");

            var deploymentRoot = Path.GetFullPath(Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "pyToneRIP", "ModuleRepository")).TrimEnd(Path.DirectorySeparatorChar) +
                Path.DirectorySeparatorChar;
            var fullPackagePath = ResolveDeploymentPackagePath(packageRelativePath, deploymentRoot);
            var dependencies = (dependencyRelativePaths ?? Enumerable.Empty<string>())
                .Select(path => ResolveDeploymentPackagePath(path, deploymentRoot))
                .ToArray();

            var command = "$ErrorActionPreference = 'Stop'; "
                + "$installed = Get-AppxPackage -Name '"
                + EscapePowerShellLiteral(packageIdentity) + "'; "
                + "if ($null -eq $installed -or $installed.Version.ToString() -ne '"
                + EscapePowerShellLiteral(packageVersion) + "') { "
                + "Add-AppxPackage -Path '"
                + EscapePowerShellLiteral(fullPackagePath) + "'";
            if (dependencies.Length != 0)
            {
                command += " -DependencyPath @(" + string.Join(",", dependencies.Select(path =>
                    "'" + EscapePowerShellLiteral(path) + "'")) + ")";
            }
            command += " }";
            var encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(command));
            using (var process = Process.Start(new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + encoded,
                CreateNoWindow = true,
                UseShellExecute = false,
                WindowStyle = ProcessWindowStyle.Hidden,
            }))
            {
                if (process == null)
                    throw new InvalidOperationException("Could not start Windows package installation.");
                process.WaitForExit();
                if (process.ExitCode != 0)
                    throw new InvalidOperationException(
                        "Windows package installation failed with exit code " + process.ExitCode + ".");
            }
        }

        private static void RemoveModelPackage(string packageIdentity)
        {
            const string prefix = "pyToneRIP.Printer.";
            if (string.IsNullOrWhiteSpace(packageIdentity)
                || !packageIdentity.StartsWith(prefix, StringComparison.Ordinal)
                || packageIdentity.Substring(prefix.Length).Any(character => !char.IsLetterOrDigit(character)))
                throw new InvalidDataException("Invalid pyToneRIP model package identity.");

            var command = "$ErrorActionPreference = 'Stop'; "
                + "$package = Get-AppxPackage -Name '" + EscapePowerShellLiteral(packageIdentity) + "'; "
                + "if ($null -ne $package) { Remove-AppxPackage -Package $package.PackageFullName }";
            var encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(command));
            using (var process = Process.Start(new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + encoded,
                CreateNoWindow = true,
                UseShellExecute = false,
                WindowStyle = ProcessWindowStyle.Hidden,
            }))
            {
                if (process == null)
                    throw new InvalidOperationException("Could not start Windows package removal.");
                process.WaitForExit();
                if (process.ExitCode != 0)
                    throw new InvalidOperationException(
                        "Windows package removal failed with exit code " + process.ExitCode + ".");
            }
        }

        private static string ResolveDeploymentPackagePath(string relativePath, string deploymentRoot)
        {
            if (string.IsNullOrWhiteSpace(relativePath) || Path.IsPathRooted(relativePath))
                throw new InvalidDataException("A module package path is missing.");
            var segments = relativePath.Replace('/', '\\').Split('\\');
            if (segments.Length == 0 || segments.Any(segment =>
                string.IsNullOrWhiteSpace(segment) || segment == "." || segment == ".."))
                throw new InvalidDataException("Invalid pyToneRIP module package path.");
            var fullPath = Path.GetFullPath(Path.Combine(deploymentRoot, relativePath));
            if (!fullPath.StartsWith(deploymentRoot, StringComparison.OrdinalIgnoreCase) ||
                !string.Equals(Path.GetExtension(fullPath), ".msix", StringComparison.OrdinalIgnoreCase) ||
                !File.Exists(fullPath))
                throw new InvalidDataException("Invalid pyToneRIP module package path.");
            return fullPath;
        }

        private static string EscapePowerShellLiteral(string value) => value.Replace("'", "''");

        private static void WriteJsonAtomically<T>(string path, T value)
        {
            var temporaryPath = path + ".tmp";
            using (var stream = new FileStream(temporaryPath, FileMode.Create, FileAccess.Write, FileShare.None))
                new DataContractJsonSerializer(typeof(T)).WriteObject(stream, value);
            if (File.Exists(path))
                File.Delete(path);
            File.Move(temporaryPath, path);
        }

        private static string GetPackageLocalStatePath()
        {
            uint length = 0;
            var result = GetCurrentPackageFamilyName(ref length, null);
            if (result != ErrorInsufficientBuffer)
            {
                if (result == AppModelErrorNoPackage)
                    throw new InvalidOperationException("Queue helper must run from the installed pyToneRIP package.");
                throw new Win32Exception(result, "Could not determine the package family name.");
            }

            var familyName = new StringBuilder((int)length);
            result = GetCurrentPackageFamilyName(ref length, familyName);
            if (result != 0)
                throw new Win32Exception(result, "Could not read the package family name.");

            return Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Packages",
                familyName.ToString(),
                "LocalState");
        }

        internal static void Log(string localState, string message) =>
            TryLog(localState, message);

        internal static void TryLog(string localState, string message)
        {
            try
            {
                if (string.IsNullOrWhiteSpace(localState))
                    return;
                File.AppendAllText(
                    Path.Combine(localState, "pyToneRIP-queue-helper.log"),
                    DateTimeOffset.Now.ToString("O") + " | " + message + Environment.NewLine,
                    Encoding.UTF8);
            }
            catch { }
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetCurrentPackageFamilyName(
            ref uint packageFamilyNameLength,
            StringBuilder packageFamilyName);
    }

    internal static class PrinterApi
    {
        internal static void OpenJobs(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName) || queueName.IndexOf('"') >= 0)
                throw new InvalidDataException("Invalid Windows printer queue name.");
            if (!ListPrinters().Any(queue => string.Equals(
                queue.Name, queueName, StringComparison.OrdinalIgnoreCase)))
                throw new InvalidOperationException("The selected Windows printer queue no longer exists.");

            var process = Process.Start(new ProcessStartInfo
            {
                FileName = "rundll32.exe",
                Arguments = "printui.dll,PrintUIEntry /o /n \"" + queueName + "\"",
                UseShellExecute = false,
                CreateNoWindow = true,
            });
            if (process == null)
                throw new InvalidOperationException("Windows could not open the print jobs window.");
        }

        internal static bool CancelCurrentJob(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName) || !ListPrinters().Any(queue =>
                string.Equals(queue.Name, queueName, StringComparison.OrdinalIgnoreCase)
                && !queue.IsPendingDeletion))
                throw new InvalidOperationException("The selected Windows printer queue no longer exists.");
            // Finishing between the availability check and click is normal.
            return PrintQueueCancellationMonitor.CancelCurrentJob(queueName).HasValue;
        }

        internal static bool HasCurrentJob(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName) || !ListPrinters().Any(queue =>
                string.Equals(queue.Name, queueName, StringComparison.OrdinalIgnoreCase)
                && !queue.IsPendingDeletion))
                return false;
            return PrintQueueCancellationMonitor.HasCurrentJob(queueName);
        }

        private const uint PrinterEnumLocal = 0x00000002;
        private const uint PrinterEnumConnections = 0x00000004;
        private const uint PrinterAccessAdminister = 0x00000004;
        private const uint PrinterAccessUse = 0x00000008;
        internal const uint PendingDeletionStatus = 0x00000004;
        private const uint PrinterControlPurge = 3;
        private const int ErrorInsufficientBuffer = 122;
        private const int ErrorFileNotFound = 2;
        private const int DmOrientation = 0x00000001;
        private const int DmPaperSize = 0x00000002;
        private const int DmPaperLength = 0x00000004;
        private const int DmPaperWidth = 0x00000008;
        private const int DmScale = 0x00000010;
        private const int DmPrintQuality = 0x00000400;
        private const int DmYResolution = 0x00002000;
        private const int DmFormName = 0x00010000;
        private const int DmOutBuffer = 0x00000002;
        private const int DmInBuffer = 0x00000008;
        private const short DcPapers = 2;
        private const short DcPaperSize = 3;
        private const short DcPaperNames = 16;
        private const int DevModeSizeOffset = 68;
        private const int DevModeDriverExtraOffset = 70;
        private const int DevModeFieldsOffset = 72;
        private const int DevModeOrientationOffset = 76;
        private const int DevModePaperSizeOffset = 78;
        private const int DevModeScaleOffset = 84;
        private const int DevModePrintQualityOffset = 90;
        private const int DevModeYResolutionOffset = 96;
        private const int DevModeFormNameOffset = 102;
        private const int DevModeFormNameCharacters = 32;

        public static List<QueueDescriptor> ListPrinters()
        {
            var queues = ReadPrinters(PrinterEnumLocal | PrinterEnumConnections);
            var usb = queues.Where(queue => queue.IsEpsonRawDestination
                && !EpsonRawQueueMatcher.IsOffline(queue.Status)
                && !queue.Name.StartsWith(@"\\", StringComparison.Ordinal)
                && EpsonRawQueueMatcher.IsUsbPort(queue.PortName)).ToList();
            if (usb.Count == 0) return queues;
            try
            {
                var present = UsbPrinterDiscovery.ReadPresentPorts();
                foreach (var queue in usb)
                    if (!present.Contains(queue.PortName)) queue.Status |= EpsonRawQueueMatcher.OfflineStatus;
            }
            catch (InvalidOperationException error)
            {
                // Keep the spooler's flags if PnP enumeration is temporarily unavailable.
                System.Diagnostics.Trace.WriteLine("Printer USB presence: " + error.Message);
            }
            return queues;
        }

        public static List<QueueDescriptor> ListLocalPrinters() => ReadPrinters(PrinterEnumLocal);

        private static List<QueueDescriptor> ReadPrinters(uint flags)
        {
            // Default printer is a per-user Windows setting. Read it on every
            // live enumeration instead of persisting a second, stale copy in
            // pyToneRIP's registry.
            var defaultPrinterName = GetDefaultPrinterName();
            uint needed = 0;
            uint returned = 0;
            EnumPrintersW(
                flags,
                null,
                2,
                IntPtr.Zero,
                0,
                ref needed,
                ref returned);
            var error = Marshal.GetLastWin32Error();
            if (needed == 0)
            {
                if (error == 0)
                    return new List<QueueDescriptor>();
                throw new Win32Exception(error, "Could not determine the printer list size.");
            }
            if (error != ErrorInsufficientBuffer)
                throw new Win32Exception(error, "Could not determine the printer list size.");

            var buffer = Marshal.AllocHGlobal((int)needed);
            try
            {
                if (!EnumPrintersW(
                    flags,
                    null,
                    2,
                    buffer,
                    needed,
                    ref needed,
                    ref returned))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not enumerate printers.");

                var result = new List<QueueDescriptor>((int)returned);
                var itemSize = Marshal.SizeOf(typeof(PrinterInfo2));
                for (var index = 0; index < returned; index++)
                {
                    var item = (PrinterInfo2)Marshal.PtrToStructure(
                        IntPtr.Add(buffer, index * itemSize),
                        typeof(PrinterInfo2));
                    var descriptor = ToDescriptor(item);
                    descriptor.IsDefault = string.Equals(
                        descriptor.Name,
                        defaultPrinterName,
                        StringComparison.OrdinalIgnoreCase);
                    result.Add(descriptor);
                }
                return result;
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }

        public static List<QueueDescriptor> SetDefaultPrinter(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName))
                throw new InvalidDataException("The selected Windows printer queue name is empty.");

            var target = ListPrinters().FirstOrDefault(queue => string.Equals(
                queue.Name, queueName, StringComparison.OrdinalIgnoreCase)
                && !queue.IsPendingDeletion);
            if (target == null)
                throw new InvalidOperationException(
                    "The selected Windows printer queue no longer exists.");
            if (string.IsNullOrWhiteSpace(target.DriverName)
                || string.IsNullOrWhiteSpace(target.PortName))
                throw new InvalidOperationException(
                    "The selected Windows printer queue has no valid driver or port.");

            // Windows 10/11 can keep "Let Windows manage my default printer"
            // enabled. In that mode Windows may immediately replace a default
            // selected through SetDefaultPrinterW (often with Microsoft Print
            // to PDF). Opt the current user into the explicit default-printer
            // mode before applying and verifying the requested queue.
            SetExplicitDefaultPrinterMode();
            if (!SetDefaultPrinterW(target.Name))
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Windows could not set the selected printer as default.");

            // Notify the shell and the modern Printers & scanners page. Without
            // this broadcast those clients can retain the previous PDF printer
            // until their cached printer view is reopened.
            NotifyPrinterSettingsChanged();

            var actualDefault = GetDefaultPrinterName();
            if (!string.Equals(actualDefault, target.Name, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    "Windows did not retain the selected printer as the default printer.");

            // Return a fresh enumeration so the UI can update every row from
            // the state Windows accepted, including removal of the old mark.
            return ListPrinters();
        }

        private static void SetExplicitDefaultPrinterMode()
        {
            using (var key = Registry.CurrentUser.CreateSubKey(
                @"Software\Microsoft\Windows NT\CurrentVersion\Windows"))
            {
                if (key == null)
                    throw new InvalidOperationException(
                        "Windows did not provide the current-user printer settings key.");
                key.SetValue(
                    "LegacyDefaultPrinterMode",
                    1,
                    RegistryValueKind.DWord);
            }
        }

        private static void NotifyPrinterSettingsChanged()
        {
            SHChangeNotify(
                ShellChangeAssociationChanged,
                ShellChangeNotifyIdList,
                IntPtr.Zero,
                IntPtr.Zero);
        }

        private static string GetDefaultPrinterName()
        {
            uint required = 0;
            GetDefaultPrinterW(null, ref required);
            var error = Marshal.GetLastWin32Error();
            if (required == 0)
            {
                // ERROR_FILE_NOT_FOUND means that this user has no default
                // printer yet; it is a valid state, not a helper failure.
                if (error == 0 || error == ErrorFileNotFound)
                    return null;
                throw new Win32Exception(error, "Could not read the default printer.");
            }
            if (error != ErrorInsufficientBuffer)
                throw new Win32Exception(error, "Could not determine the default printer name size.");

            var name = new StringBuilder((int)required);
            if (!GetDefaultPrinterW(name, ref required))
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Could not read the default printer.");
            return name.ToString();
        }

        public static QueueDescriptor RenamePrinter(string currentName, string portName, string desiredName)
        {
            if (string.IsNullOrWhiteSpace(desiredName))
                throw new InvalidDataException("The requested printer name is empty.");

            var queues = ListPrinters();
            // A package reinstall can temporarily leave the old renamed queue
            // PendingDeletion beside the newly registered free queue. Select
            // the exact active queue name first; port identity alone is then
            // ambiguous because both records deliberately share one slot port.
            var target = !string.IsNullOrWhiteSpace(currentName)
                ? queues.FirstOrDefault(queue => string.Equals(
                    queue.Name, currentName, StringComparison.OrdinalIgnoreCase)
                    && !queue.IsPendingDeletion)
                : null;
            if (target == null && !string.IsNullOrWhiteSpace(portName))
                target = queues.FirstOrDefault(queue =>
                    string.Equals(queue.PortName, portName, StringComparison.OrdinalIgnoreCase)
                    && !queue.IsPendingDeletion);
            if (target == null && !string.IsNullOrWhiteSpace(portName))
                target = queues.FirstOrDefault(queue => string.Equals(
                    queue.PortName, portName, StringComparison.OrdinalIgnoreCase));
            if (target == null)
                throw new InvalidOperationException("The Windows printer queue could not be found.");

            PurgeStaleSlotDuplicates(target);
            queues = WaitForStaleSlotDuplicates(target.PortName);
            target = queues.FirstOrDefault(queue => string.Equals(
                queue.Name, currentName, StringComparison.OrdinalIgnoreCase))
                ?? queues.FirstOrDefault(queue =>
                    string.Equals(queue.PortName, target.PortName, StringComparison.OrdinalIgnoreCase)
                    && !queue.IsPendingDeletion);
            if (target == null)
                throw new InvalidOperationException(
                    "The active Windows printer queue disappeared while its stale copy was removed.");

            var conflict = queues.FirstOrDefault(queue =>
                !string.Equals(queue.Name, target.Name, StringComparison.OrdinalIgnoreCase) &&
                string.Equals(queue.Name, desiredName, StringComparison.OrdinalIgnoreCase));
            if (conflict != null)
                throw new InvalidOperationException("A Windows printer with that name already exists.");
            if (string.Equals(target.Name, desiredName, StringComparison.Ordinal))
                return target;

            var defaults = new PrinterDefaults { DesiredAccess = PrinterAccessAdminister | PrinterAccessUse };
            IntPtr handle;
            if (!OpenPrinterW(target.Name, out handle, ref defaults))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not open the printer for renaming.");

            try
            {
                uint needed = 0;
                GetPrinterW(handle, 2, IntPtr.Zero, 0, ref needed);
                var error = Marshal.GetLastWin32Error();
                if (needed == 0 || error != ErrorInsufficientBuffer)
                    throw new Win32Exception(error, "Could not determine the printer settings size.");

                var buffer = Marshal.AllocHGlobal((int)needed);
                var namePointer = IntPtr.Zero;
                try
                {
                    if (!GetPrinterW(handle, 2, buffer, needed, ref needed))
                        throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read the printer settings.");
                    var info = (PrinterInfo2)Marshal.PtrToStructure(buffer, typeof(PrinterInfo2));
                    namePointer = Marshal.StringToHGlobalUni(desiredName.Trim());
                    info.PrinterName = namePointer;
                    info.SecurityDescriptor = IntPtr.Zero;
                    Marshal.StructureToPtr(info, buffer, false);
                    if (!SetPrinterW(handle, 2, buffer, 0))
                        throw new Win32Exception(Marshal.GetLastWin32Error(), "Windows rejected the printer rename.");
                }
                finally
                {
                    if (namePointer != IntPtr.Zero)
                        Marshal.FreeHGlobal(namePointer);
                    Marshal.FreeHGlobal(buffer);
                }
            }
            finally
            {
                ClosePrinter(handle);
            }

            var renamed = ListPrinters().FirstOrDefault(queue =>
                string.Equals(queue.Name, desiredName, StringComparison.Ordinal));
            if (renamed == null)
                throw new InvalidOperationException("The renamed printer could not be read back from Windows.");
            return renamed;
        }

        public static void SetUserDefaults(
            string queueName,
            string pageId,
            double pageWidthMm,
            double pageHeightMm,
            string orientation,
            int resolutionX,
            int resolutionY,
            bool? borderless = null)
        {
            if (string.IsNullOrWhiteSpace(queueName))
                throw new InvalidDataException("The Windows queue name is empty.");
            if (string.IsNullOrWhiteSpace(pageId) || pageWidthMm <= 0 || pageHeightMm <= 0)
                throw new InvalidDataException("The requested page dimensions are invalid.");
            if (resolutionX <= 0 || resolutionX > short.MaxValue
                || resolutionY <= 0 || resolutionY > short.MaxValue)
                throw new InvalidDataException("The requested raster resolution is invalid.");

            var queue = ListPrinters().FirstOrDefault(item => string.Equals(
                item.Name, queueName, StringComparison.OrdinalIgnoreCase));
            if (queue == null)
                throw new InvalidOperationException("The Windows printer queue could not be found.");
            if (!queue.PortName.StartsWith(
                    "pyToneRIP.Manager_", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    "Printer defaults can be changed only for a Manager-owned virtual queue.");
            var convertedMode = ResolvePrintTicketDevMode(queue.Name, pageId,
                pageWidthMm, pageHeightMm, borderless, orientation, resolutionX, resolutionY);
            var paper = FindPaperCapability(
                queue.Name, queue.PortName, pageId, pageWidthMm, pageHeightMm, borderless, convertedMode);

            var defaults = new PrinterDefaults
            {
                DesiredAccess = PrinterAccessAdminister | PrinterAccessUse,
            };
            IntPtr handle;
            if (!OpenPrinterW(queue.Name, out handle, ref defaults))
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(), "Could not open the printer defaults.");

            try
            {
                var size = DocumentPropertiesW(
                    IntPtr.Zero, handle, queue.Name,
                    IntPtr.Zero, IntPtr.Zero, 0);
                if (size <= 0)
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(), "Could not determine the DEVMODE size.");
                size = Math.Max(size, convertedMode.Length);

                var devMode = Marshal.AllocHGlobal(size);
                try
                {
                    var result = DocumentPropertiesW(
                        IntPtr.Zero, handle, queue.Name,
                        devMode, IntPtr.Zero, DmOutBuffer);
                    if (result < 0)
                        throw new Win32Exception(
                            Marshal.GetLastWin32Error(), "Could not read the current DEVMODE; result="
                            + result + "; queue=" + queue.Name + "; page=" + pageId + ".");

                    var publicSize = (ushort)Marshal.ReadInt16(devMode, DevModeSizeOffset);
                    var privateSize = (ushort)Marshal.ReadInt16(devMode, DevModeDriverExtraOffset);
                    if (publicSize < DevModeFormNameOffset
                            + DevModeFormNameCharacters * sizeof(char)
                        || publicSize + privateSize > size)
                        throw new InvalidDataException("Windows returned an invalid DEVMODE buffer.");

                    // Keep the provider's private ticket data, including the
                    // borderless selection, along with the public paper ID.
                    Marshal.Copy(convertedMode, 0, devMode, convertedMode.Length);
                    var fields = Marshal.ReadInt32(devMode, DevModeFieldsOffset);
                    fields |= DmOrientation | DmPaperSize
                        | DmPrintQuality | DmYResolution;
                    // Use the named form without custom width/height overrides.
                    // Keep an explicit 100% value even when DM_SCALE is not active.
                    fields &= ~(DmPaperLength | DmPaperWidth | DmScale);
                    Marshal.WriteInt32(devMode, DevModeFieldsOffset, fields);
                    Marshal.WriteInt16(
                        devMode,
                        DevModeOrientationOffset,
                        string.Equals(orientation, "landscape", StringComparison.OrdinalIgnoreCase)
                            ? (short)2 : (short)1);
                    Marshal.WriteInt16(devMode, DevModePaperSizeOffset, paper.Id);
                    Marshal.WriteInt16(devMode, DevModeScaleOffset, 100);
                    Marshal.WriteInt16(devMode, DevModePrintQualityOffset, (short)resolutionX);
                    Marshal.WriteInt16(devMode, DevModeYResolutionOffset, (short)resolutionY);
                    // DC_PAPERNAMES supplies display labels, not EnumForms
                    // system names. Preserve the converter's dmFormName and
                    // DM_FORMNAME flag instead of injecting a localized label.

                    result = DocumentPropertiesW(
                        IntPtr.Zero, handle, queue.Name,
                        devMode, devMode, DmInBuffer | DmOutBuffer);
                    if (result < 0)
                        throw new Win32Exception(
                            Marshal.GetLastWin32Error(), "The printer driver rejected the DEVMODE; result="
                            + result + "; queue=" + queue.Name + "; page=" + pageId + ".");

                    var validatedFields = Marshal.ReadInt32(devMode, DevModeFieldsOffset);
                    var validatedScale = Marshal.ReadInt16(devMode, DevModeScaleOffset);
                    if ((validatedFields & DmScale) != 0 && validatedScale <= 0)
                        throw new InvalidDataException(
                            "The printer driver returned an unsafe zero DEVMODE scale.");
                    EnsureDevModeSelection(devMode, paper.Id, pageId, orientation,
                        resolutionX, resolutionY, "normalized DEVMODE", queue.Name,
                        pageWidthMm, pageHeightMm, borderless);

                    var printerInfo = Marshal.AllocHGlobal(IntPtr.Size);
                    try
                    {
                        Marshal.WriteIntPtr(printerInfo, devMode);
                        // This local, unshared virtual queue belongs to one user.
                        // Keep both Windows default views on the selected page:
                        // classic applications may initialize Page Setup from the
                        // queue default rather than the per-user DEVMODE.
                        if (!SetPrinterW(handle, 8, printerInfo, 0))
                            throw new Win32Exception(
                                Marshal.GetLastWin32Error(),
                                "Windows rejected the queue printer defaults.");
                        if (!SetPrinterW(handle, 9, printerInfo, 0))
                            throw new Win32Exception(
                                Marshal.GetLastWin32Error(),
                                "Windows rejected the per-user printer defaults.");
                        EnsureStoredPaperSelection(handle, 8, paper.Id, pageId,
                            orientation, resolutionX, resolutionY, queue.Name,
                            pageWidthMm, pageHeightMm, borderless);
                        EnsureStoredPaperSelection(handle, 9, paper.Id, pageId,
                            orientation, resolutionX, resolutionY, queue.Name,
                            pageWidthMm, pageHeightMm, borderless);
                    }
                    finally
                    {
                        Marshal.FreeHGlobal(printerInfo);
                    }
                }
                finally
                {
                    Marshal.FreeHGlobal(devMode);
                }
            }
            finally
            {
                ClosePrinter(handle);
            }
        }

        private static void EnsureStoredPaperSelection(
            IntPtr handle, uint level, short expectedPaperId, string pageId,
            string orientation, int resolutionX, int resolutionY, string queueName,
            double widthMm, double heightMm, bool? borderless)
        {
            uint needed = 0;
            GetPrinterW(handle, level, IntPtr.Zero, 0, ref needed);
            var error = Marshal.GetLastWin32Error();
            if (needed < IntPtr.Size || error != ErrorInsufficientBuffer)
                throw new Win32Exception(error, "Could not determine the stored printer defaults size.");

            var buffer = Marshal.AllocHGlobal((int)needed);
            try
            {
                if (!GetPrinterW(handle, level, buffer, needed, ref needed))
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(), "Could not read back the printer defaults.");
                var storedDevMode = Marshal.ReadIntPtr(buffer);
                if (storedDevMode == IntPtr.Zero)
                    throw new InvalidDataException(
                        "Windows returned no DEVMODE in printer defaults level " + level + ".");
                EnsureDevModeSelection(storedDevMode, expectedPaperId, pageId,
                    orientation, resolutionX, resolutionY, "defaults level " + level,
                    queueName, widthMm, heightMm, borderless);
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }

        public static void VerifyUserDefaults(
            string queueName, string pageId, double pageWidthMm, double pageHeightMm,
            string orientation, int resolutionX, int resolutionY, bool? borderless = null)
        {
            var queue = ListPrinters().FirstOrDefault(item => string.Equals(
                item.Name, queueName, StringComparison.OrdinalIgnoreCase));
            if (queue == null || !queue.PortName.StartsWith(
                "pyToneRIP.Manager_", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("A Manager-owned virtual queue is required.");
            var paper = FindPaperCapability(queue.Name, queue.PortName,
                pageId, pageWidthMm, pageHeightMm, borderless);
            var defaults = new PrinterDefaults { DesiredAccess = PrinterAccessUse };
            IntPtr handle;
            if (!OpenPrinterW(queue.Name, out handle, ref defaults))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read printer defaults.");
            try
            {
                EnsureStoredPaperSelection(handle, 8, paper.Id, pageId, orientation, resolutionX, resolutionY,
                    queue.Name, pageWidthMm, pageHeightMm, borderless);
                EnsureStoredPaperSelection(handle, 9, paper.Id, pageId, orientation, resolutionX, resolutionY,
                    queue.Name, pageWidthMm, pageHeightMm, borderless);
                var size = DocumentPropertiesW(IntPtr.Zero, handle, queue.Name,
                    IntPtr.Zero, IntPtr.Zero, 0);
                if (size <= 0) throw new InvalidDataException("Windows returned an invalid DEVMODE size.");
                var mode = Marshal.AllocHGlobal(size);
                try
                {
                    if (DocumentPropertiesW(IntPtr.Zero, handle, queue.Name, mode,
                        IntPtr.Zero, DmOutBuffer) < 0)
                        throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read application DEVMODE.");
                    EnsureDevModeSelection(mode, paper.Id, pageId, orientation,
                        resolutionX, resolutionY, "DocumentProperties", queue.Name,
                        pageWidthMm, pageHeightMm, borderless);
                }
                finally { Marshal.FreeHGlobal(mode); }
            }
            finally { ClosePrinter(handle); }
        }

        public static LegacyPrinterDefaults ReadUserDefaults(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName))
                throw new InvalidDataException("The Windows queue name is empty.");
            var queue = ListPrinters().FirstOrDefault(item => string.Equals(
                item.Name, queueName, StringComparison.OrdinalIgnoreCase));
            if (queue == null || !queue.PortName.StartsWith(
                "pyToneRIP.Manager_", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("A Manager-owned virtual queue is required.");

            var defaults = new PrinterDefaults { DesiredAccess = PrinterAccessUse };
            IntPtr handle;
            if (!OpenPrinterW(queue.Name, out handle, ref defaults))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read per-user printer defaults.");
            try
            {
                var data = ReadStoredDevMode(handle, 9);
                var hasUserDevMode = data != null;
                if (data == null)
                {
                    var size = DocumentPropertiesW(IntPtr.Zero, handle, queue.Name,
                        IntPtr.Zero, IntPtr.Zero, 0);
                    if (size <= 0) throw new InvalidDataException("Windows returned an invalid DEVMODE size.");
                    var mode = Marshal.AllocHGlobal(size);
                    try
                    {
                        if (DocumentPropertiesW(IntPtr.Zero, handle, queue.Name, mode,
                            IntPtr.Zero, DmOutBuffer) < 0)
                            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read the application DEVMODE.");
                        data = CopyDevMode(mode, size);
                    }
                    finally { Marshal.FreeHGlobal(mode); }
                }

                using (var converter = new System.Printing.Interop.PrintTicketConverter(queue.Name, 1))
                {
                    var ticket = converter.ConvertDevModeToPrintTicket(data);
                    var page = ticket.PageMediaSize;
                    var widthMm = page?.Width * 25.4 / 96.0;
                    var heightMm = page?.Height * 25.4 / 96.0;
                    if (!widthMm.HasValue || !heightMm.HasValue)
                        throw new InvalidDataException("Per-user DEVMODE contains no readable page dimensions.");
                    return new LegacyPrinterDefaults
                    {
                        HasUserDevMode = hasUserDevMode,
                        PrintTicketXml = ReadTicketXml(ticket.GetXmlStream()),
                        PageWidthMm = widthMm.Value,
                        PageHeightMm = heightMm.Value,
                        Borderless = ticket.PageBorderless.HasValue
                            ? ticket.PageBorderless.Value == System.Printing.PageBorderless.Borderless
                            : (bool?)null,
                        Orientation = ticket.PageOrientation == System.Printing.PageOrientation.Landscape
                            ? "landscape" : "portrait",
                        ResolutionX = ticket.PageResolution?.X,
                        ResolutionY = ticket.PageResolution?.Y,
                    };
                }
            }
            finally { ClosePrinter(handle); }
        }

        public static string ReadConfigurationTicketXml(string queueName)
        {
            var queue = ListPrinters().FirstOrDefault(item => string.Equals(
                item.Name, queueName, StringComparison.OrdinalIgnoreCase));
            if (queue == null || queue.PortName == null || !queue.PortName.StartsWith(
                "pyToneRIP.Manager_", StringComparison.OrdinalIgnoreCase) || queue.PortName.IndexOf(
                "_pytone-rip:surecolorp900rip/", StringComparison.OrdinalIgnoreCase) < 0)
                throw new InvalidOperationException("A Manager-owned P900 queue is required for configuration XML recovery.");

            var defaults = new PrinterDefaults { DesiredAccess = PrinterAccessUse };
            IntPtr handle;
            if (!OpenPrinterW(queue.Name, out handle, ref defaults))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not inspect per-user printer defaults.");
            try
            {
                // CIM supplies queue configuration. It is a safe initial seed
                // only when Windows has no existing per-user DEVMODE. Never
                // silently substitute global settings for unreadable user data.
                EnsureConfigurationSeedHasNoUserDefaults(ReadStoredDevMode(handle, 9) != null);
                var xml = ConfigurationTicketXmlReader.Read(queue.Name);
                EnsureConfigurationSeedHasNoUserDefaults(ReadStoredDevMode(handle, 9) != null);
                return xml;
            }
            finally { ClosePrinter(handle); }
        }

        internal static void EnsureConfigurationSeedHasNoUserDefaults(bool hasUserDevMode)
        {
            if (hasUserDevMode)
                throw new InvalidOperationException(
                    "Queue configuration XML cannot replace existing per-user printer defaults.");
        }

        private static string ReadTicketXml(System.IO.Stream stream)
        {
            using (stream)
            using (var reader = new System.IO.StreamReader(stream, System.Text.Encoding.UTF8, true))
                return reader.ReadToEnd();
        }

        private static byte[] ReadStoredDevMode(IntPtr handle, uint level)
        {
            uint needed = 0;
            GetPrinterW(handle, level, IntPtr.Zero, 0, ref needed);
            var error = Marshal.GetLastWin32Error();
            if (needed == 0 && error != ErrorInsufficientBuffer)
                throw new Win32Exception(error, "Could not determine the stored DEVMODE size.");
            if (needed < IntPtr.Size) return null;
            var buffer = Marshal.AllocHGlobal((int)needed);
            try
            {
                if (!GetPrinterW(handle, level, buffer, needed, ref needed))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read the stored DEVMODE.");
                var mode = Marshal.ReadIntPtr(buffer);
                if (mode == IntPtr.Zero) return null;
                return CopyDevMode(mode, checked((int)needed));
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        private static byte[] CopyDevMode(IntPtr mode, int availableBytes)
        {
            var publicSize = (ushort)Marshal.ReadInt16(mode, DevModeSizeOffset);
            var privateSize = (ushort)Marshal.ReadInt16(mode, DevModeDriverExtraOffset);
            var totalSize = publicSize + privateSize;
            if (publicSize < DevModeFormNameOffset || totalSize > availableBytes)
                throw new InvalidDataException("Windows returned an invalid DEVMODE buffer.");
            var data = new byte[totalSize];
            Marshal.Copy(mode, data, 0, totalSize);
            return data;
        }

        private static void EnsureDevModeSelection(
            IntPtr mode, short paperId, string pageId, string orientation,
            int resolutionX, int resolutionY, string source, string queueName,
            double widthMm, double heightMm, bool? borderless)
        {
            if ((ushort)Marshal.ReadInt16(mode, DevModeSizeOffset) < DevModeFormNameOffset)
                throw new InvalidDataException("Windows returned a truncated DEVMODE in " + source + ".");
            var fields = Marshal.ReadInt32(mode, DevModeFieldsOffset);
            var required = DmPaperSize | DmOrientation | DmPrintQuality | DmYResolution;
            var expectedOrientation = string.Equals(orientation, "landscape",
                StringComparison.OrdinalIgnoreCase) ? 2 : 1;
            if ((fields & required) != required
                || (!borderless.HasValue && Marshal.ReadInt16(mode, DevModePaperSizeOffset) != paperId)
                || Marshal.ReadInt16(mode, DevModeOrientationOffset) != expectedOrientation
                || Marshal.ReadInt16(mode, DevModePrintQualityOffset) != resolutionX
                || Marshal.ReadInt16(mode, DevModeYResolutionOffset) != resolutionY
                || ((fields & DmScale) != 0 && Marshal.ReadInt16(mode, DevModeScaleOffset) <= 0))
                throw new InvalidDataException(
                    "Windows did not persist " + pageId + ", " + orientation + ", "
                    + resolutionX + "x" + resolutionY + " in " + source + ".");
            // Windows normalizes some borderless forms to a standard DMPAPER
            // number while preserving PageBorderless in the private DEVMODE.
            // Check both physical dimensions and that flag, including readback
            // of levels 8/9 and the application's DocumentProperties view.
            var publicSize = (ushort)Marshal.ReadInt16(mode, DevModeSizeOffset);
            var privateSize = (ushort)Marshal.ReadInt16(mode, DevModeDriverExtraOffset);
            var data = new byte[publicSize + privateSize];
            Marshal.Copy(mode, data, 0, data.Length);
            using (var converter = new System.Printing.Interop.PrintTicketConverter(queueName, 1))
            {
                var ticket = converter.ConvertDevModeToPrintTicket(data);
                EnsureTicketPageSelection(ticket, pageId, widthMm, heightMm, borderless, source);
            }
        }

        private static void EnsureTicketPageSelection(System.Printing.PrintTicket ticket,
            string pageId, double widthMm, double heightMm, bool? borderless, string source)
        {
            var page = ticket.PageMediaSize;
            var width = page?.Width * 25.4 / 96;
            var height = page?.Height * 25.4 / 96;
            var flagMatches = !borderless.HasValue || (borderless.Value
                ? ticket.PageBorderless == System.Printing.PageBorderless.Borderless
                : ticket.PageBorderless != System.Printing.PageBorderless.Borderless);
            if (!width.HasValue || !height.HasValue
                || Math.Abs(width.Value - widthMm) > 0.354
                || Math.Abs(height.Value - heightMm) > 0.354 || !flagMatches)
                throw new InvalidDataException("Windows did not persist the physical page or borderless mode for "
                    + pageId + " in " + source + ".");
        }

        public static bool NotifyDefaultSettingsChanged(string queueName)
        {
            var queue = ListPrinters().FirstOrDefault(item => string.Equals(
                item.Name, queueName, StringComparison.OrdinalIgnoreCase));
            if (queue == null || !queue.PortName.StartsWith(
                "pyToneRIP.Manager_", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("A Manager-owned virtual queue is required.");
            // https://learn.microsoft.com/windows/win32/gdi/wm-devmodechange
            // A hung window must not hold the Manager indefinitely.
            UIntPtr result;
            return SendMessageTimeoutW(new IntPtr(0xffff), 0x001b, UIntPtr.Zero,
                queue.Name, 0x0002, 1000, out result) != IntPtr.Zero;
        }

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr SendMessageTimeoutW(
            IntPtr window, uint message, UIntPtr wParam, string deviceName,
            uint flags, uint timeoutMilliseconds, out UIntPtr result);

        private static PaperCapability FindPaperCapability(
            string queueName,
            string portName,
            string pageId,
            double widthMm,
            double heightMm,
            bool? borderless = null,
            byte[] convertedMode = null)
        {
            var count = DeviceCapabilitiesW(
                queueName, portName, DcPapers, IntPtr.Zero, IntPtr.Zero);
            if (count <= 0)
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(), "Windows returned no supported paper sizes.");

            var papers = Marshal.AllocHGlobal(count * sizeof(short));
            var sizes = Marshal.AllocHGlobal(count * sizeof(int) * 2);
            var names = Marshal.AllocHGlobal(count * 64 * sizeof(char));
            try
            {
                var paperCount = DeviceCapabilitiesW(
                    queueName, portName, DcPapers, papers, IntPtr.Zero);
                var sizeCount = DeviceCapabilitiesW(
                    queueName, portName, DcPaperSize, sizes, IntPtr.Zero);
                var nameCount = DeviceCapabilitiesW(
                    queueName, portName, DcPaperNames, names, IntPtr.Zero);
                var available = Math.Min(paperCount, Math.Min(sizeCount, nameCount));
                if (available <= 0)
                    throw new InvalidOperationException(
                        "Windows could not enumerate the queue paper capabilities.");

                var values = new List<PaperCapability>(available);
                for (var index = 0; index < available; index++)
                {
                    values.Add(new PaperCapability
                    {
                        Id = Marshal.ReadInt16(papers, index * sizeof(short)),
                        WidthTenthsMm = Marshal.ReadInt32(sizes, index * sizeof(int) * 2),
                        HeightTenthsMm = Marshal.ReadInt32(
                            sizes, index * sizeof(int) * 2 + sizeof(int)),
                        Name = (Marshal.PtrToStringUni(
                            IntPtr.Add(names, index * 64 * sizeof(char)), 64) ?? "")
                            .TrimEnd('\0', ' '),
                    });
                }

                // Resolve the schema option before considering display names.
                // Equal dimensions do not distinguish A4 from A4 Borderless,
                // and DC_PAPERNAMES is localized (e.g. DIN A4 (Randlos)).
                var ticketMode = convertedMode ?? ResolvePrintTicketDevMode(queueName, pageId, widthMm, heightMm, borderless);
                var ticketPaperId = BitConverter.ToInt16(ticketMode, DevModePaperSizeOffset);
                var byTicket = values.FirstOrDefault(value => value.Id == ticketPaperId);
                if (byTicket != null
                    && Math.Abs(byTicket.WidthTenthsMm - Math.Round(widthMm * 10)) <= 4
                    && Math.Abs(byTicket.HeightTenthsMm - Math.Round(heightMm * 10)) <= 4)
                {
                    return byTicket;
                }
                throw new InvalidOperationException(
                    "The selected PrintTicket paper is absent from Windows DEVMODE capabilities: "
                    + pageId + "; converted paper=" + ticketPaperId + ".");
            }
            finally
            {
                Marshal.FreeHGlobal(names);
                Marshal.FreeHGlobal(sizes);
                Marshal.FreeHGlobal(papers);
            }
        }

        private static byte[] ResolvePrintTicketDevMode(string queueName, string pageId,
            double widthMm, double heightMm, bool? borderless,
            string orientation = null, int resolutionX = 0, int resolutionY = 0)
        {
            using (var server = new System.Printing.LocalPrintServer())
            using (var queue = server.GetPrintQueue(queueName))
            using (var stream = queue.GetPrintCapabilitiesAsXml())
            using (var converter = new System.Printing.Interop.PrintTicketConverter(queueName, 1))
            {
                var caps = new XmlDocument { XmlResolver = null };
                caps.Load(stream);
                var doc = CreatePageSelectionTicket(caps, pageId, widthMm, heightMm, borderless);
                AddOrientationAndResolution(doc, caps, orientation, resolutionX, resolutionY);
                using (var ticketStream = new MemoryStream())
                {
                    doc.Save(ticketStream);
                    ticketStream.Position = 0;
                    var ticket = new System.Printing.PrintTicket(ticketStream);
                    var mode = converter.ConvertPrintTicketToDevMode(ticket,
                        System.Printing.Interop.BaseDevModeType.UserDefault);
                    if (mode.Length < DevModePaperSizeOffset + sizeof(short))
                        throw new InvalidDataException("Windows returned a truncated converted DEVMODE.");
                    return mode;
                }
            }
        }

        private static XmlDocument CreatePageSelectionTicket(XmlDocument caps, string pageId,
            double widthMm, double heightMm, bool? borderless)
        {
            const string framework = "http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework";
            const string keywords = "http://schemas.microsoft.com/windows/2003/08/printing/printschemakeywords";
            var standard = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                { "A2", "ISOA2" },
                { "A3", "ISOA3" }, { "A4", "ISOA4" }, { "A5", "ISOA5" }, { "A6", "ISOA6" },
                { "B5", "JISB5" }, { "Letter", "NorthAmericaLetter" }, { "Legal", "NorthAmericaLegal" },
                { "ENV10", "NorthAmericaNumber10Envelope" }, { "ENVC6", "ISOC6Envelope" },
                { "Postcard", "JapanHagakiPostcard" },
            };
            var customName = "Page_" + new string(pageId.Select(c =>
                char.IsLetterOrDigit(c) || c == '_' || c == '.' || c == '-' ? c : '_').ToArray());
            string standardName;
            standard.TryGetValue(pageId, out standardName);
            var ns = new XmlNamespaceManager(caps.NameTable);
            ns.AddNamespace("psf", framework);
            var options = caps.SelectNodes("/psf:PrintCapabilities/psf:Feature/psf:Option", ns);
            var hasBorderless = caps.SelectNodes("/psf:PrintCapabilities/psf:Feature", ns)
                .Cast<XmlElement>().Any(feature => feature.GetAttribute("name").Split(':').Last() == "PageBorderless");
            XmlElement selected = null;
            foreach (XmlElement option in options)
            {
                var feature = (XmlElement)option.ParentNode;
                var featureParts = feature.GetAttribute("name").Split(':');
                if (featureParts.Last() != "PageMediaSize") continue;
                var parts = option.GetAttribute("name").Split(':');
                var local = parts.Last();
                if (local == customName) { selected = option; break; }
                if (selected == null && local == standardName) selected = option;
            }
            if (selected == null && borderless.HasValue && hasBorderless)
            {
                // Windows can merge equal-size custom page variants. The
                // separate PageBorderless feature preserves their meaning.
                foreach (XmlElement option in options)
                {
                    var feature = (XmlElement)option.ParentNode;
                    if (feature.GetAttribute("name").Split(':').Last() != "PageMediaSize") continue;
                    double? width = null, height = null;
                    foreach (XmlElement property in option.ChildNodes.OfType<XmlElement>())
                    {
                        if (property.LocalName != "ScoredProperty") continue;
                        double value;
                        if (!double.TryParse(property.InnerText, System.Globalization.NumberStyles.Integer,
                            System.Globalization.CultureInfo.InvariantCulture, out value)) continue;
                        var name = property.GetAttribute("name").Split(':').Last();
                        if (name == "MediaSizeWidth") width = value / 1000;
                        if (name == "MediaSizeHeight") height = value / 1000;
                    }
                    if (width.HasValue && height.HasValue && Math.Abs(width.Value - widthMm) <= 0.354
                        && Math.Abs(height.Value - heightMm) <= 0.354) { selected = option; break; }
                }
            }
            if (selected == null)
                throw new InvalidOperationException("Page size is absent from Windows PrintCapabilities: " + pageId + ".");
            var partsSelected = selected.GetAttribute("name").Split(':');
            var optionUri = selected.GetNamespaceOfPrefix(partsSelected.Length > 1 ? partsSelected[0] : "");
            var doc = new XmlDocument { XmlResolver = null };
            doc.LoadXml("<psf:PrintTicket xmlns:psf=\"" + framework + "\" xmlns:psk=\"" + keywords
                + "\" version=\"1\" />");
            var root = doc.DocumentElement;
            root.SetAttribute("xmlns:selected", optionUri);
            var ticketFeature = doc.CreateElement("psf", "Feature", framework);
            ticketFeature.SetAttribute("name", "psk:PageMediaSize");
            var ticketOption = doc.CreateElement("psf", "Option", framework);
            ticketOption.SetAttribute("name", "selected:" + partsSelected.Last());
            ticketFeature.AppendChild(ticketOption);
            root.AppendChild(ticketFeature);
            if (borderless.HasValue && hasBorderless)
            {
                var feature = doc.CreateElement("psf", "Feature", framework);
                feature.SetAttribute("name", "psk:PageBorderless");
                var option = doc.CreateElement("psf", "Option", framework);
                option.SetAttribute("name", borderless.Value ? "psk:Borderless" : "psk:None");
                feature.AppendChild(option);
                root.AppendChild(feature);
            }
            return doc;
        }

        private static void AddOrientationAndResolution(XmlDocument doc, XmlDocument caps,
            string orientation, int resolutionX, int resolutionY)
        {
            const string framework = "http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework";
            var root = doc.DocumentElement;
            if (!string.IsNullOrEmpty(orientation))
            {
                var feature = doc.CreateElement("psf", "Feature", framework);
                feature.SetAttribute("name", "psk:PageOrientation");
                var option = doc.CreateElement("psf", "Option", framework);
                option.SetAttribute("name", string.Equals(orientation, "landscape", StringComparison.OrdinalIgnoreCase)
                    ? "psk:Landscape" : "psk:Portrait");
                feature.AppendChild(option); root.AppendChild(feature);
            }
            if (resolutionX <= 0 || resolutionY <= 0) return;
            var ns = new XmlNamespaceManager(caps.NameTable);
            ns.AddNamespace("psf", framework);
            XmlElement selected = null;
            foreach (XmlElement option in caps.SelectNodes("/psf:PrintCapabilities/psf:Feature/psf:Option", ns))
            {
                if (((XmlElement)option.ParentNode).GetAttribute("name").Split(':').Last() != "PageResolution") continue;
                int x = 0, y = 0;
                foreach (XmlElement property in option.ChildNodes.OfType<XmlElement>())
                {
                    int value;
                    if (property.LocalName != "ScoredProperty" || !int.TryParse(property.InnerText, out value)) continue;
                    var name = property.GetAttribute("name").Split(':').Last();
                    if (name == "ResolutionX") x = value;
                    if (name == "ResolutionY") y = value;
                }
                if (x == resolutionX && y == resolutionY) { selected = option; break; }
            }
            if (selected == null) throw new InvalidOperationException("Requested raster resolution is absent from Windows PrintCapabilities.");
            var parts = selected.GetAttribute("name").Split(':');
            root.SetAttribute("xmlns:resolution", selected.GetNamespaceOfPrefix(parts.Length > 1 ? parts[0] : ""));
            root.SetAttribute("xmlns:xsd", "http://www.w3.org/2001/XMLSchema");
            root.SetAttribute("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance");
            var featureResolution = doc.CreateElement("psf", "Feature", framework);
            featureResolution.SetAttribute("name", "psk:PageResolution");
            var optionResolution = doc.CreateElement("psf", "Option", framework);
            optionResolution.SetAttribute("name", "resolution:" + parts.Last());
            foreach (var item in new[] { new { Name = "ResolutionX", Value = resolutionX }, new { Name = "ResolutionY", Value = resolutionY } })
            {
                var property = doc.CreateElement("psf", "ScoredProperty", framework);
                property.SetAttribute("name", "psk:" + item.Name);
                var value = doc.CreateElement("psf", "Value", framework);
                value.SetAttribute("type", "http://www.w3.org/2001/XMLSchema-instance", "xsd:integer");
                value.InnerText = item.Value.ToString(System.Globalization.CultureInfo.InvariantCulture);
                property.AppendChild(value); optionResolution.AppendChild(property);
            }
            featureResolution.AppendChild(optionResolution); root.AppendChild(featureResolution);
        }

        private sealed class PaperCapability
        {
            public short Id { get; set; }
            public int WidthTenthsMm { get; set; }
            public int HeightTenthsMm { get; set; }
            public string Name { get; set; }
        }

        public static List<QueueDescriptor> RepairStaleDuplicates()
        {
            var duplicateSlots = ListPrinters()
                .Where(queue => queue.IsPyToneRipSlot)
                .GroupBy(queue => queue.PortName, StringComparer.OrdinalIgnoreCase)
                .Where(group => group.Count() > 1)
                .ToArray();
            foreach (var slot in duplicateSlots)
            {
                var active = slot.FirstOrDefault(queue => !queue.IsPendingDeletion);
                if (active == null || !slot.Any(queue => queue.IsPendingDeletion))
                    continue;
                PurgeStaleSlotDuplicates(active);
                WaitForStaleSlotDuplicates(active.PortName);
            }
            return ListPrinters();
        }

        private static void PurgeStaleSlotDuplicates(QueueDescriptor target)
        {
            var staleQueues = ListPrinters().Where(queue =>
                queue.IsPendingDeletion
                && queue.IsPyToneRipSlot
                && string.Equals(queue.PortName, target.PortName, StringComparison.OrdinalIgnoreCase)
                && !string.Equals(queue.Name, target.Name, StringComparison.OrdinalIgnoreCase))
                .ToArray();
            foreach (var stale in staleQueues)
            {
                var defaults = new PrinterDefaults
                {
                    DesiredAccess = PrinterAccessAdminister | PrinterAccessUse,
                };
                IntPtr handle;
                if (!OpenPrinterW(stale.Name, out handle, ref defaults))
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "Could not open the stale pyToneRIP queue for cleanup.");
                try
                {
                    if (!SetPrinterW(handle, 0, IntPtr.Zero, PrinterControlPurge))
                        throw new Win32Exception(
                            Marshal.GetLastWin32Error(),
                            "Could not purge retained jobs from the stale pyToneRIP queue.");
                }
                finally
                {
                    ClosePrinter(handle);
                }
            }
        }

        private static List<QueueDescriptor> WaitForStaleSlotDuplicates(string portName)
        {
            for (var attempt = 0; attempt < 50; attempt++)
            {
                var queues = ListPrinters();
                if (!queues.Any(queue => queue.IsPendingDeletion
                    && queue.IsPyToneRipSlot
                    && string.Equals(queue.PortName, portName, StringComparison.OrdinalIgnoreCase)))
                    return queues;
                System.Threading.Thread.Sleep(100);
            }
            throw new InvalidOperationException(
                "Windows is still removing an old pyToneRIP queue. Close all print dialogs "
                + "and restart Windows before assigning this slot.");
        }

        private static QueueDescriptor ToDescriptor(PrinterInfo2 info) => new QueueDescriptor
        {
            Name = Marshal.PtrToStringUni(info.PrinterName) ?? "",
            PortName = Marshal.PtrToStringUni(info.PortName) ?? "",
            DriverName = Marshal.PtrToStringUni(info.DriverName) ?? "",
            PrintProcessor = Marshal.PtrToStringUni(info.PrintProcessor) ?? "",
            DataType = Marshal.PtrToStringUni(info.DataType) ?? "",
            RawUsbReady = string.Equals(Marshal.PtrToStringUni(info.PrintProcessor), "winprint", StringComparison.OrdinalIgnoreCase)
                && string.Equals(Marshal.PtrToStringUni(info.DataType), "RAW", StringComparison.OrdinalIgnoreCase)
                && string.IsNullOrEmpty(Marshal.PtrToStringUni(info.SeparatorFile)) && (info.Attributes & 2) == 0,
            Status = (info.Attributes & EpsonRawQueueMatcher.WorkOfflineAttribute) != 0
                ? info.Status | EpsonRawQueueMatcher.OfflineStatus : info.Status,
            Jobs = info.Jobs,
        };

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct PrinterInfo2
        {
            public IntPtr ServerName;
            public IntPtr PrinterName;
            public IntPtr ShareName;
            public IntPtr PortName;
            public IntPtr DriverName;
            public IntPtr Comment;
            public IntPtr Location;
            public IntPtr DevMode;
            public IntPtr SeparatorFile;
            public IntPtr PrintProcessor;
            public IntPtr DataType;
            public IntPtr Parameters;
            public IntPtr SecurityDescriptor;
            public uint Attributes;
            public uint Priority;
            public uint DefaultPriority;
            public uint StartTime;
            public uint UntilTime;
            public uint Status;
            public uint Jobs;
            public uint AveragePagesPerMinute;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct PrinterDefaults
        {
            public IntPtr DataType;
            public IntPtr DevMode;
            public uint DesiredAccess;
        }

        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool EnumPrintersW(
            uint flags,
            string name,
            uint level,
            IntPtr printerEnum,
            uint bufferSize,
            ref uint needed,
            ref uint returned);

        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool OpenPrinterW(string printerName, out IntPtr printer, ref PrinterDefaults defaults);

        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool GetPrinterW(IntPtr printer, uint level, IntPtr buffer, uint bufferSize, ref uint needed);

        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool SetPrinterW(IntPtr printer, uint level, IntPtr buffer, uint command);

        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern int DocumentPropertiesW(
            IntPtr window,
            IntPtr printer,
            string deviceName,
            IntPtr devModeOutput,
            IntPtr devModeInput,
            int mode);

        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern int DeviceCapabilitiesW(
            string deviceName,
            string port,
            short capability,
            IntPtr output,
            IntPtr devMode);

        [DllImport("winspool.drv", SetLastError = true)]
        private static extern bool ClosePrinter(IntPtr printer);

        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool GetDefaultPrinterW(StringBuilder printerName, ref uint bufferSize);

        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool SetDefaultPrinterW(string printerName);

        private const uint ShellChangeAssociationChanged = 0x08000000;
        private const uint ShellChangeNotifyIdList = 0x0000;

        [DllImport("shell32.dll")]
        private static extern void SHChangeNotify(
            uint eventId,
            uint flags,
            IntPtr item1,
            IntPtr item2);
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
        [DataMember(Name = "module_package_path", Order = 11)] public string ModulePackagePath { get; set; }
        [DataMember(Name = "module_dependency_paths", Order = 12)] public List<string> ModuleDependencyPaths { get; set; }
        [DataMember(Name = "module_package_identity", Order = 13)] public string ModulePackageIdentity { get; set; }
        [DataMember(Name = "module_package_version", Order = 14)] public string ModulePackageVersion { get; set; }
        [DataMember(Name = "maintenance_json", Order = 15)] public string MaintenanceJson { get; set; }
        [DataMember(Name = "manager_log", Order = 16)] public string ManagerLog { get; set; }
        [DataMember(Name = "usb_device_id", Order = 17)] public string UsbDeviceId { get; set; }
        [DataMember(Name = "job_id", Order = 18)] public string JobId { get; set; }
        [DataMember(Name = "driver_name", Order = 19, EmitDefaultValue = false)] public string DriverName { get; set; }
    }

    [DataContract]
    internal sealed class QueueHelperResponse
    {
        [DataMember(Name = "success", Order = 1)] public bool Success { get; set; }
        [DataMember(Name = "error_code", Order = 2)] public int ErrorCode { get; set; }
        [DataMember(Name = "error_message", Order = 3, EmitDefaultValue = false)] public string ErrorMessage { get; set; }
        [DataMember(Name = "queues", Order = 4)] public List<QueueDescriptor> Queues { get; set; }
        [DataMember(Name = "report_path", Order = 5, EmitDefaultValue = false)] public string ReportPath { get; set; }
        [DataMember(Name = "usb_printers", Order = 6, EmitDefaultValue = false)] public List<UsbPrinterDescriptor> UsbPrinters { get; set; }
        [DataMember(Name = "legacy_defaults", Order = 7, EmitDefaultValue = false)] public LegacyPrinterDefaults LegacyDefaults { get; set; }
        [DataMember(Name = "usb_ink_levels", Order = 8, EmitDefaultValue = false)] public List<UsbInkLevel> UsbInkLevels { get; set; }
        [DataMember(Name = "configuration_ticket_xml", Order = 9, EmitDefaultValue = false)] public string ConfigurationTicketXml { get; set; }
        [DataMember(Name = "printer_information", Order = 10, EmitDefaultValue = false)] public PrinterInformationReport PrinterInformation { get; set; }
        [DataMember(Name = "maintenance_transport", Order = 11, EmitDefaultValue = false)] public string MaintenanceTransport { get; set; }

        public static QueueHelperResponse Pass(List<QueueDescriptor> queues) => new QueueHelperResponse
        {
            Success = true,
            Queues = queues ?? new List<QueueDescriptor>(),
        };

        public static QueueHelperResponse Fail(int code, string message) => new QueueHelperResponse
        {
            Success = false,
            ErrorCode = code,
            ErrorMessage = message,
            Queues = new List<QueueDescriptor>(),
        };
    }

    [DataContract]
    internal sealed class LegacyPrinterDefaults
    {
        [DataMember(Name = "print_ticket_xml", Order = 7, EmitDefaultValue = false)] public string PrintTicketXml { get; set; }
        [DataMember(Name = "has_user_devmode", Order = 8)] public bool HasUserDevMode { get; set; }
        [DataMember(Name = "page_width_mm", Order = 1)] public double PageWidthMm { get; set; }
        [DataMember(Name = "page_height_mm", Order = 2)] public double PageHeightMm { get; set; }
        [DataMember(Name = "borderless", Order = 3, EmitDefaultValue = false)] public bool? Borderless { get; set; }
        [DataMember(Name = "orientation", Order = 4)] public string Orientation { get; set; }
        [DataMember(Name = "resolution_x", Order = 5, EmitDefaultValue = false)] public int? ResolutionX { get; set; }
        [DataMember(Name = "resolution_y", Order = 6, EmitDefaultValue = false)] public int? ResolutionY { get; set; }
    }

    [DataContract]
    internal sealed class QueueDescriptor
    {
        [DataMember(Name = "name", Order = 1)] public string Name { get; set; }
        [DataMember(Name = "port_name", Order = 2)] public string PortName { get; set; }
        [DataMember(Name = "driver_name", Order = 3)] public string DriverName { get; set; }
        [DataMember(Name = "status", Order = 4)] public uint Status { get; set; }
        [DataMember(Name = "jobs", Order = 5)] public uint Jobs { get; set; }
        [DataMember(Name = "is_default", Order = 6)] public bool IsDefault { get; set; }
        public bool IsPendingDeletion => (Status & PrinterApi.PendingDeletionStatus) != 0;
        public string PrintProcessor { get; set; }
        public string DataType { get; set; }
        [DataMember(Name = "raw_ready", Order = 7)] public bool RawUsbReady { get; set; }
        public bool IsEpsonRawDestination => EpsonRawQueueMatcher.IsCompatible(
            Name, PortName, DriverName, RawUsbReady, Status);
        public bool IsPyToneRipSlot => PortName != null && PortName.StartsWith(
            "pyToneRIP.PSA_", StringComparison.OrdinalIgnoreCase);
    }
}
