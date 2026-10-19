#requires -Version 5.1
<#
  What Negi-Setup.exe runs after it unpacks itself.

  The exe exists for one reason: a person who does not want to read a terminal
  should be able to double-click, pick a folder, and get Negi installed. Every
  real decision still belongs to install.ps1 - this script only asks for the
  folder and passes it through, so the one-line `iwr | iex` channel and the
  exe cannot drift apart.
#>
param(
    [string]$Dir,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$installer = Join-Path $here 'install.ps1'
if (-not (Test-Path -LiteralPath $installer)) {
    Write-Error "[setup] install.ps1 is missing next to this script"
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

Write-Host "[setup] 安装到 $Dir" -ForegroundColor Cyan
& $installer -Dir $Dir
exit $LASTEXITCODE
