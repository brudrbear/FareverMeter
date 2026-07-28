; Inno Setup script for the Farever+ Meter.
;
; Compiled by packaging/build.ps1, which passes AppVersion in from the VERSION
; constant in meter/farever_meter.py so the two can't drift:
;
;   ISCC.exe /DAppVersion=2.1 packaging\FareverMeter.iss
;
; Installs per-user under %LOCALAPPDATA%\Programs — no UAC prompt, no admin
; rights, and nothing written outside the user's own profile. Farever doesn't
; need admin either, so asking for it would be the odd thing.

#ifndef AppVersion
  #define AppVersion "0.0"
#endif

#define AppName "Farever+ Meter"
#define AppExe "FareverMeter.exe"
#define AppPublisher "Brudr"
#define AppUrl "https://github.com/brudrbear/FareverMeter"

[Setup]
; Stable across releases — it's how Windows knows an install is an upgrade of
; this program rather than a second copy of it. Never regenerate it.
AppId={{8B4B1F2E-9C6A-4E7D-93A5-2F1D6C0B7A34}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppSupportURL={#AppUrl}
AppUpdatesURL={#AppUrl}/releases
DefaultDirName={autopf}\FareverMeter
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
; Per-user: {autopf} resolves to %LOCALAPPDATA%\Programs under `lowest`, so the
; whole install happens without an elevation prompt.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=FareverMeter-{#AppVersion}-Setup
SetupIconFile=..\assets\farevermeter.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The meter must be stopped from its own tray icon so it can unload the Frida
; hook and detach from the game. Letting the Restart Manager close it would
; force-kill it instead, which is the exact thing that leaves a half-attached
; agent behind — so the check in [Code] handles it by asking, not by killing.
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The whole PyInstaller onedir output: the exe plus _internal, which carries
; Python, frida, Pillow, Tk and the meter's own data files. This is what means
; the user installs nothing else.
Source: "..\dist\FareverMeter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Farever+ log folder"; Filename: "{localappdata}\FareverMeter"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start the {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The bundle only. %LOCALAPPDATA%\FareverMeter is deliberately left alone: it
; holds the user's parse screenshots and window positions, and an uninstall
; (including the one that precedes an upgrade) has no business deleting those.
Type: filesandordirs; Name: "{app}\_internal"

[Code]
{ The tray icon's hidden window. Its class name is registered by TrayIcon._run
  in farever_meter.py, and is the most reliable way to tell a running meter
  from any other process that happens to share the executable name. }
const
  TrayClass = 'FareverMeterTray';

function AskToStopMeter(const Verb: String; Silent: Boolean): Boolean;
begin
  Result := True;
  if FindWindowByClassName(TrayClass) = 0 then
    Exit;
  { A silent run has nobody to answer the prompt below, and a suppressed MsgBox
    returns its default button — Retry — which would spin here forever. Refuse
    the run instead, so a scripted install fails visibly rather than hanging. }
  if Silent then
  begin
    Result := False;
    Exit;
  end;
  while FindWindowByClassName(TrayClass) <> 0 do
  begin
    if MsgBox('The Farever+ Meter is still running.' + #13#10#13#10 +
              'Right-click its icon in the notification area (by the clock - ' +
              'click the ^ arrow if you don''t see it) and choose "Stop the ' +
              'meter". You can also use the Stop button in the meter''s control ' +
              'menu, which opens with the game''s Esc menu.' + #13#10#13#10 +
              'Stopping it that way lets it detach from Farever cleanly. Then ' +
              'click Retry to carry on with the ' + Verb + '.',
              mbError, MB_RETRYCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := AskToStopMeter('install', WizardSilent);
end;

function InitializeUninstall(): Boolean;
begin
  Result := AskToStopMeter('uninstall', UninstallSilent);
end;
