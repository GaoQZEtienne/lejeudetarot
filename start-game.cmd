@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please follow README.md to create .venv and install requirements first.
  pause
  exit /b 1
)
echo Open http://127.0.0.1:8080 in your browser.
echo Keep this window open while playing. Press Ctrl+C to stop.
".venv\Scripts\python.exe" server.py
pause
