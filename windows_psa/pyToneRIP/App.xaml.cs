using System;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using Windows.ApplicationModel.Activation;
using Windows.ApplicationModel.Resources.Core;
using Windows.Globalization;
using Windows.Graphics.Printing.PrintSupport;
using Windows.Storage;
using Windows.UI.Xaml;
using Windows.UI.Xaml.Controls;
using Windows.UI.Xaml.Media;
using Windows.UI.Xaml.Media.Imaging;

namespace pyToneRIP
{
    sealed partial class App : Application
    {
        internal const string StartupLogFileName = "pyToneRIP-startup.log";
        private const string InstallerLanguageArgument = "--set-installer-language=";
        private Exception startupFailure;
        public App()
        {
            ResetStartupLog();
            WriteStartupStage("App constructor entered");
            UnhandledException += AppUnhandledException;
            try
            {
                WriteStartupStage("App.InitializeComponent begin");
                InitializeComponent();
                WriteStartupStage("App.InitializeComponent complete");
            }
            catch (Exception error)
            {
                startupFailure = error;
                WriteStartupException("App.InitializeComponent failed", error);
            }
        }

        protected override async void OnLaunched(LaunchActivatedEventArgs args)
        {
            WriteStartupStage("App.OnLaunched entered");
            WriteStartupStage("App.OnLaunched prelaunch=" + args.PrelaunchActivated);
            var installerLanguageRequest = args.Arguments != null
                && args.Arguments.StartsWith(
                    InstallerLanguageArgument, StringComparison.Ordinal);
            try
            {
                if (startupFailure != null)
                {
                    if (installerLanguageRequest)
                    {
                        WriteStartupException("Installer language startup failed", startupFailure);
                        WriteInstallerLanguageFailure(args.Arguments, startupFailure);
                        Exit();
                        return;
                    }
                    WriteStartupStage("App.OnLaunched is showing the App.InitializeComponent failure");
                    ShowStartupFailure(startupFailure);
                    return;
                }

                if (TryApplyInstallerLanguage(args.Arguments))
                {
                    // Setup activates this instance solely to persist the
                    // user's installer choice. No Manager window is needed.
                    await Task.Yield();
                    Exit();
                    return;
                }

                if (args.PrelaunchActivated)
                {
                    // A prelaunch without our installer argument is not a
                    // user request to open Manager.
                    WriteStartupStage("App.OnLaunched prelaunch without installer language");
                    return;
                }

                // Use the same lightweight Grid-first activation as Settings.
                // Manager also places its artwork here so it appears with the
                // ring, before ExtendedSplash's heavier Page is loaded.
                WriteStartupStage("App.OnLaunched creating progress shell");
                var splashFrame = new Frame();
                var launchProgress = CreateLaunchProgressShell(
                    out var splashArtwork, out var artworkReady);
                var launchProgressLoaded = new TaskCompletionSource<bool>(
                    TaskCreationOptions.RunContinuationsAsynchronously);
                launchProgress.Loaded += (sender, eventArgs) =>
                {
                    WriteStartupStage("App.OnLaunched progress shell loaded");
                    launchProgressLoaded.TrySetResult(true);
                };
                splashFrame.Content = launchProgress;
                Window.Current.Content = splashFrame;
                Window.Current.Activate();
                WriteStartupStage("App.OnLaunched progress shell activated");
                await launchProgressLoaded.Task;
                if (await Task.WhenAny(artworkReady, Task.Delay(3000)) != artworkReady)
                {
                    // Keep startup moving if XAML never reports ImageOpened.
                    ((StackPanel)launchProgress.Children[0]).Opacity = 1;
                    WriteStartupStage("App.OnLaunched early artwork render timed out");
                }
                await Task.Delay(50);

                WriteStartupStage("App.OnLaunched creating ExtendedSplash");
                var extendedSplash = new ExtendedSplash(splashArtwork);
                splashFrame.Content = extendedSplash;
                WriteStartupStage("App.OnLaunched ExtendedSplash assigned");
                await extendedSplash.FirstRender;

                WriteStartupStage("App.OnLaunched creating hidden MainPage");
                var frame = new Frame();
                MainPage.PrepareNextInstanceForAppStartup();
                if (!frame.Navigate(typeof(MainPage)))
                    throw new InvalidOperationException("Could not navigate to MainPage.");
                var mainPage = frame.Content as MainPage
                    ?? throw new InvalidOperationException("MainPage navigation returned no page.");

                WriteStartupStage("App.OnLaunched preparing hidden MainPage");
                await mainPage.PrepareForStartupAsync(extendedSplash.ReportStatus);
                if (!mainPage.HasInstalledPrinters)
                    WriteStartupStage("App.OnLaunched waiting for first-run Continue");
                await extendedSplash.CompleteAsync(mainPage.HasInstalledPrinters);

                WriteStartupStage("App.OnLaunched revealing prepared MainPage");
                Window.Current.Content = frame;
                await Task.Yield();
                if (!mainPage.HasInstalledPrinters)
                    await mainPage.OpenInitialAddPrinterIfNeededAsync();
                WriteStartupStage("App.OnLaunched startup complete");
            }
            catch (Exception error)
            {
                WriteStartupException("App.OnLaunched failed", error);
                if (installerLanguageRequest)
                {
                    WriteInstallerLanguageFailure(args.Arguments, error);
                    Exit();
                    return;
                }
                ShowStartupFailure(error);
            }
        }

        private static bool TryApplyInstallerLanguage(string arguments)
        {
            if (arguments == null || !arguments.StartsWith(
                InstallerLanguageArgument, StringComparison.Ordinal))
                return false;

            const string requestIdArgument = " --request-id=";
            var requestIdOffset = arguments.IndexOf(
                requestIdArgument, InstallerLanguageArgument.Length,
                StringComparison.Ordinal);
            if (requestIdOffset < 0)
                throw new ArgumentException("Installer language request ID is missing.");
            var language = arguments.Substring(
                InstallerLanguageArgument.Length,
                requestIdOffset - InstallerLanguageArgument.Length);
            var requestIdText = arguments.Substring(
                requestIdOffset + requestIdArgument.Length);
            if (!Guid.TryParseExact(requestIdText, "N", out var requestId))
                throw new ArgumentException("Installer language request ID is invalid.");

            // Verify an actual string candidate in the requested language.
            // ManifestLanguages rejected ru-RU on the installed package even
            // though its manifest and PRI both contained that language.
            var context = ResourceContext.GetForViewIndependentUse().Clone();
            context.Languages = new[] { language };
            var candidate = ResourceManager.Current.MainResourceMap
                .GetSubtree("Resources")
                .GetValue("MainPage/Language/Label", context);
            var hasRequestedResource = false;
            foreach (var qualifier in candidate.Qualifiers)
            {
                if (string.Equals(qualifier.QualifierName, "Language",
                    StringComparison.OrdinalIgnoreCase)
                    && string.Equals(qualifier.QualifierValue, language,
                        StringComparison.OrdinalIgnoreCase))
                {
                    hasRequestedResource = true;
                    break;
                }
            }
            if (!hasRequestedResource)
                throw new ArgumentException("Installer language resource is unavailable: " + language);

            ApplicationLanguages.PrimaryLanguageOverride = language;
            Shared.SharedApplicationStorage.LanguageOverride = language;
            // The setter persists the override for this package. An immediate
            // getter result is useful for diagnostics, but rejecting the
            // request here caused false setup failures even after the app
            // accepted the selected language.
            WriteStartupStage("Installer language setter completed: " + language
                + "; immediate readback: "
                + ApplicationLanguages.PrimaryLanguageOverride);

            // A packaged app may stay alive after activation. A request-specific
            // receipt is the installer's completion signal, not process exit.
            var receiptPath = Path.Combine(
                ApplicationData.Current.LocalFolder.Path,
                "installer-language-" + requestId.ToString("N") + ".txt");
            var pendingReceiptPath = receiptPath + ".pending";
            File.WriteAllText(pendingReceiptPath, language, Encoding.UTF8);
            File.Move(pendingReceiptPath, receiptPath);
            WriteStartupStage("Installer language request confirmed: " + language);
            return true;
        }

        private static void WriteInstallerLanguageFailure(
            string arguments, Exception error)
        {
            try
            {
                const string requestIdArgument = " --request-id=";
                var offset = arguments.LastIndexOf(
                    requestIdArgument, StringComparison.Ordinal);
                if (offset < 0 || !Guid.TryParseExact(
                    arguments.Substring(offset + requestIdArgument.Length),
                    "N", out var requestId))
                    return;
                var failurePath = Path.Combine(
                    ApplicationData.Current.LocalFolder.Path,
                    "installer-language-" + requestId.ToString("N") + ".txt.error");
                var pendingPath = failurePath + ".pending";
                File.WriteAllText(pendingPath, error.Message, Encoding.UTF8);
                File.Move(pendingPath, failurePath);
            }
            catch (Exception writeError)
            {
                WriteStartupException("Could not report installer language failure", writeError);
            }
        }

        protected override async void OnActivated(IActivatedEventArgs args)
        {
            WriteStartupStage("App.OnActivated entered: " + args.Kind);
            if (args.Kind != ActivationKind.PrintSupportSettingsUI)
            {
                WriteStartupStage("App.OnActivated forwarding non-print activation");
                base.OnActivated(args);
                return;
            }

            Windows.Foundation.Deferral settingsDeferral = null;
            try
            {
                if (startupFailure != null)
                {
                    WriteStartupStage("App.OnActivated is showing the App.InitializeComponent failure");
                    ShowStartupFailure(startupFailure);
                    return;
                }

                WriteStartupStage("App.OnActivated obtaining PrintSupportSettings deferral");
                var activation = (PrintSupportSettingsActivatedEventArgs)args;
                settingsDeferral = activation.GetDeferral();
                var settingsSession = activation.Session;
                // Keep Preferences independent of the main launch artwork.
                // Present its own lightweight ring before constructing XAML.
                var frame = new Frame();
                var settingsProgress = CreateProgressShell(Windows.UI.Colors.White);
                settingsProgress.Loaded += (sender, eventArgs) =>
                    WriteStartupStage("App.OnActivated progress shell loaded");
                frame.Content = settingsProgress;
                Window.Current.Content = frame;
                Window.Current.Activate();
                WriteStartupStage("App.OnActivated progress shell activated");
                await Task.Delay(50);
                WriteStartupStage("App.OnActivated creating PropertiesPage");
                frame.Content = new PropertiesPage(settingsSession, settingsDeferral);
                settingsDeferral = null; // The page completes it on Apply/Cancel.
                WriteStartupStage("App.OnActivated PropertiesPage displayed");
            }
            catch (Exception error)
            {
                settingsDeferral?.Complete();
                WriteStartupException("App.OnActivated failed", error);
                ShowStartupFailure(error);
            }
        }

        private static Grid CreateProgressShell(Windows.UI.Color background)
        {
            return new Grid
            {
                Background = new Windows.UI.Xaml.Media.SolidColorBrush(background),
                Children =
                {
                    new ProgressRing
                    {
                        IsActive = true,
                        Width = 42,
                        Height = 42,
                        HorizontalAlignment = HorizontalAlignment.Center,
                        VerticalAlignment = VerticalAlignment.Center,
                    }
                }
            };
        }

        private static Grid CreateLaunchProgressShell(
            out BitmapImage artworkSource, out Task artworkReady)
        {
            var ready = new TaskCompletionSource<bool>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            artworkSource = new BitmapImage(new Uri("ms-appx:///Assets/SplashScreen.png"));
            artworkReady = ready.Task;
            var shell = new Grid
            {
                Background = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(255, 243, 245, 248)),
            };
            var content = new StackPanel
            {
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
                Spacing = 12,
                Opacity = 0,
            };
            var artwork = new Image
            {
                Width = 620,
                Height = 300,
                Stretch = Stretch.Uniform,
            };
            artwork.ImageOpened += (sender, eventArgs) =>
            {
                content.Opacity = 1;
                WriteStartupStage("App.OnLaunched early artwork opened with ring");
                ready.TrySetResult(true);
            };
            artwork.ImageFailed += (sender, eventArgs) =>
            {
                content.Opacity = 1;
                WriteStartupStage("App.OnLaunched early artwork failed: " + eventArgs.ErrorMessage);
                ready.TrySetResult(false);
            };
            artwork.Source = artworkSource;
            content.Children.Add(artwork);
            content.Children.Add(new ProgressRing
            {
                IsActive = true,
                Width = 42,
                Height = 42,
                HorizontalAlignment = HorizontalAlignment.Center,
            });
            shell.Children.Add(content);
            return shell;
        }

        private static void ShowStartupFailure(Exception error)
        {
            WriteStartupException("ShowStartupFailure entered", error);
            try
            {
                Window.Current.Content = new ScrollViewer
                {
                    Content = new TextBox
                    {
                        Text = "pyToneRIP UI startup failed:\r\n\r\n" + error,
                        IsReadOnly = true,
                        IsTabStop = false,
                        AcceptsReturn = true,
                        TextWrapping = TextWrapping.Wrap,
                    }
                };
                Window.Current.Activate();
                WriteStartupStage("ShowStartupFailure Window activated");
            }
            catch (Exception fallbackError)
            {
                WriteStartupException("ShowStartupFailure failed", fallbackError);
                throw;
            }
        }

        private static void AppUnhandledException(object sender, Windows.UI.Xaml.UnhandledExceptionEventArgs args)
        {
            WriteStartupException("Application.UnhandledException", args.Exception);
        }

        internal static void WriteStartupStage(string stage)
        {
            WriteStartupLine(stage);
        }

        internal static void WriteStartupException(string stage, Exception error)
        {
            WriteStartupLine(stage + Environment.NewLine + FormatException(error));
        }

        private static string FormatException(Exception error)
        {
            var result = new StringBuilder();
            int depth = 0;
            while (error != null)
            {
                if (depth > 0)
                {
                    result.AppendLine("Inner exception:");
                }
                result.AppendLine("Type: " + error.GetType().FullName);
                result.AppendLine("Message: " + error.Message);
                result.AppendLine("HRESULT: 0x" + error.HResult.ToString("X8", CultureInfo.InvariantCulture));
                if (!string.IsNullOrWhiteSpace(error.StackTrace))
                {
                    result.AppendLine("Stack trace:");
                    result.AppendLine(error.StackTrace);
                }
                error = error.InnerException;
                depth++;
            }
            return result.ToString();
        }

        private static void ResetStartupLog()
        {
            try
            {
                string path = Path.Combine(ApplicationData.Current.LocalFolder.Path, StartupLogFileName);
                File.WriteAllText(
                    path,
                    "pyToneRIP startup diagnostics" + Environment.NewLine,
                    System.Text.Encoding.UTF8);
            }
            catch
            {
                // Diagnostics must never become a startup dependency.
            }
        }

        private static void WriteStartupLine(string value)
        {
            try
            {
                string path = Path.Combine(ApplicationData.Current.LocalFolder.Path, StartupLogFileName);
                string line = DateTimeOffset.Now.ToString("O", CultureInfo.InvariantCulture)
                    + " | " + value + Environment.NewLine;
                File.AppendAllText(path, line, System.Text.Encoding.UTF8);
            }
            catch
            {
                // Diagnostics must never hide the original startup failure.
            }
        }
    }
}
