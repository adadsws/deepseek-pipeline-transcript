@echo off
setlocal
pushd "%~dp0"
set "PYTHONUTF8=1"
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\launch_gui.py" %*
set "exit_code=%ERRORLEVEL%"
popd
exit /b %exit_code%
