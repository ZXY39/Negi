#requires -Version 5.1
<#
  Build Negi-Setup.exe with Inno Setup.

    .\packaging\build-setup.ps1                     # -> dist\Negi-Setup-<version>.exe
    .\packaging\build-setup.ps1 -Version 2.6.0
    .\packaging\build-setup.ps1 -PayloadOnly        # payload only, no Inno Setup needed
    .\packaging\build-setup.ps1 -UvZipPath <zip>    # use an already downloaded uv release

  Inno Setup is not part of Windows, so the script looks for it in the per-user
  and machine install folders before giving up; winget install JRSoftware.Inno
  Setup is the one-liner that provides it. The alternative that needed no tool at
  all was IExpress, which produced no artifact and reported nothing on failure -
  see docs/architecture/operations-and-maintenance.md.

  Before compiling, two payload artifacts are prepared in <OutDir>\payload:

    negi-source.zip  A shallow checkout of the tag, .git included, with the same
                     sparse rules an online install would have applied. A fresh
                     exe install copies this tree instead of cloning from GitHub.
    uv.exe           The uv release pinned in packaging\uv-version.txt, accepted
                     only after its .sha256 matches.

  The wizard itself lives in negi.iss. This script resolves the version (so the
  exe name, install.ps1 $Ref, install.sh REF and the payload tag cannot
  disagree), prepares the payload and runs the compiler.
#>
param(
    [string]$Version = '',
    [string]$IsccPath = '',
    [string]$OutDir = 'dist',
    [string]$UvZipPath = '',
    [string]$RepoUrl = 'https://github.com/ZXY39/Negi.git',
    [switch]$PayloadOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = Split-Path -Parent $PSScriptRoot
$canonicalRepo = 'https://github.com/ZXY39/Negi.git'

function Invoke-Checked {
    param([string[]]$Command, [string]$Where)
    $exe = $Command[0]
    $rest = @($Command | Select-Object -Skip 1)
    # PowerShell 5.1 promotes a native command's stderr to a terminating error
    # under 'Stop', and git writes ordinary progress there. The exit code is the
    # only failure signal.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $exe @rest 2>&1 | ForEach-Object { [string]$_ } | Out-Host
    }
    finally {
        $ErrorActionPreference = $saved
    }
    if ($LASTEXITCODE -ne 0) {
        throw "failed: $exe $($rest -join ' ') (exit $LASTEXITCODE) in $Where"
    }
}

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

$payloadDir = Join-Path (Join-Path $root $OutDir) 'payload'
if (Test-Path -LiteralPath $payloadDir) {
    Remove-Item -LiteralPath $payloadDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $payloadDir | Out-Null

$checkout = Join-Path $payloadDir 'checkout'
Write-Host "[build] payload: $tag from $RepoUrl"
# core.autocrlf=false is written into the payload's repository config, so the
# device's global setting cannot turn every file into a tracked change: -Upgrade
# refuses to run on a dirty worktree.
Invoke-Checked @('git', '-c', 'core.autocrlf=false', 'clone', '--depth', '1', '--branch', $tag, $RepoUrl, $checkout) -Where $payloadDir
Invoke-Checked @('git', '-C', $checkout, 'remote', 'set-url', 'origin', $canonicalRepo) -Where $checkout
Invoke-Checked @('git', '-C', $checkout, 'config', 'core.autocrlf', 'false') -Where $checkout

$payloadInit = Get-Content -LiteralPath (Join-Path $checkout 'g3ku\__init__.py') -Raw
if ($payloadInit -notmatch '__version__\s*=\s*"([^"]+)"') {
    throw 'payload checkout has no readable g3ku.__version__'
}
if ($Matches[1] -ne $Version) {
    throw "payload declares $($Matches[1]) but the build version is $Version"
}

$listFile = Join-Path $checkout 'packaging\distribution-excludes.txt'
$excludes = @()
if (Test-Path -LiteralPath $listFile) {
    foreach ($raw in Get-Content -LiteralPath $listFile) {
        $name = ('' + $raw).Trim()
        if (-not $name -or $name.StartsWith('#')) { continue }
        if ($name -notmatch '^[A-Za-z0-9._-]+$' -or $name.Contains('..')) {
            throw "distribution exclusion '$name' is not a single top-level name"
        }
        $excludes += $name
    }
}
if (@($excludes).Count -gt 0) {
    $patterns = @('/*')
    foreach ($name in $excludes) { $patterns += "!/$name/" }
    Write-Host "[build] sparse checkout: excluding $($excludes -join ', ')"
    Invoke-Checked (@('git', '-C', $checkout, 'sparse-checkout', 'set', '--no-cone') + $patterns) -Where $checkout
    Invoke-Checked @('git', '-C', $checkout, 'checkout') -Where $checkout
}

$status = Invoke-Captured @('git', '-C', $checkout, 'status', '--porcelain')
if ($status.Code -ne 0) { throw "git status failed in $checkout" }
if (@($status.Lines).Count -gt 0) {
    throw "payload worktree is not clean: $($status.Lines -join '; ')"
}

$sourceZip = Join-Path $payloadDir 'negi-source.zip'
if (Test-Path -LiteralPath $sourceZip) { Remove-Item -LiteralPath $sourceZip -Force }
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $checkout,
        $sourceZip,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )
}
catch {
    # Constrained Language mode blocks Add-Type. Compress-Archive is slower and
    # skips hidden entries, and Git for Windows marks .git hidden, so clear the
    # attribute first or the payload would arrive without a repository.
    Write-Host "[build] .NET zip unavailable; falling back to Compress-Archive"
    & attrib.exe -h -s "$checkout\*" /s /d | Out-Null
    & attrib.exe -h "$checkout" | Out-Null
    Compress-Archive -Path (Join-Path $checkout '*') -DestinationPath $sourceZip -CompressionLevel Optimal -Force
}
$verify = Join-Path $env:TEMP ('zip-verify-' + (New-Guid).ToString('N'))
Expand-Archive -LiteralPath $sourceZip -DestinationPath $verify -Force
if (-not (Test-Path -LiteralPath (Join-Path $verify '.git\HEAD'))) {
    throw 'source payload does not contain .git/HEAD'
}
Remove-Item -LiteralPath $verify -Recurse -Force
Write-Host "[build] source payload zip verified"
Remove-Item -LiteralPath $checkout -Recurse -Force
Write-Host ("[build] source payload: {0} ({1:N1} MB)" -f $sourceZip, ((Get-Item -LiteralPath $sourceZip).Length / 1MB))

$uvVersion = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'uv-version.txt') -Raw).Trim()
if ($uvVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "packaging/uv-version.txt is not a pinned version: '$uvVersion'"
}
$uvAsset = 'uv-x86_64-pc-windows-msvc.zip'
$uvExe = Join-Path $payloadDir 'uv.exe'
if (Test-Path -LiteralPath $uvExe) { Remove-Item -LiteralPath $uvExe -Force }
if ($UvZipPath) {
    $uvZip = (Resolve-Path -LiteralPath $UvZipPath).Path
    Write-Host "[build] using local uv zip without checksum verification: $uvZip"
}
else {
    $base = "https://github.com/astral-sh/uv/releases/download/$uvVersion"
    $uvZip = Join-Path $env:TEMP ("uv-$uvVersion.zip")
    $shaFile = Join-Path $env:TEMP ("uv-$uvVersion.sha256")
    Write-Host "[build] downloading $base/$uvAsset"
    Invoke-WebRequest -Uri "$base/$uvAsset" -OutFile $uvZip -UseBasicParsing
    Invoke-WebRequest -Uri "$base/$uvAsset.sha256" -OutFile $shaFile -UseBasicParsing
    $shaText = (Get-Content -LiteralPath $shaFile -Raw).Trim()
    $expected = ($shaText -split '\s+')[0].ToLower()
    if ($expected -notmatch '^[0-9a-f]{64}$') {
        throw "uv release did not publish a usable sha256: $shaText"
    }
    $actual = (Get-FileHash -LiteralPath $uvZip -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        throw "uv sha256 mismatch: expected $expected, got $actual"
    }
    Write-Host "[build] uv sha256 verified: $expected"
}

$uvStage = Join-Path $env:TEMP ('uv-extract-' + (New-Guid).ToString('N'))
New-Item -ItemType Directory -Force -Path $uvStage | Out-Null
try {
    $uvExtracted = $false
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::ExtractToDirectory($uvZip, $uvStage)
        $uvExtracted = $true
    }
    catch {
        Expand-Archive -LiteralPath $uvZip -DestinationPath $uvStage -Force
    }
    $found = Get-ChildItem -LiteralPath $uvStage -Filter 'uv.exe' -Recurse | Select-Object -First 1
    if (-not $found) { throw "uv zip did not contain uv.exe" }
    Copy-Item -LiteralPath $found.FullName -Destination $uvExe -Force
}
finally {
    Remove-Item -LiteralPath $uvStage -Recurse -Force -ErrorAction SilentlyContinue
}
if ((Get-Item -LiteralPath $uvExe).Length -lt 1MB) {
    throw "extracted uv.exe is implausibly small"
}
Write-Host ("[build] uv payload: {0} ({1:N1} MB, version {2})" -f $uvExe, ((Get-Item -LiteralPath $uvExe).Length / 1MB), $uvVersion)

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
& $iscc "/DVersion=$Version" "/DPayloadDir=$payloadDir" $script
if ($LASTEXITCODE -ne 0) {
    throw "iscc failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $target)) {
    throw "iscc reported success but $target is missing (check OutputBaseFilename)"
}

$size = (Get-Item -LiteralPath $target).Length
Write-Host ("[build] ok: {0} ({1:N0} KB)" -f $target, ($size / 1KB))
