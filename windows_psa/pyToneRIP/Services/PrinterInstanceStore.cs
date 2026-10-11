using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.Serialization.Json;
using System.Threading.Tasks;
using pyToneRIP.Models;
using Windows.Storage;

namespace pyToneRIP.Services
{
    internal static class PrinterInstanceStore
    {
        public const string FileName = "printer-instances.json";
        public const int RawTcpPort = 9100;

        public static async Task<PrinterInstanceRegistry> ReadAsync()
        {
            try
            {
                var file = await pyToneRIP.Shared.SharedApplicationStorage.Root.GetFileAsync(FileName);
                using (var stream = await file.OpenStreamForReadAsync())
                {
                    var serializer = new DataContractJsonSerializer(typeof(PrinterInstanceRegistry));
                    var result = (PrinterInstanceRegistry)serializer.ReadObject(stream);
                    if (result?.Schema != "pytone-rip.printer-instances" || result.Version != 1)
                        throw new InvalidDataException("Unsupported printer instance registry.");
                    if (result.Instances == null)
                        result.Instances = new System.Collections.Generic.List<PrinterInstanceRecord>();
                    return result;
                }
            }
            catch (FileNotFoundException)
            {
                return new PrinterInstanceRegistry();
            }
        }

        public static async Task WriteAsync(PrinterInstanceRegistry registry)
        {
            ValidateUniqueNetworkHosts(registry);
            var file = await pyToneRIP.Shared.SharedApplicationStorage.Root.CreateFileAsync(
                FileName, CreationCollisionOption.ReplaceExisting);
            using (var stream = await file.OpenStreamForWriteAsync())
            {
                stream.SetLength(0);
                var serializer = new DataContractJsonSerializer(typeof(PrinterInstanceRegistry));
                serializer.WriteObject(stream, registry);
            }
        }

        public static bool TryNormalizeIpv4(string value, out string normalized)
        {
            normalized = null;
            if (string.IsNullOrWhiteSpace(value))
                return false;

            // Do not use IPAddress.TryParse here: it accepts abbreviated forms
            // such as "127.1".  The Manager deliberately accepts only four
            // decimal octets and stores one canonical spelling for comparison.
            var parts = value.Trim().Split('.');
            if (parts.Length != 4)
                return false;
            var octets = new byte[4];
            for (var index = 0; index < parts.Length; index++)
            {
                if (parts[index].Length == 0
                    || !parts[index].All(char.IsDigit)
                    || !byte.TryParse(parts[index], out octets[index]))
                    return false;
            }
            normalized = string.Join(".", octets.Select(valuePart => valuePart.ToString()));
            return true;
        }

        public static void EnsureNetworkHostAvailable(
            PrinterInstanceRegistry registry,
            string host,
            PrinterInstanceRecord excluded = null)
        {
            string normalized;
            if (!TryNormalizeIpv4(host, out normalized))
                throw new InvalidOperationException("Enter a valid IPv4 address.");

            var conflict = (registry?.Instances ?? new List<PrinterInstanceRecord>())
                .FirstOrDefault(item =>
                    !ReferenceEquals(item, excluded)
                    && !string.Equals(item.ConnectionKind, "usb", StringComparison.OrdinalIgnoreCase)
                    && TryNormalizeIpv4(item.Host, out string existing)
                    && string.Equals(existing, normalized, StringComparison.Ordinal));
            if (conflict != null)
                throw new InvalidOperationException(
                    "IPv4 address " + normalized + " is already assigned to "
                    + (conflict.QueueName ?? "another pyToneRIP printer") + ".");
        }

        private static void ValidateUniqueNetworkHosts(PrinterInstanceRegistry registry)
        {
            if (registry?.Instances == null)
                throw new InvalidOperationException("Printer registry is unavailable.");

            var assigned = new Dictionary<string, PrinterInstanceRecord>(
                StringComparer.Ordinal);
            foreach (var item in registry.Instances.Where(value =>
                !string.Equals(value.ConnectionKind, "usb", StringComparison.OrdinalIgnoreCase)
                && !string.IsNullOrWhiteSpace(value.Host)))
            {
                string normalized;
                if (!TryNormalizeIpv4(item.Host, out normalized))
                    throw new InvalidOperationException(
                        "Printer " + (item.QueueName ?? "record")
                        + " has an invalid IPv4 address.");
                if (assigned.TryGetValue(normalized, out PrinterInstanceRecord previous))
                    throw new InvalidOperationException(
                        "IPv4 address " + normalized + " is already assigned to "
                        + (previous.QueueName ?? "another pyToneRIP printer") + ".");
                assigned.Add(normalized, item);
            }
        }

    }
}
