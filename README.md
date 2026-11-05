# pyToneRIP

[![Release](https://img.shields.io/github/v/release/dfoxelf/pyToneRIP?include_prereleases&style=flat-square&v=248)](https://github.com/dfoxelf/pyToneRIP/releases)
[![Release downloads](https://img.shields.io/github/downloads/dfoxelf/pyToneRIP/total?style=flat-square&label=release%20downloads&v=248)](https://github.com/dfoxelf/pyToneRIP/releases)
[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2011%20x64-0078D4?style=flat-square)](#install)
[![Download Beta 1.0.0.248](https://img.shields.io/badge/Download-Beta%201.0.0.248-2ea44f?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.248-beta/pyToneRIP-Setup-1.0.0.248-Beta.exe)

Copyright © 2026 Alexander Eichhorn.

pyToneRIP is a monochrome Epson raster image processor for Windows 11. Print from Windows applications, choose your grayscale `.quad` curves, and control how the image is rendered and delivered to the printer. The project includes a Python/Cython raster engine, a model-specific printer catalog, a Windows Print Support App, and a bootstrap installer.

## Download the beta

The current beta is **1.0.0.248**. [Download the setup installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.248-beta/pyToneRIP-Setup-1.0.0.248-Beta.exe).

This is a **prerelease** for Windows 11 x64, build 26100 or later. Printer support and the available settings depend on the selected model. Start with a small test print when using a new printer, paper, curve set, or print mode.

<details>
<summary>Verify the installer checksum</summary>

Installer size: **68,758,557 bytes**. SHA-256:

```text
b7259cf4e2b4c74dd46c2c59ee02947b42effe26debdf018dd5fecaff23a0475
```

```powershell
Get-FileHash .\pyToneRIP-Setup-1.0.0.248-Beta.exe -Algorithm SHA256
```

</details>

## Changes in beta 248

The changes below describe **1.0.0.248 Beta**, relative to the previous public beta 244.

### New ET-18100 / L18050 / L8050 family

- Added Epson EcoTank ET-18100, Epson L18050 and Epson L8050 as separate printer entries, with their own cartridge-channel names and model capabilities.
- Imported paper choices, paper selectors, print modes, drop-size levels and their output codes, page dimensions, margins, and available borderless formats. ET-18100/L18050 expose A3+; L8050 has its own A4-class page set.
- Kept the input-raster resolution, printer coordinate grid and advertised mode resolution separate. Mode-dependent drop levels are applied without treating color-correction tables as ink-coverage multipliers.
- Added the paper-feed choices described by this family. The presence of a roll/continuous output mode in the RIP does not establish that a printer has a factory roll holder; check the physical setup before using it.

### Model-specific pass scheduling and coordinate fixes

- **P50 / T50 / T60:** corrected leading/trailing pass planning and the use of the existing top margin. The initial head-position offset participates in scheduling instead of being added after the edge passes have been planned. The P50 improvement at the beginning of a physical print was confirmed by the user.
- **Stylus Photo R series:** corrected trailing-edge planning where applicable, retaining each model's established origin, nozzle offsets and feed geometry. R2880 retains its own initial-feed handling.
- **ET-14000 / ET-14100:** replaced incompatible imported geometry with their own head/feed profiles and corrected the trailing scheduling limit for staggered channels.
- **ET-8500 / ET-8550 / L8160 / L8180:** corrected mixed-resolution nozzle spacing, the model-specific sheet/borderless starting position and the horizontal subpass phase of the affected Super modes.
- **ET-18100 / L18050 / L8050 and F500:** moved their own initial-feed offsets into edge planning and allowed the required head travel at the end of the image.
- **T3200 / T5200 / T7200:** use the verified firmware-weave, single-row transport path with mode-dependent coordinate units. Raster preparation uses the transport grid before dithering.
- **F500:** corrected mode-dependent drop-code depth, command length and mixed-resolution geometry.

These changes use explicit model/family conditions. The P50 correction is not applied universally to SureColor, EcoTank or Stylus Pro printers. Coordinate reconstruction checks distinguish changes in pass order from deliberate corrections to previously incorrect origins or pixel phases. The 8-bit and 16-bit input-processing paths remain independent of the output drop-code depth.

### SureColor T5400 catalog and output

T5400 now uses its own model description instead of an incompatible inherited profile. The catalog contains **20 paper entries, 36 mode rows and 23 fixed page sizes**, with verified paper-selector codes, mode-dependent coordinate grids and drop-code depths. Physical feed choices are Tray, Manual Feed, Roll Paper without cutting, and Roll Paper with cutting.

Paper handling, roll cutting and the page origin follow this model's commands. Settings inherited from an unrelated printer have been removed. Existing T5400 configurations may need their paper and mode selected again. T5400's secondary nozzle banks and complete native pass distribution are not implemented; physical quality and throughput still need testing.

### USB discovery and cancellation

- Improved USB model matching for devices whose reported name repeats the Epson manufacturer prefix, including the P50 discovery case.
- Added an explicit abort/finalization sequence for direct USB cancellation on P50/T50/T60 and ET-18100/L18050/L8050. Sheet jobs receive the verified sheet-eject sequence after the abort; roll jobs do not receive an added form feed.
- Preserved sender-thread ownership, partial-write handling, bounded cancellation waits and the original failure cause. Successful jobs do not receive cancellation commands.

The USB cancellation sequence is covered by transport tests; physical sheet ejection after cancellation still needs confirmation on the affected devices.

### Preview cropping, fit and placement

- Added white crop handles at the middle of the four photo edges. Drag these to crop the visible image; the corner handles continue to resize it.
- Added **Fill Photo to Page**, alongside **Fit Photo to Page**, as toolbar icons with localized tooltips. Fit keeps the whole photo visible; Fill covers the page using an aspect-preserving crop. Fit Page remains a separate view/zoom action.
- Preserved crop geometry through resizing, rotation, settings updates, rendering and edited-job output. Cropping retains the original image resources rather than replacing a 16-bit image with an 8-bit preview.
- Preserved the Photo Placement selection when Soft Proof is toggled or the preview is rebuilt.

## Monochrome printing controls

- Select grayscale `.quad` channel curves for your ink set and paper.
- Choose the model's supported paper, size, physical feed source, print mode and borderless options.
- Use the available dithering, resampling and ink controls; calibration printing provides a separate workflow for evaluating channels.
- Use the optional 16-bit processing path for suitable source data. Enabling it cannot recover tonal information missing from an 8-bit source.
- Prepare large pages with bounded working buffers and buffered USB delivery where the selected transport supports it.

The printer catalog includes desktop Stylus Photo and Expression models, EcoTank printers, Stylus Pro wide-format models and SureColor families. A catalog entry is not a claim that every paper/mode/feed combination has been physically tested. Consult the settings available for the selected model.

## Soft Proof preview and viewing simulation

Enable the optional preview in printer preferences to inspect a Windows print job before approving transmission. You can approve it, use Print and Close, or cancel the job and close the preview.

### Inspect and arrange the page

The preview includes page thumbnails, printer/paper/mode/curve/ICC details, the selected dithering algorithm, numerical zoom, Ctrl + mouse-wheel zoom, page fitting, and rulers in millimetres or inches. Rulers are enabled by default. A leading-edge marker shows where printing starts for portrait and landscape pages.

Photo Placement supports mouse dragging, corner resizing, edge cropping, 90-degree rotation, fitting or filling the page, and horizontal/vertical centering. Open the job settings from the preview to change supported print options and then return to the page. Preview controls and explanations are localized in all 27 application languages.

### Compare paper and viewing light

Use a measured print ICC profile when one is available, or explore the built-in paper and viewing-light simulations without selecting a profile. The paper models use measurement data grouped by paper category to estimate paper white and black levels. Compare paper with and without optical brighteners under D50, D55, D60 and D65 viewing light, with chromatic adaptation accounting for the lighting change.

These controls provide a practical visual comparison of paper tone, contrast and lighting choices. For a specific measured printing condition, choose its ICC profile. Preview rendering uses the system display-color settings. The ICC, paper/OBA and viewing-light controls affect the preview; they do not replace or modify the `.quad` curves used to print.

## Beta validation and current scope

Validation combines targeted automated checks, independently decoded printer streams and physical prints on selected setups. Pass order is implemented by this project; matching every pass of another implementation is not claimed.

The latest edge-scheduling audit covers selected full-page cases for 26 models. Other catalog entries still need separate full-page qualification. Confirmed physical removal of the reported leading-edge bands applies to the tested P50 setup; stream checks on other models do not substitute for physical print testing.

Print progress covers preparation and transmission. Confirmation of the final physical sheet state depends on the status available from the printer. Each preview approves one Windows print job; start another job to print again after completion.

Violet printing in wide-format Commercial Edition models is not implemented. Roll/borderless operation and additional printer/paper combinations continue to require hardware validation. The previously reported P900 configuration issue on a tester's machine is not confirmed resolved. Support reports are generated locally and are not uploaded automatically; review them before sharing.

## Install

1. Run the [pyToneRIP 1.0.0.248 Beta installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.248-beta/pyToneRIP-Setup-1.0.0.248-Beta.exe) on Windows 11 x64 build 26100 or later.
2. Approve the administrator request to trust the package certificate and install the Manager.
3. Start pyToneRIP, add your printer, and select its paper, physical feed, size, mode and `.quad` curves.
4. Enable Soft Proof if you want to inspect and approve jobs before transmission, then print through the Windows printer queue from your application.

The bootstrap EXE is not Authenticode signed. Windows may show a publisher or SmartScreen warning during beta testing. The MSIX inside is signed with a development certificate; the installer imports its public certificate into the local machine's Trusted People store.

For a package without the bootstrap installer, open PowerShell as Administrator and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\artifacts\pyToneRIP-ModuleSet-1.0.0.248\Install-ModuleSet-Manager.ps1
```

## Build requirements

- Windows 11 x64, build 26100 or later.
- Python 3.14 x64 and [uv](https://docs.astral.sh/uv/).
- Visual Studio 2022 Build Tools with MSVC x64, UWP build tools and MSBuild.
- Windows 11 SDK with `signtool.exe` and `rc.exe`.
- Inno Setup 6 for the bootstrap installer.

The build reads printer packages and their PPDs from `printer_catalog`; a sibling source checkout is not required. Included Epson ESC/P-R PPDs for XP-750, XP-760 and XP-960 are licensed under GPL 2.0 or later; see [the Epson PPD license](licenses/Epson-escpr-GPL-2.0.txt).

## Build the package and installer

Run PowerShell from the repository root:

```powershell
uv sync --locked
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-ModelPackageSet.ps1 -Version 1.0.0.248
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-InnoSetup.ps1 -Version 1.0.0.248
```

Use a new four-part version for each build. The first script compiles Cython, freezes the Python runtime, builds and signs the Manager MSIX, and checks the result. The second creates `windows_psa/artifacts/pyToneRIP-Setup-<version>-Beta.exe`.

On first use, the package builder creates a self-signed development certificate and a random password in `windows_psa/signing-local`. This directory is ignored by Git. Keep its PFX and password together and do not commit or share them. To reuse an existing signing identity outside the checkout, pass `-SigningRoot 'C:\path\to\private-signing-dir'` to `Build-ModelPackageSet.ps1`.

## Source layout and license

- `src/xp750_rip`: raster engine, printer command generation, calibration and package generation.
- `printer_catalog`: source printer capabilities, runtime descriptors and PPDs.
- `windows_psa`: Windows app, build scripts and installer.
- `licenses`: third-party license notices.

The project is licensed under GPL 3.0 only unless a file says otherwise; see [LICENSE](LICENSE).
