#!/usr/bin/env python3
"""GUI front-end for the Vive tracker RF diagnostics engine.

Shows every tracker grouped under the USB dongle it is paired with and
separates radio (dongle) problems from optical (lighthouse) problems.
Sampling and statistics live in vive_rf_core; this module is display only.

Requires SteamVR running locally. No network access is used.
"""

import glob
import os
import subprocess
from datetime import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

import vive_rf_core as core

APP_TITLE = "Vive Tracker Link Monitor"
APP_VERSION = "2026-06-10k"   # shown in the title bar to confirm the build

# fg / bg per severity
SEV_STYLE = {
    core.HEALTHY: ("#1b5e20", "#e8f5e9"),
    core.WARN:    ("#7a4f01", "#fff3e0"),
    core.CRIT:    ("#b71c1c", "#ffebee"),
}
BANNER_STYLE = {
    "idle":         ("#37474f", "#cfd8dc"),
    core.HEALTHY:   ("#ffffff", "#2e7d32"),
    core.WARN:      ("#3e2723", "#ffb300"),
    core.CRIT:      ("#ffffff", "#c62828"),
}
HEADER_BG = "#263238"

COLUMNS = (
    ("radio",   "Radio link (to dongle)",        180, "w"),
    ("drops",   "Radio drops",                    90, "center"),
    ("optic",   "Lighthouse (line of sight)",    180, "w"),
    ("batt",    "Battery",                        75, "center"),
    ("link",    "Connected",                      90, "center"),
)

LEGEND = (
    (core.HEALTHY, "Green = healthy"),
    (core.WARN, "Amber = degraded, keep an eye on it"),
    (core.CRIT, "Red = failing, needs attention"),
)
LEGEND_HINT = ("\"Radio\" problems point at the dongle or its placement. "
               "\"Lighthouse\" problems point at line of sight to the base "
               "stations.")


class MonitorApp:
    def __init__(self, root):
        self.root = root
        self.mon = None
        self._dongle_nodes = {}
        self._census_node = None
        root.title(f"{APP_TITLE}  -  build {APP_VERSION}")
        root.geometry("1000x680")
        root.minsize(860, 560)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_topbar()
        self._build_banner()
        self._build_table()
        self._build_legend()
        self._build_event_log()
        self._build_statusbar()
        self._tick()

    # ---- layout ----

    def _build_topbar(self):
        bar = tk.Frame(self.root, bg=HEADER_BG)
        bar.pack(fill="x")
        tk.Label(bar, text="  " + APP_TITLE, bg=HEADER_BG, fg="white",
                 font=("Segoe UI", 13, "bold")).pack(side="left", pady=8)

        # Persistent state chip so it's always obvious whether a run is live.
        self.chip = tk.Label(bar, text="  IDLE  ", bg="#607d8b", fg="white",
                             font=("Segoe UI", 10, "bold"), padx=6)
        self.chip.pack(side="left", padx=10, pady=8)

        self.stop_btn = tk.Button(
            bar, text="Stop and save report", command=self.stop,
            bg="#c62828", fg="white", font=("Segoe UI", 10, "bold"),
            relief="flat", padx=12, state="disabled")
        self.stop_btn.pack(side="right", padx=(4, 10), pady=6)

        self.start_btn = tk.Button(
            bar, text="Start monitoring", command=self.start,
            bg="#2e7d32", fg="white", font=("Segoe UI", 10, "bold"),
            relief="flat", padx=12)
        self.start_btn.pack(side="right", padx=4, pady=6)

        self.site_var = tk.StringVar(value="")
        tk.Entry(bar, textvariable=self.site_var, width=22).pack(
            side="right", padx=6)
        tk.Label(bar, text="Location label (used in report file names):",
                 bg=HEADER_BG, fg="#b0bec5").pack(side="right")

    def _build_banner(self):
        self.banner = tk.Label(
            self.root, text="", font=("Segoe UI", 12, "bold"),
            anchor="center", pady=10)
        self.banner.pack(fill="x")
        self._set_banner("idle",
                         "Not monitoring.  Start SteamVR, power on the "
                         "trackers, then press \"Start monitoring\".")

    def _set_banner(self, key, text):
        fg, bg = BANNER_STYLE[key]
        self.banner.config(text=text, fg=fg, bg=bg)

    def _set_chip(self, text, bg):
        self.chip.config(text=f"  {text}  ", bg=bg)

    def _build_table(self):
        frame = tk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=8, pady=(8, 2))

        cols = tuple(c[0] for c in COLUMNS)
        self.tree = ttk.Treeview(frame, columns=cols, show="tree headings",
                                 height=14)
        self.tree.heading("#0", text="Dongle  /  Tracker")
        self.tree.column("#0", width=250, anchor="w")
        for key, head, width, anchor in COLUMNS:
            self.tree.heading(key, text=head)
            self.tree.column(key, width=width, anchor=anchor)
        for sev, (fg, bg) in SEV_STYLE.items():
            self.tree.tag_configure(sev, foreground=fg, background=bg)
        self.tree.tag_configure("dongle", font=("Segoe UI", 10, "bold"),
                                background="#eceff1")

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Right-click a tracker row to hide devices that aren't part of this
        # run (e.g. controllers used only for room setup).
        self._tree_menu = tk.Menu(self.root, tearoff=0)
        self.tree.bind("<Button-3>", self._on_tree_menu)
        self.tree.bind("<Button-2>", self._on_tree_menu)  # macOS

    def _build_legend(self):
        frame = tk.Frame(self.root)
        frame.pack(fill="x", padx=8, pady=(0, 4))
        row = tk.Frame(frame)
        row.pack(fill="x")
        for sev, text in LEGEND:
            fg, bg = SEV_STYLE[sev]
            tk.Label(row, text="  " + text + "  ", fg=fg, bg=bg,
                     font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))
        tk.Label(row, text=LEGEND_HINT, fg="#546e7a",
                 font=("Segoe UI", 9)).pack(side="left", padx=4)
        tk.Label(frame, text=f"Colours and % show roughly the last "
                 f"{core.RECENT_WINDOW_S:.0f}s (live); the saved report "
                 f"covers the whole session.   Right-click a row to hide a "
                 f"device that isn't part of this run.", fg="#90a4ae",
                 font=("Segoe UI", 8)).pack(anchor="w", padx=2)

        # Shown only when devices are hidden; click to bring them all back.
        self.hidden_var = tk.StringVar(value="")
        self.hidden_btn = tk.Button(
            frame, textvariable=self.hidden_var, relief="flat", fg="#0277bd",
            cursor="hand2", font=("Segoe UI", 9), bd=0,
            command=self._restore_hidden, state="disabled")
        self.hidden_btn.pack(anchor="w", padx=2)

    def _build_event_log(self):
        frame = tk.LabelFrame(self.root, text=" Event log ",
                              font=("Segoe UI", 9, "bold"), fg="#37474f")
        frame.pack(fill="x", padx=8, pady=4)
        self.event_log = scrolledtext.ScrolledText(
            frame, height=5, wrap="word", font=("Consolas", 9),
            state="disabled")
        self.event_log.pack(fill="x", padx=4, pady=4)
        self.event_log.tag_configure("alert", foreground="#c62828")
        self.event_log.tag_configure("info", foreground="#37474f")

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg="#eceff1")
        bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(bar, textvariable=self.status_var, bg="#eceff1",
                 anchor="w").pack(side="left", padx=8, pady=4)
        self.logs_btn = tk.Button(bar, text="Open reports folder",
                                  command=self._open_logs, relief="flat",
                                  fg="#0277bd", bg="#eceff1", cursor="hand2",
                                  state="disabled")
        self.logs_btn.pack(side="right", padx=8)
        self.open_report_btn = tk.Button(bar, text="Open report",
                                         command=self._open_report,
                                         relief="flat", fg="#0277bd",
                                         bg="#eceff1", cursor="hand2",
                                         state="disabled")
        self.open_report_btn.pack(side="right", padx=2)
        self.elapsed_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.elapsed_var, bg="#eceff1",
                 anchor="e").pack(side="right", padx=8)

    # ---- control ----

    def start(self):
        if self.mon:
            return
        site = self.site_var.get().strip()
        if not site:
            messagebox.showinfo(
                APP_TITLE,
                "Enter a location label first (for example \"bay3-floor\").\n"
                "It is used to name the report files so runs can be compared.")
            return
        # Connecting to SteamVR can take a few seconds (and may retry), so do
        # it on the monitor's own thread and poll for the result from the Tk
        # event loop. Blocking here would freeze the window ("Not responding").
        self.mon = core.RFMonitor(site=site)
        self.mon.start()
        self._clear_table()
        self.start_btn.config(state="disabled")
        self._set_chip("CONNECTING", "#ff9800")
        self._set_banner(core.WARN, "Connecting to SteamVR...")
        self.status_var.set("Connecting to SteamVR...")
        self.root.after(150, self._await_ready)

    def _await_ready(self):
        if not self.mon:
            return
        if not self.mon.ready.is_set():
            self.root.after(150, self._await_ready)
            return
        if self.mon.error:
            messagebox.showerror(APP_TITLE, self.mon.error)
            self.mon = None
            self.start_btn.config(state="normal")
            self._set_chip("IDLE", "#607d8b")
            self._set_banner("idle", "Not monitoring. Start SteamVR, power "
                             "on the trackers, then press \"Start "
                             "monitoring\".")
            self.status_var.set("Ready.")
            return
        site = self.mon.site
        self.stop_btn.config(state="normal")
        self.logs_btn.config(state="normal")
        self._set_chip("● MONITORING", "#2e7d32")
        self.status_var.set(f"Monitoring \"{site}\". Reports are saved "
                            f"automatically when you stop.")
        self._log_line("info", f"Monitoring started for location: {site}")

    def stop(self):
        if not self.mon:
            return
        site = self.mon.site
        # 1) Stop sampling and capture what we can. Anything here that fails
        #    must NOT prevent the on-screen confirmation below.
        snap = None
        try:
            self.mon.stop()
            self.mon.join(timeout=5)
            self.log_dir = os.path.abspath(self.mon.log_dir)
            # Store an absolute path: a relative one can fail os.path.isfile()
            # later (that was why "Open report" claimed no report existed even
            # though the file was sitting in the reports folder).
            rp = getattr(self.mon, "report_path", None)
            self.report_path = os.path.abspath(rp) if rp else None
            snap = self.mon.snapshot()
        except Exception as exc:
            self._log_line("alert", f"Error while stopping: {exc}")
        finally:
            self.mon = None

        # 2) Always give clear, immediate feedback (and force a repaint so it
        #    shows before the modal dialog opens).
        report = os.path.basename(self.report_path) if getattr(
            self, "report_path", None) else "(none)"
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.open_report_btn.config(state="normal")
        self._set_chip("STOPPED - SAVED", "#2e7d32")
        self._set_banner(core.HEALTHY,
                         f"✓ MONITORING STOPPED - report saved: {report}")
        self.status_var.set(f"Report saved in: {getattr(self, 'log_dir', '')}")
        self._log_line("info", f"Monitoring stopped. Report saved: {report}")
        self.root.update_idletasks()

        # 3) Modal confirmation - guaranteed to appear and require an OK.
        messagebox.showinfo(
            "Monitoring stopped - report saved",
            f"Monitoring has stopped and the report has been saved.\n\n"
            f"File:  {report}\n"
            f"Folder:  {getattr(self, 'log_dir', '')}\n\n"
            f"Use \"Open report\" or \"Open reports folder\" to view it.")

        # 4) Detailed summary window (best-effort; never blocks the feedback).
        if snap is not None:
            try:
                self._show_summary_window(site, snap)
            except Exception as exc:
                messagebox.showerror(
                    APP_TITLE,
                    f"The report was saved, but the summary view could not be "
                    f"shown:\n{exc}")

    # Colours for the summary window, keyed by report line tag.
    SUMMARY_TAGS = {
        "title":          {"font": ("Segoe UI", 14, "bold")},
        "meta":           {"foreground": "#546e7a"},
        "banner-healthy": {"background": "#2e7d32", "foreground": "#ffffff",
                           "font": ("Segoe UI", 11, "bold")},
        "banner-warn":    {"background": "#ffb300", "foreground": "#3e2723",
                           "font": ("Segoe UI", 11, "bold")},
        "banner-crit":    {"background": "#c62828", "foreground": "#ffffff",
                           "font": ("Segoe UI", 11, "bold")},
        "h2":             {"font": ("Segoe UI", 11, "bold")},
        "note":           {"foreground": "#546e7a"},
        "header":         {"background": "#cfd8dc",
                           "font": ("Consolas", 10, "bold")},
        "row-healthy":    {"background": "#e8f5e9", "foreground": "#1b5e20"},
        "row-warn":       {"background": "#fff3e0", "foreground": "#7a4f01"},
        "row-crit":       {"background": "#ffebee", "foreground": "#b71c1c"},
        "advice":         {"background": "#e3f2fd", "foreground": "#01579b"},
        "plain":          {},
    }

    def _show_summary_window(self, site, snap):
        win = tk.Toplevel(self.root)
        win.title(f"Run summary - {site}  (report saved)")
        win.geometry("920x580")
        head = tk.Label(win, text="✓  Monitoring stopped - report saved",
                        bg="#2e7d32", fg="white", anchor="w",
                        font=("Segoe UI", 12, "bold"), padx=12, pady=8)
        head.pack(fill="x")
        path = tk.Label(win, text=f"Saved in:  {self.log_dir}", anchor="w",
                        fg="#37474f", padx=12, pady=4)
        path.pack(fill="x")
        body = tk.Frame(win)
        body.pack(fill="both", expand=True)
        text = tk.Text(body, font=("Consolas", 10), wrap="none",
                       padx=12, pady=10, relief="flat")
        vsb = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscroll=vsb.set)
        text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for tag, opts in self.SUMMARY_TAGS.items():
            if opts:
                text.tag_configure(tag, **opts)
        for tag, line in core.build_report_lines(site, snap):
            text.insert("end", line + "\n", (tag,))
        text.config(state="disabled")
        tk.Button(win, text="Open reports folder", command=self._open_logs,
                  relief="flat", fg="#0277bd", cursor="hand2").pack(pady=6)
        # Make sure the operator actually notices it.
        win.transient(self.root)
        win.lift()
        win.focus_force()
        win.attributes("-topmost", True)
        win.after(700, lambda: win.attributes("-topmost", False))

    @staticmethod
    def _open_file(path):
        try:
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    def _on_close(self):
        # Stop sampling and let the monitor flush its report/logs.
        try:
            if self.mon:
                self.mon.stop()
                self.mon.join(timeout=5)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        # The SteamVR/OpenVR runtime keeps native threads alive, which can stop
        # the process from exiting on its own; force a clean exit so the window
        # closes without needing Task Manager.
        os._exit(0)

    def _open_logs(self):
        path = getattr(self, "log_dir", os.path.abspath("logs"))
        if not os.path.isdir(path):
            messagebox.showinfo(APP_TITLE, "No reports have been saved yet.")
            return
        self._open_file(path)

    def _newest_report(self):
        """Most recent *_report.txt in the reports folder, or None."""
        folder = getattr(self, "log_dir", os.path.abspath("logs"))
        try:
            reports = glob.glob(os.path.join(folder, "*_report.txt"))
            return max(reports, key=os.path.getmtime) if reports else None
        except Exception:
            return None

    def _open_report(self):
        report = getattr(self, "report_path", None)
        # Fall back to the newest report on disk if the tracked path is missing
        # (e.g. it was written a moment after Stop, or the path was relative).
        if not (report and os.path.isfile(report)):
            report = self._newest_report()
        if report and os.path.isfile(report):
            self._open_file(os.path.abspath(report))
        else:
            messagebox.showinfo(APP_TITLE, "No report has been saved yet.")

    # ---- periodic refresh ----

    def _tick(self):
        # Only refresh once the monitor has connected; while it is still
        # connecting, _await_ready owns the banner/status.
        if self.mon and self.mon.ready.is_set() and not self.mon.error:
            snap = self.mon.snapshot()
            self._update_table(snap)
            self._update_banner(snap)
            self._refresh_hidden_indicator(snap)
            # Reflect the paused ("not in use") state in the run-state chip.
            if snap.get("paused"):
                self._set_chip("❚❚ PAUSED (not in use)", "#607d8b")
            else:
                self._set_chip("● MONITORING", "#2e7d32")
            for ts, kind, msg in snap["notifications"]:
                self._log_line(kind, msg, ts)
            self.elapsed_var.set(f"Running for {snap['elapsed']:.0f}s   |   "
                                 f"{len(snap['rows'])} tracker(s)")
        self.root.after(500, self._tick)

    def _update_banner(self, snap):
        if snap.get("paused"):
            reason = snap.get("paused_reason") or "not in use"
            self._set_banner("idle",
                             f"NOT IN USE ({reason}). Monitoring paused - this "
                             f"time is not counted. Put the headset on or power "
                             f"the trackers to resume.")
            return
        if not snap["rows"]:
            counts = snap.get("class_counts", {})
            if counts:
                seen = ", ".join(f"{n} {name}{'s' if n != 1 else ''}"
                                 for name, n in sorted(counts.items()))
                self._set_banner(core.WARN,
                                 f"Connected. SteamVR reports: {seen}. "
                                 f"No trackers detected yet - power on the "
                                 f"Vive trackers (not just the headset).")
            else:
                self._set_banner(core.WARN,
                                 "Connected to SteamVR. Waiting for devices...")
            self._update_devices_panel(snap.get("census", []))
            return
        worst_key = core.HEALTHY
        worst_dongle = None
        worst_loss = 0.0
        for dongle, a in snap["aggregates"].items():
            loss = a["recent_rf_loss_pct"]   # live banner = current health
            sev = core.severity(loss, core.RF_LOSS_WARN, core.RF_LOSS_CRIT)
            if sev == core.CRIT or (sev == core.WARN and worst_key != core.CRIT):
                if loss > worst_loss:
                    worst_key = sev
                    worst_dongle = dongle
                    worst_loss = loss
        if worst_key == core.CRIT:
            self._set_banner(core.CRIT,
                             f"PROBLEM: dongle {worst_dongle} is losing its "
                             f"radio link ({worst_loss:.1f}% of the time). "
                             f"Check that dongle's position.")
        elif worst_key == core.WARN:
            self._set_banner(core.WARN,
                             f"Warning: dongle {worst_dongle} shows a weak "
                             f"radio link ({worst_loss:.1f}% loss).")
        else:
            self._set_banner(core.HEALTHY,
                             "All radio links healthy. Monitoring...")

    def _clear_table(self):
        for node in self.tree.get_children(""):
            self.tree.delete(node)
        self._dongle_nodes = {}
        self._census_node = None

    def _update_devices_panel(self, census):
        """While no trackers are grouped yet, list every device SteamVR sees
        so it is obvious what is and isn't detected (and its dongle value)."""
        if self.tree.get_children("") and self._census_node is None:
            return  # tracker rows are present; don't draw the census
        if self._census_node is None:
            self._census_node = self.tree.insert(
                "", "end", text="Devices SteamVR can see (no trackers yet)",
                values=("", "", "", "", "", ""), open=True,
                tags=("dongle",))
        existing = set(self.tree.get_children(self._census_node))
        wanted = set()
        for d in census:
            iid = f"dev{d['index']}"
            wanted.add(iid)
            dongle = d["dongle"] or "(none)"
            vals = (d["class"], "", f"dongle: {dongle}", "", "",
                    "Up" if d["connected"] else "DOWN")
            text = "    " + (d["serial"] or d["model"] or f"index {d['index']}")
            if self.tree.exists(iid):
                self.tree.item(iid, text=text, values=vals)
            else:
                self.tree.insert(self._census_node, "end", iid=iid, text=text,
                                 values=vals, tags=("warn",))
        for iid in existing - wanted:
            self.tree.delete(iid)

    def _update_table(self, snap):
        if self._census_node is not None and snap["by_dongle"]:
            self.tree.delete(self._census_node)
            self._census_node = None
        for dongle in sorted(snap["by_dongle"]):
            a = snap["aggregates"][dongle]
            # Live view uses the recent window so it tracks current health.
            rf_word, _ = core.rf_verdict(a["recent_rf_loss_pct"])
            op_word, _ = core.optical_verdict(a["recent_optical_loss_pct"])
            d_text = f"Dongle {dongle}   ({a['count']} tracker(s))"
            d_vals = (f"{rf_word}  -  {a['recent_rf_loss_pct']:.2f}% lost",
                      a["dropouts"],
                      f"{op_word}  -  {a['recent_optical_loss_pct']:.2f}% lost",
                      "", "")
            node = self._dongle_nodes.get(dongle)
            if node is None:
                node = self.tree.insert("", "end", text=d_text, values=d_vals,
                                        open=True, tags=("dongle",))
                self._dongle_nodes[dongle] = node
            else:
                self.tree.item(node, text=d_text, values=d_vals)

            for r in sorted(snap["by_dongle"][dongle],
                            key=lambda r: r["serial"]):
                iid = f"{dongle}/{r['serial']}"
                batt = (f"{r['battery']:.0f}%" if r["battery"] is not None
                        else "?")
                # Live view: recent-window verdicts and colour so a recovered
                # link clears within ~30s instead of staying red all session.
                rf_word, _ = core.rf_verdict(r["recent_rf_loss_pct"])
                op_word, _ = core.optical_verdict(r["recent_optical_loss_pct"])
                vals = (f"{rf_word}  -  {r['recent_rf_loss_pct']:.2f}% lost",
                        r["disconnect_events"],
                        f"{op_word}  -  {r['recent_optical_loss_pct']:.2f}% lost",
                        batt,
                        "Up" if r["connected"] else "DOWN")
                name = r.get("name")
                label = (f"{name}  ({r['serial']})" if name
                         else r["serial"])
                if self.tree.exists(iid):
                    self.tree.item(iid, text="    " + label, values=vals,
                                   tags=(r["recent_severity"],))
                else:
                    self.tree.insert(node, "end", iid=iid,
                                     text="    " + label,
                                     values=vals, tags=(r["recent_severity"],))

    # ---- hiding irrelevant devices ----

    def _on_tree_menu(self, event):
        iid = self.tree.identify_row(event.y)
        # Only tracker rows have a "dongle/serial" iid; dongle headers don't.
        if not iid or "/" not in iid:
            return
        serial = iid.split("/", 1)[1]
        label = self.tree.item(iid, "text").strip()
        self._tree_menu.delete(0, "end")
        self._tree_menu.add_command(
            label=f"Hide \"{label}\"  (not part of this run)",
            command=lambda s=serial, i=iid: self._hide_device(s, i))
        try:
            self._tree_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._tree_menu.grab_release()

    def _hide_device(self, serial, iid):
        if not self.mon:
            return
        self.mon.dismiss(serial)
        if self.tree.exists(iid):
            parent = self.tree.parent(iid)
            self.tree.delete(iid)
            # Drop the dongle header too if it now has no visible trackers.
            if parent and not self.tree.get_children(parent):
                self.tree.delete(parent)
                for d, node in list(self._dongle_nodes.items()):
                    if node == parent:
                        self._dongle_nodes.pop(d, None)
        self._log_line("info",
                       f"Device hidden and excluded from the data: {serial}")
        self._refresh_hidden_indicator()

    def _restore_hidden(self):
        if self.mon:
            self.mon.restore_all()
            self._log_line("info", "Restored all hidden devices.")
        self._refresh_hidden_indicator()

    def _refresh_hidden_indicator(self, snap=None):
        if snap is not None:
            n = len(snap.get("dismissed", []))
        elif self.mon:
            n = len(self.mon.dismissed)
        else:
            n = 0
        if n:
            self.hidden_var.set(
                f"Hidden devices: {n}  -  click to show them again")
            self.hidden_btn.config(state="normal")
        else:
            self.hidden_var.set("")
            self.hidden_btn.config(state="disabled")

    def _log_line(self, kind, msg, ts=None):
        stamp = (ts or datetime.now()).strftime("%H:%M:%S")
        self.event_log.config(state="normal")
        self.event_log.insert("end", f"{stamp}  {msg}\n", (kind,))
        self.event_log.see("end")
        self.event_log.config(state="disabled")


def main():
    root = tk.Tk()
    MonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
