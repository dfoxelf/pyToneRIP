using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;

namespace pyToneRIP.Services
{
    // Epson's @BDC ST2 reply is also exposed as the EpsonBDC SNMP blob.
    // The 0x0f block contains ink records; 3- and 4-byte records were checked
    // against ReferenceDriver's saved ET-2550, XP-760 and Stylus Pro 7890 replies.
    // Ink kind mappings also follow the table used by ReferenceDriver zedomon010.dll.
    internal static class EpsonBdcStatusParser
    {
        private static readonly byte[] Header =
            { (byte)'@', (byte)'B', (byte)'D', (byte)'C', (byte)' ',
              (byte)'S', (byte)'T', (byte)'2', 13, 10 };

        internal static byte? ReadState(byte[] reply)
        {
            if (reply == null || reply.Length == 0) return null;
            var start = reply[0] == 0 ? 1 : 0;
            var body = start + 12;
            if (reply.Length < body || !Header.SequenceEqual(reply.Skip(start).Take(Header.Length)))
                return null;
            var end = body + (reply[start + 10] | reply[start + 11] << 8);
            if (end > reply.Length) return null;
            byte? state = null;
            for (var position = body; position < end;)
            {
                if (end - position < 2) return null;
                var tag = reply[position++];
                var length = reply[position++];
                if (position + length > end) return null;
                if (tag == 1)
                {
                    if (length != 1 || state.HasValue) return null;
                    state = reply[position];
                }
                position += length;
            }
            return state;
        }

        internal static IReadOnlyList<PrinterInkLevel> ReadInk(byte[] reply)
        {
            var inks = new List<PrinterInkLevel>();
            // Some Epson SNMP envelopes prepend a zero byte to the BDC frame.
            if (reply != null && reply.Length > 0 && reply[0] == 0)
                reply = reply.Skip(1).ToArray();
            if (reply == null || reply.Length < 12 ||
                !Header.SequenceEqual(reply.Take(Header.Length))) return inks;

            var bodyLength = reply[10] | reply[11] << 8;
            if (bodyLength > reply.Length - 12) return inks;
            var end = 12 + bodyLength;
            var entries = new List<int>();
            for (var position = 12; position < end;)
            {
                if (end - position < 2) return new List<PrinterInkLevel>();
                var tag = reply[position++];
                var length = reply[position++];
                if (position + length > end) return new List<PrinterInkLevel>();
                if (tag == 0x0f)
                {
                    if (length < 1) return new List<PrinterInkLevel>();
                    var width = reply[position];
                    if (width < 3 || (length - 1) % width != 0)
                        return new List<PrinterInkLevel>();
                    for (var entry = position + 1; entry + width <= position + length; entry += width)
                        entries.Add(entry);
                }
                position += length;
            }
            var matte = entries.Any(entry => reply[entry] == 0x0b);
            foreach (var entry in entries)
            {
                var level = reply[entry + 2] == (byte)'i' ? 0 : reply[entry + 2];
                inks.Add(new PrinterInkLevel
                {
                    Name = InkName(reply[entry], matte),
                    Percent = level <= 100 ? level : -1,
                });
            }
            return inks;
        }

        private static string InkName(byte kind, bool hasMatteBlack)
        {
            switch (kind)
            {
                case 0x01: return hasMatteBlack ? "PK" : "K";
                case 0x03: return "C";
                case 0x04: return "M";
                case 0x05: return "Y";
                case 0x06: return "LC";
                case 0x07: return "LM";
                case 0x0a: return "LK";
                case 0x0b: return "MK";
                case 0x0c: return "R";
                case 0x0d: return "B";
                case 0x0e: return "GO";
                case 0x0f: return "LLK";
                case 0x10: return "OR";
                case 0x11: return "G";
                case 0x16: return "LK";
                default: return "Ink " + kind.ToString("X2", CultureInfo.InvariantCulture);
            }
        }
    }
}
