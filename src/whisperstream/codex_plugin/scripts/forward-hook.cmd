@echo off
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0forward-hook.ps1" 2>nul
if errorlevel 1 echo {}
exit /b 0
