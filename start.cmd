@echo off
cd /d "%~dp0"
if exist "dist\BiliWatch.exe" (
  start "" "dist\BiliWatch.exe"
) else (
  ".venv\Scripts\python.exe" app.py
)
