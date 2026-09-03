@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo ScanForge is not set up yet - run setup.bat first.
    pause
    exit /b 1
)

rem pythonw keeps the console window from hanging around behind the app.
rem Any files or folders dropped on this .bat are opened straight into the queue.
start "" ".venv\Scripts\pythonw.exe" -m scanforge %*
