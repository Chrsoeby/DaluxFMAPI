@echo off
REM Double-click this file on Windows to open the Dalux app.
cd /d "%~dp0"
python workorder_app.py
if errorlevel 1 (
  echo.
  echo If you saw a "python is not recognized" error, install Python from
  echo https://www.python.org/downloads/ and tick "Add Python to PATH".
  pause
)
