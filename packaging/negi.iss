; Negi one-click installer for Windows.
;
;   ISCC.exe /DVersion=2.5.0 packaging\negi.iss   ->  dist\Negi-Setup-2.5.0.exe
;   (packaging\build-setup.ps1 does this and reads the version out of the tree.)
;
; What the exe owns: the wizard, the folder choice, and the Add/Remove Programs
; entry. Everything about installing Negi itself stays in install.ps1, which the
; wizard hands the chosen folder to with -NoUninstallEntry so the two installers
; cannot leave two "Negi" rows that uninstall differently.
;
; The payload is the win-x64 offline kit: the trimmed source tree, every wheel this
; release resolves, a pinned CPython, uv and kit.json. install.ps1 -Kit installs
; from it with --offline --no-index, so one downloaded exe needs no index, no
; python-build-standalone download and no GitHub checkout. The generic installer
; ships the same kit as Negi-<version>-offline-<platform>.zip; this file only
; wraps its Windows member in the wizard.
;
; PrivilegesRequired=lowest on purpose - the per-user layout (HKCU uninstall key,
; %LOCALAPPDATA% state, ~/.local/bin stub) never needs admin, and asking for it
; would make the uninstall path diverge between the two installers.

#define MyAppName "Negi"
#define MyAppPublisher "ZXY39"
#define MyAppURL "https://github.com/ZXY39/Negi"
#define AppGuid "7B4F0C6E-2D91-4C58-9A73-5E1C0D8A4F62"
; build-setup.ps1 passes an absolute path. The default names the payload a local
; build leaves behind, so a manual ISCC run works after that script has run.
#ifndef KitZip
  #define KitZip "..\dist\payload\negi-kit.zip"
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
; Every Log() call below lands in %TEMP%\Negi Uninstallation Log*.txt without the
; operator having to pass /LOG, which is the only way an uninstall that ends in a
; closed dialog can still be read back afterwards.
SetupLogging=yes

[Files]
Source: "..\install.ps1"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "setup-run.ps1"; DestDir: "{tmp}"; Flags: deleteafterinstall
; One archive, the same bytes the release publishes as the win-x64 kit. Passed
; through as-is: install.ps1 opens it with the tar.exe every Windows 10 1803+ and
; Windows 11 ships, verifies kit.json against the device and every wheel's sha256,
; then unpacks it into its own staging directory.
Source: "{#KitZip}"; DestDir: "{tmp}"; DestName: "negi-kit.zip"; Flags: deleteafterinstall

[Run]
; [Run] waits for the process to exit by default; the install must finish before
; the wizard can report success.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{tmp}\setup-run.ps1"" -Dir ""{code:TargetDir}"" -Quiet -NoUninstallEntry -Kit ""{tmp}\negi-kit.zip"""; StatusMsg: "正在安装 Negi：从随包套件解出代码并建立环境（不访问网络，需要几分钟）……"; Flags: runhidden
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

var
  UninstallDataFlag: String;

function UninstallScriptPath: String;
begin
  Result := ExpandConstant('{app}\uninstall.ps1');
end;

function DataRootHint: String;
var
  FromEnv: String;
begin
  // Same three sources uninstall.ps1 resolves, kept to the two an operator can see
  // without reading the script: the environment override, then the default inside
  // the program directory.
  FromEnv := GetEnv('G3KU_DATA_DIR');
  if FromEnv <> '' then
    Result := FromEnv
  else
    Result := ExpandConstant('{app}\.g3ku');
end;

procedure ReportUninstallProblem(const Text: String);
begin
  Log('[uninstall] PROBLEM: ' + Text);
  if UninstallSilent then
  begin
    // Measured: a message box shown during a silent uninstall waits for an OK click all
    // the same, so an automated removal would hang here. Silent runs get the log line.
    Exit;
  end;
  MsgBox(Text, mbError, MB_OK);
end;

function AskUninstallDataFlag: Boolean;
var
  Choice: Integer;
  Question: String;
begin
  if UninstallSilent then
  begin
    // A silent run cannot answer a dialog: message boxes are auto-answered, so the
    // decision comes from the documented default and never from a return value.
    UninstallDataFlag := '-KeepData';
    Log('[uninstall] silent mode: data will be kept');
    Result := True;
    Exit;
  end;
  // Button captions are English because the wizard carries no language file; naming
  // them in the text is what makes the three-way choice unambiguous.
  Question :=
    '卸载 Negi。会话、任务与配置数据放在：' + #13#10 +
    DataRootHint + #13#10#13#10 +
    'Yes = 保留这些数据并卸载程序（下次装回同一目录直接认领）' + #13#10 +
    'No = 连同这些数据一起删除，无法恢复' + #13#10 +
    'Cancel = 中止，什么都不删';
  Choice := MsgBox(Question, mbConfirmation, MB_YESNOCANCEL);
  if Choice = IDCANCEL then
  begin
    Log('[uninstall] cancelled at the data question');
    Result := False;
    Exit;
  end;
  if Choice = IDNO then
    UninstallDataFlag := '-PurgeData'
  else
    UninstallDataFlag := '-KeepData';
  Log('[uninstall] data decision: ' + UninstallDataFlag);
  Result := True;
end;

function InitializeUninstall(): Boolean;
var
  Script: String;
begin
  // Returning False here is the only supported abort, and it happens before the
  // confirmation prompt and before a single file is touched. Inno Setup 6.7.3 has no
  // CancelUninstall, so anything that must not leave a half-uninstalled device is
  // decided at this point.
  Result := False;
  Script := UninstallScriptPath;
  if not FileExists(Script) then
  begin
    ReportUninstallProblem('找不到卸载脚本：' + #13#10 + Script + #13#10#13#10 +
      '这台设备的 Negi 可能装在这个目录之外（安装向导认的是 %LOCALAPPDATA%\Negi\install-root.json ' +
      '记录的根）。卸载项已保留，没有删除任何文件；请到实际安装目录里运行 uninstall.ps1。');
    Exit;
  end;
  if not AskUninstallDataFlag then
    Exit;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Started: Boolean;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  // usUninstall is the first step after the operator confirmed, so the work below
  // never runs for a declined prompt. uninstall.ps1 carries the data flag and is
  // therefore never interactive: nothing here waits on a window nobody can see.
  Started := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -File "' + UninstallScriptPath + '" ' + UninstallDataFlag,
    '', SW_HIDE, ewWaitUntilTerminated, Code);
  Log('[uninstall] powershell started=' + IntToStr(Ord(Started)) +
    ' exit code=' + IntToStr(Code));
  if (not Started) or (Code <> 0) then
    // Inno still finishes its own removal and drops the installed-apps row: there is
    // no way to cancel from here. The compensation is that the failure is announced
    // instead of silent, and that uninstall.ps1 only fails on its delete guards.
    ReportUninstallProblem('卸载没有完成，程序文件可能还留在：' + #13#10 +
      ExpandConstant('{app}') + #13#10#13#10 +
      '逐项结果写在 %TEMP% 里最新一份 negi-uninstall-<时间>.log 的最后一行，向导自己的日志在 ' +
      '%TEMP% 的 Negi Uninstallation Log 里。常见原因是某个 Negi 进程结束不掉，或卸载护栏认不出这个目录属于 Negi。');
end;
