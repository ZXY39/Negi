$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bootstrap = Join-Path $scriptDir "g3ku_bootstrap.py"

if (-not (Test-Path $bootstrap)) {
    Write-Error "[g3ku] Missing bootstrap script: $bootstrap"
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.14 $bootstrap @args
    exit $LASTEXITCODE
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    & python $bootstrap @args
    exit $LASTEXITCODE
}

Write-Error "[g3ku] Python not found. Install Python or create a local .venv first."
