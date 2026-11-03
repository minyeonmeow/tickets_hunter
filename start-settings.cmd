@echo off
setlocal
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONUTF8=1"
cd /d "%~dp0src"
"%~dp0.venv\Scripts\python.exe" settings.py
if errorlevel 1 pause
endlocal
