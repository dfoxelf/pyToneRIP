using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Linq;
using pyToneRIP.Models;

namespace pyToneRIP.Shared
{
    // Changes only FixedPage geometry in a private copy of the captured job.
    // Image/font resources remain byte-identical, including 16-bit images.
    internal static class PreviewPageTransform
    {
        private static readonly Regex Piece = new Regex(@"^(.*)/\[(\d+)\](\.last)?\.piece$", RegexOptions.CultureInvariant);

        public static void Write(Stream source, Stream destination, IReadOnlyDictionary<int, PreviewPhotoPlacement> placements,
            double paperWidthMm, double paperHeightMm)
        {
            if (paperWidthMm <= 0 || paperHeightMm <= 0 || double.IsNaN(paperWidthMm) || double.IsNaN(paperHeightMm)
                || double.IsInfinity(paperWidthMm) || double.IsInfinity(paperHeightMm)) throw new InvalidDataException("Invalid paper dimensions.");
            using (var input = new ZipArchive(source, ZipArchiveMode.Read, true))
            // The workflow converter consumes IInputStream sequentially. ZIP data
            // descriptors make the OPC reader consume entries instead of seeking
            // past buffered input, which InputStreamWrapper does not implement.
            using (var output = new ZipArchive(new ForwardOnlyWriteStream(destination), ZipArchiveMode.Create, true))
            {
                var parts = Parts(input);
                var orderedPages = Pages(parts);
                var types = ReadXml(parts, "[Content_Types].xml");
                const string ticketType = "application/vnd.ms-printing.printticket+xml";
                var ticketParts = new HashSet<string>(types.Root.Elements().Where(item => item.Name.LocalName == "Override"
                    && (string)item.Attribute("ContentType") == ticketType).Select(item => Uri.UnescapeDataString(((string)item.Attribute("PartName")).TrimStart('/'))));
                var ticketExtensions = new HashSet<string>(types.Root.Elements().Where(item => item.Name.LocalName == "Default"
                    && (string)item.Attribute("ContentType") == ticketType).Select(item => (string)item.Attribute("Extension")), StringComparer.OrdinalIgnoreCase);
                foreach (var item in placements)
                {
                    if (item.Key != item.Value.PageIndex || item.Key < 0 || item.Key >= orderedPages.Count)
                        throw new InvalidDataException("Photo placement refers to an absent source page.");
                    item.Value.Validate(paperWidthMm, paperHeightMm);
                }
                var pageIndices = orderedPages.Select((name, index) => new { name, index }).ToDictionary(item => item.name, item => item.index);
                foreach (var part in parts)
                {
                    var entry = output.CreateEntry(part.Key, CompressionLevel.NoCompression);
                    using (var target = entry.Open())
                    {
                        if (pageIndices.TryGetValue(part.Key, out var pageIndex))
                        {
                            var page = ReadXml(parts, part.Key);
                            Transform(page.Root, placements.TryGetValue(pageIndex, out var placement) ? placement : null,
                                paperWidthMm, paperHeightMm);
                            using (var writer = XmlWriter.Create(target, new XmlWriterSettings { Encoding = new UTF8Encoding(false), CloseOutput = false }))
                                page.Save(writer);
                        }
                        else if (ticketParts.Contains(part.Key) || ticketExtensions.Contains(Path.GetExtension(part.Key).TrimStart('.')))
                        {
                            var ticket = ReadXml(parts, part.Key);
                            if (ticket.Root?.Name != XName.Get("PrintTicket", "http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework"))
                                throw new InvalidDataException("Invalid embedded PrintTicket.");
                            // The workflow ticket owns the sheet/resolution.
                            // Embedded application scopes must not restore the
                            // previous paper after changing it in Job UI.
                            foreach (var feature in ticket.Root.Elements().Where(item => item.Name.LocalName == "Feature").ToList())
                            {
                                var name = (string)feature.Attribute("name") ?? "";
                                var colon = name.IndexOf(':');
                                if (colon <= 0 || feature.GetNamespaceOfPrefix(name.Substring(0, colon))?.NamespaceName
                                    != "http://schemas.microsoft.com/windows/2003/08/printing/printschemakeywords") continue;
                                if (new[] { "PageMediaSize", "PageOrientation", "PageResolution", "PageMediaType", "JobInputBin", "PageScaling" }
                                    .Contains(name.Substring(colon+1))) feature.Remove();
                            }
                            using (var writer = XmlWriter.Create(target, new XmlWriterSettings { Encoding = new UTF8Encoding(false), CloseOutput = false }))
                                ticket.Save(writer);
                        }
                        else foreach (var piece in Ordered(part.Value)) using (var data = piece.Item2.Open()) data.CopyTo(target);
                    }
                }
            }
        }

        public static List<double[]> SourceSizes(Stream source)
        {
            using (var input = new ZipArchive(source, ZipArchiveMode.Read, true))
            {
                var parts = Parts(input);
                return Pages(parts).Select(name => ReadXml(parts, name).Root).Select(page => new[] {
                    double.Parse((string)page.Attribute("Width"), CultureInfo.InvariantCulture)*25.4/96,
                    double.Parse((string)page.Attribute("Height"), CultureInfo.InvariantCulture)*25.4/96 }).ToList();
            }
        }

        private sealed class ForwardOnlyWriteStream : Stream
        {
            private readonly Stream target;
            private long position;
            public ForwardOnlyWriteStream(Stream target)
            { this.target = target; position = target.CanSeek ? target.Position : 0; }
            public override bool CanRead => false;
            public override bool CanSeek => false;
            public override bool CanWrite => target.CanWrite;
            public override long Length => throw new NotSupportedException();
            public override long Position { get => position; set => throw new NotSupportedException(); }
            public override void Flush() => target.Flush();
            public override void Write(byte[] buffer, int offset, int count)
            { target.Write(buffer, offset, count); position += count; }
            public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
            public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
            public override void SetLength(long value) => throw new NotSupportedException();
        }

        private static Dictionary<string, List<Tuple<int, ZipArchiveEntry>>> Parts(ZipArchive input)
        {
            var parts = new Dictionary<string, List<Tuple<int, ZipArchiveEntry>>>(StringComparer.Ordinal);
            foreach (var entry in input.Entries)
            {
                if (entry.FullName.EndsWith("/", StringComparison.Ordinal)) continue;
                var match = Piece.Match(entry.FullName);
                var name = match.Success ? match.Groups[1].Value : entry.FullName;
                var index = match.Success ? int.Parse(match.Groups[2].Value, CultureInfo.InvariantCulture) : 0;
                if (!parts.TryGetValue(name, out var pieces)) parts[name] = pieces = new List<Tuple<int, ZipArchiveEntry>>();
                if (pieces.Any(piece => piece.Item1 == index)) throw new InvalidDataException("Duplicate package part.");
                pieces.Add(Tuple.Create(index, entry));
            }
            return parts;
        }

        private static IEnumerable<Tuple<int, ZipArchiveEntry>> Ordered(List<Tuple<int, ZipArchiveEntry>> pieces)
        {
            var ordered = pieces.OrderBy(piece => piece.Item1).ToList();
            for (var i = 0; i < ordered.Count; i++)
                if (ordered[i].Item1 != i) throw new InvalidDataException("Missing interleaved package piece.");
            return ordered;
        }

        private static XDocument ReadXml(Dictionary<string, List<Tuple<int, ZipArchiveEntry>>> parts, string name)
        {
            if (!parts.TryGetValue(name, out var pieces)) throw new InvalidDataException("Missing package part: " + name);
            using (var bytes = new MemoryStream())
            {
                foreach (var piece in Ordered(pieces))
                {
                    if (bytes.Length+piece.Item2.Length > 16*1024*1024) throw new InvalidDataException("XPS markup exceeds the size limit.");
                    using (var stream = piece.Item2.Open()) stream.CopyTo(bytes);
                }
                bytes.Position = 0;
                using (var reader = XmlReader.Create(bytes, new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null }))
                    return XDocument.Load(reader, LoadOptions.PreserveWhitespace);
            }
        }

        private static List<string> Pages(Dictionary<string, List<Tuple<int, ZipArchiveEntry>>> parts)
        {
            var root = ReadXml(parts, "_rels/.rels");
            var sequence = root.Root.Elements().Single(item => ((string)item.Attribute("Type") ?? "").EndsWith("/fixedrepresentation", StringComparison.Ordinal));
            var sequenceName = Resolve("", (string)sequence.Attribute("Target"));
            var pages = new List<string>();
            foreach (var document in ReadXml(parts, sequenceName).Root.Elements().Where(item => item.Name.LocalName == "DocumentReference"))
            {
                var name = Resolve(sequenceName, (string)document.Attribute("Source"));
                foreach (var page in ReadXml(parts, name).Root.Elements().Where(item => item.Name.LocalName == "PageContent"))
                    pages.Add(Resolve(name, (string)page.Attribute("Source")));
            }
            if (pages.Count == 0) throw new InvalidDataException("The source document has no pages.");
            return pages;
        }

        private static string Resolve(string part, string target)
        {
            if (string.IsNullOrWhiteSpace(target)) throw new InvalidDataException("Empty package reference.");
            var uri = new Uri(new Uri("https://job.invalid/"+part), target);
            if (uri.Host != "job.invalid" || uri.Scheme != "https" || !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment))
                throw new InvalidDataException("External package references are not supported.");
            return Uri.UnescapeDataString(uri.AbsolutePath.TrimStart('/'));
        }

        private static string Number(double value) => value.ToString("R", CultureInfo.InvariantCulture);

        private static void Transform(XElement page, PreviewPhotoPlacement placement, double paperWidthMm, double paperHeightMm)
        {
            if (page?.Name.LocalName != "FixedPage") throw new InvalidDataException("Invalid XPS page.");
            var sourceWidth = double.Parse((string)page.Attribute("Width"), CultureInfo.InvariantCulture);
            var sourceHeight = double.Parse((string)page.Attribute("Height"), CultureInfo.InvariantCulture);
            if (sourceWidth <= 0 || sourceHeight <= 0 || double.IsInfinity(sourceWidth) || double.IsInfinity(sourceHeight)
                || double.IsNaN(sourceWidth) || double.IsNaN(sourceHeight)) throw new InvalidDataException("Invalid source page dimensions.");
            if (placement == null && (Math.Abs(sourceWidth-paperWidthMm*96/25.4) > .01
                || Math.Abs(sourceHeight-paperHeightMm*96/25.4) > .01))
            {
                var scale = Math.Min(paperWidthMm/sourceWidth, paperHeightMm/sourceHeight);
                placement = new PreviewPhotoPlacement { Width = sourceWidth*scale, Height = sourceHeight*scale };
                placement.X = (paperWidthMm-placement.Width)/2; placement.Y = (paperHeightMm-placement.Height)/2;
            }
            if (placement != null)
            {
                var canvas = new XElement(page.Name.Namespace+"Canvas",
                    new XAttribute("RenderTransform", string.Join(",", placement.Matrix(sourceWidth, sourceHeight, 96/25.4).Select(Number))),
                    new XAttribute("Clip", "M0,0 L"+Number(sourceWidth)+",0 "+Number(sourceWidth)+","+Number(sourceHeight)+" 0,"+Number(sourceHeight)+" Z"));
                foreach (var visual in page.Elements().Where(item => !item.Name.LocalName.Contains(".")).ToList())
                { visual.Remove(); canvas.Add(visual); }
                page.Add(canvas);
            }
            page.SetAttributeValue("Width", Number(paperWidthMm*96/25.4));
            page.SetAttributeValue("Height", Number(paperHeightMm*96/25.4));
            page.Attribute("BleedBox")?.Remove(); page.Attribute("ContentBox")?.Remove();
        }
    }
}
