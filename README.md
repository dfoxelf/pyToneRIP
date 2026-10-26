# pyToneRIP

[![Release](https://img.shields.io/github/v/release/dfoxelf/pyToneRIP?include_prereleases&style=flat-square&v=229)](https://github.com/dfoxelf/pyToneRIP/releases)
[![Release downloads](https://img.shields.io/github/downloads/dfoxelf/pyToneRIP/total?style=flat-square&label=release%20downloads&v=229)](https://github.com/dfoxelf/pyToneRIP/releases)
[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2011%20x64-0078D4?style=flat-square)](#install)
[![Download Beta 1.0.0.229](https://img.shields.io/badge/Download-Beta%201.0.0.229-2ea44f?style=flat-square)](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.229-beta/pyToneRIP-Setup-1.0.0.229-Beta.exe)

Copyright © 2026 Alexander Eichhorn.

pyToneRIP is a monochrome Epson RIP for Windows 11. It contains the Python/Cython raster engine, a printer catalog, a Windows Print Support App, and an Inno Setup bootstrap installer.

## Download the beta

The current beta is **1.0.0.229**. [Download the setup installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.229-beta/pyToneRIP-Setup-1.0.0.229-Beta.exe).

<details>
<summary>Verify the installer checksum</summary>

Installer size: **67,554,885 bytes**. SHA-256:

```text
4bce9de05b8e50f92558212d2027c0dc7e1088f6f778956a3e40e94135e26af7
```

```powershell
Get-FileHash .\pyToneRIP-Setup-1.0.0.229-Beta.exe -Algorithm SHA256
```

</details>

This beta improves P900 paper-size compatibility, recovery when Windows cannot read initial printer settings, and the evidence collected for difficult configuration failures. Changes below are relative to public beta 224.

- **P900 paper sizes:** the catalog uses exact geometry measured from the installed Epson driver and public Windows Print Schema identities. Six sheet labels now match Windows built-in form names, addressing repeated entries such as Japanese Postcard / 100 x 148 mm. Saved page IDs are preserved.
- **Borderless choices in applications:** named borderless variants are available in paper-size lists used by both legacy and modern print dialogs. They also select the standard borderless feature. Shorter names keep every localized variant within the Windows form-name limit, including 4 x 6 inch borderless.
- **P900 initial-settings recovery:** if reading the Windows user PrintTicket and the existing DEVMODE path both fail, the Manager can obtain the queue configuration XML through the Windows CIM provider. This is limited to initial recovery when no per-user DEVMODE exists. Existing user settings are not replaced by queue defaults, and normal Windows validation, saving and readback checks remain required.
- **More useful support reports:** diagnostics separate provider opening, DEVMODE conversion and capability/resource retrieval. Reports retain original Windows error codes and available provider explanations, and compare the actual Manager/helper user context, session, elevation and AppContainer state. Activation events include process and correlation information. Reports are created locally and are not uploaded automatically; review them before sharing.
- **Uninstalling under another administrator account:** removal identifies the original user's package and virtual queues before elevation. Shared print resources needed by another Windows user are retained without treating that as a failed removal.
- **Start menu and installer:** the uninstaller shortcut is placed directly in the program list with its own identity and a localized label. The packaged app supplies the main entry. The installer now has matching welcome-page and header artwork.
- **Smaller runtime:** unused Tcl/Tk components, the remaining Pillow AVIF decoder, and additional optional imagecodecs modules for HEIF, JPEG XS and Jetraw are excluded. Standard TIFF compression and the existing 16-bit processing path remain available. TIFF pages compressed with an excluded codec are not supported.
- **Native XPS maintenance:** the high-precision Windows renderer has detailed English comments explaining its interfaces and conversion steps, alongside the native safety checks developed for this branch.

The P900 catalog currently contains **21 physical sheet sizes and 7 named borderless variants**. Five sizes present in the Epson driver remain omitted: 5 x 8 inch, 16:9 Wide (101.6 x 180.6 mm), 11 x 14 inch, 12 x 12 inch and 16 x 20 inch. This release does not claim complete parity with every Epson driver paper or borderless mode.

Experimental 16-bit processing, strip-based large-page preparation, buffered USB delivery, paired-channel dithering, and independent resampling controls from beta 224 remain available. An 8-bit source cannot regain missing tonal information simply by enabling 16-bit processing.

This is a **prerelease**. Automated tests and read-only Windows provider checks have passed on the development PC. The original P900 `E_FAIL` on the tester's machine is not confirmed fixed: XML recovery and the expanded diagnostics still need testing there. The revised paper labels and borderless 4 x 6 entry also need confirmation after installing this build. Physical output and paper handling have not been verified on every supported printer.

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
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-ModelPackageSet.ps1 -Version 1.0.0.229
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\Build-InnoSetup.ps1 -Version 1.0.0.229
```

Use a new four-part version for each build. The first script compiles Cython, freezes the Python runtime, builds and signs the Manager MSIX, and checks the result. The second produces `windows_psa/artifacts/pyToneRIP-Setup-<version>-Beta.exe`.

On first use, the package builder creates a self-signed development certificate and its random password in `windows_psa/signing-local`. This directory is ignored by Git. Keep the PFX and password together and do not commit or share them. To reuse an existing signing identity outside the checkout, pass `-SigningRoot 'C:\path\to\private-signing-dir'` to `Build-ModelPackageSet.ps1`.

The bootstrap EXE is not Authenticode signed. Windows may show a publisher or SmartScreen warning during beta testing. The MSIX inside is signed with the local development certificate; the installer imports its public certificate into the local machine's Trusted People store.

## Install

Run the [pyToneRIP 1.0.0.229 Beta installer](https://github.com/dfoxelf/pyToneRIP/releases/download/v1.0.0.229-beta/pyToneRIP-Setup-1.0.0.229-Beta.exe) on Windows 11 build 26100 or later. The installer requests administrator approval to trust the package certificate and install the Manager. Start pyToneRIP and add a printer through its interface.

For a package without the bootstrap installer, open PowerShell as Administrator and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows_psa\artifacts\pyToneRIP-ModuleSet-1.0.0.229\Install-ModuleSet-Manager.ps1
```

## Source layout

- `src/xp750_rip`: print engine, calibration, and package generator.
- `printer_catalog`: printer capabilities and vendor PPDs.
- `windows_psa`: Windows app, build scripts, and installer.

The project is licensed under GPL 3.0 only unless a file says otherwise; see [LICENSE](LICENSE).
