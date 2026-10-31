using System;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using pyToneRIP.Shared;
using Windows.Data.Json;
using Windows.Storage;

namespace pyToneRIP.Services
{
    // Explicit user assignments, not a claim inferred from ICC descriptions.
    internal static class PreviewProfileAssociations
    {
        private const string FileName = "preview-profile-associations.json";

        private static string Hash(byte[] bytes)
        {
            using (var sha = SHA256.Create())
                return string.Concat(sha.ComputeHash(bytes).Select(value => value.ToString("x2")));
        }

        private static string Canonical(IJsonValue value)
        {
            if (value.ValueType == JsonValueType.Object)
                return "{" + string.Join(",", value.GetObject().OrderBy(pair => pair.Key, StringComparer.Ordinal)
                    .Select(pair => JsonValue.CreateStringValue(pair.Key).Stringify() + ":" + Canonical(pair.Value))) + "}";
            if (value.ValueType == JsonValueType.Array)
                return "[" + string.Join(",", value.GetArray().Select(Canonical)) + "]";
            return value.Stringify();
        }

        public static async Task<string> FingerprintAsync(JsonObject configuration)
        {
            var conditions = new JsonObject();
            foreach (var key in new[] { "printer", "profiles", "channels", "adjustments" })
                conditions[key] = JsonValue.Parse(configuration.GetNamedObject(key).Stringify());
            var printer = conditions.GetNamedObject("printer");
            printer.Remove("raster_resolution_x"); printer.Remove("raster_resolution_y");
            var curves = conditions.GetNamedObject("profiles").GetNamedArray("curves");
            for (var i = 0; i < curves.Count; i++)
            {
                var curve = await ExternalCurveAccessStore.OpenCurrentAsync(curves[i].GetString());
                using (var input = await curve.OpenStreamForReadAsync())
                using (var sha = SHA256.Create())
                    curves[i] = JsonValue.CreateStringValue(string.Concat(sha.ComputeHash(input).Select(value => value.ToString("x2"))));
            }
            return Hash(Encoding.UTF8.GetBytes(Canonical(conditions)));
        }

        public static async Task<string> ProfileHashAsync(StorageFile profile)
        {
            using (var input = await profile.OpenStreamForReadAsync())
            using (var sha = SHA256.Create())
                return string.Concat(sha.ComputeHash(input).Select(value => value.ToString("x2")));
        }

        private static async Task<JsonObject> ReadAsync()
        {
            try { return JsonObject.Parse(await FileIO.ReadTextAsync(await SharedApplicationStorage.Root.GetFileAsync(FileName))); }
            catch (FileNotFoundException) { return new JsonObject(); }
        }

        public static async Task RememberAsync(JsonObject configuration)
        {
            var preview = configuration.GetNamedObject("preview");
            var path = preview.GetNamedString("icc_path", "");
            if (string.IsNullOrWhiteSpace(path)) return;
            var fingerprint = await FingerprintAsync(configuration);
            var hash = await ProfileHashAsync(await ExternalIccAccessStore.OpenAsync(path));
            preview["condition_fingerprint"] = JsonValue.CreateStringValue(fingerprint);
            preview["icc_sha256"] = JsonValue.CreateStringValue(hash);
            var entries = await ReadAsync();
            entries[fingerprint] = JsonObject.Parse(preview.Stringify());
            await PreviewWorkflowStore.WriteAtomicAsync(SharedApplicationStorage.Root, FileName, entries.Stringify());
        }

        public static async Task<bool> RestoreAsync(JsonObject configuration)
        {
            var preview = configuration.GetNamedObject("preview");
            var fingerprint = await FingerprintAsync(configuration);
            var entries = await ReadAsync();
            var saved = entries.GetNamedObject(fingerprint, null);
            if (saved == null) return false;
            try
            {
                var path = saved.GetNamedString("icc_path");
                if (await ProfileHashAsync(await ExternalIccAccessStore.OpenAsync(path)) != saved.GetNamedString("icc_sha256")) return false;
                foreach (var key in new[] { "icc_path", "condition_fingerprint", "icc_sha256" }) preview[key] = saved[key];
                return true;
            }
            catch (FileNotFoundException) { return false; }
        }
    }
}
