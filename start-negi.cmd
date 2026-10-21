@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "BOOTSTRAP=%SCRIPT_DIR%negi_bootstrap.py"
set "PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=py -3"

if not exist "%BOOTSTRAP%" (
  echo [negi] Missing bootstrap script: %BOOTSTRAP%
  exit /b 1
)

rem Deliberately no PowerShell here: an unsigned .ps1 can be refused outright by the
rem machine's script policy, and this entry must still boot. Defaults come first so an
rem operator-supplied -Port / -BindHost in %* wins, and the switch names are translated
rem by negi_bootstrap.py (one table, shared with start-negi.ps1).
"%PY%" "%BOOTSTRAP%" web -BindHost 127.0.0.1 -Port 18790 %*
exit /b %ERRORLEVEL%
