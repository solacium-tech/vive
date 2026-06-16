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

echo [3/3] Building single-file executables with PyInstaller...
REM --collect-all openvr ensures openvr_api.dll and bindings are bundled.
REM Two outputs: a windowed GUI and a console version. Both are standalone.

echo   - GUI build...
pyinstaller --onefile --noconsole --name vive_dongle_gui ^
    --collect-all openvr ^
    vive_dongle_gui.py
if errorlevel 1 (
    echo ERROR: GUI build failed.
    goto :end
)

echo   - Console build...
pyinstaller --onefile --console --name vive_dongle_monitor ^
    --collect-all openvr ^
    vive_dongle_monitor.py
if errorlevel 1 (
    echo ERROR: console build failed.
    goto :end
)

echo   - SteamVR probe build...
pyinstaller --onefile --console --name steamvr_probe ^
    --paths . --hidden-import lighthouse_stats ^
    --collect-all openvr ^
    tools\steamvr_probe.py
if errorlevel 1 (
    echo ERROR: probe build failed.
    goto :end
)

echo.
echo =====================================================================
echo  DONE.  Your tools are in dist\ :
echo    dist\vive_dongle_gui.exe       (friendly window - start here)
echo    dist\vive_dongle_monitor.exe   (console + CLI flags, scriptable)
echo    dist\steamvr_probe.exe         (read-only SteamVR diagnostic probe)
echo  Copy whichever you want to the firewalled SteamVR PC and run it.
echo =====================================================================

:end
deactivate
endlocal
pause
