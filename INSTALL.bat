@echo off
cd /d "%~dp0"
rem double-click inside a ZIP: Windows extracts only this file, without the rest of PULSE
if not exist "%~dp0scripts\install.ps1" (
  echo Unpack the archive first: right click - "Extract All", then run INSTALL.bat from the unpacked folder.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
pause
