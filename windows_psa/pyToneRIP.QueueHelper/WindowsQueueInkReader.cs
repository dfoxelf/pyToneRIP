using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Xml;
using pyToneRIP.Models;

namespace pyToneRIP.QueueHelper
{
    internal static class WindowsQueueInkReader
    {
        private const string BidiNamespace = "http://schemas.microsoft.com/windows/2005/03/printing/bidi";
        private const string Consumables = @"\Printer.Consumables.";
        private const string LevelSuffix = ":Level";
        private const uint UsePrinter = 2;
        private static readonly Guid BidiSpl = new Guid("2A614240-A4C5-4C33-BD87-1BC709331639");

        internal static List<UsbInkLevel> Read(string queueName)
        {
            var instance = Activator.CreateInstance(Type.GetTypeFromCLSID(BidiSpl, true));
            try
            {
                var bidi = (IBidiSpl2)instance;
                bidi.BindDevice(queueName, UsePrinter);
                try { return QueryInk(request => Send(bidi, request)); }
                finally { bidi.UnbindDevice(); }
            }
            finally { Marshal.FinalReleaseComObject(instance); }
        }

        internal static List<UsbInkLevel> QueryInk(Func<string, string> send)
        {
            var schema = ReadXml(send("<bidi:EnumSchema xmlns:bidi=\"" + BidiNamespace + "\"/>"));
            var levels = schema.DocumentElement.ChildNodes.OfType<XmlElement>()
                .Where(item => item.LocalName == "Schema").Select(item => item.GetAttribute("name"))
                .Where(name => name.StartsWith(Consumables, StringComparison.OrdinalIgnoreCase)
                    && name.EndsWith(LevelSuffix, StringComparison.OrdinalIgnoreCase))
                .Distinct(StringComparer.OrdinalIgnoreCase).Take(64).ToList();
            var inks = new List<UsbInkLevel>();
            if (levels.Count == 0) return inks;

            var request = new XmlDocument();
            var root = request.CreateElement("bidi", "Get", BidiNamespace);
            request.AppendChild(root);
            foreach (var level in levels)
            {
                var property = level.Substring(0, level.Length - LevelSuffix.Length);
                foreach (var field in new[] { level, property + ":Type", property + ":Color" })
                {
                    var query = request.CreateElement("Query");
                    query.SetAttribute("schema", field);
                    root.AppendChild(query);
                }
            }
            var reply = ReadXml(send(request.OuterXml));
            var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var query in reply.DocumentElement.ChildNodes.OfType<XmlElement>())
            {
                if (query.LocalName != "Query") continue;
                foreach (var field in query.ChildNodes.OfType<XmlElement>().Where(node => node.LocalName == "Schema"))
                {
                    var value = field.ChildNodes.OfType<XmlElement>().FirstOrDefault();
                    if (value != null && value.LocalName.StartsWith("BIDI_", StringComparison.Ordinal))
                        values[field.GetAttribute("name")] = value.InnerText;
                }
            }
            foreach (var level in levels)
            {
                var property = level.Substring(0, level.Length - LevelSuffix.Length);
                var name = property.Substring(Consumables.Length);
                if (!values.TryGetValue(level, out var value)
                    || !double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var percent)
                    || !(percent >= 0 && percent <= 100)) continue;
                if (values.TryGetValue(property + ":Type", out var type)
                    ? !string.Equals(type, "Ink", StringComparison.OrdinalIgnoreCase)
                    : name.IndexOf("Ink", StringComparison.OrdinalIgnoreCase) < 0) continue;
                values.TryGetValue(property + ":Color", out var color);
                inks.Add(new UsbInkLevel { Name = GetInkName(name, color), Percent = (int)Math.Round(percent) });
            }
            return inks;
        }

        private static string GetInkName(string name, string color)
        {
            var label = name.EndsWith("Ink", StringComparison.OrdinalIgnoreCase)
                ? name.Substring(0, name.Length - 3) : name;
            switch (label.Replace(" ", "").ToUpperInvariant())
            {
                case "BLACK": return "K";
                case "PHOTOBLACK": return "PK";
                case "MATTEBLACK": return "MK";
                case "CYAN": return "C";
                case "MAGENTA": return "M";
                case "YELLOW": return "Y";
                case "LIGHTCYAN": return "LC";
                case "LIGHTMAGENTA": return "LM";
                default: return string.IsNullOrWhiteSpace(color) ? label : color;
            }
        }

        private static XmlDocument ReadXml(string xml)
        {
            var document = new XmlDocument { XmlResolver = null };
            using (var reader = XmlReader.Create(new StringReader(xml), new XmlReaderSettings
            {
                DtdProcessing = DtdProcessing.Prohibit,
                MaxCharactersInDocument = 1024 * 1024,
            })) document.Load(reader);
            return document;
        }

        private static string Send(IBidiSpl2 bidi, string request)
        {
            bidi.SendRecvXMLString(request, out var response);
            return response;
        }

        [ComImport, Guid("0E8F51B8-8273-4906-8E7B-BE453FFD2E2B"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IBidiSpl2
        {
            void BindDevice([MarshalAs(UnmanagedType.LPWStr)] string name, uint access);
            void UnbindDevice();
            void SendRecvXMLString([MarshalAs(UnmanagedType.BStr)] string request,
                [MarshalAs(UnmanagedType.BStr)] out string response);
        }
    }
}
