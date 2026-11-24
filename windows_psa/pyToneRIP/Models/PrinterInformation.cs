// Copyright (C) 2026 Alexander Eichhorn
// SPDX-License-Identifier: GPL-3.0-only
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Runtime.Serialization;
using System.Text;
using System.Text.RegularExpressions;

namespace pyToneRIP.Models
{
    [DataContract]
    public sealed class PrinterInformationSupply
    {
        [DataMember(Name = "name", Order = 1)] public string Name { get; set; }
        [DataMember(Name = "percent", Order = 2)] public int Percent { get; set; }
    }

    [DataContract]
    public sealed class PrinterInformationReply
    {
        [DataMember(Name = "command", Order = 1)] public string Command { get; set; }
        [DataMember(Name = "hex", Order = 2)] public string Hex { get; set; }
        [DataMember(Name = "error", Order = 3)] public string Error { get; set; }
    }

    [DataContract]
    public sealed class PrinterInformationReport
    {
        [DataMember(Name = "source", Order = 1)] public string Source { get; set; }
        [DataMember(Name = "replies", Order = 2)] public List<PrinterInformationReply> Replies { get; set; }
            = new List<PrinterInformationReply>();
        [DataMember(Name = "supplies", Order = 3, EmitDefaultValue = false)]
        public List<PrinterInformationSupply> Supplies { get; set; }

        public void Add(string command, byte[] bytes) => Replies.Add(new PrinterInformationReply
            { Command = command, Hex = BitConverter.ToString(bytes).Replace("-", "") });
        public void AddFailure(string command, string error) => Replies.Add(new PrinterInformationReply
            { Command = command, Error = error });
    }
}

namespace pyToneRIP.Shared
{
    using pyToneRIP.Models;

    internal sealed class PrinterInformationField
    {
        internal string Section;
        internal string Label;
        internal string LabelKey;
        internal string Value;
        internal string ValueKey;
        internal string Severity = "Value";
    }

    // Shared by the UWP view and desktop helper; no UI or printer model database.
    internal static class PrinterInformationDecoder
    {
        internal static readonly string[] Queries = { "st", "di", "vi", "ia" };
        private static readonly byte[] Header = Encoding.ASCII.GetBytes("@BDC ST2\r\n");
        private static readonly Dictionary<byte, string> InkNames = new Dictionary<byte, string>
        {
            {1,"K"},{3,"C"},{4,"M"},{5,"Y"},{6,"LC"},{7,"LM"},{10,"LK"},{11,"MK"},
            {12,"R"},{13,"B"},{14,"GO"},{15,"LLK"},{16,"OR"},{17,"G"},{22,"LK"},
        };
        private static readonly Dictionary<byte, string> States = new Dictionary<byte, string>
        {
            {0,"Error"},{1,"Self printing"},{2,"Busy"},{3,"Waiting"},{4,"Idle"},{5,"Paused"},
            {7,"Cleaning"},{8,"Not initialized"},{10,"Shutdown"},{15,"Nozzle check"},{17,"Charging"},
        };
        private static readonly Dictionary<byte, string> Errors = new Dictionary<byte, string>
        {
            {0,"Fatal error"},{1,"Other interface selected"},{2,"Cover open"},{4,"Paper jam"},
            {5,"Ink out"},{6,"Paper out"},{12,"Paper size, type or path mismatch"},
            {16,"Waste ink pad full"},{18,"Double feed"},{26,"Cartridge cover open"},
            {28,"Cutter error"},{29,"Cutter jam"},{34,"Maintenance cartridge missing"},
            {37,"Rear cover open"},{41,"CD tray out"},{43,"Tray cover open"},
            {54,"Maintenance cartridge cover open"},{55,"Scanner/front cover open"},
            {65,"Maintenance request"},{71,"Printing disabled"},{74,"Maintenance box near end"},
            {75,"Driver mismatch"},
        };
        private static readonly Dictionary<byte, string> FieldNames = new Dictionary<byte, string>
        {
            {3,"self_print_code"},{6,"paper_path"},{7,"paper_mismatch"},{12,"cleaning_time"},
            {13,"maintenance_tanks"},{14,"replace_cartridge"},{16,"loading_path"},{19,"cancel_code"},
            {20,"cutter"},{24,"tray_open"},{25,"job_name"},{53,"paper_jam"},
            {69,"ink_replacement_counters"},{70,"maintenance_box_replacement_counter"},
        };
        private static readonly Dictionary<byte, string> Warnings = new Dictionary<byte, string>
        {
            {16,"Ink low (black/yellow)"},{17,"Ink low (magenta)"},{18,"Ink low (yellow/cyan)"},
            {19,"Ink low (cyan/matte black)"},{20,"Ink low (photo black)"},{21,"Ink low (red)"},
            {22,"Ink low (blue)"},{23,"Ink low (gloss optimizer)"},{68,"Black print mode"},
            {81,"Cleaning disabled (cyan)"},{82,"Cleaning disabled (magenta)"},
            {83,"Cleaning disabled (yellow)"},{84,"Cleaning disabled (black)"},
        };

        internal static byte[] Request(string command)
        {
            if (command == "st" || command == "di")
                return Encoding.ASCII.GetBytes(command).Concat(new byte[] { 1, 0, 1 }).ToArray();
            if (command == "vi" || command == "ia")
                return Encoding.ASCII.GetBytes(command).Concat(new byte[] { 1, 0, 0 }).ToArray();
            int slot;
            if (command != null && command.StartsWith("ii:", StringComparison.Ordinal) &&
                int.TryParse(command.Substring(3), out slot) && slot >= 1 && slot <= 32)
                return new byte[] { (byte)'i', (byte)'i', 2, 0, 1, (byte)slot };
            throw new ArgumentException("Unsupported read-only printer information query.", nameof(command));
        }

        internal static string Oid(string command) => "1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1." +
            string.Join(".", Request(command).Select(value => value.ToString(CultureInfo.InvariantCulture)));

        internal static byte[] Bytes(string hex)
        {
            if (hex == null || hex.Length % 2 != 0 || hex.Length > 131096)
                throw new FormatException("Invalid printer reply hex length.");
            var bytes = new byte[hex.Length / 2];
            for (var i = 0; i < bytes.Length; i++)
                bytes[i] = byte.Parse(hex.Substring(i * 2, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
            return bytes;
        }

        private static PrinterInformationField Field(string section, string label, string value,
            string severity = "Value", bool key = true) => new PrinterInformationField
        {
            Section = section, LabelKey = key ? "PrinterInfo." + label : null,
            Label = key ? null : label, Value = value, Severity = severity,
        };

        private static string Hex(byte[] data) => BitConverter.ToString(data).Replace("-", " ");

        private static string Text(byte[] data) => string.Concat(data.Select(value =>
            value == 10 || value == 13 || value == 9 || value >= 32 && value < 127
                ? ((char)value).ToString() : value == 0 || value == 12 ? "" : "\\x" + value.ToString("X2"))).Trim();

        internal static string ReadDeviceIdModel(byte[] data) =>
            data == null ? null : MatchDeviceIdModel(Text(data));

        private static string MatchDeviceIdModel(string text)
        {
            var model = Regex.Match(text, @"(?:^|[;\r\n])MDL:([^;]+)", RegexOptions.IgnoreCase);
            return model.Success ? model.Groups[1].Value.Trim() : null;
        }

        internal static IReadOnlyList<PrinterInformationField> Decode(PrinterInformationReport report)
        {
            var result = new List<PrinterInformationField> { Field("Connection", "Source", report.Source ?? "") };
            foreach (var supply in report.Supplies ?? new List<PrinterInformationSupply>())
                result.Add(new PrinterInformationField { Section = "Inks", Label = supply.Name,
                    Value = supply.Percent >= 0 ? supply.Percent.ToString(CultureInfo.CurrentCulture) + "%" : null,
                    ValueKey = supply.Percent < 0 ? "PrinterInfo.NoData" : null,
                    Severity = supply.Percent < 0 ? "Raw" : supply.Percent == 0 ? "Error" : supply.Percent <= 10 ? "Warning" : "Value" });
            foreach (var command in Queries)
                if (!report.Replies.Any(reply => reply.Command == command))
                    result.Add(new PrinterInformationField { Section = "Identity", Label = command,
                        ValueKey = "PrinterInfo.Unsupported", Severity = "Warning" });
            foreach (var reply in report.Replies)
            {
                if (string.IsNullOrEmpty(reply.Hex))
                {
                    result.Add(new PrinterInformationField { Section = "Details", Label = reply.Command,
                        Value = reply.Error, ValueKey = string.IsNullOrWhiteSpace(reply.Error) ? "PrinterInfo.Unsupported" : null,
                        Severity = "Warning" });
                    continue;
                }
                byte[] data;
                try { data = Bytes(reply.Hex); }
                catch (FormatException error)
                { result.Add(Field("Details", reply.Command, error.Message, "Error", false)); continue; }
                result.Add(Field("Raw", reply.Command, Hex(data), "Raw", false));
                if (reply.Command == "st")
                {
                    try { result.AddRange(DecodeBdc(data)); }
                    catch (FormatException error)
                    { result.Add(Field("Status", "Error", error.Message, "Error")); }
                    continue;
                }
                var text = Text(data);
                if (reply.Command == "di")
                {
                    var model = MatchDeviceIdModel(text);
                    if (!string.IsNullOrEmpty(model)) result.Add(Field("Identity", "Model", model));
                    result.Add(Field("Identity", "DeviceId", text));
                }
                else if (reply.Command == "vi")
                {
                    var version = Regex.Match(text, @"vi:00:([^;]+)", RegexOptions.IgnoreCase);
                    result.Add(Field("Identity", "Firmware", version.Success ? version.Groups[1].Value : text));
                }
                else if (reply.Command == "ia") result.Add(Field("Identity", "Cartridges", text));
                else result.Add(Field("Details", reply.Command, text, "Value", false));
            }
            return result;
        }

        private static IEnumerable<PrinterInformationField> DecodeBdc(byte[] reply)
        {
            if (reply.Length > 0 && reply[0] == 0) reply = reply.Skip(1).ToArray();
            if (reply.Length < 12 || !reply.Take(10).SequenceEqual(Header)) throw new FormatException("Invalid BDC header.");
            var end = 12 + reply[10] + 256 * reply[11];
            if (end > reply.Length) throw new FormatException("Incomplete BDC body.");
            var blocks = new List<Tuple<byte, byte[]>>();
            var inkRecords = new List<byte[]>();
            for (var pos = 12; pos < end;)
            {
                if (pos + 2 > end) throw new FormatException("Incomplete BDC field.");
                var tag = reply[pos++]; var count = reply[pos++];
                if (pos + count > end) throw new FormatException("Incomplete BDC field value.");
                var value = reply.Skip(pos).Take(count).ToArray();
                if (tag == 15)
                {
                    if (count < 1 || value[0] < 3 || (count - 1) % value[0] != 0)
                        throw new FormatException("Invalid BDC ink records.");
                    for (var offset = 1; offset < count; offset += value[0])
                        inkRecords.Add(value.Skip(offset).Take(value[0]).ToArray());
                }
                blocks.Add(Tuple.Create(tag, value)); pos += count;
            }
            var fields = new List<PrinterInformationField>();
            var matte = inkRecords.Any(record => record[0] == 11);
            foreach (var record in inkRecords)
            {
                string name;
                if (!InkNames.TryGetValue(record[0], out name)) name = "0x" + record[0].ToString("X2");
                if (record[0] == 1 && matte) name = "PK";
                var level = record[2] == (byte)'i' ? 0 : record[2];
                fields.Add(new PrinterInformationField { Section = "Inks", Label = name,
                    Value = level <= 100 ? level.ToString(CultureInfo.CurrentCulture) + "%" : null,
                    ValueKey = level > 100 ? "PrinterInfo.NoData" : null,
                    Severity = level > 100 ? "Raw" : level == 0 ? "Error" : level <= 10 ? "Warning" : "Value" });
            }
            foreach (var block in blocks)
            {
                var tag = block.Item1; var value = block.Item2;
                string text;
                if (tag == 1 && value.Length > 0)
                    fields.Add(Field("Status", "State", "0x" + value[0].ToString("X2") +
                        (States.TryGetValue(value[0], out text) ? " — " + text : ""),
                        value[0] == 0 ? "Error" : value[0] == 3 || value[0] == 4 ? "Success" : "Warning"));
                else if (tag == 2 && value.Length > 0)
                    fields.Add(Field("Status", "Error", "0x" + value[0].ToString("X2") +
                        (Errors.TryGetValue(value[0], out text) ? " — " + text : ""), "Error"));
                else if (tag == 4)
                    foreach (var warning in value)
                        fields.Add(Field("Status", "Warning", "0x" + warning.ToString("X2") +
                            (Warnings.TryGetValue(warning, out text) ? " — " + text : ""), "Warning"));
                else if (tag == 0x40 || tag == 0x1f) fields.Add(Field("Identity", "SerialNumber", Text(value)));
                else if (tag == 0x36 && value.Length == 20)
                {
                    var counts = new[] { "normal", "total", "color", "monochrome", "blank" };
                    fields.Add(Field("Details", "PaperCounters", string.Join("; ", Enumerable.Range(0, 5)
                        .Select(i => counts[i] + "=" + ((uint)value[i*4] | (uint)value[i*4+1] << 8 |
                            (uint)value[i*4+2] << 16 | (uint)value[i*4+3] << 24).ToString(CultureInfo.CurrentCulture)))));
                }
                else if (tag == 0x37 && value.Length > 0 && value[0] >= 1 && value[0] <= 2 &&
                    (value.Length - 1) % value[0] == 0)
                {
                    for (var i = 1; i < value.Length; i += value[0])
                        fields.Add(Field("Details", "MaintenanceBox", "#" + ((i-1)/value[0]+1) + ": " +
                            (value[i] == 0 ? "not full" : value[i] == 1 ? "near full" : value[i] == 2 ? "full" : "0x" + value[i].ToString("X2")) +
                            (value[0] == 2 ? "; resets=" + value[i+1] : ""), value[i] == 2 ? "Error" : value[i] == 1 ? "Warning" : "Value"));
                }
                else if (tag == 0x1c) fields.Add(Field("Details", "Temperature", Hex(value) +
                    (value.Length == 1 && value[0] == 1 ? " — > 40 °C" : value.Length == 1 && value[0] == 0 ? " — < 40 °C" : "")));
                else if (tag == 0x3d) fields.Add(Field("Details", "Interface", Hex(value) +
                    (value.Length == 1 && value[0] == 0 ? " — available" : value.Length == 1 && value[0] == 1 ? " — busy" : "")));
                else if (tag != 15)
                {
                    string name;
                    var known = FieldNames.TryGetValue(tag, out name);
                    var display = Hex(value);
                    if (tag == 0x19) display = Text(value);
                    if (tag == 0x45 && value.Length == 4)
                        display = "K=" + value[0] + "; C=" + value[1] + "; M=" + value[2] + "; Y=" + value[3];
                    if (tag == 0x46 && value.Length == 1) display = value[0].ToString(CultureInfo.CurrentCulture);
                    if (tag == 0x18 && value.Length == 1)
                        display += value[0] == 2 ? " — closed" : value[0] == 3 ? " — open" : "";
                    if (tag == 0x35 && value.Length == 1)
                        display += value[0] == 0 ? " — no jam" : value[0] == 1 ? " — ejecting" :
                            value[0] == 2 ? " — rear feed/no feed" : value[0] == 0x80 ? " — rear feed empty" : "";
                    fields.Add(Field("Details", known ? name + " (0x" + tag.ToString("X2") + ")" :
                        "0x" + tag.ToString("X2"), display,
                        tag == 0x35 && value.Length == 1 && value[0] != 0 ? "Warning" : known ? "Value" : "Raw", false));
                }
            }
            return fields;
        }
    }
}
