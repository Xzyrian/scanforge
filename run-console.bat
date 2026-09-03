@echo off
rem Same as run.bat but keeps a console window open so you can see any errors.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ScanForge is not set up yet - run setup.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m scanforge %*
pause
