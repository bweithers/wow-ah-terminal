@echo off
setlocal
cd /d "%~dp0"
title WoW auction scans -^> shared pool

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python is not on your PATH.
  echo Install it from https://www.python.org/downloads/ and tick
  echo "Add python.exe to PATH" in the installer, then run this again.
  echo.
  pause
  exit /b 1
)

echo Leave this window open. Scan the auction house in game, then /reload,
echo and the scan goes to the shared pool a few seconds later.
echo Close the window or press Ctrl-C to stop.
echo.

python ah.py watch %*

echo.
echo The uploader stopped.
pause
