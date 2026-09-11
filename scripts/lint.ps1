$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "[g3ku] lint: no virtualenv python found at $venvPython" -ForegroundColor Red
    Write-Host "[g3ku] lint: ensure the repository virtualenv exists under $projectRoot\.venv" -ForegroundColor Red
    exit 1
}

$exitCode = 0

Write-Host "[g3ku] lint: ruff check ."
Push-Location $projectRoot
try {
    & $venvPython -m ruff check .
    $checkExit = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 0 }
    if ($checkExit -ne 0) {
        Write-Host "[g3ku] lint: FAILED - ruff check reported violations (exit $checkExit)" -ForegroundColor Red
        $exitCode = 1
    }

    Write-Host "[g3ku] lint: ruff format --check ."
    & $venvPython -m ruff format --check .
    $formatExit = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 0 }
    if ($formatExit -ne 0) {
        Write-Host "[g3ku] lint: FAILED - ruff format --check found unformatted files (exit $formatExit)" -ForegroundColor Red
        $exitCode = 1
    }
} finally {
    Pop-Location
}

if ($exitCode -ne 0) {
    Write-Host "[g3ku] lint: FAILED - one or more lint checks failed" -ForegroundColor Red
    exit 1
}

Write-Host "[g3ku] lint: all checks passed"
exit 0