; Harvest-Setup.exe: per-user installer for the Harvest tray app (Inno Setup 6).
;
; Installs to %LOCALAPPDATA%\Programs\Harvest without needing admin rights, so the
; app's own auto-updater can replace Harvest.exe in place later. CI passes the
; version: iscc /DAppVersion=0.2.0 desktop\installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B1E2F4A-8C3D-4E59-9A7B-2D4C6E8F1A3B}
AppName=Harvest
AppVersion={#AppVersion}
AppVerName=Harvest {#AppVersion}
AppPublisher=codevexx
AppPublisherURL=https://harvest.codevexx.com
AppSupportURL=https://github.com/codevexx/harvest/issues
AppUpdatesURL=https://github.com/codevexx/harvest/releases
DefaultDirName={localappdata}\Programs\Harvest
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Harvest-Setup
SetupIconFile=harvest.ico
UninstallDisplayIcon={app}\Harvest.exe
UninstallDisplayName=Harvest
WizardStyle=modern
WizardSizePercent=100
Compression=lzma2/max
SolidCompression=yes
CloseApplications=force
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName=Harvest
VersionInfoDescription=Harvest setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Start Harvest when I sign in to Windows"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "..\dist\Harvest.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\Harvest"; Filename: "{app}\Harvest.exe"
Name: "{userdesktop}\Harvest"; Filename: "{app}\Harvest.exe"; Tasks: desktopicon

[Registry]
; Same value the app writes from its own "Start with Windows" switch.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Harvest"; ValueData: """{app}\Harvest.exe"" --background"; Tasks: autostart; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Harvest"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\Harvest.exe"; Description: "Open Harvest now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\Harvest.old.exe"
Type: filesandordirs; Name: "{localappdata}\Harvest\updates"

[Code]
procedure StopHarvest();
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM Harvest.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopHarvest();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopHarvest();
  Result := True;
end;
