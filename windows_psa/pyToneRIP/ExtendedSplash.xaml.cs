using System;
using System.Threading.Tasks;
using pyToneRIP.Services;
using Windows.Foundation;
using Windows.UI.Core;
using Windows.UI;
using Windows.UI.Xaml;
using Windows.UI.Xaml.Controls;
using Windows.UI.Xaml.Media;
using Windows.UI.Xaml.Media.Imaging;

namespace pyToneRIP
{
    public sealed partial class ExtendedSplash : Page
    {
        private readonly TaskCompletionSource<bool> firstRender = new TaskCompletionSource<bool>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource<bool> continueRequested = new TaskCompletionSource<bool>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        private Canvas splashCanvas;
        private Image splashImage;
        private StackPanel startupDetails;
        private TextBlock titleText;
        private TextBlock statusText;
        private ProgressRing startupProgress;
        private Button continueButton;

        private readonly BitmapImage splashArtwork;

        public ExtendedSplash(BitmapImage splashArtwork)
        {
            this.splashArtwork = splashArtwork ?? throw new ArgumentNullException(nameof(splashArtwork));
            App.WriteStartupStage("ExtendedSplash constructor entered");
            // The Manager's launch-only progress shell stays visible while this
            // page is constructed. Its first frame already contains the ring.
            App.WriteStartupStage("ExtendedSplash building bootstrap visual");
            BuildBootstrapVisual();
            BuildStartupDetails();
            App.WriteStartupStage("ExtendedSplash bootstrap visual complete");
            Loaded += ExtendedSplashLoaded;
            Unloaded += ExtendedSplashUnloaded;
            Window.Current.SizeChanged += WindowSizeChanged;
            // Positioning is deferred until Loaded so the window has final bounds.
            App.WriteStartupStage("ExtendedSplash constructor complete");
        }

        internal Task FirstRender => firstRender.Task;

        internal void ReportStatus(string message)
        {
            if (statusText != null)
                statusText.Text = message;
        }

        internal Task CompleteAsync(bool hasInstalledPrinters)
        {
            startupProgress.IsActive = false;
            startupProgress.Visibility = Visibility.Collapsed;
            if (hasInstalledPrinters)
            {
                statusText.Text = LocalizationService.Get("Code.PyToneRIPIsReady");
                return Task.CompletedTask;
            }

            statusText.Text = LocalizationService.Get("Code.PyToneRIPIsReadyContinueToAddYourFirstPrinter");
            continueButton.IsEnabled = true;
            continueButton.Visibility = Visibility.Visible;
            PositionContent();
            return continueRequested.Task;
        }

        private async void ExtendedSplashLoaded(object sender, RoutedEventArgs args)
        {
            try
            {
                PositionContent();
                App.WriteStartupStage("ExtendedSplash bootstrap loaded");

                // Observe a XAML rendering pass with the ring in the tree, then
                // allow the compositor to present it before MainPage startup.
                var frameObserved = await WaitForRenderingAsync();
                await Task.Delay(100);
                App.WriteStartupStage(frameObserved
                    ? "ExtendedSplash ProgressRing render frame observed"
                    : "ExtendedSplash ProgressRing render frame timed out");
                LocalizeStartupDetails();
                App.WriteStartupStage("ExtendedSplash released startup work");
                firstRender.TrySetResult(true);
            }
            catch (Exception error)
            {
                firstRender.TrySetException(error);
            }
        }

        private static async Task<bool> WaitForRenderingAsync()
        {
            var rendered = new TaskCompletionSource<bool>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            EventHandler<object> onRendering = null;
            onRendering = (sender, args) =>
            {
                Windows.UI.Xaml.Media.CompositionTarget.Rendering -= onRendering;
                rendered.TrySetResult(true);
            };
            Windows.UI.Xaml.Media.CompositionTarget.Rendering += onRendering;
            try
            {
                return await Task.WhenAny(rendered.Task, Task.Delay(1000)) == rendered.Task;
            }
            finally
            {
                Windows.UI.Xaml.Media.CompositionTarget.Rendering -= onRendering;
            }
        }

        private void BuildBootstrapVisual()
        {
            var background = new SolidColorBrush(Color.FromArgb(255, 243, 245, 248));
            Background = background;
            splashCanvas = new Canvas { Background = background };
            splashImage = new Image
            {
                Source = splashArtwork,
                Stretch = Stretch.Uniform,
            };
            splashCanvas.Children.Add(splashImage);
            Content = splashCanvas;
        }

        private void BuildStartupDetails()
        {
            if (startupDetails != null)
                return;
            titleText = new TextBlock
            {
                Text = "pyToneRIP",
                FontSize = 22,
                FontWeight = Windows.UI.Text.FontWeights.SemiBold,
                HorizontalAlignment = HorizontalAlignment.Center,
            };
            statusText = new TextBlock
            {
                Text = "",
                TextWrapping = TextWrapping.Wrap,
                TextAlignment = TextAlignment.Center,
                HorizontalAlignment = HorizontalAlignment.Stretch,
            };
            startupProgress = new ProgressRing
            {
                IsActive = true,
                Width = 42,
                Height = 42,
                HorizontalAlignment = HorizontalAlignment.Center,
                Margin = new Thickness(0, 8, 0, 0),
            };
            continueButton = new Button
            {
                IsEnabled = false,
                Visibility = Visibility.Collapsed,
                HorizontalAlignment = HorizontalAlignment.Center,
                Margin = new Thickness(0, 8, 0, 0),
            };
            continueButton.Click += ContinueClicked;
            startupDetails = new StackPanel { Width = 430, Spacing = 12 };
            startupDetails.Children.Add(titleText);
            startupDetails.Children.Add(statusText);
            startupDetails.Children.Add(startupProgress);
            startupDetails.Children.Add(continueButton);
            splashCanvas.Children.Add(startupDetails);
        }

        private void LocalizeStartupDetails()
        {
            titleText.Text = LocalizationService.Get("Code.StartingPyToneRIP");
            statusText.Text = LocalizationService.Get("Code.PreparingTheRasterEngineAndLoadingInstalledPrinters");
            continueButton.Content = LocalizationService.Get("Code.Continue");
        }

        private void ExtendedSplashUnloaded(object sender, RoutedEventArgs args)
        {
            Window.Current.SizeChanged -= WindowSizeChanged;
        }

        private void WindowSizeChanged(object sender, WindowSizeChangedEventArgs args)
        {
            PositionContent();
        }

        private void PositionContent()
        {
            var windowBounds = Window.Current.Bounds;
            var imageBounds = FallbackImageBounds(windowBounds);
            splashImage.Width = imageBounds.Width;
            splashImage.Height = imageBounds.Height;
            Canvas.SetLeft(splashImage, imageBounds.X);
            Canvas.SetTop(splashImage, imageBounds.Y);

            if (startupDetails == null)
                return;

            var detailsWidth = Math.Min(430, Math.Max(240, windowBounds.Width - 40));
            startupDetails.Width = detailsWidth;
            startupDetails.Measure(new Size(detailsWidth, double.PositiveInfinity));
            var detailsHeight = startupDetails.DesiredSize.Height;
            var detailsLeft = Math.Max(20, imageBounds.X + (imageBounds.Width - detailsWidth) / 2);
            var preferredTop = imageBounds.Y + imageBounds.Height + 12;
            var detailsTop = Math.Min(preferredTop, Math.Max(20, windowBounds.Height - detailsHeight - 20));
            Canvas.SetLeft(startupDetails, detailsLeft);
            Canvas.SetTop(startupDetails, detailsTop);
        }

        private static Rect FallbackImageBounds(Rect windowBounds)
        {
            var width = Math.Min(620, Math.Max(240, windowBounds.Width - 40));
            var height = width * 300 / 620;
            return new Rect(
                (windowBounds.Width - width) / 2,
                Math.Max(20, (windowBounds.Height - height) / 2 - 70),
                width,
                height);
        }

        private void ContinueClicked(object sender, RoutedEventArgs args)
        {
            continueButton.IsEnabled = false;
            continueRequested.TrySetResult(true);
        }
    }
}
