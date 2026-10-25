using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using pyToneRIP.Tasks.Services;
using Windows.ApplicationModel.Background;
using Windows.Foundation;
using Windows.Foundation.Metadata;
using Windows.Graphics.Printing.PrintSupport;
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
                QueuePdcLog("PrintSupportExtension Run entered");

                var details = taskInstance.TriggerDetails as PrintSupportExtensionTriggerDetails;
                session = details?.Session;
                if (session == null)
                {
                    QueuePdcLog("PrintSupportExtension session is unavailable");
                    CompleteTaskOnce("session unavailable");
                    return;
                }

                // Without a custom validator, Windows validates tickets against the PDC.
                // Do not replace that check with an unconditional Resolved response.
                session.PrintDeviceCapabilitiesChanged += OnPrintDeviceCapabilitiesChanged;
                session.Start();
                QueuePdcLog("PrintSupportExtension session started");
            }
            catch (Exception error)
            {
                // An exception must never escape a print-support background task.
                // The calling print UI is waiting on this session even though the
                // extension runs separately; an abrupt activation failure can
                // leave the Page Setup request without a valid completion path.
                QueuePdcLog(
                    "PrintSupportExtension Run FAILED: " + DescribeException(error));
                DetachSessionHandlers();
                CompleteTaskOnce("Run failure");
            }
        }

        private void OnTaskCanceled(
            IBackgroundTaskInstance sender,
            BackgroundTaskCancellationReason reason)
        {
            QueuePdcLog(
                "PrintSupportExtension cancellation requested: " + reason);
            DetachSessionHandlers();
            CompleteTaskOnce("background-task cancellation");
        }

        private async void OnPrintDeviceCapabilitiesChanged(
            PrintSupportExtensionSession session,
            PrintSupportPrintDeviceCapabilitiesChangedEventArgs args)
        {
            Deferral callbackDeferral = null;
            try
            {
                callbackDeferral = args.GetDeferral();
                ConfigureLosslessMxdc(args);
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

        private static void ConfigureLosslessMxdc(
            PrintSupportPrintDeviceCapabilitiesChangedEventArgs args)
        {
            try
            {
                if (!ApiInformation.IsPropertyPresent(
                    "Windows.Graphics.Printing.PrintSupport.PrintSupportPrintDeviceCapabilitiesChangedEventArgs",
                    "MxdcImageQualityConfiguration"))
                {
                    QueuePdcLog("MXDC lossless image configuration is unavailable");
                    return;
                }

                var quality = args.MxdcImageQualityConfiguration;
                if (quality == null)
                {
                    QueuePdcLog("MXDC lossless image configuration returned null");
                    return;
                }

                // RGB calibration encodes the channel in exact R/B bytes.
                // JPEG in GDI-to-XPS destroys those selectors before XpsToTiff.
                // Configure the queue for every PageOutputQuality, not per job.
                quality.NormalOutputQuality = XpsImageQuality.Png;
                quality.DraftOutputQuality = XpsImageQuality.Png;
                quality.HighOutputQuality = XpsImageQuality.Png;
                quality.PhotographicOutputQuality = XpsImageQuality.Png;
                quality.TextOutputQuality = XpsImageQuality.Png;
                quality.AutomaticOutputQuality = XpsImageQuality.Png;
                quality.FaxOutputQuality = XpsImageQuality.Png;
                QueuePdcLog("MXDC image configuration applied: PNG for all output qualities");
            }
            catch (Exception error)
            {
                QueuePdcLog("MXDC lossless image configuration FAILED: "
                    + DescribeException(error));
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
                activeSession.PrintDeviceCapabilitiesChanged -=
                    OnPrintDeviceCapabilitiesChanged;
            }
            catch (Exception error)
            {
                QueuePdcLog(
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
                QueuePdcLog(
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
                QueuePdcLog(
                    callbackName + " deferral completion FAILED: "
                    + DescribeException(error));
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

        private static void QueuePdcLog(string message)
        {
            // Keep file I/O off activation, validation and cancellation paths.
            // Only the diagnostic string crosses threads, never a print object.
            try { _ = Task.Run(() => WritePdcLogAsync(message)); }
            catch { /* Diagnostics must not prevent the callback from returning. */ }
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
