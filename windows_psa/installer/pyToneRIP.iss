#ifndef PackageVersion
  #error PackageVersion must be supplied by Build-InnoSetup.ps1
#endif
#ifndef ModuleSetDir
  #error ModuleSetDir must be supplied by Build-InnoSetup.ps1
#endif
#ifndef InstallerIcon
  #error InstallerIcon must be supplied by Build-InnoSetup.ps1
#endif
#ifndef InstallerLicense
  #error InstallerLicense must be supplied by Build-InnoSetup.ps1
#endif
#ifndef InstallerWizardImage
  #error InstallerWizardImage must be supplied by Build-InnoSetup.ps1
#endif
#ifndef InstallerWizardSmallImage
  #error InstallerWizardSmallImage must be supplied by Build-InnoSetup.ps1
#endif

[Setup]
AppId={{C6BD7D38-8D0C-4E7E-8DB2-AF66D4356387}
AppName=pyToneRIP Setup
AppVersion={#PackageVersion}
AppPublisher=pyToneRIP Open Source Project
DefaultDirName={autopf}\pyToneRIP Setup
DisableDirPage=yes
DefaultGroupName=pyToneRIP
DisableProgramGroupPage=no
AllowNoIcons=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.26100
WizardStyle=modern
DisableWelcomePage=no
WizardImageFile={#InstallerWizardImage}
WizardSmallImageFile={#InstallerWizardSmallImage}
WizardImageStretch=yes
Compression=lzma2
SolidCompression=yes
OutputBaseFilename=pyToneRIP-Setup-{#PackageVersion}-Beta
SetupIconFile={#InstallerIcon}
LicenseFile={#InstallerLicense}
UninstallDisplayName=pyToneRIP Setup
UninstallDisplayIcon={app}\pyToneRIP.ico
CloseApplications=no
RestartApplications=no
UsePreviousTasks=yes

[Languages]
; Keep these locale entries in sync with pyToneRIP/Strings and Package.appxmanifest.
Name: "en_US"; MessagesFile: "compiler:Default.isl"
Name: "en_GB"; MessagesFile: "compiler:Default.isl,Languages\EnglishBritish.isl"
Name: "bg_BG"; MessagesFile: "compiler:Languages\Bulgarian.isl"
Name: "cs_CZ"; MessagesFile: "compiler:Languages\Czech.isl"
Name: "da_DK"; MessagesFile: "compiler:Languages\Danish.isl"
Name: "de_DE"; MessagesFile: "compiler:Languages\German.isl"
Name: "el_GR"; MessagesFile: "compiler:Default.isl,Languages\Greek.isl"
Name: "es_ES"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "et_EE"; MessagesFile: "compiler:Default.isl,Languages\Estonian.isl"
Name: "fi_FI"; MessagesFile: "compiler:Languages\Finnish.isl"
Name: "fr_FR"; MessagesFile: "compiler:Languages\French.isl"
Name: "ga_IE"; MessagesFile: "compiler:Default.isl,Languages\Irish.isl"
Name: "hr_HR"; MessagesFile: "compiler:Default.isl,Languages\Croatian.isl"
Name: "hu_HU"; MessagesFile: "compiler:Languages\Hungarian.isl"
Name: "it_IT"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "lt_LT"; MessagesFile: "compiler:Default.isl,Languages\Lithuanian.isl"
Name: "lv_LV"; MessagesFile: "compiler:Default.isl,Languages\Latvian.isl"
Name: "mt_MT"; MessagesFile: "compiler:Default.isl,Languages\Maltese.isl"
Name: "nl_NL"; MessagesFile: "compiler:Languages\Dutch.isl"
Name: "pl_PL"; MessagesFile: "compiler:Languages\Polish.isl"
Name: "pt_PT"; MessagesFile: "compiler:Languages\Portuguese.isl"
Name: "ro_RO"; MessagesFile: "compiler:Default.isl,Languages\Romanian.isl"
Name: "ru_RU"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "sk_SK"; MessagesFile: "compiler:Languages\Slovak.isl"
Name: "sl_SI"; MessagesFile: "compiler:Languages\Slovenian.isl"
Name: "sv_SE"; MessagesFile: "compiler:Default.isl,Languages\Swedish.isl"
Name: "uk_UA"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[LangOptions]
en_US.LanguageName=English (United States)

[CustomMessages]
LanguageSetupFailed=pyToneRIP was installed, but its interface language could not be saved. Select the language in the application settings. Details:
ru_RU.LanguageSetupFailed=pyToneRIP установлен, но язык интерфейса не удалось сохранить. Выберите язык в настройках программы. Подробности:
de_DE.LanguageSetupFailed=pyToneRIP wurde installiert, aber die Sprache der Benutzeroberfläche konnte nicht gespeichert werden. Wählen Sie die Sprache in den Programmeinstellungen. Details:
PrivacyTitle=Privacy and support logs
PrivacyDescription=How diagnostic logs are handled
PrivacyBody=pyToneRIP never sends logs or personal data to the developer or a support server. The Prepare log files button creates a ZIP archive on your desktop containing only technical error codes, categories, counts and Windows event IDs. The ZIP contains no personal data: original log text, names, file paths, IP addresses and document data are excluded. The app does not send this archive; only you can share it outside the app.
ru_RU.PrivacyTitle=Конфиденциальность и журналы
ru_RU.PrivacyDescription=Как обрабатываются диагностические журналы
ru_RU.PrivacyBody=pyToneRIP никогда не отправляет журналы или персональные данные разработчику либо на сервер поддержки. Кнопка «Отправка Log файлов» создаёт на рабочем столе ZIP только с техническими кодами ошибок, категориями, счётчиками и номерами событий Windows. Персональных данных в ZIP нет: исходный текст журналов, имена, пути к файлам, IP-адреса и данные документов исключены. Программа не отправляет архив; передать его можете только вы сами вне программы.
de_DE.PrivacyTitle=Datenschutz und Protokolle
de_DE.PrivacyDescription=Umgang mit Diagnoseprotokollen
de_DE.PrivacyBody=pyToneRIP sendet niemals Protokolle oder personenbezogene Daten an den Entwickler oder einen Support-Server. Die Schaltfläche „Logdateien vorbereiten“ erstellt auf Ihrem Desktop eine ZIP-Datei nur mit technischen Fehlercodes, Kategorien, Zählern und Windows-Ereignis-IDs. Die ZIP-Datei enthält keine personenbezogenen Daten: Originaltexte der Protokolle, Namen, Dateipfade, IP-Adressen und Dokumentdaten sind ausgeschlossen. Die App versendet das Archiv nicht; nur Sie können es außerhalb der App weitergeben.
UninstallReportQuestion=Would you like to create a ZIP of the available pyToneRIP logs on your desktop?
UninstallReportCreated=A ZIP with available logs was created on your desktop (pyToneRIP-logs-*.zip).
UninstallReportFailed=The log archive could not be created. The cleanup log remains here:
ru_RU.UninstallReportQuestion=Создать ZIP-архив доступных журналов pyToneRIP на рабочем столе?
ru_RU.UninstallReportCreated=ZIP-архив доступных журналов создан на рабочем столе (pyToneRIP-logs-*.zip).
ru_RU.UninstallReportFailed=Не удалось создать архив журналов. Журнал удаления остаётся здесь:
de_DE.UninstallReportQuestion=Möchten Sie eine ZIP-Datei mit den verfügbaren pyToneRIP-Protokollen auf dem Desktop erstellen?
de_DE.UninstallReportCreated=Eine ZIP-Datei mit verfügbaren Protokollen wurde auf dem Desktop erstellt (pyToneRIP-logs-*.zip).
de_DE.UninstallReportFailed=Das Protokollarchiv konnte nicht erstellt werden. Das Bereinigungsprotokoll befindet sich hier:

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#ModuleSetDir}\catalog.json"; DestDir: "{app}\Payload"; Flags: ignoreversion
Source: "{#ModuleSetDir}\catalog.sig"; DestDir: "{app}\Payload"; Flags: ignoreversion
Source: "{#ModuleSetDir}\PACKAGE-READY.json"; DestDir: "{app}\Payload"; Flags: ignoreversion
Source: "{#ModuleSetDir}\pyToneRIP-TestSigning.cer"; DestDir: "{app}\Payload"; Flags: ignoreversion
Source: "{#ModuleSetDir}\Test-ModulePackageSet.ps1"; DestDir: "{app}\Payload"; Flags: ignoreversion
Source: "{#ModuleSetDir}\Install-ModuleSet-Manager.ps1"; DestDir: "{app}\Payload"; Flags: ignoreversion
Source: "Set-InstallerLanguage.ps1"; DestDir: "{app}\Payload"; Flags: ignoreversion
Source: "..\Uninstall-pyToneRIP.ps1"; DestDir: "{app}\Payload"; Flags: ignoreversion
Source: "..\Clear-PyToneRipPrintState.ps1"; DestDir: "{app}\Payload"; Flags: ignoreversion
Source: "..\Clear-PyToneRipRegistryState.ps1"; DestDir: "{app}\Payload"; Flags: ignoreversion
Source: "..\Collect-SupportReport.ps1"; DestDir: "{app}\Payload"; Flags: ignoreversion
Source: "{#ModuleSetDir}\Manager\*.msix"; DestDir: "{app}\Payload\Manager"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ModuleSetDir}\Manager\*.appx"; DestDir: "{app}\Payload\Manager"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#InstallerIcon}"; DestDir: "{app}"; DestName: "pyToneRIP.ico"; Flags: ignoreversion
Source: "{#InstallerLicense}"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

; MSIX supplies the application's Start entry.
; Remove only the legacy installer shortcut when it still exists.
[InstallDelete]
Type: files; Name: "{group}\pyToneRIP.lnk"; Check: FileExists(ExpandConstant('{group}\pyToneRIP.lnk'))
Type: files; Name: "{group}\{cm:UninstallProgram,pyToneRIP}.lnk"; Check: not WizardNoIcons

; Optional desktop and Quick Launch shortcuts use the stable packaged AUMID.
; Keep the uninstaller directly in Programs: the installed nested shortcut
; was absent from the Start app catalog, while a fresh root shortcut appeared.
; It has its own identity, separate from the packaged application. Keep the
; localized label and honor the installer's choice to omit Start shortcuts.
[Icons]
Name: "{autoprograms}\{cm:UninstallProgram,pyToneRIP}"; Filename: "{uninstallexe}"; IconFilename: "{app}\pyToneRIP.ico"; AppUserModelID: "pyToneRIP.Setup.Uninstall"; Check: not WizardNoIcons
Name: "{autodesktop}\pyToneRIP"; Filename: "{win}\explorer.exe"; Parameters: "shell:AppsFolder\pyToneRIP.Manager_qg4mjaqw0qctt!App"; IconFilename: "{app}\pyToneRIP.ico"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\pyToneRIP"; Filename: "{win}\explorer.exe"; Parameters: "shell:AppsFolder\pyToneRIP.Manager_qg4mjaqw0qctt!App"; IconFilename: "{app}\pyToneRIP.ico"; Tasks: quicklaunchicon

; The checkbox appears on the final page after the MSIX install has succeeded.
[Run]
Filename: "{win}\explorer.exe"; Parameters: "shell:AppsFolder\pyToneRIP.Manager_qg4mjaqw0qctt!App"; Description: "{cm:LaunchProgram,pyToneRIP}"; Flags: postinstall nowait skipifsilent runasoriginaluser; Check: CanLaunchManager

[Code]
var
  ManagerInstalled: Boolean;

procedure InitializeWizard;
begin
  CreateOutputMsgPage(wpWelcome,
    ExpandConstant('{cm:PrivacyTitle}'),
    ExpandConstant('{cm:PrivacyDescription}'),
    ExpandConstant('{cm:PrivacyBody}'));
end;

function CanLaunchManager(): Boolean;
begin
  Result := ManagerInstalled;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Parameters: string;
begin
  if CurStep <> ssPostInstall then
    exit;

  Parameters := '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden ' +
    '-ExecutionPolicy Bypass -File "' +
    ExpandConstant('{app}\Payload\Install-ModuleSet-Manager.ps1') +
    '" -ModuleSetRoot "' + ExpandConstant('{app}\Payload') +
    '" -LogPath "' + ExpandConstant('{app}\install-msix.log') + '"';
  if not ShellExec('runas', ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Parameters, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated,
    ResultCode) or (ResultCode <> 0) then
    RaiseException('pyToneRIP MSIX installation failed. See ' +
      ExpandConstant('{app}\install-msix.log') + ' for details.');
  ManagerInstalled := True;

  Parameters := '-NoLogo -NoProfile -NonInteractive -Sta -WindowStyle Hidden ' +
    '-ExecutionPolicy Bypass -File "' +
    ExpandConstant('{app}\Payload\Set-InstallerLanguage.ps1') +
    '" -Language "' + ExpandConstant('{language}') +
    '" -LogPath "' + ExpandConstant('{app}\language-setup.log') + '"';
  if not ExecAsOriginalUser(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Parameters, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated,
    ResultCode) or (ResultCode <> 0) then
  begin
    Log('Could not set pyToneRIP interface language; exit code ' + IntToStr(ResultCode));
    MsgBox(ExpandConstant('{cm:LanguageSetupFailed}') + #13#10 +
      ExpandConstant('{app}\language-setup.log'), mbError, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  Parameters: string;
begin
  if CurUninstallStep <> usUninstall then
    exit;

  Parameters := '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden ' +
    '-ExecutionPolicy Bypass -File "' +
    ExpandConstant('{app}\Payload\Uninstall-pyToneRIP.ps1') + '" -LogPath "' +
    ExpandConstant('{app}\Payload\uninstall-cleanup.log') + '"';
  { The script records this user's SID before elevating its worker through UAC. }
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Parameters, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated,
    ResultCode) or (ResultCode <> 0) then
  begin
    MsgBox('pyToneRIP could not be completely removed. Installer files were kept. Cleanup log:' + #13#10 +
      ExpandConstant('{app}\Payload\uninstall-cleanup.log'), mbError, MB_OK);
    if MsgBox(ExpandConstant('{cm:UninstallReportQuestion}'), mbConfirmation, MB_YESNO) = IDYES then
    begin
      Parameters := '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden ' +
        '-ExecutionPolicy Bypass -File "' +
        ExpandConstant('{app}\Payload\Collect-SupportReport.ps1') +
        '" -InstallerRoot "' + ExpandConstant('{app}') +
        '" -OutputDirectory "' + ExpandConstant('{userdesktop}') + '"';
      if Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
        Parameters, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated,
        ResultCode) and (ResultCode = 0) then
        MsgBox(ExpandConstant('{cm:UninstallReportCreated}'), mbInformation, MB_OK)
      else
        MsgBox(ExpandConstant('{cm:UninstallReportFailed}') + #13#10 +
          ExpandConstant('{app}\Payload\uninstall-cleanup.log'), mbError, MB_OK);
    end;
    Abort;
  end;
end;
