@echo off
rem Builds dist\TwitchChatOverlay\ (a no-install .exe version) and a zip of it for sharing.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call "%~dp0setup.bat"
.venv\Scripts\python.exe -m pip install pyinstaller >nul || (pause & exit /b 1)
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onedir ^
  --name TwitchChatOverlay --icon NONE overlay.py || (pause & exit /b 1)
set OUT=dist\TwitchChatOverlay
copy /y README.md "%OUT%\" >nul
rem no .env in the zip: the program creates it on first run, so unzipping an update never overwrites it
copy /y .env.example "%OUT%\.env.example" >nul
(echo @echo off& echo start "" "%%~dp0TwitchChatOverlay.exe" login) > "%OUT%\Login to Twitch.bat"
powershell -NoProfile -Command "Compress-Archive -Force -Path '%OUT%' -DestinationPath 'dist\TwitchChatOverlay-win64.zip'"
echo.
echo Built dist\TwitchChatOverlay-win64.zip
