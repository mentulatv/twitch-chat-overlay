@echo off
rem Log in to Twitch for follow + channel point alerts (needs TWITCH_CLIENT_ID in .env).
cd /d "%~dp0"
if exist .venv\Scripts\pythonw.exe (
  start "" .venv\Scripts\pythonw.exe overlay.py login
) else (
  start "" pythonw overlay.py login
)
