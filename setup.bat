@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  ScanForge setup
echo ============================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python was not found on PATH.
        echo Install Python 3.10 or newer from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during installation.
        pause
        exit /b 1
    )
    set "PY=python"
)

echo Creating a private virtual environment in .venv ...
%PY% -m venv .venv
if errorlevel 1 (
    echo Could not create the virtual environment.
    pause
    exit /b 1
)

echo.
echo Installing packages ^(this pulls about 250 MB the first time^) ...
call .venv\Scripts\python.exe -m pip install --upgrade pip
call .venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo Package installation failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Done. Start the app with  run.bat
echo ============================================
pause
