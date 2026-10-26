# pyToneRIP

[![Release](https://img.shields.io/github/v/release/dfoxelf/pyToneRIP?include_prereleases&style=flat-square&v=224)](https://github.com/dfoxelf/pyToneRIP/releases)
[![Release downloads](https://img.shields.io/github/downloads/dfoxelf/pyToneRIP/total?style=flat-square&label=release%20downloads&v=224)](https://github.com/dfoxelf/pyToneRIP/releases)
[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2011%20x64-0078D4?style=flat-square)](#install)
[![Download Beta 1.0.0.224](https://img.shields.io/badge/Download-Beta%201.0.0.224-2ea44f?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.224-beta/pyToneRIP-Setup-1.0.0.224-Beta.exe)

Copyright © 2026 Alexander Eichhorn.

pyToneRIP is a monochrome Epson RIP for Windows 11. It contains the Python/Cython raster engine, a printer catalog, a Windows Print Support App, and an Inno Setup bootstrap installer.

## Download the beta

The current beta is **1.0.0.224**. [Download the setup installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.224-beta/pyToneRIP-Setup-1.0.0.224-Beta.exe).

<details>
<summary>Verify the installer checksum</summary>

Installer size: **73,821,554 bytes**. SHA-256:

```text
dfa60090734a28f9feeea9c6355eb457b48641ce84fbe395c35c9dfcc160e2bf
```

```powershell
Get-FileHash .\pyToneRIP-Setup-1.0.0.224-Beta.exe -Algorithm SHA256
```

</details>

This beta adds an experimental high-precision monochrome path and improves how large pages are prepared and sent to the printer. It includes the changes developed since the public 219 beta.

- **Experimental 16-bit processing:** the new checkbox in Printer Preferences enables higher-precision tone, curve, resampling, and dithering calculations across supported models. Windows print jobs use a dedicated high-precision XPS renderer instead of the usual 8-bit converter. An 8-bit source, or an application that has already reduced its precision, cannot regain missing tones by enabling this option. Leave it off for the established 8-bit path when comparing results.
- **Large-format printing:** page preparation, resolution conversion, and raster processing now work with bounded strips and row caches instead of allocating an entire image at printer resolution. This reduces peak memory and temporary-file demands on A1/A0 and larger jobs. Windows input rasterization can still create a full source TIFF in 8-bit mode; sufficient free disk space is still required.
- **USB delivery:** a bounded buffer and a separate sender allow the RIP to prepare data while USB writes are in progress. Initial buffering helps absorb short processing pauses; it cannot guarantee continuous head movement if rendering or the connection is too slow. Cancellation and transfer diagnostics have also been improved.
- **Dithering and page completion:** corrected the paired-channel error-diffusion calculations and improved handling of blank areas and the page tail. These changes address uneven paired-ink output and unnecessary processing delays near the end of a page.
- **Resampling controls:** choose the resolution-conversion method even when RIP Layout is disabled and the source application controls placement. Added Mitchell resampling and explanations beside the resampling and dithering selectors in all 27 interface languages. The job log identifies the selected resampling method.
- **Clearer progress:** streaming logs distinguish raster preparation from sending data. While output is still being generated, the byte total is shown as unknown rather than presenting a misleading fixed total.
- **Printer settings:** improved handling of Windows PrintTickets and legacy application settings. This does not claim to resolve every Paint/Sumatra default-setting issue or the reported P900 configuration failure on one tester's system.
- **Drying controls:** added a drying-delay setting for supported XP-750/XP-960 modes. Availability depends on the selected model; physical results need to be checked with the actual paper and ink.
- **Installation and support:** removed the redundant Start menu shortcut and expanded technical diagnostics for Windows printer configuration failures. Support archives are created locally and are not automatically uploaded; review them before sharing.
- **Package size:** omitted the optional AVIF and JPEG XL modules from imagecodecs. TIFF with JPEG XL compression is no longer supported by the RIP input decoder. Standard TIFF compression and the 16-bit path remain available.

This is a **prerelease**. Automated and native rendering tests have passed, but physical output, paper handling, and print quality have not been verified on every supported printer. The reported P900 case with only eight paper-size entries remains under investigation.

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
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-ModelPackageSet.ps1 -Version 1.0.0.224
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-InnoSetup.ps1 -Version 1.0.0.224
```

Use a new four-part version for each build. The first script compiles Cython, freezes the Python runtime, builds and signs the Manager MSIX, and checks the result. The second produces `windows_psa/artifacts/pyToneRIP-Setup-<version>-Beta.exe`.

On first use, the package builder creates a self-signed development certificate and its random password in `windows_psa/signing-local`. This directory is ignored by Git. Keep the PFX and password together and do not commit or share them. To reuse an existing signing identity outside the checkout, pass `-SigningRoot 'C:\path\to\private-signing-dir'` to `Build-ModelPackageSet.ps1`.

The bootstrap EXE is not Authenticode signed. Windows may show a publisher or SmartScreen warning during beta testing. The MSIX inside is signed with the local development certificate; the installer imports its public certificate into the local machine's Trusted People store.

## Install

Run the [pyToneRIP 1.0.0.224 Beta installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.224-beta/pyToneRIP-Setup-1.0.0.224-Beta.exe) on Windows 11 build 26100 or later. The installer requests administrator approval to trust the package certificate and install the Manager. Start pyToneRIP and add a printer through its interface.

For a package without the bootstrap installer, open PowerShell as Administrator and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\artifacts\pyToneRIP-ModuleSet-1.0.0.224\Install-ModuleSet-Manager.ps1
```

## Source layout

- `src/xp750_rip`: print engine, calibration, and package generator.
- `printer_catalog`: printer capabilities and vendor PPDs.
- `windows_psa`: Windows app, build scripts, and installer.

The project is licensed under GPL 3.0 only unless a file says otherwise; see [LICENSE](LICENSE).
