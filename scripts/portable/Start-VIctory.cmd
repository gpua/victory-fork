@echo off
setlocal EnableExtensions DisableDelayedExpansion
"%~dp0bin\python\python.exe" -I -B "%~dp0bin\launch.py" %*
set "result=%errorlevel%"
if not "%result%"=="0" if "%~1"=="" pause
exit /b %result%
