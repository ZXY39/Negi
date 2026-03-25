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
$bootstrapScript = Join-Path $scriptDir "g3ku.ps1"
$rootPattern = [regex]::Escape($scriptDir)

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
    if (-not (Test-Path $bootstrapScript)) {
        throw "[g3ku] Missing launcher script: $bootstrapScript"
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

if ($ForceRestart) {
    Write-Host "[g3ku] Force-restarting existing g3ku web/worker processes..." -ForegroundColor Yellow
    Stop-G3kuManagedPythonProcesses
}

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
Write-Host "[g3ku] Task worker will start after project unlock."
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
