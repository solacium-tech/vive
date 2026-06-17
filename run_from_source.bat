@echo off
REM ===========================================================================
REM  Run the monitor GUI directly from source (no .exe, so nothing for
REM  antivirus to flag). Use this on a PC that has Python + the openvr package
REM  (or where "pip install openvr" is allowed).
REM
REM  This launches the same windowed tool as the build - including the live
REM  jitter detection - just run from the .py instead of a packaged .exe.
REM ===========================================================================
setlocal
python -c "import openvr" 2>nul
if errorlevel 1 (
    echo openvr not found. Attempting install ^(needs internet^)...
    python -m pip install openvr
    if errorlevel 1 (
        echo.
        echo Could not install openvr. On a firewalled PC with no Python,
        echo use the prebuilt vive_dongle_gui folder instead ^(build_exe.bat^).
        pause
        exit /b 1
    )
)
python vive_dongle_gui.py
endlocal
