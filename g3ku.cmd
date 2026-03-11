@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "BOOTSTRAP=%SCRIPT_DIR%g3ku_bootstrap.py"
set "VENV_PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"

if not exist "%BOOTSTRAP%" (
  echo [g3ku] Missing bootstrap script: %BOOTSTRAP%
  exit /b 1
)

if exist "%VENV_PYTHON%" (
  "%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 goto run_venv_python
)

where py >nul 2>nul
if %ERRORLEVEL%==0 goto run_py_launcher

where python >nul 2>nul
if %ERRORLEVEL%==0 goto run_python_path

echo [g3ku] Python not found. Install Python or create a local .venv first.
exit /b 1

:run_venv_python
"%VENV_PYTHON%" "%BOOTSTRAP%" %*
exit /b %ERRORLEVEL%

:run_py_launcher
py -3 "%BOOTSTRAP%" %*
exit /b %ERRORLEVEL%

:run_python_path
python "%BOOTSTRAP%" %*
exit /b %ERRORLEVEL%
