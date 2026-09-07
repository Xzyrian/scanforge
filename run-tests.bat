@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)

echo Pipeline tests ^(builds a synthetic 10000x14000 scan; takes a couple of minutes^)
".venv\Scripts\python.exe" tests\test_pipeline.py
echo.
echo UI tests
".venv\Scripts\python.exe" tests\test_ui.py
echo.
echo Self-test ^(the same checks build-exe.bat runs against the built .exe^)
".venv\Scripts\python.exe" tools\freeze_check.py
pause
