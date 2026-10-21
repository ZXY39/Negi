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

rem Deliberately no PowerShell here: a .ps1 can be refused before it runs (measured: an
rem unsigned script with a low-integrity label is rejected even under RemoteSigned), and
rem this entry must still boot. Defaults come first so an operator-supplied -Port /
rem -BindHost in %* wins; negi_bootstrap.py translates the switch names (one table).
"%PY%" "%BOOTSTRAP%" web -BindHost 127.0.0.1 -Port 18790 %*
exit /b %ERRORLEVEL%
