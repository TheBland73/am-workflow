@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stopper.ps1"
timeout /t 2 >nul
endlocal
