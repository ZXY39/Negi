@echo off
rem Negi 卡死设备的一次性更新恢复入口。主体在 recovery.py（Python stdlib，锁定策略免疫）。
rem 这里只找一个能跑它的解释器：py 启动器 -> python -> 默认安装根的 venv python。
rem 用法：双击，或 recovery.cmd [--root <安装根>] [--ref <vX.Y.Z>] [--port <端口>]
setlocal
set "HERE=%~dp0"

where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
set "VENV=%USERPROFILE%\Negi\.venv\Scripts\python.exe"
if exist "%VENV%" goto use_venv

echo [recovery] 找不到 Python 3.12+。装一个 Python，或用安装根 .venv\Scripts\python.exe 跑 recovery.py。
exit /b 1

:use_py
py -3 "%HERE%recovery.py" %*
exit /b %ERRORLEVEL%

:use_python
python "%HERE%recovery.py" %*
exit /b %ERRORLEVEL%

:use_venv
"%VENV%" "%HERE%recovery.py" %*
exit /b %ERRORLEVEL%