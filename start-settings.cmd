@echo off
setlocal
chcp 65001 >nul
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONUTF8=1"
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_scripts\start_settings.ps1"
set "START_EXIT=%ERRORLEVEL%"
if not "%START_EXIT%"=="0" pause
exit /b %START_EXIT%
