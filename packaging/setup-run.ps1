#requires -Version 5.1
<#
  What Negi-Setup.exe runs after it unpacks itself.

  The exe exists for one reason: a person who does not want to read a terminal
  should be able to double-click, pick a folder, and get Negi installed. Every
  real decision still belongs to install.ps1 - this script resolves which folder
  this device's Negi lives in and hands it over, so the one-line `iwr | iex`
  channel and the exe cannot drift apart.

  On a device that already has Negi, this is also the adoption path: the wizard's
  folder page is a default, not a vote, and running the exe upgrades the install
  the pointer records instead of putting a second copy beside it.

  The wizard unpacks one offline kit next to this script and hands it over as -Kit.
  install.ps1 owns every check on it (platform, version, each wheel's sha256), so
  this file forwards the path and nothing else.
#>
param(
    [string]$Dir,
    [switch]$Quiet,
    [switch]$NoUninstallEntry,
    [string]$Kit
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$installer = Join-Path $here 'install.ps1'
if (-not (Test-Path -LiteralPath $installer)) {
    Write-Error "[setup] install.ps1 is missing next to this script"
}

function Get-InstalledRoot {
    # The same pointer file the `negi` stub reads. A root is only a fact when the
    # code is still under it: a kept-data uninstall leaves the pointer with the
    # code gone, and then the next install is a fresh one by design.
    param([string]$Candidate)
    if (-not $Candidate) { return '' }
    if (-not (Test-Path -LiteralPath (Join-Path $Candidate 'pyproject.toml'))) { return '' }
    return $Candidate
}

function Read-PointerRoot {
    $pointer = Join-Path $env:LOCALAPPDATA 'Negi\install-root.json'
    if (-not (Test-Path -LiteralPath $pointer)) { return '' }
    try {
        $record = Get-Content -LiteralPath $pointer -Raw | ConvertFrom-Json
    }
    catch {
        return ''
    }
    return Get-InstalledRoot -Candidate ([string]$record.root)
}

$default = Join-Path $env:USERPROFILE 'Negi'
if (-not $Dir) {
    if ($Quiet) {
        $Dir = $default
    }
    else {
        Write-Host "Negi 安装目录（回车使用默认）：" -ForegroundColor Cyan
        Write-Host "  默认 $default"
        $answer = Read-Host '目录'
        $Dir = if ($answer) { $answer.Trim().Trim('"') } else { $default }
    }
}

# A relative answer is resolved against the folder the user was standing in when
# they double-clicked, not against the temp directory this script unpacked into.
if (-not [System.IO.Path]::IsPathRooted($Dir)) {
    $Dir = Join-Path (Get-Location).Path $Dir
}

$chosen = Get-InstalledRoot -Candidate $Dir
$recorded = Read-PointerRoot
if (-not $chosen -and $recorded) {
    # The folder page pre-fills %USERPROFILE%\Negi, so a device installed elsewhere
    # presses Next without looking. Installing beside the running one would give the
    # machine two roots, one port and one pointer - the worse of the two mistakes.
    Write-Host "[setup] 检测到已安装的 Negi：$recorded" -ForegroundColor Cyan
    Write-Host "[setup] 这次升级它（未使用所选目录 $Dir）"
    $Dir = $recorded
}

$upgrade = [bool](Get-InstalledRoot -Candidate $Dir)
if ($upgrade) {
    Write-Host '[setup] 已存在安装：按升级处理（换代码 + 补依赖 + 重写入口面）' -ForegroundColor Cyan
}

$parameters = @{ Dir = $Dir; NoStart = $true }
# -NoStart is not optional here: without it install.ps1 ends by running the web server
# in the foreground, and the wizard's [Run] entry waits for that process to exit - so
# the install never reports success and the postinstall "launch" entry never runs.
# Launching belongs to the wizard's own [Run] entry (`negi.cmd start --window`).
if ($upgrade) {
    # Without -Upgrade, install.ps1 leaves an existing checkout alone but still
    # rewrites the shortcuts and the stub. That combination is the one state where
    # the icon can be broken: the new shortcut args (`start --window`) would point
    # at code old enough not to know the flag, and it passes the argument through
    # to `g3ku web`, which refuses it.
    $parameters['Upgrade'] = $true
}
if ($NoUninstallEntry) {
    # Negi-Setup.exe owns the Add/Remove Programs row, so install.ps1 must not add
    # its own: two "Negi" entries that uninstall differently is worse than none.
    $parameters['NoUninstallEntry'] = $true
}
if ($Kit) { $parameters['Kit'] = $Kit }

Write-Host "[setup] 安装到 $Dir" -ForegroundColor Cyan
$logDir = Join-Path $env:LOCALAPPDATA 'Negi\logs'
$log = Join-Path $logDir 'setup.log'
try {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    Add-Content -LiteralPath $log -Value ("[{0}] setup start: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Dir) -Encoding UTF8
}
catch {
    Write-Host "[setup] 无法写入日志：$($_.Exception.Message)" -ForegroundColor Yellow
}

# The wizard runs this script hidden. Without a log, a failed install leaves
# nothing to read; this file is the only diagnosis a user can send back. Lines
# are appended one by one so setup.log stays a single UTF-8 text file instead of
# mixing Tee-Object's default encoding with the header above.
$code = 1
try {
    & $installer @parameters 2>&1 | ForEach-Object {
        $text = ($_ | Out-String).TrimEnd()
        if ($text) {
            Write-Host $text
            try {
                Add-Content -LiteralPath $log -Value $text -Encoding UTF8
            }
            catch { }
        }
    }
    $code = $LASTEXITCODE
}
catch {
    $message = $_.Exception.Message
    try {
        Add-Content -LiteralPath $log -Value "[setup] failed: $message" -Encoding UTF8
    }
    catch { }
    Write-Host "[setup] 安装失败：$message" -ForegroundColor Red
    $code = 1
}
if ($code -ne 0) {
    Write-Host "[setup] 完整日志：$log" -ForegroundColor Yellow
}
exit $code
