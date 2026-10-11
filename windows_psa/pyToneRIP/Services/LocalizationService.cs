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
    }
}
