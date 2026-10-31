#requires -Version 5.1
<#
  Build a fully offline install kit for one platform.

  A kit is one zip: the trimmed source tree, every wheel the locked runtime needs for
  that interpreter and machine, the two source-only packages pre-built into wheels, a
  pinned CPython, and uv itself. `install.ps1 -Kit <zip>` installs from it with
  `uv sync --frozen --offline --no-index --find-links <kit>\wheels`, so nothing on the
  device reaches an index. --offline is deliberate: a kit that is missing one artifact
  fails here, in CI, instead of quietly downloading on a user's machine.

    -Platform   win-x64 | linux-x64 | macos-arm64. Run the job on that platform: the
                interpreter is installed by uv, which only builds one for its own host.
    -OutDir     where dist/offline lands (default: dist)
    -Ref        git ref to take the source snapshot from (default: the version tag)
    -SkipChecks build only (the self-check is the product's real test, keep it on)

  Everything the wheels are chosen from comes out of uv.lock through
  packaging/offline_kit_plan.py, so the kit cannot drift from what a normal install
  resolves. The source tree comes from packaging/distribution-excludes.txt, the same
  list the two one-line installers read.
#>
param(
    [Parameter(Mandatory = $true)][string]$Platform,
    [string]$OutDir = 'dist',
    [string]$Ref = '',
    [string]$UvPath = '',
    [switch]$SkipChecks
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Invoke-Step {
    param([string]$Message)
    Write-Host "[kit] $Message"
}

function Invoke-Checked {
    param([string[]]$Command, [string]$Where)
    $exe = $Command[0]
    $rest = @($Command | Select-Object -Skip 1)
    # Native stderr is a terminating error under 'Stop' in PowerShell 5.1, and both uv
    # and git write ordinary progress there; trust only the exit code.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $exe @rest 2>&1 | ForEach-Object { [string]$_ } | Out-Host
    }
    finally {
        $ErrorActionPreference = $saved
    }
    if ($LASTEXITCODE -ne 0) {
        throw "[kit] failed: $exe $($rest -join ' ') (exit $LASTEXITCODE) in $Where"
    }
}

function Find-Uv {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path -LiteralPath $Explicit)) { throw "[kit] uv not found at $Explicit" }
        return (Resolve-Path -LiteralPath $Explicit).Path
    }
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'), (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'))) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    throw '[kit] uv is required (PATH or ~/.local/bin); install it or pass -UvPath'
}

function Remove-Tree {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    # Remove-Item on Windows PowerShell 5.1 cannot delete a path past MAX_PATH, and the
    # copied CPython tree is full of them (site-packages/pip/_internal/.../*.pyc).
    # Mirroring an empty directory with robocopy is the purge that does handle it.
    $empty = Join-Path ([System.IO.Path]::GetTempPath()) ('negi-empty-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $empty | Out-Null
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & robocopy $empty $Path /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = 'Stop'
        Remove-Item -LiteralPath $empty -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($code -gt 7) { throw "[kit] could not clear $Path (robocopy exit $code)" }
    Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
}

function Get-Sha256 {
    param([string]$Path)
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-RefFileText {
    param([string]$Ref, [string]$Path)
    # Two transcode traps on this platform: PowerShell decodes a native command's
    # stdout with the console codepage (GBK here, which mangles the Chinese comments in
    # the exclusion list), and Set-Content -Encoding UTF8 writes a BOM that tomllib
    # rejects at line 1. Decode as UTF-8, write as UTF-8 without BOM.
    $saved = [Console]::OutputEncoding
    $ErrorActionPreference = 'Continue'
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $text = (& git -C $root show "${Ref}:$Path" 2>$null | Out-String)
        $code = $LASTEXITCODE
    }
    finally {
        [Console]::OutputEncoding = $saved
        $ErrorActionPreference = 'Stop'
    }
    if ($code -ne 0 -or -not $text) { return $null }
    return $text
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

$uv = Find-Uv -Explicit $UvPath

# Pin Windows' own bsdtar. Git for Windows ships a GNU tar earlier on some PATHs and it
# reads a drive-letter path as remote-host syntax (Cannot connect to C: resolve failed),
# which is how the source snapshot extraction died on the first local run.
$tarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path -LiteralPath $tarExe)) {
    throw '[kit] Windows bsdtar (System32\tar.exe) is required to pack the kit'
}

$declared = ''
$initText = Get-Content -LiteralPath (Join-Path $root 'g3ku\__init__.py') -Raw
if ($initText -match '__version__\s*=\s*"([^"]+)"') { $declared = $Matches[1] }
if (-not $declared) { throw '[kit] g3ku/__init__.py declares no version' }
if (-not $Ref) { $Ref = "v$declared" }

# Same guard the Windows wizard build runs: a kit named for one version must not carry
# another version's code, because the asset name is how a user picks it.
$ErrorActionPreference = 'Continue'
& git -C $root rev-parse --verify "$Ref^{commit}" 2>$null | Out-Null
$refOk = ($LASTEXITCODE -eq 0)
$gitVersion = Get-RefFileText -Ref $Ref -Path 'g3ku/__init__.py'
$ErrorActionPreference = 'Stop'
if (-not $refOk) {
    throw "[kit] ref $Ref is not present in this checkout; tag it before building a kit (declared version is $declared)"
}
if (-not $gitVersion) {
    throw "[kit] $Ref has no readable g3ku/__init__.py"
}
if ($gitVersion -notmatch ('__version__\s*=\s*"' + [regex]::Escape($declared) + '"')) {
    throw "[kit] $Ref does not declare $declared in g3ku/__init__.py; the asset name would lie"
}

$work = Join-Path $root (Join-Path $OutDir 'offline')
Remove-Tree -Path $work
New-Item -ItemType Directory -Force -Path $work | Out-Null
$kitDir = Join-Path $work "Negi-$declared-offline-$Platform"
foreach ($sub in @('wheels', 'source', 'runtime')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $kitDir $sub) | Out-Null
}

Invoke-Step "platform=$Platform version=$declared ref=$Ref"

# ---------------------------------------------------------------- plan and wheels
$planJson = Join-Path $work 'plan.json'
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
# The lock and the interpreter pin are read out of the same ref the source snapshot
# comes from. Planning from the working tree while packaging the tag would ship a
# source/uv.lock the wheels cannot satisfy.
$refLockText = Get-RefFileText -Ref $Ref -Path 'uv.lock'
if (-not $refLockText) { throw "[kit] $Ref has no uv.lock" }
$refLock = Join-Path $work 'uv.lock'
Write-Utf8NoBom -Path $refLock -Text $refLockText
$refPin = (Get-RefFileText -Ref $Ref -Path '.python-version')
if ($null -eq $refPin) { throw "[kit] $Ref has no readable .python-version" }
$refPin = $refPin.Trim()
if (-not $refPin) { throw "[kit] $Ref has an empty .python-version" }
Invoke-Checked @($python, (Join-Path $root 'packaging\offline_kit_plan.py'),
    '--platform', $Platform, '--json', $planJson, '--quiet', '--lock', $refLock) -Where $root
$plan = Get-Content -LiteralPath $planJson -Raw | ConvertFrom-Json
if (@($plan.missing).Count -gt 0) {
    throw "[kit] no artifact for: $($plan.missing -join ', ') - the kit would fail on a device"
}
Invoke-Step "plan: $($plan.packages) packages, $($plan.wheel_files) wheels, $(@($plan.prebuild).Count) to prebuild, $([math]::Round($plan.bytes_total / 1MB, 1)) MB"

$wheelsDir = Join-Path $kitDir 'wheels'
foreach ($artifact in @($plan.artifacts)) {
    $target = Join-Path $wheelsDir $artifact.filename
    if (-not (Test-Path -LiteralPath $target)) {
        Invoke-WebRequest -Uri $artifact.url -OutFile $target -UseBasicParsing
    }
    $actual = Get-Sha256 -Path $target
    if ($actual -ne $artifact.sha256) {
        throw "[kit] sha256 mismatch for $($artifact.filename): lock says $($artifact.sha256), file is $actual"
    }
}
Invoke-Step "wheels verified: $(@($plan.artifacts).Count) files"

# Packages that publish no wheel at all: build one now (network is allowed here, never
# on the device). Their digest is not in uv.lock, so it is recorded in kit.json instead.
$prebuilt = @()
foreach ($sdist in @($plan.prebuild)) {
    $archive = Join-Path $work $sdist.filename
    if (-not (Test-Path -LiteralPath $archive)) {
        Invoke-WebRequest -Uri $sdist.url -OutFile $archive -UseBasicParsing
    }
    $actual = Get-Sha256 -Path $archive
    if ($actual -ne $sdist.sha256) {
        throw "[kit] sha256 mismatch for sdist $($sdist.filename): lock says $($sdist.sha256), file is $actual"
    }
    Invoke-Step "building wheel from $($sdist.filename)"
    Invoke-Checked @($uv, 'build', $archive, '--wheel', '--out-dir', $wheelsDir, '--quiet') -Where $work
}
foreach ($wheel in Get-ChildItem -LiteralPath $wheelsDir -Filter '*.whl') {
    $known = @($plan.artifacts | Where-Object { $_.filename -eq $wheel.Name })
    if (@($known).Count -eq 0) {
        $prebuilt += [pscustomobject]@{ name = $wheel.Name; filename = $wheel.Name; sha256 = (Get-Sha256 -Path $wheel.FullName); size = $wheel.Length; source = 'prebuild-sdist' }
    }
}
Invoke-Step ("prebuilt wheels: {0} ({1})" -f @($prebuilt).Count, (($prebuilt | ForEach-Object { $_.name }) -join ', '))

# ---------------------------------------------------------------- build backend
# uv.lock does not carry [build-system] requires, and `uv sync` still builds the
# project's own editable install: without these wheels the offline sync dies on
# "hatchling was not found in the provided package locations".
Invoke-Step 'resolving the project build backend'
$backendPinsText = Get-RefFileText -Ref $Ref -Path 'packaging/build-requires.txt'
if (-not $backendPinsText) { throw "[kit] $Ref has no packaging/build-requires.txt" }
$backendPins = Join-Path $work 'build-requires.txt'
Write-Utf8NoBom -Path $backendPins -Text $backendPinsText
$backendJson = Join-Path $work 'build-requires.json'
Invoke-Checked @($python, (Join-Path $root 'packaging\offline_build_deps.py'),
    '--platform', $Platform, '--pins', $backendPins,
    '--download-to', $wheelsDir, '--json', $backendJson) -Where $root
$buildBackend = @((Get-Content -LiteralPath $backendJson -Raw | ConvertFrom-Json).build_requires)

# ---------------------------------------------------------------- source snapshot
# git archive of the ref with the same top-level exclusions an online install applies,
# extracted without a .git: a kit-installed device is an archive-lane device.
$excludeFile = Join-Path $work 'distribution-excludes.txt'
$excludeText = Get-RefFileText -Ref $Ref -Path 'packaging/distribution-excludes.txt'
if ($null -eq $excludeText) {
    # A ref predating the list installs the full tree, exactly like the online lane.
    Write-Host '[kit] this ref carries no distribution-excludes.txt; snapshotting the full tree'
    $excludeText = ''
}
Write-Utf8NoBom -Path $excludeFile -Text $excludeText
$pathspec = @('.')
foreach ($raw in Get-Content -LiteralPath $excludeFile) {
    $name = ('' + $raw).Trim()
    if (-not $name -or $name.StartsWith('#')) { continue }
    if ($name -notmatch '^[A-Za-z0-9._-]+$' -or $name.Contains('..')) {
        throw "[kit] distribution exclusion '$name' is not a single top-level name"
    }
    $pathspec += (':(exclude)' + $name)
}
Invoke-Step 'extracting the trimmed source snapshot'
$tar = Join-Path $work 'source.tar'
$savedPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & git -C $root archive --format=tar -o $tar $Ref -- @pathspec 2>&1 | Out-Host
    $archiveCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $savedPreference
}
if ($archiveCode -ne 0) { throw '[kit] git archive failed' }
New-Item -ItemType Directory -Force -Path (Join-Path $kitDir 'source') | Out-Null
Invoke-Checked @($tarExe, '-xf', $tar, '-C', (Join-Path $kitDir 'source')) -Where $work

# ---------------------------------------------------------------- interpreter and uv
Invoke-Step 'installing the pinned interpreter into the kit'
$pin = $refPin
# --install-dir into the kit itself. `uv python find` is not used because inside a
# project directory it resolves the project's own .venv, which is a scripts folder and
# not an interpreter - the first local run copied .venv\Scripts and called it a runtime.
$runtimeDir = Join-Path $kitDir 'runtime'
Invoke-Checked @($uv, 'python', 'install', $pin, '--install-dir', $runtimeDir, '--no-bin') -Where $root
# -Include is unreliable with -LiteralPath (it silently stops filtering, and the first
# hit here was the .gitignore uv writes into the install dir), so the name test lives
# in code where it cannot quietly do nothing.
$candidates = @(Get-ChildItem -LiteralPath $runtimeDir -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('python.exe', 'python3', 'python') } |
        Sort-Object { (($_.FullName.Substring($runtimeDir.Length)) -split '[\/]').Count }, FullName)
if (@($candidates).Count -eq 0) { throw "[kit] uv python install $pin produced no interpreter under $runtimeDir" }
# Shallowest first, and then actually run it: CPython ships a venv seed stub at
# Lib/venv/scripts/nt/python.exe which sorts before the real interpreter by name and is
# not one. --version is the only thing that tells them apart.
$kitPythonExe = ''
$probeVersion = ''
$saved = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
foreach ($candidate in $candidates) {
    $reported = ''
    try { $reported = ((& $candidate.FullName --version 2>&1 | Out-String).Trim()) } catch { $reported = '' }
    if ($LASTEXITCODE -ne 0 -or $reported -notmatch [regex]::Escape('Python ' + $pin)) { continue }
    if ($reported -notmatch 'Python\s') { continue }
    $kitPythonExe = $candidate.FullName
    $probeVersion = $reported
    break
}
$ErrorActionPreference = $saved
if (-not $kitPythonExe) {
    throw "[kit] no runnable interpreter for $pin under $runtimeDir ($(@($candidates).Count) name matches, none reported 'Python $pin')"
}
# Windows layout is <dir>\python.exe, the standalone one is <dir>\bin\python3.
$pythonHome = Split-Path -Parent $kitPythonExe
if ((Split-Path -Leaf $pythonHome) -eq 'bin') { $pythonHome = Split-Path -Parent $pythonHome }
$pythonRel = ($kitPythonExe.Substring($kitDir.Length).TrimStart('\', '/')) -replace '\\', '/'
# The copied uv keeps the extension its platform needs; naming it by whether the
# destination already exists is how the first run produced an unrunnable 'uv'.
# ($env:OS is the check that works on Windows PowerShell 5.1, which has no $IsWindows;
# [Environment]::OSVersion.Version has no Platform property and StrictMode says so.)
$isWindows = $env:OS -eq 'Windows_NT'
$uvName = if ($isWindows) { 'uv.exe' } else { 'uv' }
Copy-Item -LiteralPath $uv -Destination (Join-Path $kitDir $uvName) -Force
Invoke-Step "interpreter: $pythonRel ($probeVersion); uv: $uvName"

# ---------------------------------------------------------------- kit manifest
$wheelFiles = @()
foreach ($wheel in Get-ChildItem -LiteralPath $wheelsDir -Filter '*.whl' | Sort-Object Name) {
    $wheelFiles += [pscustomobject]@{
        filename = $wheel.Name
        sha256   = (Get-Sha256 -Path $wheel.FullName)
        size     = $wheel.Length
    }
}
$manifest = [ordered]@{
    schema            = 1
    version           = $declared
    ref               = $Ref
    platform          = $Platform
    python_version    = $pin
    python_executable = $pythonRel
    uv_binary         = $uvName
    lock_sha256       = $plan.lock_sha256
    source_mode       = 'archive'
    wheels            = $wheelFiles
    wheels_total_size = ($wheelFiles | Measure-Object -Property size -Sum).Sum
    prebuilt          = @($prebuilt | ForEach-Object { [pscustomobject]@{ filename = $_.filename; sha256 = $_.sha256; size = $_.size } })
    build_backend     = @($buildBackend | ForEach-Object { [pscustomobject]@{ filename = $_.filename; sha256 = $_.sha256; size = $_.size } })
    built_at          = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
}
$manifestPath = Join-Path $kitDir 'kit.json'
(ConvertTo-Json -Depth 6 -InputObject $manifest) | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Invoke-Step "kit.json: $($wheelFiles.Count) wheels, $([math]::Round((($wheelFiles | Measure-Object -Property size -Sum).Sum) / 1MB, 1)) MB of artifacts"

# ---------------------------------------------------------------- offline self-check
if (-not $SkipChecks) {
    # The only reading that matters: a fresh environment built with the network turned
    # off. Anything the kit cannot supply fails here rather than on a device.
    Invoke-Step 'self-check: uv sync --frozen --offline'
    $probe = Join-Path $env:TEMP ("negi-kit-probe-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $probe | Out-Null
    try {
        Copy-Item -LiteralPath (Join-Path $kitDir 'source') -Destination (Join-Path $probe 'proj') -Recurse -Force
        $proj = Join-Path $probe 'proj'
        $kitPython = Join-Path $kitDir $manifest.python_executable
        $kitUv = Join-Path $kitDir $manifest.uv_binary
        Invoke-Checked @($kitUv, 'venv', '--relocatable', '--python', $kitPython, (Join-Path $proj '.venv')) -Where $proj
        $env:UV_OFFLINE = '1'
        try {
            Invoke-Checked @($kitUv, 'sync', '--frozen', '--offline', '--no-index',
                '--python', $kitPython, '--find-links', (Join-Path $kitDir 'wheels'),
                '--project', $proj) -Where $proj
        }
        finally {
            Remove-Item Env:UV_OFFLINE -ErrorAction SilentlyContinue
        }
        $venvPython = Join-Path $proj '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $venvPython)) { $venvPython = Join-Path $proj '.venv/bin/python' }
        # From the project directory, like a device: only g3ku is installed as an
        # editable package, while main/ is a top-level directory the app imports from
        # its working directory. No nested quotes either - PowerShell eats them.
        Push-Location $proj
        try {
            Invoke-Checked @($venvPython, '-c',
                'import main.protocol, g3ku.cli.commands, g3ku.web.launcher, g3ku; print(g3ku.__version__)') -Where $proj
        }
        finally {
            Pop-Location
        }
    }
    finally {
        Remove-Tree -Path $probe
    }
}

# ---------------------------------------------------------------- zip
$asset = Join-Path $root (Join-Path $OutDir "Negi-$declared-offline-$Platform.zip")
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $asset) | Out-Null
if (Test-Path -LiteralPath $asset) { Remove-Item -LiteralPath $asset -Force }
# bsdtar's -a picks the format from the extension and is far faster than
# Compress-Archive on a tree with a Python runtime inside it.
Invoke-Checked @($tarExe, '-a', '-cf', $asset, '-C', $kitDir, '.') -Where $kitDir
Invoke-Step ("ok: {0} ({1:N1} MB)" -f $asset, ((Get-Item -LiteralPath $asset).Length / 1MB))
Write-Output $asset
