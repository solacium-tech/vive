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
import subprocess
import sys

# The ONLY commands this probe will ever send to lighthouse_console. Both are
# pure discovery/read. Do not add write/update/channel/pair commands here.
ALLOWED_CONSOLE_COMMANDS = ["help", "?"]

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


def run_lighthouse_console(path, timeout=30):
    """Launch the console, send ONLY whitelisted read commands, capture output."""
    # Commands are piped via stdin; we always finish with quit AND exit so the
    # process terminates regardless of which keyword this version uses.
    script = "\n".join(ALLOWED_CONSOLE_COMMANDS + ["quit", "exit", ""])
    try:
        proc = subprocess.run(
            [path], input=script, capture_output=True, text=True,
            timeout=timeout, cwd=os.path.dirname(path) or None)
        return (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr
                                      if proc.stderr else "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "ignore")
        return out + f"\n[probe] timed out after {timeout}s (process killed)"
    except Exception as exc:
        return f"[probe] failed to run lighthouse_console: {exc}"


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
    ap.add_argument("--timeout", type=int, default=30)
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
        out.append(f"sending read-only commands: {ALLOWED_CONSOLE_COMMANDS}")
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

    log_dir = os.path.join(_base_dir(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(log_dir, f"steamvr_probe_{stamp}.txt")
    text = "\n".join(out) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\n[probe] written to {path}")


if __name__ == "__main__":
    main()
