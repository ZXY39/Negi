#requires -Version 5.1
<#
  G3KU one-line installer (Windows).

    iwr https://raw.githubusercontent.com/ZXY39/G3KU-Agent/v1.0.5/install.ps1 | iex

  Provisions uv (and therefore Python) on a machine that has neither, fetches the
  pinned checkout, syncs the locked environment and hands off to g3ku_bootstrap.py.
  Runtime code is not modified by this script.
#>
param(
    [string]$Dir = (Join-Path $env:USERPROFILE 'G3KU-Agent'),
    [string]$Ref = 'v1.0.5',
    [switch]$NoStart,
    [switch]$Upgrade
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoOwner = 'ZXY39'
$RepoName = 'G3KU-Agent'
$RepoGit = "https://github.com/$RepoOwner/$RepoName.git"
$RepoZip = "https://github.com/$RepoOwner/$RepoName/archive/${Ref}.zip"
$UvInstaller = 'https://astral.sh/uv/install.ps1'
# Kept across upgrades: the environment and everything the operator created.
$ProtectedEntries = @('.venv', '.g3ku', '.git')

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

function Install-FromArchive {
    param([string]$Root, [switch]$Overwrite)
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("g3ku-" + [Guid]::NewGuid().ToString('N'))
    $zip = Join-Path $tmp 'source.zip'
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        Invoke-WebRequest -Uri $RepoZip -OutFile $zip -UseBasicParsing
        Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
        $inner = Get-ChildItem -LiteralPath $tmp -Directory | Select-Object -First 1
        if (-not $inner) {
            Write-Error "[install] archive for $Ref contained no top-level directory"
        }
        if ($Overwrite) {
            # Top-level merge: environment and operator data are never touched.
            # Files the new release deleted stay behind until a reinstall.
            Get-ChildItem -LiteralPath $inner.FullName -Force | ForEach-Object {
                if ($ProtectedEntries -notcontains $_.Name) {
                    Copy-Item -LiteralPath $_.FullName -Destination $Root -Recurse -Force
                }
            }
        }
        else {
            Get-ChildItem -LiteralPath $inner.FullName -Force | Move-Item -Destination $Root
        }
    }
    finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Update-GitCheckout {
    param([string]$Root)
    $dirty = (& git -C $Root status --porcelain 2>$null)
    if ($dirty) {
        Write-Error "[install] $Root has uncommitted changes; commit or discard them before upgrading"
    }
    Write-Step "git fetch --depth 1 origin $Ref"
    Invoke-Checked @('git', '-C', $Root, 'fetch', '--depth', '1', 'origin', $Ref) -Where $Root
    Invoke-Checked @('git', '-C', $Root, 'checkout', '--detach', 'FETCH_HEAD') -Where $Root
    Write-Step "code updated to $Ref"
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
        Install-FromArchive -Root $Root -Overwrite
        return
    }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Step "git clone --branch $Ref into $Root"
        Invoke-Checked @('git', 'clone', '--depth', '1', '--branch', $Ref, $RepoGit, $Root) -Where $Root
        return
    }
    Write-Step "git not available, downloading $RepoZip"
    Install-FromArchive -Root $Root
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

Write-Step "target directory: $Dir"
$uv = Ensure-Uv
Update-Code -Root $Dir
Install-Environment -uv $uv -Root $Dir

if ($NoStart) {
    Write-Step "environment ready at $Dir (-NoStart: web not launched)"
    exit 0
}

Write-Step 'launching G3KU web (first run asks for the project password in the browser)'
$python = Get-VenvPython -Root $Dir
$saved = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
Push-Location -LiteralPath $Dir
try {
    # No pipeline here: the server keeps the console attached so its live output
    # and clickable URL banner reach the terminal unmodified.
    & $python g3ku_bootstrap.py web
}
finally {
    $ErrorActionPreference = $saved
    Pop-Location
}
exit ([int]$LASTEXITCODE)
