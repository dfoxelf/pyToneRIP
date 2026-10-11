using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;
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
