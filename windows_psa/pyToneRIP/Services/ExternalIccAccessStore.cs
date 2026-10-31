using System;
using System.Threading.Tasks;
using Windows.Storage;
using Windows.Storage.AccessCache;

namespace pyToneRIP.Shared
{
    internal static class ExternalIccAccessStore
    {
        private static string Token(string path) => "icc-" + QueueIdentity.FileToken(path, null);

        public static string Register(StorageFile file)
        {
            if (file == null || string.IsNullOrWhiteSpace(file.Path)
                || !string.Equals(file.FileType, ".icc", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Select an ICC profile with a filesystem path.");
            StorageApplicationPermissions.FutureAccessList.AddOrReplace(Token(file.Path), file, file.Path);
            return file.Path;
        }

        public static async Task<StorageFile> OpenAsync(string path)
        {
            var access = StorageApplicationPermissions.FutureAccessList;
            return access.ContainsItem(Token(path))
                ? await access.GetFileAsync(Token(path)) : await StorageFile.GetFileFromPathAsync(path);
        }
    }
}
