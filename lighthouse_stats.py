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
    "usbstats",     # USB packet-rate stats (incremental-loss signal)
    "sensorcheck",  # hits/widths per optical sensor (dead-zone signal)
    "imustats",     # IMU statistics (rate + interval jitter)
    "period",       # sync statistics
]
# usbstats needs a ~10s window to actually measure (otherwise it just prints
# "measuring..."), so we issue it, sleep, then read it again. `sleep` is the
# console's own read-only "really just sleep" command.
PER_DEVICE_SEQUENCE = [
    "version", "battery", "errors", "sensorcheck", "imustats", "period",
    "usbstats", "sleep 10500", "usbstats",
]
ALLOWED_FIRST_TOKENS = set(SAFE_READ_COMMANDS) | {
    "help", "?", "deviceinfo", "serial", "sleep", "quit", "exit"}

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
            cmds.extend(PER_DEVICE_SEQUENCE)
    else:
        cmds.extend(PER_DEVICE_SEQUENCE)
    return cmds


# ---- summary parsing (best-effort, tolerant of version differences) --------

ERROR_NAMES = (
    "missing_rising_edge", "backward_time", "pulse_queue_overflow",
    "short_sync", "long_sync", "invalid_calibration_size",
    "invalid_calibration_crc", "invalid_calibration_version", "queue_overflow",
    "spammy_sensor", "long_optical_packet_delay",
)


def _device_blocks(raw):
    """Split the session output into (dongle_serial, text) blocks."""
    marker = re.compile(r"Connected to receiver ([0-9A-Za-z-]+)")
    blocks, cur, name = [], [], None
    for line in raw.splitlines():
        m = marker.search(line)
        if m and "lighthouse_console:" in line:
            if name is not None:
                blocks.append((name, "\n".join(cur)))
            name, cur = m.group(1), [line]
        else:
            cur.append(line)
    if name is not None:
        blocks.append((name, "\n".join(cur)))
    return blocks


def summarize(raw):
    """Turn the raw capture into a compact per-receiver health table."""
    rows = []
    for dongle, text in _device_blocks(raw):
        tracker = ""
        mt = re.search(r"(LHR-[0-9A-Fa-f]+): Connected to receiver", text)
        if mt:
            tracker = mt.group(1)
        batt = (re.search(r"battery \([^)]*\):\s*(\d+)", text) or [None, "?"])[1]
        imu = re.search(r"rate ([\d.]+)Hz interval [\d.]+ms sigma ([\d.]+)ms",
                        text)
        imu_hz = imu.group(1) if imu else "?"
        imu_sigma = imu.group(2) if imu else "?"
        # nonzero error counters
        errs = []
        for name in ERROR_NAMES:
            m = re.search(rf"{name}\s*:\s*(\d+)", text)
            if m and int(m.group(1)) > 0:
                errs.append(f"{name}={m.group(1)}")
        # sensor hits: take the table with the most rows seeing light
        best_total = best_hit = 0
        for tbl in re.split(r"SensorID\s+HitCount", text)[1:]:
            total = hit = 0
            for ln in tbl.splitlines():
                m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+\d+\s+\d+", ln)
                if m:
                    total += 1
                    if int(m.group(2)) > 0:
                        hit += 1
            if total and hit >= best_hit:
                best_total, best_hit = total, hit
        sensors = f"{best_hit}/{best_total}" if best_total else "-"
        # usb packet rates (after the dwell); grab any "<stream> <n>/sec"-ish
        usb = re.findall(r"\b(IMU|Optical|VrController)\b[^\n]*?([\d.]+)\s*/?s",
                         text)
        usb_str = ", ".join(f"{k}={v}" for k, v in usb) or "measuring/none"
        rows.append((dongle, tracker, f"{batt}%", imu_hz, imu_sigma,
                     sensors, usb_str, ", ".join(errs) or "none"))

    # The device auto-connected at launch yields a duplicate, sparser row;
    # keep the last (fullest) row per dongle, preserving first-seen order.
    order, dedup = [], {}
    for r in rows:
        if r[0] not in dedup:
            order.append(r[0])
        dedup[r[0]] = r
    rows = [dedup[k] for k in order]

    head = ("dongle", "tracker", "batt", "imuHz", "imuSig",
            "sens(hit/seen)", "usb_rate", "nonzero_errors")
    widths = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = ["=== SUMMARY (per receiver; read-only) ===",
             fmt.format(*head)]
    lines += [fmt.format(*map(str, r)) for r in rows]
    lines.append("")
    lines.append("notes: low sens(hit/seen) = optical dead zone/occlusion; "
                 "low imuHz or high imuSig = jittery/starved link; any "
                 "nonzero_errors worth investigating.")
    return "\n".join(lines)


def run_capture(path, timeout=240):
    """Capture the banner, then read-only per-device stats. Returns text with a
    parsed summary on top and the raw dump below."""
    banner = console_session(path, [], timeout=30)
    serials = parse_device_serials(banner)
    raw = console_session(path, build_capture_script(serials), timeout=timeout)
    try:
        summary = summarize(raw)
    except Exception as exc:
        summary = f"[probe] summary parse failed: {exc}"
    return "\n".join([
        f"[probe] parsed {len(serials)} device serial(s): "
        f"{', '.join(serials) or '(none)'}",
        "",
        summary,
        "",
        "=== RAW SESSION OUTPUT ===",
        raw,
    ])


def capture_to_file(log_dir, label, console=None, timeout=240):
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
