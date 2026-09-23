@echo off
cd /d "%~dp0"
if exist .venv\Scripts\pythonw.exe (
  start "" .venv\Scripts\pythonw.exe overlay.py %*
) else (
  start "" pythonw overlay.py %*
)
