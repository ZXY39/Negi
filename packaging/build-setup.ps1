#requires -Version 5.1
<#
  Build Negi-Setup.exe with the Windows-built-in IExpress.

    .\packaging\build-setup.ps1                 # -> dist\Negi-Setup-<version>.exe
    .\packaging\build-setup.ps1 -OutDir build

  Why IExpress and not Inno Setup: the installer's whole job is to ask for a
  folder and hand off to install.ps1, which already owns the directory choice,
  the command stub, the shortcuts and the uninstall entry. IExpress ships with
  Windows, so a release build needs no extra tool and no new supply chain. The
  cost is a plain wizard (no progress or license page) and, like any unsigned
  installer, a SmartScreen prompt on first run.

  The exe carries two scripts, not the program: the checkout is fetched by
  install.ps1 from the pinned tag, exactly as the one-line channel fetches it.

  Status: UNVERIFIED. The first attempts on a headless box produced no artifact
  and IExpress reports nothing on failure, so nothing here proves the wizard
  works end to end. Treat a successful run (exe built, double-clicked, installs
  into the chosen folder) as the first real test.
#>
param(
    [string]$OutDir = 'dist',
    [string]$Version = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = Split-Path -Parent $PSScriptRoot

if (-not $Version) {
    $init = Get-Content -LiteralPath (Join-Path $root 'g3ku\__init__.py') -Raw
    if ($init -match '__version__\s*=\s*"([^"]+)"') { $Version = $Matches[1] } else { throw 'cannot read g3ku.__version__' }
}

$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("negi-setup-stage-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $stage | Out-Null
try {
    foreach ($name in @('install.ps1', 'packaging\setup-run.ps1')) {
        Copy-Item -LiteralPath (Join-Path $root $name) -Destination $stage -Force
    }

    New-Item -ItemType Directory -Force -Path (Join-Path $root $OutDir) | Out-Null
    $target = Join-Path (Join-Path $root $OutDir) "Negi-Setup-$Version.exe"
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }

    $sed = Join-Path $stage 'negi.sed'
    $template = @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=0
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=%DisplayLicense%
FinishMessage=%FinishMessage%
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=%PostInstallCmd%
AdminQuietInstCmd=%AdminQuietInstCmd%
UserQuietInstCmd=%UserQuietInstCmd%
SourceFiles=SourceFiles
[Strings]
InstallPrompt=
DisplayLicense=
FinishMessage=Negi 安装完成。以后可以直接敲 negi，或点桌面上的 Negi 图标。
TargetName=$target
FriendlyName=Negi Setup $Version
AppLaunched=powershell.exe -NoProfile -ExecutionPolicy Bypass -File setup-run.ps1
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
FILE0="install.ps1"
FILE1="setup-run.ps1"
[SourceFiles]
SourceFiles0=$stage
[SourceFiles0]
%FILE0%=
%FILE1%=
"@
    # IExpress reads the template as ANSI text with CRLF lines; a UTF-8 BOM or
    # bare LFs make it miss the section headers and fail silently, producing no
    # exe and no message.
    $template = $template -replace "`r?`n", "`r`n"
    [System.IO.File]::WriteAllText($sed, $template, [System.Text.Encoding]::ASCII)

    Write-Host "[build] iexpress -> $target"
    # IExpress is quiet and reports nothing useful on stderr; the produced file
    # is the only trustworthy signal, so the exit code is not consulted.
    & iexpress.exe /N /Q $sed | Out-Null
    if (-not (Test-Path -LiteralPath $target)) { throw 'iexpress produced no exe (check the .sed template and that the payload files exist)' }

    $size = (Get-Item -LiteralPath $target).Length
    Write-Host ("[build] ok: {0} ({1:N0} KB)" -f $target, ($size / 1KB))
}
finally {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
