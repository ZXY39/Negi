; Negi one-click installer for Windows.
;
;   ISCC.exe /DVersion=2.5.0 packaging\negi.iss   ->  dist\Negi-Setup-2.5.0.exe
;   (packaging\build-setup.ps1 does this and reads the version out of the tree.)
;
; What the exe owns: the wizard, the folder choice, and the Add/Remove Programs
; entry. Everything about installing Negi itself stays in install.ps1, which the
; wizard hands the chosen folder to with -NoUninstallEntry so the two installers
; cannot leave two "Negi" rows that uninstall differently. The payload is two
; scripts, not the program: the checkout is fetched from the pinned tag over the
; network, exactly like `iwr ... | iex`.
;
; PrivilegesRequired=lowest on purpose - the per-user layout (HKCU uninstall key,
; %LOCALAPPDATA% state, ~/.local/bin stub) never needs admin, and asking for it
; would make the uninstall path diverge between the two installers.

#define MyAppName "Negi"
#define MyAppPublisher "ZXY39"
#define MyAppURL "https://github.com/ZXY39/Negi"

[Setup]
AppId={{7B4F0C6E-2D91-4C58-9A73-5E1C0D8A4F62}
AppName={#MyAppName}
AppVersion={#Version}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={%USERPROFILE}\Negi
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

[Run]
; [Run] waits for the process to exit by default; the install must finish before
; the wizard can report success.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{tmp}\setup-run.ps1"" -Dir ""{code:TargetDir}"" -Quiet -NoUninstallEntry"; StatusMsg: "正在安装 Negi：取代码并建立环境（首次会下载依赖）……"; Flags: runhidden
Filename: "{app}\negi.cmd"; Parameters: "start --window"; Description: "启动 Negi（独立窗口）"; Flags: postinstall nowait skipifsilent skipifdoesntexist runhidden

[Code]
function TargetDir(Param: String): String;
begin
  Result := WizardDirValue;
end;

[UninstallRun]
; No data flag: the keep/purge choice is made at uninstall time, so it belongs to
; uninstall.ps1, which also refuses to delete a directory it cannot prove is Negi's.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\uninstall.ps1"""; RunOnceId: "NegiUninstallScript"; Flags: runhidden
