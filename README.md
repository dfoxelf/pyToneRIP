# pyToneRIP

[![Prerelease](https://img.shields.io/github/v/release/dfoxelf/pyToneRIP?include_prereleases&style=flat-square&label=prerelease&v=270)](https://github.com/dfoxelf/pyToneRIP/releases/tag/v1.0.0.270-beta)
[![Release downloads](https://img.shields.io/github/downloads/dfoxelf/pyToneRIP/total?style=flat-square&label=release%20downloads&v=270)](https://github.com/dfoxelf/pyToneRIP/releases)
[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2011%20x64-0078D4?style=flat-square)](#install)
[![Download Beta 1.0.0.270](https://img.shields.io/badge/Download-Beta%201.0.0.270-2ea44f?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.270-beta/pyToneRIP-Setup-1.0.0.270-Beta.exe)

Copyright © 2026 Alexander Eichhorn.

pyToneRIP is a monochrome Epson raster image processor for Windows 11. Print from Windows applications, choose your grayscale `.quad` curves, and control how the image is rendered and delivered to the printer. The project includes a Python/Cython raster engine, a model-specific printer catalog, a Windows Print Support App, and a bootstrap installer.

## Download the beta

The current beta is **1.0.0.270**. [Download the setup installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.270-beta/pyToneRIP-Setup-1.0.0.270-Beta.exe).

This is a **prerelease** for Windows 11 x64, build 26100 or later. Printer support and the available settings depend on the selected model. Start with a small test print when using a new printer, paper, curve set, or print mode.

<details>
<summary>Verify the installer checksum</summary>

Installer size: **69,614,050 bytes**. SHA-256:

```text
cbdd3eb5e166b63212e4ca6f16c4f855c0bbadfc1ba750d3301783e3a722c4b8
```

```powershell
Get-FileHash .\pyToneRIP-Setup-1.0.0.270-Beta.exe -Algorithm SHA256
```

</details>

## What's new in beta 270

### Find and connect your Epson printer

- **Network.** Search for supported Epson printers on your local network. Choose a result by model and IP address, and pyToneRIP fills in the connection. When switching an existing printer to Network, search results are limited to its model. Manual IP entry is still available.
- **Windows queue (RAW).** Use an existing Epson queue with the official Epson driver when one is installed. Continue printing from the pyToneRIP virtual printer. pyToneRIP applies the selected curves and sends the rendered printer data through the Epson queue.
- **Direct USB.** Connect directly when the official Epson driver is absent. Printer setup now opens with Network selected.

The built-in Epson V4 class driver can handle ordinary Windows printing while being incompatible with this RAW path. pyToneRIP detects the affected drivers and tells you to install the official driver. Network discovery requires local reachability and may miss printers across routed networks or virtual-machine NAT.

### WorkForce and printer maintenance

- Extended tuned WorkForce printhead geometry and page offsets to related profiles. **Super Photo Fine 2400 x 1200 dpi PyToneRIP** is available for WF-3620, WF-3640, WF-7110, WF-7610 and WF-7620. Physical validation varies by model.
- Added a catalog profile for **WF-4820**. Physical output for this model still needs validation.
- Improved repeated USB ink readings and offline-state reporting for Windows queues. Ink readings can use direct USB when a supported Windows queue cannot return them and the matching USB device is available.
- Nozzle checks and supported maintenance commands follow the configured connection. For supported local USB queues using the built-in Epson class driver, a nozzle check can use direct USB.

### Windows integration

- Queue commands now use Windows App Services for the normal request path. Opening Windows print jobs and receiving printer status can proceed independently.
- Improved recovery when applying legacy Windows printer defaults. If the helper does not reply in time, pyToneRIP reads back the settings before flagging the write as unconfirmed.
- Improved upgrade and uninstall handling, including guidance to close the old app and cleanup of app-owned virtual-printer state.

## Monochrome printing controls

- Select grayscale `.quad` channel curves for your ink set and paper.
- Choose the model's supported paper, size, physical feed source, print mode and borderless options.
- Use the available dithering, resampling and ink controls; calibration printing provides a separate workflow for evaluating channels.
- Use the optional 16-bit processing path for suitable source data. Enabling it cannot recover tonal information missing from an 8-bit source.
- Prepare large pages with bounded working buffers and buffered USB delivery where the selected transport supports it.

The printer catalog includes desktop Stylus Photo and Expression models, EcoTank and WorkForce printers, Stylus Pro wide-format models and SureColor families. A catalog entry is not a claim that every paper/mode/feed combination has been physically tested. Consult the settings available for the selected model.

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

1. Run the [pyToneRIP 1.0.0.270 Beta installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.270-beta/pyToneRIP-Setup-1.0.0.270-Beta.exe) on Windows 11 x64 build 26100 or later.
2. Approve the administrator request to trust the package certificate and install the Manager.
3. Start pyToneRIP, add your printer, and select its paper, physical feed, size, mode and `.quad` curves.
4. Enable Soft Proof if you want to inspect and approve jobs before transmission, then print through the Windows printer queue from your application.

The bootstrap EXE is not Authenticode signed. Windows may show a publisher or SmartScreen warning during beta testing. The MSIX inside is signed with a development certificate; the installer imports its public certificate into the local machine's Trusted People store.

For a package without the bootstrap installer, open PowerShell as Administrator and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\artifacts\pyToneRIP-ModuleSet-1.0.0.270\Install-ModuleSet-Manager.ps1
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
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-ModelPackageSet.ps1 -Version 1.0.0.270
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-InnoSetup.ps1 -Version 1.0.0.270
```

Use a new four-part version for each build. The first script compiles Cython, freezes the Python runtime, builds and signs the Manager MSIX, and checks the result. The second creates `windows_psa/artifacts/pyToneRIP-Setup-<version>-Beta.exe`.

On first use, the package builder creates a self-signed development certificate and a random password in `windows_psa/signing-local`. This directory is ignored by Git. Keep its PFX and password together and do not commit or share them. To reuse an existing signing identity outside the checkout, pass `-SigningRoot 'C:\path\to\private-signing-dir'` to `Build-ModelPackageSet.ps1`.

## Source layout and license

- `src/xp750_rip`: raster engine, printer command generation, calibration and package generation.
- `printer_catalog`: source printer capabilities, runtime descriptors and PPDs.
- `windows_psa`: Windows app, build scripts and installer.
- `licenses`: third-party license notices.

The project is licensed under GPL 3.0 only unless a file says otherwise; see [LICENSE](LICENSE).
