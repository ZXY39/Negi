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
$rootPattern = [regex]::Escape($scriptDir)

function Show-Usage {
    @"
Usage: .\start-negi.ps1 [-BindHost HOST] [-Port PORT] [-OpenBrowser] [-PromptLog] [-Reload] [-KeepWorker] [-h|--help]

Quick start:
  .\start-negi.ps1

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

function Get-NegiManagedPythonProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like "python*" -and
        $_.CommandLine -and
        $_.CommandLine -match $rootPattern -and
        (
            $_.CommandLine -match 'negi_bootstrap\.py"?\s+web' -or
            $_.CommandLine -match '-m\s+g3ku\s+web' -or
            $_.CommandLine -match '-m\s+g3ku\s+worker'
        )
    }
}

function Request-NegiGracefulExit {
    param([int]$Port = 18790)
    try {
        $body = '{"pause_running_work":true}'
        $null = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$Port/api/bootstrap/exit" `
            -Method Post `
            -ContentType "application/json" `
            -Body $body `
            -TimeoutSec 20 `
            -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Stop-NegiManagedPythonProcesses {
    param([int]$Port = 18790)
    $processes = @(Get-NegiManagedPythonProcesses)
    if (-not $processes) {
        return 0
    }
    Write-Host "[negi] Restarting existing Negi web/worker processes..." -ForegroundColor Yellow
    if (Request-NegiGracefulExit -Port $Port) {
        Write-Host "[negi] Graceful exit requested; waiting for the runtime to pause all work and stop..." -ForegroundColor Yellow
        $deadline = (Get-Date).AddSeconds(40)
        while ((Get-Date) -lt $deadline) {
            $remaining = @(Get-NegiManagedPythonProcesses)
            if (-not $remaining) {
                return $processes.Count
            }
            Start-Sleep -Milliseconds 500
        }
    }
    Write-Host "[negi] Force-stopping remaining Negi processes..." -ForegroundColor Yellow
    foreach ($process in $processes) {
        # $processes 是发 graceful exit 之前拍的快照；等轮询结束再回来时，
        # 其中不少 PID 已经自己退干净了。按实况跳过，别把"已经不在了"报成停止失败。
        if (-not (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue)) {
            continue
        }
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        } catch {
            if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) {
                Write-Warning "[negi] Failed to stop PID $($process.ProcessId): $($_.Exception.Message)"
            }
        }
    }
    Start-Sleep -Seconds 2
    return $processes.Count
}

function Assert-StartPreconditions {
    if (-not (Test-Path $bootstrapScript)) {
        throw "[negi] Missing launcher script: $bootstrapScript"
    }

    $existingWeb = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($existingWeb.Count -gt 0) {
        $pids = ($existingWeb | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique) -join ", "
        throw "[negi] Port $Port is already in use by PID(s): $pids. Stop the existing process before starting Negi."
    }

    $existingManaged = @(Get-NegiManagedPythonProcesses)
    if ($existingManaged.Count -gt 0) {
        $summary = $existingManaged |
            Select-Object ProcessId, CommandLine |
            ForEach-Object { "PID=$($_.ProcessId) $($_.CommandLine)" }
        throw "[negi] Existing Negi web/worker processes are still running after restart attempt:`n$($summary -join "`n")"
    }
}

[void](Stop-NegiManagedPythonProcesses -Port $Port)
Assert-StartPreconditions

$webArgs = @("web", "--host", $BindHost, "--port", "$Port")

if ($PromptLog) {
    $env:G3KU_PROMPT_TRACE = "1"
    Write-Host "[negi] Prompt logging enabled via G3KU_PROMPT_TRACE=1." -ForegroundColor Yellow
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

Write-Host "[negi] Project root: $scriptDir"
if ($Reload) {
    Write-Host "[negi] Reload mode enabled; the web runtime will not auto-start a managed worker." -ForegroundColor Yellow
} else {
    Write-Host "[negi] Task worker will start after project unlock."
}
Write-Host "[negi] Starting web server on http://${BindHost}:$Port ..."

if ($KeepWorker) {
    $env:G3KU_WEB_KEEP_WORKER = "1"
    Write-Host "[negi] KeepWorker enabled; web-managed worker will be left running when the web server exits." -ForegroundColor Yellow
} else {
    Remove-Item Env:G3KU_WEB_KEEP_WORKER -ErrorAction SilentlyContinue
}

$webExitCode = 0
& $bootstrapScript @webArgs
$webExitCode = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 0 }

exit $webExitCode
