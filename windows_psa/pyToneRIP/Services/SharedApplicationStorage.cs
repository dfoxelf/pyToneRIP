using Windows.Storage;
using Windows.Globalization;

namespace pyToneRIP.Shared
{
    internal static class SharedApplicationStorage
    {
        public const string PublisherFolderName = "pyToneRIP.Shared";

        // The print extension runs in a separate process. Keep the user's
        // explicit choice in package settings, rather than relying on that
        // process's already-initialized ApplicationLanguages list.
        public static string LanguageOverride
        {
            get
            {
                var values = ApplicationData.Current.LocalSettings.Values;
                return values.ContainsKey("SelectedLanguage")
                    ? values["SelectedLanguage"] as string ?? string.Empty
                    : ApplicationLanguages.PrimaryLanguageOverride ?? string.Empty;
            }
            set => ApplicationData.Current.LocalSettings.Values["SelectedLanguage"] =
                value ?? string.Empty;
        }

        public static StorageFolder Root
        {
            get
            {
                try
                {
                    return ApplicationData.Current.GetPublisherCacheFolder(
                        PublisherFolderName);
                }
                catch
                {
                    // Legacy 1.0.0.47 has no publisherCacheFolders manifest
                    // declaration. Keep its checkpoint build readable while
                    // generated Manager/model packages use the shared folder.
                    return ApplicationData.Current.LocalFolder;
                }
            }
        }
    }
}
