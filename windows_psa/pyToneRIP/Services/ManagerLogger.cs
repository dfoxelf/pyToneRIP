using System;
using System.Globalization;
using System.Text;
using Windows.Globalization.DateTimeFormatting;

namespace pyToneRIP.Services
{
    internal static class ManagerLogger
    {
        private static readonly object Sync = new object();
        private static readonly StringBuilder SessionLog = new StringBuilder();
        private static readonly DateTimeFormatter ShortDateFormatter = DateTimeFormatter.ShortDate;
        private static readonly DateTimeFormatter LongTimeFormatter = DateTimeFormatter.LongTime;

        internal static event Action<string> LineWritten;

        internal static string ReadAll()
        {
            lock (Sync)
                return SessionLog.ToString();
        }

        internal static void Write(string message)
        {
            var line = FormatLocalTime(DateTimeOffset.Now) + " | " + message;
            lock (Sync)
                SessionLog.AppendLine(line);
            LineWritten?.Invoke(line);
        }

        internal static void AppendRecordedLine(string line)
        {
            if (string.IsNullOrWhiteSpace(line))
                return;
            line = FormatProgressLine(line);
            lock (Sync)
                SessionLog.AppendLine(line);
            LineWritten?.Invoke(line);
        }

        private static string FormatLocalTime(DateTimeOffset timestamp)
        {
            var local = timestamp.ToLocalTime();
            return ShortDateFormatter.Format(local) + " " + LongTimeFormatter.Format(local);
        }

        private static string FormatProgressLine(string line)
        {
            const string prefix = "PRINT | Job ";
            if (!line.StartsWith(prefix, StringComparison.Ordinal))
                return line;

            var jobEnd = line.IndexOf(" | ", prefix.Length, StringComparison.Ordinal);
            if (jobEnd < 0)
                return line;
            var timestampStart = jobEnd + 3;
            var timestampEnd = line.IndexOf(" | ", timestampStart, StringComparison.Ordinal);
            if (timestampEnd < 0 || !DateTimeOffset.TryParse(
                line.Substring(timestampStart, timestampEnd - timestampStart),
                CultureInfo.InvariantCulture, DateTimeStyles.None, out var timestamp))
                return line;

            return line.Substring(0, timestampStart)
                + FormatLocalTime(timestamp)
                + line.Substring(timestampEnd);
        }

        internal static void WriteException(string stage, Exception error) =>
            Write(stage + " failed | " + error.GetType().FullName + " | HRESULT=0x"
                + error.HResult.ToString("X8", CultureInfo.InvariantCulture)
                + " | " + error.Message);
    }
}
