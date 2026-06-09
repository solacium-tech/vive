#!/usr/bin/env python3
"""Console front-end for the Vive tracker RF diagnostics engine.

Displays per-tracker and per-dongle link statistics, separating radio
(dongle) problems from optical (lighthouse) problems. Sampling lives in
vive_rf_core; see README.md for the method and field descriptions.

Usage:
  vive_dongle_monitor.exe --site "WarehouseA-bay3" --duration 300

Ctrl+C stops early; the summary and log files are still written.
"""

import argparse
import os
import sys
import time

import vive_rf_core as core

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
CLEAR = "\033[H\033[J"
SEV_COLOUR = {core.HEALTHY: GREEN, core.WARN: YELLOW, core.CRIT: RED}


def c(text, code):
    return f"{code}{text}{RESET}"


def enable_ansi():
    """Enable VT escape processing on Windows consoles."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        k.GetConsoleMode(h, ctypes.byref(mode))
        k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def render(snap, site, recent_notes):
    out = [CLEAR, c("=" * 78, CYAN),
           c(f" VIVE TRACKER LINK MONITOR   site: {site}   "
             f"elapsed: {snap['elapsed']:6.0f}s", BOLD),
           c("=" * 78, CYAN),
           f"{DIM} Radio (dongle) loss vs optical (lighthouse) loss.  "
           f"Ctrl+C to stop.{RESET}", ""]

    if not snap["rows"]:
        out.append(c("  Waiting for trackers... (power them on)", YELLOW))
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()
        return

    for dongle in sorted(snap["by_dongle"]):
        a = snap["aggregates"][dongle]
        out.append(c(f" DONGLE {dongle}   [{a['count']} tracker(s)]  "
                     f"avg radio loss {a['rf_loss_pct']:5.2f}%   "
                     f"dropouts {a['dropouts']}", BOLD))
        for r in sorted(snap["by_dongle"][dongle], key=lambda r: r["serial"]):
            col = SEV_COLOUR[r["severity"]]
            serial = r["serial"]
            label = r["label"]
            batt = f"{r['battery']:3.0f}%" if r["battery"] is not None else "  ?"
            rf_col = SEV_COLOUR[core.severity(
                r["rf_loss_pct"], core.RF_LOSS_WARN, core.RF_LOSS_CRIT)]
            op_col = SEV_COLOUR[core.severity(
                r["optical_loss_pct"], core.OPTICAL_WARN, core.OPTICAL_CRIT)]
            link = "UP " if r["connected"] else "DOWN"
            rf_txt = c("loss " + f"{r['rf_loss_pct']:5.2f}%", rf_col)
            op_txt = c("loss " + f"{r['optical_loss_pct']:5.2f}%", op_col)
            out.append(f"  {serial:<16} {c(f'{label:<24}', col)} link {link}")
            out.append(f"    radio: {rf_txt}"
                       f"  dropouts {r['disconnect_events']:<3} "
                       f"stalls {r['stall_events']:<3} "
                       f"longest {r['longest_disconnect_s']:4.1f}s")
            out.append(f"    optic: {op_txt}"
                       f"  rate {r['update_hz']:5.1f}Hz  batt {batt}")
        out.append("")

    if recent_notes:
        out.append(c(" Recent events (newest last):", BOLD))
        for ts, kind, msg in recent_notes[-6:]:
            out.append(c(f"   {ts.strftime('%H:%M:%S')} {msg}", YELLOW))
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(
        description="Vive tracker dongle/lighthouse link monitor (console)")
    ap.add_argument("--site", default="site",
                    help="label for this run, used in log filenames + CSV")
    ap.add_argument("--log-dir", default="logs", help="directory for logs")
    ap.add_argument("--duration", type=float, default=0,
                    help="auto-stop after N seconds (0 = until Ctrl+C)")
    ap.add_argument("--no-log", action="store_true", help="console only")
    args = ap.parse_args()

    enable_ansi()
    mon = core.RFMonitor(site=args.site, log_dir=args.log_dir,
                         enable_log=not args.no_log)
    mon.start()
    mon.ready.wait(timeout=10)
    if mon.error:
        sys.stderr.write("\n" + c(mon.error, RED) + "\n\n")
        sys.exit(1)

    notes = []
    start = time.time()
    try:
        while True:
            snap = mon.snapshot()
            notes.extend(snap["notifications"])
            notes = notes[-50:]
            render(snap, args.site, notes)
            if args.duration and (time.time() - start) >= args.duration:
                break
            time.sleep(1.0 / 6)
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop()
        mon.join(timeout=5)
        print("\n" + mon.verdict_text())
        if not args.no_log and getattr(mon, "event_path", None):
            print(c(f"\nLogs: {os.path.abspath(args.log_dir)}", CYAN))
        if os.name == "nt":
            try:
                input("\nPress Enter to exit...")
            except EOFError:
                pass


if __name__ == "__main__":
    main()
