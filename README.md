# pyToneRIP

[![Release](https://img.shields.io/github/v/release/dfoxelf/pyToneRIP?include_prereleases&style=flat-square&v=219)](https://github.com/dfoxelf/pyToneRIP/releases)
[![Release downloads](https://img.shields.io/github/downloads/dfoxelf/pyToneRIP/total?style=flat-square&label=release%20downloads)](https://github.com/dfoxelf/pyToneRIP/releases)
[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2011%20x64-0078D4?style=flat-square)](#install)
[![Download Beta 1.0.0.219](https://img.shields.io/badge/Download-Beta%201.0.0.219-2ea44f?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.219-beta/pyToneRIP-Setup-1.0.0.219-Beta.exe)

Copyright © 2026 Alexander Eichhorn.

pyToneRIP is a monochrome Epson RIP for Windows 11. It contains the Python/Cython raster engine, a printer catalog, a Windows Print Support App, and an Inno Setup bootstrap installer.

## Download the beta

The current beta is **1.0.0.219**. [Download the setup installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.219-beta/pyToneRIP-Setup-1.0.0.219-Beta.exe).

<details>
<summary>Verify the installer checksum</summary>

Installer size: **56,995,970 bytes**. SHA-256:

```text
898cccbbecc9bb9597da77c8617bfc5e290343aa17a4b709caea0dfb1df51b59
```

```powershell
Get-FileHash .\pyToneRIP-Setup-1.0.0.219-Beta.exe -Algorithm SHA256
```

</details>

This rebuilt 1.0.0.219 beta corrects P900 K/MK channel handling and retains the original USB transport. It also includes the streaming dithering, multipage processing, printer capability, and Windows print settings changes listed below.

- **Dithering:** added X2Y1 with tone-dependent threshold modulation and serpentine traversal, plus Coupled ED variants for C/M and C/M + LC/LM. Preferences shows variants compatible with the selected printer's ink channels. The implementations retain state across streaming bands and handle blank regions.
- **Multipage streaming:** the live transport can receive the first page while later pages are still being processed. Job configuration and curve snapshots are prepared once; additional copies reuse rendered data.
- **Windows rasterization:** added 360 and 720 DPI input raster choices for every model, alongside 300 and 600 DPI. These settings are separate from the printer's physical print resolution.
- **Printer capabilities:** added the P800 720 x 1440 photo mode and restored missing Stylus Pro 3880 sheet formats, including 4 x 6 inches. The 3880 catalog now contains 25 sheet formats; the model does not support roll feed.
- **P900 QUAD channels:** retain separate logical K and MK curves from ten-channel profiles, with independent physical routing and simultaneous output on either glossy or matte paper. Restore MK in older automatic channel lists while preserving user assignments.
- **Print settings and diagnostics:** corrected application of legacy per-user defaults in Preferences and expanded support-report collection of PrintTickets, DEVMODE settings, and Windows print/security configuration.
- **Protocol support:** extended native command encoding and validation for explicitly supported mode variants.

These changes have software regression coverage. Print quality, uninterrupted USB delivery, and physical output on every supported printer have not been verified. This remains a beta release.

## Support development and professional services

pyToneRIP is developed independently. Financial support helps sustain development, printer testing, and support for more models.

Paid printer setup, paper calibration, technical support, and sponsored development are available by arrangement. Companies interested in further development can contact Alexander Eichhorn at [df11@mail.de](mailto:df11@mail.de). The project remains open source under GPL 3.0.

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
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-ModelPackageSet.ps1 -Version 1.0.0.219
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-InnoSetup.ps1 -Version 1.0.0.219
```

Use a new four-part version for each build. The first script compiles Cython, freezes the Python runtime, builds and signs the Manager MSIX, and checks the result. The second produces `windows_psa/artifacts/pyToneRIP-Setup-<version>-Beta.exe`.

On first use, the package builder creates a self-signed development certificate and its random password in `windows_psa/signing-local`. This directory is ignored by Git. Keep the PFX and password together and do not commit or share them. To reuse an existing signing identity outside the checkout, pass `-SigningRoot 'C:\path\to\private-signing-dir'` to `Build-ModelPackageSet.ps1`.

The bootstrap EXE is not Authenticode signed. Windows may show a publisher or SmartScreen warning during beta testing. The MSIX inside is signed with the local development certificate; the installer imports its public certificate into the local machine's Trusted People store.

## Install

Run the [pyToneRIP 1.0.0.219 Beta installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.219-beta/pyToneRIP-Setup-1.0.0.219-Beta.exe) on Windows 11 build 26100 or later. The installer requests administrator approval to trust the package certificate and install the Manager. Start pyToneRIP and add a printer through its interface.

For a package without the bootstrap installer, open PowerShell as Administrator and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\artifacts\pyToneRIP-ModuleSet-1.0.0.219\Install-ModuleSet-Manager.ps1
```

## Source layout

- `src/xp750_rip`: print engine, calibration, and package generator.
- `printer_catalog`: printer capabilities and vendor PPDs.
- `windows_psa`: Windows app, build scripts, and installer.

The project is licensed under GPL 3.0 only unless a file says otherwise; see [LICENSE](LICENSE).
