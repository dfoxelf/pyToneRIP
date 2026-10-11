using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using pyToneRIP.Tasks.Services;
using Windows.ApplicationModel.Background;
using Windows.Foundation;
using Windows.Graphics.Printing.PrintSupport;
using Windows.Graphics.Printing.PrintTicket;
using Windows.Storage;

namespace pyToneRIP.Tasks
{
    public sealed class PrintSupportExtensionBackgroundTask : IBackgroundTask
    {
        private static readonly SemaphoreSlim PdcLogGate = new SemaphoreSlim(1, 1);

        private BackgroundTaskDeferral taskDeferral;
        private PrintSupportExtensionSession session;
        private int taskCompletionStarted;

        public void Run(IBackgroundTaskInstance taskInstance)
        {
            try
            {
                if (taskInstance == null)
                    throw new ArgumentNullException(nameof(taskInstance));

                taskDeferral = taskInstance.GetDeferral();
                taskInstance.Canceled += OnTaskCanceled;
                _ = WritePdcLogAsync("PrintSupportExtension Run entered");

                var details = taskInstance.TriggerDetails as PrintSupportExtensionTriggerDetails;
                session = details?.Session;
                if (session == null)
                {
                    _ = WritePdcLogAsync("PrintSupportExtension session is unavailable");
                    CompleteTaskOnce("session unavailable");
                    return;
                }

                session.PrintTicketValidationRequested += OnPrintTicketValidationRequested;
                session.PrintDeviceCapabilitiesChanged += OnPrintDeviceCapabilitiesChanged;
                session.Start();
                _ = WritePdcLogAsync("PrintSupportExtension session started");
            }
            catch (Exception error)
            {
                // An exception must never escape a print-support background task.
                // The calling print UI is waiting on this session even though the
                // extension runs separately; an abrupt activation failure can
                // leave the Page Setup request without a valid completion path.
                _ = WritePdcLogAsync(
                    "PrintSupportExtension Run FAILED: " + DescribeException(error));
                DetachSessionHandlers();
                CompleteTaskOnce("Run failure");
            }
        }

        private void OnTaskCanceled(
            IBackgroundTaskInstance sender,
            BackgroundTaskCancellationReason reason)
        {
            _ = WritePdcLogAsync(
                "PrintSupportExtension cancellation requested: " + reason);
            DetachSessionHandlers();
            CompleteTaskOnce("background-task cancellation");
        }

        private static async void OnPrintTicketValidationRequested(
            PrintSupportExtensionSession session,
            PrintSupportPrintTicketValidationRequestedEventArgs args)
        {
            Deferral callbackDeferral = null;
            var validationStatusWasSet = false;
            try
            {
                callbackDeferral = args.GetDeferral();
                await WritePdcLogAsync(
                    "PrintTicket validation entered: "
                    + SummarizePrinter(session)
                    + "; " + SummarizeTicket(args.PrintTicket));

                // pyToneRIP currently does not rewrite the application's ticket
                // here. Model constraints are already expressed by the installed
                // per-queue PDC, so a ticket accepted by the Windows print UI is
                // acknowledged unchanged.
                args.SetPrintTicketValidationStatus(
                    WorkflowPrintTicketValidationStatus.Resolved);
                validationStatusWasSet = true;
                await WritePdcLogAsync(
                    "PrintTicket validation completed: Resolved");
            }
            catch (Exception error)
            {
                await WritePdcLogAsync(
                    "PrintTicket validation FAILED: " + DescribeException(error));

                // If validation failed before a result was returned, explicitly
                // reject the ticket instead of leaving Windows waiting for an
                // unfinished callback. Never overwrite a status already supplied.
                if (!validationStatusWasSet && args != null)
                {
                    try
                    {
                        args.SetPrintTicketValidationStatus(
                            WorkflowPrintTicketValidationStatus.Invalid);
                        await WritePdcLogAsync(
                            "PrintTicket validation fallback completed: Invalid");
                    }
                    catch (Exception fallbackError)
                    {
                        await WritePdcLogAsync(
                            "PrintTicket validation fallback FAILED: "
                            + DescribeException(fallbackError));
                    }
                }
            }
            finally
            {
                CompleteCallbackDeferralSafely(
                    callbackDeferral, "PrintTicket validation");
            }
        }

        private async void OnPrintDeviceCapabilitiesChanged(
            PrintSupportExtensionSession session,
            PrintSupportPrintDeviceCapabilitiesChangedEventArgs args)
        {
            Deferral callbackDeferral = null;
            try
            {
                callbackDeferral = args.GetDeferral();
                var printerUri = session.Printer?.PrinterUri?.ToString();
                var printerName = session.Printer?.PrinterName;
                await WritePdcLogAsync(
                    "PDC callback entered: uri=" + (printerUri ?? "")
                    + "; name=" + (printerName ?? ""));
                await WritePdcLogAsync(
                    "PDC callback current: "
                    + SummarizePageMediaSizes(
                        args.GetCurrentPrintDeviceCapabilities()));
                var bundle = await ModelPdcProvider.LoadForQueueAsync(
                    printerUri, printerName, args.ResourceLanguage);
                await WritePdcLogAsync(
                    "PDC callback replacement: model=" + bundle.ModelId
                    + "; pdr=" + bundle.ResourcesFilename
                    + "; " + SummarizePageMediaSizes(bundle.Capabilities));
                args.UpdatePrintDeviceCapabilities(bundle.Capabilities);
                args.UpdatePrintDeviceResources(bundle.Resources);
                // A virtual printer's installed PDC file does not change when
                // the user changes language. RefreshPrintDeviceCapabilities
                // alone has not raised this event for those unchanged files.
                // Expire the Windows capabilities cache on the next bind so a
                // newly opened print dialog obtains the current PDR as well.
                args.SetPrintDeviceCapabilitiesUpdatePolicy(
                    PrintSupportPrintDeviceCapabilitiesUpdatePolicy.CreatePeriodicRefresh(
                        TimeSpan.Zero));
                await WritePdcLogAsync(
                    "PDC/PDR callback update returned: model=" + bundle.ModelId
                    + "; language=" + (args.ResourceLanguage ?? "")
                    + "; selected=" + pyToneRIP.Shared.SharedApplicationStorage.LanguageOverride
                    + "; refreshPolicy=periodic-zero");
                await WritePdcLogAsync(
                    "PDC callback completed: model=" + bundle.ModelId);
            }
            catch (Exception error)
            {
                await WritePdcLogAsync(
                    "PDC callback FAILED: " + DescribeException(error));
            }
            finally
            {
                CompleteCallbackDeferralSafely(callbackDeferral, "PDC callback");
            }
        }

        private void DetachSessionHandlers()
        {
            var activeSession = session;
            session = null;
            if (activeSession == null)
                return;

            try
            {
                activeSession.PrintTicketValidationRequested -=
                    OnPrintTicketValidationRequested;
                activeSession.PrintDeviceCapabilitiesChanged -=
                    OnPrintDeviceCapabilitiesChanged;
            }
            catch (Exception error)
            {
                _ = WritePdcLogAsync(
                    "PrintSupportExtension handler cleanup FAILED: "
                    + DescribeException(error));
            }
        }

        private void CompleteTaskOnce(string reason)
        {
            // Cancellation and activation failure can race. BackgroundTaskDeferral
            // must be completed exactly once; a second Complete can itself throw.
            if (Interlocked.Exchange(ref taskCompletionStarted, 1) != 0)
                return;

            try
            {
                taskDeferral?.Complete();
            }
            catch (Exception error)
            {
                _ = WritePdcLogAsync(
                    "PrintSupportExtension task completion FAILED (" + reason
                    + "): " + DescribeException(error));
            }
        }

        private static void CompleteCallbackDeferralSafely(
            Deferral callbackDeferral,
            string callbackName)
        {
            if (callbackDeferral == null)
                return;

            try
            {
                callbackDeferral.Complete();
            }
            catch (Exception error)
            {
                _ = WritePdcLogAsync(
                    callbackName + " deferral completion FAILED: "
                    + DescribeException(error));
            }
        }

        private static string SummarizePrinter(PrintSupportExtensionSession session)
        {
            try
            {
                return "uri=" + (session?.Printer?.PrinterUri?.ToString() ?? "")
                    + "; name=" + (session?.Printer?.PrinterName ?? "");
            }
            catch (Exception error)
            {
                return "printer summary unavailable: " + DescribeException(error);
            }
        }

        private static string DescribeException(Exception error)
        {
            if (error == null)
                return "unknown exception";
            return error.GetType().FullName + ": " + error.Message
                + "; HResult=0x" + error.HResult.ToString("X8");
        }

        private static string SummarizePageMediaSizes(
            Windows.Data.Xml.Dom.XmlDocument document)
        {
            if (document == null)
                return "document=null";
            var feature = document.SelectSingleNode(
                "//*[local-name()='PageMediaSize' or (local-name()='Feature' and substring-after(@name, ':')='PageMediaSize')]");
            if (feature == null)
                return "PageMediaSize=absent";
            var options = feature.SelectNodes("./*");
            var names = new List<string>();
            for (uint index = 0; index < options.Length; ++index)
            {
                var element = options.Item(index) as Windows.Data.Xml.Dom.XmlElement;
                if (element != null)
                {
                    var name = element.GetAttribute("name");
                    names.Add(string.IsNullOrWhiteSpace(name)
                        ? element.LocalName?.ToString() ?? "" : name);
                }
            }
            return "PageMediaSize.count=" + options.Length
                + "; names=" + string.Join(",", names);
        }

        private static string SummarizeTicket(
            Windows.Graphics.Printing.PrintTicket.WorkflowPrintTicket ticket)
        {
            try
            {
                var selection = pyToneRIP.Shared.PrintTicketSynchronization.Read(ticket);
                return "page=" + (selection.PageOptionName ?? "<absent>")
                    + "; media=" + (selection.MediaOptionName ?? "<absent>")
                    + "; input=" + (selection.InputOptionName ?? "<absent>")
                    + "; size=" + (selection.PageWidthMm?.ToString("0.###") ?? "?")
                    + "x" + (selection.PageHeightMm?.ToString("0.###") ?? "?")
                    + "mm; orientation=" + (selection.OrientationOptionName ?? "<absent>")
                    + "; resolution=" + (selection.ResolutionX?.ToString() ?? "?")
                    + "x" + (selection.ResolutionY?.ToString() ?? "?");
            }
            catch (Exception error)
            {
                return "ticket summary unavailable: " + DescribeException(error);
            }
        }

        private static Task WritePdcLogAsync(string message)
        {
            var lockWasTaken = false;
            try
            {
                // A print callback must not await a log file held by another
                // process. Drop a diagnostic line if a writer is already busy.
                if (!PdcLogGate.Wait(0)) return Task.CompletedTask;
                lockWasTaken = true;
                var path = Path.Combine(ApplicationData.Current.LocalFolder.Path, "pyToneRIP-pdc.log");
                using (var stream = new FileStream(path, FileMode.Append, FileAccess.Write,
                    FileShare.ReadWrite | FileShare.Delete))
                using (var writer = new StreamWriter(stream, new System.Text.UTF8Encoding(false)))
                    writer.WriteLine(DateTimeOffset.Now.ToString("O") + " | " + message);
            }
            catch
            {
                // Diagnostics must never break Print Support activation.
            }
            finally
            {
                if (lockWasTaken)
                    PdcLogGate.Release();
            }
            return Task.CompletedTask;
        }

    }
}
