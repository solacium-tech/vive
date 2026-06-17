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

echo [3/3] Building executables with PyInstaller...
REM --collect-all openvr ensures openvr_api.dll and bindings are bundled.
REM --onedir (folder) builds are used instead of --onefile: the single-file
REM self-extracting stub is a common antivirus false-positive trigger. Each
REM tool ends up as a FOLDER under dist\ - copy the whole folder to the rig.

echo   - GUI build...
pyinstaller --onedir --noconsole --name vive_dongle_gui ^
    --collect-all openvr ^
    vive_dongle_gui.py
if errorlevel 1 (
    echo ERROR: GUI build failed.
    goto :end
)

echo   - Console build...
pyinstaller --onedir --console --name vive_dongle_monitor ^
    --collect-all openvr ^
    vive_dongle_monitor.py
if errorlevel 1 (
    echo ERROR: console build failed.
    goto :end
)

REM The probe reads the registry and launches lighthouse_console, which trips
REM antivirus "trojan" heuristics. It is NOT built by default - pass "probe" to
REM build it only when you specifically need the deep diagnostic:
REM     build_exe.bat probe
if /I "%~1"=="probe" (
    echo   - SteamVR probe build...
    pyinstaller --onedir --console --name steamvr_probe ^
        --paths . --hidden-import lighthouse_stats ^
        --collect-all openvr ^
        tools\steamvr_probe.py
    if errorlevel 1 (
        echo ERROR: probe build failed.
        goto :end
    )
)

echo.
echo =====================================================================
echo  DONE.  Each tool is a FOLDER under dist\ - copy the WHOLE folder:
echo    dist\vive_dongle_gui\vive_dongle_gui.exe       (start here)
echo    dist\vive_dongle_monitor\vive_dongle_monitor.exe   (console + CLI)
echo  For normal monitoring copy ONLY the vive_dongle_gui folder.
echo  The SteamVR probe is built only with: build_exe.bat probe
echo  (it can trip antivirus; build/copy it only when you need it).
echo =====================================================================

:end
deactivate
endlocal
pause
