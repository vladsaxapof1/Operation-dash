@echo off
cd /d "%~dp0"
call "%~dp0ops.cmd" stop
timeout /t 3 >nul
