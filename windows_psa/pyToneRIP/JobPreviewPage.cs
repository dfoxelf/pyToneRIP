using System;
using System.IO;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Globalization;
using System.Linq;
using Microsoft.Graphics.Canvas.UI.Xaml;
using Windows.Foundation;
using Windows.System;
using Windows.UI.ViewManagement;
using Windows.UI.Xaml.Input;
using Windows.UI.Xaml.Data;
using Windows.UI.Xaml.Markup;
using System.Runtime.Serialization.Json;
using System.Threading.Tasks;
using System.Threading;
using pyToneRIP.Services;
using pyToneRIP.Shared;
using Windows.Data.Json;
using Windows.Graphics.Display;
using Windows.Graphics.Printing.Workflow;
using Windows.Storage;
using Windows.UI;
using Windows.UI.Core;
using Windows.UI.Xaml;
using Windows.UI.Xaml.Controls;
using Windows.UI.Xaml.Media;

namespace pyToneRIP
{
    public sealed class PreviewThumbnail : INotifyPropertyChanged
    {
        private CanvasImageSource bitmap;
        public string Caption { get; set; }
        public double PaperWidth { get; set; }
        public double PaperHeight { get; set; }
        public double PaperLeft { get; set; }
        public double PaperTop { get; set; }
        public Geometry PrintStartMarker { get; set; }
        public string PrintStartHint { get; set; }
        public CanvasImageSource Bitmap { get => bitmap; set { bitmap = value; PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Bitmap))); } }
        public event PropertyChangedEventHandler PropertyChanged;
    }

    public sealed partial class JobPreviewPage : Page
    {
        private readonly PrintWorkflowJobUISession session;
        private readonly TaskCompletionSource<bool> closed = new TaskCompletionSource<bool>();
        private readonly TextBlock title = new TextBlock { FontSize = 22, TextWrapping = TextWrapping.Wrap };
        private readonly TextBlock status = new TextBlock { TextWrapping = TextWrapping.Wrap };
        private readonly TextBlock physicalStatus = new TextBlock { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0,5,0,0) };
        private readonly TextBlock pageNumber = new TextBlock { VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(10,0,10,0) };
        private readonly Image image = new Image { Stretch = Stretch.Uniform };
        private readonly ScrollViewer viewer = new ScrollViewer
        {
            ZoomMode = ZoomMode.Enabled, MinZoomFactor = .1f, MaxZoomFactor = 512,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalContentAlignment = HorizontalAlignment.Left,
            VerticalContentAlignment = VerticalAlignment.Top,
        };
        private readonly ProgressBar progress = new ProgressBar { Minimum = 0, Maximum = 100, IsIndeterminate = true, Margin = new Thickness(0,8,0,8) };
        private readonly Button print = new Button { IsEnabled = false };
        private readonly Button cancel = new Button();
        private readonly Button previous = new Button { IsEnabled = false };
        private readonly Button next = new Button { IsEnabled = false };
        private readonly CheckBox proof = new CheckBox { IsChecked = true, VerticalContentAlignment = VerticalAlignment.Center };
        private readonly Button printClose = new Button { IsEnabled = false };
        private readonly Button editSettings = new Button { IsEnabled = false };
        private readonly Button actualSize = new Button { Content = "100%" };
        private readonly TextBox zoomBox = new TextBox { Width = 76, Text = "100%", VerticalContentAlignment = VerticalAlignment.Center };
        private readonly Canvas surface = new Canvas { Background = new SolidColorBrush(Colors.White) };
        private readonly Canvas detailLayer = new Canvas();
        private readonly Image detail = new Image { Stretch = Stretch.Fill };
        private CancellationTokenSource detailCancellation;
        private Rect detailRegion;
        private double detailScale;
        private uint detailPage;
        private bool detailProof, hasDetail;
        private int detailContentRevision;
        private readonly ListView thumbnails = new ListView { Width = 145, SelectionMode = ListViewSelectionMode.Single };
        private readonly ObservableCollection<PreviewThumbnail> thumbnailItems = new ObservableCollection<PreviewThumbnail>();
        private readonly StackPanel information = new StackPanel { Width = 235, Spacing = 8, Margin = new Thickness(16,0,0,0) };
        private readonly CanvasControl horizontalRuler = new CanvasControl { Height = 25, Visibility = Visibility.Collapsed };
        private readonly CanvasControl verticalRuler = new CanvasControl { Width = 28, Visibility = Visibility.Collapsed };
        private readonly CheckBox rulers = new CheckBox { VerticalContentAlignment = VerticalAlignment.Center };
        private readonly ComboBox units = new ComboBox { ItemsSource = new[] { "mm", "in" }, SelectedIndex = 0, MinWidth = 68 };
        private readonly Queue<uint> thumbnailQueue = new Queue<uint>();
        private readonly Queue<uint> thumbnailOrder = new Queue<uint>();
        private readonly HashSet<uint> thumbnailPending = new HashSet<uint>();
        private bool thumbnailRunning, detailRunning, detached, fitting = true, changingPage, editing, approving;
        private int renderRevision;
        private string printerUri, queueName;
        private int viewRevision;
        private double worldScale = 1;
        private Size pageSize;
        private Point? lastPageOrigin;
        private double lastPageZoom;
        private StorageFolder folder;
        private PreviewRenderer renderer;
        private DisplayInformation display;
        private uint pageIndex;
        private bool terminal;
        private bool approved;
        private bool rendering;
        private bool previewValid;
        private bool renderAgain;
        private bool invalidateProof;
        private bool monitorRunning;
        private const uint requestedEdge = 900;

        public JobPreviewPage(PrintWorkflowJobUISession session)
        {
            this.session = session ?? throw new ArgumentNullException(nameof(session));
            Background = (Brush)Application.Current.Resources["PageBackgroundBrush"];
            title.Text = Text("Title"); status.Text = Text("Preparing");
            print.Content = Text("Print"); cancel.Content = Text("Cancel");
            ToolTipService.SetToolTip(cancel, Text("CancelCloseHelp"));
            printClose.Content = Text("PrintClose"); rulers.Content = Text("Rulers");
            editSettings.Content = Text("EditSettings");
            ApplicationView.GetForCurrentView().Title = Text("Title");
            previous.Content = Text("Previous"); next.Content = Text("Next"); proof.Content = "Softproofing";
            var toolbar = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(0,12,0,12) };
            toolbar.Children.Add(previous); toolbar.Children.Add(pageNumber); toolbar.Children.Add(next);
            var minus = new Button { Content = "−" };
            var plus = new Button { Content = "+" };
            var fit = new Button { Content = Text("Fit") };
            toolbar.Children.Add(minus); toolbar.Children.Add(plus); toolbar.Children.Add(zoomBox);
            toolbar.Children.Add(actualSize); toolbar.Children.Add(fit); toolbar.Children.Add(rulers);
            toolbar.Children.Add(units); toolbar.Children.Add(proof);
            toolbar.Children.Add(editSettings);
            Loaded += (_, __) => WidenRussianPreviewWindow(toolbar);
            ToolTipService.SetToolTip(zoomBox, Text("ZoomHelp"));
            ToolTipService.SetToolTip(actualSize, Text("ActualSizeHelp"));
            var toolbarScroll = new ScrollViewer { Content = toolbar, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                VerticalScrollBarVisibility = ScrollBarVisibility.Disabled, HorizontalScrollMode = ScrollMode.Enabled,
                VerticalScrollMode = ScrollMode.Disabled };
            var photoToolbar = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(0,0,0,10) };
            AddPhotoTools(photoToolbar);
            var photoToolbarScroll = new ScrollViewer { Content = photoToolbar, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                VerticalScrollBarVisibility = ScrollBarVisibility.Disabled, HorizontalScrollMode = ScrollMode.Enabled, VerticalScrollMode = ScrollMode.Disabled };
            var header = new StackPanel(); header.Children.Add(title); header.Children.Add(toolbarScroll); header.Children.Add(photoToolbarScroll);
            var footer = new StackPanel(); footer.Children.Add(status); footer.Children.Add(progress); footer.Children.Add(physicalStatus);
            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Spacing = 10 };
            buttons.Children.Add(print); buttons.Children.Add(printClose); buttons.Children.Add(cancel); footer.Children.Add(buttons);
            var grid = new Grid { Padding = new Thickness(24) };
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            grid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            detailLayer.Children.Add(detail); surface.Children.Add(image); surface.Children.Add(detailLayer); InstallPhotoOverlay(); viewer.Content = surface;
            var viewport = new Grid();
            viewport.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            viewport.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            viewport.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            viewport.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            Grid.SetColumn(horizontalRuler, 1); Grid.SetRow(verticalRuler, 1);
            Grid.SetRow(viewer, 1); Grid.SetColumn(viewer, 1);
            viewport.Children.Add(horizontalRuler); viewport.Children.Add(verticalRuler); viewport.Children.Add(viewer);
            var body = new Grid();
            body.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            body.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            body.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            Grid.SetColumn(viewport, 1);
            var infoScroll = new ScrollViewer { Content = information, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
            Grid.SetColumn(infoScroll, 2);
            var thumbnailPanel = new Grid { Width = 145 };
            thumbnailPanel.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            thumbnailPanel.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            var printStartLabel = new TextBlock { Text = Text("PrintStart"), FontSize = 12,
                TextWrapping = TextWrapping.Wrap, TextAlignment = TextAlignment.Center, Margin = new Thickness(0,0,0,4) };
            ToolTipService.SetToolTip(printStartLabel, Text("PrintStartHelp"));
            Grid.SetRow(thumbnails, 1);
            thumbnailPanel.Children.Add(printStartLabel); thumbnailPanel.Children.Add(thumbnails);
            body.Children.Add(thumbnailPanel); body.Children.Add(viewport); body.Children.Add(infoScroll);
            Grid.SetRow(body, 1); Grid.SetRow(footer, 2);
            grid.Children.Add(header); grid.Children.Add(body); grid.Children.Add(footer);
            thumbnails.ItemsSource = thumbnailItems;
            thumbnails.ItemTemplate = (DataTemplate)XamlReader.Load(
                "<DataTemplate xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'><StackPanel Margin='3,5,3,5'>" +
                "<Canvas Width='112' Height='150' ToolTipService.ToolTip='{Binding PrintStartHint}'>" +
                "<Image Source='{Binding Bitmap}' Canvas.Left='{Binding PaperLeft}' Canvas.Top='{Binding PaperTop}' " +
                "Width='{Binding PaperWidth}' Height='{Binding PaperHeight}' Stretch='Uniform'/>" +
                "<Path Data='{Binding PrintStartMarker}' Fill='DarkOrange' Stroke='White' StrokeThickness='1' IsHitTestVisible='False'/></Canvas>" +
                "<TextBlock Text='{Binding Caption}' HorizontalAlignment='Center'/></StackPanel></DataTemplate>");
            thumbnails.SelectionChanged += async (_, __) =>
            {
                if (changingPage || thumbnails.SelectedIndex < 0 || pageIndex == (uint)thumbnails.SelectedIndex) return;
                pageIndex = (uint)thumbnails.SelectedIndex; fitting = true; await RenderAsync();
            };
            thumbnails.ContainerContentChanging += (_, args) =>
            {
                if (!args.InRecycleQueue) QueueThumbnail((uint)args.ItemIndex);
            };
            horizontalRuler.Draw += (_, args) => DrawRuler(args.DrawingSession, true);
            verticalRuler.Draw += (_, args) => DrawRuler(args.DrawingSession, false);
            rulers.Checked += (_, __) => SetRulers(); rulers.Unchecked += (_, __) => SetRulers();
            rulers.IsChecked = true;
            units.SelectionChanged += (_, __) => InvalidateRulers();
            Content = grid;
            previous.Click += async (_, __) => { if (pageIndex > 0) { pageIndex--; fitting = true; await RenderAsync(); } };
            next.Click += async (_, __) => { if (renderer != null && pageIndex+1 < renderer.PageCount) { pageIndex++; fitting = true; await RenderAsync(); } };
            proof.Checked += async (_, __) => await RenderAsync(true);
            proof.Unchecked += async (_, __) => await RenderAsync(true);
            minus.Click += (_, __) => ChangeZoom(viewer.ZoomFactor/1.25);
            plus.Click += (_, __) => ChangeZoom(viewer.ZoomFactor*1.25);
            actualSize.Click += (_, __) => ChangeZoom(OnePixelZoom());
            zoomBox.KeyDown += (_, args) => { if (args.Key == VirtualKey.Enter) { ApplyZoomText(); args.Handled = true; } };
            zoomBox.LostFocus += (_, __) => ApplyZoomText();
            fit.Click += (_, __) => { fitting = true; FitImage(); };
            viewer.SizeChanged += (_, __) => { if (fitting) FitImage(); InvalidateRulers(); ScheduleDetail(); };
            viewer.ViewChanged += (_, __) => { UpdateZoomText(); InvalidateRulers(); UpdatePhotoOverlay(); ScheduleDetail(); };
            surface.LayoutUpdated += SurfaceLayoutUpdated;
            viewer.AddHandler(UIElement.PointerWheelChangedEvent, new PointerEventHandler(WheelChanged), true);
            printClose.Click += async (_, __) => await ApproveAsync(true);
            editSettings.Click += async (_, __) => await EditSettingsAsync();
            print.Click += PrintClicked;
            cancel.Click += CancelClicked;
            Window.Current.Closed += WindowClosed;
        }

        private static string Text(string key) => LocalizationService.Get("PreviewWindow." + key);

        private PreviewThumbnail CreateThumbnail(uint index)
        {
            var size = renderer.PageSize(index);
            var scale = Math.Min(92/size.Width, 130/size.Height);
            var width = size.Width*scale; var height = size.Height*scale;
            var left = (112-width)/2; var top = (150-height)/2;
            var x = left+width/2; var y = top+height/2;
            Point[] points;
            switch (renderer.PrintStartEdge)
            {
                case 1: points = new[] { new Point(left+width+9,y-6), new Point(left+width+9,y+6), new Point(left+width-2,y) }; break;
                case 2: points = new[] { new Point(x-6,top+height+9), new Point(x+6,top+height+9), new Point(x,top+height-2) }; break;
                case 3: points = new[] { new Point(left-9,y-6), new Point(left-9,y+6), new Point(left+2,y) }; break;
                default: points = new[] { new Point(x-6,top-9), new Point(x+6,top-9), new Point(x,top+2) }; break;
            }
            var figure = new PathFigure { StartPoint = points[0], IsClosed = true };
            figure.Segments.Add(new LineSegment { Point = points[1] });
            figure.Segments.Add(new LineSegment { Point = points[2] });
            var marker = new PathGeometry(); marker.Figures.Add(figure);
            return new PreviewThumbnail { Caption = (index+1).ToString(), PaperWidth = width, PaperHeight = height,
                PaperLeft = left, PaperTop = top, PrintStartMarker = marker, PrintStartHint = Text("PrintStartHelp") };
        }

        private static void WidenRussianPreviewWindow(StackPanel toolbar)
        {
            var language = LocalizationService.CurrentLanguage;
            if (string.IsNullOrWhiteSpace(language))
                language = Windows.Globalization.ApplicationLanguages.Languages.FirstOrDefault();
            if (!(language == "ru" || language?.StartsWith("ru-", StringComparison.OrdinalIgnoreCase) == true))
                return;
            toolbar.Measure(new Size(double.PositiveInfinity, double.PositiveInfinity));
            var width = toolbar.DesiredSize.Width + 72;
            var screen = DisplayInformation.GetForCurrentView();
            if (screen.ScreenWidthInRawPixels > 0 && screen.RawPixelsPerViewPixel > 0)
                width = Math.Min(width, screen.ScreenWidthInRawPixels/screen.RawPixelsPerViewPixel-32);
            var bounds = Window.Current.Bounds;
            if (width > bounds.Width)
                ApplicationView.GetForCurrentView().TryResizeView(new Size(width, bounds.Height));
        }

        public void Start()
        {
            session.VirtualPrinterUIDataAvailable += DataAvailable;
            session.Start();
        }

        private async void DataAvailable(PrintWorkflowJobUISession sender, PrintWorkflowVirtualPrinterUIEventArgs args)
        {
            var deferral = args.GetDeferral();
            try
            {
                await OnUiAsync(async () =>
                {
                    folder = await PreviewWorkflowStore.OpenJobAsync(args.Configuration.SessionId);
                    queueName = args.Printer.PrinterName; printerUri = args.Printer.PrinterUri?.ToString();
                    title.Text = Text("Title") + " — " + args.Configuration.JobTitle + " — " + args.Printer.PrinterName;
                    _ = MonitorProgressAsync();
                    while (!closed.Task.IsCompleted
                        && !File.Exists(Path.Combine(folder.Path, "preview-capture-ready")))
                        await Task.Delay(150);
                    if (closed.Task.IsCompleted) return;
                    renderer = await PreviewRenderer.OpenAsync(folder);
                    if (renderer.PageCount == 0) throw new InvalidDataException("Print job has no preview pages.");
                    display = DisplayInformation.GetForCurrentView();
                    display.ColorProfileChanged += DisplayProfileChanged;
                    display.AdvancedColorInfoChanged += AdvancedColorChanged;
                    UpdateInformation();
                    actualSize.IsEnabled = renderer.RasterDpiX > 0 && renderer.RasterDpiX == renderer.RasterDpiY;
                    for (uint i = 0; i < renderer.PageCount; i++) thumbnailItems.Add(CreateThumbnail(i));
                    await RenderAsync();
                    if (image.Source == null) throw new InvalidDataException("Print preview could not be rendered.");
                    if (!closed.Task.IsCompleted)
                    {
                        print.IsEnabled = printClose.IsEnabled = editSettings.IsEnabled = true;
                    }
                    await closed.Task;
                });
            }
            catch (Exception error)
            {
                App.WriteStartupException("Print preview failed", error);
                await OnUiAsync(async () =>
                {
                    if (folder != null)
                        await PreviewWorkflowStore.WriteAtomicAsync(folder, "preview-failed.txt", error.ToString());
                    terminal = true; print.IsEnabled = printClose.IsEnabled = false; RefreshPhotoActions();
                    status.Text = Text("Failed") + Environment.NewLine + error.Message;
                    cancel.Content = Text("Close");
                    ToolTipService.SetToolTip(cancel, Text("Close"));
                    progress.IsIndeterminate = false;
                    await closed.Task;
                });
            }
            finally
            {
                await OnUiAsync(() =>
                {
                    if (display != null)
                    {
                        display.ColorProfileChanged -= DisplayProfileChanged;
                        display.AdvancedColorInfoChanged -= AdvancedColorChanged;
                    }
                    session.VirtualPrinterUIDataAvailable -= DataAvailable;
                    Window.Current.Closed -= WindowClosed;
                    surface.LayoutUpdated -= SurfaceLayoutUpdated;
                    return Task.CompletedTask;
                });
                detailCancellation?.Cancel();
                while (rendering || detailRunning || thumbnailRunning || photoLoading || applyingPhoto) await Task.Delay(20);
                await OnUiAsync(async () =>
                {
                    image.Source = detail.Source = null;
                    foreach (var item in thumbnailItems) item.Bitmap = null;
                    horizontalRuler.RemoveFromVisualTree(); verticalRuler.RemoveFromVisualTree();
                    await ClosePhotoSourceAsync();
                });
                if (renderer != null) await renderer.CloseAsync();
                try { deferral.Complete(); }
                catch (Exception error) { App.WriteStartupException("Preview UI deferral completion failed", error); }
                await OnUiAsync(() => { Window.Current.Close(); return Task.CompletedTask; });
            }
        }

        private async Task OnUiAsync(Func<Task> action)
        {
            if (Dispatcher.HasThreadAccess) { await action(); return; }
            var done = new TaskCompletionSource<bool>();
            await Dispatcher.RunAsync(CoreDispatcherPriority.Normal, async () =>
            {
                try { await action(); done.TrySetResult(true); }
                catch (Exception error) { done.TrySetException(error); }
            });
            await done.Task;
        }

        private async Task RenderAsync(bool invalidate = false)
        {
            if (renderer == null || editing || closed.Task.IsCompleted) return;
            if (!applyingPhoto && photoMode.IsChecked == true && (invalidate || photoPlacement?.PageIndex != renderer.SourcePageIndex(pageIndex)))
                await DisablePhotoModeAsync();
            invalidateProof |= invalidate;
            if (rendering) { renderAgain = true; return; }
            rendering = true; previewValid = false;
            print.IsEnabled = printClose.IsEnabled = false;
            viewRevision++; ClearDetail();
            var success = false;
            try
            {
                do
                {
                    renderAgain = false;
                    if (invalidateProof)
                    {
                        invalidateProof = false; renderRevision++;
                        ClearDetail();
                        foreach (var item in thumbnailItems) item.Bitmap = null;
                        thumbnailOrder.Clear();
                    }
                    var index = pageIndex; var simulation = proof.IsChecked == true;
                    var revision = renderRevision;
                    var bitmap = await renderer.RenderAsync(index, simulation, requestedEdge);
                    if (closed.Task.IsCompleted) return;
                    if (index != pageIndex || simulation != (proof.IsChecked == true) || revision != renderRevision || invalidateProof) continue;
                    image.Source = bitmap;
                    pageSize = renderer.PageSize(pageIndex);
                    worldScale = Math.Min(1, 2048/Math.Max(pageSize.Width, pageSize.Height));
                    surface.Width = image.Width = detailLayer.Width = pageSize.Width*worldScale;
                    surface.Height = image.Height = detailLayer.Height = pageSize.Height*worldScale;
                    UpdatePhotoOverlay();
                    changingPage = true; thumbnails.SelectedIndex = (int)pageIndex; changingPage = false;
                    thumbnails.ScrollIntoView(thumbnailItems[(int)pageIndex]);
                    pageNumber.Text = (pageIndex+1) + " / " + renderer.PageCount;
                    previous.IsEnabled = pageIndex > 0;
                    next.IsEnabled = pageIndex+1 < renderer.PageCount;
                    if (fitting) FitImage();
                    ScheduleDetail();
                    for (var i = Math.Max(0, (int)pageIndex-3); i < Math.Min(thumbnailItems.Count, (int)pageIndex+5); i++) QueueThumbnail((uint)i);
                } while (renderAgain);
                success = true;
                if (!approved && !terminal)
                    status.Text = proof.IsChecked == true
                        ? Text(renderer.Measured ? "Measured" : "Approximate") : Text("Unsimulated");
            }
            catch (Exception error)
            {
                print.IsEnabled = false;
                status.Text = Text("Failed") + Environment.NewLine + error.Message;
                App.WriteStartupException("Print preview render failed", error);
            }
            finally
            {
                rendering = false; previewValid = success;
                RefreshPhotoActions();
                if (success && !approved && !terminal && !editing && !closed.Task.IsCompleted) print.IsEnabled = printClose.IsEnabled = editSettings.IsEnabled = true;
            }
        }

        private void FitImage()
        {
            if (pageSize.Width <= 0 || viewer.ViewportWidth <= 1 || viewer.ViewportHeight <= 1) return;
            var factor = Math.Min((viewer.ViewportWidth-18)/surface.Width, (viewer.ViewportHeight-18)/surface.Height);
            viewer.ChangeView(0, 0, (float)Math.Max(viewer.MinZoomFactor, Math.Min(viewer.MaxZoomFactor, factor)), true);
            UpdateZoomText();
        }

        private double OnePixelZoom() => (renderer?.RasterDpiX > 0 ? renderer.RasterDpiX/96.0 : 1)
            /worldScale/(display?.RawPixelsPerViewPixel ?? 1);

        private void UpdateZoomText() => zoomBox.Text = (100*viewer.ZoomFactor/OnePixelZoom()).ToString("0.#", CultureInfo.CurrentCulture) + "%";

        private void ApplyZoomText()
        {
            if (double.TryParse(zoomBox.Text.Trim().TrimEnd('%'), NumberStyles.Float, CultureInfo.CurrentCulture, out var percent)
                && !double.IsNaN(percent) && !double.IsInfinity(percent) && percent > 0)
                ChangeZoom(OnePixelZoom()*percent/100);
            else UpdateZoomText();
        }

        private void ChangeZoom(double factor, Point? anchor = null)
        {
            fitting = false;
            factor = Math.Max(viewer.MinZoomFactor, Math.Min(viewer.MaxZoomFactor, factor));
            var point = anchor ?? new Point(viewer.ViewportWidth/2, viewer.ViewportHeight/2);
            var ratio = factor/viewer.ZoomFactor;
            viewer.ChangeView(Math.Max(0, (viewer.HorizontalOffset+point.X)*ratio-point.X),
                Math.Max(0, (viewer.VerticalOffset+point.Y)*ratio-point.Y), (float)factor, true);
        }

        private void WheelChanged(object sender, PointerRoutedEventArgs args)
        {
            if ((Window.Current.CoreWindow.GetKeyState(VirtualKey.Control) & CoreVirtualKeyStates.Down) == 0) return;
            var point = args.GetCurrentPoint(viewer);
            ChangeZoom(viewer.ZoomFactor*Math.Pow(1.15, point.Properties.MouseWheelDelta/120.0), point.Position);
            args.Handled = true;
        }

        private void SetRulers()
        {
            horizontalRuler.Visibility = verticalRuler.Visibility = rulers.IsChecked == true ? Visibility.Visible : Visibility.Collapsed;
            units.IsEnabled = rulers.IsChecked == true; InvalidateRulers();
        }

        private void InvalidateRulers() { horizontalRuler.Invalidate(); verticalRuler.Invalidate(); }

        private void SurfaceLayoutUpdated(object sender, object args)
        {
            if (renderer == null || closed.Task.IsCompleted || surface.ActualWidth <= 0) return;
            var origin = surface.TransformToVisual(viewer).TransformPoint(new Point());
            if (lastPageOrigin.HasValue && Math.Abs(origin.X-lastPageOrigin.Value.X) < .01
                && Math.Abs(origin.Y-lastPageOrigin.Value.Y) < .01 && Math.Abs(viewer.ZoomFactor-lastPageZoom) < .00001) return;
            lastPageOrigin = origin; lastPageZoom = viewer.ZoomFactor;
            InvalidateRulers(); ScheduleDetail();
        }

        private void DrawRuler(Microsoft.Graphics.Canvas.CanvasDrawingSession draw, bool horizontal)
        {
            var size = horizontal ? horizontalRuler.ActualWidth : verticalRuler.ActualHeight;
            var unitDips = units.SelectedIndex == 1 ? 96.0 : 96/25.4;
            // The paper can be centered inside a larger viewport. Scroll
            // offsets alone omit that gap; measure the actual paper transform.
            var ruler = horizontal ? horizontalRuler : verticalRuler;
            var transform = surface.TransformToVisual(ruler);
            var origin = transform.TransformPoint(new Point());
            var unit = transform.TransformPoint(horizontal ? new Point(unitDips*worldScale, 0) : new Point(0, unitDips*worldScale));
            var offset = -(horizontal ? origin.X : origin.Y);
            var pixels = horizontal ? unit.X-origin.X : unit.Y-origin.Y;
            if (pixels <= 0 || pageSize.Width <= 0) return;
            var major = Math.Pow(10, Math.Floor(Math.Log10(75/pixels)));
            if (major*pixels < 35) major *= 5; else if (major*pixels < 60) major *= 2;
            var minor = major/5;
            var first = Math.Max(0, Math.Floor(offset/pixels/minor));
            var end = Math.Min((offset+size)/pixels, (horizontal ? pageSize.Width : pageSize.Height)/unitDips);
            var color = (Foreground as SolidColorBrush)?.Color ?? Colors.Gray;
            using (var format = new Microsoft.Graphics.Canvas.Text.CanvasTextFormat { FontSize = 10 })
            for (var tick = first; tick*minor <= end; tick++)
            {
                var position = (float)(tick*minor*pixels-offset);
                var isMajor = (long)tick % 5 == 0;
                if (horizontal) draw.DrawLine(position, 25, position, isMajor ? 12 : 19, color);
                else draw.DrawLine(28, position, isMajor ? 14 : 23, position, color);
                if (isMajor)
                {
                    var label = (tick*minor).ToString("0.##", CultureInfo.CurrentCulture);
                    draw.DrawText(label, horizontal ? position+2 : 0, horizontal ? 0 : position+1, color, format);
                }
            }
        }

        private void ClearDetail()
        {
            detailCancellation?.Cancel();
            detail.Source = null; detail.Visibility = Visibility.Collapsed; hasDetail = false;
        }

        private Rect VisiblePageRegion()
        {
            if (viewer.ViewportWidth <= 0 || viewer.ViewportHeight <= 0 || pageSize.Width <= 0) return new Rect();
            var inverse = surface.TransformToVisual(viewer).Inverse;
            if (inverse == null) return new Rect();
            var visible = inverse.TransformBounds(new Rect(0, 0, viewer.ViewportWidth, viewer.ViewportHeight));
            var left = Math.Max(0, visible.Left/worldScale); var top = Math.Max(0, visible.Top/worldScale);
            var right = Math.Min(pageSize.Width, visible.Right/worldScale); var bottom = Math.Min(pageSize.Height, visible.Bottom/worldScale);
            return right > left && bottom > top ? new Rect(left, top, right-left, bottom-top) : new Rect();
        }

        private bool DetailCovers(Rect visible) => hasDetail && visible.Width > 0 && visible.Height > 0
            && detailPage == pageIndex && detailProof == (proof.IsChecked == true) && detailContentRevision == renderRevision
            && detailRegion.Left <= visible.Left && detailRegion.Top <= visible.Top
            && detailRegion.Right >= visible.Right && detailRegion.Bottom >= visible.Bottom;

        private async void ScheduleDetail()
        {
            if (renderer == null || editing || photoDragging || closed.Task.IsCompleted) return;
            var revision = ++viewRevision;
            detailCancellation?.Cancel();
            if (hasDetail) detail.Visibility = DetailCovers(VisiblePageRegion()) ? Visibility.Visible : Visibility.Collapsed;
            await Task.Delay(120);
            if (revision != viewRevision || renderer == null || editing || rendering || closed.Task.IsCompleted) return;
            if (detailRunning) return;
            detailRunning = true;
            var cancellation = new CancellationTokenSource();
            detailCancellation = cancellation;
            try
            {
                var visible = VisiblePageRegion();
                if (visible.Width <= 0 || visible.Height <= 0) return;
                var left = visible.Left; var top = visible.Top; var right = visible.Right; var bottom = visible.Bottom;
                var index = pageIndex; var simulation = proof.IsChecked == true;
                var contentRevision = renderRevision;
                var scale = worldScale*viewer.ZoomFactor*(display?.RawPixelsPerViewPixel ?? 1);
                var requestedScale = scale;
                if (DetailCovers(visible) && Math.Abs(detailScale-scale) <= scale*.001) return;
                // A single native region has no internal tile seams. Overscan
                // keeps its outside edges beyond the visible part of the page.
                var padding = 64/scale;
                var region = new Rect(Math.Max(0, left-padding), Math.Max(0, top-padding), 0, 0);
                region.Width = Math.Min(pageSize.Width, right+padding)-region.X;
                region.Height = Math.Min(pageSize.Height, bottom+padding)-region.Y;
                // Bound work by display size, even for very large paper/zoom.
                // Leave room for integral dimension rounding in the renderer.
                scale = Math.Min(scale, Math.Sqrt(4.5*1024*1024/(region.Width*region.Height)));
                scale = Math.Min(scale, 8190/Math.Max(region.Width, region.Height));
                var alignedLeft = Math.Max(0, Math.Floor(region.Left*scale)/scale);
                var alignedTop = Math.Max(0, Math.Floor(region.Top*scale)/scale);
                region = new Rect(alignedLeft, alignedTop,
                    Math.Min(pageSize.Width, Math.Ceiling(region.Right*scale)/scale)-alignedLeft,
                    Math.Min(pageSize.Height, Math.Ceiling(region.Bottom*scale)/scale)-alignedTop);
                var frame = await renderer.RenderRegionAsync(index, simulation, region, scale, cancellation.Token);
                if (revision != viewRevision || cancellation.IsCancellationRequested || closed.Task.IsCompleted
                    || contentRevision != renderRevision || index != pageIndex || simulation != (proof.IsChecked == true)) return;
                // Publish only a complete current frame. Until then the previous
                // complete detail image stays visible over the coarse page.
                detail.Width = region.Width*worldScale; detail.Height = region.Height*worldScale;
                Canvas.SetLeft(detail, region.X*worldScale); Canvas.SetTop(detail, region.Y*worldScale);
                detail.Source = frame;
                UpdatePhotoOverlay();
                detail.Visibility = Visibility.Visible;
                detailRegion = region; detailScale = requestedScale; detailPage = index; detailProof = simulation;
                detailContentRevision = contentRevision; hasDetail = true;
            }
            catch (OperationCanceledException) { }
            catch (Exception error) { App.WriteStartupException("Preview detail rendering failed", error); }
            finally
            {
                if (ReferenceEquals(detailCancellation, cancellation)) detailCancellation = null;
                cancellation.Dispose(); detailRunning = false;
                if (revision != viewRevision && !closed.Task.IsCompleted) ScheduleDetail();
            }
        }

        private async void QueueThumbnail(uint index)
        {
            if (renderer == null || editing || index >= thumbnailItems.Count || closed.Task.IsCompleted
                || thumbnailItems[(int)index].Bitmap != null || !thumbnailPending.Add(index)) return;
            thumbnailQueue.Enqueue(index);
            if (thumbnailRunning) return;
            thumbnailRunning = true;
            try
            {
                while (thumbnailQueue.Count > 0 && !editing && !closed.Task.IsCompleted)
                {
                    while ((rendering || detailRunning) && !editing && !closed.Task.IsCompleted) await Task.Delay(60);
                    if (editing || closed.Task.IsCompleted) return;
                    var nextIndex = thumbnailQueue.Dequeue(); thumbnailPending.Remove(nextIndex);
                    var revision = renderRevision;
                    var bitmap = await renderer.RenderAsync(nextIndex, proof.IsChecked == true, 160, true);
                    if (editing || closed.Task.IsCompleted || revision != renderRevision) continue;
                    while (thumbnailOrder.Count >= 24) thumbnailItems[(int)thumbnailOrder.Dequeue()].Bitmap = null;
                    thumbnailItems[(int)nextIndex].Bitmap = bitmap; thumbnailOrder.Enqueue(nextIndex);
                }
            }
            catch (Exception error) { App.WriteStartupException("Preview thumbnail failed", error); }
            finally { thumbnailRunning = false; }
        }

        private void UpdateInformation()
        {
            information.Children.Clear();
            foreach (var item in renderer.Information)
            {
                information.Children.Add(new TextBlock { Text = Text(item.Key), FontWeight = Windows.UI.Text.FontWeights.SemiBold });
                var detail = new TextBlock { Text = string.IsNullOrWhiteSpace(item.Value) ? "—" : item.Value,
                    TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true, Margin = new Thickness(0,0,0,7) };
                ToolTipService.SetToolTip(detail, item.Value); information.Children.Add(detail);
            }
        }

        private async Task EditSettingsAsync()
        {
            if (approved || approving || editing || rendering || renderer == null || closed.Task.IsCompleted) return;
            var restorePhotoMode = photoMode.IsChecked == true;
            var committed = false;
            try
            {
                await DisablePhotoModeAsync();
                if (!await FlushPhotoPlacementsAsync()) return;
                editing = true; viewRevision++;
                detailCancellation?.Cancel();
                print.IsEnabled = printClose.IsEnabled = editSettings.IsEnabled = proof.IsEnabled = false;
                previous.IsEnabled = next.IsEnabled = thumbnails.IsEnabled = false;
                while (detailRunning || thumbnailRunning) await Task.Delay(30);
                thumbnailQueue.Clear(); thumbnailPending.Clear();
                var registry = await PrinterInstanceStore.ReadAsync();
                var instance = registry.Instances.FirstOrDefault(item => item.PrinterUri == printerUri || item.QueueName == queueName);
                if (instance == null) throw new InvalidDataException("The print queue is no longer registered.");
                var configuration = await FileIO.ReadTextAsync(await folder.GetFileAsync("preview-configuration.json"));
                string selected = null;
                var dialog = new ContentDialog { FullSizeDesired = true, HorizontalContentAlignment = HorizontalAlignment.Stretch };
                dialog.Resources["ContentDialogMaxWidth"] = 1120d;
                // Buttons live in our content grid. Do not reserve the native
                // command row's extra bottom gap as well.
                dialog.Resources["ContentDialogCommandSpaceMargin"] = new Thickness(0);
                dialog.Resources["ContentDialogTitleMargin"] = new Thickness(0);
                dialog.Resources["ContentDialogPadding"] = new Thickness(24,18,24,18);
                var editor = new PropertiesPage(new PreviewJobSettings
                {
                    Printer = instance, Configuration = configuration,
                    Applied = value => selected = value, Canceled = () => dialog.Hide(),
                }) { MaxWidth = 1080, HorizontalAlignment = HorizontalAlignment.Stretch, VerticalAlignment = VerticalAlignment.Stretch };
                var apply = new Button { Content = LocalizationService.Get("Auto.AF70020B40EA.Content") };
                var dismiss = new Button { Content = Text("Cancel") };
                var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right,
                    Spacing = 10, Margin = new Thickness(0,8,0,0) };
                actions.Children.Add(dismiss); actions.Children.Add(apply);
                var dialogContent = new Grid();
                dialogContent.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
                dialogContent.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                Grid.SetRow(actions, 1); dialogContent.Children.Add(editor); dialogContent.Children.Add(actions);
                dialog.Content = dialogContent;
                apply.SetBinding(Button.IsEnabledProperty, new Binding
                {
                    Source = editor.ApplyActionButton, Path = new PropertyPath("IsEnabled"), Mode = BindingMode.OneWay,
                });
                dismiss.Click += (_, __) => dialog.Hide();
                apply.Click += async (_, __) =>
                {
                    selected = null;
                    await editor.ApplyJobSettingsAsync();
                    if (selected != null) dialog.Hide();
                };
                void SizeSettingsDialog()
                {
                    dialog.MaxHeight = Math.Max(160, ActualHeight-48);
                    dialog.Resources["ContentDialogMaxHeight"] = dialog.MaxHeight;
                    dialogContent.Height = Math.Max(120, dialog.MaxHeight-38);
                }
                SizeSettingsDialog();
                SizeChangedEventHandler resizeSettings = (_, __) => SizeSettingsDialog();
                SizeChanged += resizeSettings;
                try { await dialog.ShowAsync(); }
                finally { SizeChanged -= resizeSettings; }
                if (selected == null || closed.Task.IsCompleted) return;
                status.Text = Text("Editing");
                var result = await SendPreviewEditAsync(selected);
                if (closed.Task.IsCompleted || result == null) return;
                if (!result.GetNamedBoolean("success", false))
                {
                    committed = result.GetNamedBoolean("fatal", false);
                    throw new InvalidDataException(result.GetNamedString("error", Text("Failed")));
                }
                committed = true;
                await ReloadPreviewRendererAsync();
                pendingPhotoPlacements.Clear();
                fitting = true;
                editing = false;
                await RenderAsync(true);
                if (!previewValid) throw new InvalidDataException(Text("Failed"));
            }
            catch (Exception error)
            {
                App.WriteStartupException("Preview settings change failed", error);
                status.Text = Text("Failed") + Environment.NewLine + error.Message;
                if (committed)
                {
                    terminal = true;
                    await PreviewWorkflowStore.WriteAtomicAsync(folder, "preview-failed.txt", error.ToString());
                }
            }
            finally
            {
                editing = false; proof.IsEnabled = thumbnails.IsEnabled = true;
                if (!approved && !terminal && !closed.Task.IsCompleted)
                {
                    print.IsEnabled = printClose.IsEnabled = previewValid; editSettings.IsEnabled = true;
                    previous.IsEnabled = pageIndex > 0; next.IsEnabled = pageIndex+1 < renderer.PageCount;
                    if (restorePhotoMode && previewValid)
                    {
                        changingPhotoMode = true;
                        photoMode.IsChecked = true;
                        changingPhotoMode = false;
                        await SetPhotoModeAsync(true);
                    }
                    RefreshPhotoActions();
                    ScheduleDetail();
                }
            }
        }

        private async void PrintClicked(object sender, RoutedEventArgs args) => await ApproveAsync(false);

        private async Task ApproveAsync(bool closeAfterApproval)
        {
            if (terminal || approved || approving || applyingPhoto || photoDragging || !previewValid || folder == null || image.Source == null || rendering || editing) return;
            approving = true;
            print.IsEnabled = printClose.IsEnabled = editSettings.IsEnabled = false;
            try
            {
                await DisablePhotoModeAsync();
                if (!await renderer.SourceFilesUnchangedAsync())
                    throw new InvalidDataException(Text("SourceChanged"));
                if (!await FlushPhotoPlacementsAsync()) return;
                if (!await renderer.SourceFilesUnchangedAsync())
                    throw new InvalidDataException(Text("SourceChanged"));
                await PreviewWorkflowStore.WriteAtomicAsync(folder, "preview-approved", "");
                approved = true;
                await DisablePhotoModeAsync();
                RefreshPhotoActions();
                editSettings.IsEnabled = false;
                status.Text = Text("Preparing");
                if (!closeAfterApproval) _ = MonitorPrinterAsync();
                if (closeAfterApproval) { detached = true; closed.TrySetResult(true); }
            }
            catch (Exception error) { status.Text = error.Message; print.IsEnabled = printClose.IsEnabled = editSettings.IsEnabled = previewValid && !terminal && !closed.Task.IsCompleted; }
            finally { approving = false; }
        }

        private async void CancelClicked(object sender, RoutedEventArgs args)
        {
            cancel.IsEnabled = false;
            try { if (!terminal && folder != null) await CancelJobAsync(); }
            catch (Exception error) { App.WriteStartupException("Preview cancellation failed", error); }
            closed.TrySetResult(true);
        }

        private Task CancelJobAsync() => PreviewWorkflowStore.WriteAtomicAsync(folder, "cancel.requested", "Canceled in print preview.");

        private void WindowClosed(object sender, CoreWindowEventArgs args)
        {
            if (!terminal && !detached && folder != null)
            {
                try { File.WriteAllText(Path.Combine(folder.Path, "cancel.requested"), "Preview window closed."); }
                catch (Exception error) { App.WriteStartupException("Preview close cancellation failed", error); }
            }
            closed.TrySetResult(true);
        }

        private async Task MonitorProgressAsync()
        {
            if (monitorRunning) return;
            monitorRunning = true;
            while (!closed.Task.IsCompleted)
            {
                try
                {
                    var file = await folder.GetFileAsync("progress.json");
                    JobProgressSnapshot snapshot;
                    using (var stream = await file.OpenStreamForReadAsync())
                        snapshot = (JobProgressSnapshot)new DataContractJsonSerializer(typeof(JobProgressSnapshot)).ReadObject(stream);
                    if (snapshot?.JobId != folder.Name) throw new InvalidDataException("Preview progress belongs to another job.");
                    if (renderer == null || approved || snapshot.Terminal)
                    {
                        progress.IsIndeterminate = false;
                        progress.Value = Math.Max(0, Math.Min(100, snapshot.Percent));
                        if (snapshot.Terminal)
                        {
                            terminal = true; print.IsEnabled = printClose.IsEnabled = false; RefreshPhotoActions(); cancel.Content = Text("Close");
                            ToolTipService.SetToolTip(cancel, Text("Close"));
                            var sent = snapshot.Status == "SENT";
                            status.Text = Text(sent ? "Sent" : snapshot.Status == "CANCELLED" ? "Cancelled" : "Failed");
                            if (!sent) status.Text += Environment.NewLine + snapshot.Message;
                            return;
                        }
                        status.Text = Text((snapshot.Stage ?? "").Contains("SEND") || snapshot.Stage == "RIP_STREAMING" ? "Sending" : "Preparing");
                    }
                }
                catch (FileNotFoundException) { }
                catch (IOException) { }
                catch (Exception error)
                {
                    App.WriteStartupException("Preview progress read failed", error);
                    status.Text = Text("Failed") + Environment.NewLine + error.Message;
                }
                await Task.Delay(250);
            }
        }

        private async Task MonitorPrinterAsync()
        {
            var host = renderer?.NetworkHost;
            if (string.IsNullOrWhiteSpace(host)) { physicalStatus.Text = Text("PhysicalUnavailable"); return; }
            while (!closed.Task.IsCompleted)
            {
                try
                {
                    var reported = await PrinterSnmpService.ReadReportedStateAsync(host);
                    if (closed.Task.IsCompleted) return;
                    physicalStatus.Text = Text(reported.HasValue ? "Device" + reported.Value : "PhysicalUnavailable")
                        + Environment.NewLine + Text("PhysicalUnconfirmed");
                }
                catch (Exception error)
                {
                    App.WriteStartupException("Preview printer status unavailable", error);
                    physicalStatus.Text = Text("PhysicalUnavailable");
                }
                await Task.Delay(2000);
            }
        }

        private async void DisplayProfileChanged(DisplayInformation sender, object args) => await RefreshDisplayAsync();
        private async void AdvancedColorChanged(DisplayInformation sender, object args) => await RefreshDisplayAsync();
        private async Task RefreshDisplayAsync()
        {
            if (renderer == null || closed.Task.IsCompleted) return;
            try { await renderer.UpdateDisplayProfileAsync(); await RenderAsync(true); }
            catch (Exception error) { App.WriteStartupException("Preview display refresh failed", error); }
        }
    }
}
