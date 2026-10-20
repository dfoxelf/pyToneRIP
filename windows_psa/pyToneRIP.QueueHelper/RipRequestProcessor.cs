using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Reflection;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace pyToneRIP.QueueHelper
{
    internal static class RipRequestProcessor
    {
        private const string FolderName = "rip-runtime";
        private const string RequestSuffix = ".request.json";
        private const string ProcessingSuffix = ".processing.json";
        private const string ResponseSchema = "pytone-rip.runtime-response";
        private const string CancellationFileName = "cancel.requested";
        private const string SchedulerMutexName = "Local\\pyToneRIP.RipScheduler";

        public static void ValidateRuntime(string localState)
        {
            using (var mutex = new Mutex(false, "Local\\pyToneRIP.Phase12.RuntimeSelfTest"))
            {
                var ownsMutex = false;
                try
                {
                    try
                    {
                        ownsMutex = mutex.WaitOne(TimeSpan.FromMinutes(2));
                    }
                    catch (AbandonedMutexException)
                    {
                        ownsMutex = true;
                    }
                    if (!ownsMutex)
                        throw new TimeoutException("Another pyToneRIP runtime self-test did not finish.");

                    using (var process = StartRuntime(localState, "--self-test"))
                    {
                        if (!process.WaitForExit((int)TimeSpan.FromMinutes(2).TotalMilliseconds))
                        {
                            try { process.Kill(); } catch { }
                            throw new TimeoutException("Packaged Python RIP self-test exceeded the 2 minute limit.");
                        }
                        if (process.ExitCode != 0)
                            throw new InvalidOperationException(
                                "Packaged Python RIP self-test exited with code " + process.ExitCode + ".");
                    }
                }
                finally
                {
                    if (ownsMutex)
                        mutex.ReleaseMutex();
                }
            }
        }

        public static void RunMaintenance(string localState, string requestJson)
        {
            if (string.IsNullOrWhiteSpace(requestJson) || requestJson.Length > 1024 * 1024)
                throw new InvalidDataException("Maintenance request JSON is missing or too large.");
            MaintenanceBrokerRequest request;
            using (var stream = new MemoryStream(Encoding.UTF8.GetBytes(requestJson)))
                request = (MaintenanceBrokerRequest)new DataContractJsonSerializer(
                    typeof(MaintenanceBrokerRequest)).ReadObject(stream);
            IPAddress address;
            if (request == null
                || request.Schema != "pytone-rip.maintenance-request"
                || request.Version != 1
                || !IPAddress.TryParse((request.Host ?? "").Trim(), out address)
                || request.Port < 1
                || request.Port > 65535)
                throw new InvalidDataException("Maintenance request endpoint is invalid.");

            var printerKey = "network|" + address + "|" + request.Port;
            using (var printerMutex = new Mutex(false, PhysicalPrinterMutexName(printerKey)))
            {
                var ownsPrinter = false;
                try
                {
                    try
                    {
                        ownsPrinter = printerMutex.WaitOne(0);
                    }
                    catch (AbandonedMutexException)
                    {
                        ownsPrinter = true;
                    }
                    if (!ownsPrinter)
                        throw new InvalidOperationException(
                            "The printer is busy with another RIP or maintenance job.");

                    var folder = Path.Combine(localState, "maintenance");
                    Directory.CreateDirectory(folder);
                    var requestPath = Path.Combine(folder, Guid.NewGuid().ToString("N") + ".json");
                    var errorPath = Path.ChangeExtension(requestPath, ".error.txt");
                    try
                    {
                        File.WriteAllText(requestPath, requestJson, new UTF8Encoding(false));
                        using (var process = StartRuntime(
                            localState, "--maintenance-request " + Quote(requestPath)))
                        {
                            if (!process.WaitForExit(
                                (int)TimeSpan.FromMinutes(10).TotalMilliseconds))
                            {
                                try { process.Kill(); } catch { }
                                throw new TimeoutException(
                                    "Printer maintenance exceeded the 10 minute limit.");
                            }
                            if (process.ExitCode != 0)
                            {
                                var detail = File.Exists(errorPath)
                                    ? File.ReadAllText(errorPath).Trim()
                                    : "Packaged Python RIP exited with code " + process.ExitCode + ".";
                                throw new InvalidOperationException(detail);
                            }
                        }
                    }
                    finally
                    {
                        try { File.Delete(requestPath); } catch { }
                        try { File.Delete(errorPath); } catch { }
                    }
                }
                finally
                {
                    if (ownsPrinter)
                        printerMutex.ReleaseMutex();
                }
            }
        }

        public static void ProcessPending(string localState)
        {
            var folder = Path.Combine(localState, FolderName);
            Directory.CreateDirectory(folder);
            using (var cancellationSweep = new PendingCancellationSweep(folder, localState))
            {
                while (true)
                {
                    using (var claimed = TryClaimNextRunnable(folder))
                    {
                        if (claimed != null)
                        {
                            ProcessOne(claimed, localState);
                            continue;
                        }
                    }

                    // A request for a busy printer remains queued while a request
                    // for another printer can be claimed by this or another helper.
                    if (!Directory.GetFiles(folder, "*" + RequestSuffix).Any())
                        return;
                    Thread.Sleep(200);
                }
            }
        }

        // The per-printer mutex must serialize rendering, but must not serialize
        // cancellation.  Observe pending Windows jobs even while ProcessOne is
        // waiting for a previous job's socket to finish or time out.
        private sealed class PendingCancellationSweep : IDisposable
        {
            private readonly string folder;
            private readonly string localState;
            private readonly Thread thread;
            private volatile bool stopping;
            private volatile bool processingCancellation;

            public PendingCancellationSweep(string folder, string localState)
            {
                this.folder = folder;
                this.localState = localState;
                thread = new Thread(Run) { IsBackground = true, Name = "RIP pending cancellation" };
                thread.Start();
            }

            private void Run()
            {
                var monitors = new Dictionary<string, PrintQueueCancellationMonitor>(
                    StringComparer.OrdinalIgnoreCase);
                while (!stopping)
                {
                    try
                    {
                        var pending = Directory.GetFiles(folder, "*" + RequestSuffix);
                        var active = new HashSet<string>(pending, StringComparer.OrdinalIgnoreCase);
                        foreach (var path in monitors.Keys.Where(key => !active.Contains(key)).ToArray())
                            monitors.Remove(path);
                        foreach (var path in pending)
                        {
                            if (stopping)
                                break;
                            try
                            {
                                var candidate = ReadQueueCandidate(path);
                                if (candidate.Request == null)
                                    continue;
                                var jobFolder = Path.GetDirectoryName(candidate.Request.JobRecord);
                                var cancellationPath = Path.Combine(jobFolder, CancellationFileName);
                                if (!monitors.TryGetValue(path, out var monitor))
                                {
                                    monitor = PrintQueueCancellationMonitor.Open(
                                        candidate.Request.JobRecord, cancellationPath, localState);
                                    monitors.Add(path, monitor);
                                }
                                monitor.Poll();
                                if (stopping || !File.Exists(cancellationPath))
                                    continue;
                                var claimedPath = Path.Combine(
                                    folder, candidate.RequestId + ProcessingSuffix);
                                File.Move(path, claimedPath);
                                monitors.Remove(path);
                                processingCancellation = true;
                                try
                                {
                                    using (var claimed = new ClaimedRipRequest(
                                        candidate.RequestId, claimedPath, candidate.Request, null))
                                        ProcessOne(claimed, localState);
                                }
                                finally { processingCancellation = false; }
                            }
                            catch (IOException)
                            {
                                // Another helper may have claimed this request.
                                monitors.Remove(path);
                            }
                            catch (Exception error)
                            {
                                Program.Log(localState, "Pending cancellation sweep for "
                                    + Path.GetFileName(path) + " failed: " + error.Message);
                                monitors.Remove(path);
                            }
                        }
                    }
                    catch (Exception error)
                    {
                        Program.Log(localState, "Pending cancellation sweep failed: " + error.Message);
                    }
                    Thread.Sleep(500);
                }
            }

            public void Dispose()
            {
                stopping = true;
                if (Thread.CurrentThread != thread)
                {
                    thread.Join(1000);
                    if (processingCancellation)
                        thread.Join();
                }
            }
        }

        private static ClaimedRipRequest TryClaimNextRunnable(string folder)
        {
            using (var scheduler = new Mutex(false, SchedulerMutexName))
            {
                var ownsScheduler = false;
                try
                {
                    try
                    {
                        ownsScheduler = scheduler.WaitOne(TimeSpan.FromSeconds(30));
                    }
                    catch (AbandonedMutexException)
                    {
                        ownsScheduler = true;
                    }
                    if (!ownsScheduler)
                        throw new TimeoutException(
                            "pyToneRIP scheduler did not release its short claim lock.");

                    var candidates = Directory.GetFiles(folder, "*" + RequestSuffix)
                        .Select(ReadQueueCandidate)
                        .OrderBy(value => value.EnqueuedUtc)
                        .ThenBy(value => value.RequestId, StringComparer.OrdinalIgnoreCase)
                        .ToArray();
                    var firstPendingPerPrinter = new HashSet<string>(
                        StringComparer.OrdinalIgnoreCase);
                    foreach (var candidate in candidates)
                    {
                        // Only the oldest pending job for one physical endpoint
                        // may compete for its mutex.  Later jobs cannot overtake it.
                        if (!firstPendingPerPrinter.Add(candidate.PrinterKey))
                            continue;

                        var printerMutex = new Mutex(
                            false, PhysicalPrinterMutexName(candidate.PrinterKey));
                        var ownsPrinter = false;
                        try
                        {
                            try
                            {
                                ownsPrinter = printerMutex.WaitOne(0);
                            }
                            catch (AbandonedMutexException)
                            {
                                ownsPrinter = true;
                            }
                            if (!ownsPrinter)
                            {
                                printerMutex.Dispose();
                                continue;
                            }

                            var claimedPath = Path.Combine(
                                folder, candidate.RequestId + ProcessingSuffix);
                            File.Move(candidate.RequestPath, claimedPath);
                            return new ClaimedRipRequest(
                                candidate.RequestId,
                                claimedPath,
                                candidate.Request,
                                printerMutex);
                        }
                        catch
                        {
                            if (ownsPrinter)
                                printerMutex.ReleaseMutex();
                            printerMutex.Dispose();
                            throw;
                        }
                    }
                    return null;
                }
                finally
                {
                    if (ownsScheduler)
                        scheduler.ReleaseMutex();
                }
            }
        }

        private static QueueCandidate ReadQueueCandidate(string requestPath)
        {
            var requestId = Path.GetFileName(requestPath).Substring(
                0, Path.GetFileName(requestPath).Length - RequestSuffix.Length);
            try
            {
                var request = ReadJson<RipBrokerRequest>(requestPath);
                DateTimeOffset enqueuedUtc;
                if (request == null
                    || string.IsNullOrWhiteSpace(request.PrinterKey)
                    || !DateTimeOffset.TryParse(
                        request.EnqueuedUtc,
                        CultureInfo.InvariantCulture,
                        DateTimeStyles.RoundtripKind,
                        out enqueuedUtc))
                    throw new InvalidDataException("Invalid RIP queue metadata.");
                return new QueueCandidate(
                    requestPath, requestId, request.PrinterKey, enqueuedUtc, request);
            }
            catch
            {
                // Malformed requests still need a terminal error response.  Give
                // each one an isolated scheduling key and let ProcessOne report
                // the actual validation failure after it is atomically claimed.
                return new QueueCandidate(
                    requestPath,
                    requestId,
                    "invalid|" + requestId,
                    File.GetCreationTimeUtc(requestPath),
                    null);
            }
        }

        private static void ProcessOne(ClaimedRipRequest claimed, string localState)
        {
            var token = claimed.RequestId;
            var claimedPath = claimed.ClaimedPath;
            RipBrokerResponse response;
            RipBrokerRequest request = claimed.Request;
            var requestValidated = false;
            Program.Log(
                localState,
                "RIP worker acquired request " + token
                + "; printer=" + (request == null ? "unknown" : request.PrinterKey)
                + "; pid=" + Process.GetCurrentProcess().Id + ".");
            try
            {
                if (request == null)
                    request = ReadJson<RipBrokerRequest>(claimedPath);
                ValidateRequest(request, token, claimedPath, localState);
                requestValidated = true;
                var jobFolder = Path.GetDirectoryName(request.JobRecord);
                var cancellationPath = Path.Combine(jobFolder, CancellationFileName);
                ThrowIfCancellationRequested(cancellationPath);
                PrintQueueCancellationMonitor queueCancellation = null;
                try
                {
                    queueCancellation = PrintQueueCancellationMonitor.Open(
                        request.JobRecord, cancellationPath, localState);
                    queueCancellation.Poll();
                }
                catch (Exception error)
                {
                    Program.Log(localState, "Windows queue cancellation monitor unavailable for "
                        + request.JobId + ": " + error.Message);
                }
                ThrowIfCancellationRequested(cancellationPath);
                using (var process = StartRuntime(localState, "--request " + Quote(claimedPath)))
                {
                    var cancellationObserved = WaitForRuntimeExit(
                        process, cancellationPath, queueCancellation);
                    if (process.ExitCode != 0)
                    {
                        if (cancellationObserved || File.Exists(cancellationPath))
                            throw new OperationCanceledException("Windows print job was canceled.");
                        throw new InvalidOperationException(
                            "Packaged Python RIP exited with code " + process.ExitCode + ".");
                    }
                }

                var renderResultPath = Path.Combine(
                    jobFolder, "render-result.json");
                RipRenderResult result;
                try
                {
                    result = ReadJson<RipRenderResult>(renderResultPath);
                }
                catch (Exception) when (File.Exists(cancellationPath))
                {
                    throw new OperationCanceledException("Windows print job was canceled.");
                }
                if (result == null ||
                    result.Schema != "pytone-rip.render-result" ||
                    result.Version != 1 ||
                    result.JobId != request.JobId ||
                    result.Status != "SENT" ||
                    !result.TransportAttempted)
                {
                    if (File.Exists(cancellationPath))
                        throw new OperationCanceledException("Windows print job was canceled.");
                    throw new InvalidDataException("Packaged RIP render-result failed validation.");
                }
                // A marker arriving after the completed transport is too late
                // to undo the physical job; preserve the durable SENT outcome.
                response = RipBrokerResponse.Pass(request);
            }
            catch (OperationCanceledException error)
            {
                response = RipBrokerResponse.Cancelled(
                    request == null ? token : request.RequestId,
                    request == null ? "unknown" : request.JobId,
                    error.Message);
                if (request != null)
                    WriteTerminalProgress(
                        Path.GetDirectoryName(request.JobRecord), request.JobId,
                        "CANCELLED", error.Message);
                Program.Log(localState, "RIP request " + token + " canceled: " + error.Message);
            }
            catch (Exception error)
            {
                if (request == null)
                    try { request = ReadJson<RipBrokerRequest>(claimedPath); } catch { }
                response = RipBrokerResponse.Fail(
                    request == null ? token : request.RequestId,
                    request == null ? "unknown" : request.JobId,
                    error);
                if (requestValidated)
                    WriteTerminalProgress(
                        Path.GetDirectoryName(request.JobRecord), request.JobId,
                        "FAILED", error.GetType().Name + ": " + error.Message);
                Program.Log(localState, "RIP request " + token + " failed: " + error);
            }
            finally
            {
                try { File.Delete(claimedPath); } catch { }
            }
            WriteJsonAtomically(
                Path.Combine(Path.GetDirectoryName(claimedPath), token + ".response.json"),
                response);
            Program.Log(
                localState,
                "RIP worker completed request " + token
                + "; status=" + response.Status
                + "; printer=" + (request == null ? "unknown" : request.PrinterKey)
                + "; pid=" + Process.GetCurrentProcess().Id + ".");
        }

        private static bool WaitForRuntimeExit(
            Process process, string cancellationPath,
            PrintQueueCancellationMonitor queueCancellation)
        {
            var deadline = DateTime.UtcNow.AddMinutes(30);
            while (!process.WaitForExit(200))
            {
                queueCancellation?.Poll();
                if (File.Exists(cancellationPath))
                {
                    // Let the RIP observe its marker, stop the socket writer,
                    // and attempt ESC/P2 reset before forcing termination.
                    if (process.WaitForExit(15000))
                    {
                        UsbRawJobCleanup.TryCancel(Path.GetDirectoryName(cancellationPath));
                        return true;
                    }
                    try { process.Kill(); } catch { }
                    process.WaitForExit(5000);
                    UsbRawJobCleanup.TryCancel(Path.GetDirectoryName(cancellationPath));
                    throw new OperationCanceledException("Windows print job was canceled.");
                }
                if (DateTime.UtcNow >= deadline)
                {
                    try { process.Kill(); } catch { }
                    process.WaitForExit(5000);
                    UsbRawJobCleanup.TryCancel(Path.GetDirectoryName(cancellationPath));
                    throw new TimeoutException("Packaged Python RIP exceeded the 30 minute limit.");
                }
            }
            if (process.ExitCode != 0 || File.Exists(cancellationPath))
                UsbRawJobCleanup.TryCancel(Path.GetDirectoryName(cancellationPath));
            return File.Exists(cancellationPath);
        }

        private static void ThrowIfCancellationRequested(string cancellationPath)
        {
            if (File.Exists(cancellationPath))
                throw new OperationCanceledException("Windows print job was canceled.");
        }

        private static void WriteTerminalProgress(
            string jobFolder, string jobId, string status, string message)
        {
            try
            {
                var progressPath = Path.Combine(jobFolder, "progress.json");
                if (File.Exists(progressPath))
                {
                    try
                    {
                        if (ReadJson<RipJobProgress>(progressPath)?.Terminal == true)
                            return;
                    }
                    catch (IOException) { }
                    catch (SerializationException) { }
                }
                File.AppendAllText(
                    Path.Combine(jobFolder, "progress-history.log"),
                    DateTimeOffset.UtcNow.ToString("O") + " | 100% | " + status + " | "
                    + message.Replace("\r", " ").Replace("\n", " ")
                    + Environment.NewLine);
                WriteJsonAtomically(
                    progressPath,
                    new RipJobProgress
                    {
                        JobId = jobId,
                        Status = status,
                        Stage = status,
                        Percent = 100,
                        Message = message,
                        UpdatedUtc = DateTimeOffset.UtcNow.ToString("O"),
                        Terminal = true,
                    });
            }
            catch
            {
                // Cancellation must still terminate the runtime even if a UI-only
                // progress snapshot is temporarily locked by the Manager.
            }
        }

        private static Process StartRuntime(string localState, string arguments)
        {
            var runtime = Path.Combine(
                Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location),
                "Runtime",
                "pyToneRIP.Runtime.exe");
            if (!File.Exists(runtime))
                throw new FileNotFoundException("Packaged Python RIP executable is missing.", runtime);

            var startInfo = new ProcessStartInfo
            {
                FileName = runtime,
                Arguments = arguments,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                WorkingDirectory = Path.GetDirectoryName(runtime),
            };
            var process = Process.Start(startInfo);
            if (process == null)
                throw new InvalidOperationException("Packaged Python RIP process did not start.");
            return process;
        }

        private static void ValidateRequest(
            RipBrokerRequest request,
            string token,
            string claimedPath,
            string localState)
        {
            if (request == null ||
                request.Schema != "pytone-rip.runtime-request" ||
                request.Version != 2 ||
                request.RequestId != token ||
                string.IsNullOrWhiteSpace(request.JobId) ||
                string.IsNullOrWhiteSpace(request.PrinterKey) ||
                string.IsNullOrWhiteSpace(request.EnqueuedUtc))
                throw new InvalidDataException("Unsupported packaged RIP request.");
            DateTimeOffset enqueuedUtc;
            if (!DateTimeOffset.TryParse(
                request.EnqueuedUtc,
                CultureInfo.InvariantCulture,
                DateTimeStyles.RoundtripKind,
                out enqueuedUtc))
                throw new InvalidDataException("RIP request enqueue timestamp is invalid.");
            var expectedLocalRecord = Path.GetFullPath(Path.Combine(
                localState, "jobs", request.JobId, "job-record.json"));
            var expectedSharedRecord = Path.GetFullPath(Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Publishers", "qg4mjaqw0qctt", "pyToneRIP.Shared",
                "jobs", request.JobId, "job-record.json"));
            var actualRecord = Path.GetFullPath(request.JobRecord);
            if ((!string.Equals(expectedLocalRecord, actualRecord,
                     StringComparison.OrdinalIgnoreCase)
                    && !string.Equals(expectedSharedRecord, actualRecord,
                        StringComparison.OrdinalIgnoreCase))
                || !File.Exists(actualRecord))
                throw new InvalidDataException(
                    "RIP job record is outside package LocalState/shared publisher jobs.");
            var expectedPrinterKey = ReadPhysicalPrinterKey(
                Path.GetDirectoryName(actualRecord));
            if (!string.Equals(
                request.PrinterKey,
                expectedPrinterKey,
                StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException(
                    "RIP request physical printer key does not match its configuration snapshot.");
            if (claimedPath.IndexOf('"') >= 0)
                throw new InvalidDataException("RIP request path contains an invalid quote.");
        }

        private static string ReadPhysicalPrinterKey(string jobFolder)
        {
            var configuration = ReadJson<PhysicalPrinterConfiguration>(
                Path.Combine(jobFolder, "configuration.json"));
            var connection = configuration == null ? null : configuration.Connection;
            var kind = connection == null
                ? ""
                : (connection.Kind ?? "").Trim().ToLowerInvariant();
            if (kind == "network")
            {
                IPAddress address;
                if (!IPAddress.TryParse((connection.Host ?? "").Trim(), out address))
                    throw new InvalidDataException("RIP network address is invalid.");
                if (connection.Port < 1 || connection.Port > 65535)
                    throw new InvalidDataException("RIP network port is invalid.");
                return "network|" + address + "|" + connection.Port;
            }
            if (kind == "usb")
            {
                var deviceId = (connection.UsbDeviceId ?? "").Trim();
                if (string.IsNullOrWhiteSpace(deviceId))
                    throw new InvalidDataException("RIP USB device identity is missing.");
                return "usb|" + deviceId.ToUpperInvariant();
            }
            throw new InvalidDataException("RIP connection kind is invalid.");
        }

        private static string PhysicalPrinterMutexName(string printerKey)
        {
            byte[] digest;
            using (var sha = SHA256.Create())
                digest = sha.ComputeHash(Encoding.UTF8.GetBytes(printerKey));
            var token = BitConverter.ToString(digest).Replace("-", "").Substring(0, 32);
            return "Local\\pyToneRIP.PhysicalPrinter." + token;
        }

        private static string Quote(string value) => "\"" + value + "\"";

        private static T ReadJson<T>(string path)
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                return (T)new DataContractJsonSerializer(typeof(T)).ReadObject(stream);
        }

        private static void WriteJsonAtomically<T>(string path, T value)
        {
            var temporary = path + ".tmp";
            using (var stream = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None))
                new DataContractJsonSerializer(typeof(T)).WriteObject(stream, value);
            if (File.Exists(path)) File.Delete(path);
            File.Move(temporary, path);
        }
    }

    internal sealed class QueueCandidate
    {
        public string RequestPath { get; private set; }
        public string RequestId { get; private set; }
        public string PrinterKey { get; private set; }
        public DateTimeOffset EnqueuedUtc { get; private set; }
        public RipBrokerRequest Request { get; private set; }

        public QueueCandidate(
            string requestPath,
            string requestId,
            string printerKey,
            DateTimeOffset enqueuedUtc,
            RipBrokerRequest request)
        {
            RequestPath = requestPath;
            RequestId = requestId;
            PrinterKey = printerKey;
            EnqueuedUtc = enqueuedUtc;
            Request = request;
        }
    }

    internal sealed class ClaimedRipRequest : IDisposable
    {
        private Mutex printerMutex;

        public string RequestId { get; private set; }
        public string ClaimedPath { get; private set; }
        public RipBrokerRequest Request { get; private set; }

        public ClaimedRipRequest(
            string requestId,
            string claimedPath,
            RipBrokerRequest request,
            Mutex printerMutex)
        {
            RequestId = requestId;
            ClaimedPath = claimedPath;
            Request = request;
            this.printerMutex = printerMutex;
        }

        public void Dispose()
        {
            var current = Interlocked.Exchange(ref printerMutex, null);
            if (current == null)
                return;
            try
            {
                current.ReleaseMutex();
            }
            finally
            {
                current.Dispose();
            }
        }
    }

    [DataContract]
    internal sealed class MaintenanceBrokerRequest
    {
        [DataMember(Name = "schema", Order = 1)] public string Schema { get; set; }
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; }
        [DataMember(Name = "host", Order = 3)] public string Host { get; set; }
        [DataMember(Name = "port", Order = 4)] public int Port { get; set; }
    }

    [DataContract]
    internal sealed class RipBrokerRequest
    {
        [DataMember(Name = "schema", Order = 1)] public string Schema { get; set; }
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; }
        [DataMember(Name = "request_id", Order = 3)] public string RequestId { get; set; }
        [DataMember(Name = "job_id", Order = 4)] public string JobId { get; set; }
        [DataMember(Name = "job_record", Order = 5)] public string JobRecord { get; set; }
        [DataMember(Name = "printer_key", Order = 6)] public string PrinterKey { get; set; }
        [DataMember(Name = "enqueued_utc", Order = 7)] public string EnqueuedUtc { get; set; }
    }

    [DataContract]
    internal sealed class PhysicalPrinterConfiguration
    {
        [DataMember(Name = "connection")] public PhysicalPrinterConnection Connection { get; set; }
    }

    [DataContract]
    internal sealed class PhysicalPrinterConnection
    {
        [DataMember(Name = "kind")] public string Kind { get; set; }
        [DataMember(Name = "host")] public string Host { get; set; }
        [DataMember(Name = "port")] public int Port { get; set; }
        [DataMember(Name = "usb_port")] public string UsbPort { get; set; }
        [DataMember(Name = "usb_device_id")] public string UsbDeviceId { get; set; }
        [DataMember(Name = "printer_queue")] public string PhysicalQueueName { get; set; }
    }

    [DataContract]
    internal sealed class RipRenderResult
    {
        [DataMember(Name = "schema", Order = 1)] public string Schema { get; set; }
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; }
        [DataMember(Name = "job_id", Order = 3)] public string JobId { get; set; }
        [DataMember(Name = "status", Order = 4)] public string Status { get; set; }
        [DataMember(Name = "transport_attempted", Order = 5)] public bool TransportAttempted { get; set; }
    }

    [DataContract]
    internal sealed class RipBrokerResponse
    {
        [DataMember(Name = "schema", Order = 1)] public string Schema { get; set; } = ResponseSchemaValue;
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; } = 1;
        [DataMember(Name = "request_id", Order = 3)] public string RequestId { get; set; }
        [DataMember(Name = "job_id", Order = 4)] public string JobId { get; set; }
        [DataMember(Name = "success", Order = 5)] public bool Success { get; set; }
        [DataMember(Name = "status", Order = 6)] public string Status { get; set; }
        [DataMember(Name = "exit_code", Order = 7)] public int ExitCode { get; set; }
        [DataMember(Name = "error", Order = 8, EmitDefaultValue = false)] public string Error { get; set; }

        private const string ResponseSchemaValue = "pytone-rip.runtime-response";

        public static RipBrokerResponse Pass(RipBrokerRequest request) => new RipBrokerResponse
        {
            RequestId = request.RequestId,
            JobId = request.JobId,
            Success = true,
            Status = "SENT",
            ExitCode = 0,
        };

        public static RipBrokerResponse Fail(string requestId, string jobId, Exception error) =>
            new RipBrokerResponse
            {
                RequestId = requestId,
                JobId = jobId,
                Success = false,
                Status = "FAILED",
                ExitCode = error.HResult,
                Error = error.Message,
            };

        public static RipBrokerResponse Cancelled(
            string requestId, string jobId, string message) =>
            new RipBrokerResponse
            {
                RequestId = requestId,
                JobId = jobId,
                Success = false,
                Status = "CANCELLED",
                ExitCode = unchecked((int)0x800704C7),
                Error = message,
            };
    }

    [DataContract]
    internal sealed class RipJobProgress
    {
        [DataMember(Name = "schema", Order = 1)]
        public string Schema { get; set; } = "pytone-rip.job-progress";
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; } = 1;
        [DataMember(Name = "job_id", Order = 3)] public string JobId { get; set; }
        [DataMember(Name = "status", Order = 4)] public string Status { get; set; }
        [DataMember(Name = "stage", Order = 5)] public string Stage { get; set; }
        [DataMember(Name = "percent", Order = 6)] public int Percent { get; set; }
        [DataMember(Name = "message", Order = 7)] public string Message { get; set; }
        [DataMember(Name = "updated_utc", Order = 8)] public string UpdatedUtc { get; set; }
        [DataMember(Name = "terminal", Order = 9)] public bool Terminal { get; set; }
    }
}
