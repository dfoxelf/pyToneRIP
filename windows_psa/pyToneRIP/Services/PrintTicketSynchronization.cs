using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.Serialization;
using System.Threading.Tasks;
using Windows.Data.Json;
using Windows.Graphics.Printing.PrintTicket;

namespace pyToneRIP.Shared
{
    [DataContract]
    internal sealed class LegacyPrinterDefaults
    {
        [DataMember(Name = "print_ticket_xml", Order = 7, EmitDefaultValue = false)] public string PrintTicketXml { get; set; }
        [DataMember(Name = "has_user_devmode", Order = 8)] public bool HasUserDevMode { get; set; }
        [DataMember(Name = "page_width_mm", Order = 1)] public double PageWidthMm { get; set; }
        [DataMember(Name = "page_height_mm", Order = 2)] public double PageHeightMm { get; set; }
        [DataMember(Name = "borderless", Order = 3, EmitDefaultValue = false)] public bool? Borderless { get; set; }
        [DataMember(Name = "orientation", Order = 4)] public string Orientation { get; set; }
        [DataMember(Name = "resolution_x", Order = 5, EmitDefaultValue = false)] public int? ResolutionX { get; set; }
        [DataMember(Name = "resolution_y", Order = 6, EmitDefaultValue = false)] public int? ResolutionY { get; set; }
    }

    internal sealed class StandardPrintTicketSelection
    {
        public string PageOptionName { get; set; }
        public string MediaOptionName { get; set; }
        public string InputOptionName { get; set; }
        public double? PageWidthMm { get; set; }
        public double? PageHeightMm { get; set; }
        public string OrientationOptionName { get; set; }
        public int? ResolutionX { get; set; }
        public int? ResolutionY { get; set; }
        public bool? Borderless { get; set; }
    }

    internal sealed class RasterResolutionChoice
    {
        public int X { get; set; }
        public int Y { get; set; }
        public string DetailLabel { get; set; }
        public string DisplayName => X + " × " + Y + " DPI"
            + (string.IsNullOrWhiteSpace(DetailLabel) ? "" : " — " + DetailLabel);
        public override string ToString() => DisplayName;
    }

    internal static class PrintTicketSynchronization
    {
        // Imported page dimensions may be truncated to whole PostScript points.
        // Windows normalizes standard sizes to exact millimeters. Allow one point
        // plus the PrintTicket's integer-micron rounding, not a different format.
        public const double PageDimensionToleranceMm = 25.4 / 72.0 + 0.001;

        private const string Keywords =
            "http://schemas.microsoft.com/windows/2003/08/printing/printschemakeywords";
        private static readonly IReadOnlyDictionary<string, string> StandardPageOptions =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["A3"] = "ISOA3",
                ["A4"] = "ISOA4",
                ["A5"] = "ISOA5",
                ["A6"] = "ISOA6",
                ["B5"] = "JISB5",
                ["Letter"] = "NorthAmericaLetter",
                ["Legal"] = "NorthAmericaLegal",
                ["ENV10"] = "NorthAmericaNumber10Envelope",
                ["ENVC6"] = "ISOC6Envelope",
                ["Postcard"] = "JapanHagakiPostcard",
            };

        public static StandardPrintTicketSelection Read(WorkflowPrintTicket ticket)
        {
            if (ticket == null) return new StandardPrintTicketSelection();
            // Some application-provided tickets crash inside the native
            // GetSelectedOption implementation. Read the documented XML DOM
            // snapshot instead; absent features remain unspecified.
            return ReadXml(ticket.XmlNode.GetXml());
        }

        internal static StandardPrintTicketSelection ReadXml(string xml)
        {
            var document = System.Xml.Linq.XDocument.Parse(xml);
            System.Xml.Linq.XNamespace framework =
                "http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework";
            var root = document.Root;
            System.Xml.Linq.XName QName(System.Xml.Linq.XElement node, string name)
            {
                if (string.IsNullOrWhiteSpace(name)) return null;
                var colon = name.IndexOf(':');
                if (colon < 0) return node.GetDefaultNamespace() + name;
                var ns = node.GetNamespaceOfPrefix(name.Substring(0, colon));
                return ns == null ? null : ns + name.Substring(colon + 1);
            }
            bool Named(System.Xml.Linq.XElement node, string name) =>
                QName(node, (string)node.Attribute("name"))
                    == System.Xml.Linq.XName.Get(name, Keywords);
            System.Xml.Linq.XElement Option(string feature) => root
                .Elements(framework + "Feature").FirstOrDefault(node => Named(node, feature))
                ?.Elements(framework + "Option").FirstOrDefault();
            string Name(System.Xml.Linq.XElement option) => (string)option?.Attribute("name");
            int? Value(System.Xml.Linq.XElement option, string property)
            {
                var scored = option?.Elements(framework + "ScoredProperty")
                    .FirstOrDefault(node => Named(node, property));
                var value = scored?.Element(framework + "Value");
                var reference = scored?.Element(framework + "ParameterRef");
                if (value == null && reference != null)
                {
                    var parameter = QName(reference, (string)reference.Attribute("name"));
                    if (parameter != null)
                        value = root.Elements(framework + "ParameterInit")
                            .FirstOrDefault(node => QName(node, (string)node.Attribute("name")) == parameter)
                            ?.Element(framework + "Value");
                }
                return int.TryParse(value?.Value, System.Globalization.NumberStyles.Integer,
                    System.Globalization.CultureInfo.InvariantCulture, out int parsed)
                    ? parsed : (int?)null;
            }
            var page = Option("PageMediaSize");
            var resolution = Option("PageResolution");
            var borderless = Option("PageBorderless");
            return new StandardPrintTicketSelection
            {
                PageOptionName = Name(page),
                MediaOptionName = Name(Option("PageMediaType")),
                InputOptionName = Name(Option("JobInputBin")),
                PageWidthMm = MicronsToMillimeters(Value(page, "MediaSizeWidth")),
                PageHeightMm = MicronsToMillimeters(Value(page, "MediaSizeHeight")),
                OrientationOptionName = Name(Option("PageOrientation")),
                ResolutionX = Value(resolution, "ResolutionX"),
                ResolutionY = Value(resolution, "ResolutionY"),
                Borderless = borderless == null ? (bool?)null
                    : Named(borderless, "Borderless"),
            };
        }

        public static void Apply(
            WorkflowPrintTicket ticket,
            string pageId,
            double pageWidthMm,
            double pageHeightMm,
            string orientation,
            int resolutionX,
            int resolutionY,
            string mediaId = null,
            string inputSourceId = null,
            bool? borderless = null)
        {
            if (ticket == null) throw new ArgumentNullException(nameof(ticket));
            SetByMeasurements(
                ticket.PageMediaSizeFeature,
                option => OptionMatchesPageId(option, pageId),
                borderless == true && ticket.PageBorderlessFeature == null ? (Func<PrintTicketOption, bool>)null
                    : option => MatchesMillimeters(option, pageWidthMm, pageHeightMm),
                "Page size");
            if (borderless.HasValue && ticket.PageBorderlessFeature != null)
                SetByMeasurements(ticket.PageBorderlessFeature,
                    option => OptionNameIs(option, borderless.Value ? "Borderless" : "None"),
                    null, "Borderless printing");
            var orientationName = string.Equals(
                orientation, "landscape", StringComparison.OrdinalIgnoreCase)
                ? "Landscape" : "Portrait";
            SetByMeasurements(
                ticket.PageOrientationFeature,
                option => OptionNameIs(option, orientationName),
                null,
                "Orientation");
            SetByMeasurements(
                ticket.PageResolutionFeature,
                option => Integer(option, "ResolutionX") == resolutionX
                    && Integer(option, "ResolutionY") == resolutionY,
                null,
                "Windows raster resolution");
            if (!string.IsNullOrWhiteSpace(mediaId) && ticket.PageMediaTypeFeature != null)
                SetByMeasurements(ticket.PageMediaTypeFeature,
                    option => OptionNameMatchesId(option?.Name, "Media", mediaId),
                    null, "Paper type");
            var inputFeature = ticket.GetFeature("JobInputBin", Keywords);
            if (!string.IsNullOrWhiteSpace(inputSourceId) && inputFeature != null)
                SetByMeasurements(inputFeature,
                    option => OptionNameMatchesId(option?.Name, "Input", inputSourceId),
                    null, "Paper tray");
        }

        public static async Task<WorkflowPrintTicketValidationResult> ValidateAsync(
            WorkflowPrintTicket ticket, Action<string> log)
        {
            // Capture optional COM diagnostics before asking Windows to validate.
            // Never read ticket features from the extension's pending callback.
            WriteValidationLog(log, "PrintTicket validation begin: " + SummarizeTicket(ticket));
            var timer = Stopwatch.StartNew();
            try
            {
                var result = await ticket.ValidateAsync();
                timer.Stop();
                WriteValidationLog(log, "PrintTicket validation returned: validated="
                    + result.Validated + "; elapsedMs=" + timer.ElapsedMilliseconds
                    + "; error=" + (result.ExtendedError == null ? "<none>"
                        : "0x" + result.ExtendedError.HResult.ToString("X8")
                          + "; " + result.ExtendedError));
                return result;
            }
            catch (Exception error)
            {
                timer.Stop();
                WriteValidationLog(log, "PrintTicket validation threw: elapsedMs="
                    + timer.ElapsedMilliseconds + "; HResult=0x"
                    + error.HResult.ToString("X8") + "; " + error);
                throw;
            }
        }

        private static string SummarizeTicket(WorkflowPrintTicket ticket)
        {
            try
            {
                var selection = Read(ticket);
                return "page=" + (selection.PageOptionName ?? "<absent>")
                    + "; media=" + (selection.MediaOptionName ?? "<absent>")
                    + "; input=" + (selection.InputOptionName ?? "<absent>")
                    + "; size=" + (selection.PageWidthMm?.ToString("0.###") ?? "?")
                    + "x" + (selection.PageHeightMm?.ToString("0.###") ?? "?")
                    + "mm; orientation=" + (selection.OrientationOptionName ?? "<absent>")
                    + "; resolution=" + (selection.ResolutionX?.ToString() ?? "?")
                    + "x" + (selection.ResolutionY?.ToString() ?? "?");
            }
            catch (Exception error)
            {
                return "ticket summary unavailable: " + error;
            }
        }

        private static void WriteValidationLog(Action<string> log, string message)
        {
            try { log?.Invoke(message); }
            catch { /* Diagnostics must not change validation behavior. */ }
        }

        public static IReadOnlyList<RasterResolutionChoice> ResolutionChoices(
            WorkflowPrintTicket ticket)
        {
            var feature = ticket?.PageResolutionFeature;
            var options = feature?.Options?
                .Select(option => new RasterResolutionChoice
                {
                    X = Integer(option, "ResolutionX") ?? 0,
                    Y = Integer(option, "ResolutionY") ?? 0,
                })
                .Where(item => item.X > 0 && item.Y > 0)
                .GroupBy(item => item.X + "x" + item.Y)
                .Select(group => group.First())
                .OrderBy(item => item.X)
                .ThenBy(item => item.Y)
                .ToList();
            if (options != null && options.Count > 0) return options;
            var fallback = new List<RasterResolutionChoice>
            {
                new RasterResolutionChoice { X = 300, Y = 300 },
                new RasterResolutionChoice { X = 360, Y = 360 },
                new RasterResolutionChoice { X = 600, Y = 600 },
                new RasterResolutionChoice { X = 720, Y = 720 },
            };
            return fallback.OrderBy(item => item.X).ThenBy(item => item.Y).ToList();
        }

        public static string OverlayConfiguration(
            WorkflowPrintTicket ticket,
            string configurationJson,
            string capabilitiesJson,
            Action<string> log = null)
        {
            var selection = Read(ticket);
            var configuration = RipConfigurationProjection.FromQueueJson(configurationJson);
            var printer = configuration.GetNamedObject("printer");
            var layout = configuration.GetNamedObject("layout");
            var catalog = JsonObject.Parse(capabilitiesJson);
            var printerId = printer.GetNamedString("model");
            var model = catalog.GetNamedArray("printers")
                .Select(item => item.GetObject())
                .FirstOrDefault(item => item.GetNamedString("id") == printerId);
            if (model == null) return configuration.Stringify();

            var selectedMedia = model.GetNamedArray("media")
                .Select(item => item.GetObject())
                .FirstOrDefault(item => item.GetNamedBoolean("enabled")
                    && OptionNameMatchesId(selection.MediaOptionName, "Media",
                        item.GetNamedString("id")));
            if (selectedMedia != null)
                printer.SetNamedValue("media", JsonValue.CreateStringValue(
                    selectedMedia.GetNamedString("id")));
            var selectedInput = model.GetNamedArray("input_sources")
                .Select(item => item.GetObject())
                .FirstOrDefault(item => item.GetNamedBoolean("enabled")
                    && OptionNameMatchesId(selection.InputOptionName, "Input",
                        item.GetNamedString("id")));
            if (selectedInput != null)
                printer.SetNamedValue("input_source", JsonValue.CreateStringValue(
                    selectedInput.GetNamedString("id")));

            var pages = model.GetNamedArray("pages")
                .Select(item => item.GetObject())
                .Where(item => item.GetNamedBoolean("enabled"))
                .ToList();
            var page = pages.FirstOrDefault(item =>
                OptionNameMatchesPageId(selection.PageOptionName, item.GetNamedString("id"))
                && (!selection.Borderless.HasValue || item.GetNamedBoolean("borderless") == selection.Borderless.Value));
            if (page == null && selection.PageWidthMm.HasValue && selection.PageHeightMm.HasValue)
            {
                page = pages.FirstOrDefault(item =>
                        (!selection.Borderless.HasValue || item.GetNamedBoolean("borderless") == selection.Borderless.Value)
                        && Math.Abs(item.GetNamedNumber("width_mm") - selection.PageWidthMm.Value) <= PageDimensionToleranceMm
                        && Math.Abs(item.GetNamedNumber("height_mm") - selection.PageHeightMm.Value) <= PageDimensionToleranceMm);
            }
            if (page == null && selection.Borderless == true)
            {
                // Applications can retain PageBorderless from an earlier
                // DEVMODE while selecting a different, explicitly bordered
                // paper (for example ISOA6). Resolve that conflict to the
                // named supported paper only when no borderless match exists.
                // Unknown names and explicit unsupported borderless page IDs
                // still fail; supported normalized borderless sizes above win.
                page = pages.FirstOrDefault(item => !item.GetNamedBoolean("borderless")
                    && OptionNameMatchesPageId(selection.PageOptionName, item.GetNamedString("id"))
                    && (!selection.PageWidthMm.HasValue || !selection.PageHeightMm.HasValue
                        || (Math.Abs(item.GetNamedNumber("width_mm") - selection.PageWidthMm.Value) <= PageDimensionToleranceMm
                            && Math.Abs(item.GetNamedNumber("height_mm") - selection.PageHeightMm.Value) <= PageDimensionToleranceMm)));
                if (page != null)
                {
                    SetByMeasurements(ticket.PageBorderlessFeature,
                        option => OptionNameIs(option, "None"), null, "Borderless printing");
                    selection.Borderless = false;
                    WriteValidationLog(log, "PrintTicket borderless conflict resolved: model="
                        + printerId + "; page=" + selection.PageOptionName
                        + "; unsupported Borderless replaced with None");
                }
            }
            if (page != null)
            {
                printer.SetNamedValue("page", JsonValue.CreateStringValue(page.GetNamedString("id")));
                if (page.ContainsKey("allowed_media"))
                {
                    var allowedMedia = page.GetNamedArray("allowed_media")
                        .Select(item => item.GetString()).ToList();
                    var currentMedia = printer.GetNamedString("media", "");
                    var allowUnsupported = printer.ContainsKey(
                        "allow_unsupported_ppd_combinations")
                        && printer.GetNamedBoolean("allow_unsupported_ppd_combinations");
                    if (!allowUnsupported && allowedMedia.Count > 0
                        && !allowedMedia.Any(item => string.Equals(
                        item, currentMedia, StringComparison.OrdinalIgnoreCase)))
                    {
                        var media = model.GetNamedArray("media")
                            .Select(item => item.GetObject()).FirstOrDefault(item => string.Equals(
                                item.GetNamedString("id"), currentMedia,
                                StringComparison.OrdinalIgnoreCase));
                        throw new InvalidOperationException(
                            "Unsupported printer combination: "
                            + page.GetNamedString("display_name") + " with "
                            + (media?.GetNamedString("display_name") ?? currentMedia)
                            + ". Select a compatible media type in pyToneRIP Preferences.");
                    }
                }
            }

            if (page == null && selection.Borderless == true)
                throw new InvalidOperationException("The selected borderless paper size is unavailable for this printer.");

            if (!string.IsNullOrWhiteSpace(selection.OrientationOptionName))
            {
                var value = selection.OrientationOptionName.IndexOf(
                    "Landscape", StringComparison.OrdinalIgnoreCase) >= 0
                    ? "landscape" : "portrait";
                layout.SetNamedValue("orientation", JsonValue.CreateStringValue(value));
            }

            return configuration.Stringify();
        }

        public static string OverlayLegacyUserDefaults(
            string configurationJson,
            string capabilitiesJson,
            LegacyPrinterDefaults defaults)
        {
            if (defaults == null) throw new ArgumentNullException(nameof(defaults));
            // OverlayConfiguration already crossed the queue-to-RIP schema
            // boundary. Apply the DEVMODE selection to that exact projection.
            var configuration = JsonObject.Parse(configurationJson);
            if (configuration.GetNamedString("schema", "") != RipConfigurationProjection.Schema
                || configuration.GetNamedNumber("version", 0) != 1)
                throw new ArgumentException("Projected configuration schema is invalid.", nameof(configurationJson));
            var printer = configuration.GetNamedObject("printer");
            var layout = configuration.GetNamedObject("layout");
            var catalog = JsonObject.Parse(capabilitiesJson);
            var printerId = printer.GetNamedString("model");
            var model = catalog.GetNamedArray("printers")
                .Select(item => item.GetObject())
                .FirstOrDefault(item => item.GetNamedString("id") == printerId);
            if (model == null) return configuration.Stringify();

            var pages = model.GetNamedArray("pages")
                .Select(item => item.GetObject())
                .Where(item => item.GetNamedBoolean("enabled"))
                .Where(item => Math.Abs(item.GetNamedNumber("width_mm") - defaults.PageWidthMm)
                    <= PageDimensionToleranceMm
                    && Math.Abs(item.GetNamedNumber("height_mm") - defaults.PageHeightMm)
                    <= PageDimensionToleranceMm)
                .ToList();
            if (defaults.Borderless.HasValue)
                pages = pages.Where(item => item.GetNamedBoolean("borderless")
                    == defaults.Borderless.Value).ToList();

            var currentPageId = printer.GetNamedString("page", "");
            var selectedPage = pages.FirstOrDefault(item => string.Equals(
                    item.GetNamedString("id"), currentPageId, StringComparison.OrdinalIgnoreCase))
                ?? pages.FirstOrDefault(item => !item.GetNamedBoolean("borderless"))
                ?? pages.FirstOrDefault();
            if (selectedPage != null)
            {
                printer.SetNamedValue("page", JsonValue.CreateStringValue(
                    selectedPage.GetNamedString("id")));
                if (selectedPage.ContainsKey("allowed_media"))
                {
                    var allowedMedia = selectedPage.GetNamedArray("allowed_media")
                        .Select(item => item.GetString()).ToList();
                    var currentMedia = printer.GetNamedString("media", "");
                    if (allowedMedia.Count > 0 && !allowedMedia.Any(item => string.Equals(
                        item, currentMedia, StringComparison.OrdinalIgnoreCase)))
                    {
                        var media = model.GetNamedArray("media")
                            .Select(item => item.GetObject())
                            .FirstOrDefault(item => allowedMedia.Any(allowed => string.Equals(
                                allowed, item.GetNamedString("id"), StringComparison.OrdinalIgnoreCase)));
                        if (media != null)
                            printer.SetNamedValue("media", JsonValue.CreateStringValue(
                                media.GetNamedString("id")));
                    }
                }
            }
            if (!string.IsNullOrWhiteSpace(defaults.Orientation))
                layout.SetNamedValue("orientation", JsonValue.CreateStringValue(
                    defaults.Orientation));
            if (defaults.ResolutionX.HasValue && defaults.ResolutionX.Value > 0
                && defaults.ResolutionY.HasValue && defaults.ResolutionY.Value > 0)
            {
                printer.SetNamedValue("raster_resolution_x",
                    JsonValue.CreateNumberValue(defaults.ResolutionX.Value));
                printer.SetNamedValue("raster_resolution_y",
                    JsonValue.CreateNumberValue(defaults.ResolutionY.Value));
            }
            return configuration.Stringify();
        }

        private static void SetByMeasurements(
            PrintTicketFeature feature,
            Func<PrintTicketOption, bool> predicate,
            Func<PrintTicketOption, bool> fallbackPredicate,
            string featureName)
        {
            if (feature == null)
                throw new InvalidOperationException(featureName + " is unavailable in PrintDeviceCapabilities.");
            var current = feature.GetSelectedOption();
            if (current != null && predicate(current)) return;
            var option = feature.Options.FirstOrDefault(predicate);
            if (option == null && fallbackPredicate != null)
            {
                if (current != null && fallbackPredicate(current)) return;
                option = feature.Options.FirstOrDefault(fallbackPredicate);
            }
            if (option == null)
                throw new InvalidOperationException(
                    featureName + " value is absent from PrintDeviceCapabilities.");
            feature.SetSelectedOption(option);
        }

        private static bool OptionNameIs(PrintTicketOption option, string localName)
        {
            var name = option?.Name ?? "";
            return string.Equals(name, localName, StringComparison.OrdinalIgnoreCase)
                || name.EndsWith(":" + localName, StringComparison.OrdinalIgnoreCase);
        }

        private static bool OptionNameMatchesId(string optionName, string kind, string id)
        {
            if (string.IsNullOrWhiteSpace(optionName) || string.IsNullOrWhiteSpace(id))
                return false;
            var localName = kind + "_" + new string(id.Select(character =>
                (character >= 'A' && character <= 'Z')
                || (character >= 'a' && character <= 'z')
                || (character >= '0' && character <= '9')
                || character == '_' || character == '.' || character == '-'
                    ? character : '_').ToArray());
            return string.Equals(optionName, localName, StringComparison.OrdinalIgnoreCase)
                || optionName.EndsWith(":" + localName, StringComparison.OrdinalIgnoreCase);
        }

        public static bool OptionNameMatchesPageId(string optionName, string pageId)
        {
            if (string.IsNullOrWhiteSpace(optionName) || string.IsNullOrWhiteSpace(pageId))
                return false;
            var localName = StandardPageOptions.TryGetValue(pageId, out string standardName)
                ? standardName : "Page_" + SanitizePageId(pageId);
            return string.Equals(optionName, localName, StringComparison.OrdinalIgnoreCase)
                || optionName.EndsWith(":" + localName, StringComparison.OrdinalIgnoreCase)
                || string.Equals(optionName, "Page_" + SanitizePageId(pageId), StringComparison.OrdinalIgnoreCase)
                || optionName.EndsWith(":Page_" + SanitizePageId(pageId), StringComparison.OrdinalIgnoreCase);
        }

        public static bool SelectionMatchesPage(
            StandardPrintTicketSelection selection,
            string pageId,
            double pageWidthMm,
            double pageHeightMm,
            bool? borderless = null)
        {
            if (selection == null) return false;
            if (borderless.HasValue && selection.Borderless.HasValue
                && selection.Borderless.Value != borderless.Value) return false;
            var nameMatches = OptionNameMatchesPageId(selection.PageOptionName, pageId);
            if (borderless == true && !selection.Borderless.HasValue && !nameMatches)
                return false;
            if (!selection.PageWidthMm.HasValue || !selection.PageHeightMm.HasValue)
                return nameMatches;
            // Windows can legally normalize a standard envelope option to a
            // custom name such as _114x162mm.  Dimensions, not its generated
            // label, are authoritative. A matching name must not hide conflicting
            // dimensions, including an invalid zero-size page.
            return selection.PageWidthMm.HasValue && selection.PageHeightMm.HasValue
                && Math.Abs(selection.PageWidthMm.Value - pageWidthMm) <= PageDimensionToleranceMm
                && Math.Abs(selection.PageHeightMm.Value - pageHeightMm) <= PageDimensionToleranceMm;
        }

        public static bool SelectionMatchesDefaults(
            StandardPrintTicketSelection selection, string pageId,
            double pageWidthMm, double pageHeightMm, string orientation,
            int resolutionX, int resolutionY, string mediaId, string inputSourceId,
            bool? borderless = null)
        {
            var orientationName = string.Equals(orientation, "landscape",
                StringComparison.OrdinalIgnoreCase) ? "Landscape" : "Portrait";
            var actualOrientation = selection?.OrientationOptionName ?? "";
            return SelectionMatchesPage(selection, pageId, pageWidthMm, pageHeightMm, borderless)
                && (string.Equals(actualOrientation, orientationName, StringComparison.OrdinalIgnoreCase)
                    || actualOrientation.EndsWith(":" + orientationName, StringComparison.OrdinalIgnoreCase))
                && selection.ResolutionX == resolutionX && selection.ResolutionY == resolutionY
                && (string.IsNullOrWhiteSpace(mediaId)
                    || OptionNameMatchesId(selection.MediaOptionName, "Media", mediaId))
                && (string.IsNullOrWhiteSpace(inputSourceId)
                    || OptionNameMatchesId(selection.InputOptionName, "Input", inputSourceId));
        }

        private static bool OptionMatchesPageId(PrintTicketOption option, string pageId) =>
            OptionNameMatchesPageId(option?.Name, pageId);

        private static string SanitizePageId(string pageId) =>
            new string((pageId ?? "").Select(character =>
                char.IsLetterOrDigit(character) || character == '_'
                    || character == '.' || character == '-'
                    ? character : '_').ToArray());

        private static bool MatchesMillimeters(
            PrintTicketOption option, double widthMm, double heightMm)
        {
            var width = MicronsToMillimeters(Integer(option, "MediaSizeWidth"));
            var height = MicronsToMillimeters(Integer(option, "MediaSizeHeight"));
            return width.HasValue && height.HasValue
                && Math.Abs(width.Value - widthMm) <= PageDimensionToleranceMm
                && Math.Abs(height.Value - heightMm) <= PageDimensionToleranceMm;
        }

        private static int? Integer(PrintTicketOption option, string name)
        {
            try
            {
                return option?.GetScoredPropertyValue(name, Keywords)?.GetValueAsInteger();
            }
            catch
            {
                return null;
            }
        }

        private static double? MicronsToMillimeters(int? value) =>
            value.HasValue ? value.Value / 1000.0 : (double?)null;
    }
}
