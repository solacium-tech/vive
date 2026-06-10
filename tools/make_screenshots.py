#!/usr/bin/env python3
"""Render how-to screenshots of the Vive Tracker Link Monitor GUI.

The real GUI talks to SteamVR through OpenVR, which needs Windows and live
hardware. For documentation we drive the *actual* GUI widgets but feed them a
canned snapshot built from real TrackerStat/RFMonitor code, so the layout,
colours and wording match a genuine run exactly. Only the sampling thread is
faked.

Run on a machine with tkinter + Pillow. Headless Linux works under Xvfb:

    xvfb-run -s "-screen 0 1280x960x24" python3 tools/make_screenshots.py

Images are written to docs/screenshots/.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tkinter as tk          # noqa: E402
from PIL import ImageGrab      # noqa: E402

import vive_rf_core as core    # noqa: E402
import vive_dongle_gui as gui  # noqa: E402

OUT = os.path.join(ROOT, "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)


def make_tracker(serial, dongle, name, rf_pct, opt_pct, hz, batt, drops,
                 connected=True, longest=0.0, first_issue=None,
                 first_kind="", last_issue=None):
    """Build a real TrackerStat with counters set to the wanted percentages."""
    st = core.TrackerStat(serial, "Vive Tracker 3.0", dongle, name=name)
    total = 100000
    st.samples = total
    st.rf_loss_samples = int(round(total * rf_pct / 100.0))
    st.connected_samples = total - st.rf_loss_samples
    st.out_of_range_samples = int(round(st.connected_samples * opt_pct / 100.0))
    st.pose_valid_samples = st.connected_samples - st.out_of_range_samples
    if hz is not None:
        st.packet_ever_advanced = True
        st.connected_elapsed_s = 400.0
        st.packet_increments = int(hz * st.connected_elapsed_s)
    st.battery = batt
    st.disconnect_events = drops
    st.was_connected = connected
    st.longest_disconnect_s = longest
    st.first_issue_time = first_issue
    st.first_issue_kind = first_kind
    st.last_issue_time = last_issue
    return st


def build_monitor(site, trackers, elapsed, census=None, notifications=None):
    """A non-running RFMonitor pre-loaded with stats, ready for snapshot()."""
    mon = core.RFMonitor(site=site, enable_log=False, names_path="__none__")
    mon.trackers = {t.serial: t for t in trackers}
    mon.started_at = time.time() - elapsed
    mon.census = census or []
    if notifications:
        from collections import deque
        mon.notifications = deque(notifications, maxlen=200)
    mon.ready.set()
    mon.error = None
    mon.report_path = os.path.join("logs", f"{site}_20260610_141500_report.txt")
    mon.log_dir = "logs"
    return mon


# ---- scenarios -------------------------------------------------------------

def healthy_set():
    mast = [
        make_tracker("LHR-1A2B3C01", "D-MAST-3C04", "left-hip", 0.04, 0.10,
                     119, 86, 0),
        make_tracker("LHR-1A2B3C02", "D-MAST-3C04", "right-hip", 0.02, 0.22,
                     120, 91, 0),
        make_tracker("LHR-1A2B3C03", "D-MAST-3C04", "chest", 0.08, 0.05,
                     119, 78, 0),
    ]
    return mast


def problem_set():
    floor = [
        make_tracker("LHR-9F8E7D01", "D-FLOOR-7A21", "left-foot", 4.82, 0.30,
                     104, 41, 37, longest=2.4,
                     first_issue=time.time() - 280, first_kind="drop",
                     last_issue=time.time() - 6),
        make_tracker("LHR-9F8E7D02", "D-FLOOR-7A21", "right-foot", 3.91, 0.18,
                     108, 47, 29, longest=1.9,
                     first_issue=time.time() - 254, first_kind="drop",
                     last_issue=time.time() - 11),
        make_tracker("LHR-9F8E7D03", "D-FLOOR-7A21", "waist", 5.40, 0.44,
                     99, 33, 44, longest=3.1,
                     first_issue=time.time() - 263, first_kind="drop",
                     last_issue=time.time() - 3),
    ]
    return floor + healthy_set()


def grab_widget(widget, path, pad=0):
    """Save a PNG of one Tk widget/window by its on-screen geometry."""
    widget.update_idletasks()
    widget.update()
    # let the X server settle so the pixels are actually drawn
    for _ in range(8):
        widget.update()
        time.sleep(0.05)
    x = widget.winfo_rootx() - pad
    y = widget.winfo_rooty() - pad
    w = widget.winfo_width() + pad * 2
    h = widget.winfo_height() + pad * 2
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    img.save(path)
    print(f"wrote {path}  ({img.size[0]}x{img.size[1]})")


def shot_start():
    root = tk.Tk()
    root.geometry("1000x680+20+20")
    gui.MonitorApp(root)
    grab_widget(root, os.path.join(OUT, "01-start.png"))
    root.destroy()


def shot_live(name, trackers, banner_problem):
    root = tk.Tk()
    root.geometry("1000x680+20+20")
    app = gui.MonitorApp(root)
    notes = []
    if banner_problem:
        now = time.time()
        notes = [
            (core.datetime.fromtimestamp(now - 263), "alert",
             "DROP: all 3 trackers on dongle D-FLOOR-7A21 lost their radio "
             "link at the same time (waist, left-foot, right-foot)"),
            (core.datetime.fromtimestamp(now - 255), "info",
             "RECONNECTED: radio link restored - left-foot (dongle "
             "D-FLOOR-7A21)"),
            (core.datetime.fromtimestamp(now - 14), "alert",
             "DROP: radio link lost - waist (dongle D-FLOOR-7A21)"),
            (core.datetime.fromtimestamp(now - 3), "info",
             "RECONNECTED: radio link restored - waist (dongle "
             "D-FLOOR-7A21)"),
        ]
    mon = build_monitor("bay3-floor", trackers, elapsed=412,
                        notifications=notes)
    app.mon = mon
    # Reflect the running state the GUI would show after a successful connect.
    app.stop_btn.config(state="normal")
    app.logs_btn.config(state="normal")
    app._set_chip("● MONITORING", "#2e7d32")
    app.status_var.set('Monitoring "bay3-floor". Reports are saved '
                       "automatically when you stop.")
    snap = mon.snapshot()
    app._update_table(snap)
    app._update_banner(snap)
    for ts, kind, msg in notes:
        app._log_line(kind, msg, ts)
    app.elapsed_var.set(f"Running for {snap['elapsed']:.0f}s   |   "
                        f"{len(snap['rows'])} tracker(s)")
    grab_widget(root, os.path.join(OUT, name))
    root.destroy()


def shot_summary():
    # Keep the root mapped: with no window manager (Xvfb) a Toplevel of a
    # withdrawn root never gets mapped and grabs come back black.
    root = tk.Tk()
    root.geometry("1000x680+1280+40")   # push the main window off-screen-ish
    app = gui.MonitorApp(root)
    root.update()
    mon = build_monitor("bay3-floor", problem_set(), elapsed=412)
    app.log_dir = os.path.abspath("logs")
    snap = mon.snapshot()
    app._show_summary_window("bay3-floor", snap)
    win = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][0]
    win.geometry("920x600+20+20")
    win.deiconify()
    win.lift()
    win.update()
    grab_widget(win, os.path.join(OUT, "04-summary.png"))
    root.destroy()


def main():
    shot_start()
    shot_live("02-live-healthy.png", healthy_set(), banner_problem=False)
    shot_live("03-live-problem.png", problem_set(), banner_problem=True)
    shot_summary()


if __name__ == "__main__":
    main()
