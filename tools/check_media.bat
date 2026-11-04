@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
rem Launch the media-check subtool from the project root.
pushd "%~dp0.."
if errorlevel 1 (
    echo Status: cannot open tool directory. Exit code: 2.
    pause
    exit /b 2
)
set "PYTHONUTF8=1"
if "%~1"=="" goto prompt
if exist "%~dp0..\.venv\Scripts\python.exe" goto venv_args
py -3.12 -m tools.media_check %*
goto finish
:venv_args
"%~dp0..\.venv\Scripts\python.exe" -m tools.media_check %*
goto finish
:prompt
if exist "%~dp0..\.venv\Scripts\python.exe" goto venv_prompt
py -3.12 -m tools.media_check --prompt
goto finish
:venv_prompt
"%~dp0..\.venv\Scripts\python.exe" -m tools.media_check --prompt
:finish
set "exit_code=%ERRORLEVEL%"
echo.
if "%exit_code%"=="0" echo Status: checked, no damage found.
if "%exit_code%"=="1" echo Status: damaged media or check failure. See the report above.
if "%exit_code%"=="2" echo Status: invalid input or unsupported files. See details above.
if not "%exit_code%"=="0" if not "%exit_code%"=="1" if not "%exit_code%"=="2" echo Status: tool error, exit code %exit_code%.
echo Press any key to close this window.
pause >nul
popd
exit /b %exit_code%
