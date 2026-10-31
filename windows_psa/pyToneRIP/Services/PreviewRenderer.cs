using System;
using System.IO;
using System.Collections.Generic;
using System.Linq;
using System.Numerics;
using System.Runtime.Serialization.Json;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Graphics.Canvas;
using Microsoft.Graphics.Canvas.Effects;
using pyToneRIP.Models;
using Windows.Foundation;
using Windows.Data.Json;
using Windows.Data.Pdf;
using Windows.Graphics.Display;
using Windows.Storage;
using Windows.Storage.Streams;
using Windows.UI;
using Microsoft.Graphics.Canvas.UI.Xaml;

namespace pyToneRIP.Services
{
    internal sealed class PreviewRenderer : IDisposable
    {
        private readonly CanvasDevice device = new CanvasDevice();
        private readonly ColorManagementProfile srgb = new ColorManagementProfile(CanvasColorSpace.Srgb);
        private readonly ColorManagementProfile linearSrgb = new ColorManagementProfile(CanvasColorSpace.ScRgb);
        private readonly SemaphoreSlim operations = new SemaphoreSlim(1, 1);
        private readonly PdfDocument document;
        private readonly JsonObject configuration;
        private readonly JsonObject options;
        private CapabilityOption paper;
        private string mediaClass;
        private ColorManagementProfile printerProfile;
        private ColorManagementProfile displayProfile;
        private bool disposed;
        private CanvasRenderTarget photoProofBitmap, photoProofPaper;
        private CanvasImageSource photoProofImage, photoProofPaperImage;
        private uint photoProofPage;
        private bool photoProofEnabled;
        private uint photoProofEdge;
        public CanvasImageSource PhotoProofImage => photoProofImage;
        public CanvasImageSource PhotoProofPaperImage => photoProofPaperImage;
        private bool originalSource;
        private bool composedPreview;
        private readonly List<PreviewSourcePage> sourcePages = new List<PreviewSourcePage>();
        private readonly Dictionary<int, PreviewPhotoPlacement> placements = new Dictionary<int, PreviewPhotoPlacement>();
        private bool reversePageOrder;
        private string conditionFingerprint;
        public uint PageCount => document.PageCount;
        public bool Measured => printerProfile != null;
        public int RasterDpiX { get; private set; }
        public int RasterDpiY { get; private set; }
        public int PrintStartEdge { get; private set; }
        public string NetworkHost => configuration.GetNamedObject("connection", null)?.GetNamedString("kind", "network") == "network"
            ? configuration.GetNamedObject("connection").GetNamedString("host", "") : null;
        public Dictionary<string, string> Information { get; } = new Dictionary<string, string>();

        private PreviewRenderer(PdfDocument document, JsonObject configuration, JsonObject options)
        {
            this.document = document;
            this.configuration = configuration;
            this.options = options;
        }

        public static async Task<PreviewRenderer> OpenAsync(StorageFolder folder, bool originalSource = false)
        {
            JsonObject descriptor = null;
            try { descriptor = JsonObject.Parse(await FileIO.ReadTextAsync(await folder.GetFileAsync("preview-document.json"))); }
            catch (FileNotFoundException) { }
            var document = await PdfDocument.LoadFromFileAsync(await folder.GetFileAsync(originalSource ? "preview.pdf" : descriptor?.GetNamedString("pdf") ?? "preview.pdf"));
            var config = JsonObject.Parse(await FileIO.ReadTextAsync(await folder.GetFileAsync(descriptor?.GetNamedString("configuration") ?? "configuration.json")));
            var options = JsonObject.Parse(await FileIO.ReadTextAsync(await folder.GetFileAsync(descriptor?.GetNamedString("settings") ?? "preview-settings.json")));
            var renderer = new PreviewRenderer(document, config, options);
            renderer.originalSource = originalSource;
            renderer.composedPreview = descriptor?.ContainsKey("placements") == true;
            try
            {
                if (renderer.composedPreview)
                {
                    using (var stream = await (await folder.GetFileAsync(descriptor.GetNamedString("placements"))).OpenStreamForReadAsync())
                        foreach (var placement in (List<PreviewPhotoPlacement>)new DataContractJsonSerializer(typeof(List<PreviewPhotoPlacement>)).ReadObject(stream))
                            renderer.placements.Add(placement.PageIndex, placement);
                }
                if (document.PageCount == 0) throw new InvalidDataException("Print job has no preview pages.");
                renderer.conditionFingerprint = await PreviewProfileAssociations.FingerprintAsync(config);
                if (!string.IsNullOrWhiteSpace(options.GetNamedString("icc_path", "")))
                {
                    var file = await folder.GetFileAsync(options.GetNamedString("icc_file", "preview-printer.icc"));
                    renderer.printerProfile = ColorManagementProfile.CreateCustom((await FileIO.ReadBufferAsync(file)).ToArray());
                    var expected = options.GetNamedString("condition_fingerprint", "");
                    renderer.Information["ProfileMatch"] = LocalizationService.Get("PreviewWindow." + (string.IsNullOrWhiteSpace(expected)
                        ? "ProfileUnknown" : expected == renderer.conditionFingerprint
                            && options.GetNamedString("icc_sha256", "") == await PreviewProfileAssociations.ProfileHashAsync(file)
                                ? "ProfileAssigned" : "ProfileMismatch"));
                }
                var printer = config.GetNamedObject("printer");
                var catalog = await CapabilityCatalog.LoadForModelAsync(printer.GetNamedString("model"));
                renderer.paper = catalog.Printers.Single().Pages.Single(item => item.Id == printer.GetNamedString("page"));
                var medium = catalog.Printers.Single().Media.Single(item => item.Id == printer.GetNamedString("media"));
                renderer.Information["Printer"] = catalog.Printers.Single().DisplayName;
                renderer.Information["Paper"] = medium.DisplayName;
                renderer.Information["Size"] = renderer.paper.DisplayName;
                renderer.Information["Mode"] = catalog.Printers.Single().Modes.Single(item => item.Id == printer.GetNamedString("mode")).DisplayName;
                var selectedDither = config.GetNamedObject("adjustments").GetNamedString("dither");
                var effectiveDither = selectedDither == "Adaptive Hybrid" ? "Ordered" : selectedDither;
                var effectiveDitherName = catalog.Dither?.FirstOrDefault(item => item.Id == effectiveDither)?.DisplayName ?? effectiveDither;
                renderer.Information["Dither"] = selectedDither == effectiveDither
                    ? effectiveDitherName : selectedDither + " \u2192 " + effectiveDitherName;
                renderer.Information["Curves"] = string.Join(Environment.NewLine,
                    config.GetNamedObject("profiles").GetNamedArray("curves").Select(value => value.GetString()));
                renderer.Information["ICC"] = options.GetNamedString("icc_path", "");
                var ticket = pyToneRIP.Shared.PrintTicketSynchronization.ReadXml(
                    await FileIO.ReadTextAsync(await folder.GetFileAsync(descriptor?.GetNamedString("ticket") ?? "print-ticket.xml")));
                renderer.PrintStartEdge = PreviewPrintDirection.LeadingEdge(ticket.OrientationOptionName
                    ?? config.GetNamedObject("layout").GetNamedString("orientation", "portrait"));
                renderer.reversePageOrder = ticket.PageOrderOptionName == "Reverse"
                    || (ticket.PageOrderOptionName ?? "").EndsWith(":Reverse", StringComparison.Ordinal);
                renderer.RasterDpiX = ticket.ResolutionX ?? 0;
                renderer.RasterDpiY = ticket.ResolutionY ?? 0;
                renderer.Information["Resolution"] = renderer.RasterDpiX + " × " + renderer.RasterDpiY + " DPI";
                var originalTicket = pyToneRIP.Shared.PrintTicketSynchronization.ReadXml(
                    await FileIO.ReadTextAsync(await folder.GetFileAsync("print-ticket.xml")));
                var originalPdl = (await folder.TryGetItemAsync("payload.oxps")) as StorageFile
                    ?? await folder.GetFileAsync("payload.pdl");
                List<double[]> originalSizes;
                using (var stream = await originalPdl.OpenStreamForReadAsync())
                    originalSizes = pyToneRIP.Shared.PreviewPageTransform.SourceSizes(stream);
                if (originalSizes.Count != document.PageCount)
                    throw new InvalidDataException("Preview PDF and source XPS page counts differ.");
                var reverseLandscape = (originalTicket.OrientationOptionName ?? "")
                    .EndsWith("ReverseLandscape", StringComparison.OrdinalIgnoreCase);
                for (uint index = 0; index < document.PageCount; index++)
                {
                    using (var sourcePage = document.GetPage(index))
                    {
                        var size = originalSizes[renderer.SourcePageIndex(index)];
                        renderer.sourcePages.Add(new PreviewSourcePage(sourcePage.Size.Width, sourcePage.Size.Height,
                            size[0], size[1], reverseLandscape));
                    }
                }
                // An empty placement snapshot from a no-change settings save
                // does not turn the document into a newly composed page.
                var sheet = renderer.PaperSizeMm;
                renderer.composedPreview = renderer.composedPreview && (renderer.placements.Count > 0
                    || renderer.sourcePages.Any(source => !source.MatchesPaper(sheet.Width, sheet.Height)));
                // Preserve the specific substrate when the generic catalog
                // category is simply photo/matte.
                renderer.mediaClass = medium.PreviewMediaClass == "generic" || string.IsNullOrWhiteSpace(medium.PreviewMediaClass)
                    ? medium.DisplayName : medium.PreviewMediaClass;
                if (renderer.paper.PreviewPaperPoints?.Count != 2 || renderer.paper.PreviewImageablePoints?.Count != 4)
                    throw new InvalidDataException("Preview page geometry is missing; regenerate package assets.");
                await renderer.UpdateDisplayProfileAsync();
                return renderer;
            }
            catch { renderer.Dispose(); throw; }
        }

        public async Task UpdateDisplayProfileAsync()
        {
            await operations.WaitAsync();
            try
            {
                if (disposed) return;
                await UpdateDisplayProfileCoreAsync();
            }
            finally { operations.Release(); }
        }

        public async Task<bool> SourceFilesUnchangedAsync() => conditionFingerprint == await PreviewProfileAssociations.FingerprintAsync(configuration);

        private async Task UpdateDisplayProfileCoreAsync()
        {
            var previous = displayProfile;
            displayProfile = new ColorManagementProfile(CanvasColorSpace.Srgb);
            try
            {
                var display = DisplayInformation.GetForCurrentView();
                // Advanced Color manages our sRGB output. Legacy SDR needs
                // the Windows-assigned physical monitor profile explicitly.
                if (display.GetAdvancedColorInfo().CurrentAdvancedColorKind == AdvancedColorKind.StandardDynamicRange)
                {
                    using (var stream = await display.GetColorProfileAsync())
                    using (var reader = new DataReader(stream.GetInputStreamAt(0)))
                    {
                        if (stream.Size > 32 * 1024 * 1024) throw new InvalidDataException("Display ICC is too large.");
                        await reader.LoadAsync((uint)stream.Size);
                        var data = new byte[(int)stream.Size];
                        reader.ReadBytes(data);
                        var profile = ColorManagementProfile.CreateCustom(data);
                        displayProfile.Dispose();
                        displayProfile = profile;
                    }
                }
            }
            catch (Exception error)
            {
                App.WriteStartupException("Preview display profile unavailable; using sRGB", error);
            }
            previous?.Dispose();
            ClearPhotoProof();
        }

        public Size SourcePageSize(uint index)
        {
            var page = sourcePages[(int)index];
            return new Size(page.Width, page.Height);
        }

        public Size PageSize(uint index)
        {
            if (!originalSource && composedPreview)
            {
                var sheet = PaperSizeMm;
                return new Size(sheet.Width*96/25.4, sheet.Height*96/25.4);
            }
            return SourcePageSize(index);
        }

        public PreviewPhotoPlacement PhotoPlacement(uint index) => placements.TryGetValue(SourcePageIndex(index), out var value) ? value.Copy() : null;

        public void SetPhotoPlacement(PreviewPhotoPlacement placement)
        {
            placement.Validate(PaperSizeMm.Width, PaperSizeMm.Height);
            placements[placement.PageIndex] = placement.Copy();
        }

        public int SourcePageIndex(uint displayIndex) => (int)(reversePageOrder ? document.PageCount-1-displayIndex : displayIndex);

        public Rect PhotoPrintableArea => PrintableArea(new Size(PaperSizeMm.Width*96/25.4, PaperSizeMm.Height*96/25.4));

        public Size PaperSizeMm
        {
            get
            {
                // Placement is serialized in mm and validated against these
                // same catalog dimensions by JobRecordWriter. Rounded PPD
                // points describe imageable geometry, not the editing bounds.
                var width = paper.WidthMm ?? throw new InvalidDataException("Preview paper width is missing.");
                var height = paper.HeightMm ?? throw new InvalidDataException("Preview paper height is missing.");
                return configuration.GetNamedObject("layout").GetNamedString("orientation", "portrait") == "landscape"
                    ? new Size(height, width) : new Size(width, height);
            }
        }

        public PreviewPhotoPlacement InitialPhotoPlacement(uint index, bool includeRipLayout = true, bool sourceGeometry = false)
        {
            var size = sourceGeometry ? SourcePageSize(index) : PageSize(index); var sheet = PaperSizeMm;
            if (includeRipLayout && configuration.GetNamedObject("layout").GetNamedBoolean("apply_rip_layout", false))
            {
                Rect source, destination; Placement(size, out source, out destination);
                return new PreviewPhotoPlacement { PageIndex = SourcePageIndex(index), X = destination.X/size.Width*sheet.Width,
                    Y = destination.Y/size.Height*sheet.Height, Width = destination.Width/size.Width*sheet.Width,
                    Height = destination.Height/size.Height*sheet.Height };
            }
            var width = size.Width*25.4/96; var height = size.Height*25.4/96;
            if (Math.Abs(width-sheet.Width) > .01 || Math.Abs(height-sheet.Height) > .01)
            {
                var fit = Math.Min(sheet.Width/width, sheet.Height/height);
                width *= fit; height *= fit;
            }
            return new PreviewPhotoPlacement { PageIndex = SourcePageIndex(index), X = Math.Max(0, (sheet.Width-width)/2),
                Y = Math.Max(0, (sheet.Height-height)/2), Width = Math.Min(width, sheet.Width), Height = Math.Min(height, sheet.Height) };
        }

        public PreviewPhotoPlacement EffectivePhotoPlacement(PreviewPhotoPlacement placement, uint index)
        {
            if (!configuration.GetNamedObject("layout").GetNamedBoolean("apply_rip_layout", false)) return placement;
            var size = PageSize(index); var sheet = PaperSizeMm;
            Rect source, target; Placement(size, out source, out target);
            var result = placement.Copy();
            result.ApplyPageBox(target.X/size.Width*sheet.Width, target.Y/size.Height*sheet.Height,
                target.Width/size.Width*sheet.Width, target.Height/size.Height*sheet.Height, sheet.Width, sheet.Height);
            return result;
        }

        public async Task<CanvasImageSource> RenderAsync(uint index, bool proof, uint maxEdge, bool thumbnail = false)
        {
            var size = PageSize(index);
            return await RenderRegionAsync(index, proof, new Rect(0, 0, size.Width, size.Height),
                Math.Max(128, Math.Min(2048, maxEdge))/Math.Max(size.Width, size.Height), CancellationToken.None, !thumbnail);
        }

        public async Task<CanvasImageSource> RenderRegionAsync(uint index, bool proof, Rect region, double scale, CancellationToken cancellation = default(CancellationToken), bool usePhotoCache = true)
        {
            await operations.WaitAsync(cancellation);
            try { return await RenderRegionCoreAsync(index, proof, region, scale, cancellation, false, usePhotoCache); }
            finally { operations.Release(); }
        }

        private async Task<CanvasImageSource> RenderRegionCoreAsync(uint index, bool proof, Rect region, double scale, CancellationToken cancellation, bool capturePhoto = false, bool usePhotoCache = true)
        {
            cancellation.ThrowIfCancellationRequested();
            if (disposed) throw new ObjectDisposedException(nameof(PreviewRenderer));
            if (index >= document.PageCount) throw new ArgumentOutOfRangeException(nameof(index));
            if (region.Width <= 0 || region.Height <= 0 || scale <= 0)
                throw new ArgumentOutOfRangeException(nameof(region));
            if (usePhotoCache && !capturePhoto && !originalSource && PhotoPlacement(index) != null)
            {
                var sourceSize = SourcePageSize(index);
                var edge = PixelExtent(Math.Max(sourceSize.Width, sourceSize.Height)*scale);
                await EnsurePhotoProofAsync(index, proof, edge, cancellation);
                return ComposePhoto(index, region, scale);
            }
            var sourceOnly = originalSource || capturePhoto;
            var width = PixelExtent(region.Width*scale);
            var height = PixelExtent(region.Height*scale);
            // The backing bitmap has integral dimensions. Map the complete
            // requested region onto those dimensions rather than leaving the
            // fractional last row/column untouched after rounding upward.
            var scaleX = width/region.Width;
            var scaleY = height/region.Height;
            if ((long)width*height > (capturePhoto ? 8L : 5L)*1024*1024) throw new InvalidDataException("Preview region exceeds the memory limit.");
            using (var page = document.GetPage(index))
            using (var composed = new CanvasRenderTarget(device, width, height, 96))
            {
                var sourceSize = SourcePageSize(index);
                Rect source, destination;
                var pose = sourceOnly ? null : PhotoPlacement(index);
                Matrix3x2 transform;
                if (pose != null || (!sourceOnly && composedPreview))
                {
                    if (pose == null)
                    {
                        var fit = Math.Min(PaperSizeMm.Width/sourceSize.Width, PaperSizeMm.Height/sourceSize.Height);
                        pose = new PreviewPhotoPlacement { PageIndex = SourcePageIndex(index), Width = sourceSize.Width*fit, Height = sourceSize.Height*fit };
                        pose.X = (PaperSizeMm.Width-pose.Width)/2; pose.Y = (PaperSizeMm.Height-pose.Height)/2;
                        pose = EffectivePhotoPlacement(pose, index);
                    }
                    var values = pose.Matrix(sourceSize.Width, sourceSize.Height, 96/25.4);
                    transform = new Matrix3x2((float)values[0], (float)values[1], (float)values[2], (float)values[3], (float)values[4], (float)values[5]);
                    source = new Rect(0, 0, sourceSize.Width, sourceSize.Height);
                    destination = new Rect(pose.X*96/25.4, pose.Y*96/25.4, pose.Width*96/25.4, pose.Height*96/25.4);
                }
                else
                {
                    if (sourceOnly) source = destination = new Rect(0, 0, sourceSize.Width, sourceSize.Height);
                    else Placement(sourceSize, out source, out destination);
                    transform = Matrix3x2.CreateTranslation((float)-source.X, (float)-source.Y)
                        * Matrix3x2.CreateScale((float)(destination.Width/source.Width), (float)(destination.Height/source.Height))
                        * Matrix3x2.CreateTranslation((float)destination.X, (float)destination.Y);
                }
                var pdfToSource = sourcePages[(int)index].PdfToSourceMatrix();
                transform = new Matrix3x2((float)pdfToSource[0], (float)pdfToSource[1], (float)pdfToSource[2],
                    (float)pdfToSource[3], (float)pdfToSource[4], (float)pdfToSource[5]) * transform;
                var visible = destination;
                visible.Intersect(region);
                if (!sourceOnly) visible.Intersect(PrintableArea(PageSize(index)));
                using (var draw = composed.CreateDrawingSession()) draw.Clear(Colors.White);
                if (visible.Width > 0 && visible.Height > 0)
                {
                    if (!Matrix3x2.Invert(transform, out var inverse)) throw new InvalidDataException("Invalid preview placement transform.");
                    var corners = new[] { new Vector2((float)visible.Left,(float)visible.Top), new Vector2((float)visible.Right,(float)visible.Top),
                        new Vector2((float)visible.Left,(float)visible.Bottom), new Vector2((float)visible.Right,(float)visible.Bottom) }
                        .Select(point => Vector2.Transform(point, inverse)).ToArray();
                    var crop = new Rect(corners.Min(point => point.X), corners.Min(point => point.Y),
                        corners.Max(point => point.X)-corners.Min(point => point.X), corners.Max(point => point.Y)-corners.Min(point => point.Y));
                    crop.Intersect(new Rect(0, 0, page.Size.Width, page.Size.Height));
                    if (crop.Width <= 0 || crop.Height <= 0) throw new InvalidDataException("Empty preview source region.");
                    var sourceScaleX = Math.Sqrt(Math.Pow(transform.M11*scaleX,2)+Math.Pow(transform.M12*scaleY,2));
                    var sourceScaleY = Math.Sqrt(Math.Pow(transform.M21*scaleX,2)+Math.Pow(transform.M22*scaleY,2));
                    using (var stream = new InMemoryRandomAccessStream())
                    {
                        await page.RenderToStreamAsync(stream, new PdfPageRenderOptions
                        {
                            SourceRect = crop,
                            DestinationWidth = PixelExtent(crop.Width*sourceScaleX),
                            DestinationHeight = PixelExtent(crop.Height*sourceScaleY),
                            BackgroundColor = Colors.White,
                        }).AsTask(cancellation);
                        cancellation.ThrowIfCancellationRequested();
                        stream.Seek(0);
                        using (var bitmap = await CanvasBitmap.LoadAsync(device, stream, 96))
                        using (var gray = new ColorMatrixEffect
                        {
                            Source = bitmap,
                            ColorMatrix = new Matrix5x4
                            {
                                M11 = .299f, M12 = .299f, M13 = .299f,
                                M21 = .587f, M22 = .587f, M23 = .587f,
                                M31 = .114f, M32 = .114f, M33 = .114f, M44 = 1,
                            },
                        })
                        {
                            var gamma = PreviewToneMapping.Gamma((int)configuration.GetNamedObject("adjustments").GetNamedNumber("gamma", 0));
                            using (var corrected = new TableTransferEffect { Source = gray, RedTable = gamma, GreenTable = gamma, BlueTable = gamma, AlphaDisable = true })
                            using (var draw = composed.CreateDrawingSession())
                            {
                                draw.Transform = transform * Matrix3x2.CreateTranslation((float)-region.X, (float)-region.Y)
                                    * Matrix3x2.CreateScale((float)scaleX, (float)scaleY);
                                draw.DrawImage(corrected, crop, new Rect(0, 0, bitmap.Size.Width, bitmap.Size.Height));
                            }
                        }
                    }
                }
                cancellation.ThrowIfCancellationRequested();
                var correctedFrame = ApplyProof(composed, width, height, proof);
                if (capturePhoto)
                {
                    ClearPhotoProof(); photoProofBitmap = correctedFrame;
                    using (var white = new CanvasRenderTarget(device, 1, 1, 96))
                    {
                        using (var draw = white.CreateDrawingSession()) draw.Clear(Colors.White);
                        photoProofPaper = ApplyProof(white, 1, 1, proof);
                    }
                    photoProofImage = Present(photoProofBitmap);
                    photoProofPaperImage = Present(photoProofPaper);
                    return photoProofImage;
                }
                using (correctedFrame) return Present(correctedFrame);
            }
        }

        private CanvasRenderTarget ApplyProof(ICanvasImage source, uint width, uint height, bool proof)
        {
            using (var paperEffect = new TableTransferEffect { Source = source, AlphaDisable = true })
            using (var linearProof = new ColorManagementEffect
            {
                Source = source, SourceColorProfile = printerProfile ?? srgb, OutputColorProfile = linearSrgb,
                SourceRenderingIntent = ColorManagementRenderingIntent.AbsoluteColorimetric,
                OutputRenderingIntent = ColorManagementRenderingIntent.AbsoluteColorimetric,
            })
            using (var viewingLight = new ColorMatrixEffect { Source = linearProof })
            using (var color = new ColorManagementEffect
            {
                Source = source, SourceColorProfile = srgb, OutputColorProfile = displayProfile,
                SourceRenderingIntent = ColorManagementRenderingIntent.AbsoluteColorimetric,
                OutputRenderingIntent = ColorManagementRenderingIntent.AbsoluteColorimetric,
            })
                {
                    if (proof && printerProfile != null)
                    {
                        var step = (int)options.GetNamedNumber("illuminant_step", 0);
                        if (step == 0) color.SourceColorProfile = printerProfile;
                        else
                        {
                            var light = PreviewToneMapping.IlluminantMatrix(step);
                            viewingLight.ColorMatrix = new Matrix5x4 {
                                M11 = (float)light[0], M21 = (float)light[1], M31 = (float)light[2],
                                M12 = (float)light[3], M22 = (float)light[4], M32 = (float)light[5],
                                M13 = (float)light[6], M23 = (float)light[7], M33 = (float)light[8], M44 = 1 };
                            color.Source = viewingLight; color.SourceColorProfile = linearSrgb;
                        }
                    }
                    else if (proof)
                    {
                        var table = PreviewToneMapping.ApproximatePaper(mediaClass, options.GetNamedBoolean("paper_with_oba", false),
                            (int)options.GetNamedNumber("illuminant_step", 0));
                        paperEffect.RedTable = table[0]; paperEffect.GreenTable = table[1]; paperEffect.BlueTable = table[2];
                        color.Source = paperEffect;
                    }
                    var result = new CanvasRenderTarget(device, width, height, 96);
                    try
                    {
                        using (var draw = result.CreateDrawingSession()) { draw.Clear(Colors.Transparent); draw.DrawImage(color); }
                        return result;
                    }
                    catch { result.Dispose(); throw; }
                }
        }

        private CanvasImageSource Present(CanvasRenderTarget bitmap)
        {
            var result = new CanvasImageSource(device, (float)bitmap.Size.Width, (float)bitmap.Size.Height, 96);
            using (var draw = result.CreateDrawingSession(Colors.Transparent)) draw.DrawImage(bitmap);
            return result;
        }

        private void ClearPhotoProof()
        {
            photoProofBitmap?.Dispose(); photoProofPaper?.Dispose();
            photoProofBitmap = photoProofPaper = null;
            photoProofImage = photoProofPaperImage = null; photoProofEdge = 0;
        }

        private async Task EnsurePhotoProofAsync(uint index, bool proof, uint requestedEdge, CancellationToken cancellation)
        {
            var size = SourcePageSize(index);
            var longest = Math.Max(size.Width, size.Height);
            var limit = Math.Min(4096, Math.Sqrt(7.9*1024*1024/(size.Width*size.Height))*longest);
            var edge = (uint)Math.Min(limit, Math.Max(1200, requestedEdge));
            if (photoProofBitmap != null && photoProofPage == index && photoProofEnabled == proof && photoProofEdge >= edge) return;
            await RenderRegionCoreAsync(index, proof, new Rect(0, 0, size.Width, size.Height), edge/longest, cancellation, true);
            photoProofPage = index; photoProofEnabled = proof; photoProofEdge = edge;
        }

        public async Task<CanvasImageSource> RenderPhotoSourceAsync(uint index, bool proof)
        {
            await operations.WaitAsync();
            try
            {
                await EnsurePhotoProofAsync(index, proof, 1200, CancellationToken.None);
                return photoProofImage;
            }
            finally { operations.Release(); }
        }

        private CanvasImageSource ComposePhoto(uint index, Rect region, double scale)
        {
            var width = PixelExtent(region.Width*scale); var height = PixelExtent(region.Height*scale);
            if ((long)width*height > 5*1024*1024) throw new InvalidDataException("Preview region exceeds the memory limit.");
            var pose = PhotoPlacement(index);
            var values = pose.Matrix(photoProofBitmap.Size.Width, photoProofBitmap.Size.Height, 96/25.4);
            var transform = new Matrix3x2((float)values[0], (float)values[1], (float)values[2], (float)values[3], (float)values[4], (float)values[5]);
            var result = new CanvasImageSource(device, width, height, 96);
            using (var draw = result.CreateDrawingSession(Colors.Transparent))
            {
                draw.DrawImage(photoProofPaper, new Rect(0, 0, width, height), new Rect(0, 0, 1, 1), 1, CanvasImageInterpolation.NearestNeighbor);
                var printable = PrintableArea(PageSize(index)); printable.Intersect(region);
                if (printable.Width > 0 && printable.Height > 0)
                {
                    var sx = width/region.Width; var sy = height/region.Height;
                    using (draw.CreateLayer(1, new Rect((printable.X-region.X)*sx, (printable.Y-region.Y)*sy, printable.Width*sx, printable.Height*sy)))
                    {
                        draw.Transform = transform * Matrix3x2.CreateTranslation((float)-region.X, (float)-region.Y)
                            * Matrix3x2.CreateScale((float)sx, (float)sy);
                        draw.DrawImage(photoProofBitmap);
                    }
                }
            }
            return result;
        }

        // Pixel-grid snapping can produce N + a floating point epsilon. Avoid
        // accidentally allocating N+1 and resampling an otherwise exact 100%.
        private static uint PixelExtent(double value) => Math.Max(1, (uint)Math.Ceiling(value-1e-6));

        private Rect PrintableArea(Size sourceSize)
        {
            var printer = configuration.GetNamedObject("printer");
            var region = paper.PreviewRegions?.FirstOrDefault(item =>
                item.Modes.Contains(printer.GetNamedString("mode"))
                && item.InputSources.Contains(printer.GetNamedString("input_source")));
            var points = region?.Points ?? paper.PreviewImageablePoints;
            var pw = paper.PreviewPaperPoints[0];
            var ph = paper.PreviewPaperPoints[1];
            var left = Math.Max(0, points[0]); var top = Math.Max(0, ph-points[3]);
            var right = Math.Min(pw, points[2]); var bottom = Math.Min(ph, ph-points[1]);
            if ((sourceSize.Width > sourceSize.Height) != (pw > ph))
            {
                var oldLeft = left; var oldTop = top; var oldRight = right; var oldBottom = bottom;
                left = ph-oldBottom; top = oldLeft; right = ph-oldTop; bottom = oldRight;
                var oldWidth = pw; pw = ph; ph = oldWidth;
            }
            var sx = sourceSize.Width/pw; var sy = sourceSize.Height/ph;
            return new Rect(left*sx, top*sy, (right-left)*sx, (bottom-top)*sy);
        }

        private void Placement(Size sourceSize, out Rect sourceRectangle, out Rect destination)
        {
            var area = PrintableArea(sourceSize);
            var sheet = PaperSizeMm;
            var layout = configuration.GetNamedObject("layout");
            destination = area;
            sourceRectangle = area;
            if (!layout.GetNamedBoolean("apply_rip_layout", false)) return;
            sourceRectangle = new Rect(0, 0, sourceSize.Width, sourceSize.Height);
            var aspect = sourceSize.Width/sourceSize.Height;
            double targetWidth, targetHeight;
            var mmX = sourceSize.Width/sheet.Width; var mmY = sourceSize.Height/sheet.Height;
            var w = Number(layout, "width_mm", 0); var h = Number(layout, "height_mm", 0);
            if (w > 0 || h > 0)
            {
                var widthMm = w > 0 ? (h > 0 ? Math.Min(w, h*aspect) : w) : h*aspect;
                targetWidth = widthMm*mmX; targetHeight = widthMm/aspect*mmY;
            }
            else if (layout.GetNamedString("sizing", "fit") == "native" || layout.GetNamedString("sizing", "fit") == "scale")
            {
                var scale = Number(layout, "scale_percent", 100)/100;
                targetWidth = sourceSize.Width/96*25.4*scale*mmX;
                targetHeight = sourceSize.Height/96*25.4*scale*mmY;
            }
            else
            {
                var fit = Math.Min(area.Width/sourceSize.Width, area.Height/sourceSize.Height);
                targetWidth = sourceSize.Width*fit; targetHeight = sourceSize.Height*fit;
            }
            var center = layout.GetNamedString("placement", "center") == "center";
            destination = new Rect(area.X + (center ? (area.Width-targetWidth)/2 : Number(layout,"x_mm",0)*mmX),
                area.Y + (center ? (area.Height-targetHeight)/2 : Number(layout,"y_mm",0)*mmY), targetWidth, targetHeight);
            if (destination.Left < area.Left-1 || destination.Top < area.Top-1
                || destination.Right > area.Right+1 || destination.Bottom > area.Bottom+1)
                throw new InvalidDataException("RIP layout exceeds the printable paper area.");
        }

        private static double Number(JsonObject data, string key, double fallback)
        {
            var value = data.GetNamedValue(key, null);
            return value?.ValueType == JsonValueType.Number ? value.GetNumber() : fallback;
        }

        public void Dispose()
        {
            if (disposed) return;
            disposed = true;
            ClearPhotoProof();
            printerProfile?.Dispose();
            displayProfile?.Dispose();
            srgb.Dispose();
            linearSrgb.Dispose();
            device.Dispose();
        }

        public async Task CloseAsync()
        {
            await operations.WaitAsync();
            try { Dispose(); }
            finally { operations.Release(); }
        }
    }
}
