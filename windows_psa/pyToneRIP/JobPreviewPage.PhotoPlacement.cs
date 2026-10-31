using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading.Tasks;
using pyToneRIP.Models;
using pyToneRIP.Services;
using pyToneRIP.Shared;
using Windows.Data.Json;
using Windows.Foundation;
using Windows.Storage;
using Windows.UI;
using Windows.UI.Core;
using Windows.UI.Xaml;
using Windows.UI.Xaml.Controls;
using Windows.UI.Xaml.Input;
using Windows.UI.Xaml.Media;

namespace pyToneRIP
{
    public sealed partial class JobPreviewPage
    {
        private readonly CheckBox photoMode = new CheckBox { VerticalContentAlignment = VerticalAlignment.Center, IsEnabled = false };
        private readonly Button photoLeft = new Button { Content = "↶ 90°", IsEnabled = false };
        private readonly Button photoRight = new Button { Content = "↷ 90°", IsEnabled = false };
        private readonly Button photoFit = new Button { IsEnabled = false };
        private readonly Button photoReset = new Button { IsEnabled = false };
        private readonly Button photoCenterHorizontal = new Button { IsEnabled = false };
        private readonly Button photoCenterVertical = new Button { IsEnabled = false };
        private readonly TextBlock photoSize = new TextBlock { VerticalAlignment = VerticalAlignment.Center };
        private readonly Canvas photoLayer = new Canvas { Visibility = Visibility.Collapsed };
        private readonly Image photoPaper = new Image { Stretch = Stretch.Fill, Visibility = Visibility.Collapsed, IsHitTestVisible = false };
        private readonly Canvas photoVisual = new Canvas { IsHitTestVisible = false };
        private readonly Image photoBitmap = new Image { Stretch = Stretch.Fill, Visibility = Visibility.Collapsed, IsHitTestVisible = false };
        private readonly Border photoFrame = new Border { BorderBrush = new SolidColorBrush(Colors.DodgerBlue),
            Background = new SolidColorBrush(Colors.Transparent), ManipulationMode = ManipulationModes.None };
        private readonly List<Border> photoCorners = new List<Border>();
        private PreviewPhotoPlacement photoPlacement, photoCommitted, photoStart;
        private Size photoSourceSize, photoPaperSize;
        private Point photoPointerStart;
        private Point? photoHoverPosition;
        private int photoCorner;
        private uint photoPointer;
        private bool changingPhotoMode, photoDragging, applyingPhoto, photoLoading;
        private readonly Dictionary<int, PreviewPhotoPlacement> pendingPhotoPlacements = new Dictionary<int, PreviewPhotoPlacement>();

        private void AddPhotoTools(StackPanel toolbar)
        {
            photoMode.Content = Text("PhotoPlacement"); photoFit.Content = Text("PhotoFit"); photoReset.Content = Text("PhotoReset");
            photoCenterHorizontal.Content = CreatePhotoCenterIcon(true);
            photoCenterVertical.Content = CreatePhotoCenterIcon(false);
            ToolTipService.SetToolTip(photoCenterHorizontal, Text("PhotoCenterHorizontal"));
            ToolTipService.SetToolTip(photoCenterVertical, Text("PhotoCenterVertical"));
            Windows.UI.Xaml.Automation.AutomationProperties.SetName(photoCenterHorizontal, Text("PhotoCenterHorizontal"));
            Windows.UI.Xaml.Automation.AutomationProperties.SetName(photoCenterVertical, Text("PhotoCenterVertical"));
            ToolTipService.SetToolTip(photoMode, Text("PhotoHelp"));
            toolbar.Children.Add(photoMode); toolbar.Children.Add(photoLeft); toolbar.Children.Add(photoRight);
            toolbar.Children.Add(photoFit); toolbar.Children.Add(photoReset);
            toolbar.Children.Add(photoCenterHorizontal); toolbar.Children.Add(photoCenterVertical); toolbar.Children.Add(photoSize);
            photoMode.Checked += async (_, __) => { if (!changingPhotoMode) await SetPhotoModeAsync(true); };
            photoMode.Unchecked += async (_, __) => { if (!changingPhotoMode) await SetPhotoModeAsync(false); };
            photoLeft.Click += async (_, __) => await ChangePhotoAsync(-1);
            photoRight.Click += async (_, __) => await ChangePhotoAsync(1);
            photoFit.Click += async (_, __) => await FitPhotoToPageAsync();
            photoReset.Click += async (_, __) => await ChangePhotoAsync(2);
            photoCenterHorizontal.Click += async (_, __) => await CenterPhotoAsync(true);
            photoCenterVertical.Click += async (_, __) => await CenterPhotoAsync(false);
        }

        private static PathIcon CreatePhotoCenterIcon(bool horizontal)
        {
            var geometry = new GeometryGroup();
            geometry.Children.Add(new RectangleGeometry { Rect = new Rect(8.5, 0, 1, 18) });
            geometry.Children.Add(new RectangleGeometry { Rect = new Rect(3, 3, 12, 3) });
            geometry.Children.Add(new RectangleGeometry { Rect = new Rect(0, 11, 18, 3) });
            if (!horizontal)
                geometry.Transform = new RotateTransform { Angle = 90, CenterX = 9, CenterY = 9 };
            return new PathIcon { Data = geometry, Width = 18, Height = 18 };
        }

        private void InstallPhotoOverlay()
        {
            photoVisual.Children.Add(photoBitmap);
            photoLayer.Children.Add(photoPaper); photoLayer.Children.Add(photoVisual); photoLayer.Children.Add(photoFrame);
            WirePhotoPointer(photoFrame, -1);
            for (var corner = 0; corner < 4; corner++)
            {
                var handle = new Border { Background = new SolidColorBrush(Colors.DodgerBlue),
                    BorderBrush = new SolidColorBrush(Colors.White), ManipulationMode = ManipulationModes.None };
                photoCorners.Add(handle); photoLayer.Children.Add(handle); WirePhotoPointer(handle, corner);
            }
            surface.Children.Add(photoLayer);
            viewer.AddHandler(UIElement.PointerMovedEvent, new PointerEventHandler((_, args) =>
            {
                if (args.Pointer.PointerDeviceType != Windows.Devices.Input.PointerDeviceType.Mouse) return;
                photoHoverPosition = args.GetCurrentPoint(surface).Position;
                RefreshPhotoCursor();
            }), true);
            viewer.PointerExited += (_, __) =>
            {
                if (photoDragging) return;
                photoHoverPosition = null; RefreshPhotoCursor();
            };
        }

        private void RefreshPhotoActions()
        {
            var ready = renderer != null && previewValid && !approved && !approving && !terminal && !editing && !rendering && !applyingPhoto && !photoDragging;
            photoMode.IsEnabled = ready;
            var selected = ready && photoMode.IsChecked == true && photoPlacement != null;
            photoLeft.IsEnabled = photoRight.IsEnabled = photoFit.IsEnabled = photoReset.IsEnabled = selected;
            photoCenterHorizontal.IsEnabled = selected && photoPlacement.Width < photoPaperSize.Width-.01;
            photoCenterVertical.IsEnabled = selected && photoPlacement.Height < photoPaperSize.Height-.01;
            RefreshPhotoCursor();
        }

        private async Task SetPhotoModeAsync(bool enabled)
        {
            if (!enabled)
            {
                photoDragging = false;
                photoFrame.ReleasePointerCaptures(); foreach (var corner in photoCorners) corner.ReleasePointerCaptures();
                if (photoCommitted != null) photoPlacement = photoCommitted.Copy();
                ShowPhotoWorking(false); photoLayer.Visibility = Visibility.Collapsed; photoSize.Text = "";
                await ClosePhotoSourceAsync(); RefreshPhotoActions(); return;
            }
            if (renderer == null || !previewValid || approved || terminal || editing || rendering || applyingPhoto || closed.Task.IsCompleted)
            { await DisablePhotoModeAsync(); return; }
            editing = photoLoading = true; detailCancellation?.Cancel();
            print.IsEnabled = printClose.IsEnabled = editSettings.IsEnabled = false;
            previous.IsEnabled = next.IsEnabled = thumbnails.IsEnabled = proof.IsEnabled = false;
            RefreshPhotoActions();
            try
            {
                var bitmap = await renderer.RenderPhotoSourceAsync(pageIndex, proof.IsChecked == true);
                if (closed.Task.IsCompleted) return;
                photoSourceSize = renderer.SourcePageSize(pageIndex); photoPaperSize = renderer.PaperSizeMm;
                photoPlacement = renderer.PhotoPlacement(pageIndex);
                if (photoPlacement == null)
                    photoPlacement = renderer.EffectivePhotoPlacement(renderer.InitialPhotoPlacement(pageIndex, false, true), pageIndex);
                photoPlacement.Validate(photoPaperSize.Width, photoPaperSize.Height);
                photoCommitted = photoPlacement.Copy();
                photoBitmap.Source = bitmap; photoLayer.Visibility = Visibility.Visible;
                UpdatePhotoOverlay(); status.Text = Text("PhotoHelp");
            }
            catch (Exception error)
            {
                App.WriteStartupException("Photo placement editor failed", error);
                changingPhotoMode = true; photoMode.IsChecked = false; changingPhotoMode = false;
                photoLayer.Visibility = Visibility.Collapsed; status.Text = Text("Failed") + Environment.NewLine + error.Message;
            }
            finally
            {
                editing = photoLoading = false; thumbnails.IsEnabled = proof.IsEnabled = true;
                print.IsEnabled = printClose.IsEnabled = previewValid && !approved && !terminal;
                editSettings.IsEnabled = !approved && !terminal;
                previous.IsEnabled = pageIndex > 0; next.IsEnabled = pageIndex+1 < renderer.PageCount;
                RefreshPhotoActions();
            }
        }

        private async Task DisablePhotoModeAsync()
        {
            changingPhotoMode = true; photoMode.IsChecked = false; changingPhotoMode = false;
            if (photoBitmap.Source != null || photoDragging) await SetPhotoModeAsync(false);
        }

        private Task ClosePhotoSourceAsync()
        {
            photoBitmap.Source = photoPaper.Source = null;
            return Task.CompletedTask;
        }

        private void ShowPhotoWorking(bool working)
        {
            image.Visibility = detailLayer.Visibility = working ? Visibility.Collapsed : Visibility.Visible;
            photoPaper.Visibility = photoBitmap.Visibility = working ? Visibility.Visible : Visibility.Collapsed;
        }

        private void UpdatePhotoOverlay()
        {
            if (photoMode.IsChecked != true) { photoSize.Text = ""; RefreshPhotoCursor(); return; }
            if (photoPlacement == null || photoPaperSize.Width <= 0 || photoPaperSize.Height <= 0) return;
            photoLayer.Width = photoVisual.Width = photoPaper.Width = surface.Width;
            photoLayer.Height = photoVisual.Height = photoPaper.Height = surface.Height;
            photoBitmap.Source = renderer.PhotoProofImage;
            photoPaper.Source = renderer.PhotoProofPaperImage;
            var printable = renderer.PhotoPrintableArea;
            var paperToSurface = surface.Width/(photoPaperSize.Width*96/25.4);
            photoVisual.Clip = new RectangleGeometry { Rect = new Rect(printable.X*paperToSurface, printable.Y*paperToSurface,
                printable.Width*paperToSurface, printable.Height*paperToSurface) };
            var sx = surface.Width/photoPaperSize.Width; var sy = surface.Height/photoPaperSize.Height;
            var matrix = photoPlacement.Matrix(photoSourceSize.Width, photoSourceSize.Height, 1);
            photoBitmap.Width = photoSourceSize.Width; photoBitmap.Height = photoSourceSize.Height;
            photoBitmap.RenderTransform = new MatrixTransform { Matrix = new Matrix(matrix[0]*sx, matrix[1]*sy,
                matrix[2]*sx, matrix[3]*sy, matrix[4]*sx, matrix[5]*sy) };
            var x = photoPlacement.X*sx; var y = photoPlacement.Y*sy;
            var width = photoPlacement.Width*sx; var height = photoPlacement.Height*sy;
            photoFrame.Width = width; photoFrame.Height = height; photoFrame.BorderThickness = new Thickness(1/viewer.ZoomFactor);
            Canvas.SetLeft(photoFrame, x); Canvas.SetTop(photoFrame, y);
            var handleSize = 12/viewer.ZoomFactor;
            for (var i = 0; i < photoCorners.Count; i++)
            {
                var handle = photoCorners[i]; handle.Width = handle.Height = handleSize;
                handle.BorderThickness = new Thickness(1/viewer.ZoomFactor);
                Canvas.SetLeft(handle, Math.Max(0, Math.Min(surface.Width-handleSize, x+((i&1) != 0 ? width : 0)-handleSize/2)));
                Canvas.SetTop(handle, Math.Max(0, Math.Min(surface.Height-handleSize, y+((i&2) != 0 ? height : 0)-handleSize/2)));
            }
            photoSize.Text = string.Format(CultureInfo.CurrentCulture, "{0:0.#} × {1:0.#} mm · X {2:0.#} · Y {3:0.#} · {4}°",
                photoPlacement.Width, photoPlacement.Height, photoPlacement.X, photoPlacement.Y, photoPlacement.Rotation);
            RefreshPhotoCursor();
        }

        private void RefreshPhotoCursor()
        {
            var cursor = CoreCursorType.Arrow;
            if (photoMode.IsChecked == true && photoPlacement != null && !approved && !terminal && !editing)
            {
                var corner = -2;
                if (photoDragging) corner = photoCorner;
                else if (photoHoverPosition.HasValue)
                {
                    var point = photoHoverPosition.Value;
                    for (var i = 0; i < photoCorners.Count; i++)
                    {
                        var handle = photoCorners[i];
                        if (new Rect(Canvas.GetLeft(handle), Canvas.GetTop(handle), handle.Width, handle.Height).Contains(point)) { corner = i; break; }
                    }
                    if (corner == -2 && new Rect(Canvas.GetLeft(photoFrame), Canvas.GetTop(photoFrame), photoFrame.Width, photoFrame.Height).Contains(point)) corner = -1;
                }
                if (corner == -1) cursor = CoreCursorType.SizeAll;
                else if (corner >= 0) cursor = corner == 0 || corner == 3 ? CoreCursorType.SizeNorthwestSoutheast : CoreCursorType.SizeNortheastSouthwest;
            }
            Window.Current.CoreWindow.PointerCursor = new CoreCursor(cursor, 0);
        }

        private async Task CenterPhotoAsync(bool horizontal)
        {
            if (photoPlacement == null || photoMode.IsChecked != true || applyingPhoto || editing || rendering
                || photoDragging || approved || approving || terminal || closed.Task.IsCompleted) return;
            photoPlacement.Center(horizontal, photoPaperSize.Width, photoPaperSize.Height);
            ShowPhotoWorking(true); UpdatePhotoOverlay(); await CommitPhotoAsync();
        }

        private void WirePhotoPointer(Border element, int corner)
        {
            element.PointerPressed += (sender, args) =>
            {
                if (photoMode.IsChecked != true || photoPlacement == null || editing || rendering || applyingPhoto || approved || terminal || photoDragging) return;
                var point = args.GetCurrentPoint(surface);
                if (args.Pointer.PointerDeviceType == Windows.Devices.Input.PointerDeviceType.Mouse && !point.Properties.IsLeftButtonPressed) return;
                if (!element.CapturePointer(args.Pointer)) return;
                photoDragging = true; photoPointer = args.Pointer.PointerId; photoCorner = corner;
                photoStart = photoPlacement.Copy(); photoPointerStart = point.Position;
                detailCancellation?.Cancel(); ShowPhotoWorking(true); print.IsEnabled = printClose.IsEnabled = false;
                RefreshPhotoActions(); args.Handled = true;
            };
            element.PointerMoved += (sender, args) =>
            {
                if (!photoDragging || args.Pointer.PointerId != photoPointer) return;
                var point = args.GetCurrentPoint(surface).Position;
                var dx = (point.X-photoPointerStart.X)*photoPaperSize.Width/surface.Width;
                var dy = (point.Y-photoPointerStart.Y)*photoPaperSize.Height/surface.Height;
                photoPlacement = photoStart.Copy();
                if (photoCorner < 0) photoPlacement.Move(dx, dy, photoPaperSize.Width, photoPaperSize.Height);
                else photoPlacement.Resize(photoCorner, dx, dy, photoPaperSize.Width, photoPaperSize.Height);
                UpdatePhotoOverlay(); args.Handled = true;
            };
            element.PointerReleased += async (sender, args) =>
            {
                if (!photoDragging || args.Pointer.PointerId != photoPointer) return;
                photoDragging = false; element.ReleasePointerCapture(args.Pointer); args.Handled = true;
                photoHoverPosition = args.GetCurrentPoint(surface).Position; RefreshPhotoCursor();
                await CommitPhotoAsync();
            };
            element.PointerCaptureLost += (sender, args) => CancelPhotoGesture();
            element.PointerCanceled += (sender, args) => CancelPhotoGesture();
        }

        private void CancelPhotoGesture()
        {
            if (!photoDragging) return;
            photoDragging = false; photoPlacement = photoCommitted.Copy();
            ShowPhotoWorking(false); UpdatePhotoOverlay(); RefreshPhotoActions();
            print.IsEnabled = printClose.IsEnabled = previewValid && !approved && !terminal;
            ScheduleDetail();
        }

        private async Task FitPhotoToPageAsync()
        {
            if (renderer == null || !previewValid || approved || approving || terminal || editing || rendering
                || applyingPhoto || photoDragging || photoLoading || closed.Task.IsCompleted) return;
            if (photoMode.IsChecked != true)
            {
                // Initialize the same editor used by mouse placement without
                // starting a second asynchronous Checked handler.
                changingPhotoMode = true; photoMode.IsChecked = true; changingPhotoMode = false;
                await SetPhotoModeAsync(true);
            }
            if (photoMode.IsChecked != true || photoBitmap.Source == null || photoPlacement == null
                || approved || approving || terminal || editing || rendering || closed.Task.IsCompleted) return;
            await ChangePhotoAsync(0);
        }

        private async Task ChangePhotoAsync(int operation)
        {
            if (photoPlacement == null || applyingPhoto || editing || rendering || approved || terminal) return;
            if (operation == 2) photoPlacement = renderer.InitialPhotoPlacement(pageIndex, true, true);
            else if (operation == 0)
            {
                var aspect = photoSourceSize.Width/photoSourceSize.Height;
                if (photoPlacement.Rotation == 90 || photoPlacement.Rotation == 270) aspect = 1/aspect;
                photoPlacement.Width = Math.Min(photoPaperSize.Width, photoPaperSize.Height*aspect);
                photoPlacement.Height = photoPlacement.Width/aspect;
                photoPlacement.X = (photoPaperSize.Width-photoPlacement.Width)/2;
                photoPlacement.Y = (photoPaperSize.Height-photoPlacement.Height)/2;
            }
            else photoPlacement.Rotate(operation, photoPaperSize.Width, photoPaperSize.Height);
            ShowPhotoWorking(true); UpdatePhotoOverlay(); await CommitPhotoAsync();
        }

        private async Task CommitPhotoAsync()
        {
            if (closed.Task.IsCompleted) return;
            try
            {
                detailCancellation?.Cancel();
                photoPlacement.Validate(photoPaperSize.Width, photoPaperSize.Height);
                // A mouse gesture changes the local native composition only.
                // Publish all pending pages together before settings/approval.
                var placement = photoPlacement.Copy();
                pendingPhotoPlacements[placement.PageIndex] = placement;
                renderer.SetPhotoPlacement(placement);
                photoCommitted = placement.Copy();
                thumbnailItems[(int)pageIndex].Bitmap = null;
                // Keep the dragged image at its final position until the new
                // composed frame replaces the old image underneath it.
                await RenderAsync();
                if (previewValid && !closed.Task.IsCompleted) ShowPhotoWorking(false);
                UpdatePhotoOverlay();
            }
            catch (Exception error)
            {
                App.WriteStartupException("Photo placement render failed", error);
                status.Text = Text("Failed") + Environment.NewLine + error.Message;
            }
        }

        private async Task<bool> FlushPhotoPlacementsAsync()
        {
            if (pendingPhotoPlacements.Count == 0) return true;
            applyingPhoto = editing = true; detailCancellation?.Cancel(); RefreshPhotoActions();
            print.IsEnabled = printClose.IsEnabled = editSettings.IsEnabled = false;
            previous.IsEnabled = next.IsEnabled = thumbnails.IsEnabled = proof.IsEnabled = false;
            var committed = false;
            try
            {
                while (rendering || detailRunning || thumbnailRunning) await Task.Delay(30);
                var configuration = await FileIO.ReadTextAsync(await folder.GetFileAsync("preview-configuration.json"));
                var result = await SendPreviewEditAsync(configuration);
                if (closed.Task.IsCompleted || result == null) return false;
                committed = result.GetNamedBoolean("success", false) || result.GetNamedBoolean("fatal", false);
                if (!result.GetNamedBoolean("success", false))
                    throw new InvalidDataException(result.GetNamedString("error", Text("Failed")));
                await ReloadPreviewRendererAsync();
                pendingPhotoPlacements.Clear();
                editing = false;
                await RenderAsync();
                if (!previewValid) throw new InvalidDataException(Text("Failed"));
                return true;
            }
            catch (Exception error)
            {
                App.WriteStartupException("Photo placement apply failed", error);
                status.Text = Text("Failed") + Environment.NewLine + error.Message;
                if (committed)
                {
                    terminal = true;
                    await PreviewWorkflowStore.WriteAtomicAsync(folder, "preview-failed.txt", error.ToString());
                }
                // Keep the user's visible placement on a recoverable failure.
                return false;
            }
            finally
            {
                applyingPhoto = editing = false; thumbnails.IsEnabled = proof.IsEnabled = true;
                if (!closed.Task.IsCompleted && !approved && !approving && !terminal)
                {
                    print.IsEnabled = printClose.IsEnabled = previewValid; editSettings.IsEnabled = true;
                    previous.IsEnabled = pageIndex > 0; next.IsEnabled = pageIndex+1 < renderer.PageCount;
                    ScheduleDetail();
                }
                RefreshPhotoActions();
            }
        }

        private async Task<JsonObject> SendPreviewEditAsync(string configuration)
        {
            status.Text = Text("Editing");
            var id = Guid.NewGuid().ToString("D");
            var started = DateTime.UtcNow;
            var request = new JsonObject { ["id"] = JsonValue.CreateStringValue(id), ["configuration"] = JsonObject.Parse(configuration) };
            if (pendingPhotoPlacements.Count != 0)
            {
                using (var bytes = new MemoryStream())
                {
                    new DataContractJsonSerializer(typeof(List<PreviewPhotoPlacement>)).WriteObject(bytes, pendingPhotoPlacements.Values.ToList());
                    request["placements"] = JsonArray.Parse(Encoding.UTF8.GetString(bytes.ToArray()));
                }
            }
            await PreviewWorkflowStore.WriteAtomicAsync(folder, "preview-edit-request.json", request.Stringify());
            while (!closed.Task.IsCompleted && !terminal)
            {
                try
                {
                    var result = JsonObject.Parse(await FileIO.ReadTextAsync(await folder.GetFileAsync("preview-edit-result.json")));
                    if (result.GetNamedString("id", "") == id) return result;
                }
                catch (FileNotFoundException) { }
                if (DateTime.UtcNow-started > TimeSpan.FromMinutes(2))
                {
                    // An in-flight edit may still publish: never approve the old
                    // document after losing its acknowledgement.
                    terminal = true;
                    await CancelJobAsync();
                    throw new TimeoutException("The print workflow did not acknowledge the preview edit.");
                }
                await Task.Delay(100);
            }
            if (terminal && !closed.Task.IsCompleted) throw new InvalidOperationException(Text("Failed"));
            return null;
        }

        private async Task ReloadPreviewRendererAsync()
        {
            var replacement = await PreviewRenderer.OpenAsync(folder);
            await renderer.CloseAsync(); renderer = replacement;
            pageIndex = Math.Min(pageIndex, renderer.PageCount-1);
            thumbnailQueue.Clear(); thumbnailPending.Clear(); thumbnailOrder.Clear();
            thumbnailItems.Clear();
            for (uint i = 0; i < renderer.PageCount; i++) thumbnailItems.Add(CreateThumbnail(i));
            actualSize.IsEnabled = renderer.RasterDpiX > 0 && renderer.RasterDpiX == renderer.RasterDpiY;
            UpdateInformation();
        }
    }
}
