using System;
using System.Security.Cryptography;
using System.Text;

namespace pyToneRIP.Shared
{
    internal static class QueueIdentity
    {
        public static string StableKey(string printerUri, string printerName)
        {
            if (!string.IsNullOrWhiteSpace(printerUri))
                return "uri:" + printerUri.Trim().ToLowerInvariant();
            if (!string.IsNullOrWhiteSpace(printerName))
                return "name:" + printerName.Trim().ToLowerInvariant();
            throw new InvalidOperationException("Printer URI and printer name are both missing.");
        }

        public static string FileToken(string printerUri, string printerName)
        {
            var bytes = Encoding.UTF8.GetBytes(StableKey(printerUri, printerName));
            using (var hash = SHA256.Create())
            {
                var digest = hash.ComputeHash(bytes);
                var result = new StringBuilder(digest.Length * 2);
                foreach (var value in digest)
                    result.Append(value.ToString("x2"));
                return result.ToString();
            }
        }
    }
}
