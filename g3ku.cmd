@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "BOOTSTRAP=%SCRIPT_DIR%g3ku_bootstrap.py"

if not exist "%BOOTSTRAP%" (
  echo [g3ku] Missing bootstrap script: %BOOTSTRAP%
  exit /b 1
)

where py >nul 2>nul
if %ERRORLEVEL%==0 goto run_py_launcher

where python >nul 2>nul
if %ERRORLEVEL%==0 goto run_python_path

echo [g3ku] Python not found. Install Python or create a local .venv first.
exit /b 1

:run_py_launcher
py -3.14 "%BOOTSTRAP%" %*
exit /b %ERRORLEVEL%

:run_python_path
python "%BOOTSTRAP%" %*
exit /b %ERRORLEVEL%
