using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Windows.Data.Json;
using Windows.Data.Xml.Dom;
using Windows.Globalization;
using Windows.Storage;

namespace pyToneRIP.Tasks.Services
{
    internal sealed class ModelPdcBundle
    {
        public XmlDocument Capabilities { get; set; }
        public XmlDocument Resources { get; set; }
        public string ModelId { get; set; }
        public string ResourcesFilename { get; set; }
    }

    internal static class ModelPdcProvider
    {
        private const string RegistryFileName = "printer-instances.json";
        private const string ModelIndexUri = "ms-appx:///Assets/PdcModels/index.json";
        private const string PdrIndexUri = "ms-appx:///Assets/PdrModels/index.json";

        public static async Task<ModelPdcBundle> LoadForQueueAsync(
            string printerUri,
            string printerName,
            string resourceLanguage)
        {
            var indexFile = await StorageFile.GetFileFromApplicationUriAsync(
                new Uri(ModelIndexUri));
            var indexText = await FileIO.ReadTextAsync(indexFile);
            if (!JsonObject.TryParse(indexText, out JsonObject index)
                || index.GetNamedString("schema", "") != "pytone-rip.model-pdc-index"
                || index.GetNamedNumber("version", 0) != 1)
                throw new InvalidDataException("Packaged model PDC index is invalid.");

            var modelId = await FindAssignedModelIdAsync(printerUri, printerName);
            if (string.IsNullOrWhiteSpace(modelId))
                modelId = FindModelIdFromDynamicUri(index, printerUri);
            if (string.IsNullOrWhiteSpace(modelId))
                throw new InvalidDataException(
                    "The managed printer queue has no registered model identity.");

            var pdcFilename = FindFilename(index, "models", modelId);
            var pdrIndexFile = await StorageFile.GetFileFromApplicationUriAsync(
                new Uri(PdrIndexUri));
            var pdrIndexText = await FileIO.ReadTextAsync(pdrIndexFile);
            if (!JsonObject.TryParse(pdrIndexText, out JsonObject pdrIndex)
                || pdrIndex.GetNamedString("schema", "") != "pytone-rip.model-pdr-index"
                || pdrIndex.GetNamedNumber("version", 0) != 1)
                throw new InvalidDataException("Packaged model PDR index is invalid.");
            var pdrFilename = FindPdrFilename(pdrIndex, modelId, resourceLanguage);
            return new ModelPdcBundle
            {
                Capabilities = await LoadXmlAsync(
                    "ms-appx:///Assets/PdcModels/" + pdcFilename),
                Resources = await LoadXmlAsync(
                    "ms-appx:///Assets/PdrModels/" + pdrFilename),
                ModelId = modelId,
                ResourcesFilename = pdrFilename,
            };
        }

        private static string FindPdrFilename(
            JsonObject index, string modelId, string resourceLanguage)
        {
            var models = index.GetNamedObject("models", new JsonObject());
            var model = models.FirstOrDefault(item => string.Equals(
                item.Key, modelId, StringComparison.OrdinalIgnoreCase));
            if (string.IsNullOrWhiteSpace(model.Key)
                || model.Value.ValueType != JsonValueType.Object)
                throw new InvalidDataException("No packaged PDR exists for " + modelId + ".");

            var resources = model.Value.GetObject();
            var languages = new List<string>();
            // The Manager's explicit choice controls its printer labels. When
            // it is unset, use the language requested by the Windows print UI.
            AddLanguage(languages, pyToneRIP.Shared.SharedApplicationStorage.LanguageOverride);
            AddLanguage(languages, resourceLanguage);
            foreach (var language in ApplicationLanguages.Languages)
                AddLanguage(languages, language);
            AddLanguage(languages, "en_US");
            AddLanguage(languages, "en_GB");
            AddLanguage(languages, "en");

            foreach (var language in languages)
            {
                var entry = resources.FirstOrDefault(item => string.Equals(
                    item.Key, language, StringComparison.OrdinalIgnoreCase));
                if (string.IsNullOrWhiteSpace(entry.Key))
                    entry = resources.FirstOrDefault(item => item.Key.StartsWith(
                        language + "_", StringComparison.OrdinalIgnoreCase));
                if (string.IsNullOrWhiteSpace(entry.Key)
                    || entry.Value.ValueType != JsonValueType.String)
                    continue;
                var filename = entry.Value.GetString();
                if (string.IsNullOrWhiteSpace(filename)
                    || filename.IndexOfAny(new[] { '/', '\\' }) >= 0
                    || filename.Contains(".."))
                    throw new InvalidDataException("Packaged PDR filename is invalid.");
                return filename;
            }
            throw new InvalidDataException("No usable model PDR exists for " + modelId + ".");
        }

        private static void AddLanguage(List<string> languages, string language)
        {
            if (string.IsNullOrWhiteSpace(language)) return;
            var normalized = language.Replace('-', '_');
            if (!languages.Contains(normalized, StringComparer.OrdinalIgnoreCase))
                languages.Add(normalized);
            var separator = normalized.IndexOf('_');
            if (separator > 0)
            {
                var primary = normalized.Substring(0, separator);
                if (!languages.Contains(primary, StringComparer.OrdinalIgnoreCase))
                    languages.Add(primary);
            }
        }

        private static string FindModelIdFromDynamicUri(
            JsonObject index,
            string printerUri)
        {
            if (string.IsNullOrWhiteSpace(printerUri))
                return null;
            var models = index.GetNamedObject("models", new JsonObject());
            foreach (var model in models)
            {
                var token = new string(model.Key.Where(char.IsLetterOrDigit).ToArray())
                    .ToLowerInvariant();
                if (printerUri.StartsWith(
                    "pytone-rip:" + token + "/",
                    StringComparison.OrdinalIgnoreCase))
                    return model.Key;
            }
            return null;
        }

        private static string FindFilename(JsonObject index, string section, string modelId)
        {
            var values = index.GetNamedObject(section, new JsonObject());
            var property = values.FirstOrDefault(item => string.Equals(
                item.Key, modelId, StringComparison.OrdinalIgnoreCase));
            if (string.IsNullOrWhiteSpace(property.Key))
                throw new InvalidDataException(
                    "No packaged " + section + " entry exists for " + modelId + ".");
            var filename = property.Value.GetString();
            if (string.IsNullOrWhiteSpace(filename)
                || filename.IndexOfAny(new[] { '/', '\\' }) >= 0
                || filename.Contains(".."))
                throw new InvalidDataException("Packaged model resource filename is invalid.");
            return filename;
        }

        private static async Task<string> FindAssignedModelIdAsync(
            string printerUri,
            string printerName)
        {
            try
            {
                var file = await pyToneRIP.Shared.SharedApplicationStorage.Root.GetFileAsync(
                    RegistryFileName);
                var text = await FileIO.ReadTextAsync(file);
                if (!JsonObject.TryParse(text, out JsonObject registry))
                    throw new InvalidDataException("Printer instance registry is invalid.");
                var instance = registry.GetNamedArray("instances", new JsonArray())
                    .Select(item => item.GetObject())
                    .FirstOrDefault(item =>
                        (!string.IsNullOrWhiteSpace(printerUri) && string.Equals(
                            item.GetNamedString("printer_uri", ""), printerUri,
                            StringComparison.OrdinalIgnoreCase))
                        || (!string.IsNullOrWhiteSpace(printerName) && string.Equals(
                            item.GetNamedString("queue_name", ""), printerName,
                            StringComparison.OrdinalIgnoreCase)));
                return instance?.GetNamedString("model_id", "");
            }
            catch (FileNotFoundException)
            {
                return null;
            }
        }

        private static async Task<XmlDocument> LoadXmlAsync(string uri)
        {
            var file = await StorageFile.GetFileFromApplicationUriAsync(new Uri(uri));
            var text = await FileIO.ReadTextAsync(file);
            var document = new XmlDocument();
            document.LoadXml(text);
            return document;
        }
    }
}
