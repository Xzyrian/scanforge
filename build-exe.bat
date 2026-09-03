@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo  ScanForge - build a shareable executable
echo ============================================
echo.

rem ---- onefile?  "build-exe.bat onefile" for a single .exe --------------
set "SCANFORGE_ONEFILE=0"
set "MODE=folder"
if /i "%~1"=="onefile" (
    set "SCANFORGE_ONEFILE=1"
    set "MODE=onefile"
)
echo Build mode: %MODE%
echo.

if not exist ".venv\Scripts\python.exe" (
    echo ScanForge is not set up yet - run setup.bat first.
    pause
    exit /b 1
)
set "PY=.venv\Scripts\python.exe"

rem Both OpenCV wheels installed means both get bundled - about 150 MB of
rem duplicate libraries in the output for no benefit.
"%PY%" -c "import importlib.util as u, sys; sys.exit(0 if (u.find_spec('cv2') and __import__('importlib.metadata',fromlist=['x']).distributions and len([d for d in __import__('importlib.metadata',fromlist=['x']).distributions() if (d.metadata['Name'] or '').startswith('opencv-python')])>1) else 1)" >nul 2>nul
if not errorlevel 1 (
    echo.
    echo   NOTE: more than one OpenCV package is installed in .venv.
    echo   The build will be much larger than it needs to be. To fix:
    echo     .venv\Scripts\python.exe -m pip uninstall opencv-python opencv-contrib-python
    echo   ^(keep opencv-python-headless^)
    echo.
)

echo [1/5] Making sure PyInstaller is installed...
"%PY%" -m pip install --upgrade "pyinstaller>=6.3" >nul
if errorlevel 1 (
    echo Could not install PyInstaller.
    pause
    exit /b 1
)

echo [2/5] Drawing the application icon...
"%PY%" tools\make_icon.py
if errorlevel 1 (
    echo Icon generation failed - continuing without a custom icon.
)

echo [3/5] Building ^(this takes a few minutes the first time^)...
"%PY%" -m PyInstaller scanforge.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo BUILD FAILED. The output above says why.
    pause
    exit /b 1
)

rem ---- locate what we built --------------------------------------------
if "%SCANFORGE_ONEFILE%"=="1" (
    set "APP=dist\ScanForge.exe"
) else (
    set "APP=dist\ScanForge\ScanForge.exe"
)
if not exist "!APP!" (
    echo Expected !APP! but it is not there.
    pause
    exit /b 1
)

echo.
echo [4/5] Testing the built executable...
rem The point of this step: a frozen build can import cleanly from source and
rem still be missing a C extension. Only running the real .exe proves it works.
"!APP!" --selftest
if errorlevel 1 (
    echo.
    echo ============================================
    echo  THE BUILD IS BROKEN - do not share it.
    echo  The failing check is listed above.
    echo ============================================
    pause
    exit /b 1
)

echo.
echo [5/5] Packaging for sharing...
set "ZIP=dist\ScanForge-windows.zip"
if exist "%ZIP%" del "%ZIP%"
if "%SCANFORGE_ONEFILE%"=="1" (
    powershell -NoProfile -Command ^
      "Compress-Archive -Path 'dist\ScanForge.exe','README.md' -DestinationPath '%ZIP%' -Force"
) else (
    powershell -NoProfile -Command ^
      "Copy-Item 'README.md' 'dist\ScanForge\README.md' -Force; Compress-Archive -Path 'dist\ScanForge\*' -DestinationPath '%ZIP%' -Force"
)

echo.
echo ============================================
echo  Done.
echo.
if "%SCANFORGE_ONEFILE%"=="1" (
    echo   Executable : dist\ScanForge.exe
    echo   To share   : %ZIP%
    echo.
    echo   Note: a onefile build unpacks itself on every launch,
    echo   so it starts several seconds slower than the folder build.
) else (
    echo   Executable : dist\ScanForge\ScanForge.exe
    echo   To share   : %ZIP%
    echo.
    echo   Whoever you send it to unzips it and runs ScanForge.exe.
    echo   No Python needed on their machine.
)
echo ============================================
pause
