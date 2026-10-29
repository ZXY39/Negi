; Negi one-click installer for Windows.
;
;   ISCC.exe /DVersion=2.5.0 packaging\negi.iss   ->  dist\Negi-Setup-2.5.0.exe
;   (packaging\build-setup.ps1 does this and reads the version out of the tree.)
;
; What the exe owns: the wizard, the folder choice, and the Add/Remove Programs
; entry. Everything about installing Negi itself stays in install.ps1, which the
; wizard hands the chosen folder to with -NoUninstallEntry so the two installers
; cannot leave two "Negi" rows that uninstall differently. The payload carries
; the tag's shallow checkout (negi-source.zip) and a pinned uv.exe, so a fresh
; install does not fetch code from GitHub and does not download uv from
; astral.sh; install.ps1 keeps its network lanes as the fallback.
;
; PrivilegesRequired=lowest on purpose - the per-user layout (HKCU uninstall key,
; %LOCALAPPDATA% state, ~/.local/bin stub) never needs admin, and asking for it
; would make the uninstall path diverge between the two installers.

#define MyAppName "Negi"
#define MyAppPublisher "ZXY39"
#define MyAppURL "https://github.com/ZXY39/Negi"
#define AppGuid "7B4F0C6E-2D91-4C58-9A73-5E1C0D8A4F62"
; build-setup.ps1 passes an absolute path; the default keeps a manual ISCC run
; working after that script has prepared dist\payload.
#ifndef PayloadDir
  #define PayloadDir "..\dist\payload"
#endif

[Setup]
AppId={{{#AppGuid}
AppName={#MyAppName}
AppVersion={#Version}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={code:SuggestRoot}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
DisableDirPage=no
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Negi-Setup-{#Version}
SetupIconFile=..\g3ku\web\frontend\favicon.ico
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={localappdata}\Negi\negi.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Files]
Source: "..\install.ps1"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "setup-run.ps1"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "{#PayloadDir}\negi-source.zip"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "{#PayloadDir}\uv.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Run]
; [Run] waits for the process to exit by default; the install must finish before
; the wizard can report success.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{tmp}\setup-run.ps1"" -Dir ""{code:TargetDir}"" -Quiet -NoUninstallEntry -SourceArchive ""{tmp}\negi-source.zip"" -UvPath ""{tmp}\uv.exe"""; StatusMsg: "正在安装 Negi：解出代码并建立环境（首次会下载依赖）……"; Flags: runhidden
Filename: "{app}\negi.cmd"; Parameters: "start --window"; Description: "启动 Negi（独立窗口）"; Flags: postinstall nowait skipifsilent skipifdoesntexist runhidden

[Code]
function SuggestRoot(Param: String): String;
var
  Found: String;
begin
  // The folder page is a default, not a decision: on a device that already runs
  // Negi from another folder, accepting %USERPROFILE%\Negi would put a second root
  // beside the live one - two trees, one port, one pointer. Inno remembers this
  // setup's own previous directory, so what is added here is the row install.ps1
  // wrote for a device that was installed from the terminal.
  Result := '';
  if RegQueryStringValue(HKCU,
       'Software\Microsoft\Windows\CurrentVersion\Uninstall\{' + '{#AppGuid}' + '}_is1',
       'InstallLocation', Found) then
    Result := Found;
  if (Result = '') and RegQueryStringValue(HKCU,
       'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#MyAppName}',
       'InstallLocation', Found) then
    Result := Found;
  if Result = '' then
    Result := ExpandConstant('{%USERPROFILE}\Negi');
end;

function TargetDir(Param: String): String;
begin
  Result := WizardDirValue;
end;

[UninstallRun]
; No data flag: the keep/purge choice is made at uninstall time, so it belongs to
; uninstall.ps1, which also refuses to delete a directory it cannot prove is Negi's.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\uninstall.ps1"""; RunOnceId: "NegiUninstallScript"; Flags: runhidden
