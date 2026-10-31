#requires -Version 5.1
<#
  G3KU one-line installer (Windows).

    iwr https://github.com/ZXY39/Negi/releases/latest/download/install.ps1 | iex

  Provisions uv (and therefore Python) on a machine that has neither, fetches the
  pinned checkout, syncs the locked environment and hands off to negi_bootstrap.py.
  Runtime code is not modified by this script.

  Negi-Setup.exe passes -Kit: the win-x64 offline kit it carries, holding the trimmed
  source tree, every wheel, a pinned CPython, uv and kit.json. That lane verifies the
  kit against this device and installs without touching an index or GitHub. Without
  -Kit the network lanes below are the install.
#>
param(
    [string]$Dir = (Join-Path $env:USERPROFILE 'Negi'),
    [string]$Ref = 'v2.9.1',
    [switch]$NoStart,
    [switch]$Upgrade,
    [switch]$NoPath,
    [switch]$NoShortcut,
    [switch]$NoUninstallEntry,
    [string]$Kit = '',
    [switch]$Mirror,
    [switch]$NoMirror
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($Mirror -and $NoMirror) {
    Write-Error '[install] -Mirror and -NoMirror cannot be combined'
}

$RepoOwner = 'ZXY39'
$RepoName = 'Negi'
$RepoGit = "https://github.com/$RepoOwner/$RepoName.git"
$RepoZip = "https://github.com/$RepoOwner/$RepoName/archive/${Ref}.zip"
$UvInstaller = 'https://astral.sh/uv/install.ps1'
# Mirrors are only used after the direct source fails, unless -Mirror asks for
# them up front. UV_DEFAULT_INDEX / UV_PYTHON_INSTALL_MIRROR keep working and
# suppress the automatic retry (they are an explicit choice already).
$PyPiMirror = 'https://pypi.tuna.tsinghua.edu.cn/simple'
$PythonMirror = 'https://mirror.sjtu.edu.cn/github-release/astral-sh/python-build-standalone/releases/download'
# Kept across upgrades: the environment and everything the operator created.
$ProtectedEntries = @('.venv', '.g3ku', '.git')
# Per-user install surface outside the project root: the command stub, the icon
# the shortcuts point at, and the pointer that tells the stub where the code is.
$AppDisplayName = 'Negi'
$StateDir = Join-Path $env:LOCALAPPDATA $AppDisplayName
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppDisplayName"

function Write-Step {
    param([string]$Message)
    Write-Host "[install] $Message"
}

function Invoke-Checked {
    param([string[]]$Command, [string]$Where)
    $exe = $Command[0]
    $rest = @($Command | Select-Object -Skip 1)
    # PowerShell 5.1 promotes native stderr to a terminating error under
    # $ErrorActionPreference='Stop', and uv writes ordinary progress there. The
    # exit code is the only failure signal, so relax the preference for the call
    # and push the child's output to the console instead of the success stream
    # (which is the caller's return value).
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $exe @rest 2>&1 | ForEach-Object { [string]$_ } | Out-Host
    }
    finally {
        $ErrorActionPreference = $saved
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[install] failed: $exe $($rest -join ' ') (exit $LASTEXITCODE) in $Where"
    }
}

function Get-UvCommand {
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    foreach ($candidate in @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'), (Join-Path $env:USERPROFILE '\.cargo\bin\uv.exe'))) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Install-BundledUv {
    param([string]$Path)
    # The exe's {tmp} disappears after setup, and later upgrades (including the
    # web "restart and update" lane) need uv again. The official astral layout
    # is %USERPROFILE%\.local\bin, which Get-UvCommand already probes.
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $null }
    $bin = Join-Path $env:USERPROFILE '.local\bin'
    $target = Join-Path $bin 'uv.exe'
    try {
        New-Item -ItemType Directory -Force -Path $bin | Out-Null
        Copy-Item -LiteralPath $Path -Destination $target -Force
    }
    catch {
        Write-Step "could not persist the bundled uv: $($_.Exception.Message)"
        return $null
    }
    return $target
}

function Get-DeviceKitPlatform {
    # Keeps the check honest: a kit is named for the machine it installs on, and a
    # win-x64 kit on an arm64 device (or a linux kit here) must be refused, not half-applied.
    if ($env:OS -ne 'Windows_NT') { return 'unknown' }
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
    return "win-$arch"
}

function Clear-LongPathTree {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return }
    # A kit staging directory contains a copied CPython tree, and Remove-Item on
    # Windows PowerShell 5.1 cannot delete paths past MAX_PATH. Mirroring an empty
    # directory with robocopy is the purge that can.
    if ($env:OS -ne 'Windows_NT') {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
        return
    }
    $empty = Join-Path ([System.IO.Path]::GetTempPath()) ('negi-empty-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $empty | Out-Null
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & robocopy $empty $Path /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $saved
        Remove-Item -LiteralPath $empty -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($code -gt 7) { Write-Step "could not clear the kit staging directory (robocopy exit $code)" }
    Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
}

function Resolve-OfflineKit {
    param([string]$KitArg, [string]$RefValue, [switch]$RefExplicit, [string]$StageRoot)
    # A kit is a directory or a zip holding source/, wheels/, runtime/, uv and kit.json.
    # Nothing here reaches the network: the point of the kit is a device that cannot.
    if (-not (Test-Path -LiteralPath $KitArg)) {
        Write-Error "[install] -Kit not found: $KitArg"
    }
    $root = (Resolve-Path -LiteralPath $KitArg).Path
    if (-not (Test-Path -LiteralPath (Join-Path $root 'kit.json'))) {
        $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
        if (-not (Test-Path -LiteralPath $tar)) { Write-Error '[install] bsdtar is required to open a kit archive' }
        $extracted = Join-Path $StageRoot 'kit'
        New-Item -ItemType Directory -Force -Path $extracted | Out-Null
        Invoke-Checked @($tar, '-xf', $root, '-C', $extracted) -Where $extracted
        $root = $extracted
    }
    $manifestPath = Join-Path $root 'kit.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) { Write-Error "[install] no kit.json under $root" }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

    $device = Get-DeviceKitPlatform
    if ($manifest.platform -ne $device) {
        Write-Error "[install] this kit is for $($manifest.platform); this device is $device"
    }
    $initText = Get-Content -LiteralPath (Join-Path $root 'source\g3ku\__init__.py') -Raw
    if ($initText -notmatch '__version__\s*=\s*"([^"]+)"') {
        Write-Error '[install] the kit source declares no version'
    }
    $sourceVersion = $Matches[1]
    if ($sourceVersion -ne $manifest.version) {
        Write-Error "[install] kit.json says $($manifest.version) but its source declares $sourceVersion"
    }
    if ($RefExplicit -and $RefValue -and $RefValue -ne "v$sourceVersion") {
        Write-Error "[install] -Ref $RefValue conflicts with the kit's version v$sourceVersion"
    }
    $checked = 0
    $bytes = 0
    foreach ($wheel in @($manifest.wheels)) {
        $wheelPath = Join-Path (Join-Path $root 'wheels') $wheel.filename
        if (-not (Test-Path -LiteralPath $wheelPath)) {
            Write-Error "[install] kit is missing $($wheel.filename)"
        }
        $actual = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $wheel.sha256) {
            Write-Error "[install] kit wheel $($wheel.filename) does not match its recorded sha256"
        }
        $checked++
        $bytes += [int64]$wheel.size
    }
    $python = Join-Path $root $manifest.python_executable
    if (-not (Test-Path -LiteralPath $python)) { Write-Error "[install] kit interpreter is missing: $python" }
    $uvPath = Join-Path $root $manifest.uv_binary
    if (-not (Test-Path -LiteralPath $uvPath)) { Write-Error "[install] kit uv is missing: $uvPath" }
    # The requirement list is what the offline install actually reads. A kit built before
    # it existed carries no such field; under StrictMode reading a missing property is a
    # terminating error, so it is looked for by name first and refused with a sentence.
    $reqName = ''
    if ($manifest.PSObject.Properties.Name -contains 'requirements') { $reqName = [string]$manifest.requirements }
    if (-not $reqName) { Write-Error '[install] this kit has no requirements list; rebuild it from a newer tag' }
    $requirements = Join-Path $root $reqName
    if (-not (Test-Path -LiteralPath $requirements)) { Write-Error "[install] kit requirement list is missing: $requirements" }
    Write-Step ("kit v$sourceVersion verified: $checked wheels ({0:N1} MB)" -f ($bytes / 1MB))
    return [pscustomobject]@{
        Root = $root
        Source = (Join-Path $root 'source')
        Wheels = (Join-Path $root 'wheels')
        Python = $python
        Uv = $uvPath
        Requirements = $requirements
        Version = $sourceVersion
    }
}

function Ensure-Uv {
    $uv = Get-UvCommand
    if ($uv) {
        Write-Step "using uv: $uv"
        return $uv
    }
    Write-Step 'uv not found, installing it from astral.sh'
    Invoke-Checked @('powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', "irm $UvInstaller | iex") -Where 'installer'
    $uv = Get-UvCommand
    if (-not $uv) {
        Write-Error '[install] uv installer finished but uv is still not reachable. Add %USERPROFILE%\.local\bin to PATH and rerun.'
    }
    return $uv
}

function Get-ProjectPin {
    param([string]$Root)
    $pinFile = Join-Path $Root '.python-version'
    if (-not (Test-Path $pinFile)) { return $null }
    $raw = Get-Content -LiteralPath $pinFile -Raw
    if ($raw) { return $raw.Trim() }
    return $null
}

function Get-DistributionExcludes {
    param([string]$Source, [switch]$FromGit)
    # The exclusion list travels inside the payload (packaging/distribution-excludes.txt),
    # so the installer never carries a second copy that could drift. -FromGit reads it out
    # of the fetched commit while the worktree is still empty.
    $names = @()
    if ($FromGit) {
        $saved = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $lines = @(& git -C $Source show 'HEAD:packaging/distribution-excludes.txt' 2>$null)
            $ok = ($LASTEXITCODE -eq 0)
        }
        finally { $ErrorActionPreference = $saved }
        if (-not $ok) { return @() }
    }
    else {
        $file = Join-Path $Source 'packaging\distribution-excludes.txt'
        if (-not (Test-Path -LiteralPath $file)) { return @() }
        $lines = @(Get-Content -LiteralPath $file)
    }
    foreach ($line in $lines) {
        $name = ('' + $line).Trim()
        if (-not $name -or $name.StartsWith('#')) { continue }
        if ($name -notmatch '^[A-Za-z0-9._-]+$' -or $name.Contains('..')) {
            Write-Error "[install] distribution exclusion '$name' is not a single top-level name"
        }
        $names += $name
    }
    return $names
}

function Copy-CheckoutInto {
    param([string]$Source, [string]$Root, [switch]$KeepProtected)
    # Top-level merge: environment and operator data are never touched on an
    # upgrade. Files the new release deleted stay behind until a reinstall.
    $excludes = Get-DistributionExcludes -Source $Source
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        if ($KeepProtected -and $ProtectedEntries -contains $_.Name) { return }
        if ($_.Name -eq '.git' -and (Test-Path (Join-Path $Root '.git'))) { return }
        if ($excludes -contains $_.Name) { return }
        Copy-Item -LiteralPath $_.FullName -Destination $Root -Recurse -Force
    }
}

function Get-CheckoutVersion {
    param([string]$Checkout)
    $initFile = Join-Path $Checkout 'g3ku\__init__.py'
    if (-not (Test-Path -LiteralPath $initFile)) { return '' }
    $text = Get-Content -LiteralPath $initFile -Raw
    if ($text -match '__version__\s*=\s*"([^"]+)"') { return $Matches[1] }
    return ''
}

function Get-CheckoutStage {
    param([string]$StageRoot, [switch]$Archive, [object]$Kit = $null)
    if ($Kit) {
        # The kit already holds the tree; no clone and no archive download, because the
        # device it is meant for cannot reach GitHub either.
        Write-Step "using the offline kit's source tree (v$($Kit.Version))"
        return $Kit.Source
    }
    if (-not $Archive -and (Get-Command git -ErrorAction SilentlyContinue)) {
        $target = Join-Path $StageRoot 'checkout'
        Write-Step "git clone --depth 1 --branch $Ref (staged)"
        Invoke-Checked @('git', 'clone', '--depth', '1', '--branch', $Ref, '--no-checkout', $RepoGit, $target) -Where $target
        # Sparse rules, never a delete: skipping the files after checkout would leave
        # every one of them reported as removed, and -Upgrade's first guard is "no
        # tracked changes", so a delete would lock the device out of upgrading forever.
        # The rules live in .git and travel with it, so later checkouts stay trimmed too.
        $excludes = Get-DistributionExcludes -Source $target -FromGit
        if (@($excludes).Count -gt 0) {
            $patterns = @('/*')
            foreach ($name in $excludes) { $patterns += "!/$name/" }
            Write-Step "sparse checkout: excluding $($excludes -join ', ')"
            Invoke-Checked (@('git', '-C', $target, 'sparse-checkout', 'set', '--no-cone') + $patterns) -Where $target
        }
        Invoke-Checked @('git', '-C', $target, 'checkout') -Where $target
        return $target
    }
    Write-Step "downloading $RepoZip"
    $zip = Join-Path $StageRoot 'source.zip'
    Invoke-WebRequest -Uri $RepoZip -OutFile $zip -UseBasicParsing
    $expanded = Join-Path $StageRoot 'expanded'
    Expand-Archive -LiteralPath $zip -DestinationPath $expanded -Force
    $inner = Get-ChildItem -LiteralPath $expanded -Directory | Select-Object -First 1
    if (-not $inner) {
        Write-Error "[install] archive for $Ref contained no top-level directory"
    }
    return $inner.FullName
}

function Install-CodeInto {
    param([string]$Root, [switch]$KeepProtected, [switch]$Archive, [object]$Kit = $null)
    # Code always lands in a staging directory first and is merged top-level:
    # `git clone` refuses a non-empty target, and a keep-data uninstall leaves
    # exactly that (a directory holding only .g3ku).
    $stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('negi-stage-' + [Guid]::NewGuid().ToString('N'))
    try {
        New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null
        $checkout = Get-CheckoutStage -StageRoot $stageRoot -Archive:$Archive -Kit $Kit
        Copy-CheckoutInto -Source $checkout -Root $Root -KeepProtected:$KeepProtected
    }
    finally {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Test-TrackedChanges {
    param([string]$Root)
    # Only content differences of tracked files count: untracked entries (market
    # skills, bridge artifacts, local scripts) are never empty on a real device, so
    # counting them would flag every upgrade. The check uses diff, not status --
    # on autocrlf devices git checks out CRLF while the runtime writes LF back, and
    # status reports that byte-identical line-ending difference as modified (measured:
    # same blob hash); diff normalizes and calls it clean. --cached covers staged-only
    # edits. PS 5.1 under 'Stop' promotes a native command's stderr to a terminating
    # error, and git warns on exactly this kind of tree; relax it like Invoke-Checked
    # does and trust only the exit code.
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & git -C $Root diff --quiet --ignore-cr-at-eol 2>$null
        $worktreeDirty = ($LASTEXITCODE -ne 0)
        & git -C $Root diff --cached --quiet --ignore-cr-at-eol 2>$null
        $stagedDirty = ($LASTEXITCODE -ne 0)
    }
    finally {
        $ErrorActionPreference = $savedPreference
    }
    return ($worktreeDirty -or $stagedDirty)
}

function Save-TrackedChanges {
    param([string]$Root, [string]$Label)
    # Local edits are neither overwritten nor turned into a permanent upgrade
    # blocker: the whole tracked working tree goes into a stash before the code is
    # swapped and is put back afterwards. `git stash` writes commit objects and
    # therefore needs an identity, which a freshly installed device does not have --
    # an upgrade must not stall on git config, so one is supplied inline.
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $code = 1
    try {
        & git -C $Root -c user.name=negi-upgrade -c user.email=negi-upgrade@localhost stash push --quiet --message $Label 2>&1 | ForEach-Object { [string]$_ } | Out-Host
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedPreference
    }
    if ($code -ne 0) {
        Write-Error "[install] could not stash the local edits in $Root; commit or discard them before upgrading"
    }
}

function Restore-TrackedChanges {
    param([string]$Root, [string]$Label)
    # A conflict means the new release touched the same lines. Drop the merge
    # result -- the edits themselves stay in the stash -- because a tree left with
    # conflict markers cannot serve: the installer is about to hand it to a live
    # process.
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $code = 1
    try {
        & git -C $Root stash pop --quiet 'stash@{0}' 2>&1 | ForEach-Object { [string]$_ } | Out-Host
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            & git -C $Root reset --hard HEAD 2>&1 | Out-Null
        }
    }
    finally {
        $ErrorActionPreference = $savedPreference
    }
    if ($code -eq 0) {
        Write-Step 'local edits restored'
        return
    }
    Write-Step "local edits conflict with $Ref; kept in git stash as $Label (resolve with: git -C $Root stash pop)"
}

function Update-GitCheckout {
    param([string]$Root)
    $label = 'negi-upgrade-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $stashed = $false
    if (Test-TrackedChanges -Root $Root) {
        Write-Step "local edits to tracked files found; stashing them as $label (restored after the upgrade)"
        Save-TrackedChanges -Root $Root -Label $label
        $stashed = $true
    }
    try {
        Write-Step "git fetch --depth 1 origin $Ref"
        Invoke-Checked @('git', '-C', $Root, 'fetch', '--depth', '1', 'origin', $Ref) -Where $Root
        Invoke-Checked @('git', '-C', $Root, 'checkout', '--detach', 'FETCH_HEAD') -Where $Root
        Write-Step "code updated to $Ref"
    }
    catch {
        # A failed fetch leaves the tree on the old commit, so put the edits back
        # before rethrowing: a failed upgrade must not hide them in the stash.
        if ($stashed) { Restore-TrackedChanges -Root $Root -Label $label }
        throw
    }
    if ($stashed) { Restore-TrackedChanges -Root $Root -Label $label }
}

function Update-Code {
    param([string]$Root, [object]$Kit = $null)
    if ($Kit) {
        if (Test-Path (Join-Path $Root 'pyproject.toml')) {
            if (-not $Upgrade) {
                Write-Step "checkout already present at $Root, code untouched (pass -Upgrade to update it)"
                return
            }
            if (Test-Path (Join-Path $Root '.git')) {
                # Copying a kit over a git checkout leaves that .git describing a
                # different tree, and -Upgrade's first guard is "no tracked changes" -
                # the device would be unable to upgrade by either lane afterwards.
                Write-Step "[install] $Root is a git checkout; a kit upgrade would leave it inconsistent with its own .git. Remove $Root\.git first if the kit should own this tree."
                exit 1
            }
            Write-Step "upgrading code from the kit v$($Kit.Version) (keeping .venv and .g3ku)"
            Install-CodeInto -Root $Root -KeepProtected -Kit $Kit
            return
        }
        New-Item -ItemType Directory -Force -Path $Root | Out-Null
        Install-CodeInto -Root $Root -Kit $Kit
        return
    }
    if (Test-Path (Join-Path $Root 'pyproject.toml')) {
        if (-not $Upgrade) {
            Write-Step "checkout already present at $Root, code untouched (pass -Upgrade to update it)"
            return
        }
        if ((Test-Path (Join-Path $Root '.git')) -and (Get-Command git -ErrorAction SilentlyContinue)) {
            Update-GitCheckout -Root $Root
            return
        }
        if (Test-Path (Join-Path $Root '.git')) {
            Write-Error "[install] $Root is a git checkout but git is unavailable; install git so the upgrade stays consistent"
        }
        Write-Step "upgrading code from the $Ref source archive (keeping .venv and .g3ku)"
        Install-CodeInto -Root $Root -KeepProtected -Archive
        return
    }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    if (Test-Path (Join-Path $Root '.g3ku\config.json')) {
        # A device whose uninstall kept its data: the code is gone but the
        # passphrase, sessions and tasks are still here, so adopt them instead of
        # initializing a second project.
        Write-Step "adopting the data root already present at $Root (config, sessions and keys kept)"
    }
    Install-CodeInto -Root $Root
}

function Invoke-UvCapture {
    param([string[]]$Command)
    # uv writes progress to stderr; capture both streams without letting PS 5.1
    # promote stderr into a terminating error, and keep the exit code for retries.
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
    foreach ($line in $lines) { Write-Host $line }
    return @{ Code = $code }
}

function Install-ProjectPython {
    param([string]$uv, [string]$Pin)
    Write-Step "uv python install $Pin"
    $first = Invoke-UvCapture @($uv, 'python', 'install', $Pin)
    if ($first['Code'] -eq 0) { return }
    if ($NoMirror -or $env:UV_PYTHON_INSTALL_MIRROR) {
        Write-Error "[install] uv python install failed (exit $($first['Code'])); set UV_PYTHON_INSTALL_MIRROR to a reachable mirror and rerun"
    }
    Write-Step "python download failed; retrying via $PythonMirror"
    $env:UV_PYTHON_INSTALL_MIRROR = $PythonMirror
    $second = Invoke-UvCapture @($uv, 'python', 'install', $Pin)
    if ($second['Code'] -ne 0) {
        Write-Error "[install] uv python install failed direct and via $PythonMirror; set UV_PYTHON_INSTALL_MIRROR yourself"
    }
}

function Sync-ProjectEnvironment {
    param([string]$uv)
    Write-Step 'uv sync --frozen'
    $first = Invoke-UvCapture @($uv, 'sync', '--frozen')
    if ($first['Code'] -eq 0) { return }
    if ($NoMirror -or $env:UV_DEFAULT_INDEX) {
        Write-Error "[install] uv sync failed (exit $($first['Code'])); set UV_DEFAULT_INDEX to a reachable index and rerun"
    }
    Write-Step "dependency download failed; retrying via $PyPiMirror"
    $env:UV_DEFAULT_INDEX = $PyPiMirror
    $second = Invoke-UvCapture @($uv, 'sync', '--frozen')
    if ($second['Code'] -ne 0) {
        Write-Error "[install] uv sync failed with PyPI and $PyPiMirror; set UV_DEFAULT_INDEX yourself"
    }
}

function Install-OfflineEnvironment {
    param([object]$Kit, [string]$Root)
    # The venv records its base interpreter, so the kit's runtime has to be somewhere
    # permanent before the venv is built - not inside the staging directory that is
    # deleted when the install finishes.
    $runtimeHome = Join-Path $Root '.runtime'
    $runtimeTarget = Join-Path $runtimeHome 'negi-python'
    $sourceRuntime = Split-Path -Parent $Kit.Python
    if ((Split-Path -Leaf $sourceRuntime) -eq 'bin') { $sourceRuntime = Split-Path -Parent $sourceRuntime }
    if (Test-Path -LiteralPath $runtimeTarget) { Clear-LongPathTree -Path $runtimeTarget }
    New-Item -ItemType Directory -Force -Path $runtimeHome | Out-Null
    Copy-Item -LiteralPath $sourceRuntime -Destination $runtimeTarget -Recurse -Force
    $python = if ($env:OS -eq 'Windows_NT') { Join-Path $runtimeTarget 'python.exe' } else { Join-Path $runtimeTarget 'bin/python3' }
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Error "[install] the copied kit runtime has no interpreter at $python"
    }
    $uv = Get-UvCommand
    if (-not $uv) { $uv = Install-BundledUv -Path $Kit.Uv }
    if (-not $uv) {
        Write-Error '[install] neither the kit nor the device provides uv, so the environment cannot be built offline'
    }
    if (-not (Test-Path (Join-Path $Root '.venv'))) {
        Write-Step 'creating the virtual environment from the kit runtime'
        Invoke-Checked @($uv, 'venv', '--relocatable', '--python', $python, (Join-Path $Root '.venv')) -Where $Root
    }
    $venvPython = Get-VenvPython -Root $Root
    # `uv pip install` and not `uv sync`: sync goes looking for the URLs recorded in
    # uv.lock, and with --offline it reads them out of uv's download cache - so a device
    # that has never downloaded anything fails there even with every wheel sitting in the
    # kit. pip install treats --find-links as the source of the packages themselves, which
    # is all an offline machine actually has.
    $installArgs = @($uv, 'pip', 'install', '--python', $venvPython, '--offline', '--no-index',
        '--find-links', $Kit.Wheels)
    Write-Step 'uv pip install --offline --no-index (kit wheels, no index)'
    Push-Location -LiteralPath $Root
    try {
        Invoke-Checked ($installArgs + @('-r', $Kit.Requirements)) -Where $Root
        # The project itself last: its editable build needs the [build-system] wheels the
        # kit carries, and that is the part no device can fetch without an index.
        Invoke-Checked ($installArgs + @('-e', $Root)) -Where $Root
        # Without this, the first `negi` run calls _ensure_project_installed(), which
        # runs `uv sync --frozen` and reaches for an index this device does not have.
        Invoke-Checked @($venvPython, (Join-Path $Root 'negi_bootstrap.py'), 'mark-installed') -Where $Root
    }
    finally { Pop-Location }
}

function Install-Environment {
    param([string]$uv, [string]$Root, [object]$Kit = $null)
    if ($Kit) {
        Install-OfflineEnvironment -Kit $Kit -Root $Root
        return
    }
    $pin = Get-ProjectPin -Root $Root
    if ($pin) { Install-ProjectPython -uv $uv -Pin $pin }
    Push-Location -LiteralPath $Root
    try {
        Sync-ProjectEnvironment -uv $uv
    }
    finally {
        Pop-Location
    }
}

function Get-VenvPython {
    param([string]$Root)
    foreach ($candidate in @((Join-Path $Root '.venv\Scripts\python.exe'), (Join-Path $Root '.venv/bin/python'))) {
        if (Test-Path $candidate) { return $candidate }
    }
    Write-Error "[install] no interpreter inside $Root/.venv after uv sync"
}

function Get-UserBinDir {
    return Join-Path $env:USERPROFILE '.local\bin'
}

function Get-TreeSizeBytes {
    param([string]$Path)
    # EstimatedSize and the uninstall dialog both need a real number; a .NET
    # stack walk stays in the seconds range on a 14k-file .venv, where
    # Get-ChildItem -Recurse | Measure-Object builds an object per file.
    $total = 0
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($Path)
    while ($stack.Count -gt 0) {
        $entries = @()
        try { $entries = [System.IO.Directory]::EnumerateFileSystemEntries($stack.Pop()) } catch { continue }
        foreach ($entry in $entries) {
            try {
                if ([System.IO.Directory]::Exists($entry)) { $stack.Push($entry) }
                else { $total += (New-Object System.IO.FileInfo($entry)).Length }
            } catch { }
        }
    }
    return $total
}

function Add-UserPathEntry {
    param([string]$Directory)
    # Returns $true only when this install is the one that added the entry, so
    # uninstall can take it back without touching a PATH the operator edited.
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($current) { $parts = @($current -split ';' | Where-Object { $_ }) }
    $wanted = $Directory.TrimEnd('\')
    foreach ($part in $parts) {
        if ($part.TrimEnd('\') -ieq $wanted) { return $false }
    }
    # .NET broadcasts WM_SETTINGCHANGE here, so terminals opened from Explorer
    # later pick the entry up without a sign-out.
    [Environment]::SetEnvironmentVariable('Path', (($parts + $wanted) -join ';'), 'User')
    $env:Path = "$env:Path;$wanted"
    return $true
}

function Write-InstallPointer {
    param([string]$Root, [bool]$PathEntryAdded, [string]$BinDir)
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    $payload = [ordered]@{
        version          = $Ref
        root             = $Root
        bin_dir          = $BinDir
        path_entry_added = $PathEntryAdded
        installed_at     = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    } | ConvertTo-Json
    Set-Content -LiteralPath (Join-Path $StateDir 'install-root.json') -Value $payload -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $BinDir 'negi-root.cmd') -Value "@echo off`r`nset `"G3KU_HOME=$Root`"" -Encoding ASCII
}

function Install-EntryPoint {
    param([string]$Root)
    $bin = Get-UserBinDir
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $stub = Join-Path $bin 'negi.cmd'
    $stubBody = @'
@echo off
setlocal
if not defined G3KU_HOME if exist "%~dp0negi-root.cmd" call "%~dp0negi-root.cmd"
if not defined G3KU_HOME goto :noroot
set "NEGI_PYTHON=%G3KU_HOME%\.venv\Scripts\python.exe"
if not exist "%NEGI_PYTHON%" goto :nopython
"%NEGI_PYTHON%" "%G3KU_HOME%\negi_bootstrap.py" %*
exit /b %ERRORLEVEL%
:nopython
echo [negi] %NEGI_PYTHON% is missing. Run install.ps1 again to rebuild the environment.
exit /b 1
:noroot
echo [negi] Negi install root is unknown. Set G3KU_HOME to the Negi folder, or run install.ps1 again.
exit /b 1
'@
    Set-Content -LiteralPath $stub -Value $stubBody -Encoding ASCII
    $added = $false
    if (-not $NoPath) { $added = Add-UserPathEntry -Directory $bin }
    Write-InstallPointer -Root $Root -PathEntryAdded $added -BinDir $bin
    $resolved = Get-Command negi -ErrorAction SilentlyContinue
    if ($resolved) {
        Write-Step "command ready: negi -> $($resolved.Source)"
    }
    elseif ($NoPath) {
        Write-Step "stub written to $stub (-NoPath: PATH untouched; add $bin yourself)"
    }
    else {
        Write-Step "stub written to $stub but $bin is not reachable in this session; open a new terminal, or add it to PATH"
    }
}

function Copy-NegiIcon {
    param([string]$Root)
    # The tracked favicon is already a square multi-size .ico, so no image
    # conversion happens at install time. It lives under LOCALAPPDATA because
    # -Upgrade merges top-level entries over the project root.
    $source = Join-Path $Root 'g3ku\web\frontend\favicon.ico'
    if (-not (Test-Path $source)) { return $null }
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    $target = Join-Path $StateDir 'negi.ico'
    Copy-Item -LiteralPath $source -Destination $target -Force
    return $target
}

function Install-DesktopIcons {
    param([string]$Root, [string]$IconPath)
    $pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path $pythonw)) {
        Write-Step "no pythonw.exe under $Root, skipping shortcuts"
        return
    }
    $targets = @()
    $desktop = [Environment]::GetFolderPath('Desktop')
    if ($desktop) { $targets += (Join-Path $desktop "$AppDisplayName.lnk") }
    $programs = [Environment]::GetFolderPath('Programs')
    if ($programs) { $targets += (Join-Path $programs "$AppDisplayName.lnk") }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($target in $targets) {
        $link = $shell.CreateShortcut($target)
        # pythonw allocates no console, so the icon starts Negi without the
        # window flash a .cmd or -WindowStyle Hidden would still show. `--window`
        # asks for the desktop shell; it falls back to the browser when the shell
        # cannot start, so the icon never ends up doing nothing.
        $link.TargetPath = $pythonw
        $link.Arguments = """$Root\negi_bootstrap.py"" start --window"
        $link.WorkingDirectory = $Root
        $link.Description = 'Start Negi and open the browser'
        if ($IconPath) { $link.IconLocation = "$IconPath,0" }
        $link.Save()
        Write-Step "shortcut: $target"
    }
}

function Get-RegistryLocation {
    param([string]$Path)
    # A `*_is1` key without InstallLocation is malformed but real; under
    # StrictMode the missing property is a terminating error, not $null.
    try {
        $value = (Get-ItemProperty -LiteralPath $Path -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
    }
    catch {
        return ''
    }
    if ($value) { return [string]$value }
    return ''
}

function Get-SetupExeOwner {
    param([string]$Root)
    # Inno Setup registers its uninstall key as '<AppId>_is1' and stores the chosen
    # folder in InstallLocation. A row like that means the setup program owns how
    # this install is uninstalled, so this script must not add a second row named
    # 'Negi' pointing at uninstall.ps1: two rows for one install, removing it two
    # different ways, is worse than either one alone.
    $parent = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
    if (-not (Test-Path -LiteralPath $parent)) { return '' }
    $wanted = $Root.TrimEnd('\')
    foreach ($entry in (Get-ChildItem -LiteralPath $parent -ErrorAction SilentlyContinue)) {
        if ($entry.PSChildName -notlike '*_is1') { continue }
        $location = Get-RegistryLocation -Path $entry.PSPath
        if ($location -and ($location.TrimEnd('\') -ieq $wanted)) { return $entry.PSChildName }
    }
    return ''
}

function Write-UninstallRegistryEntry {
    param([string]$Root, [string]$IconPath)
    $sizeBytes = Get-TreeSizeBytes -Path $Root
    $uninstaller = Join-Path $Root 'uninstall.ps1'
    $fields = [ordered]@{
        DisplayName     = $AppDisplayName
        DisplayVersion  = $Ref.TrimStart('v')
        Publisher       = $RepoOwner
        InstallLocation = $Root
        UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstaller`""
        EstimatedSize   = [int][math]::Round($sizeBytes / 1KB)
        NoModify        = 1
        NoRepair        = 1
        URLInfoAbout    = "https://github.com/$RepoOwner/$RepoName"
    }
    if ($IconPath) { $fields['DisplayIcon'] = "$IconPath,0" }
    if (-not (Test-Path $UninstallKey)) { New-Item -Path $UninstallKey -Force | Out-Null }
    foreach ($name in $fields.Keys) {
        Set-ItemProperty -Path $UninstallKey -Name $name -Value $fields[$name]
    }
    Write-Step "installed apps entry: $AppDisplayName ($([math]::Round($sizeBytes / 1MB, 1)) MB)"
}

Write-Step "target directory: $Dir"
if ($Mirror) {
    $env:UV_DEFAULT_INDEX = $PyPiMirror
    $env:UV_PYTHON_INSTALL_MIRROR = $PythonMirror
    Write-Step "mirror mode: index=$PyPiMirror python=$PythonMirror"
}
# PowerShell variable names are case-insensitive: a local spelled $kit is the same
# variable as the -Kit parameter, and assigning it there silently un-installs the kit
# argument (the first probe ran the online lane with -Kit pointing at a real kit).
$resolvedKit = $null
$kitStage = ''
if ($Kit) {
    $kitStage = Join-Path ([System.IO.Path]::GetTempPath()) ('negi-kit-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $kitStage | Out-Null
    $resolvedKit = Resolve-OfflineKit -KitArg $Kit -RefValue $Ref -RefExplicit:($PSBoundParameters.ContainsKey('Ref')) -StageRoot $kitStage
    # Persist uv before Ensure-Uv, or a device without uv would be sent to astral.sh -
    # the one network call an offline install must not make.
    if (-not (Get-UvCommand)) { $null = Install-BundledUv -Path $resolvedKit.Uv }
}
$uv = Ensure-Uv
Update-Code -Root $Dir -Kit $resolvedKit
Install-Environment -uv $uv -Root $Dir -Kit $resolvedKit
Clear-LongPathTree -Path $kitStage

$icon = Copy-NegiIcon -Root $Dir
Install-EntryPoint -Root $Dir
if (-not $NoShortcut) { Install-DesktopIcons -Root $Dir -IconPath $icon }
$setupExeOwner = Get-SetupExeOwner -Root $Dir
if ($setupExeOwner) {
    # The setup program's row already describes this folder. Retire the row an older
    # install.ps1 wrote for it, but only when that row really points here - a device
    # that also keeps a script-installed copy elsewhere must not lose its entry.
    $legacyLocation = ''
    if (Test-Path -LiteralPath $UninstallKey) {
        $legacyLocation = Get-RegistryLocation -Path $UninstallKey
    }
    if ($legacyLocation -and ($legacyLocation.TrimEnd('\') -ieq $Dir.TrimEnd('\'))) {
        Remove-Item -LiteralPath $UninstallKey -Recurse -Force
        Write-Step "installed-apps row for this folder retired; $setupExeOwner owns uninstalling now"
    }
    else {
        Write-Step "installed apps entry: left to $setupExeOwner"
    }
}
elseif ($NoUninstallEntry) {
    # The setup program owns the Add/Remove Programs entry when it drives this
    # script; writing both would show two "Negi" rows that uninstall differently.
    Write-Step 'installed-apps entry skipped (-NoUninstallEntry; the setup program owns it)'
}
else {
    Write-UninstallRegistryEntry -Root $Dir -IconPath $icon
}
Write-Step 'from any terminal now on: negi'

if ($NoStart) {
    Write-Step "environment ready at $Dir (-NoStart: web not launched)"
    exit 0
}

Write-Step 'launching Negi web (first run asks for the project password in the browser)'
$python = Get-VenvPython -Root $Dir
$saved = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
Push-Location -LiteralPath $Dir
try {
    # No pipeline here: the server keeps the console attached so its live output
    # and clickable URL banner reach the terminal unmodified.
    & $python negi_bootstrap.py web
}
finally {
    $ErrorActionPreference = $saved
    Pop-Location
}
exit ([int]$LASTEXITCODE)
