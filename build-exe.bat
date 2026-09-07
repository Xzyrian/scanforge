@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo  ScanForge - build a shareable executable
echo ============================================
echo.

rem ---- arguments -------------------------------------------------------
rem   onefile   one big .exe instead of a folder (slower to start)
rem   quick     skip the source self-test before building
rem   nopause   do not wait for a keypress at the end (for scripts)
set "SCANFORGE_ONEFILE=0"
set "MODE=folder"
set "PAUSE_AT_END=1"
set "SKIP_SOURCE_CHECK=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="onefile" (
    set "SCANFORGE_ONEFILE=1"
    set "MODE=onefile"
)
if /i "%~1"=="quick" set "SKIP_SOURCE_CHECK=1"
if /i "%~1"=="nopause" set "PAUSE_AT_END=0"
shift
goto parse_args
:args_done

echo Build mode: %MODE%
echo.

if not exist ".venv\Scripts\python.exe" (
    echo ScanForge is not set up yet - run setup.bat first.
    goto fail
)
set "PY=.venv\Scripts\python.exe"

rem ---------------------------------------------------------------------
echo [1/8] Checking the build environment...

rem Both OpenCV wheels installed means both get bundled - about 150 MB of
rem duplicate libraries in the output for no benefit.
"%PY%" -c "from importlib.metadata import distributions; names=sorted({(d.metadata['Name'] or '') for d in distributions() if (d.metadata['Name'] or '').lower().startswith('opencv')}); print(' '.join(names)); raise SystemExit(0 if len(names)>1 else 1)" >"%TEMP%\sf_cv.txt" 2>nul
if not errorlevel 1 (
    echo.
    echo   NOTE: more than one OpenCV package is installed in .venv:
    type "%TEMP%\sf_cv.txt"
    echo   The build will be much larger than it needs to be. To fix:
    echo     .venv\Scripts\python.exe -m pip uninstall opencv-python opencv-contrib-python
    echo   ^(keep opencv-python-headless^)
    echo.
)
del "%TEMP%\sf_cv.txt" >nul 2>nul

rem ---------------------------------------------------------------------
echo [2/8] Making sure the dependencies are current...
rem requirements.txt changes as the app grows; a venv from six months ago
rem builds an exe that is missing whatever was added since.
"%PY%" -m pip install -r requirements.txt --upgrade-strategy only-if-needed >nul
if errorlevel 1 (
    echo Could not install the packages in requirements.txt.
    goto fail
)
"%PY%" -m pip install --upgrade "pyinstaller>=6.3" >nul
if errorlevel 1 (
    echo Could not install PyInstaller.
    goto fail
)

rem ---------------------------------------------------------------------
if "%SKIP_SOURCE_CHECK%"=="1" (
    echo [3/8] Source self-test skipped ^("quick"^).
) else (
    echo [3/8] Self-testing the source tree first...
    rem Two minutes here beats ten minutes of PyInstaller followed by the
    rem discovery that the source was already broken.
    set "SCANFORGE_SELFTEST_LOG="
    "%PY%" tools\freeze_check.py
    if errorlevel 1 (
        echo.
        echo The source tree does not pass its own checks - fix that first.
        echo Nothing was built.
        goto fail
    )
)

rem ---------------------------------------------------------------------
echo [4/8] Drawing the application icon...
"%PY%" tools\make_icon.py
if errorlevel 1 (
    echo Icon generation failed - continuing without a custom icon.
)

rem ---------------------------------------------------------------------
echo [5/8] Clearing the previous build...
rem Without this a failed build leaves the last good exe in dist\, and the
rem steps below happily test and zip that instead - which is how a stale
rem build gets shipped.
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

rem ---------------------------------------------------------------------
echo [6/8] Building ^(this takes a few minutes the first time^)...
"%PY%" -m PyInstaller scanforge.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo BUILD FAILED. The output above says why.
    goto fail
)

if "%SCANFORGE_ONEFILE%"=="1" (
    set "APP=dist\ScanForge.exe"
) else (
    set "APP=dist\ScanForge\ScanForge.exe"
)
if not exist "!APP!" (
    echo Expected !APP! but it is not there.
    echo PyInstaller reported success, so the spec file and this script
    echo disagree about where the output goes.
    goto fail
)

rem ---------------------------------------------------------------------
echo [7/8] Testing the built executable...
rem A frozen build can import cleanly from source and still be missing a C
rem extension. Only running the real .exe proves it works.  It is a windowed
rem build, so it has no console to print to - it writes its report to a file
rem and we show that instead.
set "SELFTEST_LOG=%CD%\dist\selftest.log"
set "SCANFORGE_SELFTEST_LOG=%SELFTEST_LOG%"
"!APP!" --selftest
set "SELFTEST_RC=!errorlevel!"
set "SCANFORGE_SELFTEST_LOG="
echo.
if exist "%SELFTEST_LOG%" (
    type "%SELFTEST_LOG%"
) else (
    echo   The executable produced no report at all. It could not start.
)
if not "!SELFTEST_RC!"=="0" (
    echo.
    echo ============================================
    echo  THE BUILD IS BROKEN - do not share it.
    echo  The failing check is listed above.
    echo ============================================
    goto fail
)

rem ---------------------------------------------------------------------
echo.
echo [8/8] Packaging for sharing...
set "ZIP=dist\ScanForge-windows.zip"
if exist "%ZIP%" del "%ZIP%"
if "%SCANFORGE_ONEFILE%"=="1" (
    powershell -NoProfile -Command ^
      "Compress-Archive -Path 'dist\ScanForge.exe','README.md' -DestinationPath '%ZIP%' -Force"
) else (
    del "%SELFTEST_LOG%" >nul 2>nul
    powershell -NoProfile -Command ^
      "Copy-Item 'README.md' 'dist\ScanForge\README.md' -Force; Compress-Archive -Path 'dist\ScanForge\*' -DestinationPath '%ZIP%' -Force"
)
if errorlevel 1 (
    echo The zip could not be created, but the executable itself is fine.
)

for %%F in ("%ZIP%") do set "ZIPMB=%%~zF"
if defined ZIPMB set /a ZIPMB=!ZIPMB! / 1048576

echo.
echo ============================================
echo  Done.
echo.
if "%SCANFORGE_ONEFILE%"=="1" (
    echo   Executable : dist\ScanForge.exe
    echo   To share   : %ZIP%  ^(!ZIPMB! MB^)
    echo.
    echo   Note: a onefile build unpacks itself on every launch,
    echo   so it starts several seconds slower than the folder build.
) else (
    echo   Executable : dist\ScanForge\ScanForge.exe
    echo   To share   : %ZIP%  ^(!ZIPMB! MB^)
    echo.
    echo   Whoever you send it to unzips it and runs ScanForge.exe.
    echo   No Python needed on their machine.
)
echo ============================================
if "%PAUSE_AT_END%"=="1" pause
exit /b 0

:fail
echo.
if "%PAUSE_AT_END%"=="1" pause
exit /b 1
