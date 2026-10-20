using System.Collections.Generic;
using System.Linq;
using System.Runtime.Serialization;

namespace pyToneRIP.Models
{
    [DataContract]
    public sealed class CapabilityExport
    {
        [DataMember(Name = "schema")] public string Schema { get; set; }
        [DataMember(Name = "version")] public int Version { get; set; }
        [DataMember(Name = "printers")] public List<PrinterCapability> Printers { get; set; }
        [DataMember(Name = "adjustments")] public AdjustmentRanges Adjustments { get; set; }
        [DataMember(Name = "dither")] public List<CapabilityOption> Dither { get; set; }
        [DataMember(Name = "layout")] public LayoutCapabilities Layout { get; set; }
    }

    [DataContract]
    public sealed class PrinterCapability
    {
        [DataMember(Name = "id")] public string Id { get; set; }
        [DataMember(Name = "display_name")] public string DisplayName { get; set; }
        [DataMember(Name = "backend_family")] public string BackendFamily { get; set; }
        [DataMember(Name = "media_specific_modes")] public bool MediaSpecificModes { get; set; }
        [DataMember(Name = "physical_paths")] public List<PhysicalPathCapability> PhysicalPaths { get; set; }
        [DataMember(Name = "ink_configurations")] public List<InkConfigurationCapability> InkConfigurations { get; set; }
        [DataMember(Name = "default_ink_configuration")] public string DefaultInkConfiguration { get; set; }
        [DataMember(Name = "modes")] public List<CapabilityOption> Modes { get; set; }
        [DataMember(Name = "media")] public List<CapabilityOption> Media { get; set; }
        [DataMember(Name = "media_aliases")] public Dictionary<string, string> MediaAliases { get; set; }
        [DataMember(Name = "pages")] public List<CapabilityOption> Pages { get; set; }
        [DataMember(Name = "input_sources")] public List<CapabilityOption> InputSources { get; set; }
        [DataMember(Name = "device_settings")] public List<DeviceSettingCapability> DeviceSettings { get; set; }
        [DataMember(Name = "maintenance_actions")] public List<MaintenanceActionCapability> MaintenanceActions { get; set; }

        public override string ToString() => DisplayName + " [" + Id + "]";
    }

    [DataContract]
    public sealed class MaintenanceActionCapability
    {
        [DataMember(Name = "id")] public string Id { get; set; }
        [DataMember(Name = "display_name")] public string DisplayName { get; set; }
        [DataMember(Name = "description")] public string Description { get; set; }
        [DataMember(Name = "requires_paper")] public bool RequiresPaper { get; set; }
        [DataMember(Name = "consumes_ink")] public bool ConsumesInk { get; set; }
    }

    [DataContract]
    public sealed class DeviceSettingCapability
    {
        [DataMember(Name = "id")] public string Id { get; set; }
        [DataMember(Name = "display_name")] public string DisplayName { get; set; }
        [DataMember(Name = "default_choice")] public string DefaultChoice { get; set; }
        [DataMember(Name = "choices")] public List<DeviceSettingChoice> Choices { get; set; }
    }

    [DataContract]
    public sealed class DeviceSettingChoice
    {
        [DataMember(Name = "id")] public string Id { get; set; }
        [DataMember(Name = "display_name")] public string DisplayName { get; set; }

        [IgnoreDataMember] public string LocalizedDisplayName { get; set; }
        public override string ToString() => LocalizedDisplayName ?? DisplayName ?? Id;
    }

    [DataContract]
    public sealed class PhysicalPathCapability
    {
        [DataMember(Name = "id")] public string Id { get; set; }
        [DataMember(Name = "factory_role")] public string FactoryRole { get; set; }
        [DataMember(Name = "short_name")] public string ShortName { get; set; }
        [DataMember(Name = "display_name")] public string DisplayName { get; set; }
        [DataMember(Name = "default_logical_ink")] public string DefaultLogicalInk { get; set; }
        [DataMember(Name = "configuration_roles")] public List<PhysicalPathConfigurationRole> ConfigurationRoles { get; set; }

        // Set by the Preferences page before binding. It affects only the
        // human-readable PK/MK label; Id remains the stable routing identity.
        public string ActiveInkConfiguration { get; set; }

        public override string ToString()
        {
            var configured = ConfigurationRoles?.FirstOrDefault(item =>
                string.Equals(item.Configuration, ActiveInkConfiguration,
                    System.StringComparison.OrdinalIgnoreCase));
            var ink = configured == null ? ShortName : InkLabels.ShortName(configured.Role);
            var role = configured?.Role ?? FactoryRole;
            var label = string.IsNullOrWhiteSpace(ink) ? Id : Id + " — " + ink;
            return string.IsNullOrWhiteSpace(role)
                ? label
                : label + " (" + role.Replace('_', ' ') + ")";
        }
    }

    [DataContract]
    public sealed class PhysicalPathConfigurationRole
    {
        [DataMember(Name = "configuration")] public string Configuration { get; set; }
        [DataMember(Name = "role")] public string Role { get; set; }
    }

    [DataContract]
    public sealed class InkConfigurationCapability
    {
        [DataMember(Name = "id")] public string Id { get; set; }
        [DataMember(Name = "display_name")] public string DisplayName { get; set; }
        [DataMember(Name = "active_logical_ink")] public string ActiveLogicalInk { get; set; }
        public override string ToString() => DisplayName ?? Id;
    }

    internal static class InkLabels
    {
        internal static string ShortName(string role)
        {
            switch ((role ?? "").Trim().ToLowerInvariant().Replace(" ", "_"))
            {
                case "photo_black": case "photoblack": return "PK";
                case "matte_black": case "matteblack": return "MK";
                case "light_black": case "lightblack": case "gray": case "grey": return "LK";
                case "light_light_black": case "lightlightblack": case "light_gray": return "LLK";
                case "cyan": return "C";
                case "light_cyan": case "lightcyan": return "LC";
                case "magenta": return "M";
                case "vivid_magenta": case "vividmagenta": return "VM";
                case "light_magenta": case "lightmagenta": return "LM";
                case "vivid_light_magenta": case "vividlightmagenta": return "VLM";
                case "yellow": return "Y";
                case "violet": return "V";
                default: return role?.ToUpperInvariant();
            }
        }
    }

    [DataContract]
    public sealed class CapabilityOption
    {
        [DataMember(Name = "id")] public string Id { get; set; }
        [DataMember(Name = "display_name")] public string DisplayName { get; set; }
        [DataMember(Name = "implemented")] public bool Implemented { get; set; }
        [DataMember(Name = "enabled")] public bool Enabled { get; set; }
        [DataMember(Name = "status")] public string Status { get; set; }
        [DataMember(Name = "blocked_reason")] public string BlockedReason { get; set; }
        [DataMember(Name = "supported_modes")] public List<string> SupportedModes { get; set; }
        [DataMember(Name = "resolution_dpi")] public List<int> ResolutionDpi { get; set; }
        [DataMember(Name = "raster_resolution_dpi")] public List<int> RasterResolutionDpi { get; set; }
        [DataMember(Name = "printer_resolution_dpi")] public List<int> PrinterResolutionDpi { get; set; }
        [DataMember(Name = "output_resolution_dpi")] public List<int> OutputResolutionDpi { get; set; }
        [DataMember(Name = "width_mm")] public double? WidthMm { get; set; }
        [DataMember(Name = "height_mm")] public double? HeightMm { get; set; }
        [DataMember(Name = "borderless")] public bool Borderless { get; set; }
        [DataMember(Name = "allowed_media")] public List<string> AllowedMedia { get; set; }

        public override string ToString()
        {
            var label = DisplayName ?? Id;
            return Enabled ? label : label + " — " + Status;
        }
    }

    [DataContract]
    public sealed class AdjustmentRanges
    {
        [DataMember(Name = "gamma")] public NumericRange Gamma { get; set; }
        [DataMember(Name = "ink_limit")] public NumericRange InkLimit { get; set; }
    }

    [DataContract]
    public sealed class NumericRange
    {
        [DataMember(Name = "minimum")] public int Minimum { get; set; }
        [DataMember(Name = "maximum")] public int Maximum { get; set; }
    }

    [DataContract]
    public sealed class LayoutCapabilities
    {
        [DataMember(Name = "orientations")] public List<string> Orientations { get; set; }
        [DataMember(Name = "resampling")] public List<string> Resampling { get; set; }
        [DataMember(Name = "sizing_modes")] public List<string> SizingModes { get; set; }
        [DataMember(Name = "placement_modes")] public List<string> PlacementModes { get; set; }
        [DataMember(Name = "directions")] public List<string> Directions { get; set; }
    }
}
