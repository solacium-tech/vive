#!/usr/bin/env python3
"""Read-only SteamVR / Lighthouse diagnostic probe.

Purpose: discover what the deeper SteamVR channels actually expose on a given
rig, WITHOUT changing anything on the devices. Run it once on a problem site
and send back the output file; that tells us whether there's usable per-device
radio / optical data worth wiring into the monitor, or whether we've hit the
firmware ceiling. Hardware and SteamVR versions vary between sites, so this
auto-discovers paths and only enumerates - it does not assume a fixed command
set.

SAFETY
------
`lighthouse_console.exe` can ALSO update firmware and change device settings
(channels, pairing). This probe therefore sends ONLY a hard-coded whitelist of
read-only discovery commands (`help`, `?`) - never anything that writes. The
optional DriverDebugRequest pass is OFF unless you pass --driver-debug, and even
then only sends a few harmless informational candidate strings.

Usage
-----
    python steamvr_probe.py                 # lighthouse_console discovery only
    python steamvr_probe.py --console PATH   # point at a specific exe
    python steamvr_probe.py --driver-debug   # also try OpenVR DriverDebugRequest

Output is written to logs/steamvr_probe_<timestamp>.txt next to this script.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys

# The ONLY command first-tokens this probe will ever send to lighthouse_console.
# Every one is read-only (prints/dumps state). Destructive commands seen in the
# menu - reboot, poweroff, isp, uploadconfig, haptic, associatecontroller,
# clear, save, record, trackpadcalibrate, etc. - are deliberately excluded and
# must never be added here.
# Run once per selected device.
SAFE_READ_COMMANDS = [
    "version",      # firmware/hardware version
    "battery",      # battery status
    "errors",       # lighthouse error/status structure
    "usbstats",     # one-time USB packet-rate stats (incremental-loss signal)
    "sensorcheck",  # hits/widths per optical sensor (dead-zone signal)
    "imustats",     # IMU statistics
    "period",       # sync statistics
]
ALLOWED_FIRST_TOKENS = set(SAFE_READ_COMMANDS) | {
    "help", "?", "deviceinfo", "serial", "quit", "exit"}

# Optional, experimental, read-ish strings for IVRSystem::DriverDebugRequest.
# The lighthouse driver decides what these mean; unknown ones typically return
# empty. Kept minimal and informational on purpose.
DRIVER_DEBUG_CANDIDATES = [
    "GetSerialNumber",
    "version",
    "stats",
    "debug",
]

CONSOLE_RELPATH = os.path.join(
    "tools", "lighthouse", "bin", "win64", "lighthouse_console.exe")


def _base_dir():
    """Directory to write output into: next to the exe when frozen, else here."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _steam_paths():
    """Best-effort list of Steam install roots, across drives and libraries."""
    roots = []

    # 1) Windows registry (most reliable when present).
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    val = winreg.QueryValueEx(
                        k, "SteamPath" if hive == winreg.HKEY_CURRENT_USER
                        else "InstallPath")[0]
                    if val:
                        roots.append(os.path.normpath(val))
            except OSError:
                pass
    except Exception:
        pass

    # 2) Common install locations across drive letters.
    for drive in "CDEFG":
        for sub in (r"Program Files (x86)\Steam", r"Steam", r"SteamLibrary"):
            roots.append(f"{drive}:\\{sub}")

    # 3) Extra Steam library folders declared in libraryfolders.vdf.
    for r in list(roots):
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    # crude: pull any "path" "<dir>" entries without a vdf parser
                    if '"path"' in line.lower():
                        parts = line.split('"')
                        if len(parts) >= 4:
                            roots.append(os.path.normpath(parts[3]))
        except Exception:
            pass

    # de-dup, keep order
    seen, out = set(), []
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def find_lighthouse_console(explicit=None):
    """Locate lighthouse_console.exe, or return None."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for root in _steam_paths():
        cand = os.path.join(
            root, "steamapps", "common", "SteamVR", CONSOLE_RELPATH)
        if os.path.isfile(cand):
            return cand
    return None


def console_session(path, commands, timeout=60):
    """Launch lighthouse_console once, send `commands` (whitelisted) via stdin,
    capture output. Always appends quit AND exit so it terminates regardless of
    which keyword this version uses."""
    for c in commands:
        token = c.split()[0] if c.strip() else ""
        if token and token not in ALLOWED_FIRST_TOKENS:
            # Hard guard: refuse to ever emit a non-whitelisted command.
            raise ValueError(f"refusing non-whitelisted command: {c!r}")
    script = "\n".join(list(commands) + ["quit", "exit", ""])
    try:
        proc = subprocess.run(
            [path], input=script, capture_output=True, text=True,
            timeout=timeout, cwd=os.path.dirname(path) or None)
        return (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr
                                      if proc.stderr else "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "ignore")
        return out + f"\n[probe] timed out after {timeout}s (process killed)"
    except Exception as exc:
        return f"[probe] failed to run lighthouse_console: {exc}"


def parse_device_serials(banner):
    """Pull the attached-receiver serial list out of the launch banner."""
    serials = []
    grabbing = False
    for line in banner.splitlines():
        if "Attached lighthouse receiver devices" in line:
            grabbing = True
            continue
        if grabbing:
            s = line.strip()
            if not s:
                continue
            if not line[:1].isspace():
                break  # left the indented device list
            # Serials are indented alnum/dash tokens that always contain a
            # digit (e.g. 6D25A40A5C, LHR-065846B2, 08E7C183C7-RYB).
            if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z-]{5,}", s) and \
                    any(c.isdigit() for c in s):
                serials.append(s)
    return serials


def build_capture_script(serials):
    """Read-only command sequence: per-device stats for every receiver."""
    cmds = ["help", "deviceinfo"]
    if serials:
        for s in serials:
            cmds.append(f"serial {s}")
            cmds.extend(SAFE_READ_COMMANDS)
    else:
        # Couldn't parse the list; just probe whatever device auto-connected.
        cmds.extend(SAFE_READ_COMMANDS)
    return cmds


def run_lighthouse_console(path, timeout=90):
    """Capture the banner, then read-only per-device stats."""
    banner = console_session(path, [], timeout=30)
    serials = parse_device_serials(banner)
    script = build_capture_script(serials)
    out = [f"[probe] parsed {len(serials)} device serial(s): "
           f"{', '.join(serials) or '(none)'}",
           f"[probe] read-only commands per device: {SAFE_READ_COMMANDS}",
           "--- session output ---",
           console_session(path, script, timeout=timeout)]
    return "\n".join(out)


def probe_driver_debug():
    """Optional: try IVRSystem::DriverDebugRequest on each tracked device."""
    lines = []
    try:
        import openvr
    except Exception as exc:
        return f"[probe] openvr not importable: {exc}\n"
    vr = None
    try:
        vr = openvr.init(openvr.VRApplication_Background)
        for idx in range(openvr.k_unMaxTrackedDeviceCount):
            cls = vr.getTrackedDeviceClass(idx)
            if cls in (openvr.TrackedDeviceClass_Invalid,):
                continue
            serial = ""
            try:
                serial = vr.getStringTrackedDeviceProperty(
                    idx, openvr.Prop_SerialNumber_String)
            except Exception:
                pass
            lines.append(f"\n-- device {idx} class={cls} serial={serial} --")
            for req in DRIVER_DEBUG_CANDIDATES:
                try:
                    resp = vr.driverDebugRequest(idx, req, 4096)
                except Exception as exc:
                    resp = f"<error: {exc}>"
                lines.append(f"  driverDebugRequest({req!r}) -> {resp!r}")
    except Exception as exc:
        lines.append(f"[probe] openvr init/enumerate failed: {exc}")
    finally:
        try:
            if vr is not None:
                openvr.shutdown()
        except Exception:
            pass
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", help="path to lighthouse_console.exe")
    ap.add_argument("--driver-debug", action="store_true",
                    help="also try OpenVR DriverDebugRequest (experimental)")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    out = []
    out.append("SteamVR / Lighthouse read-only probe")
    out.append(f"generated: {datetime.datetime.now().isoformat()}")
    out.append(f"platform : {sys.platform}")
    if sys.platform != "win32":
        out.append("[probe] note: lighthouse_console is Windows-only; "
                   "run this on the rig for real output.")

    out.append("\n=== Steam roots searched ===")
    out.extend(_steam_paths())

    console = find_lighthouse_console(args.console)
    out.append("\n=== lighthouse_console ===")
    if console:
        out.append(f"found: {console}")
        out.append("--- output ---")
        out.append(run_lighthouse_console(console, args.timeout))
    else:
        out.append("NOT FOUND. Pass --console <path> if SteamVR is installed "
                   "somewhere unusual.")

    out.append("\n=== DriverDebugRequest ===")
    if args.driver_debug:
        out.append(probe_driver_debug())
    else:
        out.append("skipped (pass --driver-debug to enable)")

    text = "\n".join(out) + "\n"
    # Print first, so the console shows output even if the file write fails.
    print(text)

    log_dir = os.path.join(_base_dir(), "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(log_dir, f"steamvr_probe_{stamp}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[probe] written to {path}")
    except Exception as exc:
        print(f"[probe] could NOT write output file: {exc}")


def _entry():
    """Wrapper: surface any crash and keep the window open for double-clicks."""
    try:
        main()
    except Exception:
        import traceback
        print("\n[probe] CRASHED:")
        traceback.print_exc()
    finally:
        # When run as the packaged .exe (double-clicked), the console would
        # otherwise vanish before it can be read.
        if getattr(sys, "frozen", False):
            try:
                input("\nDone. Press Enter to close this window...")
            except Exception:
                pass


if __name__ == "__main__":
    _entry()
