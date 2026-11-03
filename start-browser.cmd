@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_scripts\start_browser.ps1" %*
if errorlevel 1 pause
