using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using Windows.Storage;
using Windows.Storage.AccessCache;

namespace pyToneRIP.Shared
{
    internal static class ExternalCurveAccessStore
    {
        private const string TokenPrefix = "curve-";

        public static string Register(StorageFile file)
        {
            Validate(file);
            StorageApplicationPermissions.FutureAccessList.AddOrReplace(
                Token(file.Path),
                file,
                file.Path);
            return file.Path;
        }

        public static async Task<StorageFile> OpenCurrentAsync(string path)
        {
            if (string.IsNullOrWhiteSpace(path))
                throw new InvalidDataException("Curve path is empty.");
            var token = Token(path);
            if (StorageApplicationPermissions.FutureAccessList.ContainsItem(token))
            {
                try
                {
                    var remembered = await StorageApplicationPermissions.FutureAccessList
                        .GetFileAsync(token);
                    Validate(remembered);
                    return remembered;
                }
                catch (FileNotFoundException)
                {
                    StorageApplicationPermissions.FutureAccessList.Remove(token);
                }
            }

            var direct = await StorageFile.GetFileFromPathAsync(path);
            Validate(direct);
            return direct;
        }

        public static async Task<IReadOnlyList<string>> ReadLogicalInksAsync(
            string path, bool excludeZeroCurves = false)
        {
            var file = await OpenCurrentAsync(path);
            return ParseLogicalInks(await FileIO.ReadTextAsync(file), excludeZeroCurves);
        }

        internal static IReadOnlyList<string> ParseLogicalInks(
            string text, bool excludeZeroCurves = false)
        {
            List<string> declared = null;
            var markers = new List<string>();
            foreach (var raw in (text ?? "").Split('\n'))
            {
                var line = raw.Trim().TrimStart('\uFEFF');
                var header = Regex.Match(line, @"^##\s*\S*RIP\s+(.+?)\s*$", RegexOptions.IgnoreCase);
                if (header.Success)
                {
                    if (declared != null) throw new InvalidDataException("Duplicate QUAD ink header.");
                    var value = header.Groups[1].Value.Trim();
                    declared = value.Contains(",")
                        ? value.Split(',').Select(item => item.Trim()).ToList()
                        : value.Select(item => item.ToString()).ToList();
                }
                var marker = Regex.Match(line, @"^#\s*([^#\s]+)\s+curve\s*$", RegexOptions.IgnoreCase);
                if (marker.Success) markers.Add(marker.Groups[1].Value);
            }
            var inks = declared ?? markers;
            if (inks.Count == 0 || inks.Any(string.IsNullOrWhiteSpace)
                || inks.Distinct(StringComparer.Ordinal).Count() != inks.Count)
                throw new InvalidDataException("QUAD has no valid logical ink declaration.");
            if (declared != null && markers.Count > 0 && !declared.SequenceEqual(markers))
                throw new InvalidDataException("QUAD header ink order does not match curve markers.");
            if (!excludeZeroCurves) return inks;

            // Declarations also contain unused ink tables. Inspect every sample,
            // since a useful curve can return to zero at either endpoint.
            var counts = new int[inks.Count];
            var nonzero = new bool[inks.Count];
            int index = -1;
            foreach (var raw in (text ?? "").Split('\n'))
            {
                var line = raw.Trim().TrimStart('\uFEFF');
                var marker = Regex.Match(line, @"^#\s*([^#\s]+)\s+curve\s*$", RegexOptions.IgnoreCase);
                if (marker.Success)
                {
                    index = inks.IndexOf(marker.Groups[1].Value);
                    continue;
                }
                if (line.Length == 0 || line.StartsWith("#") || line.StartsWith("\"#")) continue;
                if (markers.Count == 0 && (index < 0 || counts[index] == 256)) index++;
                if (index < 0 || index >= inks.Count || counts[index] >= 256
                    || !Regex.IsMatch(line, @"^[0-9]+$")
                    || !ushort.TryParse(line, out ushort sample))
                    throw new InvalidDataException("QUAD has an invalid curve sample.");
                counts[index]++;
                nonzero[index] |= sample != 0;
            }
            if (counts.Any(count => count != 256))
                throw new InvalidDataException("Each QUAD curve must contain exactly 256 samples.");
            var active = inks.Where((ink, position) => nonzero[position]).ToList();
            if (active.Count == 0)
                throw new InvalidDataException("QUAD contains only zero curves.");
            return active;
        }

        private static void Validate(StorageFile file)
        {
            if (file == null)
                throw new ArgumentNullException(nameof(file));
            if (!string.Equals(file.FileType, ".quad", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Only compiled .quad profiles can be selected.");
            if (string.IsNullOrWhiteSpace(file.Path))
                throw new InvalidDataException("The selected curve has no filesystem path.");
        }

        private static string Token(string path)
        {
            var normalized = Path.GetFullPath(path).Trim().ToUpperInvariant();
            using (var sha = SHA256.Create())
            {
                var hash = sha.ComputeHash(Encoding.UTF8.GetBytes(normalized));
                var result = new StringBuilder(TokenPrefix, TokenPrefix.Length + 64);
                foreach (var value in hash)
                    result.Append(value.ToString("x2"));
                return result.ToString();
            }
        }
    }
}
