@echo off
rem Windows Setup runs this as SYSTEM once after OOBE; setup.ps1 powers off.
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
