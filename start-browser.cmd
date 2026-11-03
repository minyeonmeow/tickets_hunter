@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_scripts\start_browser.ps1" -PromptPort %*
if errorlevel 1 pause
