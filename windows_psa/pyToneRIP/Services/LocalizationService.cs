using System;
using Windows.ApplicationModel.Resources;
using Windows.ApplicationModel.Resources.Core;
using Windows.Globalization;

namespace pyToneRIP.Services
{
    internal static class LocalizationService
    {
        internal static string CurrentLanguage =>
            Shared.SharedApplicationStorage.LanguageOverride;

        internal static void SetLanguage(string language)
        {
            var selectedLanguage = language ?? string.Empty;
            ApplicationLanguages.PrimaryLanguageOverride = selectedLanguage;
            Shared.SharedApplicationStorage.LanguageOverride = selectedLanguage;

            // PrimaryLanguageOverride is persistent, but propagation into an
            // already-created view context is not guaranteed to be immediate.
            // Set the language qualifier explicitly before the new XAML tree
            // is created so ResourceLoader and x:Uid resolve the same language
            // on the first selection. An empty override restores the computed
            // Windows/app runtime language list.
            System.Collections.Generic.IReadOnlyList<string> languages;
            if (string.IsNullOrWhiteSpace(selectedLanguage))
                languages = ApplicationLanguages.Languages;
            else
                languages = new[] { selectedLanguage };
            var viewContext = ResourceContext.GetForCurrentView();
            var independentContext = ResourceContext.GetForViewIndependentUse();
            viewContext.Reset(new[] { "Language" });
            independentContext.Reset(new[] { "Language" });
            viewContext.Languages = languages;
            independentContext.Languages = languages;
        }

        internal static string Get(string key)
        {
            // MRT compiles segmented RESW names such as "Code.Cancel" into
            // resource paths such as "Code/Cancel". ResourceLoader expects
            // that path form for programmatic lookups; x:Uid performs the
            // equivalent conversion automatically.
            var resourceIdentifier = key?.Replace('.', '/');
            var value = ResourceLoader.GetForCurrentView().GetString(resourceIdentifier);
            if (string.IsNullOrWhiteSpace(value))
                throw new InvalidOperationException("Missing GUI resource: " + key);
            return value;
        }

        internal static string Format(string key, params object[] values) =>
            string.Format(System.Globalization.CultureInfo.CurrentCulture,
                Get(key), values);

        internal static string DeviceSettingName(Models.DeviceSettingCapability setting) =>
            Get("DeviceSetting." + setting.Id);

        internal static string DeviceSettingChoiceName(
            Models.DeviceSettingCapability setting, Models.DeviceSettingChoice choice)
        {
            switch (choice.Id)
            {
                case "Auto": return Get("DeviceChoice.MediaDefault");
                case "Default": return Get(setting.Id == "Platen_Gap"
                    ? "DeviceChoice.MediaDefault" : "DeviceChoice.Default");
                case "Large": return Get(choice.DisplayName == "Wider"
                    ? "DeviceChoice.Wider" : "DeviceChoice.Large");
                case "Off": case "On": case "Wide":
                case "Standard": case "Narrow": case "Model_A": case "Model_B":
                    return Get("DeviceChoice." + choice.Id);
                case "Minus_4": return Format("DeviceChoice.ThinPaper", "-4");
            }
            if (choice.Id.StartsWith("Code_", StringComparison.Ordinal))
            {
                var percent = choice.DisplayName?.IndexOf('%') ?? -1;
                if (setting.Id == "Feed_Adjustment" && percent > 0
                    && double.TryParse(choice.DisplayName.Substring(0, percent),
                        System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture, out double adjustment))
                    return adjustment.ToString("0.00", System.Globalization.CultureInfo.CurrentCulture)
                        + "% (" + Format("DeviceChoice.Code", choice.Id.Substring(5)) + ")";
                // Keep unverified protocol codes explicit; do not invent units.
                if (choice.DisplayName?.EndsWith(" mm", StringComparison.Ordinal) != true)
                    return Format("DeviceChoice.Code", choice.Id.Substring(5));
            }
            var label = choice.DisplayName ?? choice.Id;
            var separator = label.IndexOf(' ');
            if (separator > 0 && double.TryParse(label.Substring(0, separator),
                System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out double number))
                return number.ToString(System.Globalization.CultureInfo.CurrentCulture)
                    + label.Substring(separator);
            return label;
        }
    }
}
