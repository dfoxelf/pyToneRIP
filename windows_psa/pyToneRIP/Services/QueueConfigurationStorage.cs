using System;
using System.IO;
using System.Threading.Tasks;
using Windows.Storage;

namespace pyToneRIP.Shared
{
    internal static class QueueConfigurationStorage
    {
        public const string LegacyConfigurationFileName = "configuration.current.json";
        public const string ConfigurationFolderName = "configurations";

        public static string FileName(string printerUri, string printerName) =>
            "configuration." + QueueIdentity.FileToken(printerUri, printerName) + ".json";

        public static async Task<string> ReadAsync(
            string printerUri,
            string printerName,
            bool allowLegacyFallback = true)
        {
            try
            {
                var folder = await SharedApplicationStorage.Root.GetFolderAsync(
                    ConfigurationFolderName);
                var file = await folder.GetFileAsync(FileName(printerUri, printerName));
                return await FileIO.ReadTextAsync(file);
            }
            catch (FileNotFoundException)
            {
                if (!allowLegacyFallback || !CanUseLegacyConfiguration(printerUri, printerName))
                    return null;
            }

            try
            {
                var legacy = await SharedApplicationStorage.Root.GetFileAsync(
                    LegacyConfigurationFileName);
                return await FileIO.ReadTextAsync(legacy);
            }
            catch (FileNotFoundException)
            {
                return null;
            }
        }

        private static bool CanUseLegacyConfiguration(string printerUri, string printerName)
        {
            var uri = (printerUri ?? "").Trim().ToLowerInvariant();
            if (uri == "pytone-rip:queue")
                return true;
            return string.IsNullOrWhiteSpace(uri)
                && string.Equals(
                    (printerName ?? "").Trim(),
                    "pyToneRIP",
                    StringComparison.OrdinalIgnoreCase);
        }

        public static async Task WriteAsync(
            string printerUri,
            string printerName,
            string configurationJson)
        {
            var folder = await SharedApplicationStorage.Root.CreateFolderAsync(
                ConfigurationFolderName,
                CreationCollisionOption.OpenIfExists);
            var file = await folder.CreateFileAsync(
                FileName(printerUri, printerName),
                CreationCollisionOption.ReplaceExisting);
            await FileIO.WriteTextAsync(file, configurationJson);
        }

        public static async Task DeleteAsync(string printerUri, string printerName)
        {
            try
            {
                var folder = await SharedApplicationStorage.Root.GetFolderAsync(
                    ConfigurationFolderName);
                var file = await folder.GetFileAsync(FileName(printerUri, printerName));
                await file.DeleteAsync(StorageDeleteOption.PermanentDelete);
            }
            catch (FileNotFoundException)
            {
            }
        }
    }
}
