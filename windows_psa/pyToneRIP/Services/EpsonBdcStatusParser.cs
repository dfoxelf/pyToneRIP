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

        internal static IReadOnlyList<PrinterInkLevel> ReadInk(byte[] reply)
        {
            var inks = new List<PrinterInkLevel>();
            if (reply == null || reply.Length < 12 ||
                !Header.SequenceEqual(reply.Take(Header.Length))) return inks;

            var bodyLength = reply[10] | reply[11] << 8;
            if (bodyLength > reply.Length - 12) return inks;
            var end = 12 + bodyLength;
            for (var position = 12; position + 2 <= end;)
            {
                var tag = reply[position++];
                var length = reply[position++];
                if (position + length > end) return new List<PrinterInkLevel>();
                if (tag == 0x0f && length >= 4)
                {
                    var width = reply[position];
                    if ((width == 3 || width == 4) &&
                        (length - 1) % width == 0)
                    {
                        var kinds = new HashSet<byte>();
                        for (var entry = position + 1;
                             entry + width <= position + length; entry += width)
                            kinds.Add(reply[entry]);
                        for (var entry = position + 1;
                             entry + width <= position + length; entry += width)
                        {
                            // ReferenceDriver treats 'i' as an empty cartridge and
                            // 'n' as an unavailable level in this field.
                            var level = reply[entry + 2] == (byte)'i'
                                ? 0 : reply[entry + 2];
                            if (level > 100) continue;
                            inks.Add(new PrinterInkLevel
                            {
                                Name = InkName(reply[entry], kinds.Contains(0x0b)),
                                Percent = level,
                            });
                        }
                    }
                }
                position += length;
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
                case 0x0e: return "O";
                case 0x0f: return "LLK";
                case 0x10: return "OR";
                case 0x11: return "G";
                case 0x16: return "LK";
                default: return "Ink " + kind.ToString("X2", CultureInfo.InvariantCulture);
            }
        }
    }
}
