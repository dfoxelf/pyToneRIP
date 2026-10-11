using System;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Windows.Data.Json;
using Windows.Storage;

namespace pyToneRIP.Tasks.Services
{
    internal static class PrinterAssignmentGate
    {
        private const string RegistryFileName = "printer-instances.json";

        public static async Task RequireConfiguredAsync(string printerUri, string printerName)
        {
            JsonObject registry;
            try
            {
                var file = await pyToneRIP.Shared.SharedApplicationStorage.Root.GetFileAsync(
                    RegistryFileName);
                var json = await FileIO.ReadTextAsync(file);
                if (!JsonObject.TryParse(json, out registry))
                    throw new InvalidDataException("Printer instance registry is invalid.");
            }
            catch (FileNotFoundException)
            {
                throw FreeSlot(printerName);
            }

            var instances = registry.GetNamedArray("instances", new JsonArray());
            var instance = instances
                .Select(item => item.GetObject())
                .FirstOrDefault(item =>
                    (!string.IsNullOrWhiteSpace(printerUri) && string.Equals(
                        item.GetNamedString("printer_uri", ""),
                        printerUri,
                        StringComparison.OrdinalIgnoreCase))
                    || (!string.IsNullOrWhiteSpace(printerName) && string.Equals(
                        item.GetNamedString("queue_name", ""),
                        printerName,
                        StringComparison.OrdinalIgnoreCase)));
            if (instance == null
                || !instance.GetNamedBoolean("configured", false)
                || string.IsNullOrWhiteSpace(instance.GetNamedString("model_id", "")))
                throw FreeSlot(printerName);
        }

        private static InvalidOperationException FreeSlot(string printerName) =>
            new InvalidOperationException(
                "Printing is blocked for " + (printerName ?? "this pyToneRIP printer")
                + ". Add and configure this printer in pyToneRIP Printer Manager first.");
    }
}
