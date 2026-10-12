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

    public static class UsbPrinterModelMatcher
    {
        public static bool Matches(string modelId, string displayName, UsbPrinterDescriptor device)
        {
            if (device == null) return false;
            var id = Normalize(modelId);
            var label = Normalize(displayName);
            var detected = Normalize(device.DisplayName);
            var hardware = Normalize(device.ModelName);
            return (!string.IsNullOrEmpty(id) && (id == detected || id == hardware))
                || (!string.IsNullOrEmpty(label) && (label == detected || label == hardware));
        }

        private static string Normalize(string value)
        {
            var text = (value ?? "").ToUpperInvariant();
            // USBPRINT model tokens may have a CRC after the SERIES suffix.
            text = Regex.Replace(text, @"SERIES[0-9A-F]{0,4}$", "");
            text = Regex.Replace(text, "[^A-Z0-9]", "");
            foreach (var prefix in new[] { "EPSON", "SURECOLOR", "STYLUSPHOTO", "STYLUSPRO", "STYLUS", "ECOTANK", "WORKFORCE", "EXPRESSIONPHOTO", "EXPRESSIONPREMIUM", "EXPRESSIONHOME" })
                if (text.StartsWith(prefix, StringComparison.Ordinal)) text = text.Substring(prefix.Length);
            if (text.StartsWith("SC", StringComparison.Ordinal)) text = text.Substring(2);
            return text.EndsWith("SERIES", StringComparison.Ordinal)
                ? text.Substring(0, text.Length - 6) : text;
        }
    }

}
