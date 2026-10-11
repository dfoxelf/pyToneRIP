[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z]{2}_[A-Z]{2}$')]
    [string]$Language,
    [string]$LogPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
    $logDirectory = [System.IO.Path]::GetDirectoryName(
        [System.IO.Path]::GetFullPath($LogPath))
    [System.IO.Directory]::CreateDirectory($logDirectory) | Out-Null
    Start-Transcript -LiteralPath $LogPath -Force | Out-Null
}

# Activate the packaged app in this user's session. The app stores the
# installer's language through ApplicationLanguages.PrimaryLanguageOverride.
# Unlike the elevated MSIX installation, this must run as the original user.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace pyToneRIP.Setup
{
    [ComImport]
    [Guid("2E941141-7F97-4756-BA1D-9DECDE894A3D")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IApplicationActivationManager
    {
        [PreserveSig]
        int ActivateApplication(
            [MarshalAs(UnmanagedType.LPWStr)] string appUserModelId,
            [MarshalAs(UnmanagedType.LPWStr)] string arguments,
            uint options,
            out uint processId);
    }

    public static class PackagedAppLauncher
    {
        // Prelaunch delivers the arguments without showing the app window.
        private const uint Launch = 0x02000000;

        public static uint Activate(string appUserModelId, string arguments)
        {
            object instance = Activator.CreateInstance(Type.GetTypeFromCLSID(
                new Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C"), true));
            try
            {
                uint processId;
                int result = ((IApplicationActivationManager)instance)
                    .ActivateApplication(appUserModelId, arguments, Launch, out processId);
                Marshal.ThrowExceptionForHR(result);
                return processId;
            }
            finally
            {
                Marshal.FinalReleaseComObject(instance);
            }
        }
    }
}
'@

$languageTag = $Language.Replace('_', '-')
$requestId = [guid]::NewGuid().ToString('N')
$receiptPath = Join-Path $env:LOCALAPPDATA (
    'Packages\pyToneRIP.Manager_qg4mjaqw0qctt\LocalState\installer-language-' +
    $requestId + '.txt')
$failurePath = $receiptPath + '.error'
$arguments = '--set-installer-language=' + $languageTag +
    ' --request-id=' + $requestId
try {
    $processId = [pyToneRIP.Setup.PackagedAppLauncher]::Activate(
        'pyToneRIP.Manager_qg4mjaqw0qctt!App', $arguments)
    if ($processId -eq 0) {
        throw 'Windows did not return a process ID for pyToneRIP language activation.'
    }

    # Process lifetime is not a reliable signal for a packaged app. Its
    # request-specific receipt is written after the language setter completes.
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $failurePath) {
            $failure = [System.IO.File]::ReadAllText($failurePath).Trim()
            throw "pyToneRIP rejected installer language ${languageTag}: $failure"
        }
        if (Test-Path -LiteralPath $receiptPath) {
            $savedLanguage = [System.IO.File]::ReadAllText($receiptPath).Trim()
            if ($savedLanguage -ne $languageTag) {
                throw "pyToneRIP saved an unexpected installer language: $savedLanguage"
            }
            Write-Host "pyToneRIP installer language confirmed: $languageTag"
            return
        }
        Start-Sleep -Milliseconds 100
    }
    $startupLog = Join-Path $env:LOCALAPPDATA (
        'Packages\pyToneRIP.Manager_qg4mjaqw0qctt\LocalState\pyToneRIP-startup.log')
    throw "pyToneRIP did not confirm the installer language. See $startupLog"
} catch {
    Write-Host ('pyToneRIP installer language failed: ' + $_.Exception.ToString())
    throw
} finally {
    Remove-Item -LiteralPath $receiptPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $failurePath -Force -ErrorAction SilentlyContinue
    if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
        Stop-Transcript | Out-Null
    }
}
