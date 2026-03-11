$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bootstrap = Join-Path $scriptDir "g3ku_bootstrap.py"
$venvPython = Join-Path $scriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $bootstrap)) {
    Write-Error "[g3ku] Missing bootstrap script: $bootstrap"
}

if (Test-Path $venvPython) {
    $venvReady = $false
    try {
        & $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
        $venvReady = ($LASTEXITCODE -eq 0)
    } catch {
        $venvReady = $false
    }
}

if ($venvReady) {
    & $venvPython $bootstrap @args
    exit $LASTEXITCODE
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $bootstrap @args
    exit $LASTEXITCODE
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    & python $bootstrap @args
    exit $LASTEXITCODE
}

Write-Error "[g3ku] Python not found. Install Python or create a local .venv first."
