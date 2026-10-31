# pyToneRIP

[![Release](https://img.shields.io/github/v/release/dfoxelf/pyToneRIP?include_prereleases&style=flat-square&v=244)](https://github.com/dfoxelf/pyToneRIP/releases)
[![Release downloads](https://img.shields.io/github/downloads/dfoxelf/pyToneRIP/total?style=flat-square&label=release%20downloads&v=244)](https://github.com/dfoxelf/pyToneRIP/releases)
[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2011%20x64-0078D4?style=flat-square)](#install)
[![Download Beta 1.0.0.244](https://img.shields.io/badge/Download-Beta%201.0.0.244-2ea44f?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.244-beta/pyToneRIP-Setup-1.0.0.244-Beta.exe)

Copyright © 2026 Alexander Eichhorn.

pyToneRIP is a monochrome Epson RIP for Windows 11. It contains the Python/Cython raster engine, a printer catalog, a Windows Print Support App, and an Inno Setup bootstrap installer.

## Download the beta

The current beta is **1.0.0.244**. [Download the setup installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.244-beta/pyToneRIP-Setup-1.0.0.244-Beta.exe).

<details>
<summary>Verify the installer checksum</summary>

Installer size: **68,670,303 bytes**. SHA-256:

```text
5abf8688e362691d204357b207a2cb6e5d901dbe89029299393b83c658e5d59f
```

```powershell
Get-FileHash .\pyToneRIP-Setup-1.0.0.244-Beta.exe -Algorithm SHA256
```

</details>

## Changes in beta 244

Changes below are relative to public beta 229.

- **Optional Soft Proof preview:** enable the preview in printer settings, inspect the pages, and approve or cancel the print job before transmission. Use a print ICC profile or the built-in paper and viewing-light simulations to compare paper appearance with and without optical brighteners under D50–D65 lighting. The preview uses the system display-color settings. These options affect the preview, not the selected `.quad` printing curves.
- **Page inspection and placement:** page thumbnails, printer/paper/mode/curve/ICC details, the selected dithering algorithm, zoom controls and Ctrl + mouse wheel, a numerical zoom, rulers in millimetres or inches, and an indication of the leading print edge. Photo Placement supports dragging, resizing, rotation, fitting the photo to the sheet, and horizontal/vertical centering. Fit Page controls the view separately. Rulers are enabled by default.
- **Settings and print controls in preview:** edit the job settings and return to the preview, print, print and close, or cancel and close. The cancel/close button explains its action in a tooltip. Preview controls and explanations are localized in all 27 application languages.
- **Printing after preview edits:** XPS rewriting now uses a forward-only ZIP-writing path compatible with the Windows conversion stream, addressing the `NotImplementedException` after updating an edited job. Placement/settings refresh fixes preserve the Photo Placement selection, orientation and OBA settings, avoid misleading rotation errors, and reduce visible placement rollback. The 8-bit and 16-bit processing paths remain independent.
- **New printers:** Epson Stylus Photo P50, T50 and T60 are available as separate models using their shared printer class, including the implemented paper and borderless capabilities.
- **Epson 1500W / Artisan 1430:** restored per-pass drying control, roll-feed capability and a 13-inch roll format, with corrected model-specific paper, borderless and native-coordinate handling. Roll and borderless combinations still require physical testing on the target printer.
- **Wide-format printers:** P6000/P8000 and P7000/P9000 have corrected sheet-feed commands, explicit Photo Black/Matte Black selection, more accurate metric page dimensions, and corrected left-origin handling. Commercial Edition no longer advertises an LLK channel in place of unsupported Violet. Additional P7000/P9000 media selectors are available; entries transferred within a family are distinguished from independently captured model data in the source.
- **Paper names and thickness:** 7890/9890 and P6000/P8000 show their shared glossy choice as Epson Premium Glossy / Satin. Sheet and roll media selectors are handled separately where their descriptors require it. Wide-format P5000 through P20000 variants and 7890/9890 offer the complete 0.1–1.5 mm thickness range in 0.1 mm steps, with existing saved setting IDs retained. The green channel marker recognizes the `GR` name.
- **Other fixes:** restored A6 combinations for XP-750, improved the available space in preview settings and localized Manager toolbars, and expanded local support-report evidence for Windows capability and settings failures.

Experimental 16-bit processing, bounded large-page preparation, buffered USB delivery, paired-channel dithering, and independent resampling controls remain available. An 8-bit source cannot regain missing tonal information simply by enabling 16-bit processing.

## Soft Proof preview and viewing simulation

Explore paper appearance before printing, with or without a dedicated print ICC profile. The built-in paper models use measurements grouped by paper category to estimate paper white and black levels. Select paper with or without optical brighteners and compare D50, D55, D60 and D65 viewing light, with chromatic adaptation accounting for the change in illumination.

These controls provide a practical visual comparison of paper tone, contrast and lighting choices even when a print ICC profile is unavailable. For a measured printing condition, select its ICC profile. Both approaches use the system display-color settings and leave the selected `.quad` printing curves unchanged.

## Beta status and printer support

This is a **prerelease**. Validation includes automated checks, native-stream comparisons and physical testing on selected printer setups. Wide-format vertical page geometry follows the previously tested 7890 path. Additional printer, paper, feed and borderless combinations continue to be validated. Violet printing in Commercial Edition is not implemented.

Print progress covers preparation and transmission; confirmation of physical sheet completion depends on available printer status. Each preview approves one Windows print job; start a new job to print again after completion.

The P900 catalog has 21 physical sheet sizes and 7 named borderless variants. Five sizes present in the Epson driver remain omitted: 5 x 8 inch, 16:9 Wide (101.6 x 180.6 mm), 11 x 14 inch, 12 x 12 inch and 16 x 20 inch. The original P900 configuration failure on the tester's machine is not confirmed fixed; recovery and diagnostic improvements still need confirmation there. Support reports are created locally and are not uploaded automatically; review them before sharing.

## Build requirements

- Windows 11 x64, build 26100 or later.
- Python 3.14 x64 and [uv](https://docs.astral.sh/uv/).
- Visual Studio 2022 Build Tools with MSVC x64, UWP build tools and MSBuild.
- Windows 11 SDK with `signtool.exe` and `rc.exe`.
- Inno Setup 6 for the bootstrap installer.

The build reads printer packages and their PPDs from `printer_catalog`. It does not require a sibling source checkout. The included Epson ESC/P-R PPDs for XP-750, XP-760, and XP-960 are licensed under GPL 2.0 or later; see `licenses/Epson-escpr-GPL-2.0.txt`.

## Build the package and installer

Run PowerShell from the repository root:

```powershell
uv sync --locked
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-ModelPackageSet.ps1 -Version 1.0.0.244
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-InnoSetup.ps1 -Version 1.0.0.244
```

Use a new four-part version for each build. The first script compiles Cython, freezes the Python runtime, builds and signs the Manager MSIX, and checks the result. The second produces `windows_psa/artifacts/pyToneRIP-Setup-<version>-Beta.exe`.

On first use, the package builder creates a self-signed development certificate and its random password in `windows_psa/signing-local`. This directory is ignored by Git. Keep the PFX and password together and do not commit or share them. To reuse an existing signing identity outside the checkout, pass `-SigningRoot 'C:\path\to\private-signing-dir'` to `Build-ModelPackageSet.ps1`.

The bootstrap EXE is not Authenticode signed. Windows may show a publisher or SmartScreen warning during beta testing. The MSIX inside is signed with the local development certificate; the installer imports its public certificate into the local machine's Trusted People store.

## Install

Run the [pyToneRIP 1.0.0.244 Beta installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.244-beta/pyToneRIP-Setup-1.0.0.244-Beta.exe) on Windows 11 build 26100 or later. The installer requests administrator approval to trust the package certificate and install the Manager. Start pyToneRIP and add a printer through its interface.

For a package without the bootstrap installer, open PowerShell as Administrator and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\artifacts\pyToneRIP-ModuleSet-1.0.0.244\Install-ModuleSet-Manager.ps1
```

## Source layout

- `src/xp750_rip`: print engine, calibration, and package generator.
- `printer_catalog`: printer capabilities and vendor PPDs.
- `windows_psa`: Windows app, build scripts, and installer.

The project is licensed under GPL 3.0 only unless a file says otherwise; see [LICENSE](LICENSE).
