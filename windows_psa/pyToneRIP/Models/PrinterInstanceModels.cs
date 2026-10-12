using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.Serialization;
using pyToneRIP.Services;
using Windows.UI;
using Windows.UI.Xaml;
using Windows.UI.Xaml.Media;

namespace pyToneRIP.Models
{
    [DataContract]
    public sealed class PrinterInstanceRegistry
    {
        [DataMember(Name = "schema", Order = 1)] public string Schema { get; set; } = "pytone-rip.printer-instances";
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; } = 1;
        [DataMember(Name = "instances", Order = 3)] public List<PrinterInstanceRecord> Instances { get; set; } = new List<PrinterInstanceRecord>();
    }

    [DataContract]
    public sealed class PrinterInstanceRecord : INotifyPropertyChanged
    {
        private string networkStatus = "Unknown";
        private bool isSystemDefault;
        [DataMember(Name = "printer_uri", Order = 2)] public string PrinterUri { get; set; }
        [DataMember(Name = "queue_name", Order = 3)] public string QueueName { get; set; }
        [DataMember(Name = "model_id", Order = 4)] public string ModelId { get; set; }
        [DataMember(Name = "model_name", Order = 5)] public string ModelName { get; set; }
        [DataMember(Name = "connection_kind", Order = 6)] public string ConnectionKind { get; set; } = "network";
        [DataMember(Name = "host", Order = 7, EmitDefaultValue = false)] public string Host { get; set; }
        [DataMember(Name = "port", Order = 8)] public int Port { get; set; } = 9100;
        [DataMember(Name = "configured", Order = 9)] public bool Configured { get; set; }
        [DataMember(Name = "system_queue_id", Order = 10, EmitDefaultValue = false)] public string SystemQueueId { get; set; }
        [DataMember(Name = "calibration_mode", Order = 11)] public bool CalibrationMode { get; set; }
        [DataMember(Name = "model_capability", Order = 12, EmitDefaultValue = false)] public PrinterCapability ModelCapability { get; set; }

        public string ConnectionSummary => ConnectionKind == "usb"
            ? LocalizationService.Get("Choice.UsbPlanned")
            : (string.IsNullOrWhiteSpace(Host)
                ? LocalizationService.Get("Choice.NetworkNotConfigured")
                : Host + ":" + Port);
        public string ConfigurationStatus => !Configured
            ? LocalizationService.Get("Choice.SetupRequired")
            : CalibrationMode
                ? LocalizationService.Get("Choice.CalibrationRaw")
                : LocalizationService.Get("Choice.Configured");
        public Brush ConfigurationStatusBrush => new SolidColorBrush(CalibrationMode ? Color.FromArgb(255, 127, 255, 255) : Colors.Transparent);
        public Brush ConfigurationStatusTextBrush => new SolidColorBrush(CalibrationMode ? Color.FromArgb(255, 127, 0, 255) : Colors.Black);
        public GridLength ConfigurationColumnWidth { get; set; } = GridLength.Auto;
        public GridLength NetworkColumnWidth { get; set; } = new GridLength(105);
        // This value is deliberately not serialized. Windows is the source of
        // truth for the user's default printer, so Manager refreshes it from
        // GetDefaultPrinterW whenever it reads the live queue list.
        public bool IsSystemDefault
        {
            get => isSystemDefault;
            set
            {
                if (isSystemDefault == value) return;
                isSystemDefault = value;
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(IsSystemDefault)));
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(DefaultPrinterMark)));
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(DefaultPrinterBrush)));
            }
        }
        public string DefaultPrinterMark => IsSystemDefault ? "\u2713" : string.Empty;
        public Brush DefaultPrinterBrush => new SolidColorBrush(
            IsSystemDefault ? Colors.ForestGreen : Colors.Transparent);
        public string NetworkStatus
        {
            get => LocalizationService.Get("Choice." + NormalizeNetworkStatus(networkStatus));
            set
            {
                var normalized = NormalizeNetworkStatus(value);
                if (string.Equals(networkStatus, normalized,
                    System.StringComparison.OrdinalIgnoreCase)) return;
                networkStatus = normalized;
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(NetworkStatus)));
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(NetworkStatusBrush)));
            }
        }
        public Brush NetworkStatusBrush
        {
            get
            {
                switch (NormalizeNetworkStatus(networkStatus))
                {
                    case "Online": return new SolidColorBrush(Colors.ForestGreen);
                    case "Preparing": return new SolidColorBrush(Colors.DarkGoldenrod);
                    case "Queued": return new SolidColorBrush(Colors.SteelBlue);
                    case "Processing": return new SolidColorBrush(Colors.DarkOrange);
                    case "Printing": return new SolidColorBrush(Colors.DarkBlue);
                    case "Offline": return new SolidColorBrush(Colors.Firebrick);
                    case "Checking": return new SolidColorBrush(Colors.DarkOrange);
                    default: return new SolidColorBrush(Colors.Gray);
                }
            }
        }

        private static string NormalizeNetworkStatus(string value)
        {
            switch (value)
            {
                case "Online":
                case "Preparing":
                case "Queued":
                case "Processing":
                case "Printing":
                case "Offline":
                case "Checking":
                case "Unknown":
                    return value;
                default:
                    return "Unknown";
            }
        }

        public event PropertyChangedEventHandler PropertyChanged;
    }

}
