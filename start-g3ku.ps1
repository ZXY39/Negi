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
$bootstrapScript = Join-Path $scriptDir "g3ku.ps1"
$rootPattern = [regex]::Escape($scriptDir)

function Show-Usage {
    @"
Usage: .\start-g3ku.ps1 [-BindHost HOST] [-Port PORT] [-OpenBrowser] [-PromptLog] [-Reload] [-KeepWorker] [-h|--help]

Quick start:
  .\start-g3ku.ps1

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

function Get-G3kuManagedPythonProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like "python*" -and
        $_.CommandLine -and
        $_.CommandLine -match $rootPattern -and
        (
            $_.CommandLine -match 'g3ku_bootstrap\.py"?\s+web' -or
            $_.CommandLine -match '-m\s+g3ku\s+web' -or
            $_.CommandLine -match '-m\s+g3ku\s+worker'
        )
    }
}

function Stop-G3kuManagedPythonProcesses {
    $processes = @(Get-G3kuManagedPythonProcesses)
    if (-not $processes) {
        return 0
    }
    Write-Host "[g3ku] Restarting existing g3ku web/worker processes..." -ForegroundColor Yellow
    foreach ($process in $processes) {
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        } catch {
            Write-Warning "[g3ku] Failed to stop PID $($process.ProcessId): $($_.Exception.Message)"
        }
    }
    Start-Sleep -Seconds 2
    return $processes.Count
}

function Assert-StartPreconditions {
    if (-not (Test-Path $bootstrapScript)) {
        throw "[g3ku] Missing launcher script: $bootstrapScript"
    }

    $existingWeb = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($existingWeb.Count -gt 0) {
        $pids = ($existingWeb | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique) -join ", "
        throw "[g3ku] Port $Port is already in use by PID(s): $pids. Stop the existing process before starting g3ku."
    }

    $existingManaged = @(Get-G3kuManagedPythonProcesses)
    if ($existingManaged.Count -gt 0) {
        $summary = $existingManaged |
            Select-Object ProcessId, CommandLine |
            ForEach-Object { "PID=$($_.ProcessId) $($_.CommandLine)" }
        throw "[g3ku] Existing g3ku web/worker processes are still running after restart attempt:`n$($summary -join "`n")"
    }
}

[void](Stop-G3kuManagedPythonProcesses)
Assert-StartPreconditions

$webArgs = @("web", "--host", $BindHost, "--port", "$Port")

if ($PromptLog) {
    $env:G3KU_PROMPT_TRACE = "1"
    Write-Host "[g3ku] Prompt logging enabled via G3KU_PROMPT_TRACE=1." -ForegroundColor Yellow
} else {
    Remove-Item Env:G3KU_PROMPT_TRACE -ErrorAction SilentlyContinue
}

if ($OpenBrowser) {
    Start-Job -ScriptBlock {
        param($TargetUrl)
        Start-Sleep -Seconds 3
        Start-Process $TargetUrl | Out-Null
    } -ArgumentList "http://${BindHost}:$Port" | Out-Null
}

if ($Reload) {
    $webArgs += "--reload"
}

Write-Host "[g3ku] Project root: $scriptDir"
if ($Reload) {
    Write-Host "[g3ku] Reload mode enabled; the web runtime will not auto-start a managed worker." -ForegroundColor Yellow
} else {
    Write-Host "[g3ku] Task worker will start after project unlock."
}
Write-Host "[g3ku] Starting web server on http://${BindHost}:$Port ..."

if ($KeepWorker) {
    $env:G3KU_WEB_KEEP_WORKER = "1"
    Write-Host "[g3ku] KeepWorker enabled; web-managed worker will be left running when the web server exits." -ForegroundColor Yellow
} else {
    Remove-Item Env:G3KU_WEB_KEEP_WORKER -ErrorAction SilentlyContinue
}

$webExitCode = 0
& $bootstrapScript @webArgs
$webExitCode = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 0 }

exit $webExitCode
