# pyToneRIP

[![Release](https://img.shields.io/github/v/release/dfoxelf/pyToneRIP?include_prereleases&style=flat-square&v=215)](https://github.com/dfoxelf/pyToneRIP/releases)
[![Release downloads](https://img.shields.io/github/downloads/dfoxelf/pyToneRIP/total?style=flat-square&label=release%20downloads)](https://github.com/dfoxelf/pyToneRIP/releases)
[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2011%20x64-0078D4?style=flat-square)](#install)
[![Download Beta 1.0.0.215](https://img.shields.io/badge/Download-Beta%201.0.0.215-2ea44f?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.215-beta/pyToneRIP-Setup-1.0.0.215-Beta.exe)

Copyright © 2026 Alexander Eichhorn.

pyToneRIP is a monochrome Epson RIP for Windows 11. It contains the Python/Cython raster engine, a printer catalog, a Windows Print Support App, and an Inno Setup bootstrap installer.

## Download the beta

The current beta is **1.0.0.215**. [Download the setup installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.215-beta/pyToneRIP-Setup-1.0.0.215-Beta.exe).

<details>
<summary>Verify the installer checksum</summary>

Installer size: **56,889,820 bytes**. SHA-256:

```text
e779a45c1281cbc61b458a04b44dbadf9c091f4ad2e3cac2c0a6249d1ab2418b
```

```powershell
Get-FileHash .\pyToneRIP-Setup-1.0.0.215-Beta.exe -Algorithm SHA256
```

</details>

This beta focuses on print placement, model capabilities, calibration integrity, and print settings.

- **Print placement:** corrected page origins for the Stylus Photo R2880 and SureColor P900, including sheet, roll, and borderless paths. Regression checks compare absolute raster bounds and native commands with independent reference captures.
- **Model settings:** refined P700/P900 resolution and weave selection, and exposed supported controls such as paper thickness, platen gap, and roll-paper saving for the relevant models.
- **Copies:** added collation capability and handling of collated and uncollated multipage jobs.
- **Calibration:** request lossless PNG images from the Windows GDI-to-XPS converter to avoid JPEG damage to encoded RGB calibration targets. Application color conversion and resampling can still change target values.
- **Interface:** localized additional printer settings in 27 languages, clarified printer IPv4 entry, and corrected text colors while editing in dark themes.
- **Processing and source packages:** removed redundant TIFF decoding and excluded generated printer resources and old development generators from the source release.

The placement checks are software regressions; physical output on every supported printer has not been verified. This remains a beta release.

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
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-ModelPackageSet.ps1 -Version 1.0.0.215
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-InnoSetup.ps1 -Version 1.0.0.215
```

Use a new four-part version for each build. The first script compiles Cython, freezes the Python runtime, builds and signs the Manager MSIX, and checks the result. The second produces `windows_psa/artifacts/pyToneRIP-Setup-<version>-Beta.exe`.

On first use, the package builder creates a self-signed development certificate and its random password in `windows_psa/signing-local`. This directory is ignored by Git. Keep the PFX and password together and do not commit or share them. To reuse an existing signing identity outside the checkout, pass `-SigningRoot 'C:\path\to\private-signing-dir'` to `Build-ModelPackageSet.ps1`.

The bootstrap EXE is not Authenticode signed. Windows may show a publisher or SmartScreen warning during beta testing. The MSIX inside is signed with the local development certificate; the installer imports its public certificate into the local machine's Trusted People store.

## Install

Run the [pyToneRIP 1.0.0.215 Beta installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.215-beta/pyToneRIP-Setup-1.0.0.215-Beta.exe) on Windows 11 build 26100 or later. The installer requests administrator approval to trust the package certificate and install the Manager. Start pyToneRIP and add a printer through its interface.

For a package without the bootstrap installer, open PowerShell as Administrator and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\artifacts\pyToneRIP-ModuleSet-1.0.0.215\Install-ModuleSet-Manager.ps1
```

## Source layout

- `src/xp750_rip`: print engine, calibration, and package generator.
- `printer_catalog`: printer capabilities and vendor PPDs.
- `windows_psa`: Windows app, build scripts, and installer.

The project is licensed under GPL 3.0 only unless a file says otherwise; see [LICENSE](LICENSE).
