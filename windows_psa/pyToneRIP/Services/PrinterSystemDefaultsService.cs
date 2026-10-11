using System;
using System.Threading.Tasks;
using Windows.ApplicationModel;
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
            string modelId,
            string printerUri,
            string queueName,
            string pageId,
            double pageWidthMm,
            double pageHeightMm,
            string orientation,
            int resolutionX,
            int resolutionY,
            string mediaId,
            string inputSourceId,
            bool? borderless = null)
        {
            // Dynamic VirtualPrinterManager queues are owned by Manager/Core.
            // Therefore the Manager is also the authoritative PSA context and
            // must commit the final PrintTicket itself; no model application is
            // launched and no cross-package AppService is involved.
            if (string.Equals(Package.Current.Id.Name, "pyToneRIP.Manager",
                StringComparison.OrdinalIgnoreCase))
            {
                // Update native defaults before fetching the ticket so that its
                // class-driver DEVMODE snapshot reflects the selected page and DPI.
                await QueueHelperClient.SetUserDefaultsAsync(
                    queueName, pageId, pageWidthMm, pageHeightMm,
                    orientation, resolutionX, resolutionY, borderless);
                await LogAsync("Win32 DEVMODE synchronized before Manager-owned PrintTicket: queue="
                    + queueName + "; model=" + modelId + "; page=" + pageId);
            }

            var printer = Resolve(queueName);
            if (!printer.CanModifyUserDefaultPrintTicket)
                throw new InvalidOperationException(
                    "Windows did not authorize this PSA to modify the queue user default PrintTicket.");
            var ticket = printer.UserDefaultPrintTicket;
            if (ticket == null)
                throw new InvalidOperationException("Windows returned no user default PrintTicket.");
            Shared.PrintTicketSynchronization.Apply(
                ticket, pageId, pageWidthMm, pageHeightMm,
                orientation, resolutionX, resolutionY, mediaId, inputSourceId, borderless);
            var validation = await ticket.ValidateAsync();
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
            // Notify the Windows print system after changing queue preferences.
            // Keep this on the caller's UI apartment.
            printer.RefreshPrintDeviceCapabilities();
            if (string.Equals(Package.Current.Id.Name, "pyToneRIP.Manager",
                StringComparison.OrdinalIgnoreCase))
            {
                await QueueHelperClient.VerifyUserDefaultsAsync(
                    queueName, pageId, pageWidthMm, pageHeightMm,
                    orientation, resolutionX, resolutionY, borderless);
                await QueueHelperClient.NotifyUserDefaultsChangedAsync(queueName);
            }
            await LogAsync(
                "User defaults applied: queue=" + queueName
                + "; page=" + pageId + "; orientation=" + orientation
                + "; raster=" + resolutionX + "x" + resolutionY
                + "; readback=" + storedSelection.PageOptionName);
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
