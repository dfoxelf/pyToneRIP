using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using pyToneRIP.Models;
using Windows.ApplicationModel;
using Windows.Devices.Printers;
using Windows.Globalization;
using Windows.Storage;

namespace pyToneRIP.Services
{
    /// <summary>
    /// Создаёт динамические очереди непосредственно из Manager/Core.
    /// Каждая очередь получает собственный model PDC, но все очереди принадлежат
    /// одному Manager package и используют общий Windows workflow/backend.
    /// </summary>
    internal static class VirtualPrinterManagementService
    {
        private const string ManagerPackageName = "pyToneRIP.Manager";
        private const string PrinterUriPrefix = "pytone-rip:";
        private const string WorkflowEntryPoint =
            "pyToneRIP.Tasks.VirtualPrinterBackgroundTask";

        public static string CreatePrinterUri(string modelId)
        {
            var token = ModelToken(modelId).ToLowerInvariant();
            return PrinterUriPrefix + token + "/" + Guid.NewGuid().ToString("N");
        }

        public static async Task<VirtualPrinterInstallationResult> InstallAsync(
            string modelId,
            string printerName,
            string printerUri)
        {
            AssertManagerPackage();
            var token = ModelToken(modelId);
            AssertPrinterUri(token, printerUri);
            if (string.IsNullOrWhiteSpace(printerName))
                throw new ArgumentException("A Windows printer name is required.", nameof(printerName));

            var pdcPath = "Assets\\PdcModels\\model." + token + ".pdc.xml";
            // Fail with a precise package-resource error before entering the
            // Windows API. The API otherwise reports only E_UNEXPECTED when a
            // package-relative PDC path is wrong.
            await StorageFile.GetFileFromApplicationUriAsync(
                new Uri("ms-appx:///" + pdcPath.Replace('\\', '/')));
            var pdrPath = await ResolvePdrPathAsync(token);

            var parameters = new VirtualPrinterInstallationParameters
            {
                PrinterName = printerName.Trim(),
                PrinterUri = new Uri(printerUri),
                PrintDeviceCapabilitiesPackageRelativeFilePath = pdcPath,
                PrintDeviceResourcesPackageRelativeFilePath = pdrPath,
                PreferredInputFormat = VirtualPrinterPreferredInputFormat.OpenXps,
                EntryPoint = WorkflowEntryPoint,
            };

            App.WriteStartupStage(
                "VirtualPrinterManager install begin: name=" + parameters.PrinterName
                + "; uri=" + parameters.PrinterUri
                + "; model=" + modelId
                + "; pdc=" + pdcPath
                + "; pdr=" + pdrPath
                + "; ownerPfn=" + Package.Current.Id.FamilyName
                + "; entryPoint=" + WorkflowEntryPoint);
            // Use the self-owned overload proven by Microsoft's sample. The package-level
            // manifest bootstrap registers the workflow class; passing the PFN
            // overload on this OS activates Run but can leave the OXPS job at
            // Size=0 before VirtualPrinterDataAvailable.
            var result = await VirtualPrinterManager.InstallVirtualPrinterAsync(parameters);
            App.WriteStartupStage(
                "VirtualPrinterManager install returned: status=" + result.Status
                + "; extendedError=" + FormatError(result.ExtendedError));

            if (result.Status != VirtualPrinterInstallationStatus.InstallationSucceeded
                && result.Status != VirtualPrinterInstallationStatus.PrinterAlreadyInstalled)
            {
                throw new InvalidOperationException(
                    "Windows did not install the virtual printer: " + result.Status + ".",
                    result.ExtendedError);
            }
            return result;
        }

        private static async Task<string> ResolvePdrPathAsync(string token)
        {
            var candidates = new List<string>();
            var selectedLanguage = (ApplicationLanguages.PrimaryLanguageOverride ?? "")
                .Replace('-', '_');
            if (!string.IsNullOrWhiteSpace(selectedLanguage))
            {
                candidates.Add(selectedLanguage);
                var selectedSeparator = selectedLanguage.IndexOf('_');
                if (selectedSeparator > 0)
                    candidates.Add(selectedLanguage.Substring(0, selectedSeparator));
            }
            foreach (var language in ApplicationLanguages.Languages)
            {
                var normalized = language.Replace('-', '_');
                if (!candidates.Contains(normalized, StringComparer.OrdinalIgnoreCase))
                    candidates.Add(normalized);
                var separator = normalized.IndexOf('_');
                var primary = separator < 0 ? normalized : normalized.Substring(0, separator);
                if (!candidates.Contains(primary, StringComparer.OrdinalIgnoreCase))
                    candidates.Add(primary);
            }
            if (!candidates.Contains("en_US", StringComparer.OrdinalIgnoreCase))
                candidates.Add("en_US");
            if (!candidates.Contains("en_GB", StringComparer.OrdinalIgnoreCase))
                candidates.Add("en_GB");
            // Compatibility with packages generated before every app locale
            // received its own PDR.
            if (!candidates.Contains("en", StringComparer.OrdinalIgnoreCase))
                candidates.Add("en");

            foreach (var locale in candidates)
            {
                var path = "Assets\\PdrModels\\model." + token + "." + locale + ".pdr.xml";
                try
                {
                    await StorageFile.GetFileFromApplicationUriAsync(
                        new Uri("ms-appx:///" + path.Replace('\\', '/')));
                    return path;
                }
                catch (FileNotFoundException)
                {
                    // This model's source PPD does not contain this locale.
                }
            }
            throw new FileNotFoundException(
                "The package does not contain an English PDR for model " + token + ".");
        }

        public static string GetPrinterUri(string portName)
        {
            if (string.IsNullOrWhiteSpace(portName))
                return null;
            var start = portName.IndexOf(PrinterUriPrefix, StringComparison.OrdinalIgnoreCase);
            if (start < 0)
                return null;
            var end = portName.IndexOf("_S-1-", start, StringComparison.OrdinalIgnoreCase);
            return (end < 0 ? portName.Substring(start)
                : portName.Substring(start, end - start)).TrimEnd('_');
        }

        public static WindowsQueueDescriptor FindQueue(
            PrinterInstanceRecord printer, IReadOnlyList<WindowsQueueDescriptor> queues)
        {
            if (queues == null)
                throw new InvalidDataException("Windows printer enumeration returned no result.");
            // A queue name can be reused after external deletion. Resolve the
            // saved instance by URI/port so it cannot adopt another printer.
            if (!string.IsNullOrWhiteSpace(printer.PrinterUri))
                return queues.FirstOrDefault(queue => string.Equals(
                    GetPrinterUri(queue.PortName), printer.PrinterUri,
                    StringComparison.OrdinalIgnoreCase));
            if (!string.IsNullOrWhiteSpace(printer.SystemQueueId))
                return queues.FirstOrDefault(queue => string.Equals(
                    queue.PortName, printer.SystemQueueId, StringComparison.OrdinalIgnoreCase));
            throw new InvalidOperationException("The saved printer has no queue identity.");
        }

        public static async Task<WindowsQueueDescriptor> FindQueueAsync(PrinterInstanceRecord printer) =>
            FindQueue(printer, await QueueHelperClient.ListAsync());

        // true: Windows removed the queue; false: the saved instance is already absent.
        public static async Task<bool> RemoveAsync(PrinterInstanceRecord printer)
        {
            AssertManagerPackage();
            var queue = await FindQueueAsync(printer);
            if (queue == null)
            {
                App.WriteStartupStage("Virtual printer already absent: uri=" + printer.PrinterUri);
                return false;
            }
            var printerName = queue.Name;

            IReadOnlyList<string> owned = VirtualPrinterManager.FindAllVirtualPrinters(
                Package.Current.Id.FamilyName);
            if (owned == null || !owned.Any(item => string.Equals(
                item, printerName, StringComparison.OrdinalIgnoreCase)))
            {
                if (await FindQueueAsync(printer) == null)
                    return false;
                throw new InvalidOperationException(
                    "The requested virtual printer is not owned by pyToneRIP Manager.");
            }

            App.WriteStartupStage(
                "VirtualPrinterManager remove begin: name=" + printerName);
            bool removed;
            try
            {
                removed = await VirtualPrinterManager.RemoveVirtualPrinterAsync(printerName);
            }
            catch (Exception)
            {
                // Windows Settings may remove the queue after our enumeration.
                // Only confirmed absence allows deleting the saved configuration.
                if (await FindQueueAsync(printer) == null)
                    return false;
                throw;
            }
            App.WriteStartupStage(
                "VirtualPrinterManager remove returned: removed=" + removed);
            if (!removed)
            {
                if (await FindQueueAsync(printer) == null)
                    return false;
                throw new InvalidOperationException(
                    "Windows did not remove the virtual printer.");
            }
            return true;
        }

        private static void AssertPrinterUri(string token, string printerUri)
        {
            var expected = PrinterUriPrefix + token.ToLowerInvariant() + "/";
            if (string.IsNullOrWhiteSpace(printerUri)
                || !printerUri.StartsWith(expected, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException(
                    "The virtual printer URI does not match the selected printer model.");
            }
            _ = new Uri(printerUri);
        }

        private static void AssertManagerPackage()
        {
            if (!string.Equals(
                Package.Current.Id.Name,
                ManagerPackageName,
                StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    "Virtual printer lifecycle operations must run inside pyToneRIP Manager.");
        }

        private static string ModelToken(string modelId)
        {
            var token = new string((modelId ?? "").Where(char.IsLetterOrDigit).ToArray());
            if (string.IsNullOrWhiteSpace(token))
                throw new ArgumentException(
                    "A valid printer model is required.", nameof(modelId));
            return token;
        }

        private static string FormatError(Exception error) => error == null
            ? "<null>"
            : error.GetType().FullName + "/0x" + error.HResult.ToString("X8")
                + ": " + error.Message;
    }
}
