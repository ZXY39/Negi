param(
    [Alias("h")]
    [switch]$Help,
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 18790,
    [switch]$OpenBrowser,
    [switch]$PromptLog,
    [switch]$Reload,
    [switch]$KeepWorker,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bootstrapScript = Join-Path $scriptDir "negi.ps1"

function Show-Usage {
    @"
Usage: .\start-negi.ps1 [-BindHost HOST] [-Port PORT] [-OpenBrowser] [-PromptLog] [-Reload] [-KeepWorker] [-h|--help]

Quick start:
  .\start-negi.ps1          (or start-negi.cmd, which needs no PowerShell script permission)

Common options:
  -BindHost      Web bind host. Default: 127.0.0.1
  -Port          Web bind port. Default: 18790
  -OpenBrowser   Open the browser after startup
  -PromptLog     Enable G3KU_PROMPT_TRACE=1
  -Reload        Enable web reload mode (managed worker auto-start is disabled)
  -KeepWorker    Keep the managed worker running after web exit
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
$webArgs = @("web", "--host", $BindHost, "--port", "$Port")

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
Write-Host "[negi] Starting web server on http://${BindHost}:$Port ..."

& $bootstrapScript @webArgs
$webExitCode = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 0 }

exit $webExitCode
