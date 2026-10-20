using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using pyToneRIP.Models;
using pyToneRIP.Services;
using Windows.ApplicationModel;
using Windows.Devices.Enumeration;
using Windows.System;
using Windows.Storage;
using Windows.UI.Xaml.Media.Imaging;
using Windows.UI.Xaml;
using Windows.UI.Xaml.Controls;
using Windows.UI.Xaml.Documents;
using Windows.UI.Xaml.Input;
using Windows.UI.Xaml.Media;

namespace pyToneRIP
{
    public sealed partial class MainPage : Page
    {
        private const string RuntimeReadinessSetting = "RuntimeReadinessContract";
        private const string RuntimeReadinessContract = "cython-kernels-v1";
        private CapabilityExport capabilities;
        private PrinterInstanceRegistry registry;
        private readonly DispatcherTimer networkTimer;
        private readonly DispatcherTimer progressTimer;
        private bool networkCheckRunning;
        private bool progressCheckRunning;
        private bool settingsNotificationCheckRunning;
        private HashSet<string> activeTransmissionHosts =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        private IReadOnlyCollection<PrinterJobActivity> activeJobActivities =
            Array.Empty<PrinterJobActivity>();
        private DateTimeOffset nextTransportActivityRead = DateTimeOffset.MinValue;
        private bool defaultPrinterCheckRunning;
        private bool printerLifecycleRunning;
        private DeviceWatcher windowsQueueWatcher;
        private bool windowsQueueEnumerationCompleted;
        private bool windowsQueueRefreshPending;
        private bool registryLoading;
        private bool managerPageLoaded;
        private bool managerSuspended;
        private bool loadStarted;
        private readonly bool appOwnsStartup;
        private static bool nextInstanceUsesAppStartup;
        // PrintSupportSettingsUI can replace the window content and later
        // recreate MainPage in the same application process.  The welcome
        // dialog belongs to a process launch, not to every page instance.
        private static bool startupWelcomeShown;
        private static bool showLogInManager;
        private string initializationWarning;

        public MainPage()
        {
            appOwnsStartup = nextInstanceUsesAppStartup;
            nextInstanceUsesAppStartup = false;
            InitializeComponent();
            PrinterList.AddHandler(UIElement.KeyDownEvent,
                new KeyEventHandler(PrinterListKeyDown), true);
            ShowLogCheckBox.IsChecked = showLogInManager;
            LogPanel.Visibility = showLogInManager
                ? Visibility.Visible : Visibility.Collapsed;
            if (showLogInManager)
                RenderLog();
            ManagerLogger.LineWritten += OnManagerLogLineWritten;
            ManagerLogger.Write("Manager window opened.");
            networkTimer = new DispatcherTimer
            {
                Interval = TimeSpan.FromSeconds(10),
            };
            networkTimer.Tick += NetworkTimerTick;
            progressTimer = new DispatcherTimer
            {
                Interval = TimeSpan.FromMilliseconds(750),
            };
            progressTimer.Tick += ProgressTimerTick;
            Loaded += async (_, __) =>
            {
                managerPageLoaded = true;
                managerSuspended = false;
                Window.Current.Activated += WindowActivated;
                Application.Current.Suspending += QueueWatcherSuspending;
                Application.Current.Resuming += QueueWatcherResuming;
                StartWindowsQueueWatcher();
                if (!appOwnsStartup)
                    await InitializeAsync();
            };
            Unloaded += (_, __) =>
            {
                managerPageLoaded = false;
                StopWindowsQueueWatcher();
                networkTimer.Stop();
                progressTimer.Stop();
                Window.Current.Activated -= WindowActivated;
                Application.Current.Suspending -= QueueWatcherSuspending;
                Application.Current.Resuming -= QueueWatcherResuming;
                ManagerLogger.LineWritten -= OnManagerLogLineWritten;
            };
        }

        internal static void PrepareNextInstanceForAppStartup()
        {
            nextInstanceUsesAppStartup = true;
        }

        protected override async void OnNavigatedTo(Windows.UI.Xaml.Navigation.NavigationEventArgs args)
        {
            base.OnNavigatedTo(args);
            // Returning from Preferences must show the state that was just
            // committed by the Manager-owned queue, without asking
            // the user to press Refresh.
            if (loadStarted)
                await LoadAsync();
        }

        protected override void OnNavigatedFrom(Windows.UI.Xaml.Navigation.NavigationEventArgs args)
        {
            // MainPage remains in the Frame back stack while Preferences is
            // open. Its polling timers must not keep refreshing bindings or
            // probing printers behind the settings page.
            networkTimer.Stop();
            progressTimer.Stop();
            managerPageLoaded = false;
            StopWindowsQueueWatcher();
            base.OnNavigatedFrom(args);
        }

        private async Task InitializeAsync()
        {
            if (loadStarted)
                return;
            loadStarted = true;
            if (startupWelcomeShown)
            {
                MainRoot.Visibility = Visibility.Visible;
                await LoadAsync();
                return;
            }
            startupWelcomeShown = true;
            initializationWarning = await ValidateRuntimeOnFirstLaunchAsync();
        }

        internal async Task PrepareForStartupAsync(Action<string> reportStatus)
        {
            if (loadStarted)
                return;
            loadStarted = true;
            startupWelcomeShown = true;
            var settings = Windows.Storage.ApplicationData.Current.LocalSettings.Values;
            var marker = CurrentRuntimeReadinessMarker();
            var runtimeReady = string.Equals(
                settings[RuntimeReadinessSetting] as string, marker, StringComparison.Ordinal);
            try
            {
                reportStatus?.Invoke(runtimeReady
                    ? "Loading installed printers and queue state..."
                    : "Preparing the raster engine and loading installed printers...");
                if (!runtimeReady)
                {
                    await QueueHelperClient.ValidateRuntimeAsync();
                    settings[RuntimeReadinessSetting] = marker;
                }
                await LoadAsync();
                MainRoot.Visibility = Visibility.Visible;
                reportStatus?.Invoke("pyToneRIP is ready");
            }
            catch (Exception error)
            {
                App.WriteStartupException("Startup preparation failed", error);
                initializationWarning = "Runtime preparation will be retried on the next start: " + error.Message;
                await LoadAsync();
                MainRoot.Visibility = Visibility.Visible;
                reportStatus?.Invoke("pyToneRIP is ready with a warning");
            }
        }

        internal async Task OpenInitialAddPrinterIfNeededAsync()
        {
            if (registry?.Instances?.Count == 0)
                await OpenAddPrinterAsync();
        }

        internal bool HasInstalledPrinters => registry?.Instances?.Count > 0;

        private static string CurrentRuntimeReadinessMarker()
        {
            var version = Package.Current.Id.Version;
            return RuntimeReadinessContract + ":"
                + version.Major + "." + version.Minor + "."
                + version.Build + "." + version.Revision;
        }

        private async Task<string> ValidateRuntimeOnFirstLaunchAsync()
        {
            var settings = Windows.Storage.ApplicationData.Current.LocalSettings.Values;
            var marker = CurrentRuntimeReadinessMarker();
            var runtimeReady = string.Equals(
                settings[RuntimeReadinessSetting] as string, marker, StringComparison.Ordinal);

            var title = new TextBlock
            {
                Text = LocalizationService.Get("Code.WelcomeToPyToneRIP"),
                FontSize = 22,
                FontWeight = Windows.UI.Text.FontWeights.SemiBold,
            };
            var message = new TextBlock
            {
                Text = runtimeReady
                    ? LocalizationService.Get("Code.LoadingInstalledPrintersAndQueueState")
                    : LocalizationService.Get("Code.PreparingTheRasterEngineAndLoadingInstalledPrinters"),
                TextWrapping = TextWrapping.Wrap,
                MaxWidth = 430,
            };
            var progress = new ProgressRing
            {
                IsActive = true,
                Width = 42,
                Height = 42,
                HorizontalAlignment = HorizontalAlignment.Center,
                Margin = new Thickness(0, 8, 0, 2),
            };
            var content = new StackPanel { Spacing = 12 };
            content.Children.Add(new Image
            {
                Source = new BitmapImage(new Uri("ms-appx:///Assets/SplashScreen.png")),
                Width = 620,
                Height = 300,
                Stretch = Windows.UI.Xaml.Media.Stretch.Uniform,
                HorizontalAlignment = HorizontalAlignment.Center,
            });
            content.Children.Add(new TextBlock
            {
                Text = "PyToneRIP",
                FontWeight = Windows.UI.Text.FontWeights.SemiBold,
            });
            content.Children.Add(title);
            content.Children.Add(message);
            content.Children.Add(progress);

            var dialog = new ContentDialog
            {
                Title = LocalizationService.Get("Code.StartingPyToneRIP"),
                Content = content,
                PrimaryButtonText = LocalizationService.Get("Code.Continue"),
                IsPrimaryButtonEnabled = false,
            };
            var preparationComplete = false;
            dialog.Closing += (_, closingArgs) =>
            {
                if (!preparationComplete)
                    closingArgs.Cancel = true;
            };
            var dialogOperation = dialog.ShowAsync();
            await Task.Yield();
            try
            {
                if (!runtimeReady)
                {
                    await QueueHelperClient.ValidateRuntimeAsync();
                    settings[RuntimeReadinessSetting] = marker;
                }
                await LoadAsync();
                MainRoot.Visibility = Visibility.Visible;
                title.Text = LocalizationService.Get("Code.PyToneRIPIsReady");
                message.Text = LocalizationService.Get("Code.InstalledPrintersAreReady");
                progress.IsActive = false;
                progress.Visibility = Visibility.Collapsed;
                preparationComplete = true;
                var hasInstalledPrinters = registry?.Instances?.Count > 0;
                dialog.IsPrimaryButtonEnabled = !hasInstalledPrinters;
                if (hasInstalledPrinters)
                    dialog.Hide();
                await dialogOperation;
                if (!hasInstalledPrinters)
                    await OpenAddPrinterAsync();
                return null;
            }
            catch (Exception error)
            {
                App.WriteStartupException("First-run runtime preparation failed", error);
                title.Text = LocalizationService.Get("Code.RasterEnginePreparationWasNotCompleted");
                message.Text = LocalizationService.Format("Code.ThePrinterManagerCanStillOpenPyToneRIPWillRetryPreparationTheNextTimeItS",
                    error.Message);
                progress.IsActive = false;
                progress.Visibility = Visibility.Collapsed;
                preparationComplete = true;
                dialog.IsPrimaryButtonEnabled = true;
                MainRoot.Visibility = Visibility.Visible;
                await dialogOperation;
                await LoadAsync();
                return LocalizationService.Format("Code.RuntimePreparationWillBeRetriedOnTheNextStart", error.Message);
            }
        }

        private async Task LoadAsync()
        {
            if (printerLifecycleRunning)
            {
                windowsQueueRefreshPending = true;
                return;
            }
            if (registryLoading)
                return;
            registryLoading = true;
            ManagerLogger.Write("Loading printer registry and Windows queue state.");
            try
            {
                capabilities = await CapabilityCatalog.LoadIndexAsync();
                registry = await PrinterInstanceStore.ReadAsync();
                var queueWarning = await SynchronizeWindowsQueuesAsync();
                RefreshList();
                // Queue discovery is needed before the welcome dialog closes.
                // Network probes and job progress are periodic background UI
                // updates; waiting for them here makes startup depend on TCP
                // timeouts from offline printers.
                networkTimer.Start();
                progressTimer.Start();
                _ = RefreshNetworkStatesAsync();
                StatusText.Text = initializationWarning ?? queueWarning ?? LocalizationService.Format("Code.InstalledPrintersNetworkStatusRefreshesEvery10Seconds",
                    registry.Instances.Count);
                EmptyDescription.Text = LocalizationService.Get("Code.AddASupportedModelAsAWindowsVirtualPrinter");
                ManagerLogger.Write("Printer list loaded: " + registry.Instances.Count + " registered queue(s).");
            }
            catch (Exception error)
            {
                StatusText.Text = LocalizationService.Format("Code.CouldNotLoadPrinterManager", error.Message);
                ManagerLogger.WriteException("Loading printer manager", error);
            }
            finally
            {
                registryLoading = false;
                await RefreshPendingWindowsQueueStateAsync();
            }
            await RefreshSettingsNotificationsAsync();
        }

        private async Task RefreshSettingsNotificationsAsync()
        {
            if (settingsNotificationCheckRunning) return;
            settingsNotificationCheckRunning = true;
            try
            {
                foreach (var notification in await SettingsApplyNotifications.ReadPendingAsync())
                {
                    StatusText.Text = notification.QueueName + ": " + LocalizationService.Format(
                        "Code.ConfigurationSavedWindowsDefaultsFailed", notification.Message);
                    ManagerLogger.Write("RIP settings saved; Windows preferences failed: queue="
                        + notification.QueueName + "; occurredUtc=" + notification.OccurredUtc
                        + Environment.NewLine + notification.Details);
                    await SettingsApplyNotifications.AcknowledgeAsync(notification);
                }
            }
            catch (Exception error)
            {
                ManagerLogger.WriteException("Reading settings notifications", error);
            }
            finally { settingsNotificationCheckRunning = false; }
        }

        private void RefreshList()
        {
            var items = registry?.Instances.OrderBy(item => item.ModelName)
                .ThenBy(item => item.QueueName).ToList();
            var configurationWidth = MeasureConfigurationColumnWidth(items);
            var networkWidth = MeasureNetworkColumnWidth();
            ConfigurationHeaderColumn.Width = configurationWidth;
            NetworkHeaderColumn.Width = networkWidth;
            if (items != null)
            {
                foreach (var item in items)
                {
                    item.ConfigurationColumnWidth = configurationWidth;
                    item.NetworkColumnWidth = networkWidth;
                }
            }
            PrinterList.ItemsSource = items;
            EmptyState.Visibility = items == null || items.Count == 0
                ? Visibility.Visible : Visibility.Collapsed;
            UpdateSelectedPrinterActions();
        }

        private static GridLength MeasureConfigurationColumnWidth(
            System.Collections.Generic.IEnumerable<PrinterInstanceRecord> items)
        {
            var values = (items ?? Enumerable.Empty<PrinterInstanceRecord>())
                .Select(item => item.ConfigurationStatus)
                .Concat(new[] {
                    LocalizationService.Get("MainPage.Table.Configuration.Text"),
                    LocalizationService.Get("Choice.RemovedFromWindows"),
                });
            var maximum = values.Select(value =>
            {
                var probe = new TextBlock
                {
                    Text = value ?? string.Empty,
                    FontWeight = Windows.UI.Text.FontWeights.SemiBold,
                };
                probe.Measure(new Windows.Foundation.Size(
                    double.PositiveInfinity, double.PositiveInfinity));
                return probe.DesiredSize.Width;
            }).DefaultIfEmpty(0).Max();

            // The status badge has 6 px horizontal padding on both sides.
            return new GridLength(Math.Max(105, Math.Ceiling(maximum + 12)));
        }

        private static GridLength MeasureNetworkColumnWidth()
        {
            var values = new[]
            {
                "Unknown", "Checking", "Online", "Preparing",
                "Queued", "Processing", "Printing", "Offline",
            }.Select(state => LocalizationService.Get("Choice." + state))
                .Concat(new[] { LocalizationService.Get("MainPage.Table.Network.Text") });
            var maximum = values.Select(value =>
            {
                var probe = new TextBlock
                {
                    Text = value,
                    FontWeight = Windows.UI.Text.FontWeights.SemiBold,
                };
                probe.Measure(new Windows.Foundation.Size(
                    double.PositiveInfinity, double.PositiveInfinity));
                return probe.DesiredSize.Width;
            }).Max();
            return new GridLength(Math.Max(105, Math.Ceiling(maximum + 12)));
        }

        private void PrinterSelectionChanged(object sender, SelectionChangedEventArgs args)
        {
            var selected = PrinterList.SelectedItem as PrinterInstanceRecord;
            UpdateSelectedPrinterActions();
            if (selected != null)
                ManagerLogger.Write("Selected printer: " + selected.QueueName);
        }

        private async void PrinterContextMenuOpening(object sender, object args)
        {
            var menu = sender as MenuFlyout;
            var target = menu?.Target;
            var selected = (target as FrameworkElement)?.DataContext
                as PrinterInstanceRecord;
            if (selected == null)
                return;
            PrinterList.SelectedItem = selected;
            foreach (var item in menu.Items.OfType<MenuFlyoutItem>())
            {
                switch (item.Tag as string)
                {
                    case "jobs": item.Text = LocalizationService.Get("MainPage.Context.OpenJobs.Text"); break;
                    case "preferences": item.Text = LocalizationService.Get("MainPage.Preferences.Label"); break;
                    case "rename": item.Text = LocalizationService.Get("MainPage.Rename.Label"); break;
                    case "remove": item.Text = LocalizationService.Get("MainPage.Remove.Label"); break;
                    case "default":
                        item.Text = LocalizationService.Get("MainPage.SetDefault.Label");
                        item.IsEnabled = !printerLifecycleRunning;
                        break;
                    case "cancel-job":
                        item.Text = LocalizationService.Get("MainPage.Context.CancelJob.Text");
                        item.IsEnabled = false;
                        try
                        {
                            var activities = await JobProgressReader.ReadActiveJobActivitiesAsync();
                            var ripJobs = activities.Count(activity => string.Equals(
                                activity.QueueName, selected.QueueName,
                                StringComparison.OrdinalIgnoreCase));
                            var runningRipJobs = activities.Count(activity =>
                                string.Equals(activity.QueueName, selected.QueueName,
                                    StringComparison.OrdinalIgnoreCase)
                                && activity.State != "Queued");
                            var windowsJob = await QueueHelperClient.HasCurrentJobAsync(
                                selected.QueueName);
                            if (ReferenceEquals(menu.Target, target))
                                item.IsEnabled = windowsJob || ripJobs == 1
                                    || runningRipJobs == 1;
                        }
                        catch (Exception error)
                        {
                            ManagerLogger.WriteException(
                                "Checking active print job for " + selected.QueueName, error);
                        }
                        break;
                }
            }
        }

        private void UpdateSelectedPrinterActions()
        {
            var selected = PrinterList.SelectedItem as PrinterInstanceRecord;
            OpenJobsButton.IsEnabled = selected != null;
            SetDefaultPrinterButton.IsEnabled = selected != null
                && !printerLifecycleRunning;
        }

        private void ShowLogChanged(object sender, RoutedEventArgs args)
        {
            var show = (sender as CheckBox)?.IsChecked == true;
            showLogInManager = show;
            LogPanel.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
            if (show)
                RenderLog();
        }

        private void LogContextMenuOpening(object sender, object args)
        {
            CopyLogSelectionItem.IsEnabled = !string.IsNullOrEmpty(LogTextBlock.SelectedText);
        }

        private void CopyLogSelection(object sender, RoutedEventArgs args)
        {
            LogTextBlock.CopySelectionToClipboard();
        }

        private void SelectAllLogText(object sender, RoutedEventArgs args)
        {
            LogTextBlock.SelectAll();
        }

        private async void OnManagerLogLineWritten(string line)
        {
            await Dispatcher.RunAsync(Windows.UI.Core.CoreDispatcherPriority.Low, () =>
            {
                if (LogPanel.Visibility != Visibility.Visible)
                    return;
                if (string.IsNullOrEmpty(line))
                    RenderLog();
                else
                {
                    AppendLogLine(line);
                    ScrollLogToEnd();
                }
            });
        }

        private void RenderLog()
        {
            LogTextBlock.Inlines.Clear();
            using (var reader = new StringReader(ManagerLogger.ReadAll()))
            {
                string line;
                while ((line = reader.ReadLine()) != null)
                    AppendLogLine(line);
            }
            ScrollLogToEnd();
        }

        private void ScrollLogToEnd()
        {
            LogScrollViewer.UpdateLayout();
            LogScrollViewer.ChangeView(null, LogScrollViewer.ScrollableHeight, null, true);
        }

        private void AppendLogLine(string line)
        {
            var span = new Span();
            var muted = (Brush)Application.Current.Resources["SecondaryTextBrush"];
            if (line.StartsWith("PRINT | Job ", StringComparison.Ordinal))
            {
                var jobEnd = line.IndexOf(" | ", "PRINT | Job ".Length,
                    StringComparison.Ordinal);
                var timestampStart = jobEnd + 3;
                var timestampEnd = jobEnd < 0 ? -1 : line.IndexOf(" | ",
                    timestampStart, StringComparison.Ordinal);
                if (timestampEnd >= 0)
                {
                    AddLogRun(span, line.Substring(0, timestampStart),
                        (Brush)Application.Current.Resources["AccentBrush"]);
                    AddLogRun(span, line.Substring(timestampStart,
                        timestampEnd - timestampStart), muted);
                    AddLogRun(span, line.Substring(timestampEnd),
                        LogMessageBrush(line.Substring(timestampEnd)));
                }
                else
                    AddLogRun(span, line, (Brush)Application.Current.Resources["AccentBrush"]);
            }
            else
            {
                var timestampEnd = line.IndexOf(" | ", StringComparison.Ordinal);
                if (timestampEnd > 0)
                {
                    AddLogRun(span, line.Substring(0, timestampEnd), muted);
                    AddLogRun(span, line.Substring(timestampEnd),
                        LogMessageBrush(line.Substring(timestampEnd)));
                }
                else
                    AddLogRun(span, line, LogMessageBrush(line));
            }
            span.Inlines.Add(new LineBreak());
            LogTextBlock.Inlines.Add(span);
        }

        private static void AddLogRun(Span span, string text, Brush foreground)
        {
            var run = new Run { Text = text };
            if (foreground != null)
                run.Foreground = foreground;
            span.Inlines.Add(run);
        }

        private static Brush LogMessageBrush(string message)
        {
            if (ContainsLogText(message, "failed") || ContainsLogText(message, "error")
                || ContainsLogText(message, "exception") || ContainsLogText(message, "offline"))
                return (Brush)Application.Current.Resources["LogErrorBrush"];
            if (ContainsLogText(message, "warning") || ContainsLogText(message, "unknown"))
                return (Brush)Application.Current.Resources["LogWarningBrush"];
            if (ContainsLogText(message, "completed") || ContainsLogText(message, "successfully")
                || ContainsLogText(message, " | SENT | ") || ContainsLogText(message, "loaded:"))
                return (Brush)Application.Current.Resources["LogSuccessBrush"];
            return null;
        }

        private static bool ContainsLogText(string value, string text) =>
            value.IndexOf(text, StringComparison.OrdinalIgnoreCase) >= 0;

        private async void NetworkTimerTick(object sender, object args) =>
            await RefreshNetworkStatesAsync();

        private async void WindowActivated(
            object sender,
            Windows.UI.Core.WindowActivatedEventArgs args)
        {
            if (args.WindowActivationState == Windows.UI.Core.CoreWindowActivationState.Deactivated
                || registry?.Instances == null)
                return;
            // This catches changes made in Windows Settings while Manager was
            // in the background, without polling and launching QueueHelper on
            // a fixed timer while the user is doing nothing.
            StartWindowsQueueWatcher();
            await RefreshDefaultPrinterStateAsync();
        }

        private void StartWindowsQueueWatcher()
        {
            if (!managerPageLoaded || managerSuspended || windowsQueueWatcher != null)
                return;
            try
            {
                // Microsoft documents this interface selector for installed
                // print queues. It also enumerates VirtualPrinterManager queues.
                const string selector =
                    "System.Devices.InterfaceClassGuid:=\"{0ecef634-6ef0-472a-8085-5ad023ecbccd}\"";
                var watcher = DeviceInformation.CreateWatcher(selector);
                windowsQueueWatcher = watcher;
                windowsQueueEnumerationCompleted = false;
                watcher.Added += WindowsQueueAdded;
                watcher.Removed += WindowsQueueChanged;
                watcher.Updated += WindowsQueueChanged;
                watcher.EnumerationCompleted += WindowsQueueEnumerationCompleted;
                watcher.Stopped += WindowsQueueWatcherStopped;
                watcher.Start();
            }
            catch (Exception error)
            {
                StopWindowsQueueWatcher();
                ManagerLogger.WriteException("Starting WinRT print queue watcher", error);
                StatusText.Text = LocalizationService.Format(
                    "Code.PrinterListLoadedButWindowsQueueSynchronizationFailed", error.Message);
            }
        }

        private void StopWindowsQueueWatcher()
        {
            var watcher = windowsQueueWatcher;
            windowsQueueWatcher = null;
            windowsQueueEnumerationCompleted = false;
            windowsQueueRefreshPending = false;
            if (watcher == null)
                return;
            // Stop is asynchronous. Unsubscribe first; already-dispatched UI
            // callbacks also check the watcher instance before touching the page.
            watcher.Added -= WindowsQueueAdded;
            watcher.Removed -= WindowsQueueChanged;
            watcher.Updated -= WindowsQueueChanged;
            watcher.EnumerationCompleted -= WindowsQueueEnumerationCompleted;
            watcher.Stopped -= WindowsQueueWatcherStopped;
            try
            {
                if (watcher.Status == DeviceWatcherStatus.Started
                    || watcher.Status == DeviceWatcherStatus.EnumerationCompleted)
                    watcher.Stop();
            }
            catch (Exception error)
            {
                ManagerLogger.WriteException("Stopping WinRT print queue enumeration", error);
            }
        }

        private void QueueWatcherSuspending(object sender, SuspendingEventArgs args)
        {
            managerSuspended = true;
            StopWindowsQueueWatcher();
        }

        private void QueueWatcherResuming(object sender, object args)
        {
            managerSuspended = false;
            StartWindowsQueueWatcher();
        }

        private void WindowsQueueAdded(DeviceWatcher sender, DeviceInformation args) =>
            DispatchWindowsQueueChange(sender);

        private void WindowsQueueChanged(DeviceWatcher sender, DeviceInformationUpdate args) =>
            DispatchWindowsQueueChange(sender);

        private void WindowsQueueEnumerationCompleted(DeviceWatcher sender, object args) =>
            DispatchWindowsQueueChange(sender, enumerationCompleted: true);

        private async void WindowsQueueWatcherStopped(DeviceWatcher sender, object args)
        {
            if (sender.Status != DeviceWatcherStatus.Aborted)
                return;
            try
            {
                await Dispatcher.RunAsync(Windows.UI.Core.CoreDispatcherPriority.Normal, () =>
                {
                    if (!ReferenceEquals(sender, windowsQueueWatcher))
                        return;
                    StopWindowsQueueWatcher();
                    ManagerLogger.Write("WinRT print queue watcher aborted.");
                });
            }
            catch (Exception error)
            {
                ManagerLogger.WriteException("Stopping WinRT print queue watcher", error);
            }
        }

        private async void DispatchWindowsQueueChange(
            DeviceWatcher sender, bool enumerationCompleted = false)
        {
            try
            {
                await Dispatcher.RunAsync(Windows.UI.Core.CoreDispatcherPriority.Normal, () =>
                {
                    if (!ReferenceEquals(sender, windowsQueueWatcher))
                        return;
                    if (enumerationCompleted)
                        windowsQueueEnumerationCompleted = true;
                    // Initial Added notifications form a snapshot, not separate
                    // user changes. Reconcile once after EnumerationCompleted.
                    if (!windowsQueueEnumerationCompleted)
                        return;
                    windowsQueueRefreshPending = true;
                    _ = RefreshPendingWindowsQueueStateAsync();
                });
            }
            catch (Exception error)
            {
                ManagerLogger.WriteException("Dispatching WinRT print queue change", error);
            }
        }

        private async Task RefreshPendingWindowsQueueStateAsync()
        {
            if (windowsQueueRefreshPending && managerPageLoaded && !managerSuspended
                && !registryLoading && !printerLifecycleRunning)
                await RefreshDefaultPrinterStateAsync();
        }

        private async void ProgressTimerTick(object sender, object args) =>
            await RefreshJobProgressAsync();

        private async Task RefreshJobProgressAsync()
        {
            if (progressCheckRunning) return;
            progressCheckRunning = true;
            try
            {
                await RefreshSettingsNotificationsAsync();
                await RefreshTransportActivityAsync();
                var progress = await JobProgressReader.ReadLatestAsync();
                if (progress == null)
                {
                    JobProgressPanel.Visibility = Visibility.Collapsed;
                    return;
                }
                JobProgressPanel.Visibility = Visibility.Visible;
                JobProgressTitle.Text = LocalizationService.Format("Code.Job", progress.JobId, progress.Stage);
                JobProgressMessage.Text = progress.Message;
                JobProgressBar.Value = progress.Percent;
                JobProgressPercent.Text = progress.Percent + "%";
                foreach (var line in await JobProgressReader.ReadNewStageLinesAsync(
                    progress.JobId))
                    ManagerLogger.AppendRecordedLine(line);
            }
            finally
            {
                progressCheckRunning = false;
            }
        }

        private async Task RefreshTransportActivityAsync()
        {
            if (registry?.Instances == null) return;
            if (DateTimeOffset.UtcNow < nextTransportActivityRead) return;
            nextTransportActivityRead = DateTimeOffset.UtcNow.AddSeconds(1);
            try
            {
                var current = new HashSet<string>(
                    await JobProgressReader.ReadActiveTransmissionsAsync(),
                    StringComparer.OrdinalIgnoreCase);
                var activities = await JobProgressReader.ReadActiveJobActivitiesAsync();
                var ended = false;
                foreach (var item in registry.Instances)
                {
                    var busy = GetBusyState(item, current, activities);
                    if (busy != null)
                        item.NetworkStatus = busy;
                    else if (GetBusyState(item, activeTransmissionHosts,
                        activeJobActivities) != null)
                    {
                        item.NetworkStatus = PrinterNetworkState.Unknown.ToString();
                        ended = true;
                    }
                }
                activeTransmissionHosts = current;
                activeJobActivities = activities;
                if (ended) _ = RefreshNetworkStatesAsync();
                if (registry.Instances.Any(item => item.IsWindowsQueueMissing))
                {
                    windowsQueueRefreshPending = true;
                    _ = RefreshPendingWindowsQueueStateAsync();
                }
            }
            catch (Exception error)
            {
                ManagerLogger.WriteException("Reading active printer transmissions", error);
            }
        }

        private static string GetBusyState(
            PrinterInstanceRecord item,
            IReadOnlyCollection<string> transmissionHosts,
            IReadOnlyCollection<PrinterJobActivity> activities)
        {
            var useUsb = string.Equals(item.ConnectionKind, "usb", StringComparison.OrdinalIgnoreCase);
            var host = useUsb ? null : item.Host?.Trim();
            if (!string.IsNullOrWhiteSpace(host)
                && transmissionHosts.Contains(host))
                return PrinterNetworkState.Printing.ToString();

            var best = 0;
            foreach (var activity in activities)
            {
                var matches = !useUsb && !string.IsNullOrWhiteSpace(activity.Host)
                    ? string.Equals(host, activity.Host,
                        StringComparison.OrdinalIgnoreCase)
                        && item.Port == activity.Port
                    : !string.IsNullOrWhiteSpace(activity.QueueName)
                        && string.Equals(item.QueueName, activity.QueueName,
                            StringComparison.OrdinalIgnoreCase);
                if (!matches) continue;
                var priority = activity.State == "Printing" ? 4
                    : activity.State == "Processing" ? 3
                    : activity.State == "Queued" ? 2
                    : activity.State == "Preparing" ? 1 : 0;
                if (priority > best) best = priority;
            }
            return best == 4 ? PrinterNetworkState.Printing.ToString()
                : best == 3 ? PrinterNetworkState.Processing.ToString()
                : best == 2 ? PrinterNetworkState.Queued.ToString()
                : best == 1 ? PrinterNetworkState.Preparing.ToString()
                : null;
        }

        private async Task RefreshNetworkStatesAsync()
        {
            if (networkCheckRunning || registry?.Instances == null) return;
            networkCheckRunning = true;
            try
            {
                var available = registry.Instances.Where(item =>
                    string.Equals(item.ConnectionKind, "network", StringComparison.OrdinalIgnoreCase)
                    && !string.IsNullOrWhiteSpace(item.Host)
                    && item.Port >= 1 && item.Port <= 65535).ToList();
                foreach (var item in registry.Instances.Except(available))
                    item.NetworkStatus = PrinterNetworkState.Unknown.ToString();

                // Several Windows queues can point to one physical Epson.
                // Probe that address once and apply the result to each row.
                var checks = available.GroupBy(item => item.Host.Trim(),
                    StringComparer.OrdinalIgnoreCase).Select(async group =>
                {
                    foreach (var item in group)
                    {
                        var busy = GetBusyState(item, activeTransmissionHosts,
                            activeJobActivities);
                        if (busy != null) item.NetworkStatus = busy;
                    }
                    if (group.All(item => GetBusyState(item,
                        activeTransmissionHosts, activeJobActivities) != null))
                        return;
                    var result = await PrinterConnectivityService.TestAsync(
                        group.Key, group.First().Port);
                    foreach (var item in group)
                        item.NetworkStatus = GetBusyState(item,
                            activeTransmissionHosts, activeJobActivities)
                            ?? result.ToString();
                });
                await Task.WhenAll(checks);
                var usbInstances = registry.Instances.Where(item => item.ConnectionKind == "usb").ToList();
                if (usbInstances.Count > 0)
                {
                    try
                    {
                        var devices = await QueueHelperClient.DiscoverUsbPrintersAsync();
                        foreach (var item in usbInstances)
                        {
                            var device = devices.FirstOrDefault(value => value.DeviceInstanceId == item.UsbDeviceId);
                            item.NetworkStatus = GetBusyState(item, activeTransmissionHosts, activeJobActivities)
                                ?? (device == null ? "Offline" : "Online");
                        }
                    }
                    catch (Exception error)
                    {
                        ManagerLogger.WriteException("Refreshing USB printer state", error);
                    }
                }
            }
            finally
            {
                networkCheckRunning = false;
            }
        }

        private async Task RefreshDefaultPrinterStateAsync()
        {
            if (defaultPrinterCheckRunning || registry?.Instances == null)
                return;
            defaultPrinterCheckRunning = true;
            try
            {
                do
                {
                    windowsQueueRefreshPending = false;
                    var currentRegistry = registry;
                    var queues = await QueueHelperClient.ListAsync();
                    // A load, suspension or queue lifecycle operation can start
                    // while enumeration is in flight. Its final refresh wins.
                    if (registryLoading || printerLifecycleRunning
                        || !ReferenceEquals(currentRegistry, registry))
                    {
                        windowsQueueRefreshPending = true;
                        return;
                    }
                    if (managerSuspended || !managerPageLoaded)
                        return;
                    ApplyDefaultPrinterState(queues);
                    await RemoveMissingWindowsQueuesAsync();
                    UpdateSelectedPrinterActions();
                } while (windowsQueueRefreshPending);
            }
            catch (Exception error)
            {
                // A transient helper failure must not make the whole Manager
                // unusable. The toolbar Refresh action will retry the same
                // live Windows query and report broader synchronization errors.
                windowsQueueRefreshPending = false;
                ManagerLogger.WriteException("Refreshing Windows default printer", error);
            }
            finally
            {
                defaultPrinterCheckRunning = false;
                await RefreshPendingWindowsQueueStateAsync();
            }
        }

        private async void AddPrinter(object sender, RoutedEventArgs args)
        {
            await OpenAddPrinterAsync();
        }

        private async void SelectLanguage(object sender, RoutedEventArgs args)
        {
            if (!(sender is MenuFlyoutItem item))
                return;

            string locale = item.Tag as string ?? string.Empty;
            LocalizationService.SetLanguage(locale);
            await Task.Yield();

            // Use a fresh Frame so CommandBar elements and other XAML controls
            // cannot be reused with values resolved under the previous
            // ResourceContext. This replaces only the visual tree, not the
            // process, print runtime or saved printer state.
            var replacementFrame = new Frame();
            if (!replacementFrame.Navigate(typeof(MainPage)))
                throw new InvalidOperationException("Could not reload MainPage after changing language.");
            Window.Current.Content = replacementFrame;

            // The Windows print UI holds PDR separately from the Manager view.
            // Ask it to request resources again after the explicit language
            // override has been persisted for this package.
            await Task.Yield();
            foreach (var queueName in (registry?.Instances ??
                Enumerable.Empty<PrinterInstanceRecord>())
                .Select(printer => printer.QueueName)
                .Where(name => !string.IsNullOrWhiteSpace(name))
                .Distinct(StringComparer.OrdinalIgnoreCase))
            {
                try
                {
                    await PrinterSystemDefaultsService.RequestCapabilitiesRefreshAsync(queueName);
                }
                catch (Exception error)
                {
                    ManagerLogger.Write("PDR language refresh failed for " + queueName
                        + ": " + error.Message);
                }
            }
        }

        private async Task OpenAddPrinterAsync()
        {
            if (registry == null || capabilities == null)
                return;
            await InstallDynamicPrinterAsync();
        }

        private async Task InstallDynamicPrinterAsync()
        {
            if (printerLifecycleRunning)
                return;

            var available = capabilities.Printers
                .OrderBy(model => model.DisplayName)
                .ToList();
            if (available.Count == 0)
            {
                await MessageAsync(
                    LocalizationService.Get("Code.NoPrinterModels"),
                    LocalizationService.Get("Code.TheInstalledPyToneRIPPackageContainsNoCompatiblePrinterModels"));
                return;
            }

            var modelBox = new ComboBox
            {
                Header = LocalizationService.Get("Code.PrinterModel"),
                ItemsSource = available,
                SelectedIndex = 0,
                HorizontalAlignment = HorizontalAlignment.Stretch,
            };
            var nameBox = new TextBox
            {
                Header = LocalizationService.Get("Code.WindowsPrinterName"),
                Text = NextAvailablePrinterName(available[0].DisplayName),
                HorizontalAlignment = HorizontalAlignment.Stretch,
            };
            modelBox.SelectionChanged += (_, __) =>
            {
                var model = modelBox.SelectedItem as PrinterCapability;
                if (model != null)
                    nameBox.Text = NextAvailablePrinterName(model.DisplayName);
            };
            var details = new TextBlock
            {
                Text = LocalizationService.Get("Code.PyToneRIPManagerWillCreateTheWindowsQueueDirectlyAndAssignTheSelectedMod"),
                Foreground = (Windows.UI.Xaml.Media.Brush)Application.Current.Resources["SecondaryTextBrush"],
                TextWrapping = TextWrapping.Wrap,
            };
            var content = new StackPanel { Width = 430, Spacing = 10 };
            var networkChoice = new RadioButton
            {
                Content = LocalizationService.Get("Auto.9B6A601A9FC7.Content"),
                GroupName = "AddPrinterConnection", IsChecked = true,
            };
            var usbChoice = new RadioButton { Content = "USB", GroupName = "AddPrinterConnection" };
            var usbDevices = new ComboBox
            {
                Header = LocalizationService.Get("Usb.Device"), IsEnabled = false,
                HorizontalAlignment = HorizontalAlignment.Stretch,
            };
            var usbStatus = new TextBlock { TextWrapping = TextWrapping.Wrap };
            var refreshUsb = new Button { Content = LocalizationService.Get("Usb.Refresh") };
            var connectionChosen = false;
            var refreshingUsb = false;
            networkChoice.Click += (_, __) => connectionChosen = true;
            usbChoice.Click += (_, __) => connectionChosen = true;
            networkChoice.Checked += (_, __) => usbDevices.IsEnabled = false;
            void SelectUsbModel()
            {
                var detected = usbDevices.SelectedItem as UsbPrinterDescriptor;
                if (usbChoice.IsChecked != true || detected == null) return;
                var matches = available.Where(model => UsbPrinterModelMatcher.Matches(
                    model.Id, model.DisplayName, detected)).ToList();
                if (matches.Count == 1) modelBox.SelectedItem = matches[0];
                usbStatus.Text = detected.ToString();
            }
            usbChoice.Checked += (_, __) => { usbDevices.IsEnabled = true; SelectUsbModel(); };
            usbDevices.SelectionChanged += (_, __) => SelectUsbModel();
            async Task RefreshUsbAsync()
            {
                if (refreshingUsb) return;
                refreshingUsb = true;
                refreshUsb.IsEnabled = false;
                usbStatus.Text = LocalizationService.Get("Usb.Searching");
                try
                {
                    var previous = (usbDevices.SelectedItem as UsbPrinterDescriptor)?.DeviceInstanceId;
                    var detected = await QueueHelperClient.DiscoverUsbPrintersAsync();
                    usbDevices.ItemsSource = detected;
                    var selection = previous != null
                        ? detected.FirstOrDefault(device => device.DeviceInstanceId == previous)
                        : detected.FirstOrDefault(device => available.Any(model => UsbPrinterModelMatcher.Matches(
                            model.Id, model.DisplayName, device)));
                    if (!connectionChosen && selection != null) usbChoice.IsChecked = true;
                    usbDevices.SelectedItem = selection;
                    if (detected.Count == 0) usbStatus.Text = LocalizationService.Get("Usb.None");
                }
                catch (Exception error)
                {
                    ManagerLogger.WriteException("Discovering connected USB printers", error);
                    usbStatus.Text = LocalizationService.Format("Usb.DiscoveryFailed", error.Message);
                }
                finally { refreshUsb.IsEnabled = true; refreshingUsb = false; }
            }
            refreshUsb.Click += async (_, __) => await RefreshUsbAsync();
            content.Children.Add(networkChoice);
            content.Children.Add(usbChoice);
            content.Children.Add(usbDevices);
            content.Children.Add(refreshUsb);
            content.Children.Add(usbStatus);
            content.Children.Add(modelBox);
            content.Children.Add(nameBox);
            content.Children.Add(details);
            var dialog = new ContentDialog
            {
                Title = LocalizationService.Get("Code.AddPrinter"),
                Content = content,
                PrimaryButtonText = LocalizationService.Get("Code.Continue"),
                CloseButtonText = LocalizationService.Get("Code.Cancel"),
            };
            dialog.PrimaryButtonClick += (_, clickArgs) =>
            {
                connectionChosen = true;
                var detected = usbDevices.SelectedItem as UsbPrinterDescriptor;
                var selectedModel = modelBox.SelectedItem as PrinterCapability;
                if (usbChoice.IsChecked == true && (detected == null || selectedModel == null
                    || !UsbPrinterModelMatcher.Matches(selectedModel.Id, selectedModel.DisplayName, detected)))
                {
                    usbStatus.Text = LocalizationService.Get("Usb.SelectMatchingDevice");
                    clickArgs.Cancel = true;
                    return;
                }
                var requestedName = nameBox.Text?.Trim();
                if (string.IsNullOrWhiteSpace(requestedName)
                    || registry.Instances.Any(item => string.Equals(
                        item.QueueName, requestedName, StringComparison.OrdinalIgnoreCase)))
                {
                    details.Text = string.IsNullOrWhiteSpace(requestedName)
                        ? LocalizationService.Get("Code.EnterAWindowsPrinterName")
                        : LocalizationService.Get("Code.APyToneRIPPrinterWithThisNameAlreadyExists");
                    details.Foreground = new Windows.UI.Xaml.Media.SolidColorBrush(
                        Windows.UI.Colors.Firebrick);
                    clickArgs.Cancel = true;
                }
            };
            var addDialogOperation = dialog.ShowAsync();
            _ = RefreshUsbAsync();
            if (await addDialogOperation != ContentDialogResult.Primary)
                return;
            var selectedUsb = usbChoice.IsChecked == true
                ? usbDevices.SelectedItem as UsbPrinterDescriptor : null;
            var selected = modelBox.SelectedItem as PrinterCapability;
            var queueName = nameBox.Text?.Trim();
            if (selected == null || string.IsNullOrWhiteSpace(queueName))
                return;

            var installationStatus = new TextBlock
            {
                Text = LocalizationService.Get("Code.PreparingTheSelectedPrinterModel"),
                TextWrapping = TextWrapping.Wrap,
            };
            var installationProgress = new ProgressBar
            {
                IsIndeterminate = true,
                Height = 8,
                Margin = new Thickness(0, 10, 0, 0),
            };
            var progressContent = new StackPanel { Width = 430, Spacing = 6 };
            progressContent.Children.Add(installationStatus);
            progressContent.Children.Add(installationProgress);
            var progressDialog = new ContentDialog
            {
                Title = LocalizationService.Format("Code.Adding", selected.DisplayName),
                Content = progressContent,
                PrimaryButtonText = LocalizationService.Get("Code.Close"),
                IsPrimaryButtonEnabled = false,
            };
            var installationFinished = false;
            progressDialog.Closing += (_, closingArgs) =>
            {
                if (!installationFinished)
                    closingArgs.Cancel = true;
            };

            printerLifecycleRunning = true;
                StatusText.Text = LocalizationService.Format("Code.AddingProgress", selected.DisplayName);
            var dialogOperation = progressDialog.ShowAsync();
            await Task.Yield();
            PrinterInstanceRecord installedPrinter = null;
            try
            {
                var printerUri = VirtualPrinterManagementService.CreatePrinterUri(selected.Id);
                installationStatus.Text = LocalizationService.Get("Code.CreatingTheWindowsVirtualPrinterWithItsModelPDC");
                await VirtualPrinterManagementService.InstallAsync(
                    selected.Id, queueName, printerUri);

                var queue = await WaitForWindowsQueueAsync(queueName, installationStatus);
                installedPrinter = new PrinterInstanceRecord
                {
                    PrinterUri = printerUri,
                    QueueName = queue?.Name ?? queueName,
                    SystemQueueId = queue?.PortName,
                    ModelId = selected.Id,
                    ModelName = selected.DisplayName,
                    ModelCapability = (await CapabilityCatalog.LoadForModelAsync(selected.Id))
                        .Printers.Single(),
                    ConnectionKind = selectedUsb == null ? "network" : "usb",
                    UsbDeviceId = selectedUsb?.DeviceInstanceId,
                    UsbPort = selectedUsb?.PortName,
                    PhysicalQueueName = selectedUsb?.QueueNames.Count == 1 ? selectedUsb.QueueNames[0] : null,
                    Port = PrinterInstanceStore.RawTcpPort,
                    Configured = false,
                    IsSystemDefault = queue?.IsDefault == true,
                };
                registry.Instances.Add(installedPrinter);
                try
                {
                    await PrinterInstanceStore.WriteAsync(registry);
                }
                catch
                {
                    registry.Instances.Remove(installedPrinter);
                    try
                    {
                        await VirtualPrinterManagementService.RemoveAsync(
                            installedPrinter);
                    }
                    catch { }
                    throw;
                }

                RefreshList();
                await RefreshNetworkStatesAsync();
                var result = LocalizationService.Format("Code.WasAddedThroughVirtualPrinterManagerWithItsOwnModelPDCConfigureTheConnec",
                    selected.DisplayName);
                StatusText.Text = result;
                installationStatus.Text = result;
                installationProgress.IsIndeterminate = false;
                installationProgress.Value = 100;
            }
            catch (Exception error)
            {
                App.WriteStartupException("Virtual printer installation failed", error);
                StatusText.Text = LocalizationService.Get("Code.PrinterWasNotAdded");
                installationStatus.Text = LocalizationService.Format("Code.PrinterWasNotAddedDetails", error.Message);
                installationProgress.IsIndeterminate = false;
                installationProgress.Visibility = Visibility.Collapsed;
            }
            finally
            {
                installationFinished = true;
                printerLifecycleRunning = false;
                progressDialog.IsPrimaryButtonEnabled = true;
                await RefreshPendingWindowsQueueStateAsync();
            }
            await dialogOperation;
            if (installedPrinter != null)
                Frame.Navigate(typeof(PropertiesPage), new PrinterSettingsNavigation
                {
                    Printer = installedPrinter,
                    OpenConnection = !string.Equals(
                        installedPrinter.ConnectionKind,
                        "usb",
                        StringComparison.OrdinalIgnoreCase)
                        || string.IsNullOrWhiteSpace(installedPrinter.UsbDeviceId),
                });
        }

        private async Task<WindowsQueueDescriptor> WaitForWindowsQueueAsync(
            string queueName,
            TextBlock installationStatus)
        {
            const int maximumAttempts = 30;
            for (var attempt = 1; attempt <= maximumAttempts; attempt++)
            {
                var queues = await QueueHelperClient.ListAsync();
                var queue = queues.FirstOrDefault(item => string.Equals(
                    item.Name, queueName, StringComparison.OrdinalIgnoreCase));
                if (queue != null)
                    return queue;
                installationStatus.Text = LocalizationService.Format("Code.WaitingForTheWindowsPrintQueue", attempt, maximumAttempts);
                await Task.Delay(500);
            }
            throw new InvalidOperationException(
                "Windows did not publish the new print queue within 15 seconds.");
        }

        private string NextAvailablePrinterName(string displayName)
        {
            var root = "pyToneRIP: " + displayName;
            if (!registry.Instances.Any(item => string.Equals(
                item.QueueName, root, StringComparison.OrdinalIgnoreCase)))
                return root;
            for (var index = 2; index < 10000; index++)
            {
                var candidate = root + " (" + index + ")";
                if (!registry.Instances.Any(item => string.Equals(
                    item.QueueName, candidate, StringComparison.OrdinalIgnoreCase)))
                    return candidate;
            }
            throw new InvalidOperationException(
                "Could not allocate a unique Windows printer name.");
        }

        private void PrinterItemDoubleTapped(object sender, DoubleTappedRoutedEventArgs args)
        {
            var selected = (args.OriginalSource as FrameworkElement)?.DataContext as PrinterInstanceRecord
                ?? PrinterList.SelectedItem as PrinterInstanceRecord;
            if (selected == null)
                return;
            PrinterList.SelectedItem = selected;
            OpenPreferences(sender, args);
            args.Handled = true;
        }

        private void PrinterListKeyDown(object sender, KeyRoutedEventArgs args)
        {
            if (args.Key != VirtualKey.Enter
                || !(PrinterList.SelectedItem is PrinterInstanceRecord))
                return;
            args.Handled = true;
            OpenPreferences(sender, args);
        }

        private void OpenPreferences(object sender, RoutedEventArgs args)
        {
            var selected = PrinterList.SelectedItem as PrinterInstanceRecord;
            if (selected != null)
                Frame.Navigate(typeof(PropertiesPage), selected);
        }

        private async void RenamePrinter(object sender, RoutedEventArgs args)
        {
            var selected = PrinterList.SelectedItem as PrinterInstanceRecord;
            if (selected == null) return;
            var managedName = TryGetManagedName(selected.QueueName, out var middleName);
            var box = new TextBox
            {
                Header = managedName ? LocalizationService.Get("Code.Name") : LocalizationService.Get("Code.WindowsPrinterName"),
                Text = managedName ? middleName : selected.QueueName,
                Width = 420,
            };
            var dialog = new ContentDialog
            {
                Title = LocalizationService.Get("Code.RenamePrinter"), Content = box,
                PrimaryButtonText = LocalizationService.Get("Code.Save"),
                CloseButtonText = LocalizationService.Get("Code.Cancel")
            };
            if (await dialog.ShowAsync() != ContentDialogResult.Primary || string.IsNullOrWhiteSpace(box.Text)) return;
            var requestedName = box.Text.Trim();
            if (managedName)
                requestedName = "pyToneRIP: " + requestedName;
            if (registry.Instances.Any(item => !ReferenceEquals(item, selected) &&
                string.Equals(item.QueueName, requestedName, StringComparison.OrdinalIgnoreCase)))
            {
                await MessageAsync(LocalizationService.Get("Code.PrinterWasNotRenamed"), LocalizationService.Get("Code.ChooseAUniquePrinterName"));
                return;
            }
            WindowsQueueDescriptor renamedQueue;
            try
            {
                renamedQueue = await QueueHelperClient.RenameAsync(
                    selected.QueueName,
                    selected.SystemQueueId,
                    requestedName);
            }
            catch (Exception error)
            {
                await MessageAsync(LocalizationService.Get("Code.PrinterWasNotRenamed"), error.Message);
                return;
            }
            selected.QueueName = renamedQueue.Name;
            selected.SystemQueueId = renamedQueue.PortName;
            selected.IsSystemDefault = renamedQueue.IsDefault;
            await PrinterInstanceStore.WriteAsync(registry);
            RefreshList();
            StatusText.Text = LocalizationService.Format("Code.WindowsPrinterRenamedToConfigurationRemainsLinkedByPrinterUri", selected.QueueName);
        }

        private async void RemovePrinter(object sender, RoutedEventArgs args)
        {
            var selected = PrinterList.SelectedItem as PrinterInstanceRecord;
            if (selected == null || printerLifecycleRunning)
                return;
            printerLifecycleRunning = true;
            UpdateSelectedPrinterActions();
            try
            {
                await RemoveDynamicPrinterAsync(selected);
            }
            finally
            {
                printerLifecycleRunning = false;
                UpdateSelectedPrinterActions();
                await RefreshPendingWindowsQueueStateAsync();
            }
        }

        private async Task RemoveDynamicPrinterAsync(PrinterInstanceRecord selected)
        {
            if (!await CanRemovePrinterAsync(selected))
                return;
            var dialog = new ContentDialog
            {
                Title = LocalizationService.Get("Code.RemovePrinter"),
                Content = selected.IsWindowsQueueMissing
                    ? LocalizationService.Format("Code.PrinterQueueMissingRemoveConfiguration", selected.QueueName)
                    : LocalizationService.Format("Code.WindowsWillRemoveTheVirtualQueueAndItsSavedPyToneRIPConfigurationTheSign",
                    selected.QueueName, selected.ModelName),
                PrimaryButtonText = LocalizationService.Get("Code.Remove"),
                CloseButtonText = LocalizationService.Get("Code.Cancel"),
            };
            if (await dialog.ShowAsync() != ContentDialogResult.Primary)
                return;
            if (!await CanRemovePrinterAsync(selected))
                return;
            StatusText.Text = LocalizationService.Format("Code.Removing", selected.QueueName);
            try
            {
                var removedFromWindows = await VirtualPrinterManagementService.RemoveAsync(selected);
                await Shared.QueueConfigurationStorage.DeleteAsync(
                    selected.PrinterUri, selected.QueueName);
                var index = registry.Instances.IndexOf(selected);
                registry.Instances.Remove(selected);
                try
                {
                    await PrinterInstanceStore.WriteAsync(registry);
                }
                catch
                {
                    if (index >= 0)
                        registry.Instances.Insert(index, selected);
                    throw;
                }
                await RefreshDefaultPrinterStateAsync();
                RefreshList();
                StatusText.Text = LocalizationService.Format(removedFromWindows
                    ? "Code.AndItsSavedConfigurationWereRemovedTheModelRemainsAvailableInManager"
                    : "Code.PrinterAlreadyRemovedConfigurationDeleted",
                    selected.QueueName);
            }
            catch (Exception error)
            {
                App.WriteStartupException("Virtual printer removal failed", error);
                StatusText.Text = LocalizationService.Get("Code.PrinterWasNotRemoved");
                await MessageAsync(LocalizationService.Get("Code.PrinterWasNotRemovedTitle"), error.Message);
            }
        }

        private async Task<bool> CanRemovePrinterAsync(PrinterInstanceRecord printer)
        {
            try
            {
                var queue = await VirtualPrinterManagementService.FindQueueAsync(printer);
                printer.IsWindowsQueueMissing = queue == null;
                if (queue != null)
                {
                    printer.QueueName = queue.Name;
                    printer.SystemQueueId = queue.PortName;
                }
                if (!await HasActivePrintJobAsync(printer))
                    return true;
            }
            catch (Exception error)
            {
                App.WriteStartupException(
                    "Could not inspect active jobs before virtual printer removal", error);
                StatusText.Text = LocalizationService.Get("Code.PrinterWasNotRemoved");
                await MessageAsync(
                    LocalizationService.Get("Code.PrinterWasNotRemovedTitle"), error.Message);
                return false;
            }
            await ShowActiveJobRemovalBlockedAsync(printer);
            return false;
        }

        private static async Task<bool> HasActivePrintJobAsync(PrinterInstanceRecord printer)
        {
            var activities = await JobProgressReader.ReadActiveJobActivitiesAsync();
            if (activities.Any(activity => string.Equals(
                    activity.QueueName,
                    printer.QueueName,
                    StringComparison.OrdinalIgnoreCase)))
            {
                return true;
            }
            return !printer.IsWindowsQueueMissing
                && await QueueHelperClient.HasCurrentJobAsync(printer.QueueName);
        }

        private async Task ShowActiveJobRemovalBlockedAsync(PrinterInstanceRecord printer)
        {
            StatusText.Text = LocalizationService.Get("Code.RemovePrinterAfterJobsAreCancelled");
            ManagerLogger.Write("Removal blocked because the printer has an active job: "
                + printer.QueueName + ".");
            await MessageAsync(
                LocalizationService.Get("Code.RemovePrinter"),
                LocalizationService.Get("Code.RemovePrinterAfterJobsAreCancelled"));
        }

        private async void Refresh(object sender, RoutedEventArgs args)
        {
            await LoadAsync();
            StatusText.Text = LocalizationService.Get("Code.InstalledPrintersAndStatusWereRefreshed");
        }

        private static async Task<string> TryRefreshCapabilitiesAsync(string queueName)
        {
            try
            {
                await PrinterSystemDefaultsService.RequestCapabilitiesRefreshAsync(queueName);
                return null;
            }
            catch (Exception error)
            {
                App.WriteStartupException(
                    "Windows capability refresh failed for " + queueName, error);
                return LocalizationService.Format("Code.PrinterAssignmentWasSavedButWindowsCapabilityRefreshFailed",
                    error.Message);
            }
        }

        private static async Task<string> TryRefreshCapabilitiesAsync(PrinterInstanceRecord printer)
        {
            if (printer == null)
                return LocalizationService.Get("Code.WindowsCapabilityRefreshFailedPrinterRecordIsMissing");
            try
            {
                await PrinterSystemDefaultsService.RequestCapabilitiesRefreshAsync(
                    printer.QueueName);
                return null;
            }
            catch (Exception error)
            {
                App.WriteStartupException(
                    "Manager-owned Windows capability refresh failed for " + printer.QueueName, error);
                return LocalizationService.Format("Code.WindowsCapabilityRefreshFailedFor",
                    printer.QueueName, error.Message);
            }
        }

        private async void OpenWindowsPrinters(object sender, RoutedEventArgs args) =>
            await Launcher.LaunchUriAsync(new Uri("ms-settings:printers"));

        private async void CollectSupportReport(object sender, RoutedEventArgs args)
        {
            var title = LocalizationService.Get("MainPage.SendLogs.Content");
            var confirmation = new ContentDialog
            {
                Title = title,
                Content = LocalizationService.Get("Code.SupportReportPrompt"),
                PrimaryButtonText = LocalizationService.Get("Code.Continue"),
                CloseButtonText = LocalizationService.Get("Code.Cancel"),
            };
            if (await confirmation.ShowAsync() != ContentDialogResult.Primary)
                return;

            SendLogsButton.IsEnabled = false;
            try
            {
                var managerLog = ManagerLogger.ReadAll();
                var archivePath = await QueueHelperClient.CollectSupportReportAsync(managerLog);
                await MessageAsync(title,
                    LocalizationService.Format("Code.SupportReportCreated", archivePath));
            }
            catch (Exception error)
            {
                ManagerLogger.WriteException("Collecting support report", error);
                await MessageAsync(title, error.Message);
            }
            finally
            {
                SendLogsButton.IsEnabled = true;
            }
        }

        private async void OpenPrintJobs(object sender, RoutedEventArgs args)
        {
            var selected = PrinterList.SelectedItem as PrinterInstanceRecord;
            if (selected == null)
                return;
            OpenJobsButton.IsEnabled = false;
            ManagerLogger.Write("Opening Windows print jobs for: " + selected.QueueName);
            try
            {
                await QueueHelperClient.OpenJobsAsync(selected.QueueName);
                StatusText.Text = LocalizationService.Format("Code.OpenedPrintJobsFor", selected.QueueName);
                ManagerLogger.Write("Windows print jobs window opened successfully.");
            }
            catch (Exception error)
            {
                StatusText.Text = LocalizationService.Get("Code.CouldNotOpenPrintJobs");
                ManagerLogger.WriteException("Opening print jobs for " + selected.QueueName, error);
                await MessageAsync(LocalizationService.Get("Code.PrintJobsCouldNotBeOpened"), error.Message);
            }
            finally
            {
                OpenJobsButton.IsEnabled = PrinterList.SelectedItem is PrinterInstanceRecord;
            }
        }

        private async void CancelCurrentPrintJob(object sender, RoutedEventArgs args)
        {
            var selected = PrinterList.SelectedItem as PrinterInstanceRecord;
            if (selected == null)
                return;
            var item = sender as MenuFlyoutItem;
            if (item != null)
                item.IsEnabled = false;
            try
            {
                var activities = (await JobProgressReader.ReadActiveJobActivitiesAsync())
                    .Where(activity => string.Equals(activity.QueueName,
                        selected.QueueName, StringComparison.OrdinalIgnoreCase)).ToList();
                var running = activities.Where(activity => activity.State != "Queued").ToList();
                var current = activities.Count == 1 ? activities[0]
                    : running.Count == 1 ? running[0] : null;
                var hasWindowsJob = await QueueHelperClient.HasCurrentJobAsync(
                    selected.QueueName);
                if (!hasWindowsJob && current == null)
                    throw new InvalidOperationException("The selected printer has no active print job.");
                if (current != null)
                {
                    var jobs = await Shared.SharedApplicationStorage.Root.GetFolderAsync("jobs");
                    var folder = await jobs.GetFolderAsync(current.JobFolderName);
                    var marker = await folder.CreateFileAsync("cancel.requested",
                        CreationCollisionOption.OpenIfExists);
                    await FileIO.WriteTextAsync(marker,
                        DateTimeOffset.UtcNow.ToString("O") + Environment.NewLine
                        + "Cancelled from pyToneRIP Manager." + Environment.NewLine);
                }
                if (hasWindowsJob)
                    await QueueHelperClient.CancelCurrentJobAsync(selected.QueueName);
                StatusText.Text = LocalizationService.Get("MainPage.Context.CancelJob.Text")
                    + ": " + selected.QueueName;
                ManagerLogger.Write("Current print job cancellation requested for "
                    + selected.QueueName + ".");
                await RefreshTransportActivityAsync();
            }
            catch (Exception error)
            {
                ManagerLogger.WriteException(
                    "Cancelling current print job for " + selected.QueueName, error);
                await MessageAsync(LocalizationService.Get("MainPage.Context.CancelJob.Text"),
                    error.Message);
            }
        }

        private async void SetDefaultPrinter(object sender, RoutedEventArgs args)
        {
            var selected = PrinterList.SelectedItem as PrinterInstanceRecord;
            if (selected == null)
                return;

            SetDefaultPrinterButton.IsEnabled = false;
            ManagerLogger.Write("Setting Windows default printer: " + selected.QueueName);
            try
            {
                var queues = await QueueHelperClient.SetDefaultAsync(selected.QueueName);
                if (!queues.Any(queue => queue.IsDefault && string.Equals(
                    queue.Name, selected.QueueName, StringComparison.OrdinalIgnoreCase)))
                    throw new InvalidOperationException(
                        "Windows did not report the selected printer as its default.");
                ApplyDefaultPrinterState(queues);
                StatusText.Text = LocalizationService.Format("Code.IsNowTheWindowsDefaultPrinter", selected.QueueName);
                ManagerLogger.Write("Windows default printer changed successfully.");
            }
            catch (Exception error)
            {
                StatusText.Text = LocalizationService.Get("Code.CouldNotChangeTheWindowsDefaultPrinter");
                ManagerLogger.WriteException(
                    "Setting Windows default printer to " + selected.QueueName, error);
                await MessageAsync(LocalizationService.Get("Code.DefaultPrinterWasNotChanged"), error.Message);
            }
            finally
            {
                UpdateSelectedPrinterActions();
            }
        }

        private async void OpenAbout(object sender, RoutedEventArgs args)
        {
            var content = new StackPanel { Spacing = 8, Width = 420 };
            content.Children.Add(new Image
            {
                Source = new BitmapImage(new Uri("ms-appx:///Assets/Square150x150Logo.png")),
                Width = 96,
                Height = 96,
                HorizontalAlignment = HorizontalAlignment.Center,
            });
            var packageVersion = Package.Current.Id.Version;
            var version = string.Format(
                System.Globalization.CultureInfo.InvariantCulture,
                "{0}.{1}.{2}.{3}",
                packageVersion.Major,
                packageVersion.Minor,
                packageVersion.Build,
                packageVersion.Revision);
            content.Children.Add(new TextBlock { Text = LocalizationService.Format("Code.Version", version), FontWeight = Windows.UI.Text.FontWeights.SemiBold });
            content.Children.Add(new TextBlock { Text = "Copyright © 2026 Alexander Eichhorn" });
            content.Children.Add(new HyperlinkButton
            {
                Content = "mailto: df11@mail.de",
                NavigateUri = new Uri("mailto:df11@mail.de"),
                HorizontalAlignment = HorizontalAlignment.Left,
                Padding = new Thickness(0),
            });
            content.Children.Add(new TextBlock
            {
                Text = "PyToneRIP is licensed under the GNU General Public License v3.0 only unless otherwise stated.",
                TextWrapping = TextWrapping.Wrap,
            });
            content.Children.Add(new TextBlock
            {
                Text = "Commercial licensing is available separately from the copyright holder.",
                TextWrapping = TextWrapping.Wrap,
            });
            content.Children.Add(new HyperlinkButton
            {
                Content = LocalizationService.Get("Code.SourceGithubComDfoxelf"),
                NavigateUri = new Uri("https://github.com/dfoxelf/pyToneRIP/"),
                HorizontalAlignment = HorizontalAlignment.Left,
                Padding = new Thickness(0),
            });
            await new ContentDialog
            {
                Title = "PyToneRIP",
                Content = content,
                CloseButtonText = LocalizationService.Get("Code.Close"),
            }.ShowAsync();
        }

        private static bool TryGetManagedName(string queueName, out string middleName)
        {
            middleName = null;
            var prefix = "pyToneRip: ";
            if (!string.IsNullOrWhiteSpace(queueName)
                && queueName.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)
                && queueName.Length > prefix.Length)
            {
                middleName = queueName.Substring(prefix.Length);
                return true;
            }
            return false;
        }

        private async Task<string> SynchronizeWindowsQueuesAsync()
        {
            try
            {
                var queues = await QueueHelperClient.ListAsync();
                var changed = false;
                foreach (var queue in queues)
                {
                    var printerUri = TryGetModelPrinterUri(queue.PortName);
                    if (printerUri == null || registry.Instances.Any(item =>
                        string.Equals(item.PrinterUri, printerUri, StringComparison.OrdinalIgnoreCase)))
                        continue;
                    var token = TryGetModelToken(printerUri);
                    if (string.IsNullOrWhiteSpace(token))
                        continue;
                    var model = capabilities.Printers.FirstOrDefault(item =>
                        string.Equals(ModuleToken(item.Id), token, StringComparison.OrdinalIgnoreCase));
                    if (model == null)
                        continue;
                    registry.Instances.Add(new PrinterInstanceRecord
                    {
                        PrinterUri = printerUri,
                        QueueName = queue.Name,
                        SystemQueueId = queue.PortName,
                        ModelId = model.Id,
                        ModelName = model.DisplayName,
                        ModelCapability = (await CapabilityCatalog.LoadForModelAsync(model.Id))
                            .Printers.Single(),
                        ConnectionKind = "network",
                        Port = PrinterInstanceStore.RawTcpPort,
                        Configured = false,
                    });
                    changed = true;
                }
                foreach (var record in registry.Instances)
                {
                    record.IsSystemDefault = false;
                    var queue = VirtualPrinterManagementService.FindQueue(record, queues);
                    record.IsWindowsQueueMissing = queue == null;
                    if (queue == null)
                        continue;
                    if (!string.Equals(record.QueueName, queue.Name, StringComparison.Ordinal) ||
                        !string.Equals(record.SystemQueueId, queue.PortName, StringComparison.Ordinal))
                    {
                        record.QueueName = queue.Name;
                        record.SystemQueueId = queue.PortName;
                        changed = true;
                    }
                    record.IsSystemDefault = queue.IsDefault;
                }
                if (changed)
                    await PrinterInstanceStore.WriteAsync(registry);
                return await RemoveMissingWindowsQueuesAsync();
            }
            catch (Exception error)
            {
                App.WriteStartupException("Windows queue synchronization failed", error);
                return LocalizationService.Format("Code.PrinterListLoadedButWindowsQueueSynchronizationFailed",
                    error.Message);
            }
        }

        private void ApplyDefaultPrinterState(
            System.Collections.Generic.IReadOnlyList<WindowsQueueDescriptor> queues)
        {
            if (registry?.Instances == null || queues == null)
                return;
            foreach (var record in registry.Instances)
            {
                var queue = VirtualPrinterManagementService.FindQueue(record, queues);
                record.IsWindowsQueueMissing = queue == null;
                record.IsSystemDefault = queue?.IsDefault == true;
            }
        }

        private async Task<string> RemoveMissingWindowsQueuesAsync()
        {
            if (printerLifecycleRunning || registry?.Instances == null)
                return null;
            var missing = registry.Instances.Where(item => item.IsWindowsQueueMissing).ToList();
            if (missing.Count == 0)
                return null;

            printerLifecycleRunning = true;
            UpdateSelectedPrinterActions();
            var listChanged = false;
            string notification = null;
            var selection = PrinterList.SelectedItem;
            try
            {
                // A Removed notification is only a reason to reconcile. Confirm
                // absence in a fresh, successful queue enumeration before cleanup.
                var queues = await QueueHelperClient.ListAsync();
                foreach (var printer in missing)
                {
                    if (VirtualPrinterManagementService.FindQueue(printer, queues) != null)
                    {
                        printer.IsWindowsQueueMissing = false;
                        continue;
                    }
                    // Keep configuration available to an already-running RIP.
                    // The existing progress timer retries when its activity ends.
                    if (await HasActivePrintJobAsync(printer))
                        continue;

                    var configuration = await Shared.QueueConfigurationStorage.ReadAsync(
                        printer.PrinterUri, printer.QueueName, allowLegacyFallback: false);
                    await Shared.QueueConfigurationStorage.DeleteAsync(
                        printer.PrinterUri, printer.QueueName);
                    var index = registry.Instances.IndexOf(printer);
                    registry.Instances.RemoveAt(index);
                    try
                    {
                        await PrinterInstanceStore.WriteAsync(registry);
                    }
                    catch
                    {
                        registry.Instances.Insert(index, printer);
                        if (configuration != null)
                            await Shared.QueueConfigurationStorage.WriteAsync(
                                printer.PrinterUri, printer.QueueName, configuration);
                        throw;
                    }
                    listChanged = true;
                    ManagerLogger.Write("Removed saved configuration for absent Windows queue: "
                        + printer.QueueName + "; uri=" + printer.PrinterUri);
                    notification = LocalizationService.Format(
                        "Code.PrinterAlreadyRemovedConfigurationDeleted", printer.QueueName);
                    StatusText.Text = notification;
                }
            }
            finally
            {
                printerLifecycleRunning = false;
                if (listChanged)
                {
                    RefreshList();
                    if (selection is PrinterInstanceRecord previous
                        && registry.Instances.Contains(previous))
                        PrinterList.SelectedItem = previous;
                }
                UpdateSelectedPrinterActions();
            }
            return notification;
        }

        private static string TryGetModelPrinterUri(string portName) =>
            VirtualPrinterManagementService.GetPrinterUri(portName);

        private static string TryGetModelToken(string printerUri)
        {
            const string prefix = "pytone-rip:";
            if (string.IsNullOrWhiteSpace(printerUri)
                || !printerUri.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                return null;
            var remainder = printerUri.Substring(prefix.Length).TrimStart('/');
            var separator = remainder.IndexOfAny(new[] { '/', '_', '?' });
            var token = separator < 0 ? remainder : remainder.Substring(0, separator);
            return string.IsNullOrWhiteSpace(token) ? null : token;
        }

        private static string ModuleToken(string modelId) =>
            new string((modelId ?? "").Where(char.IsLetterOrDigit).ToArray()).ToLowerInvariant();

        private static async Task MessageAsync(string title, string message) =>
            await new ContentDialog
            {
                Title = title, Content = message,
                CloseButtonText = LocalizationService.Get("Code.OK")
            }.ShowAsync();

        private static bool IsValidIpv4(string value)
        {
            var parts = (value ?? "").Split('.');
            return parts.Length == 4 && parts.All(part =>
                part.Length > 0 &&
                part.All(char.IsDigit) &&
                byte.TryParse(part, out byte _));
        }
    }
}
