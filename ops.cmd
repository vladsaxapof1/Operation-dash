@echo off
rem Pult command for cmd and PowerShell
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%~dp0.venv\Scripts\python.exe" -m opsdash %*
