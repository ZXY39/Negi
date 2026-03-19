param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 18790,
    [switch]$OpenBrowser,
    [switch]$PromptLog,
    [switch]$Reload,
    [switch]$KeepWorker,
    [switch]$ForceRestart
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $scriptDir ".venv\Scripts\python.exe"
$logsDir = Join-Path $scriptDir ".g3ku\logs"
$workerOutLog = Join-Path $logsDir "worker.out.log"
$workerErrLog = Join-Path $logsDir "worker.err.log"
$rootPattern = [regex]::Escape($scriptDir)

function Get-G3kuManagedPythonProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like "python*" -and
        $_.CommandLine -and
        $_.CommandLine -match $rootPattern -and
        (
            $_.CommandLine -match 'g3ku_bootstrap\.py"?\s+start' -or
            $_.CommandLine -match '-m\s+g3ku(?:\.g3ku_cli)?\s+start' -or
            $_.CommandLine -match '-m\s+g3ku(?:\.g3ku_cli)?\s+worker'
        )
    }
}

function Stop-G3kuManagedPythonProcesses {
    $processes = @(Get-G3kuManagedPythonProcesses)
    if (-not $processes) {
        return
    }
    foreach ($process in $processes) {
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        } catch {
            Write-Warning "[g3ku] Failed to stop PID $($process.ProcessId): $($_.Exception.Message)"
        }
    }
    Start-Sleep -Seconds 2
}

function Assert-StartPreconditions {
    if (-not (Test-Path $venvPython)) {
        throw "[g3ku] Missing project virtualenv Python: $venvPython"
    }

    $existingWeb = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($existingWeb.Count -gt 0) {
        $pids = ($existingWeb | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique) -join ", "
        throw "[g3ku] Port $Port is already in use by PID(s): $pids. Stop the existing web server or rerun with -ForceRestart."
    }

    $existingManaged = @(Get-G3kuManagedPythonProcesses)
    if ($existingManaged.Count -gt 0) {
        $summary = $existingManaged |
            Select-Object ProcessId, CommandLine |
            ForEach-Object { "PID=$($_.ProcessId) $($_.CommandLine)" }
        throw "[g3ku] Existing g3ku web/worker processes detected:`n$($summary -join "`n")`nStop them first or rerun with -ForceRestart."
    }
}

function Show-WorkerFailureLogs {
    if (Test-Path $workerOutLog) {
        Write-Host ""
        Write-Host "[g3ku] Worker stdout:" -ForegroundColor Yellow
        Get-Content $workerOutLog -Tail 80
    }
    if (Test-Path $workerErrLog) {
        Write-Host ""
        Write-Host "[g3ku] Worker stderr:" -ForegroundColor Yellow
        Get-Content $workerErrLog -Tail 80
    }
}

if ($ForceRestart) {
    Write-Host "[g3ku] Force-restarting existing g3ku web/worker processes..." -ForegroundColor Yellow
    Stop-G3kuManagedPythonProcesses
}

Assert-StartPreconditions

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

if (Test-Path $workerOutLog) {
    Remove-Item $workerOutLog -Force
}
if (Test-Path $workerErrLog) {
    Remove-Item $workerErrLog -Force
}

$workerArgs = @("-m", "g3ku.g3ku_cli", "worker")
$webArgs = @("-m", "g3ku.g3ku_cli", "start", "--no-worker", "--host", $BindHost, "--port", "$Port")

if ($PromptLog) {
    $webArgs += "--log"
}
if ($OpenBrowser) {
    $webArgs += "--open"
}
if ($Reload) {
    $webArgs += "--reload"
}

Write-Host "[g3ku] Project root: $scriptDir"
Write-Host "[g3ku] Starting worker..."
Write-Host "[g3ku] Worker logs: $workerOutLog"

$workerProcess = Start-Process `
    -FilePath $venvPython `
    -ArgumentList $workerArgs `
    -WorkingDirectory $scriptDir `
    -RedirectStandardOutput $workerOutLog `
    -RedirectStandardError $workerErrLog `
    -PassThru

Start-Sleep -Seconds 3
$workerProcess.Refresh()
if ($workerProcess.HasExited) {
    Show-WorkerFailureLogs
    throw "[g3ku] Worker exited immediately with code $($workerProcess.ExitCode)."
}

Write-Host "[g3ku] Worker PID: $($workerProcess.Id)"
Write-Host "[g3ku] Starting web server on http://${BindHost}:$Port ..."

$webExitCode = 0
try {
    & $venvPython @webArgs
    $webExitCode = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 0 }
} finally {
    if (-not $KeepWorker) {
        try {
            $workerProcess.Refresh()
            if (-not $workerProcess.HasExited) {
                Write-Host "[g3ku] Stopping worker PID $($workerProcess.Id)..." -ForegroundColor Yellow
                Stop-Process -Id $workerProcess.Id -Force -ErrorAction Stop
            }
        } catch {
            Write-Warning "[g3ku] Failed to stop worker PID $($workerProcess.Id): $($_.Exception.Message)"
        }
    } else {
        Write-Host "[g3ku] KeepWorker enabled; worker PID $($workerProcess.Id) left running." -ForegroundColor Yellow
    }
}

exit $webExitCode
