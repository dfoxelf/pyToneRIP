using System.Collections.Generic;
using System.Runtime.Serialization;

namespace pyToneRIP.Models
{
    [DataContract]
    internal sealed class MaintenanceRuntimeRequest
    {
        [DataMember(Name = "schema", Order = 1)]
        public string Schema { get; set; } = "pytone-rip.maintenance-request";
        [DataMember(Name = "version", Order = 2)] public int Version { get; set; } = 1;
        [DataMember(Name = "action", Order = 3)] public string Action { get; set; }
        [DataMember(Name = "model", Order = 4)] public string Model { get; set; }
        [DataMember(Name = "host", Order = 5)] public string Host { get; set; }
        [DataMember(Name = "port", Order = 6)] public int Port { get; set; } = 9100;
        [DataMember(Name = "mode", Order = 7, EmitDefaultValue = false)] public string Mode { get; set; }
        [DataMember(Name = "media", Order = 8, EmitDefaultValue = false)] public string Media { get; set; }
        [DataMember(Name = "page", Order = 9, EmitDefaultValue = false)] public string Page { get; set; }
        [DataMember(Name = "input_source", Order = 10, EmitDefaultValue = false)] public string InputSource { get; set; }
        [DataMember(Name = "direction", Order = 11, EmitDefaultValue = false)] public string Direction { get; set; }
        [DataMember(Name = "allow_unsupported_ppd_combinations", Order = 12)]
        public bool AllowUnsupportedPpdCombinations { get; set; }
        [DataMember(Name = "device_settings", Order = 13, EmitDefaultValue = false)]
        public Dictionary<string, string> DeviceSettings { get; set; }
        [DataMember(Name = "ink_configuration", Order = 14, EmitDefaultValue = false)]
        public string InkConfiguration { get; set; }
        [DataMember(Name = "logical_inks", Order = 15, EmitDefaultValue = false)]
        public List<string> LogicalInks { get; set; }
        [DataMember(Name = "routing", Order = 16, EmitDefaultValue = false)]
        public Dictionary<string, string> Routing { get; set; }
    }
}
