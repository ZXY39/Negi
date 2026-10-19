#requires -Version 5.1
<#
  Build Negi-Setup.exe with Inno Setup.

    .\packaging\build-setup.ps1                     # -> dist\Negi-Setup-<version>.exe
    .\packaging\build-setup.ps1 -Version 2.6.0
    .\packaging\build-setup.ps1 -IsccPath 'C:\Tools\Inno Setup 6\ISCC.exe'

  Inno Setup is not part of Windows, so the script looks for it in the per-user
  and machine install folders before giving up; winget install JRSoftware.Inno
  Setup is the one-liner that provides it. The alternative that needed no tool at
  all was IExpress, which produced no artifact and reported nothing on failure -
  see docs/architecture/operations-and-maintenance.md.

  The wizard itself lives in negi.iss. This script only resolves the version (so
  the exe name and the install.ps1 tag cannot disagree) and runs the compiler.
#>
param(
    [string]$Version = '',
    [string]$IsccPath = '',
    [string]$OutDir = 'dist'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = Split-Path -Parent $PSScriptRoot

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
& $iscc "/DVersion=$Version" $script
if ($LASTEXITCODE -ne 0) {
    throw "iscc failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $target)) {
    throw "iscc reported success but $target is missing (check OutputBaseFilename)"
}

$size = (Get-Item -LiteralPath $target).Length
Write-Host ("[build] ok: {0} ({1:N0} KB)" -f $target, ($size / 1KB))
