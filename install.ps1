#requires -Version 5.1
<#
  G3KU one-line installer (Windows).

    iwr https://github.com/ZXY39/Negi/releases/latest/download/install.ps1 | iex

  Provisions uv (and therefore Python) on a machine that has neither, fetches the
  pinned checkout, syncs the locked environment and hands off to negi_bootstrap.py.
  Runtime code is not modified by this script.
#>
param(
    [string]$Dir = (Join-Path $env:USERPROFILE 'Negi'),
    [string]$Ref = 'v2.6.0',
    [switch]$NoStart,
    [switch]$Upgrade,
    [switch]$NoPath,
    [switch]$NoShortcut,
    [switch]$NoUninstallEntry
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoOwner = 'ZXY39'
$RepoName = 'Negi'
$RepoGit = "https://github.com/$RepoOwner/$RepoName.git"
$RepoZip = "https://github.com/$RepoOwner/$RepoName/archive/${Ref}.zip"
$UvInstaller = 'https://astral.sh/uv/install.ps1'
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

function Copy-CheckoutInto {
    param([string]$Source, [string]$Root, [switch]$KeepProtected)
    # Top-level merge: environment and operator data are never touched on an
    # upgrade. Files the new release deleted stay behind until a reinstall.
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        if ($KeepProtected -and $ProtectedEntries -contains $_.Name) { return }
        if ($_.Name -eq '.git' -and (Test-Path (Join-Path $Root '.git'))) { return }
        Copy-Item -LiteralPath $_.FullName -Destination $Root -Recurse -Force
    }
}

function Get-CheckoutStage {
    param([string]$StageRoot, [switch]$Archive)
    if (-not $Archive -and (Get-Command git -ErrorAction SilentlyContinue)) {
        $target = Join-Path $StageRoot 'checkout'
        Write-Step "git clone --depth 1 --branch $Ref (staged)"
        Invoke-Checked @('git', 'clone', '--depth', '1', '--branch', $Ref, $RepoGit, $target) -Where $target
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
    param([string]$Root, [switch]$KeepProtected, [switch]$Archive)
    # Code always lands in a staging directory first and is merged top-level:
    # `git clone` refuses a non-empty target, and a keep-data uninstall leaves
    # exactly that (a directory holding only .g3ku).
    $stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('negi-stage-' + [Guid]::NewGuid().ToString('N'))
    try {
        New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null
        $checkout = Get-CheckoutStage -StageRoot $stageRoot -Archive:$Archive
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
    param([string]$Root)
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

function Install-Environment {
    param([string]$uv, [string]$Root)
    $pin = Get-ProjectPin -Root $Root
    if ($pin) {
        Write-Step "uv python install $pin"
        Invoke-Checked @($uv, 'python', 'install', $pin) -Where $Root
    }
    Push-Location -LiteralPath $Root
    try {
        Write-Step 'uv sync --frozen'
        Invoke-Checked @($uv, 'sync', '--frozen') -Where $Root
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
        $location = (Get-ItemProperty -LiteralPath $entry.PSPath -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
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
$uv = Ensure-Uv
Update-Code -Root $Dir
Install-Environment -uv $uv -Root $Dir

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
        $legacyLocation = (Get-ItemProperty -LiteralPath $UninstallKey -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
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
