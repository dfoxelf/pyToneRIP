using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.Serialization.Json;
using System.Threading.Tasks;
using pyToneRIP.Models;
using Windows.Data.Json;
using Windows.Data.Xml.Dom;
using Windows.Globalization;
using Windows.Storage;

namespace pyToneRIP.Services
{
    internal static class CapabilityCatalog
    {
        public static async Task<CapabilityExport> LoadForModelAsync(
            string modelId, PrinterCapability installedModel = null)
        {
            if (string.IsNullOrWhiteSpace(modelId))
                throw new InvalidDataException("Printer model ID is missing.");
            var catalog = await ReadCatalogAsync("capabilities.common.json");
            if (installedModel != null
                && !string.Equals(installedModel.Id, modelId, StringComparison.Ordinal))
                throw new InvalidDataException("Installed printer model does not match its queue.");
            // Persisted capabilities carry display strings from the language
            // active when the queue was created. Read the current package model
            // for every settings activation, then apply the selected PDR.
            var token = new string(modelId.Where(char.IsLetterOrDigit).ToArray());
            if (token.Length == 0 || !char.IsLetter(token[0]))
                token = "Model" + token;
            var file = await StorageFile.GetFileFromApplicationUriAsync(
                new Uri("ms-appx:///Assets/CapabilityModels/model." + token + ".json"));
            PrinterCapability model;
            using (var stream = await file.OpenStreamForReadAsync())
            {
                var serializer = new DataContractJsonSerializer(typeof(PrinterCapability));
                model = (PrinterCapability)serializer.ReadObject(stream);
            }
            if (model == null || !string.Equals(model.Id, modelId, StringComparison.Ordinal))
                throw new InvalidDataException("Printer capability file does not match its queue.");
            await ApplyLocalizedPageNamesAsync(modelId, model.Pages);
            catalog.Printers = new System.Collections.Generic.List<PrinterCapability> { model };
            return catalog;
        }

        private static async Task ApplyLocalizedPageNamesAsync(
            string modelId, IList<CapabilityOption> pages)
        {
            if (pages == null || pages.Count == 0)
                return;

            var indexFile = await StorageFile.GetFileFromApplicationUriAsync(
                new Uri("ms-appx:///Assets/PdrModels/index.json"));
            var indexText = await FileIO.ReadTextAsync(indexFile);
            if (!JsonObject.TryParse(indexText, out JsonObject index)
                || index.GetNamedString("schema", "") != "pytone-rip.model-pdr-index")
                return;

            var models = index.GetNamedObject("models", new JsonObject());
            var modelEntry = models.FirstOrDefault(item => string.Equals(
                item.Key, modelId, StringComparison.OrdinalIgnoreCase));
            if (string.IsNullOrWhiteSpace(modelEntry.Key)
                || modelEntry.Value.ValueType != JsonValueType.Object)
                return;

            var resources = modelEntry.Value.GetObject();
            var filename = FindPdrFilename(resources);
            if (string.IsNullOrWhiteSpace(filename)
                || filename.IndexOfAny(new[] { '/', '\\' }) >= 0
                || filename.Contains(".."))
                return;

            var pdrFile = await StorageFile.GetFileFromApplicationUriAsync(
                new Uri("ms-appx:///Assets/PdrModels/" + filename));
            var document = new XmlDocument();
            document.LoadXml(await FileIO.ReadTextAsync(pdrFile));
            var labels = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var node in document.SelectNodes(
                "/*[local-name()='root']/*[local-name()='data']"))
            {
                var name = node.Attributes.GetNamedItem("name")?.NodeValue?.ToString();
                var value = node.SelectSingleNode("*[local-name()='value']")?.InnerText;
                if (!string.IsNullOrWhiteSpace(name) && !string.IsNullOrWhiteSpace(value))
                    labels[name.Substring(name.LastIndexOf('/') + 1)] = value;
            }

            foreach (var page in pages)
            {
                if (page != null && labels.TryGetValue(PdrPageResourceName(page.Id), out string label))
                    page.DisplayName = label;
            }
            App.WriteStartupStage("Paper resources loaded: model=" + modelId
                + "; selected=" + LocalizationService.CurrentLanguage
                + "; runtimeOverride=" + ApplicationLanguages.PrimaryLanguageOverride
                + "; pdr=" + filename + "; labels=" + labels.Count);
        }

        private static string FindPdrFilename(JsonObject resources)
        {
            var candidates = new List<string>();
            var selectedLanguage = LocalizationService.CurrentLanguage
                .Replace('-', '_');
            AddCandidate(candidates, selectedLanguage);
            var selectedSeparator = selectedLanguage.IndexOf('_');
            if (selectedSeparator > 0)
                AddCandidate(candidates, selectedLanguage.Substring(0, selectedSeparator));
            foreach (var language in ApplicationLanguages.Languages)
            {
                var normalized = (language ?? "").Replace('-', '_');
                AddCandidate(candidates, normalized);
                var separator = normalized.IndexOf('_');
                AddCandidate(candidates, separator < 0
                    ? normalized
                    : normalized.Substring(0, separator));
            }
            AddCandidate(candidates, "en_US");
            AddCandidate(candidates, "en_GB");
            AddCandidate(candidates, "en");
            foreach (var candidate in candidates)
            {
                var resource = resources.FirstOrDefault(item => string.Equals(
                    item.Key, candidate, StringComparison.OrdinalIgnoreCase));
                // Windows can return a neutral tag (for example ru). The
                // packaged PDR index uses locales (ru_RU); do not fall through
                // to English while resources for that language exist.
                if (string.IsNullOrWhiteSpace(resource.Key))
                    resource = resources.FirstOrDefault(item => item.Key.StartsWith(
                        candidate + "_", StringComparison.OrdinalIgnoreCase));
                if (!string.IsNullOrWhiteSpace(resource.Key)
                    && resource.Value.ValueType == JsonValueType.String)
                    return resource.Value.GetString();
            }
            return null;
        }

        private static void AddCandidate(ICollection<string> candidates, string value)
        {
            if (!string.IsNullOrWhiteSpace(value)
                && !candidates.Contains(value, StringComparer.OrdinalIgnoreCase))
                candidates.Add(value);
        }

        private static string PdrPageResourceName(string pageId)
        {
            switch (pageId)
            {
                case "Letter": return "NorthAmericaLetter";
                case "A4": return "ISOA4";
                case "A3": return "ISOA3";
                case "Legal": return "NorthAmericaLegal";
                case "A6": return "ISOA6";
                case "A5": return "ISOA5";
                case "Postcard": return "JapanHagakiPostcard";
                case "ENVC6": return "ISOC6Envelope";
                case "ENV10": return "NorthAmericaNumber10Envelope";
                case "B5": return "JISB5";
                default: return "Page_" + System.Text.RegularExpressions.Regex.Replace(
                    pageId ?? string.Empty, "[^A-Za-z0-9_.-]", "_");
            }
        }

        public static async Task<CapabilityExport> LoadAsync()
        {
            return await ReadCatalogAsync("capabilities.generated.json");
        }

        public static async Task<CapabilityExport> LoadIndexAsync()
        {
            return await ReadCatalogAsync("capabilities.index.json");
        }

        private static async Task<CapabilityExport> ReadCatalogAsync(string filename)
        {
            var file = await StorageFile.GetFileFromApplicationUriAsync(
                new System.Uri("ms-appx:///Assets/" + filename));
            using (var stream = await file.OpenStreamForReadAsync())
            {
                var serializer = new DataContractJsonSerializer(typeof(CapabilityExport));
                var catalog = (CapabilityExport)serializer.ReadObject(stream);
                if (catalog.Schema != "pytone-rip.capabilities" || catalog.Version != 1)
                {
                    throw new InvalidDataException("Unsupported Python capability export schema.");
                }
                return catalog;
            }
        }
    }
}
