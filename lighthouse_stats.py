#!/usr/bin/env python3
"""Read-only capture of SteamVR's lighthouse_console diagnostics.

This is the single source of truth for which lighthouse_console commands the
project will ever send. Every command here is read-only (it prints/dumps state).
Destructive commands from the menu - reboot, poweroff, isp, uploadconfig,
haptic, associatecontroller, clear, save, record, trackpadcalibrate, etc. - are
deliberately excluded and must never be added. A hard guard in console_session()
refuses anything not on the whitelist.

Used by both tools/steamvr_probe.py (standalone probe) and the GUI (snapshot at
the end of a run). lighthouse_console can briefly contend with SteamVR for the
device HIDs, so callers should run this as an occasional snapshot, never on a
hot path during a live capture.
"""
import os
import re
import subprocess
import sys
from datetime import datetime

# Run once per selected device. All read-only.
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

CONSOLE_RELPATH = os.path.join(
    "tools", "lighthouse", "bin", "win64", "lighthouse_console.exe")


def steam_roots():
    """Best-effort list of Steam install roots, across drives and libraries."""
    roots = []
    try:
        import winreg
        for hive, key, name in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    val = winreg.QueryValueEx(k, name)[0]
                    if val:
                        roots.append(os.path.normpath(val))
            except OSError:
                pass
    except Exception:
        pass
    for drive in "CDEFG":
        for sub in (r"Program Files (x86)\Steam", r"Steam", r"SteamLibrary"):
            roots.append(f"{drive}:\\{sub}")
    for r in list(roots):
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"path"' in line.lower():
                        parts = line.split('"')
                        if len(parts) >= 4:
                            roots.append(os.path.normpath(parts[3]))
        except Exception:
            pass
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
    for root in steam_roots():
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
                break
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
        cmds.extend(SAFE_READ_COMMANDS)
    return cmds


def run_capture(path, timeout=120):
    """Capture the banner, then read-only per-device stats. Returns text."""
    banner = console_session(path, [], timeout=30)
    serials = parse_device_serials(banner)
    return "\n".join([
        f"[probe] parsed {len(serials)} device serial(s): "
        f"{', '.join(serials) or '(none)'}",
        f"[probe] read-only commands per device: {SAFE_READ_COMMANDS}",
        "--- session output ---",
        console_session(path, build_capture_script(serials), timeout=timeout),
    ])


def capture_to_file(log_dir, label, console=None, timeout=120):
    """Run the read-only capture and write <label>_<ts>_lighthouse.txt.

    Returns (path_or_None, message). Never raises.
    """
    try:
        path = find_lighthouse_console(console)
        if not path:
            return None, "lighthouse_console.exe not found"
        text = run_capture(path, timeout)
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(c for c in label if c.isalnum() or c in "-_") or "run"
        out = os.path.join(log_dir, f"{safe}_{stamp}_lighthouse.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"Lighthouse stats (read-only)  label={label}  "
                    f"generated={datetime.now().isoformat()}\n"
                    f"console={path}\n\n{text}\n")
        return out, "ok"
    except Exception as exc:
        return None, f"capture failed: {exc}"


if __name__ == "__main__":
    # Quick manual capture: python lighthouse_stats.py [label]
    lbl = sys.argv[1] if len(sys.argv) > 1 else "manual"
    p, m = capture_to_file(os.path.join(os.getcwd(), "logs"), lbl)
    print(f"{m}: {p}")
