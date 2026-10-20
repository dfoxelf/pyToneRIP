using System.Collections.Generic;
using System.Runtime.Serialization;

namespace pyToneRIP.Models
{
    [DataContract]
    public sealed class PyToneRipConfiguration
    {
        [DataMember(Name = "schema", Order = 1)] public string Schema { get; set; } = "pytone-rip.configuration";
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; } = 1;
        [DataMember(Name = "printer", Order = 3)] public PrinterSelection Printer { get; set; }
        [DataMember(Name = "profiles", Order = 4)] public ProfileSelection Profiles { get; set; }
        [DataMember(Name = "adjustments", Order = 5)] public AdjustmentSelection Adjustments { get; set; }
        [DataMember(Name = "channels", Order = 6)] public ChannelSelection Channels { get; set; }
        [DataMember(Name = "layout", Order = 7)] public LayoutSelection Layout { get; set; }
        [DataMember(Name = "connection", Order = 8, EmitDefaultValue = false)] public ConnectionSelection Connection { get; set; }
        [DataMember(Name = "calibration", Order = 9, EmitDefaultValue = false)] public CalibrationSelection Calibration { get; set; }
        // Online emits validated passes as they are produced. Strict offline
        // retains the complete native validation gate before any TCP connect.
        [DataMember(Name = "delivery_mode", Order = 10, EmitDefaultValue = false)]
        public string DeliveryMode { get; set; } = "online";
    }

    [DataContract]
    public sealed class PrinterSelection
    {
        [DataMember(Name = "model", Order = 1)] public string Model { get; set; }
        [DataMember(Name = "mode", Order = 2)] public string Mode { get; set; }
        [DataMember(Name = "media", Order = 3)] public string Media { get; set; }
        [DataMember(Name = "page", Order = 4)] public string Page { get; set; }
        [DataMember(Name = "input_source", Order = 5)] public string InputSource { get; set; }
        [DataMember(Name = "direction", Order = 6)] public string Direction { get; set; }
        [DataMember(Name = "allow_unsupported_ppd_combinations", Order = 7)]
        public bool AllowUnsupportedPpdCombinations { get; set; }
        [DataMember(Name = "device_settings", Order = 8, EmitDefaultValue = false)]
        public Dictionary<string, string> DeviceSettings { get; set; }
        [DataMember(Name = "ink_configuration", Order = 9, EmitDefaultValue = false)]
        public string InkConfiguration { get; set; }
        // Persist the Windows raster choice per queue. PrintSupport sessions
        // provide it directly, while Manager Preferences must restore it from
        // this queue-owned configuration.
        [DataMember(Name = "raster_resolution_x", Order = 10, EmitDefaultValue = false)]
        public int? RasterResolutionX { get; set; }
        [DataMember(Name = "raster_resolution_y", Order = 11, EmitDefaultValue = false)]
        public int? RasterResolutionY { get; set; }
    }

    [DataContract]
    public sealed class ProfileSelection
    {
        [DataMember(Name = "curves", Order = 1)] public List<string> Curves { get; set; }
        [DataMember(Name = "blend", Order = 2)] public List<int> Blend { get; set; }
        [DataMember(Name = "split_tone", Order = 3)] public bool SplitTone { get; set; }
        [DataMember(Name = "highlights", Order = 4)] public List<int> Highlights { get; set; }
        [DataMember(Name = "midtones", Order = 5)] public List<int> Midtones { get; set; }
        [DataMember(Name = "shadows", Order = 6)] public List<int> Shadows { get; set; }
    }

    [DataContract]
    public sealed class AdjustmentSelection
    {
        [DataMember(Name = "gamma", Order = 1)] public int Gamma { get; set; }
        [DataMember(Name = "ink_limit", Order = 2)] public int InkLimit { get; set; }
        [DataMember(Name = "dither", Order = 3)] public string Dither { get; set; }
    }

    [DataContract]
    public sealed class ChannelSelection
    {
        [DataMember(Name = "logical_inks", Order = 1)] public List<string> LogicalInks { get; set; }
        [DataMember(Name = "routing", Order = 2)] public Dictionary<string, string> Routing { get; set; }
    }

    [DataContract]
    public sealed class LayoutSelection
    {
        [DataMember(Name = "sizing", Order = 1)] public string Sizing { get; set; }
        [DataMember(Name = "width_mm", Order = 2)] public double? WidthMm { get; set; }
        [DataMember(Name = "height_mm", Order = 3)] public double? HeightMm { get; set; }
        [DataMember(Name = "scale_percent", Order = 4)] public double? ScalePercent { get; set; }
        [DataMember(Name = "orientation", Order = 5)] public string Orientation { get; set; }
        [DataMember(Name = "placement", Order = 6)] public string Placement { get; set; }
        [DataMember(Name = "x_mm", Order = 7)] public double? XMm { get; set; }
        [DataMember(Name = "y_mm", Order = 8)] public double? YMm { get; set; }
        [DataMember(Name = "resampling", Order = 9)] public string Resampling { get; set; }
        [DataMember(Name = "apply_rip_layout", Order = 10)] public bool ApplyRipLayout { get; set; }
    }

    [DataContract]
    public sealed class ConnectionSelection
    {
        [DataMember(Name = "kind", Order = 1)] public string Kind { get; set; } = "network";
        [DataMember(Name = "host", Order = 2, EmitDefaultValue = false)] public string Host { get; set; }
        [DataMember(Name = "port", Order = 3)] public int Port { get; set; } = 9100;
        [DataMember(Name = "usb_port", Order = 4, EmitDefaultValue = false)] public string UsbPort { get; set; }
        [DataMember(Name = "usb_device_id", Order = 5, EmitDefaultValue = false)] public string UsbDeviceId { get; set; }
        [DataMember(Name = "printer_queue", Order = 6, EmitDefaultValue = false)] public string PhysicalQueueName { get; set; }
        [DataMember(Name = "usb_transport_mode", Order = 7, EmitDefaultValue = false)] public string UsbTransportMode { get; set; } = "usbprint";
    }

    [DataContract]
    public sealed class CalibrationSelection
    {
        [DataMember(Name = "enabled", Order = 1)] public bool Enabled { get; set; }
        [DataMember(Name = "mode", Order = 2)] public string Mode { get; set; } = "rgb_selectors";
        [DataMember(Name = "ink_limit", Order = 3)] public int InkLimit { get; set; } = 100;
    }

    public sealed class RouteEditorRow
    {
        public string LogicalInk { get; set; }
        public string PhysicalPath { get; set; }
    }
}
