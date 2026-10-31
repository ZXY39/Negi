#requires -Version 5.1
<#
  Build Negi-Setup.exe with Inno Setup.

    .\packaging\build-setup.ps1                     # -> dist\Negi-Setup-<version>.exe
    .\packaging\build-setup.ps1 -Version 2.6.0
    .\packaging\build-setup.ps1 -Kit dist\Negi-2.6.0-offline-win-x64.zip
    .\packaging\build-setup.ps1 -PayloadOnly        # stage the kit payload, no Inno Setup needed

  Inno Setup is not part of Windows, so the script looks for it in the per-user
  and machine install folders before giving up; winget install JRSoftware.Inno
  Setup is the one-liner that provides it. The alternative that needed no tool at
  all was IExpress, which produced no artifact and reported nothing on failure -
  see docs/architecture/operations-and-maintenance.md.

  The payload is the win-x64 offline kit and nothing else. One downloaded exe then
  installs without an index, without a python-build-standalone download and without
  a GitHub checkout: install.ps1 -Kit verifies kit.json against the device and every
  wheel's sha256, copies the kit's source tree and builds the environment from the
  kit's interpreter with `uv pip install --offline --no-index`.

  -Kit takes a kit zip or an already unpacked kit directory. Without it the script
  builds one through packaging\build-offline-kit.ps1, which is the same artifact the
  release publishes as Negi-<version>-offline-win-x64.zip; the build resolves the
  version first so the exe name, install.ps1 $Ref, install.sh REF, the payload tag
  and the kit's own kit.json cannot disagree.
#>
param(
    [string]$Version = '',
    [string]$IsccPath = '',
    [string]$OutDir = 'dist',
    [string]$Kit = '',
    [switch]$PayloadOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = Split-Path -Parent $PSScriptRoot

function Invoke-Captured {
    param([string[]]$Command)
    $exe = $Command[0]
    $rest = @($Command | Select-Object -Skip 1)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $exe @rest 2>&1 | ForEach-Object { [string]$_ })
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $saved
    }
    # A plain hashtable, not [pscustomobject]: Windows PowerShell 5.1 refuses to
    # project a captured native-stderr stream into a PSCustomObject.
    return @{ Code = $code; Lines = $lines }
}

function Find-Iscc {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path -LiteralPath $Explicit)) {
            throw "ISCC not found at '$Explicit'"
        }
        return (Resolve-Path -LiteralPath $Explicit).Path
    }
    $candidates = @(
        (Join-Path ${env:LOCALAPPDATA} 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles} 'Inno Setup 6\ISCC.exe')
    )
    # StrictMode Latest refuses to read a variable that does not exist, and the
    # 32-bit program folder is exactly such a variable on some builds.
    $x86 = $null
    if (Test-Path -LiteralPath 'env:ProgramFiles(x86)') { $x86 = ${env:ProgramFiles(x86)} }
    if ($x86) { $candidates += (Join-Path $x86 'Inno Setup 6\ISCC.exe') }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $onPath = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    throw 'Inno Setup is not installed. Run: winget install JRSoftware.Inno.Setup'
}

if (-not $Version) {
    $init = Get-Content -LiteralPath (Join-Path $root 'g3ku\__init__.py') -Raw
    if ($init -match '__version__\s*=\s*"([^"]+)"') { $Version = $Matches[1] } else { throw 'cannot read g3ku.__version__' }
}

$tag = "v$Version"
$installPs1 = Get-Content -LiteralPath (Join-Path $root 'install.ps1') -Raw
if ($installPs1 -notmatch "\`$Ref\s*=\s*'([^']+)'") {
    throw 'cannot read the $Ref pin from install.ps1'
}
if ($Matches[1] -ne $tag) {
    throw "install.ps1 pins $($Matches[1]) but the build version is $tag"
}
$installSh = Get-Content -LiteralPath (Join-Path $root 'install.sh') -Raw
if ($installSh -notmatch 'REF="\$\{G3KU_REF:-([^}]+)\}"') {
    throw 'cannot read the REF pin from install.sh'
}
if ($Matches[1] -ne $tag) {
    throw "install.sh pins $($Matches[1]) but the build version is $tag"
}

# ------------------------------------------------------------------ payload: the kit
# One artifact ships: the win-x64 offline kit. Nothing is fetched at install time,
# so the exe installs on a device with no package index and no GitHub access.
$kitScript = Join-Path $PSScriptRoot 'build-offline-kit.ps1'
if (-not $Kit) {
    if (-not (Test-Path -LiteralPath $kitScript)) {
        throw 'packaging/build-offline-kit.ps1 is missing and -Kit was not given'
    }
    Write-Host "[build] no -Kit given: building the win-x64 offline kit for $tag"
    # It has to run on Windows: uv installs an interpreter for its own host.
    $built = Invoke-Captured @('powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', $kitScript, '-Platform', 'win-x64', '-Ref', $tag, '-OutDir', $OutDir)
    foreach ($line in $built.Lines) { Write-Host $line }
    if ($built.Code -ne 0) { throw "build-offline-kit.ps1 failed (exit $($built.Code))" }
    $Kit = @($built.Lines) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Last 1
    if (-not $Kit) { throw 'build-offline-kit.ps1 printed no kit path' }
}
if (-not (Test-Path -LiteralPath $Kit)) { throw "kit not found: $Kit" }
$kitPath = (Resolve-Path -LiteralPath $Kit).Path
if (-not $kitPath.EndsWith('.zip', [StringComparison]::OrdinalIgnoreCase)) {
    throw "-Kit takes the kit archive (.zip): $kitPath"
}

# Read the two files the build has to trust out of the archive instead of unpacking
# a hundred megabytes: the platform it claims and the version it declares.
$tarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path -LiteralPath $tarExe)) { throw 'tar.exe is required to read a kit archive' }
function Read-KitMember {
    param([string]$Member)
    # Called directly, not through a helper: a hashtable returned from a nested helper
    # does not survive as a single object here under PowerShell 5.1, and the failure
    # reads as "property Code not found", not as a tar problem.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $tarExe -xOf $kitPath $Member 2>&1 | ForEach-Object { [string]$_ })
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $saved
    }
    if ($code -ne 0) { throw "kit is missing $Member (tar exit $code)" }
    return ($lines -join "`n")
}
$manifest = (Read-KitMember 'kit.json') | ConvertFrom-Json
if ($manifest.platform -ne 'win-x64') {
    throw "kit is for $($manifest.platform); Negi-Setup.exe installs on win-x64"
}
if ([string]$manifest.version -ne $Version) {
    throw "kit.json says $($manifest.version) but this build is $Version"
}
$kitInit = Read-KitMember 'source/g3ku/__init__.py'
if ($kitInit -notmatch '__version__\s*=\s*"([^"]+)"') {
    throw 'kit source has no readable g3ku.__version__'
}
if ($Matches[1] -ne $Version) {
    throw "kit source declares $($Matches[1]) but this build is $Version"
}
Write-Host ("[build] kit: {0} ({1}, v{2})" -f $kitPath, $manifest.platform, $manifest.version)

$payloadDir = Join-Path (Join-Path $root $OutDir) 'payload'
if (Test-Path -LiteralPath $payloadDir) {
    Remove-Item -LiteralPath $payloadDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $payloadDir | Out-Null
$kitPayload = Join-Path $payloadDir 'negi-kit.zip'
Copy-Item -LiteralPath $kitPath -Destination $kitPayload -Force
Write-Host ("[build] payload: {0} ({1:N1} MB)" -f $kitPayload, ((Get-Item -LiteralPath $kitPayload).Length / 1MB))

if ($PayloadOnly) {
    Write-Host "[build] payload ready (no exe): $payloadDir"
    exit 0
}

$iscc = Find-Iscc -Explicit $IsccPath
$script = Join-Path $PSScriptRoot 'negi.iss'
$target = Join-Path (Join-Path $root $OutDir) "Negi-Setup-$Version.exe"

New-Item -ItemType Directory -Force -Path (Join-Path $root $OutDir) | Out-Null
if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }

Write-Host "[build] iscc   : $iscc"
Write-Host "[build] version: $Version"
Write-Host "[build] target : $target"
# ISCC returns 0 on success and nonzero with a message on stderr, which is why
# the exit code is consulted here and the file check below is only a backstop.
& $iscc "/DVersion=$Version" "/DKitZip=$kitPayload" $script
if ($LASTEXITCODE -ne 0) {
    throw "iscc failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $target)) {
    throw "iscc reported success but $target is missing (check OutputBaseFilename)"
}

$size = (Get-Item -LiteralPath $target).Length
Write-Host ("[build] ok: {0} ({1:N0} KB)" -f $target, ($size / 1KB))
