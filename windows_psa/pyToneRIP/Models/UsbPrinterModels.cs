using System;
using System.Collections.Generic;
using System.Runtime.Serialization;
using System.Text.RegularExpressions;

namespace pyToneRIP.Models
{
    // Shared with the desktop helper; discovery does not open a USB interface.
    [DataContract]
    public sealed class UsbPrinterDescriptor
    {
        [DataMember(Name = "device_instance_id", Order = 1)] public string DeviceInstanceId { get; set; }
        [DataMember(Name = "display_name", Order = 2)] public string DisplayName { get; set; }
        [DataMember(Name = "model_name", Order = 3)] public string ModelName { get; set; }
        [DataMember(Name = "port_name", Order = 4)] public string PortName { get; set; }
        [DataMember(Name = "queue_names", Order = 5)] public List<string> QueueNames { get; set; } = new List<string>();
        public override string ToString() => string.IsNullOrWhiteSpace(PortName)
            ? DisplayName : DisplayName + " — " + PortName;
    }

    [DataContract]
    public sealed class UsbInkLevel
    {
        [DataMember(Name = "name", Order = 1)] public string Name { get; set; }
        [DataMember(Name = "percent", Order = 2)] public int Percent { get; set; }
    }

    public static class UsbPrinterModelMatcher
    {
        public static bool Matches(string modelId, string displayName, UsbPrinterDescriptor device)
        {
            return device != null && (MatchesReportedModel(modelId, displayName, device.DisplayName)
                || MatchesReportedModel(modelId, displayName, device.ModelName));
        }

        internal static bool MatchesReportedModel(string modelId, string displayName, string reportedModel)
        {
            var detected = Normalize(reportedModel);
            if (detected.Length == 0) return false;
            var id = Normalize(modelId);
            var label = Normalize(displayName);
            return id == detected || label == detected;
        }

        private static string Normalize(string value)
        {
            var text = (value ?? "").ToUpperInvariant();
            // USBPRINT model tokens may have a CRC after the SERIES suffix.
            text = Regex.Replace(text, @"SERIES[0-9A-F]{0,4}$", "");
            text = Regex.Replace(text, "[^A-Z0-9]", "");
            var prefixes = new[] { "EPSON", "SURECOLOR", "STYLUSPHOTO", "STYLUSPRO", "STYLUS", "ECOTANK", "WORKFORCEPRO", "WORKFORCE", "EXPRESSIONPHOTO", "EXPRESSIONPREMIUM", "EXPRESSIONHOME" };
            // Some USBPRINT IDs repeat the manufacturer, e.g.
            // EPSONEPSON_STYLUS_PHOTO_P50. Strip complete leading names until
            // the model token remains; keep the token itself an exact match.
            bool removedPrefix;
            do
            {
                removedPrefix = false;
                foreach (var prefix in prefixes)
                {
                    if (!text.StartsWith(prefix, StringComparison.Ordinal)) continue;
                    text = text.Substring(prefix.Length);
                    removedPrefix = true;
                    break;
                }
            } while (removedPrefix);
            if (text.StartsWith("SC", StringComparison.Ordinal)) text = text.Substring(2);
            return text.EndsWith("SERIES", StringComparison.Ordinal)
                ? text.Substring(0, text.Length - 6) : text;
        }
    }

    internal static class EpsonRawQueueMatcher
    {
        internal const uint OfflineStatus = 0x00000080;
        internal const uint WorkOfflineAttribute = 0x00000400;
        internal const string RawDriverRequiredMessage =
            "Install the official Epson driver for this printer to use Windows queue (RAW) with pyToneRIP.";

        internal static bool IsOffline(uint status) =>
            (status & (OfflineStatus | 0x00001000u | 0x02000000u)) != 0;

        internal static bool IsPrinting(uint status) => (status & (0x0200u | 0x0400u | 0x4000u)) != 0;

        internal static bool IsUsbPort(string port) => port != null
            && Regex.IsMatch(port, @"^USB\d+$", RegexOptions.IgnoreCase);

        internal static bool IsEpsonClassDriver(string driver) =>
            string.Equals(driver, "Epson ESC/P-R V4 Class Driver", StringComparison.OrdinalIgnoreCase)
            || string.Equals(driver, "Epson ESC/P Standard 2 V4 Class Driver", StringComparison.OrdinalIgnoreCase);

        private static readonly string[] ExcludedPorts =
            { "pytone-rip:", "pytonerip.", "file:", "portprompt:", "nul:", "wdp", "wsd", "ipp:", "ipps:" };

        internal static bool IsCompatible(string name, string port, string driver, bool rawReady, uint status)
        {
            if (!rawReady || (status & 4) != 0 || string.IsNullOrWhiteSpace(name)
                || name.StartsWith("pyToneRIP", StringComparison.OrdinalIgnoreCase)
                || driver?.StartsWith("Epson ", StringComparison.OrdinalIgnoreCase) != true
                || string.IsNullOrWhiteSpace(port))
                return false;
            foreach (var value in port.Split(','))
            {
                var portName = value.Trim();
                if (portName.Length == 0) return false;
                foreach (var prefix in ExcludedPorts)
                    if (portName.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return false;
            }
            return true;
        }
    }

}
