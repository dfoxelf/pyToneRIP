using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace pyToneRIP.Services
{
    internal sealed class PrinterInkLevel
    {
        public string Name { get; set; }
        public int Percent { get; set; }
    }

    // Read-only SNMP v2c/v1. Printer-MIB values are used only when the printer
    // actually returns them; an unsupported or protected MIB stays unknown.
    internal static class PrinterSnmpService
    {
        // ReferenceDriver's pspfdrv.xml uses the Host Resources MIB printer-status
        // table for network status and Printer-MIB for ink supplies.
        private const string PrinterStatusOid = "1.3.6.1.2.1.25.3.5.1.1";
        private const string EpsonBdcOid = "1.3.6.1.4.1.1248.1.2.2.1.1.1.4";
        private const string SuppliesPrefix = "1.3.6.1.2.1.43.11.1.1.";
        private const string ColorantValueOid = "1.3.6.1.2.1.43.12.1.1.4.1";
        private static readonly int[] SupplyColumns = { 3, 5, 6, 8, 9 };
        private static readonly int[] EssentialSupplyColumns = { 5, 6, 8, 9 };
        private static int nextRequestId = 100;

        internal static async Task<PrinterNetworkState?> ReadStatusAsync(string host)
        {
            if (string.IsNullOrWhiteSpace(host)) return null;
            foreach (var version in new[] { 0, 1 })
            {
                var values = await QueryAsync(host, version, 0xa1,
                    PrinterStatusOid);
                if (values == null) continue;
                var status = values.FirstOrDefault(value =>
                    value.Oid.StartsWith(PrinterStatusOid + ".",
                        StringComparison.Ordinal));
                // hrPrinterStatus 4 = printing. Any valid SNMP response still
                // confirms that the network device is reachable.
                return status?.AsInteger() == 4
                    ? PrinterNetworkState.Printing : PrinterNetworkState.Online;
            }
            return null;
        }

        internal static async Task<IReadOnlyList<PrinterInkLevel>> ReadInkAsync(
            string host, int expectedInkCount = 0)
        {
            if (string.IsNullOrWhiteSpace(host)) return new List<PrinterInkLevel>();
            // ReferenceDriver exposes this Epson-specific Bidi property. Prefer it
            // before walking the generic supplies table to keep one read cheap.
            var epson = await ReadEpsonBdcInkAsync(host);
            if (epson.Count > 0 && epson.Count >= expectedInkCount &&
                !epson.Any(ink => ink.Name.StartsWith("Ink ",
                    StringComparison.Ordinal))) return epson;

            // A newer model can report a BDC ink kind unknown to this decoder.
            // Prefer the device's own Printer-MIB supply descriptions when its
            // complete supply list is available; keep BDC levels otherwise.
            var supplies = await ReadPrinterMibInkAsync(host);
            return supplies.Count >= epson.Count && supplies.Count > 0
                ? supplies : epson;
        }

        private static async Task<IReadOnlyList<PrinterInkLevel>> ReadEpsonBdcInkAsync(
            string host)
        {
            foreach (var version in new[] { 0, 1 })
            {
                var values = await QueryAsync(host, version, 0xa1, EpsonBdcOid);
                var blob = values?.FirstOrDefault(value =>
                    value.Tag == 0x04 && value.Oid.StartsWith(
                        EpsonBdcOid + ".", StringComparison.Ordinal));
                if (blob == null) continue;
                var inks = EpsonBdcStatusParser.ReadInk(blob.Data);
                if (inks.Count > 0) return inks;
            }
            return new List<PrinterInkLevel>();
        }

        private static async Task<IReadOnlyList<PrinterInkLevel>> ReadPrinterMibInkAsync(
            string host)
        {
            var rows = new Dictionary<string, SupplyRow>(StringComparer.Ordinal);
            foreach (var columns in new[] { SupplyColumns, EssentialSupplyColumns })
            {
                foreach (var version in new[] { 0, 1 })
                {
                    var active = columns.Select(column =>
                        SuppliesPrefix + column.ToString(CultureInfo.InvariantCulture))
                        .ToList();
                    for (var step = 0; step < 20 && active.Count > 0; step++)
                    {
                        var values = await QueryAsync(host, version, 0xa1,
                            active.ToArray());
                        if (values == null || values.Count != active.Count) break;
                        var next = new List<string>();
                        for (var i = 0; i < values.Count; i++)
                        {
                            var value = values[i];
                            var column = columns.FirstOrDefault(candidate =>
                                active[i].StartsWith(SuppliesPrefix + candidate + ".",
                                    StringComparison.Ordinal) ||
                                active[i] == SuppliesPrefix + candidate);
                            var prefix = SuppliesPrefix + column + ".";
                            if (column == 0 || !value.Oid.StartsWith(prefix,
                                StringComparison.Ordinal) || value.Oid == active[i])
                                continue;
                            var index = value.Oid.Substring(prefix.Length);
                            SupplyRow row;
                            if (!rows.TryGetValue(index, out row))
                            {
                                row = new SupplyRow();
                                rows[index] = row;
                            }
                            if (column == 3) row.ColorantIndex = value.AsInteger();
                            if (column == 5) row.Type = value.AsInteger();
                            if (column == 6) row.Name = value.AsText();
                            if (column == 8) row.Capacity = value.AsInteger();
                            if (column == 9) row.Level = value.AsInteger();
                            next.Add(value.Oid);
                        }
                        active = next;
                    }
                    if (HasUsableSupplies(rows)) break;
                    rows.Clear();
                }
                if (HasUsableSupplies(rows)) break;
            }

            var needsColorants = rows.Values.Any(row =>
                (row.Type == 5 || row.Type == 6) && row.Capacity > 0 &&
                row.Level >= 0 && IsGenericSupplyName(row.Name));
            var colorants = needsColorants
                ? await ReadColorantsAsync(host)
                : new Dictionary<int, string>();
            return rows.OrderBy(item => item.Key, StringComparer.Ordinal)
                .Where(item => item.Value.Type == 5 || item.Value.Type == 6)
                .Where(item => item.Value.Capacity > 0 && item.Value.Level >= 0)
                .Select(item => new PrinterInkLevel
                {
                    Name = SupplyName(item.Key, item.Value, colorants),
                    Percent = Math.Max(0, Math.Min(100,
                        (int)Math.Round(100.0 * item.Value.Level.Value /
                            item.Value.Capacity.Value))),
                }).ToList();
        }

        private static bool HasUsableSupplies(
            IReadOnlyDictionary<string, SupplyRow> rows) =>
            rows.Values.Any(row => (row.Type == 5 || row.Type == 6) &&
                row.Capacity > 0 && row.Level >= 0);

        private static async Task<Dictionary<int, string>> ReadColorantsAsync(string host)
        {
            var names = new Dictionary<int, string>();
            foreach (var version in new[] { 1, 0 })
            {
                var oid = ColorantValueOid;
                for (var step = 0; step < 20; step++)
                {
                    var values = await QueryAsync(host, version, 0xa1, oid);
                    var value = values?.FirstOrDefault();
                    if (value == null || !value.Oid.StartsWith(
                        ColorantValueOid + ".", StringComparison.Ordinal) ||
                        value.Oid == oid) break;
                    oid = value.Oid;
                    int index;
                    var suffix = oid.Substring(oid.LastIndexOf('.') + 1);
                    var name = value.AsText();
                    if (int.TryParse(suffix, NumberStyles.None,
                        CultureInfo.InvariantCulture, out index) &&
                        !string.IsNullOrWhiteSpace(name) && !names.ContainsKey(index))
                        names[index] = name.Trim();
                }
                if (names.Count > 0) break;
            }
            return names;
        }

        private static string SupplyName(string index, SupplyRow row,
            IReadOnlyDictionary<int, string> colorants)
        {
            var name = row.Name?.Trim();
            string colorant;
            if (row.ColorantIndex.HasValue && colorants.TryGetValue(
                row.ColorantIndex.Value, out colorant) &&
                IsGenericSupplyName(name))
                return colorant;
            return string.IsNullOrWhiteSpace(name) ? "Ink " + index : name;
        }

        private static bool IsGenericSupplyName(string name) =>
            string.IsNullOrWhiteSpace(name) ||
            string.Equals(name.Trim(), "Ink", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(name.Trim(), "Ink Cartridge",
                StringComparison.OrdinalIgnoreCase);

        private sealed class SupplyRow
        {
            internal int? ColorantIndex;
            internal int? Type;
            internal string Name;
            internal int? Capacity;
            internal int? Level;
        }

        private sealed class Variable
        {
            internal string Oid;
            internal byte Tag;
            internal byte[] Data;

            internal int? AsInteger()
            {
                if ((Tag != 0x02 && Tag != 0x41 && Tag != 0x42) ||
                    Data.Length < 1 || Data.Length > 4) return null;
                int value = (Data[0] & 0x80) != 0 && Tag == 0x02 ? -1 : 0;
                foreach (var b in Data) value = (value << 8) | b;
                return value;
            }

            internal string AsText() => Tag == 0x04
                ? Encoding.UTF8.GetString(Data).TrimEnd('\0') : null;
        }

        private static async Task<List<Variable>> QueryAsync(
            string host, int version, byte operation, params string[] oids)
        {
            var requestId = Interlocked.Increment(ref nextRequestId);
            byte[] request;
            try
            {
                var bindings = oids.Select(oid => Tlv(0x30,
                    Tlv(0x06, EncodeOid(oid)), Tlv(0x05, new byte[0]))).ToArray();
                var pdu = Tlv(operation, EncodeInteger(requestId),
                    EncodeInteger(0), EncodeInteger(0), Tlv(0x30, bindings));
                request = Tlv(0x30, EncodeInteger(version),
                    Tlv(0x04, Encoding.ASCII.GetBytes("public")), pdu);
            }
            catch (Exception) { return null; }

            var response = await PrinterConnectivityService.QueryUdpAsync(
                host, "161", request, TimeSpan.FromSeconds(2));
            if (response == null) return null;
            try { return ParseResponse(response, requestId); }
            catch (FormatException) { return null; }
        }

        private static byte[] EncodeInteger(int value)
        {
            var bytes = new[]
            {
                (byte)(value >> 24), (byte)(value >> 16),
                (byte)(value >> 8), (byte)value,
            };
            var first = 0;
            while (first < 3 &&
                ((bytes[first] == 0 && (bytes[first + 1] & 0x80) == 0) ||
                 (bytes[first] == 0xff && (bytes[first + 1] & 0x80) != 0)))
                first++;
            return Tlv(0x02, bytes.Skip(first).ToArray());
        }

        private static byte[] EncodeOid(string oid)
        {
            var parts = oid.Split('.').Select(uint.Parse).ToArray();
            if (parts.Length < 2 || parts[0] > 2 || parts[1] > 39)
                throw new FormatException("Invalid SNMP OID");
            var result = new List<byte> { (byte)(parts[0] * 40 + parts[1]) };
            foreach (var part in parts.Skip(2))
            {
                var encoded = new List<byte> { (byte)(part & 0x7f) };
                var remaining = part >> 7;
                while (remaining != 0)
                {
                    encoded.Insert(0, (byte)((remaining & 0x7f) | 0x80));
                    remaining >>= 7;
                }
                result.AddRange(encoded);
            }
            return result.ToArray();
        }

        private static byte[] Tlv(byte tag, params byte[][] values)
        {
            var length = values.Sum(value => value.Length);
            var result = new List<byte> { tag };
            if (length < 128) result.Add((byte)length);
            else if (length < 256) { result.Add(0x81); result.Add((byte)length); }
            else
            {
                result.Add(0x82);
                result.Add((byte)(length >> 8));
                result.Add((byte)length);
            }
            foreach (var value in values) result.AddRange(value);
            return result.ToArray();
        }

        private struct Element
        {
            internal byte Tag;
            internal int Start;
            internal int End;
        }

        private static Element Read(byte[] bytes, ref int position, int limit)
        {
            if (position + 2 > limit) throw new FormatException();
            var tag = bytes[position++];
            int length = bytes[position++];
            if ((length & 0x80) != 0)
            {
                var count = length & 0x7f;
                if (count == 0 || count > 2 || position + count > limit)
                    throw new FormatException();
                length = 0;
                for (var i = 0; i < count; i++) length = (length << 8) | bytes[position++];
            }
            if (position + length > limit) throw new FormatException();
            var element = new Element { Tag = tag, Start = position, End = position + length };
            position = element.End;
            return element;
        }

        private static byte[] Contents(byte[] bytes, Element item)
        {
            var data = new byte[item.End - item.Start];
            Array.Copy(bytes, item.Start, data, 0, data.Length);
            return data;
        }

        private static int ParseInteger(byte[] bytes, Element item)
        {
            if (item.Tag != 0x02 || item.End - item.Start < 1 ||
                item.End - item.Start > 4) throw new FormatException();
            var value = (bytes[item.Start] & 0x80) != 0 ? -1 : 0;
            for (var i = item.Start; i < item.End; i++)
                value = (value << 8) | bytes[i];
            return value;
        }

        private static string ParseOid(byte[] bytes, Element item)
        {
            if (item.Tag != 0x06 || item.Start == item.End) throw new FormatException();
            var result = new List<uint> { (uint)(bytes[item.Start] / 40),
                (uint)(bytes[item.Start] % 40) };
            uint number = 0;
            var pending = false;
            for (var i = item.Start + 1; i < item.End; i++)
            {
                if (number > (uint.MaxValue >> 7)) throw new FormatException();
                number = (number << 7) | (uint)(bytes[i] & 0x7f);
                pending = (bytes[i] & 0x80) != 0;
                if (!pending) { result.Add(number); number = 0; }
            }
            if (pending) throw new FormatException();
            return string.Join(".", result);
        }

        private static List<Variable> ParseResponse(byte[] bytes, int requestId)
        {
            var cursor = 0;
            var message = Read(bytes, ref cursor, bytes.Length);
            if (message.Tag != 0x30 || cursor != bytes.Length) throw new FormatException();
            cursor = message.Start;
            var version = Read(bytes, ref cursor, message.End);
            if (ParseInteger(bytes, version) > 1) throw new FormatException();
            var community = Read(bytes, ref cursor, message.End);
            if (community.Tag != 0x04) throw new FormatException();
            var pdu = Read(bytes, ref cursor, message.End);
            if (pdu.Tag != 0xa2) throw new FormatException();
            cursor = pdu.Start;
            if (ParseInteger(bytes, Read(bytes, ref cursor, pdu.End)) != requestId)
                throw new FormatException();
            if (ParseInteger(bytes, Read(bytes, ref cursor, pdu.End)) != 0)
                throw new FormatException();
            Read(bytes, ref cursor, pdu.End); // error-index
            var list = Read(bytes, ref cursor, pdu.End);
            if (list.Tag != 0x30) throw new FormatException();
            cursor = list.Start;
            var result = new List<Variable>();
            while (cursor < list.End)
            {
                var binding = Read(bytes, ref cursor, list.End);
                if (binding.Tag != 0x30) throw new FormatException();
                var inner = binding.Start;
                var oid = ParseOid(bytes, Read(bytes, ref inner, binding.End));
                var value = Read(bytes, ref inner, binding.End);
                if (inner != binding.End) throw new FormatException();
                result.Add(new Variable
                {
                    Oid = oid, Tag = value.Tag, Data = Contents(bytes, value),
                });
            }
            return result;
        }
    }
}
