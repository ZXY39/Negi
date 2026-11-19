@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "BOOTSTRAP=%SCRIPT_DIR%negi_bootstrap.py"
set "VENV_PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"

if not exist "%BOOTSTRAP%" (
  echo [negi] Missing bootstrap script: %BOOTSTRAP%
  exit /b 1
)

rem Same probe as negi.cmd: existence is not enough - a venv whose base interpreter
rem moved still has python.exe on disk but dies before running any code.
if exist "%VENV_PYTHON%" (
  "%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 goto run_venv_python
)

where py >nul 2>nul
if %ERRORLEVEL%==0 goto run_py_launcher

where python >nul 2>nul
if %ERRORLEVEL%==0 goto run_python_path

echo [negi] Python not found. Install Python or create a local .venv first.
exit /b 1

:run_venv_python
"%VENV_PYTHON%" "%BOOTSTRAP%" web -BindHost 127.0.0.1 -Port 18790 %*
exit /b %ERRORLEVEL%

:run_py_launcher
py -3 "%BOOTSTRAP%" web -BindHost 127.0.0.1 -Port 18790 %*
exit /b %ERRORLEVEL%

:run_python_path
python "%BOOTSTRAP%" web -BindHost 127.0.0.1 -Port 18790 %*
exit /b %ERRORLEVEL%