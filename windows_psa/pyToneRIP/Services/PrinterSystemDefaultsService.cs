using System;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using Windows.Devices.Printers;
using Windows.Graphics.Printing.PrintTicket;
using Windows.Storage;

namespace pyToneRIP.Services
{
    internal static class PrinterSystemDefaultsService
    {
        public static WorkflowPrintTicket GetUserDefaultPrintTicket(string queueName)
        {
            var printer = Resolve(queueName);
            return printer.UserDefaultPrintTicket;
        }

        public static async Task RequestCapabilitiesRefreshAsync(string queueName)
        {
            var printer = Resolve(queueName);
            await LogAsync("Refresh request begin: " + queueName);
            // Keep this call on the caller's UI apartment. The 1.0.0.33
            // experiment incorrectly moved it to a worker thread.
            printer.RefreshPrintDeviceCapabilities();
            await LogAsync("Refresh request returned: " + queueName);
        }

        public static async Task ApplyUserDefaultsAsync(
            string queueName,
            string pageId,
            double pageWidthMm,
            double pageHeightMm,
            string orientation,
            int resolutionX,
            int resolutionY,
            string mediaId,
            string inputSourceId,
            bool? borderless = null,
            bool allowConfigurationXmlFallback = false)
        {
            await LogAsync(Diagnostics.PrintSettingsContext.Describe("ManagerDefaults", queueName));
            var printer = Resolve(queueName);
            var canModify = printer.CanModifyUserDefaultPrintTicket;
            await LogAsync("User defaults authorization: canModify=" + canModify);
            if (!canModify)
                throw new InvalidOperationException(
                    "Windows did not authorize this PSA to modify the queue user default PrintTicket.");
            await LogAsync("User defaults apply begin: queue=" + queueName);
            var ticket = await ReadInitialUserDefaultPrintTicketAsync(
                printer, queueName, allowConfigurationXmlFallback);
            if (ticket == null)
                throw new InvalidOperationException("Windows returned no user default PrintTicket.");
            Shared.PrintTicketSynchronization.Apply(
                ticket, pageId, pageWidthMm, pageHeightMm,
                orientation, resolutionX, resolutionY, mediaId, inputSourceId, borderless);
            var validation = await Shared.PrintTicketSynchronization.ValidateAsync(
                ticket, message => { _ = LogAsync("queue=" + queueName + "; " + message); });
            if (!validation.Validated)
                throw new InvalidOperationException(
                    "Default PrintTicket validation failed: " + validation.ExtendedError);
            printer.UserDefaultPrintTicket = ticket;
            var storedTicket = printer.UserDefaultPrintTicket;
            if (storedTicket == null)
                throw new InvalidOperationException(
                    "Windows returned no UserDefaultPrintTicket after saving.");
            var storedSelection = Shared.PrintTicketSynchronization.Read(storedTicket);
            if (!Shared.PrintTicketSynchronization.SelectionMatchesPage(
                    storedSelection, pageId, pageWidthMm, pageHeightMm, borderless))
                throw new InvalidOperationException(
                    "Windows did not persist the requested default page. Requested "
                    + pageId + ", read back "
                    + (storedSelection.PageOptionName ?? "<absent>")
                    + "; requested size=" + pageWidthMm.ToString("0.###")
                    + "x" + pageHeightMm.ToString("0.###")
                    + "mm; read back size=" + (storedSelection.PageWidthMm?.ToString("0.###") ?? "?")
                    + "x" + (storedSelection.PageHeightMm?.ToString("0.###") ?? "?") + "mm.");
            if (!Shared.PrintTicketSynchronization.SelectionMatchesDefaults(
                storedSelection, pageId, pageWidthMm, pageHeightMm,
                orientation, resolutionX, resolutionY, mediaId, inputSourceId, borderless))
                throw new InvalidOperationException(
                    "Windows did not persist the requested orientation, raster resolution, paper type or input source.");
            var legacySyncSucceeded = true;
            try
            {
                await QueueHelperClient.SetUserDefaultsAsync(
                    queueName, pageId, pageWidthMm, pageHeightMm,
                    orientation, resolutionX, resolutionY, borderless);
                await QueueHelperClient.VerifyUserDefaultsAsync(
                    queueName, pageId, pageWidthMm, pageHeightMm,
                    orientation, resolutionX, resolutionY, borderless);
                await QueueHelperClient.NotifyUserDefaultsChangedAsync(queueName);
            }
            catch (Exception error)
            {
                legacySyncSucceeded = false;
                await LogAsync("Legacy DEVMODE synchronization failed after modern PrintTicket commit: "
                    + queueName + "; " + error.GetBaseException().Message);
                await SettingsApplyNotifications.ReportFailureAsync(queueName, error);
            }
            // Notify the Windows print system after changing queue preferences.
            // Keep this on the caller's UI apartment.
            printer.RefreshPrintDeviceCapabilities();
            await LogAsync(
                "User defaults applied: queue=" + queueName
                + "; page=" + pageId + "; orientation=" + orientation
                + "; raster=" + resolutionX + "x" + resolutionY
                + "; readback=" + storedSelection.PageOptionName
                + "; legacyDevModeSynchronized=" + legacySyncSucceeded);
        }

        public static Task SetLegacyUserDefaultsAsync(
            string queueName,
            string pageId,
            double pageWidthMm,
            double pageHeightMm,
            string orientation,
            int resolutionX,
            int resolutionY,
            bool? borderless = null) =>
            QueueHelperClient.SetUserDefaultsAsync(
                queueName, pageId, pageWidthMm, pageHeightMm,
                orientation, resolutionX, resolutionY, borderless);

        public static async Task VerifyAndNotifyLegacyUserDefaultsAsync(
            string queueName,
            string pageId,
            double pageWidthMm,
            double pageHeightMm,
            string orientation,
            int resolutionX,
            int resolutionY,
            bool? borderless = null)
        {
            await QueueHelperClient.VerifyUserDefaultsAsync(
                queueName, pageId, pageWidthMm, pageHeightMm,
                orientation, resolutionX, resolutionY, borderless);
            await QueueHelperClient.NotifyUserDefaultsChangedAsync(queueName);
        }

        private static async Task<WorkflowPrintTicket> ReadInitialUserDefaultPrintTicketAsync(
            IppPrintDevice printer, string queueName, bool allowConfigurationXmlFallback)
        {
            Exception initialError;
            try
            {
                var ticket = printer.UserDefaultPrintTicket;
                if (ticket != null || !allowConfigurationXmlFallback) return ticket;
                initialError = new InvalidDataException("Windows returned no user default PrintTicket.");
            }
            catch (Exception error) when (
                allowConfigurationXmlFallback || error.HResult == unchecked((int)0x80004005))
            {
                initialError = error;
            }
            await LogAsync("Initial UserDefaultPrintTicket read failed: queue=" + queueName
                + "; HRESULT=0x" + initialError.HResult.ToString("X8")
                + "; checking effective DEVMODE");

            Shared.LegacyPrinterDefaults defaults = null;
            Exception legacyError = null;
            try
            {
                defaults = await QueueHelperClient.ReadLegacyUserDefaultsAsync(queueName);
            }
            catch (Exception error) { legacyError = error; }
            if (legacyError == null)
            {
                // A fresh queue can have global defaults without per-user data.
                // Keep the existing guard: do not mask existing user defaults.
                if (defaults != null && defaults.HasUserDevMode)
                    throw new InvalidOperationException(
                        "Initial PrintTicket recovery cannot replace existing per-user defaults.", initialError);
                try
                {
                    if (defaults == null || string.IsNullOrWhiteSpace(defaults.PrintTicketXml))
                        throw new InvalidDataException("Windows returned no effective DEVMODE PrintTicket XML.");
                    var ticket = CreateTicketFromXml(queueName, defaults.PrintTicketXml);
                    await LogAsync("Initial user ticket loaded from effective DEVMODE: queue=" + queueName);
                    return ticket;
                }
                catch (Exception error) { legacyError = error; }
            }
            await LogAsync("Initial effective DEVMODE read failed: queue=" + queueName
                + "; " + legacyError);
            if (!allowConfigurationXmlFallback)
                throw new AggregateException("Both initial printer settings reads failed.", initialError, legacyError);

            try
            {
                // Only an initial P900 seed. Normal validation, the UserDefault
                // setter, and direct post-save readback remain authoritative.
                var xml = await QueueHelperClient.ReadConfigurationTicketXmlAsync(queueName);
                var ticket = CreateTicketFromXml(queueName, xml);
                await LogAsync("Initial user ticket loaded from CIM configuration XML: queue=" + queueName
                    + "; scope=queue; perUserDevMode=absent; xmlCharacters=" + xml.Length);
                return ticket;
            }
            catch (Exception error)
            {
                await LogAsync("Initial configuration XML recovery failed: queue=" + queueName + "; " + error);
                throw new AggregateException("All three initial printer settings reads failed.",
                    initialError, legacyError, error);
            }
        }

        private static WorkflowPrintTicket CreateTicketFromXml(string queueName, string xml)
        {
            using (var stream = new MemoryStream(Encoding.UTF8.GetBytes(xml)))
            using (var input = stream.AsInputStream())
            {
                return new WorkflowPrintTicket(queueName, input);
            }
        }

        private static IppPrintDevice Resolve(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName))
                throw new ArgumentException("Windows queue name is required.", nameof(queueName));
            var printer = IppPrintDevice.FromPrinterName(queueName);
            if (printer == null)
                throw new InvalidOperationException(
                    "Windows did not resolve the virtual printer queue " + queueName + ".");
            return printer;
        }

        private static async Task LogAsync(string message)
        {
            try
            {
                var file = await ApplicationData.Current.LocalFolder.CreateFileAsync(
                    "pyToneRIP-system-defaults.log", CreationCollisionOption.OpenIfExists);
                await FileIO.AppendTextAsync(
                    file, DateTimeOffset.Now.ToString("O") + " | " + message + Environment.NewLine);
            }
            catch
            {
                // Diagnostics must not change printing behavior.
            }
        }
    }
}
