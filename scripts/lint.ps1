$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "[negi] lint: no virtualenv python found at $venvPython" -ForegroundColor Red
    Write-Host "[negi] lint: ensure the repository virtualenv exists under $projectRoot\.venv" -ForegroundColor Red
    exit 1
}

$exitCode = 0

# First-party scope: `.` would also scan the untracked scratch files that
# accumulate at the repo root, and vendored trees are excluded in pyproject.
$scope = @("g3ku", "main", "tools", "scripts", "bridges", "tests", "negi_bootstrap.py")

Write-Host "[negi] lint: ruff check (first-party)"
Push-Location $projectRoot
try {
    & $venvPython -m ruff check @scope
    $checkExit = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 0 }
    if ($checkExit -ne 0) {
        Write-Host "[negi] lint: FAILED - ruff check reported violations (exit $checkExit)" -ForegroundColor Red
        $exitCode = 1
    }
} finally {
    Pop-Location
}

if ($exitCode -ne 0) {
    Write-Host "[negi] lint: FAILED - one or more lint checks failed" -ForegroundColor Red
    exit 1
}

Write-Host "[negi] lint: all checks passed"
exit 0