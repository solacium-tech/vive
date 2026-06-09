@echo off
REM ===========================================================================
REM  Run the monitor directly from source (no .exe build).
REM  Use this on a dev/test PC that already has Python + the openvr package,
REM  or where pip install is allowed. Pass any args straight through, e.g.:
REM      run_from_source.bat --site bay3 --duration 300
REM ===========================================================================
setlocal
python -c "import openvr" 2>nul
if errorlevel 1 (
    echo openvr not found. Attempting install ^(needs internet^)...
    python -m pip install openvr
    if errorlevel 1 (
        echo.
        echo Could not install openvr. On the firewalled PC, use the prebuilt
        echo .exe instead ^(see build_exe.bat / README.md^).
        pause
        exit /b 1
    )
)
python vive_dongle_monitor.py %*
endlocal
