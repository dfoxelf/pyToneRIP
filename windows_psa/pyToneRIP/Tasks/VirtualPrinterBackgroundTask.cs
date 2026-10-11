using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using pyToneRIP.Tasks.Services;
using Windows.ApplicationModel.Background;
using Windows.Graphics.Printing.Workflow;
using Windows.Storage;

namespace pyToneRIP.Tasks
{
    public sealed class VirtualPrinterBackgroundTask : IBackgroundTask
    {
        private BackgroundTaskDeferral deferral;
        private IBackgroundTaskInstance taskInstance;
        private PrintWorkflowVirtualPrinterSession session;
        private JobRecordWriter writer;
        private CancellationTokenSource statusMonitorCancellation;
        private int cancellationRequested;
        private int dataAvailableEntered;

        public void Run(IBackgroundTaskInstance taskInstance)
        {
            this.taskInstance = taskInstance;
            deferral = taskInstance.GetDeferral();
            taskInstance.Canceled += OnCanceled;
            WriteWorkflowStage("VirtualPrinterBackgroundTask.Run entered");
            try
            {
                var details = taskInstance.TriggerDetails as PrintWorkflowVirtualPrinterTriggerDetails;
                if (details == null)
                    throw new InvalidOperationException(
                        "Virtual printer activation did not provide PrintWorkflowVirtualPrinterTriggerDetails.");
                session = details.VirtualPrinterSession;
                if (session == null)
                    throw new InvalidOperationException("Virtual printer activation did not provide a session.");
                writer = new JobRecordWriter();
                session.VirtualPrinterDataAvailable += OnDataAvailable;
                WriteWorkflowStage("VirtualPrinterBackgroundTask starting session");
                // Windows requires the virtual-printer session to start during
                // activation. Do not await file I/O before this call.
                session.Start();
                WriteWorkflowStage("VirtualPrinterBackgroundTask session started");
                _ = LogMissingDataCallbackAsync(session);
            }
            catch (Exception error)
            {
                WriteWorkflowFailure("VirtualPrinterBackgroundTask.Run failed", error);
                if (taskInstance != null)
                    taskInstance.Canceled -= OnCanceled;
                CompleteDeferral();
            }
        }

        private async void OnDataAvailable(
            PrintWorkflowVirtualPrinterSession session,
            PrintWorkflowVirtualPrinterDataAvailableEventArgs args)
        {
            Interlocked.Exchange(ref dataAvailableEntered, 1);
            try
            {
                WriteWorkflowStage("VirtualPrinterDataAvailable entered");
                await writer.BeginAsync(session.Printer.PrinterName);
                ThrowIfCancellationRequested();
                statusMonitorCancellation = new CancellationTokenSource();
                _ = MonitorSessionStatusAsync(session, statusMonitorCancellation.Token);
                await PrinterAssignmentGate.RequireConfiguredAsync(
                    session.Printer.PrinterUri?.ToString(),
                    session.Printer.PrinterName);
                ThrowIfCancellationRequested();
                WriteWorkflowStage("PrinterAssignmentGate configured PASS");
                var record = await writer.CaptureAsync(session, args);
                ThrowIfCancellationRequested();
                WriteWorkflowStage("VirtualPrinterDataAvailable capture complete");
                var response = await CoreAppServiceClient.ExecuteAsync(record);
                ThrowIfCancellationRequested();
                WriteWorkflowStage(
                    "Packaged Python RIP complete: " + response.Status);
                if (!string.Equals(response.Status, "SENT", StringComparison.Ordinal))
                    throw new InvalidOperationException(
                        "pyToneRIP Core returned unexpected terminal status: "
                        + response.Status + ".");
                args.CompleteJob(PrintWorkflowSubmittedStatus.Succeeded);
                WriteWorkflowStage(
                    "VirtualPrinterDataAvailable completed job as Succeeded (PRN sent)");
            }
            catch (OperationCanceledException error)
            {
                WriteWorkflowStage("VirtualPrinterDataAvailable canceled: " + error.Message);
                try { await writer.WriteCancelledAsync(error.Message); } catch { }
                // CompleteJob(Canceled) is the explicit Windows workflow result.
                // It can throw if Windows has already torn down the canceled job.
                try { args.CompleteJob(PrintWorkflowSubmittedStatus.Canceled); } catch { }
            }
            catch (Exception error)
            {
                WriteWorkflowFailure("VirtualPrinterDataAvailable failed", error);
                if (IsCancellationRequested)
                {
                    try { await writer.WriteCancelledAsync(error.Message); } catch { }
                    try { args.CompleteJob(PrintWorkflowSubmittedStatus.Canceled); } catch { }
                }
                else
                {
                    try { await writer.WriteFailureAsync(error); } catch { }
                    args.CompleteJob(PrintWorkflowSubmittedStatus.Failed);
                }
            }
            finally
            {
                var monitor = Interlocked.Exchange(ref statusMonitorCancellation, null);
                monitor?.Cancel();
                monitor?.Dispose();
                if (this.session != null)
                    this.session.VirtualPrinterDataAvailable -= OnDataAvailable;
                if (taskInstance != null)
                    taskInstance.Canceled -= OnCanceled;
                this.session = null;
                CompleteDeferral();
            }
        }

        private void OnCanceled(
            IBackgroundTaskInstance sender,
            BackgroundTaskCancellationReason reason)
        {
            WriteWorkflowStage("VirtualPrinterBackgroundTask canceled: " + reason);
            try
            {
                // SystemPolicy also follows normal completion and failures;
                // the diagnostics showed it firing after SENT and after a
                // transport timeout. Only an Aborted workflow is evidence
                // that Windows canceled this particular print job.
                if (IsSessionAborted(session))
                    RequestCancellation("Windows background task ended after workflow abort: " + reason);
                else
                    WriteWorkflowStage(
                        "Windows ended the PSA host without an aborted workflow; "
                        + "durable RIP cancellation was not requested");
                if (session != null)
                    session.VirtualPrinterDataAvailable -= OnDataAvailable;
                sender.Canceled -= OnCanceled;
                session = null;
                CompleteDeferral();
            }
            catch (Exception error)
            {
                WriteWorkflowFailure("VirtualPrinterBackgroundTask cancellation cleanup failed", error);
            }
        }

        private bool IsCancellationRequested =>
            Volatile.Read(ref cancellationRequested) != 0;

        private async Task LogMissingDataCallbackAsync(
            PrintWorkflowVirtualPrinterSession currentSession)
        {
            // Photos can leave a job in Error before Windows supplies PDL.
            // Record only the workflow state; this observer never changes the job.
            foreach (var delay in new[] { 2000, 8000 })
            {
                await Task.Delay(delay);
                if (Volatile.Read(ref dataAvailableEntered) != 0
                    || !ReferenceEquals(session, currentSession))
                    return;
                try
                {
                    WriteWorkflowStage("Awaiting VirtualPrinterDataAvailable; status="
                        + currentSession.Status);
                }
                catch (Exception error)
                {
                    WriteWorkflowFailure("Could not read workflow status before PDL", error);
                    return;
                }
            }
        }

        private static bool IsSessionAborted(PrintWorkflowVirtualPrinterSession currentSession)
        {
            try
            {
                return currentSession != null
                    && currentSession.Status == PrintWorkflowSessionStatus.Aborted;
            }
            catch (Exception error)
            {
                WriteWorkflowFailure("Could not read canceled workflow status", error);
                return false;
            }
        }

        private void RequestCancellation(string reason)
        {
            if (Interlocked.Exchange(ref cancellationRequested, 1) != 0)
                return;
            WriteWorkflowStage("RIP cancellation requested: " + reason);
            writer?.RequestCancellation(reason);
        }

        private void ThrowIfCancellationRequested()
        {
            if (IsCancellationRequested)
                throw new OperationCanceledException("Windows print job was canceled.");
        }

        private async Task MonitorSessionStatusAsync(
            PrintWorkflowVirtualPrinterSession currentSession,
            CancellationToken cancellationToken)
        {
            PrintWorkflowSessionStatus? previousStatus = null;
            try
            {
                while (!cancellationToken.IsCancellationRequested)
                {
                    var status = currentSession.Status;
                    if (previousStatus != status)
                    {
                        WriteWorkflowStage("Virtual printer workflow status: " + status);
                        previousStatus = status;
                    }
                    if (status == PrintWorkflowSessionStatus.Aborted
                        || status == PrintWorkflowSessionStatus.Closed)
                    {
                        RequestCancellation("Windows workflow status changed to " + status + ".");
                        return;
                    }
                    await Task.Delay(200, cancellationToken);
                }
            }
            catch (OperationCanceledException)
            {
                // Normal shutdown after the job reaches a terminal state.
            }
            catch (Exception error)
            {
                WriteWorkflowFailure("Workflow cancellation monitor failed", error);
            }
        }

        private void CompleteDeferral()
        {
            var current = Interlocked.Exchange(ref deferral, null);
            current?.Complete();
        }

        internal static void WriteWorkflowStage(string stage)
        {
            try
            {
                File.AppendAllText(
                    Path.Combine(ApplicationData.Current.LocalFolder.Path, "pyToneRIP-workflow.log"),
                    DateTimeOffset.Now.ToString("O") + " | " + stage + Environment.NewLine);
            }
            catch
            {
                // Diagnostics must never hide the workflow completion status.
            }
        }

        internal static void WriteWorkflowFailure(string stage, Exception error)
        {
            WriteWorkflowStage(
                stage + " | HRESULT=0x" + error.HResult.ToString("X8")
                + Environment.NewLine + error);
        }
    }
}
