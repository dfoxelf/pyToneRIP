using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using pyToneRIP.Models;
using pyToneRIP.Services;
using pyToneRIP.Shared;
using Windows.Foundation;
using Windows.Graphics.Printing.PrintSupport;
using Windows.Graphics.Printing.PrintTicket;
using Windows.Storage.Pickers;
using Windows.UI;
using Windows.UI.Xaml;
using Windows.UI.Xaml.Controls;
using Windows.UI.Xaml.Media;
using Windows.UI.Xaml.Navigation;
using Windows.UI.Xaml.Shapes;

namespace pyToneRIP
{
    public sealed partial class PropertiesPage : Page
    {
        private readonly PrintSupportSettingsUISession session;
        private readonly Deferral activationDeferral;
        private CapabilityExport capabilities;
        private PyToneRipConfiguration originalConfiguration;
        private PrinterInstanceRecord instance;
        private string printerUri;
        private string printerName;
        private readonly Dictionary<string, string> routes = new Dictionary<string, string>();
        private readonly Dictionary<string, ComboBox> deviceSettingBoxes =
            new Dictionary<string, ComboBox>();
        private Slider[][] blendSliders;
        private TextBlock[][] blendValues;
        private bool updatingBlend;
        private bool updatingCurves;
        private bool updatingLogicalInks;
        private bool updatingPrinterOptions;
        private bool restoringInputSource;
        private bool loaded;
        private bool connectionReady;
        private bool connectionRefreshRunning;
        private bool usbRefreshRunning;
        private bool localizationStarted;
        private bool applyRunning;
        private IReadOnlyList<DeferredLocalization.Entry> deferredStrings;

        private sealed class LocalizedValue
        {
            internal LocalizedValue(string value) { Value = value; }
            internal string Value { get; }
            public override string ToString() => LocalizationService.Get("Choice." + Value);
        }

        public PropertiesPage() : this(null, null) { }

        public PropertiesPage(PrintSupportSettingsUISession session, Deferral deferral = null)
        {
            App.WriteStartupStage("PropertiesPage constructor entered");
            this.session = session;
            activationDeferral = deferral;
            try
            {
                App.WriteStartupStage("PropertiesPage.InitializeComponent begin");
                var capturedStrings = DeferredLocalization.BeginCapture();
                try
                {
                    InitializeComponent();
                }
                finally
                {
                    DeferredLocalization.EndCapture(capturedStrings);
                }
                deferredStrings = capturedStrings;
                App.WriteStartupStage("PropertiesPage.InitializeComponent complete");
                TestConnectionButton.Content = LocalizationService.Get("Code.Test");
                ReadStatusButton.Content = LocalizationService.Get("Code.ReadPrinterStatus");
                InkTitleText.Text = LocalizationService.Get("Code.InkLevels");
                InkReadoutText.Text = LocalizationService.Get("Code.InkUnavailable");
                ConnectionStatusText.Text = LocalizationService.Get("Choice.Unknown");
                UsbDeviceBox.Header = LocalizationService.Get("Usb.Device");
#if USB_PHYSICAL_QUEUE_FALLBACK
                UsbPortBox.Header = LocalizationService.Get("Usb.PhysicalQueue");
#endif
                RefreshUsbButton.Content = LocalizationService.Get("Usb.Refresh");
                PortBox.Width = 80;
                PortBox.MinWidth = 0;
                PortBox.HorizontalAlignment = HorizontalAlignment.Left;
                InitializeBlendControls();
                if (session != null)
                {
                    printerUri = session.SessionInfo?.Printer?.PrinterUri?.ToString();
                    printerName = session.SessionInfo?.Printer?.PrinterName;
                }
            }
            catch (Exception error)
            {
                App.WriteStartupException("PropertiesPage.InitializeComponent failed", error);
                throw;
            }
            Loaded += async (_, __) =>
            {
                if (!localizationStarted)
                {
                    localizationStarted = true;
                    _ = ApplyDeferredLocalizationAsync();
                }
                // Loaded precedes the first presented frame. Let Windows draw
                // the settings controls before touching the model catalog.
                await System.Threading.Tasks.Task.Delay(16);
                await LoadAsync();
            };
            App.WriteStartupStage("PropertiesPage constructor complete");
        }

        private async System.Threading.Tasks.Task ApplyDeferredLocalizationAsync()
        {
            try
            {
                App.WriteStartupStage("PropertiesPage deferred localization begin");
                await DeferredLocalization.ApplyAsync(deferredStrings);
                App.WriteStartupStage("PropertiesPage deferred localization complete");
            }
            catch (Exception error)
            {
                App.WriteStartupException("PropertiesPage deferred localization failed", error);
            }
        }

        protected override void OnNavigatedTo(NavigationEventArgs args)
        {
            base.OnNavigatedTo(args);
            var navigation = args.Parameter as PrinterSettingsNavigation;
            instance = navigation?.Printer ?? args.Parameter as PrinterInstanceRecord;
            if (instance != null)
            {
                printerUri = instance.PrinterUri;
                printerName = instance.QueueName;
                if (navigation?.OpenConnection == true)
                    PreferencesPivot.SelectedItem = ConnectionPivot;
            }
        }

        private void InitializeBlendControls()
        {
            blendSliders = new[]
            {
                new[] { AllTone1, AllTone2, AllTone3 },
                new[] { Highlights1, Highlights2, Highlights3 },
                new[] { Midtones1, Midtones2, Midtones3 },
                new[] { Shadows1, Shadows2, Shadows3 },
            };
            blendValues = new[]
            {
                new[] { AllTone1Value, AllTone2Value, AllTone3Value },
                new[] { Highlights1Value, Highlights2Value, Highlights3Value },
                new[] { Midtones1Value, Midtones2Value, Midtones3Value },
                new[] { Shadows1Value, Shadows2Value, Shadows3Value },
            };
            for (var group = 0; group < blendSliders.Length; group++)
                SetBlendGroup(group, new[] { 100, 0, 0 });
        }

        private async System.Threading.Tasks.Task LoadAsync()
        {
            if (loaded) return;
            loaded = true;
            try
            {
                if (session != null && instance == null)
                {
                    var registry = await PrinterInstanceStore.ReadAsync();
                    instance = registry.Instances.FirstOrDefault(item =>
                        (!string.IsNullOrWhiteSpace(printerUri) && string.Equals(
                            item.PrinterUri, printerUri, StringComparison.OrdinalIgnoreCase))
                        || (!string.IsNullOrWhiteSpace(printerName) && string.Equals(
                            item.QueueName, printerName, StringComparison.OrdinalIgnoreCase)));
                }
                if (instance == null)
                {
                    throw new InvalidOperationException(
                        "The managed printer queue is not registered in pyToneRIP Manager.");
                }
                capabilities = await CapabilityCatalog.LoadForModelAsync(
                    instance.ModelId, instance.ModelCapability);
                var assignedModel = capabilities.Printers.FirstOrDefault(
                    item => item.Id == instance.ModelId);
                if (assignedModel == null)
                    throw new InvalidOperationException(
                        "The printer model assigned to this slot is absent from the registry.");
                if (session != null)
                {
                    App.WriteStartupStage(
                        "Print settings session: launch=" + session.LaunchKind
                        + "; queue=" + (printerName ?? ""));
                }
                PrinterBox.ItemsSource = new[] { assignedModel };
                PrinterBox.IsEnabled = false;
                DitherBox.ItemsSource = assignedModel.CompatibleDithers(capabilities.Dither);
                DirectionBox.ItemsSource = LocalizedValues(capabilities.Layout.Directions);
                SizingBox.ItemsSource = LocalizedValues(capabilities.Layout.SizingModes);
                OrientationBox.ItemsSource = LocalizedValues(capabilities.Layout.Orientations);
                PlacementBox.ItemsSource = LocalizedValues(capabilities.Layout.PlacementModes);
                ResamplingBox.ItemsSource = LocalizedValues(capabilities.Layout.Resampling);
                GammaSlider.Minimum = capabilities.Adjustments.Gamma.Minimum;
                GammaSlider.Maximum = capabilities.Adjustments.Gamma.Maximum;
                InkLimitSlider.Minimum = capabilities.Adjustments.InkLimit.Minimum;
                InkLimitSlider.Maximum = capabilities.Adjustments.InkLimit.Maximum;
                WorkflowPrintTicket ticketForUi = session?.SessionPrintTicket;
                var rasterChoices = PrintTicketSynchronization.ResolutionChoices(
                    ticketForUi);
                if (assignedModel.Id == "SureColorP900RIP")
                    foreach (var choice in rasterChoices.Where(item => item.X == 720 && item.Y == 720))
                        choice.DetailLabel = LocalizationService.Get("Printer.FinestDetail");
                RasterResolutionBox.ItemsSource = rasterChoices;
                RasterResolutionBox.SelectedItem = rasterChoices.FirstOrDefault(item =>
                    item.X == 600 && item.Y == 600) ?? rasterChoices.FirstOrDefault();
                // The Manager page can apply a queue default without a PSA
                // session ticket. Raster choices match the assigned model PDC.
                RasterResolutionBox.IsEnabled = true;
                RasterResolutionBox.Opacity = 1;

                if (string.IsNullOrWhiteSpace(printerUri) && string.IsNullOrWhiteSpace(printerName))
                    throw new InvalidOperationException("Printer queue identity is unavailable.");
                QueueIdentityText.Text = (printerName ?? LocalizationService.Get("Code.PyToneRIPQueue"))
                    + "  •  " + (printerUri ?? LocalizationService.Get("Code.NameFallback"));

                var saved = await ConfigurationCodec.ReadStoredAsync(printerUri, printerName);
                if (!string.IsNullOrWhiteSpace(saved) && saved != "{}")
                    originalConfiguration = ConfigurationCodec.Deserialize(saved);

                SelectPrinter(originalConfiguration?.Printer?.Model ?? instance?.ModelId);
                SelectString(DirectionBox, originalConfiguration?.Printer?.Direction);
                SelectOption(DitherBox, assignedModel.CompatibleDitherSelection(
                    originalConfiguration?.Adjustments?.Dither));
                SelectString(SizingBox, originalConfiguration?.Layout?.Sizing);
                SelectString(OrientationBox, originalConfiguration?.Layout?.Orientation);
                SelectString(PlacementBox, originalConfiguration?.Layout?.Placement);
                SelectString(ResamplingBox, originalConfiguration?.Layout?.Resampling);
                LoadConfigurationValues(originalConfiguration);
                var connection = originalConfiguration?.Connection;
                var useUsb = (connection?.Kind ?? instance.ConnectionKind) == "usb";
                NetworkRadio.IsChecked = !useUsb;
                UsbRadio.IsChecked = useUsb;
                HostBox.Text = connection?.Host ?? instance.Host ?? "";
                if (useUsb) await RefreshUsbDevicesAsync();
                UpdateRipLayoutControls();
                ApplyStoredRasterResolution(originalConfiguration?.Printer);
                if (ticketForUi != null)
                    await LoadSessionPrintTicketSelectionAsync(ticketForUi);
                try { await UpdateLogicalInksFromCurveAsync(false); }
                catch (Exception curveError)
                {
                    // Keep Preferences editable when a previously selected
                    // profile was removed; Apply performs the strict check.
                    App.WriteStartupException("QUAD channel preview failed", curveError);
                }
                ApplyButton.IsEnabled = true;
                ValidationSummary.Text = session == null
                    ? LocalizationService.Get("Code.ConfigurationIsStoredForThisPrinterSlotWindowsQueueRenameIsManagedInWind")
                    : LocalizationService.Get("Code.PreferencesLoadedForThisQueueURIApplyUpdatesOnlyThisPrinter");
                connectionReady = true;
                if (PreferencesPivot.SelectedItem == ConnectionPivot)
                    _ = RefreshConnectionAsync();
            }
            catch (Exception error)
            {
                App.WriteStartupException("PropertiesPage.LoadAsync failed", error);
                ValidationSummary.Text = LocalizationService.Format("Code.CapabilityLoadFailed", error.Message);
            }
        }

        private async System.Threading.Tasks.Task LoadSessionPrintTicketSelectionAsync(
            WorkflowPrintTicket ticket)
        {
            var configuration = originalConfiguration ?? new PyToneRipConfiguration
            {
                Printer = new PrinterSelection
                {
                    Model = Required<PrinterCapability>(PrinterBox, "registered printer").Id,
                    Page = RequiredOption(PageBox, "page").Id,
                    Media = RequiredOption(MediaBox, "media").Id,
                    InputSource = RequiredOption(InputSourceBox, "input source").Id,
                    RasterResolutionX = Required<RasterResolutionChoice>(
                        RasterResolutionBox, "Windows raster resolution").X,
                    RasterResolutionY = Required<RasterResolutionChoice>(
                        RasterResolutionBox, "Windows raster resolution").Y,
                },
                Layout = new LayoutSelection
                {
                    Orientation = RequiredString(OrientationBox, "orientation"),
                },
            };
            var capabilitiesJson = await CapabilityCatalog.ReadGeneratedCapabilitiesJsonAsync();
            var projected = PrintTicketSynchronization.OverlayConfiguration(
                ticket, ConfigurationCodec.Serialize(configuration), capabilitiesJson);
            if (session.LaunchKind == SettingsLaunchKind.UserDefaultPrintTicket)
            {
                try
                {
                    var legacyDefaults = await QueueHelperClient.ReadLegacyUserDefaultsAsync(
                        printerName);
                    projected = PrintTicketSynchronization.OverlayLegacyUserDefaults(
                        projected, capabilitiesJson, legacyDefaults);
                    App.WriteStartupStage(
                        "Legacy per-user DEVMODE loaded: page="
                        + legacyDefaults.PageWidthMm.ToString("0.###", CultureInfo.InvariantCulture)
                        + "x" + legacyDefaults.PageHeightMm.ToString("0.###", CultureInfo.InvariantCulture)
                        + "mm; borderless=" + (legacyDefaults.Borderless?.ToString() ?? "unknown"));
                }
                catch (Exception error)
                {
                    App.WriteStartupException(
                        "Could not apply legacy per-user defaults; using the session PrintTicket",
                        error);
                }
            }
            originalConfiguration = ConfigurationCodec.Deserialize(projected);
            originalConfiguration.Schema = "pytone-rip.configuration";
            originalConfiguration.Version = 1;

            SelectOption(MediaBox, originalConfiguration.Printer.Media);
            SelectOption(PageBox, originalConfiguration.Printer.Page);
            SelectOption(InputSourceBox, originalConfiguration.Printer.InputSource);
            SelectString(OrientationBox, originalConfiguration.Layout.Orientation);
            ApplyStoredRasterResolution(originalConfiguration.Printer);
            App.WriteStartupStage(
                "Print settings ticket loaded: launch=" + session.LaunchKind
                + "; page=" + originalConfiguration.Printer.Page
                + "; media=" + originalConfiguration.Printer.Media
                + "; orientation=" + originalConfiguration.Layout.Orientation
                + "; sessionOrientation=" + PrintTicketSynchronization.Read(ticket).OrientationOptionName
                + "; resolution=" + originalConfiguration.Printer.RasterResolutionX
                + "x" + originalConfiguration.Printer.RasterResolutionY);
        }

        private void LoadConfigurationValues(PyToneRipConfiguration value)
        {
            if (value == null)
            {
                HostBox.Text = instance?.Host ?? "";
                PortBox.Text = PrinterInstanceStore.RawTcpPort.ToString(CultureInfo.InvariantCulture);
                var printer = PrinterBox.SelectedItem as PrinterCapability;
                var defaults = DefaultRoutes(printer);
                routes.Clear();
                foreach (var item in defaults)
                    routes[item.Key] = item.Value;
                LogicalInksBox.Text = string.Join(",", defaults.Keys);
                RebuildRoutes();
                SetSplitToneVisibility(false);
                UpdateCurveActivity();
                return;
            }
            Curve1Box.Text = value.Profiles?.Curves?.ElementAtOrDefault(0) ?? "";
            Curve2Box.Text = value.Profiles?.Curves?.ElementAtOrDefault(1) ?? "";
            Curve3Box.Text = value.Profiles?.Curves?.ElementAtOrDefault(2) ?? "";
            var active = ActiveCurveCount();
            if (active > 0)
            {
                SetBlendGroup(0, CyclicBlendWeights.Normalize(value.Profiles?.Blend, active));
                SetBlendGroup(1, CyclicBlendWeights.Normalize(value.Profiles?.Highlights, active));
                SetBlendGroup(2, CyclicBlendWeights.Normalize(value.Profiles?.Midtones, active));
                SetBlendGroup(3, CyclicBlendWeights.Normalize(value.Profiles?.Shadows, active));
                SplitToneSwitch.IsOn = value.Profiles?.SplitTone == true;
            }
            else
            {
                // Calibration intentionally has no .quad curves. Do not pass
                // zero to CyclicBlendWeights.Normalize: it is a 1–3 curve
                // control and previously aborted loading before IP/routing.
                SetBlendGroup(0, new[] { 100, 0, 0 });
                SetBlendGroup(1, new[] { 100, 0, 0 });
                SetBlendGroup(2, new[] { 100, 0, 0 });
                SetBlendGroup(3, new[] { 100, 0, 0 });
                SplitToneSwitch.IsOn = false;
            }
            GammaSlider.Value = value.Adjustments?.Gamma ?? 0;
            InkLimitSlider.Value = value.Adjustments?.InkLimit ?? 0;
            CalibrationEnabledBox.IsChecked = value.Calibration?.Enabled == true;
            CalibrationInkLimitSlider.Value = value.Calibration?.InkLimit ?? 100;
            SelectCalibrationMode(value.Calibration?.Mode ?? "rgb_selectors");
            var configuredInks = (value.Channels?.LogicalInks
                ?? new List<string> { "K" })
                .Select(item => (item ?? "").Trim())
                .Where(item => item.Length > 0)
                .Distinct()
                .ToList();
            var configuredRoutes = value.Channels?.Routing == null
                ? new Dictionary<string, string>()
                : new Dictionary<string, string>(value.Channels.Routing);
            MigrateBlackRoutes(
                PrinterBox.SelectedItem as PrinterCapability,
                value.Printer?.InkConfiguration,
                configuredInks,
                configuredRoutes);
            LogicalInksBox.Text = string.Join(",", configuredInks);
            routes.Clear();
            foreach (var item in configuredRoutes) routes[item.Key] = item.Value;
            WidthBox.Text = Number(value.Layout?.WidthMm);
            HeightBox.Text = Number(value.Layout?.HeightMm);
            ScaleBox.Text = Number(value.Layout?.ScalePercent);
            XBox.Text = Number(value.Layout?.XMm);
            YBox.Text = Number(value.Layout?.YMm);
            ApplyRipLayoutBox.IsChecked = value.Layout?.ApplyRipLayout == true;
            var connection = value.Connection;
            NetworkRadio.IsChecked = connection?.Kind != "usb";
            HostBox.Text = connection?.Host ?? instance?.Host ?? "";
            PortBox.Text = PrinterInstanceStore.RawTcpPort.ToString(CultureInfo.InvariantCulture);
            AllowUnsupportedPpdCombinationsBox.IsChecked =
                value.Printer?.AllowUnsupportedPpdCombinations == true;
            RebuildRoutes();
            SetSplitToneVisibility(SplitToneSwitch.IsOn);
            UpdateCurveActivity();
            UpdateCalibrationControls();
        }

        private void SelectPrinter(string id)
        {
            PrinterBox.SelectedItem = capabilities.Printers.FirstOrDefault(item => item.Id == id)
                ?? capabilities.Printers.FirstOrDefault();
        }

        private void PrinterChanged(object sender, SelectionChangedEventArgs args)
        {
            var printer = PrinterBox.SelectedItem as PrinterCapability;
            if (printer == null) return;
            restoringInputSource = true;
            try
            {
                MediaBox.ItemsSource = printer.Media;
                PageBox.ItemsSource = printer.Pages;
                InputSourceBox.ItemsSource = printer.InputSources;
                InputSourcePanel.Visibility = printer.InputSources.Count > 1
                    ? Visibility.Visible : Visibility.Collapsed;
                var savedMedia = originalConfiguration?.Printer?.Media;
                if (!string.IsNullOrWhiteSpace(savedMedia)
                    && printer.MediaAliases != null
                    && printer.MediaAliases.TryGetValue(savedMedia, out string canonicalMedia))
                    savedMedia = canonicalMedia;
                SelectOption(MediaBox, savedMedia);
                UpdateModeOptions(originalConfiguration?.Printer?.Mode);
                SelectOption(PageBox, originalConfiguration?.Printer?.Page);
                SelectOption(InputSourceBox, originalConfiguration?.Printer?.InputSource);
            }
            finally { restoringInputSource = false; }
            RebuildDeviceSettings(printer);
            RebuildInkConfigurations(printer);
            RebuildRoutes();
            RebuildMaintenance(printer);
        }

        private void RebuildInkConfigurations(PrinterCapability printer)
        {
            var configurations = printer?.InkConfigurations
                ?? new List<InkConfigurationCapability>();
            InkConfigurationPanel.Visibility = configurations.Count > 1
                ? Visibility.Visible
                : Visibility.Collapsed;
            InkConfigurationBox.ItemsSource = configurations;
            var selectedId = originalConfiguration?.Printer?.Model == printer?.Id
                ? originalConfiguration.Printer.InkConfiguration
                : null;
            if (string.IsNullOrWhiteSpace(selectedId))
                selectedId = printer?.DefaultInkConfiguration;
            InkConfigurationBox.SelectedItem = configurations.FirstOrDefault(item =>
                string.Equals(item.Id, selectedId, StringComparison.OrdinalIgnoreCase))
                ?? configurations.FirstOrDefault();
        }

        private string SelectedInkConfiguration() =>
            (InkConfigurationBox?.SelectedItem as InkConfigurationCapability)?.Id;

        private static Dictionary<string, string> DefaultRoutes(PrinterCapability printer)
        {
            var result = new Dictionary<string, string>();
            var paths = printer?.PhysicalPaths ?? new List<PhysicalPathCapability>();
            for (var index = 0; index < paths.Count; index++)
            {
                var path = paths[index];
                var alternativeInks = (path.ConfigurationRoles
                    ?? new List<PhysicalPathConfigurationRole>())
                    .Select(item => InkLabels.ShortName(item.Role))
                    .Where(item => !string.IsNullOrWhiteSpace(item))
                    .Distinct(StringComparer.OrdinalIgnoreCase)
                    .ToList();
                if (alternativeInks.Count > 1)
                {
                    // PK and MK are two physical states of the same P0 path.
                    // QUAD exposes that path as one logical curve channel K.
                    if (alternativeInks.Contains("PK", StringComparer.OrdinalIgnoreCase)
                        && alternativeInks.Contains("MK", StringComparer.OrdinalIgnoreCase))
                    {
                        result["K"] = path.Id;
                        continue;
                    }
                    foreach (var alternative in alternativeInks)
                        result[alternative.ToUpperInvariant()] = path.Id;
                    continue;
                }
                // The left side is a logical QTR curve; the combo box on the
                // right describes a physical cartridge/head path. They may
                // intentionally have different names (for example LK -> PBK).
                var logical = (path.DefaultLogicalInk
                    ?? path.ShortName
                    ?? InkLabels.ShortName(path.FactoryRole)
                    ?? "")
                    .Trim().ToUpperInvariant();
                // Shared PK/MK states were handled above. An independent MK
                // path (P900) has its own QUAD curve and must remain separate.
                if (logical == "PK" || logical == "PBK"
                    || logical == "BK" || logical == "PK/MK" || logical == "MK/PK")
                    logical = "K";
                // Logical QUAD names differ from vivid cartridge labels.
                if (logical == "VM") logical = "M";
                if (logical == "VLM") logical = "LM";
                if (logical == "O") logical = "OR";
                if (logical == "G") logical = "GR";
                logical = new string(logical.Where(char.IsLetterOrDigit).ToArray());
                if (string.IsNullOrWhiteSpace(logical)) logical = "INK" + (index + 1);
                // Repeated K labels do not invent K2. An explicitly named
                // independent MK curve is retained above.
                if (logical == "K" && result.ContainsKey("K")) continue;
                var unique = logical;
                var suffix = 2;
                while (result.ContainsKey(unique)) unique = logical + suffix++;
                result[unique] = path.Id;
            }
            return result;
        }

        private static void MigrateBlackRoutes(
            PrinterCapability printer,
            string inkConfiguration,
            List<string> logicalInks,
            Dictionary<string, string> routing)
        {
            if (printer == null || logicalInks == null || routing == null) return;
            var blackPaths = new HashSet<string>((printer.PhysicalPaths
                ?? new List<PhysicalPathCapability>())
                .Where(item => new[] { "K", "PK", "MK", "PBK", "BK", "PK/MK" }
                    .Contains((item.ShortName ?? "").ToUpperInvariant())
                    || new[] { "K", "BLACK", "PHOTO_BLACK", "MATTE_BLACK", "PHOTOBLACK", "MATTEBLACK" }
                    .Contains((item.FactoryRole ?? "").ToUpperInvariant()))
                .Select(item => item.Id));
            // Separate black banks can carry independent K and MK curves.
            if (blackPaths.Count > 1) return;
            var aliasIndexes = logicalInks
                .Select((item, index) => new { Ink = item, Index = index })
                .Where(item => new[] { "PK", "MK", "PBK", "BK" }.Contains(item.Ink)
                    && routing.TryGetValue(item.Ink, out string aliasPath) && blackPaths.Contains(aliasPath))
                .ToList();
            if (aliasIndexes.Count == 0) return;

            string path = null;
            if (!routing.TryGetValue("K", out path))
            {
                var active = (printer.InkConfigurations
                    ?? new List<InkConfigurationCapability>())
                    .FirstOrDefault(item => string.Equals(
                        item.Id, inkConfiguration, StringComparison.OrdinalIgnoreCase))
                    ?.ActiveLogicalInk;
                if (string.IsNullOrWhiteSpace(active)
                    || !routing.TryGetValue(active.ToUpperInvariant(), out path))
                    path = aliasIndexes
                        .Select(item => routing.TryGetValue(item.Ink, out string candidate)
                            ? candidate : null)
                        .FirstOrDefault(candidate => !string.IsNullOrWhiteSpace(candidate));
            }

            var insertionIndex = aliasIndexes.Min(item => item.Index);
            logicalInks.RemoveAll(item => aliasIndexes.Any(alias => alias.Ink == item));
            if (!logicalInks.Contains("K"))
                logicalInks.Insert(Math.Min(insertionIndex, logicalInks.Count), "K");
            foreach (var alias in aliasIndexes) routing.Remove(alias.Ink);
            if (!string.IsNullOrWhiteSpace(path)) routing["K"] = path;
        }

        private void InkConfigurationChanged(object sender, SelectionChangedEventArgs args)
        {
            RebuildRoutes();
        }

        private void RebuildDeviceSettings(PrinterCapability printer)
        {
            deviceSettingBoxes.Clear();
            DeviceSettingsFields.Children.Clear();
            DeviceSettingsFields.RowDefinitions.Clear();
            DeviceSettingsFields.ColumnDefinitions.Clear();
            DeviceSettingsFields.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            DeviceSettingsFields.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var settings = printer?.DeviceSettings ?? new List<DeviceSettingCapability>();
            DeviceSettingsPanel.Visibility = settings.Count == 0
                ? Visibility.Collapsed
                : Visibility.Visible;
            for (var index = 0; index < settings.Count; index++)
            {
                var setting = settings[index];
                foreach (var choice in setting.Choices ?? new List<DeviceSettingChoice>())
                    choice.LocalizedDisplayName = LocalizationService.DeviceSettingChoiceName(setting, choice);
                var row = index / 2;
                var column = index % 2;
                if (DeviceSettingsFields.RowDefinitions.Count <= row)
                    DeviceSettingsFields.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var field = new StackPanel
                {
                    Margin = new Thickness(column == 0 ? 0 : 16, 0, 12, 8),
                };
                field.Children.Add(new TextBlock
                {
                    Text = LocalizationService.DeviceSettingName(setting),
                    Margin = new Thickness(0, 5, 8, 0),
                    VerticalAlignment = VerticalAlignment.Center,
                });
                var editor = new ComboBox
                {
                    ItemsSource = setting.Choices ?? new List<DeviceSettingChoice>(),
                };
                var savedChoice = setting.DefaultChoice;
                if (originalConfiguration?.Printer?.Model == printer.Id
                    && originalConfiguration.Printer.DeviceSettings != null
                    && originalConfiguration.Printer.DeviceSettings.TryGetValue(
                        setting.Id, out string configuredChoice))
                    savedChoice = configuredChoice;
                editor.SelectedItem = (setting.Choices ?? new List<DeviceSettingChoice>())
                    .FirstOrDefault(item => string.Equals(
                        item.Id, savedChoice, StringComparison.OrdinalIgnoreCase))
                    ?? setting.Choices?.FirstOrDefault();
                field.Children.Add(editor);
                Grid.SetRow(field, row);
                Grid.SetColumn(field, column);
                DeviceSettingsFields.Children.Add(field);
                deviceSettingBoxes[setting.Id] = editor;
            }
        }

        private Dictionary<string, string> SelectedDeviceSettings()
        {
            var result = new Dictionary<string, string>();
            foreach (var item in deviceSettingBoxes)
            {
                var selected = item.Value.SelectedItem as DeviceSettingChoice;
                if (selected == null)
                    throw new InvalidOperationException(
                        "Select a value for printer setting " + item.Key + ".");
                result[item.Key] = selected.Id;
            }
            return result;
        }

        private void RebuildMaintenance(PrinterCapability printer)
        {
            var actions = printer?.MaintenanceActions ?? new List<MaintenanceActionCapability>();
            MaintenancePivot.Visibility = actions.Count == 0
                ? Visibility.Collapsed
                : Visibility.Visible;
            SetMaintenanceActionVisibility(
                HeadCleanButton, HeadCleanDescription, actions, "head_clean");
            SetMaintenanceActionVisibility(
                NozzleCheckButton, NozzleCheckDescription, actions, "nozzle_check");
            SetMaintenanceActionVisibility(
                RipTestPageButton, RipTestPageDescription, actions, "test_page");
            MaintenanceResult.Text = "";
        }

        private static void SetMaintenanceActionVisibility(
            Button button,
            TextBlock description,
            IEnumerable<MaintenanceActionCapability> actions,
            string id)
        {
            var action = actions.FirstOrDefault(item => string.Equals(
                item.Id, id, StringComparison.OrdinalIgnoreCase));
            var visibility = action == null ? Visibility.Collapsed : Visibility.Visible;
            button.Visibility = visibility;
            description.Visibility = visibility;
            if (action == null) return;
            button.Content = LocalizationService.Get("Maintenance." + action.Id + ".Title");
            description.Text = LocalizationService.Get("Maintenance." + action.Id + ".Description");
        }

        private async void RunMaintenance(object sender, RoutedEventArgs args)
        {
            var button = sender as Button;
            var actionId = button?.Tag as string;
            var printer = PrinterBox?.SelectedItem as PrinterCapability;
            var action = printer?.MaintenanceActions?.FirstOrDefault(item => string.Equals(
                item.Id, actionId, StringComparison.OrdinalIgnoreCase));
            if (action == null)
            {
                MaintenanceResult.Text = LocalizationService.Get("Code.ThisMaintenanceActionIsNotSupportedByTheSelectedPrinter");
                return;
            }
            try
            {
                SetMaintenanceBusy(true);
                string host;
                if (NetworkRadio.IsChecked != true ||
                    !PrinterInstanceStore.TryNormalizeIpv4(HostBox.Text, out host))
                {
                    MaintenanceResult.Text = LocalizationService.Get(
                        "Maintenance.InvalidAddress");
                    ManagerLogger.Write("Maintenance not started: invalid printer address.");
                    return;
                }
                if (!await VerifyMaintenanceReadyAsync(host))
                    return;
                if (action.ConsumesInk)
                {
                    var confirmation = new ContentDialog
                    {
                        Title = LocalizationService.Get("Maintenance." + action.Id + ".Title"),
                        Content = LocalizationService.Get("Code.CleaningConsumesInkStartOneStandardCleaningCycleNow"),
                        PrimaryButtonText = LocalizationService.Get("Code.Clean"),
                        CloseButtonText = LocalizationService.Get("Code.Cancel"),
                        DefaultButton = ContentDialogButton.Close,
                    };
                    if (await confirmation.ShowAsync() != ContentDialogResult.Primary)
                    {
                        MaintenanceResult.Text = string.Empty;
                        return;
                    }
                    if (!await VerifyMaintenanceReadyAsync(host))
                        return;
                }
                var request = BuildMaintenanceRequest(action.Id);
                var localizedAction = LocalizationService.Get("Maintenance." + action.Id + ".Title");
                MaintenanceResult.Text = LocalizationService.Format("Code.Sending", localizedAction);
                await QueueHelperClient.RunMaintenanceAsync(request);
                MaintenanceResult.Text = action.RequiresPaper
                    ? LocalizationService.Format("Code.WasSentCheckTheLoadedPaperAndPrinterPanel", localizedAction)
                    : LocalizationService.Format("Code.WasSentToThePrinter", localizedAction);
            }
            catch (Exception error)
            {
                MaintenanceResult.Text = LocalizationService.Format("Code.MaintenanceFailed", error.Message);
            }
            finally
            {
                SetMaintenanceBusy(false);
            }
        }

        private async System.Threading.Tasks.Task<bool> VerifyMaintenanceReadyAsync(
            string host)
        {
            MaintenanceResult.Text = LocalizationService.Get("Choice.Checking") + "…";
            PrinterNetworkState state;
            try
            {
                state = await ReadMaintenanceStateAsync(host);
            }
            catch (Exception error)
            {
                ManagerLogger.WriteException("Checking printer before maintenance", error);
                state = PrinterNetworkState.Unknown;
            }
            string currentHost;
            if (NetworkRadio.IsChecked != true ||
                !PrinterInstanceStore.TryNormalizeIpv4(HostBox.Text,
                    out currentHost) ||
                !string.Equals(currentHost, host, StringComparison.OrdinalIgnoreCase))
                state = PrinterNetworkState.Unknown;
            SetConnectionTestResult(state);
            if (state == PrinterNetworkState.Online)
                return true;
            MaintenanceResult.Text = LocalizationService.Format(
                "Maintenance.NotOnline", LocalizationService.Get("Choice." + state));
            ManagerLogger.Write("Maintenance not started for " + host
                + ": printer state is " + state + ".");
            return false;
        }

        private async System.Threading.Tasks.Task<PrinterNetworkState>
            ReadMaintenanceStateAsync(string host)
        {
            var transmissions = await JobProgressReader.ReadActiveTransmissionsAsync();
            if (transmissions.Contains(host, StringComparer.OrdinalIgnoreCase))
                return PrinterNetworkState.Printing;

            var activities = await JobProgressReader.ReadActiveJobActivitiesAsync();
            var busy = activities.Where(activity =>
                (!string.IsNullOrWhiteSpace(activity.Host)
                    ? string.Equals(activity.Host, host,
                        StringComparison.OrdinalIgnoreCase)
                        && activity.Port == PrinterInstanceStore.RawTcpPort
                    : string.Equals(activity.QueueName, printerName,
                        StringComparison.OrdinalIgnoreCase))).ToList();
            if (busy.Any(activity => activity.State == "Processing"))
                return PrinterNetworkState.Processing;
            if (busy.Any(activity => activity.State == "Queued"))
                return PrinterNetworkState.Queued;
            if (busy.Any(activity => activity.State == "Preparing"))
                return PrinterNetworkState.Preparing;

            var state = await PrinterConnectivityService.TestAsync(
                host, PrinterInstanceStore.RawTcpPort);
            if (state != PrinterNetworkState.Online)
                return state;
            // ENPC confirms reachability but does not report whether another
            // application is currently printing. Use SNMP when available.
            if (await PrinterSnmpService.ReadStatusAsync(host)
                == PrinterNetworkState.Printing)
                return PrinterNetworkState.Printing;
            // Maintenance uses RAW TCP, so an ENPC/SNMP reply alone is not
            // sufficient to start an operation when port 9100 is closed.
            return await PrinterConnectivityService.TestRawTcpPortAsync(
                host, PrinterInstanceStore.RawTcpPort);
        }

        private MaintenanceRuntimeRequest BuildMaintenanceRequest(string action)
        {
            var printer = Required<PrinterCapability>(PrinterBox, "registered printer");
            string host;
            if (!PrinterInstanceStore.TryNormalizeIpv4(HostBox.Text, out host))
                throw new InvalidOperationException(
                    "Printer maintenance requires a valid network IPv4 address.");
            var request = new MaintenanceRuntimeRequest
            {
                Action = action,
                Model = printer.Id,
                Host = host,
                Port = PrinterInstanceStore.RawTcpPort,
            };
            if (!string.Equals(action, "test_page", StringComparison.OrdinalIgnoreCase))
                return request;

            var inks = LogicalInks();
            if (inks.Count == 0)
                throw new InvalidOperationException(
                    "The pyToneRIP test page requires at least one logical ink.");
            var mapping = new Dictionary<string, string>();
            foreach (var ink in inks)
            {
                if (!routes.TryGetValue(ink, out string path))
                    throw new InvalidOperationException(
                        "Select a PhysicalPath for logical ink " + ink + ".");
                mapping[ink] = path;
            }
            var media = RequiredOption(MediaBox, "media");
            var displayedMode = RequiredOption(ModeBox, "mode");
            var resolvedMode = ResolveModeForMedia(printer, media, displayedMode)
                ?? displayedMode;
            request.Mode = resolvedMode.Id;
            request.Media = media.Id;
            request.Page = RequiredOption(PageBox, "page").Id;
            request.InputSource = RequiredOption(InputSourceBox, "input source").Id;
            request.Direction = RequiredString(DirectionBox, "direction");
            request.AllowUnsupportedPpdCombinations =
                AllowUnsupportedPpdCombinationsBox.IsChecked == true;
            request.DeviceSettings = SelectedDeviceSettings();
            request.InkConfiguration = SelectedInkConfiguration();
            request.LogicalInks = inks;
            request.Routing = mapping;
            return request;
        }

        private void SetMaintenanceBusy(bool busy)
        {
            MaintenanceProgress.IsActive = busy;
            MaintenanceProgress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
            HostBox.IsEnabled = !busy && NetworkRadio.IsChecked == true;
            foreach (var button in new[]
            {
                HeadCleanButton,
                NozzleCheckButton,
                RipTestPageButton,
            })
                button.IsEnabled = !busy;
        }

        private void PageChanged(object sender, SelectionChangedEventArgs args)
        {
            var page = PageBox?.SelectedItem as CapabilityOption;
            if (!restoringInputSource && page != null
                && page.Id.StartsWith("Roll", StringComparison.OrdinalIgnoreCase))
            {
                var sources = InputSourceBox?.ItemsSource as IEnumerable<CapabilityOption>;
                var roll = sources?.FirstOrDefault(item => item.Enabled
                    && (item.Id == "Roll_Paper" || item.Id == "Roll"));
                if (roll != null)
                {
                    restoringInputSource = true;
                    try { InputSourceBox.SelectedItem = roll; }
                    finally { restoringInputSource = false; }
                }
            }
            UpdatePageMediaCompatibility();
        }

        private void InputSourceChanged(object sender, SelectionChangedEventArgs args)
        {
            if (restoringInputSource || PageBox == null) return;
            var printer = PrinterBox?.SelectedItem as PrinterCapability;
            var source = InputSourceBox?.SelectedItem as CapabilityOption;
            if (source?.Id != "Roll_Paper" && source?.Id != "Roll") return;
            var defaultPage = printer?.Id == "SureColorP900RIP"
                    || printer?.Id == "SureColorP800" ? "Roll17x22"
                : printer?.Id == "SureColorP700RIP"
                    || printer?.Id == "Stylus_Photo_R2880"
                    || printer?.Id == "Stylus_Photo_R3000" ? "Roll13x19" : null;
            if (defaultPage == null) return;
            var pages = PageBox.ItemsSource as IEnumerable<CapabilityOption>;
            var page = pages?.FirstOrDefault(item => item.Enabled && item.Id == defaultPage);
            if (page != null) PageBox.SelectedItem = page;
        }

        private void MediaChanged(object sender, SelectionChangedEventArgs args)
        {
            UpdateModeOptions((ModeBox?.SelectedItem as CapabilityOption)?.Id);
            var printer = PrinterBox?.SelectedItem as PrinterCapability;
            var media = MediaBox?.SelectedItem as CapabilityOption;
            var selected = ModeBox?.SelectedItem as CapabilityOption;
            if (HasMediaSpecificModes(printer) && media != null
                && ResolveModeForMedia(printer, media, selected) == null)
            {
                var replacement = (ModeBox.ItemsSource as IEnumerable<CapabilityOption>)
                    ?.Where(mode => ResolveModeForMedia(printer, media, mode) != null)
                    .OrderByDescending(OutputResolutionArea)
                    .FirstOrDefault();
                if (replacement != null)
                {
                    updatingPrinterOptions = true;
                    try { ModeBox.SelectedItem = replacement; }
                    finally { updatingPrinterOptions = false; }
                }
            }
            UpdatePageMediaCompatibility();
        }

        private void ModeChanged(object sender, SelectionChangedEventArgs args)
        {
            if (updatingPrinterOptions) return;
            var printer = PrinterBox?.SelectedItem as PrinterCapability;
            var mode = ModeBox?.SelectedItem as CapabilityOption;
            var media = MediaBox?.SelectedItem as CapabilityOption;
            if (!HasMediaSpecificModes(printer) || mode == null || media == null) return;
            if (ResolveModeForMedia(printer, media, mode) != null) return;

            var compatible = printer.Media?.FirstOrDefault(item =>
                ResolveModeForMedia(printer, item, mode) != null);
            if (compatible != null)
                MediaBox.SelectedItem = compatible;
        }

        private void UpdateModeOptions(string preferredMode)
        {
            var printer = PrinterBox?.SelectedItem as PrinterCapability;
            var media = MediaBox?.SelectedItem as CapabilityOption;
            if (printer?.Modes == null || ModeBox == null) return;
            if (HasMediaSpecificModes(printer))
            {
                var preferred = printer.Modes.FirstOrDefault(mode => string.Equals(
                    mode.Id, preferredMode, StringComparison.OrdinalIgnoreCase));
                var displayedModes = printer.Modes
                    .Where(mode => mode.Enabled)
                    .GroupBy(OutputResolutionKey)
                    .Select(group => group.First())
                    .OrderBy(mode => OutputResolutionArea(mode))
                    .ThenBy(mode => OutputResolutionX(mode))
                    .ThenBy(mode => OutputResolutionY(mode))
                    .ToList();
                updatingPrinterOptions = true;
                try
                {
                    ModeBox.ItemsSource = displayedModes;
                    ModeBox.SelectedItem = displayedModes.FirstOrDefault(mode =>
                        string.Equals(mode.Id, preferredMode, StringComparison.OrdinalIgnoreCase))
                        ?? displayedModes.FirstOrDefault(mode => SameOutputResolution(mode, preferred))
                        ?? displayedModes.FirstOrDefault();
                }
                finally
                {
                    updatingPrinterOptions = false;
                }
                return;
            }
            IEnumerable<CapabilityOption> modes = printer.Modes;
            if (media?.SupportedModes != null && media.SupportedModes.Count > 0)
                modes = modes.Where(mode => media.SupportedModes.Any(id => string.Equals(
                    id, mode.Id, StringComparison.OrdinalIgnoreCase)));
            ModeBox.ItemsSource = modes.ToList();
            SelectOption(ModeBox, preferredMode);
        }

        private void UnsupportedPpdPolicyChanged(object sender, RoutedEventArgs args)
        {
            UpdatePageMediaCompatibility();
        }

        private void UpdatePageMediaCompatibility()
        {
            if (ApplyButton == null || ValidationSummary == null) return;
            var page = PageBox?.SelectedItem as CapabilityOption;
            var media = MediaBox?.SelectedItem as CapabilityOption;
            if (page == null || media == null) return;
            var compatible = page.AllowedMedia == null || page.AllowedMedia.Count == 0
                || page.AllowedMedia.Any(id => string.Equals(
                    id, media.Id, StringComparison.OrdinalIgnoreCase));
            var overrideEnabled = AllowUnsupportedPpdCombinationsBox?.IsChecked == true;
            ApplyButton.IsEnabled = !applyRunning && (compatible || overrideEnabled);
            if (!compatible)
            {
                ValidationSummary.Text = overrideEnabled
                    ? LocalizationService.Format("Code.WarningExpertOverrideEnabledWithThePPDRestrictionWillBeBypassed",
                        page.DisplayName, media.DisplayName)
                    : LocalizationService.Format("Code.UnsupportedCombinationWithSelectACompatibleMediaTypeOrEnableTheExpertOve",
                        page.DisplayName, media.DisplayName);
            }
            else if ((ValidationSummary.Text ?? "").StartsWith(
                LocalizationService.Get("Code.UnsupportedCombination"), StringComparison.Ordinal)
                || (ValidationSummary.Text ?? "").StartsWith(
                    LocalizationService.Get("Code.WarningExpertOverrideEnabled"), StringComparison.Ordinal))
            {
                ValidationSummary.Text = LocalizationService.Get("Code.ConfigurationCombinationIsSupported");
            }
        }

        private static void SelectOption(ComboBox box, string id)
        {
            var values = (box.ItemsSource as IEnumerable<CapabilityOption>)?.ToList();
            if (values == null) return;
            box.SelectedItem = values.FirstOrDefault(value => value.Id == id)
                ?? values.FirstOrDefault(value => value.Enabled);
        }

        private static void SelectString(ComboBox box, string value)
        {
            var values = (box.ItemsSource as IEnumerable<LocalizedValue>)?.ToList();
            if (values == null || values.Count == 0) return;
            box.SelectedItem = values.FirstOrDefault(item => string.Equals(
                item.Value, value, StringComparison.OrdinalIgnoreCase)) ?? values[0];
        }

        private static List<LocalizedValue> LocalizedValues(IEnumerable<string> values) =>
            (values ?? Enumerable.Empty<string>())
                .Select(value => new LocalizedValue(value)).ToList();

        private void ApplyStoredRasterResolution(PrinterSelection printer)
        {
            if (printer == null || !printer.RasterResolutionX.HasValue
                || !printer.RasterResolutionY.HasValue)
                return;
            var resolutions = (RasterResolutionBox.ItemsSource
                as IEnumerable<RasterResolutionChoice>)?.ToList()
                ?? new List<RasterResolutionChoice>();
            var resolution = resolutions.FirstOrDefault(item =>
                item.X == printer.RasterResolutionX.Value
                && item.Y == printer.RasterResolutionY.Value);
            if (resolution == null)
            {
                resolution = new RasterResolutionChoice
                {
                    X = printer.RasterResolutionX.Value,
                    Y = printer.RasterResolutionY.Value,
                };
                resolutions.Add(resolution);
                RasterResolutionBox.ItemsSource = resolutions;
            }
            RasterResolutionBox.SelectedItem = resolution;
        }

        private void ApplyPrintTicketResolutionToUi(WorkflowPrintTicket ticket)
        {
            var selection = PrintTicketSynchronization.Read(ticket);
            var resolutions = (RasterResolutionBox.ItemsSource
                as IEnumerable<RasterResolutionChoice>)?.ToList();
            var resolution = resolutions?.FirstOrDefault(item =>
                selection.ResolutionX.HasValue && selection.ResolutionY.HasValue
                && item.X == selection.ResolutionX.Value
                && item.Y == selection.ResolutionY.Value);
            if (resolution != null)
                RasterResolutionBox.SelectedItem = resolution;
        }

        private async void BrowseCurve(object sender, RoutedEventArgs args)
        {
            var index = int.Parse(
                (string)(sender as FrameworkElement)?.Tag,
                CultureInfo.InvariantCulture) - 1;
            if (index > 0 && string.IsNullOrWhiteSpace(CurveBoxes()[index - 1].Text))
                return;
            var picker = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.DocumentsLibrary };
            picker.FileTypeFilter.Add(".quad");
            var file = await picker.PickSingleFileAsync();
            if (file == null) return;
            CurveBoxes()[index].Text = ExternalCurveAccessStore.Register(file);
            try { await UpdateLogicalInksFromCurveAsync(index == 0); }
            catch (Exception error)
            {
                ValidationSummary.Text = LocalizationService.Format("Code.NotApplied", error.Message);
            }
        }

        private static string DefaultMkRoute(
            Dictionary<string, string> defaults, Dictionary<string, string> existing)
        {
            var target = defaults["MK"];
            // A saved K -> physical MK assignment remains authoritative.
            // Give the newly restored MK curve the unused other black bank.
            if (existing.Values.Contains(target) && defaults.TryGetValue("K", out string other)
                && !existing.Values.Contains(other)) return other;
            return target;
        }

        private async System.Threading.Tasks.Task UpdateLogicalInksFromCurveAsync(bool replace)
        {
            Func<string, string> canonical = ink => ink == "VM" ? "M" : ink == "VLM" ? "LM"
                : ink == "O" ? "OR" : ink == "G" ? "GR" : ink;
            if (CalibrationEnabledBox.IsChecked == true || ActiveCurveCount() == 0)
            {
                var initialDefaults = DefaultRoutes(PrinterBox.SelectedItem as PrinterCapability);
                var initialInks = LogicalInks();
                if (initialDefaults.ContainsKey("MK") && !initialInks.Contains("MK")
                    && initialInks.Count == initialDefaults.Count - 1
                    && initialInks.All(ink => initialDefaults.ContainsKey(canonical(ink))))
                {
                    LogicalInksBox.Text = string.Join(",", initialInks.Concat(new[] { "MK" }));
                    routes["MK"] = DefaultMkRoute(initialDefaults, routes);
                    RebuildRoutes();
                }
                return;
            }
            var path = Curve1Box.Text.Trim();
            var inks = await ExternalCurveAccessStore.ReadLogicalInksAsync(path);
            if (!string.Equals(path, Curve1Box.Text.Trim(), StringComparison.Ordinal)) return;
            var defaults = DefaultRoutes(PrinterBox.SelectedItem as PrinterCapability);
            var current = LogicalInks();
            // Repair the old automatically generated cartridge-name list when
            // reopening settings; retain deliberate, valid custom routing.
            var wasDefault = current.Count == defaults.Count && current.All(ink =>
                defaults.TryGetValue(canonical(ink), out string destination)
                && routes.TryGetValue(ink, out string selected) && selected == destination);
            // Repair the previous automatic nine-channel P900 list which
            // omitted MK. Preserve every existing physical assignment.
            var missingIndependentMk = defaults.ContainsKey("MK") && !current.Contains("MK")
                && current.Count == defaults.Count - 1
                && current.All(ink => defaults.ContainsKey(canonical(ink)));
            if (!replace && !wasDefault && !missingIndependentMk) return;
            var mapping = new Dictionary<string, string>();
            foreach (var ink in inks)
            {
                if (routes.TryGetValue(ink, out string selected)) mapping[ink] = selected;
                else if (routes.TryGetValue(ink == "M" ? "VM" : ink == "LM" ? "VLM" : ink, out selected))
                    mapping[ink] = selected;
                else if (defaults.TryGetValue(ink, out selected))
                    mapping[ink] = ink == "MK" ? DefaultMkRoute(defaults, routes) : selected;
            }
            routes.Clear();
            foreach (var item in mapping) routes[item.Key] = item.Value;
            LogicalInksBox.Text = string.Join(",", inks);
            RebuildRoutes();
        }

        private static async System.Threading.Tasks.Task ValidateCurveChannelsAsync(PyToneRipConfiguration configuration)
        {
            if (configuration.Calibration?.Enabled == true) return;
            foreach (var path in configuration.Profiles.Curves)
            {
                var inks = await ExternalCurveAccessStore.ReadLogicalInksAsync(path);
                var missing = configuration.Channels.LogicalInks.Where(ink => !inks.Contains(ink)).ToList();
                if (missing.Count > 0)
                    throw new InvalidOperationException("QUAD does not contain logical inks: "
                        + string.Join(", ", missing) + ". Available: " + string.Join(", ", inks));
            }
        }

        private static bool HasMediaSpecificModes(PrinterCapability printer) =>
            printer?.MediaSpecificModes == true;

        private static int OutputResolutionX(CapabilityOption mode) =>
            mode?.OutputResolutionDpi != null && mode.OutputResolutionDpi.Count > 0
                ? mode.OutputResolutionDpi[0]
                : mode?.PrinterResolutionDpi != null && mode.PrinterResolutionDpi.Count > 0
                    ? mode.PrinterResolutionDpi[0]
                    : mode?.ResolutionDpi != null && mode.ResolutionDpi.Count > 0
                        ? mode.ResolutionDpi[0]
                        : 0;

        private static int OutputResolutionY(CapabilityOption mode) =>
            mode?.OutputResolutionDpi != null && mode.OutputResolutionDpi.Count > 1
                ? mode.OutputResolutionDpi[1]
                : mode?.PrinterResolutionDpi != null && mode.PrinterResolutionDpi.Count > 1
                    ? mode.PrinterResolutionDpi[1]
                    : mode?.ResolutionDpi != null && mode.ResolutionDpi.Count > 1
                        ? mode.ResolutionDpi[1]
                        : 0;

        private static long OutputResolutionArea(CapabilityOption mode) =>
            (long)OutputResolutionX(mode) * OutputResolutionY(mode);

        private static string OutputResolutionKey(CapabilityOption mode) =>
            OutputResolutionX(mode).ToString(CultureInfo.InvariantCulture) + "x"
            + OutputResolutionY(mode).ToString(CultureInfo.InvariantCulture);

        private static bool SameOutputResolution(
            CapabilityOption left, CapabilityOption right) =>
            left != null && right != null
            && OutputResolutionX(left) == OutputResolutionX(right)
            && OutputResolutionY(left) == OutputResolutionY(right);

        private static CapabilityOption ResolveModeForMedia(
            PrinterCapability printer,
            CapabilityOption media,
            CapabilityOption displayedMode)
        {
            if (printer?.Modes == null || media == null || displayedMode == null)
                return null;
            if (media.SupportedModes == null || media.SupportedModes.Count == 0)
                return displayedMode;
            var exact = printer.Modes.FirstOrDefault(mode =>
                string.Equals(mode.Id, displayedMode.Id, StringComparison.OrdinalIgnoreCase)
                && media.SupportedModes.Any(id => string.Equals(
                    id, mode.Id, StringComparison.OrdinalIgnoreCase)));
            if (exact != null) return exact;
            return printer.Modes.FirstOrDefault(mode =>
                media.SupportedModes.Any(id => string.Equals(
                    id, mode.Id, StringComparison.OrdinalIgnoreCase))
                && SameOutputResolution(mode, displayedMode));
        }

        private void RemoveCurve(object sender, RoutedEventArgs args)
        {
            var index = int.Parse(
                (string)(sender as FrameworkElement)?.Tag,
                CultureInfo.InvariantCulture) - 1;
            var boxes = CurveBoxes();
            if (index < 0 || index >= boxes.Length || string.IsNullOrWhiteSpace(boxes[index].Text))
                return;
            var paths = boxes.Select(box => box.Text).ToArray();
            var weights = Enumerable.Range(0, blendSliders.Length)
                .Select(GetBlendGroup)
                .ToArray();
            for (var position = index; position < paths.Length - 1; position++)
                paths[position] = paths[position + 1];
            paths[paths.Length - 1] = "";
            updatingCurves = true;
            for (var position = 0; position < boxes.Length; position++)
                boxes[position].Text = paths[position];
            updatingCurves = false;
            var active = ActiveCurveCount();
            for (var group = 0; group < weights.Length; group++)
            {
                var shifted = weights[group].ToList();
                shifted.RemoveAt(index);
                shifted.Add(0);
                SetBlendGroup(
                    group,
                    active == 0
                        ? new[] { 100, 0, 0 }
                        : CyclicBlendWeights.Normalize(shifted, active));
            }
            UpdateCurveActivity();
        }

        private void CurvesChanged(object sender, TextChangedEventArgs args)
        {
            UpdateCurvePathToolTip(sender as TextBox);
            if (!updatingCurves && blendSliders != null) UpdateCurveActivity();
            UpdateRequiredFieldStates();
        }

        private static void UpdateCurvePathToolTip(TextBox box)
        {
            if (box == null) return;
            var path = box.Text?.Trim();
            if (string.IsNullOrWhiteSpace(path))
            {
                ToolTipService.SetToolTip(box, null);
                return;
            }
            ToolTipService.SetToolTip(
                box,
                new ToolTip
                {
                    Content = new TextBlock
                    {
                        Text = path,
                        TextWrapping = TextWrapping.Wrap,
                        MaxWidth = 720,
                    },
                });
        }

        private TextBox[] CurveBoxes() => new[] { Curve1Box, Curve2Box, Curve3Box };

        private int ActiveCurveCount()
        {
            if (Curve1Box == null || string.IsNullOrWhiteSpace(Curve1Box.Text)) return 0;
            if (string.IsNullOrWhiteSpace(Curve2Box.Text)) return 1;
            return string.IsNullOrWhiteSpace(Curve3Box.Text) ? 2 : 3;
        }

        private void UpdateCurveActivity()
        {
            var active = ActiveCurveCount();
            var hasCurve1 = active >= 1;
            var hasCurve2 = active >= 2;
            Curve1Box.IsEnabled = true;
            Curve2Box.IsEnabled = hasCurve1;
            Curve3Box.IsEnabled = hasCurve2;
            BrowseCurve1.IsEnabled = true;
            BrowseCurve2.IsEnabled = hasCurve1;
            BrowseCurve3.IsEnabled = hasCurve2;
            RemoveCurve1.IsEnabled = hasCurve1;
            RemoveCurve2.IsEnabled = hasCurve2;
            RemoveCurve3.IsEnabled = active >= 3;
            for (var group = 0; group < blendSliders.Length; group++)
            {
                SetBlendGroup(
                    group,
                    active == 0
                        ? new[] { 100, 0, 0 }
                        : CyclicBlendWeights.Normalize(GetBlendGroup(group), active));
                for (var index = 0; index < 3; index++)
                {
                    blendSliders[group][index].IsEnabled = index < active;
                    blendValues[group][index].Opacity = index < active ? 1 : 0.35;
                }
            }
            UpdateRequiredFieldStates();
        }

        private void UpdateRequiredFieldStates()
        {
            var calibrationEnabled = CalibrationEnabledBox?.IsChecked == true;
            SetRequiredFieldState(
                Curve1Box,
                !calibrationEnabled && string.IsNullOrWhiteSpace(Curve1Box?.Text));

            var useUsb = UsbRadio?.IsChecked == true;
            if (useUsb)
            {
                SetRequiredFieldState(HostBox, false);
                SetRequiredFieldState(UsbDeviceBox, UsbDeviceBox?.SelectedItem == null);
                return;
            }

            SetRequiredFieldState(UsbDeviceBox, false);
            SetRequiredFieldState(
                HostBox,
                !PrinterInstanceStore.TryNormalizeIpv4(HostBox?.Text, out _),
                useSystemBackground: true);
        }

        private static void SetRequiredFieldState(
            Control control, bool isMissing, bool useSystemBackground = false)
        {
            if (control == null) return;
            if (isMissing)
            {
                // Preserve the system text/background pair while an IP is
                // incomplete; the border still marks the required field.
                if (useSystemBackground)
                    control.ClearValue(Control.BackgroundProperty);
                else
                    control.Background = (Brush)Application.Current.Resources["RequiredFieldBackgroundBrush"];
                control.BorderBrush = (Brush)Application.Current.Resources["RequiredFieldBorderBrush"];
                control.BorderThickness = new Thickness(2);
                return;
            }
            control.ClearValue(Control.BackgroundProperty);
            control.ClearValue(Control.BorderBrushProperty);
            control.ClearValue(Control.BorderThicknessProperty);
        }

        private int[] GetBlendGroup(int group) => blendSliders[group]
            .Select(slider => (int)Math.Round(slider.Value)).ToArray();

        private void SetBlendGroup(int group, IEnumerable<int> weights)
        {
            var values = weights.ToArray();
            updatingBlend = true;
            for (var index = 0; index < 3; index++)
            {
                var value = index < values.Length ? values[index] : 0;
                blendSliders[group][index].Value = value;
                blendValues[group][index].Text = value + "%";
            }
            updatingBlend = false;
        }

        private void BlendChanged(
            int group,
            object sender,
            Windows.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs args)
        {
            if (updatingBlend || blendSliders == null) return;
            var slider = (Slider)sender;
            var index = int.Parse((string)slider.Tag, CultureInfo.InvariantCulture);
            var active = ActiveCurveCount();
            if (active == 0 || index >= active)
            {
                UpdateCurveActivity();
                return;
            }
            var previous = GetBlendGroup(group);
            previous[index] = (int)Math.Round(args.OldValue);
            var adjusted = CyclicBlendWeights.ApplyChange(
                previous, index, (int)Math.Round(args.NewValue), active);
            SetBlendGroup(group, adjusted);
        }

        private void AllToneChanged(object sender, Windows.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs args) => BlendChanged(0, sender, args);
        private void HighlightsChanged(object sender, Windows.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs args) => BlendChanged(1, sender, args);
        private void MidtonesChanged(object sender, Windows.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs args) => BlendChanged(2, sender, args);
        private void ShadowsChanged(object sender, Windows.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs args) => BlendChanged(3, sender, args);

        private void SplitToneChanged(object sender, RoutedEventArgs args)
        {
            if (AllTonesPanel != null) SetSplitToneVisibility(SplitToneSwitch.IsOn);
        }

        private void SetSplitToneVisibility(bool split)
        {
            AllTonesPanel.Visibility = split ? Visibility.Collapsed : Visibility.Visible;
            SplitTonesPanel.Visibility = split ? Visibility.Visible : Visibility.Collapsed;
        }

        private void LogicalInksChanged(object sender, TextChangedEventArgs args)
        {
            if (updatingLogicalInks) return;
            var original = LogicalInksBox.Text ?? "";
            var caret = Math.Min(LogicalInksBox.SelectionStart, original.Length);
            var normalized = NormalizeLogicalInkText(original);
            if (!string.Equals(original, normalized, StringComparison.Ordinal))
            {
                var normalizedPrefix = NormalizeLogicalInkText(original.Substring(0, caret));
                updatingLogicalInks = true;
                LogicalInksBox.Text = normalized;
                LogicalInksBox.SelectionStart = Math.Min(normalizedPrefix.Length, normalized.Length);
                updatingLogicalInks = false;
            }
            RebuildRoutes();
        }

        private static string NormalizeLogicalInkText(string value) =>
            new string((value ?? "").Where(character =>
                char.IsLetterOrDigit(character) || character == ',').ToArray());

        private void RebuildRoutes()
        {
            if (RoutePanel == null) return;
            RoutePanel.Children.Clear();
            var printer = PrinterBox.SelectedItem as PrinterCapability;
            if (printer == null) return;
            var inkConfiguration = SelectedInkConfiguration();
            foreach (var path in printer.PhysicalPaths ?? new List<PhysicalPathCapability>())
                path.ActiveInkConfiguration = inkConfiguration;
            foreach (var ink in LogicalInks())
            {
                var row = new Grid { Margin = new Thickness(0, 0, 0, 8) };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(32) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(150) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                var swatch = new Ellipse { Width = 14, Height = 14, Fill = new SolidColorBrush(InkColor(ink)), VerticalAlignment = VerticalAlignment.Center };
                var label = new TextBlock { Text = ink, FontWeight = Windows.UI.Text.FontWeights.SemiBold, VerticalAlignment = VerticalAlignment.Center };
                Grid.SetColumn(label, 1);
                var box = new ComboBox { ItemsSource = printer.PhysicalPaths, Tag = ink, HorizontalAlignment = HorizontalAlignment.Stretch };
                Grid.SetColumn(box, 2);
                if (routes.TryGetValue(ink, out string selected))
                    box.SelectedItem = printer.PhysicalPaths.FirstOrDefault(path => path.Id == selected);
                box.SelectionChanged += RouteChanged;
                row.Children.Add(swatch); row.Children.Add(label); row.Children.Add(box);
                RoutePanel.Children.Add(row);
            }
        }

        private static Color InkColor(string ink)
        {
            switch ((ink ?? "").Trim().ToUpperInvariant())
            {
                case "C": case "CYAN": return Colors.DeepSkyBlue;
                case "M": case "MAGENTA": return Colors.Magenta;
                case "VM": case "VIVID_MAGENTA": return Color.FromArgb(255, 226, 0, 126);
                case "Y": case "YELLOW": return Colors.Gold;
                case "LC": return Color.FromArgb(255, 120, 210, 255);
                case "LM": return Color.FromArgb(255, 255, 145, 210);
                case "VLM": case "VIVID_LIGHT_MAGENTA": return Color.FromArgb(255, 255, 105, 180);
                case "V": case "VIOLET": return Color.FromArgb(255, 138, 43, 226);
                case "O": case "OR": case "ORANGE": return Colors.DarkOrange;
                case "G": case "GREEN": return Colors.ForestGreen;
                case "R": case "RED": return Colors.IndianRed;
                case "B": case "BLUE": return Colors.RoyalBlue;
                case "K": case "BLACK": case "PK": case "MK": return Colors.Black;
                case "LK": return Color.FromArgb(255, 95, 95, 95);
                case "LLK": return Color.FromArgb(255, 180, 180, 180);
                default: return Colors.Gray;
            }
        }

        private void RouteChanged(object sender, SelectionChangedEventArgs args)
        {
            var box = (ComboBox)sender;
            var path = box.SelectedItem as PhysicalPathCapability;
            if (path != null) routes[(string)box.Tag] = path.Id;
        }

        private void ConnectionChanged(object sender, RoutedEventArgs args)
        {
            if (NetworkPanel != null)
            {
                var enabled = NetworkRadio.IsChecked == true;
                HostBox.IsEnabled = enabled;
                PortBox.IsEnabled = enabled;
                TestConnectionButton.IsEnabled = enabled;
                if (ReadStatusButton != null)
                    ReadStatusButton.IsEnabled = enabled || UsbRadio.IsChecked == true;
                NetworkPanel.Opacity = enabled ? 1 : 0.45;
                if (UsbPanel != null) UsbPanel.Visibility = enabled ? Visibility.Collapsed : Visibility.Visible;
                if (!enabled && connectionReady) _ = RefreshUsbDevicesAsync();
            }
            UpdateRequiredFieldStates();
        }

        private async void RefreshUsb(object sender, RoutedEventArgs args) => await RefreshUsbDevicesAsync();

        private async System.Threading.Tasks.Task RefreshUsbDevicesAsync()
        {
            if (usbRefreshRunning || UsbDeviceBox == null) return;
            usbRefreshRunning = true;
            RefreshUsbButton.IsEnabled = false;
            UsbStatusText.Text = LocalizationService.Get("Usb.Searching");
            var previous = (UsbDeviceBox.SelectedItem as UsbPrinterDescriptor)?.DeviceInstanceId
                ?? originalConfiguration?.Connection?.UsbDeviceId ?? instance?.UsbDeviceId;
#if USB_PHYSICAL_QUEUE_FALLBACK
            var previousQueue = UsbPortBox.SelectedItem as string
                ?? originalConfiguration?.Connection?.PhysicalQueueName ?? instance?.PhysicalQueueName;
#endif
            try
            {
                var model = PrinterBox.SelectedItem as PrinterCapability;
                var devices = (await QueueHelperClient.DiscoverUsbPrintersAsync())
                    .Where(device => UsbPrinterModelMatcher.Matches(model?.Id, model?.DisplayName, device)).ToList();
                UsbDeviceBox.ItemsSource = devices;
                UsbDeviceBox.SelectedItem = !string.IsNullOrWhiteSpace(previous)
                    ? devices.FirstOrDefault(device => device.DeviceInstanceId == previous)
                    : devices.Count == 1 ? devices[0] : null;
#if USB_PHYSICAL_QUEUE_FALLBACK
                if (previousQueue != null && UsbPortBox.Items.Contains(previousQueue))
                    UsbPortBox.SelectedItem = previousQueue;
#endif
                if (devices.Count == 0) UsbStatusText.Text = LocalizationService.Get("Usb.None");
                else if (UsbDeviceBox.SelectedItem == null) UsbStatusText.Text = LocalizationService.Get("Usb.SelectMatchingDevice");
            }
            catch (Exception error)
            {
                UsbDeviceBox.ItemsSource = new List<UsbPrinterDescriptor>();
#if USB_PHYSICAL_QUEUE_FALLBACK
                UsbPortBox.ItemsSource = new List<string>();
#endif
                UsbStatusText.Text = LocalizationService.Format("Usb.DiscoveryFailed", error.Message);
                ManagerLogger.WriteException("Discovering USB printer destinations", error);
            }
            finally { RefreshUsbButton.IsEnabled = true; usbRefreshRunning = false; }
        }

        private void UsbDeviceChanged(object sender, SelectionChangedEventArgs args)
        {
#if USB_PHYSICAL_QUEUE_FALLBACK
            if (UsbPortBox == null) return;
            var device = UsbDeviceBox.SelectedItem as UsbPrinterDescriptor;
            var queues = device?.QueueNames ?? new List<string>();
            UsbPortBox.ItemsSource = queues;
            UsbPortBox.IsEnabled = queues.Count > 0;
            UsbPortBox.SelectedItem = queues.Count == 1 ? queues[0] : null;
#endif
            var selected = UsbDeviceBox.SelectedItem as UsbPrinterDescriptor;
            UsbStatusText.Text = selected == null ? LocalizationService.Get("Usb.SelectMatchingDevice") : selected.ToString();
            UpdateRequiredFieldStates();
        }

        private async System.Threading.Tasks.Task ValidateUsbDestinationAsync(ConnectionSelection connection)
        {
            var devices = await QueueHelperClient.DiscoverUsbPrintersAsync();
            var device = devices.FirstOrDefault(item => item.DeviceInstanceId == connection.UsbDeviceId);
#if USB_PHYSICAL_QUEUE_FALLBACK
            if (connection.UsbTransportMode == "windows_queue"
                && (device == null || !device.QueueNames.Contains(connection.PhysicalQueueName)))
                throw new InvalidOperationException(LocalizationService.Get("Usb.DestinationChanged"));
#else
            if (connection.UsbTransportMode == "windows_queue")
                throw new InvalidOperationException(LocalizationService.Get("Usb.DestinationChanged"));
#endif
            if (device == null)
                throw new InvalidOperationException(LocalizationService.Get("Usb.DestinationChanged"));
            connection.UsbPort = device.PortName;
        }

        private void PreferencesPivotSizeChanged(object sender, SizeChangedEventArgs args)
        {
            if (ConnectionContentHost == null || ConnectionCards == null)
                return;

            // PivotItem content can be arranged at its desired width rather
            // than across the Pivot viewport. Give the Connection page a
            // viewport-sized host so both cards share the same center.
            var width = Math.Max(0, args.NewSize.Width);
            ConnectionContentHost.Width = width;
            ConnectionCards.Width = Math.Min(760, Math.Max(0, width - 24));
        }

        private void PreferencesPivotSelectionChanged(
            object sender, SelectionChangedEventArgs args)
        {
            if (connectionReady && PreferencesPivot.SelectedItem == ConnectionPivot)
                _ = RefreshConnectionAsync();
        }

        private async void TestConnection(object sender, RoutedEventArgs args)
        {
            var host = HostBox.Text?.Trim();
            if (!IsValidIpv4(host))
            {
                SetPortTestResult(PrinterNetworkState.Unknown);
                ValidationSummary.Text = LocalizationService.Get("Code.TestFailedEnterAValidIPv4Address");
                return;
            }

            var port = PrinterInstanceStore.RawTcpPort;
            TestConnectionButton.IsEnabled = false;
            SetPortTestResult(PrinterNetworkState.Checking);
            try
            {
                var result = await PrinterConnectivityService.TestRawTcpPortAsync(host, port);
                SetPortTestResult(result);
                ValidationSummary.Text = result == PrinterNetworkState.Online
                    ? LocalizationService.Format("Code.OnlineAcceptsATCPConnection", host, port)
                    : LocalizationService.Format("Code.OfflineDidNotAcceptATCPConnection", host, port);
            }
            finally
            {
                TestConnectionButton.IsEnabled = NetworkRadio.IsChecked == true;
            }
        }

        private void SetPortTestResult(PrinterNetworkState state)
        {
            TestConnectionButton.Content = state == PrinterNetworkState.Unknown
                ? LocalizationService.Get("Code.Test")
                : LocalizationService.Get("Choice." + state);
            TestConnectionButton.ClearValue(Button.BackgroundProperty);
            switch (state)
            {
                case PrinterNetworkState.Online:
                    TestConnectionButton.ClearValue(Button.ForegroundProperty);
                    break;
                case PrinterNetworkState.Offline:
                    TestConnectionButton.Foreground = (Brush)Application.Current.Resources[
                        "SystemThemeErrorBrush"];
                    break;
                case PrinterNetworkState.Checking:
                    TestConnectionButton.Foreground = (Brush)Application.Current.Resources[
                        "SystemThemeAccentBrush"];
                    break;
                default:
                    TestConnectionButton.ClearValue(Button.BackgroundProperty);
                    TestConnectionButton.ClearValue(Button.ForegroundProperty);
                    break;
            }
        }

        private async void ReadStatus(object sender, RoutedEventArgs args)
        {
            await RefreshConnectionAsync();
        }

        private async System.Threading.Tasks.Task RefreshConnectionAsync()
        {
            if (UsbRadio.IsChecked == true)
            {
                await RefreshUsbStatusAsync();
                return;
            }
            if (connectionRefreshRunning || NetworkRadio.IsChecked != true) return;
            var host = HostBox.Text?.Trim();
            if (!IsValidIpv4(host))
            {
                SetConnectionTestResult(PrinterNetworkState.Unknown);
                ValidationSummary.Text = LocalizationService.Get("Code.TestFailedEnterAValidIPv4Address");
                return;
            }
            connectionRefreshRunning = true;
            ReadStatusButton.IsEnabled = false;
            SetConnectionTestResult(PrinterNetworkState.Checking);
            RenderInkLevels(new List<PrinterInkLevel>());
            var expectedInkCount = (PrinterBox.SelectedItem as PrinterCapability)
                ?.PhysicalPaths?.Count ?? 0;
            try
            {
                var active = await JobProgressReader.ReadActiveTransmissionsAsync();
                if (active.Contains(host, StringComparer.OrdinalIgnoreCase))
                {
                    var activeInks = await PrinterSnmpService.ReadInkAsync(
                        host, expectedInkCount);
                    SetConnectionTestResult(PrinterNetworkState.Printing);
                    RenderInkLevels(activeInks);
                    return;
                }
                var result = await PrinterConnectivityService.TestAsync(
                    host, PrinterInstanceStore.RawTcpPort);
                IReadOnlyList<PrinterInkLevel> inks = result == PrinterNetworkState.Online
                    ? await PrinterSnmpService.ReadInkAsync(host, expectedInkCount)
                    : new List<PrinterInkLevel>();
                SetConnectionTestResult(result);
                RenderInkLevels(inks);
            }
            catch (Exception error)
            {
                ManagerLogger.WriteException("Reading printer status and ink", error);
                SetConnectionTestResult(PrinterNetworkState.Unknown);
                RenderInkLevels(new List<PrinterInkLevel>());
            }
            finally
            {
                connectionRefreshRunning = false;
                ReadStatusButton.IsEnabled = NetworkRadio.IsChecked == true || UsbRadio.IsChecked == true;
            }
        }

        private async System.Threading.Tasks.Task RefreshUsbStatusAsync()
        {
            if (connectionRefreshRunning) return;
            var device = UsbDeviceBox.SelectedItem as UsbPrinterDescriptor;
            if (device == null || string.IsNullOrWhiteSpace(device.DeviceInstanceId))
            {
                SetConnectionTestResult(PrinterNetworkState.Unknown);
                RenderInkLevels(new List<PrinterInkLevel>());
                ValidationSummary.Text = LocalizationService.Get("Usb.SelectMatchingDevice");
                return;
            }
            connectionRefreshRunning = true;
            ReadStatusButton.IsEnabled = false;
            SetConnectionTestResult(PrinterNetworkState.Checking);
            try
            {
                var activities = await JobProgressReader.ReadActiveJobActivitiesAsync();
                if (activities.Any(activity => activity.State != "Queued" && string.Equals(
                    activity.QueueName, printerName, StringComparison.OrdinalIgnoreCase)))
                {
                    SetConnectionTestResult(PrinterNetworkState.Printing);
                    return;
                }
                RenderInkLevels(new List<PrinterInkLevel>());
                var usbInks = await QueueHelperClient.ReadUsbInkAsync(device.DeviceInstanceId);
                var inks = usbInks.Select(ink => new PrinterInkLevel
                {
                    Name = ink.Name,
                    Percent = ink.Percent,
                }).ToList();
                SetConnectionTestResult(PrinterNetworkState.Online);
                RenderInkLevels(inks);
            }
            catch (Exception error)
            {
                ManagerLogger.WriteException("Reading USB printer status and ink", error);
                SetConnectionTestResult(PrinterNetworkState.Unknown);
                RenderInkLevels(new List<PrinterInkLevel>());
            }
            finally
            {
                connectionRefreshRunning = false;
                ReadStatusButton.IsEnabled = NetworkRadio.IsChecked == true || UsbRadio.IsChecked == true;
            }
        }

        private void SetConnectionTestResult(PrinterNetworkState state)
        {
            ConnectionStatusText.Text = LocalizationService.Get("Choice." + state);
            switch (state)
            {
                case PrinterNetworkState.Online:
                case PrinterNetworkState.Printing:
                case PrinterNetworkState.Checking:
                    ConnectionStatusText.Foreground = (Brush)Application.Current.Resources[
                        "SystemThemeAccentBrush"];
                    break;
                case PrinterNetworkState.Offline:
                    ConnectionStatusText.Foreground = (Brush)Application.Current.Resources[
                        "SystemThemeErrorBrush"];
                    break;
                default:
                    ConnectionStatusText.Foreground = (Brush)Application.Current.Resources[
                        "SystemThemeSecondaryBrush"];
                    break;
            }
        }

        private void RenderInkLevels(IReadOnlyList<PrinterInkLevel> inks)
        {
            InkLevelsPanel.Children.Clear();
            InkReadoutText.Visibility = inks.Count == 0
                ? Visibility.Visible : Visibility.Collapsed;
            if (inks.Count == 0)
            {
                foreach (var path in (PrinterBox.SelectedItem as PrinterCapability)
                    ?.PhysicalPaths ?? new List<PhysicalPathCapability>())
                {
                    InkLevelsPanel.Children.Add(new TextBlock
                    {
                        Text = (path.DisplayName ?? path.ShortName ?? path.Id) + " —",
                        Foreground = new SolidColorBrush(Colors.Gray),
                        Margin = new Thickness(0, 4, 0, 4),
                    });
                }
                return;
            }
            var paths = (PrinterBox.SelectedItem as PrinterCapability)?.PhysicalPaths;
            var reported = new HashSet<string>(inks.Select(ink =>
                FindInkPath(ink.Name)?.Id).Where(id => id != null),
                StringComparer.OrdinalIgnoreCase);
            var unnamed = inks.Where(ink => FindInkPath(ink.Name) == null).ToList();
            var missing = paths?.Where(path => !reported.Contains(path.Id)).ToList();
            var inferredInk = unnamed.Count == 1 && missing?.Count == 1
                ? unnamed[0] : null;
            var inferredPath = inferredInk == null ? null : missing[0];
            if (inferredPath != null) reported.Add(inferredPath.Id);
            foreach (var ink in inks)
            {
                var row = new StackPanel
                {
                    Orientation = Orientation.Horizontal,
                    Margin = new Thickness(0, 4, 0, 4),
                };
                row.Children.Add(new TextBlock
                {
                    Text = ReferenceEquals(ink, inferredInk)
                        ? (inferredPath.DisplayName ?? inferredPath.ShortName ?? ink.Name)
                        : InkDisplayName(ink.Name),
                    Width = 180,
                    TextTrimming = TextTrimming.CharacterEllipsis,
                    VerticalAlignment = VerticalAlignment.Center,
                });
                row.Children.Add(new ProgressBar
                {
                    Minimum = 0,
                    Maximum = 100,
                    Value = ink.Percent,
                    Width = 240,
                    Height = 14,
                    Foreground = new SolidColorBrush(InkLevelColor(
                        ReferenceEquals(ink, inferredInk)
                            ? inferredPath.ShortName
                            : FindInkPath(ink.Name)?.ShortName ?? ink.Name)),
                    VerticalAlignment = VerticalAlignment.Center,
                });
                row.Children.Add(new TextBlock
                {
                    Text = ink.Percent.ToString(CultureInfo.CurrentCulture) + "%",
                    Width = 54,
                    Margin = new Thickness(12, 0, 0, 0),
                    TextAlignment = TextAlignment.Right,
                    VerticalAlignment = VerticalAlignment.Center,
                });
                InkLevelsPanel.Children.Add(row);
            }
            if (paths != null && inks.Count < paths.Count)
            {
                foreach (var path in paths.Where(path => !reported.Contains(path.Id)))
                    InkLevelsPanel.Children.Add(new TextBlock
                    {
                        Text = (path.DisplayName ?? path.ShortName ?? path.Id) + " —",
                        Foreground = new SolidColorBrush(Colors.Gray),
                        Margin = new Thickness(0, 4, 0, 4),
                    });
            }
        }

        private string InkDisplayName(string name)
        {
            var path = FindInkPath(name);
            // P600/P800 report two cartridge levels for one switchable raster
            // path. Keep each cartridge's own name in the status readout.
            if (IsSwitchableBlackPath(path))
            {
                switch (BlackCartridgeCode(name))
                {
                    case "PK": return "Photo Black (PK)";
                    case "MK": return "Matte Black (MK)";
                }
            }
            return string.IsNullOrWhiteSpace(path?.DisplayName)
                ? name : path.DisplayName;
        }

        private static string BlackCartridgeCode(string name)
        {
            var value = new string((name ?? "").Where(char.IsLetterOrDigit)
                .Select(char.ToLowerInvariant).ToArray());
            if (value == "pk" || value == "photoblack") return "PK";
            if (value == "mk" || value == "matteblack") return "MK";
            return null;
        }

        private static bool IsSwitchableBlackPath(PhysicalPathCapability path)
        {
            var roles = path?.ConfigurationRoles;
            return roles != null
                && roles.Any(role => InkLabels.ShortName(role.Role) == "PK")
                && roles.Any(role => InkLabels.ShortName(role.Role) == "MK");
        }

        private PhysicalPathCapability FindInkPath(string name)
        {
            var paths = (PrinterBox.SelectedItem as PrinterCapability)?.PhysicalPaths;
            var blackCartridgeCode = BlackCartridgeCode(name);
            var path = paths?.FirstOrDefault(item =>
                string.Equals(item.ShortName, name,
                    StringComparison.OrdinalIgnoreCase) ||
                (item.DisplayName ?? "").EndsWith("— " + name,
                    StringComparison.OrdinalIgnoreCase) ||
                (item.ConfigurationRoles ?? new List<PhysicalPathConfigurationRole>())
                    .Any(role => string.Equals(InkLabels.ShortName(role.Role), name,
                        StringComparison.OrdinalIgnoreCase) ||
                        (blackCartridgeCode != null && string.Equals(
                            InkLabels.ShortName(role.Role), blackCartridgeCode,
                            StringComparison.OrdinalIgnoreCase))));
            if (path != null || paths == null) return path;
            var expanded = string.Equals(name, "M", StringComparison.OrdinalIgnoreCase)
                ? "VM" : string.Equals(name, "LM", StringComparison.OrdinalIgnoreCase)
                ? "VLM" : null;
            return expanded == null ? null : paths.FirstOrDefault(item =>
                string.Equals(item.ShortName, expanded,
                    StringComparison.OrdinalIgnoreCase));
        }

        private static Color InkLevelColor(string name)
        {
            var value = new string((name ?? "").Where(char.IsLetterOrDigit)
                .Select(char.ToLowerInvariant).ToArray());
            if (value == "lc" || value.Contains("lightcyan"))
                return Color.FromArgb(255, 120, 210, 255);
            if (value == "vlm" || value.Contains("vividlightmagenta"))
                return Color.FromArgb(255, 255, 105, 180);
            if (value == "lm" || value.Contains("lightmagenta"))
                return Color.FromArgb(255, 255, 145, 210);
            if (value == "llk" || value.Contains("lightlightblack")
                || value.Contains("lightlightgray") || value.Contains("lightlightgrey"))
                return Color.FromArgb(255, 180, 180, 180);
            if (value == "lk" || value.Contains("lightblack")
                || value.Contains("lightgray") || value.Contains("lightgrey"))
                return Color.FromArgb(255, 95, 95, 95);
            if (value == "c" || value.Contains("cyan"))
                return Colors.DeepSkyBlue;
            if (value == "vm" || value.Contains("vividmagenta"))
                return Color.FromArgb(255, 226, 0, 126);
            if (value == "m"
                || value.Contains("magenta")) return Colors.MediumVioletRed;
            if (value == "y" || value.Contains("yellow")) return Colors.Goldenrod;
            if (value == "o" || value == "or" || value.Contains("orange"))
                return Colors.DarkOrange;
            if (value == "g" || value.Contains("green")) return Colors.ForestGreen;
            if (value == "r" || value.Contains("red")) return Colors.IndianRed;
            if (value == "b" || value.Contains("blue")) return Colors.RoyalBlue;
            if (value == "v" || value.Contains("violet")) return Colors.DarkViolet;
            if (value == "k" || value == "bk" || value == "pk" || value == "mk"
                || value == "pkmk" || value == "mkpk"
                || value.Contains("black")) return Colors.Black;
            if (value.Contains("gray") || value.Contains("grey"))
                return Colors.DimGray;
            return Colors.SteelBlue;
        }

        private void NetworkHostBeforeTextChanging(
            TextBox sender,
            TextBoxBeforeTextChangingEventArgs args) =>
            args.Cancel = args.NewText.Any(character =>
                !char.IsDigit(character) && character != '.');

        private void NetworkHostChanged(object sender, TextChangedEventArgs args)
        {
            UpdateRequiredFieldStates();
            SetPortTestResult(PrinterNetworkState.Unknown);
        }

        private void SizingChanged(object sender, SelectionChangedEventArgs args)
        {
            if (ScaleBox == null) return;
            var isFit = string.Equals(
                (SizingBox.SelectedItem as LocalizedValue)?.Value,
                "fit",
                StringComparison.OrdinalIgnoreCase);
            ScaleBox.IsEnabled = !isFit;
            ScaleBox.Opacity = isFit ? 0.45 : 1;
        }

        private void RipLayoutModeChanged(object sender, RoutedEventArgs args) =>
            UpdateRipLayoutControls();

        private void UpdateRipLayoutControls()
        {
            if (RipLayoutPanel == null) return;
            var enabled = ApplyRipLayoutBox?.IsChecked == true;
            RipLayoutPanel.IsEnabled = enabled;
            RipLayoutPanel.Opacity = enabled ? 1 : 0.45;
        }

        private void CalibrationModeChanged(object sender, RoutedEventArgs args)
        {
            // On first enable the ComboBox has no persisted selection yet. RGB
            // Selectors is the safe/default calibration input; preserve an
            // already selected Per-channel mode when toggling the checkbox.
            if (CalibrationEnabledBox?.IsChecked == true &&
                CalibrationModeBox?.SelectedItem == null)
                SelectCalibrationMode("rgb_selectors");
            UpdateCalibrationControls();
        }
        private void CalibrationInkLimitChanged(object sender, Windows.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs args)
        {
            if (CalibrationInkLimitLabel != null) CalibrationInkLimitLabel.Text = ((int)args.NewValue).ToString(CultureInfo.InvariantCulture) + "%";
        }
        private void UpdateCalibrationControls()
        {
            var enabled = CalibrationEnabledBox?.IsChecked == true;
            if (CalibrationPanel != null) CalibrationPanel.Visibility = enabled ? Visibility.Visible : Visibility.Collapsed;
            if (LayoutSection != null) LayoutSection.Visibility = enabled ? Visibility.Collapsed : Visibility.Visible;
            if (ToneCurvesSection != null) ToneCurvesSection.Visibility = enabled ? Visibility.Collapsed : Visibility.Visible;
            UpdateRequiredFieldStates();
            // RAW bypasses profile curves and blending. Gamma, normal ink
            // limit and dither remain active with their saved values.
        }
        private void SelectCalibrationMode(string mode)
        {
            foreach (var item in CalibrationModeBox.Items.OfType<ComboBoxItem>())
                if (string.Equals(item.Tag as string, mode, StringComparison.OrdinalIgnoreCase)) { CalibrationModeBox.SelectedItem = item; return; }
            CalibrationModeBox.SelectedIndex = 0;
        }
        private string SelectedCalibrationMode() => (CalibrationModeBox.SelectedItem as ComboBoxItem)?.Tag as string ?? "rgb_selectors";

        private void PlacementChanged(object sender, SelectionChangedEventArgs args)
        {
            if (OffsetPanel == null) return;
            var isOffset = string.Equals(
                (PlacementBox.SelectedItem as LocalizedValue)?.Value,
                "offset",
                StringComparison.OrdinalIgnoreCase);
            OffsetPanel.Visibility = isOffset ? Visibility.Visible : Visibility.Collapsed;
        }

        private async void Apply(object sender, RoutedEventArgs args)
        {
            if (applyRunning) return;
            applyRunning = true;
            ApplyButton.IsEnabled = false;
            try
            {
                var configuration = BuildConfiguration();
                await ValidateCurveChannelsAsync(configuration);
                if (configuration.Connection.Kind == "usb") await ValidateUsbDestinationAsync(configuration.Connection);
                else await EnsureNetworkHostAvailableAsync(configuration.Connection.Host);
                var json = ConfigurationCodec.Serialize(configuration);
                VerifyCalibrationPersistence(configuration, json, "serialization");
                var page = RequiredOption(PageBox, "page");
                var rasterResolution = Required<RasterResolutionChoice>(
                    RasterResolutionBox, "Windows raster resolution");
                if (!page.WidthMm.HasValue || !page.HeightMm.HasValue)
                    throw new InvalidOperationException("Selected page has no physical dimensions.");
                // RIP settings belong to our store. A Windows PrintTicket
                // failure must not discard them or leave a new queue unconfigured.
                await ConfigurationCodec.WriteStoredAsync(printerUri, printerName, json);
                var storedJson = await ConfigurationCodec.ReadStoredAsync(printerUri, printerName);
                if (!string.Equals(json, storedJson, StringComparison.Ordinal))
                    throw new InvalidOperationException("Saved RIP configuration does not match the requested settings.");
                VerifyCalibrationPersistence(configuration, storedJson, "storage");
                await UpdateInstanceAsync(configuration);
                try
                {
                    await ApplyWindowsSettingsAsync(configuration, page, rasterResolution);
                }
                catch (Exception windowsError)
                {
                    await SettingsApplyNotifications.ReportFailureAsync(printerName, windowsError);
                    CompleteAndClose();
                    return;
                }
                ValidationSummary.Text = LocalizationService.Format("Code.ConfigurationSavedFor", QueueIdentityText.Text);
                CompleteAndClose();
            }
            catch (Exception error)
            {
                App.WriteStartupException("PropertiesPage.Apply failed: queue=" + printerName, error);
                ValidationSummary.Text = LocalizationService.Format("Code.NotApplied", error.Message);
            }
            finally
            {
                applyRunning = false;
                UpdatePageMediaCompatibility();
            }
        }

        private async System.Threading.Tasks.Task ApplyWindowsSettingsAsync(
            PyToneRipConfiguration configuration, CapabilityOption page,
            RasterResolutionChoice rasterResolution)
        {
            if (session == null)
            {
                await PrinterSystemDefaultsService.ApplyUserDefaultsAsync(
                    printerName, page.Id, page.WidthMm.Value, page.HeightMm.Value,
                    configuration.Layout.Orientation, rasterResolution.X, rasterResolution.Y,
                    configuration.Printer.Media, configuration.Printer.InputSource, page.Borderless);
                return;
            }
            var ticket = session.SessionPrintTicket;
            PrintTicketSynchronization.Apply(
                ticket, page.Id, page.WidthMm.Value, page.HeightMm.Value,
                configuration.Layout.Orientation, rasterResolution.X, rasterResolution.Y,
                configuration.Printer.Media, configuration.Printer.InputSource, page.Borderless);
            var validation = await PrintTicketSynchronization.ValidateAsync(
                ticket, message => App.WriteStartupStage("queue=" + printerName
                    + "; launch=" + session.LaunchKind + "; " + message));
            if (!validation.Validated)
                throw new InvalidOperationException("PrintTicket validation failed: " + validation.ExtendedError);
            session.UpdatePrintTicket(ticket);
            if (session.LaunchKind != SettingsLaunchKind.UserDefaultPrintTicket) return;
            try
            {
                await PrinterSystemDefaultsService.SetLegacyUserDefaultsAsync(
                    printerName, page.Id, page.WidthMm.Value, page.HeightMm.Value,
                    configuration.Layout.Orientation,
                    rasterResolution.X, rasterResolution.Y, page.Borderless);
                await PrinterSystemDefaultsService.VerifyAndNotifyLegacyUserDefaultsAsync(
                    printerName, page.Id, page.WidthMm.Value, page.HeightMm.Value,
                    configuration.Layout.Orientation,
                    rasterResolution.X, rasterResolution.Y, page.Borderless);
            }
            catch (Exception legacyError)
            {
                App.WriteStartupException(
                    "PSA user defaults were updated, but legacy DEVMODE synchronization failed",
                    legacyError);
                await SettingsApplyNotifications.ReportFailureAsync(printerName, legacyError);
            }
        }

        private async System.Threading.Tasks.Task UpdateInstanceAsync(PyToneRipConfiguration configuration)
        {
            if (instance == null) return;
            var registry = await PrinterInstanceStore.ReadAsync();
            var stored = FindStoredInstance(registry);
            if (stored == null) return;
            if (configuration.Connection.Kind != "usb")
                PrinterInstanceStore.EnsureNetworkHostAvailable(registry, configuration.Connection.Host, stored);
            var selected = PrinterBox.SelectedItem as PrinterCapability;
            stored.ModelId = configuration.Printer.Model;
            stored.ModelName = selected?.DisplayName ?? stored.ModelName;
            stored.ConnectionKind = configuration.Connection.Kind;
            stored.Host = configuration.Connection.Host;
            stored.Port = PrinterInstanceStore.RawTcpPort;
            stored.UsbDeviceId = configuration.Connection.UsbDeviceId;
            stored.UsbPort = configuration.Connection.UsbPort;
            stored.PhysicalQueueName = configuration.Connection.PhysicalQueueName;
            stored.Configured = true;
            stored.CalibrationMode = configuration.Calibration?.Enabled == true;
            await PrinterInstanceStore.WriteAsync(registry);
        }

        private static void VerifyCalibrationPersistence(
            PyToneRipConfiguration expected,
            string serialized,
            string stage)
        {
            if (expected?.Calibration?.Enabled != true) return;
            var actual = string.IsNullOrWhiteSpace(serialized)
                ? null : ConfigurationCodec.Deserialize(serialized)?.Calibration;
            if (actual?.Enabled != true
                || !string.Equals(actual.Mode, expected.Calibration.Mode,
                    StringComparison.OrdinalIgnoreCase)
                || actual.InkLimit != expected.Calibration.InkLimit)
            {
                throw new InvalidOperationException(
                    "RAW calibration was not preserved during " + stage + ".");
            }
        }

        private async System.Threading.Tasks.Task EnsureNetworkHostAvailableAsync(string host)
        {
            if (instance == null) return;
            var registry = await PrinterInstanceStore.ReadAsync();
            PrinterInstanceStore.EnsureNetworkHostAvailable(
                registry, host, FindStoredInstance(registry));
        }

        private PrinterInstanceRecord FindStoredInstance(PrinterInstanceRegistry registry)
        {
            // PrinterUri is the stable Windows identity and must be authoritative.
            var stored = registry.Instances.FirstOrDefault(item =>
                !string.IsNullOrWhiteSpace(printerUri)
                && string.Equals(item.PrinterUri, printerUri, StringComparison.OrdinalIgnoreCase));
            if (stored == null && !string.IsNullOrWhiteSpace(instance.SystemQueueId))
            {
                stored = registry.Instances.FirstOrDefault(item => string.Equals(
                    item.SystemQueueId, instance.SystemQueueId,
                    StringComparison.OrdinalIgnoreCase));
            }
            if (stored == null && !string.IsNullOrWhiteSpace(printerName))
            {
                stored = registry.Instances.FirstOrDefault(item => string.Equals(
                    item.QueueName, printerName, StringComparison.OrdinalIgnoreCase));
            }
            return stored;
        }

        private void Cancel(object sender, RoutedEventArgs args) => CompleteAndClose();

        private PyToneRipConfiguration BuildConfiguration()
        {
            var printer = Required<PrinterCapability>(PrinterBox, "registered printer");
            var displayedMode = RequiredOption(ModeBox, "mode");
            var media = RequiredOption(MediaBox, "media");
            var mode = ResolveModeForMedia(printer, media, displayedMode)
                ?? displayedMode;
            var page = RequiredOption(PageBox, "page");
            var input = RequiredOption(InputSourceBox, "input source");
            var calibrationEnabled = CalibrationEnabledBox.IsChecked == true;
            var dither = RequiredOption(DitherBox, "dither");
            if (AllowUnsupportedPpdCombinationsBox.IsChecked != true
                && page.AllowedMedia != null && page.AllowedMedia.Count > 0
                && !page.AllowedMedia.Any(id => string.Equals(
                    id, media.Id, StringComparison.OrdinalIgnoreCase)))
                throw new InvalidOperationException(
                    "Unsupported combination: " + page.DisplayName + " with "
                    + media.DisplayName + ". Select a compatible media type.");
            if (media.SupportedModes != null && media.SupportedModes.Count > 0 && !media.SupportedModes.Contains(mode.Id))
                throw new InvalidOperationException(media.Id + " does not support mode " + mode.Id + ".");
            if (!calibrationEnabled && string.IsNullOrWhiteSpace(Curve1Box.Text)) throw new InvalidOperationException("Curve 1 is required.");
            if (!calibrationEnabled && !string.IsNullOrWhiteSpace(Curve3Box.Text) && string.IsNullOrWhiteSpace(Curve2Box.Text))
                throw new InvalidOperationException("Curve 2 is required before Curve 3.");
            var curves = new[] { Curve1Box.Text, Curve2Box.Text, Curve3Box.Text }
                .Take(ActiveCurveCount()).Select(value => value.Trim()).ToList();
            var active = curves.Count;
            var inks = LogicalInks();
            if (inks.Count == 0) throw new InvalidOperationException("At least one logical ink is required.");
            var mapping = new Dictionary<string, string>();
            foreach (var ink in inks)
            {
                if (!routes.TryGetValue(ink, out string path))
                    throw new InvalidOperationException("Select a PhysicalPath for logical ink " + ink + ".");
                mapping[ink] = path;
            }
            var useUsb = UsbRadio.IsChecked == true;
            var usbDevice = UsbDeviceBox.SelectedItem as UsbPrinterDescriptor;
#if USB_PHYSICAL_QUEUE_FALLBACK
            var usbQueue = UsbPortBox.SelectedItem as string;
            var usbTransportMode = !string.IsNullOrWhiteSpace(usbQueue) ? "windows_queue" : "usbprint";
            if (useUsb && usbTransportMode == "windows_queue"
                && (usbDevice == null || !usbDevice.QueueNames.Contains(usbQueue)))
                throw new InvalidOperationException(LocalizationService.Get("Usb.QueueMissing"));
#else
            const string usbTransportMode = "usbprint";
#endif
            if (useUsb && usbDevice == null)
                throw new InvalidOperationException(LocalizationService.Get("Usb.SelectMatchingDevice"));
            string host;
            if (!PrinterInstanceStore.TryNormalizeIpv4(HostBox.Text, out host) && !useUsb)
                throw new InvalidOperationException("Network host must be a valid IPv4 address.");
            var sizing = RequiredString(SizingBox, "sizing");
            var placement = RequiredString(PlacementBox, "placement");
            var isOffset = string.Equals(placement, "offset", StringComparison.OrdinalIgnoreCase);
            return new PyToneRipConfiguration
            {
                Printer = new PrinterSelection { Model = printer.Id, Mode = mode.Id, Media = media.Id, Page = page.Id, InputSource = input.Id, Direction = RequiredString(DirectionBox, "direction"), AllowUnsupportedPpdCombinations = AllowUnsupportedPpdCombinationsBox.IsChecked == true, DeviceSettings = SelectedDeviceSettings(), InkConfiguration = SelectedInkConfiguration(), RasterResolutionX = (RasterResolutionBox.SelectedItem as RasterResolutionChoice)?.X, RasterResolutionY = (RasterResolutionBox.SelectedItem as RasterResolutionChoice)?.Y },
                Profiles = new ProfileSelection
                {
                    Curves = curves,
                    Blend = GetBlendGroup(0).Take(active).ToList(),
                    SplitTone = SplitToneSwitch.IsOn,
                    Highlights = GetBlendGroup(1).Take(active).ToList(),
                    Midtones = GetBlendGroup(2).Take(active).ToList(),
                    Shadows = GetBlendGroup(3).Take(active).ToList(),
                },
                Adjustments = new AdjustmentSelection { Gamma = (int)GammaSlider.Value, InkLimit = (int)InkLimitSlider.Value, Dither = dither.Id },
                Channels = new ChannelSelection { LogicalInks = inks, Routing = mapping },
                Layout = new LayoutSelection { Sizing = sizing, WidthMm = OptionalNumber(WidthBox.Text), HeightMm = OptionalNumber(HeightBox.Text), ScalePercent = string.Equals(sizing, "fit", StringComparison.OrdinalIgnoreCase) ? null : OptionalNumber(ScaleBox.Text), Orientation = RequiredString(OrientationBox, "orientation"), Placement = placement, XMm = isOffset ? OptionalNumber(XBox.Text) : null, YMm = isOffset ? OptionalNumber(YBox.Text) : null, Resampling = RequiredString(ResamplingBox, "resampling"), ApplyRipLayout = ApplyRipLayoutBox.IsChecked == true },
                Connection = new ConnectionSelection
                {
                    Kind = useUsb ? "usb" : "network", Host = host, Port = PrinterInstanceStore.RawTcpPort,
                    UsbDeviceId = usbDevice?.DeviceInstanceId ?? originalConfiguration?.Connection?.UsbDeviceId ?? instance?.UsbDeviceId,
                    UsbPort = usbDevice?.PortName ?? originalConfiguration?.Connection?.UsbPort ?? instance?.UsbPort,
#if USB_PHYSICAL_QUEUE_FALLBACK
                    PhysicalQueueName = usbQueue ?? originalConfiguration?.Connection?.PhysicalQueueName ?? instance?.PhysicalQueueName,
#else
                    PhysicalQueueName = originalConfiguration?.Connection?.PhysicalQueueName ?? instance?.PhysicalQueueName,
#endif
                    UsbTransportMode = useUsb ? usbTransportMode : null,
                },
                DeliveryMode = originalConfiguration?.DeliveryMode == "strict_offline"
                    ? "strict_offline" : "online",
                Calibration = new CalibrationSelection { Enabled = calibrationEnabled, Mode = SelectedCalibrationMode(), InkLimit = (int)CalibrationInkLimitSlider.Value },
            };
        }

        private static T Required<T>(ComboBox box, string name) where T : class => box.SelectedItem as T ?? throw new InvalidOperationException("Select " + name + ".");
        private static CapabilityOption RequiredOption(ComboBox box, string name) { var option = Required<CapabilityOption>(box, name); if (!option.Enabled) throw new InvalidOperationException(option.Status + ": " + (option.BlockedReason ?? option.Id)); return option; }
        private static string RequiredString(ComboBox box, string name) =>
            (box.SelectedItem as LocalizedValue)?.Value
            ?? throw new InvalidOperationException("Select " + name + ".");
        private List<string> LogicalInks() => (LogicalInksBox?.Text ?? "").Split(',').Select(value => value.Trim()).Where(value => value.Length > 0).Distinct().ToList();
        private static bool IsValidIpv4(string value) { if (string.IsNullOrWhiteSpace(value)) return false; var parts = value.Split('.'); return parts.Length == 4 && parts.All(part => part.Length > 0 && part.All(char.IsDigit) && byte.TryParse(part, NumberStyles.None, CultureInfo.InvariantCulture, out byte _)); }
        private static double? OptionalNumber(string text) { if (string.IsNullOrWhiteSpace(text)) return null; if (double.TryParse(text, NumberStyles.Float, CultureInfo.CurrentCulture, out double value)) return value; throw new InvalidOperationException("Layout values must be numbers."); }
        private static string Number(double? value) => value?.ToString(CultureInfo.CurrentCulture) ?? "";
        private void GammaChanged(object sender, Windows.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs args) { if (GammaLabel != null) GammaLabel.Text = ((int)args.NewValue).ToString(CultureInfo.InvariantCulture); }
        private void InkLimitChanged(object sender, Windows.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs args) { if (InkLimitLabel != null) InkLimitLabel.Text = ((int)args.NewValue).ToString(CultureInfo.InvariantCulture); }

        private void CompleteAndClose()
        {
            activationDeferral?.Complete();
            if (session != null) Window.Current.Close();
            else if (Frame?.CanGoBack == true) Frame.GoBack();
        }
    }
}
