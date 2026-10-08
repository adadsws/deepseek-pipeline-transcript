@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONPATH="

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\headless_batch.py" %*
set "exit_code=%ERRORLEVEL%"

echo.
if not "%exit_code%"=="0" echo 批处理结束，退出码：%exit_code%
popd
exit /b %exit_code%
