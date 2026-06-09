@echo off
REM ===========================================================================
REM  Build a standalone vive_dongle_monitor.exe
REM ===========================================================================
REM  RUN THIS ON A MACHINE WITH INTERNET ACCESS.
REM  The firewalled SteamVR PC does NOT need internet to RUN the resulting .exe
REM  - it only talks to SteamVR over local IPC. Build here, copy the single
REM  file in dist\ across to the SteamVR PC.
REM
REM  Requirements on the BUILD machine: Python 3.9+ on PATH.
REM ===========================================================================
setlocal
cls
echo === Vive Dongle Monitor - build ===
echo.

echo [1/3] Creating an isolated build environment...
python -m venv .build_venv
call .build_venv\Scripts\activate.bat

echo [2/3] Installing dependencies (needs internet, build machine only)...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: dependency install failed. Are you on a machine with internet?
    goto :end
)

echo [3/3] Building single-file executable with PyInstaller...
REM --collect-all openvr ensures openvr_api.dll and bindings are bundled.
pyinstaller --onefile --console --name vive_dongle_monitor ^
    --collect-all openvr ^
    vive_dongle_monitor.py
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed.
    goto :end
)

echo.
echo =====================================================================
echo  DONE.  Your tool is: dist\vive_dongle_monitor.exe
echo  Copy that single file to the firewalled SteamVR PC and run it.
echo =====================================================================

:end
deactivate
endlocal
pause
