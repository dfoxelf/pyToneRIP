using System;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace pyToneRIP.Diagnostics
{
    // Shared by the packaged UI and its desktop helper. Inspect only the
    // current process token; never adjust privileges or log account names/SIDs.
    internal static class PrintSettingsContext
    {
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr data, uint length, out uint needed);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint GetLengthSid(IntPtr sid);
        [DllImport("kernel32.dll")]
        private static extern uint GetCurrentProcessId();
        [DllImport("ole32.dll")]
        private static extern int CoGetApartmentType(out int type, out int qualifier);

        internal static string Hash(byte[] data)
        {
            using (var hash = SHA256.Create())
                return BitConverter.ToString(hash.ComputeHash(data)).Replace("-", "");
        }

        private static string TokenValue(int kind, bool user)
        {
            IntPtr buffer = IntPtr.Zero;
            try
            {
                uint needed;
                // GetCurrentProcessToken is the SDK inline pseudo-handle -4;
                // it grants query access and must not be closed (Windows 8+).
                var token = new IntPtr(-4);
                var queried = GetTokenInformation(token, kind, IntPtr.Zero, 0, out needed);
                var queryError = Marshal.GetLastWin32Error();
                if (!queried && queryError != 122)
                    throw new System.ComponentModel.Win32Exception(queryError);
                if (needed < 4 || needed > 65536)
                    throw new InvalidOperationException("Unexpected token data size.");
                buffer = Marshal.AllocHGlobal((int)needed);
                if (!GetTokenInformation(token, kind, buffer, needed, out needed))
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                if (!user && kind != 25) return Marshal.ReadInt32(buffer).ToString(System.Globalization.CultureInfo.InvariantCulture);
                if (needed < 8 || needed < IntPtr.Size) throw new InvalidOperationException("Token SID header is too short.");
                var sid = Marshal.ReadIntPtr(buffer);
                long offset = sid.ToInt64() - buffer.ToInt64();
                if (offset < 0 || offset > needed - 8) throw new InvalidOperationException("Token SID is outside its buffer.");
                var length = GetLengthSid(sid);
                if (length < 8 || length > 256 || offset + length > needed)
                    throw new InvalidOperationException("Unexpected SID size.");
                var bytes = new byte[length];
                Marshal.Copy(sid, bytes, 0, bytes.Length);
                if (kind == 25)
                {
                    if (bytes.Length < 12) throw new InvalidOperationException("Integrity SID is too short.");
                    return BitConverter.ToUInt32(bytes, bytes.Length - 4).ToString(System.Globalization.CultureInfo.InvariantCulture);
                }
                return Hash(bytes);
            }
            catch (Exception error) { return "unavailable-0x" + error.HResult.ToString("X8"); }
            finally { if (buffer != IntPtr.Zero) Marshal.FreeHGlobal(buffer); }
        }

        internal static string Describe(string role, string queueName)
        {
            try
            {
                int apartment, qualifier;
                var apartmentStatus = CoGetApartmentType(out apartment, out qualifier);
                return "Print settings context: role=" + role
                    + "; queueHash=" + Hash(Encoding.UTF8.GetBytes((queueName ?? "").ToUpperInvariant()))
                    + "; processId=" + GetCurrentProcessId()
                    + "; processBits=" + (IntPtr.Size * 8)
                    + "; userHash=" + TokenValue(1, true)
                    + "; sessionId=" + TokenValue(12, false)
                    + "; elevated=" + TokenValue(20, false)
                    + "; integrity=" + TokenValue(25, false)
                    + "; appContainer=" + TokenValue(29, false)
                    + "; apartment=" + apartment + "; apartmentQualifier=" + qualifier
                    + "; apartmentStatus=0x" + apartmentStatus.ToString("X8");
            }
            catch (Exception error) { return "Print settings context unavailable; HRESULT=0x" + error.HResult.ToString("X8"); }
        }
    }
}
