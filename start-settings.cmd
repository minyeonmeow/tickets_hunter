@echo off
setlocal
chcp 65001 >nul
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONUTF8=1"
rem Chrome connection port: change this number to match the browser.
set "TICKETS_CHROME_PORT=19327"
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_scripts\start_settings.ps1" -Port "%TICKETS_CHROME_PORT%"
set "START_EXIT=%ERRORLEVEL%"
if not "%START_EXIT%"=="0" pause
exit /b %START_EXIT%
