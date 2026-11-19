param(
    [Alias("h")]
    [switch]$Help,
    [string]$BindHost = "",
    [int]$Port = 0,
    [switch]$OpenBrowser,
    [switch]$PromptLog,
    [switch]$Reload,
    [switch]$KeepWorker,
    [switch]$NoSupervise,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bootstrapScript = Join-Path $scriptDir "negi.ps1"

function Show-Usage {
    @"
Usage: .\start-negi.ps1 [-BindHost HOST] [-Port PORT] [-OpenBrowser] [-PromptLog] [-Reload] [-KeepWorker] [-NoSupervise] [-h|--help]

Quick start:
  .\start-negi.ps1          (or start-negi.cmd, which needs no PowerShell script permission)

Common options:
  -BindHost      Web bind host. Default: from project config (web.host)
  -Port          Web bind port. Default: from project config (web.port)
  -OpenBrowser   Open the browser after startup
  -PromptLog     Enable G3KU_PROMPT_TRACE=1
  -Reload        Enable web reload mode (managed worker auto-start is disabled)
  -KeepWorker    Keep the managed worker running after web exit
  -NoSupervise   Disable crash respawn (supervision is on by default)
  -h, --help     Show this help text and exit
"@ | Write-Output
}

if ($Help -or ($ExtraArgs -contains "--help")) {
    Show-Usage
    exit 0
}

# This script only translates switches into CLI flags. Reaping the old instance, waiting for
# the port to go quiet and reporting a failed start all live in `negi_bootstrap.py`'s web
# branch (`reap_existing_runtime_processes`). Keeping that here would be wrong: a .ps1 can be
# refused before it runs (measured: an unsigned script with a low-integrity label is rejected
# even under RemoteSigned), and the recovery would disappear with it - while `negi.cmd` /
# `negi web` never pass here.
# Supervision is on by default (aligns with the icon / `negi` start lane); -NoSupervise
# opts out. Bind host/port are only forwarded when explicitly given, otherwise the
# bootstrap reads web.host / web.port from project config.
$webArgs = @("web")
if (-not $NoSupervise) {
    $webArgs += "--supervise"
}
if ($BindHost) {
    $webArgs += @("--host", $BindHost)
}
if ($Port) {
    $webArgs += @("--port", "$Port")
}

if ($Reload) {
    $webArgs += "--reload"
}

if ($PromptLog) {
    $webArgs += "--prompt-log"
    Write-Host "[negi] Prompt logging enabled via G3KU_PROMPT_TRACE=1." -ForegroundColor Yellow
}

if ($KeepWorker) {
    $webArgs += "--keep-worker"
    Write-Host "[negi] KeepWorker enabled; web-managed worker will be left running when the web server exits." -ForegroundColor Yellow
}

if ($OpenBrowser) {
    # Opening the browser also happens runtime-side: only the runtime knows the server is
    # actually ready, while this script could only guess with a fixed delay.
    $webArgs += "--open-browser"
}

# Clear switches left over from a previous shell; otherwise "this run passed no -PromptLog"
# still records prompts. The child inherits this environment, and the Python side sets them
# back only when the CLI flags ask for it.
Remove-Item Env:G3KU_PROMPT_TRACE -ErrorAction SilentlyContinue
Remove-Item Env:G3KU_WEB_KEEP_WORKER -ErrorAction SilentlyContinue

Write-Host "[negi] Project root: $scriptDir"
if ($Reload) {
    Write-Host "[negi] Reload mode enabled; the web runtime will not auto-start a managed worker." -ForegroundColor Yellow
} else {
    Write-Host "[negi] Task worker will start after project unlock."
}
if ($BindHost -and $Port) {
    Write-Host "[negi] Starting web server on http://${BindHost}:$Port ..."
} else {
    Write-Host "[negi] Starting web server (bind host/port from project config) ..."
}

& $bootstrapScript @webArgs
$webExitCode = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 0 }

exit $webExitCode
