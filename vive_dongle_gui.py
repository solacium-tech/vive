#!/usr/bin/env python3
"""GUI front-end for the Vive tracker RF diagnostics engine.

Shows every tracker grouped under the USB dongle it is paired with and
separates radio (dongle) problems from optical (lighthouse) problems.
Sampling and statistics live in vive_rf_core; this module is display only.

Requires SteamVR running locally. No network access is used.
"""

import os
import subprocess
from datetime import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

import vive_rf_core as core

APP_TITLE = "Vive Tracker Link Monitor"

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
    ("status",  "Status",            180, "w"),
    ("radio",   "Radio loss",         90, "center"),
    ("drops",   "Link drops",         90, "center"),
    ("optic",   "Lighthouse loss",   110, "center"),
    ("rate",    "Updates/sec",        90, "center"),
    ("batt",    "Battery",            70, "center"),
    ("link",    "Connected",          90, "center"),
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
        root.title(APP_TITLE)
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

    def _build_legend(self):
        frame = tk.Frame(self.root)
        frame.pack(fill="x", padx=8, pady=(0, 4))
        for sev, text in LEGEND:
            fg, bg = SEV_STYLE[sev]
            tk.Label(frame, text="  " + text + "  ", fg=fg, bg=bg,
                     font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))
        tk.Label(frame, text=LEGEND_HINT, fg="#546e7a",
                 font=("Segoe UI", 9)).pack(side="left", padx=4)

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
        self.mon = core.RFMonitor(site=site)
        self.mon.start()
        self.mon.ready.wait(timeout=10)
        if self.mon.error:
            messagebox.showerror(APP_TITLE, self.mon.error)
            self.mon = None
            return
        self._clear_table()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.logs_btn.config(state="normal")
        self.status_var.set(f"Monitoring \"{site}\". Reports are saved "
                            f"automatically when you stop.")
        self._log_line("info", f"Monitoring started for location: {site}")

    def stop(self):
        if not self.mon:
            return
        self.mon.stop()
        self.mon.join(timeout=5)
        summary = self.mon.verdict_text()
        self.log_dir = os.path.abspath(self.mon.log_dir)
        self.mon = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._set_banner("idle", "Monitoring stopped. Report saved.")
        self.status_var.set(f"Report files saved in: {self.log_dir}")
        self._log_line("info", "Monitoring stopped, report saved.")
        self._show_summary(summary)

    def _on_close(self):
        if self.mon:
            self.mon.stop()
            self.mon.join(timeout=5)
        self.root.destroy()

    def _open_logs(self):
        path = getattr(self, "log_dir", os.path.abspath("logs"))
        if not os.path.isdir(path):
            messagebox.showinfo(APP_TITLE, "No reports have been saved yet.")
            return
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

    def _show_summary(self, text):
        win = tk.Toplevel(self.root)
        win.title("Run summary")
        win.geometry("780x540")
        box = scrolledtext.ScrolledText(win, wrap="none", font=("Consolas", 9))
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.config(state="disabled")

    # ---- periodic refresh ----

    def _tick(self):
        if self.mon:
            snap = self.mon.snapshot()
            self._update_table(snap)
            self._update_banner(snap)
            for ts, kind, msg in snap["notifications"]:
                self._log_line(kind, msg, ts)
            self.elapsed_var.set(f"Running for {snap['elapsed']:.0f}s   |   "
                                 f"{len(snap['rows'])} tracker(s)")
        self.root.after(500, self._tick)

    def _update_banner(self, snap):
        if not snap["rows"]:
            self._set_banner(core.WARN,
                             "Waiting for trackers... check they are powered "
                             "on and paired in SteamVR.")
            return
        worst_key = core.HEALTHY
        worst_dongle = None
        worst_loss = 0.0
        for dongle, a in snap["aggregates"].items():
            sev = core.severity(a["rf_loss_pct"],
                                core.RF_LOSS_WARN, core.RF_LOSS_CRIT)
            if sev == core.CRIT or (sev == core.WARN and worst_key != core.CRIT):
                if a["rf_loss_pct"] > worst_loss:
                    worst_key = sev
                    worst_dongle = dongle
                    worst_loss = a["rf_loss_pct"]
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

    def _update_table(self, snap):
        for dongle in sorted(snap["by_dongle"]):
            a = snap["aggregates"][dongle]
            d_text = f"Dongle {dongle}   ({a['count']} tracker(s))"
            d_vals = ("", f"{a['rf_loss_pct']:.2f}%", a["dropouts"],
                      f"{a['optical_loss_pct']:.2f}%", "", "", "")
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
                vals = (r["label"],
                        f"{r['rf_loss_pct']:.2f}%",
                        r["disconnect_events"],
                        f"{r['optical_loss_pct']:.2f}%",
                        f"{r['update_hz']:.0f}",
                        batt,
                        "Yes" if r["connected"] else "NO")
                if self.tree.exists(iid):
                    self.tree.item(iid, values=vals, tags=(r["severity"],))
                else:
                    self.tree.insert(node, "end", iid=iid,
                                     text="    " + r["serial"],
                                     values=vals, tags=(r["severity"],))

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
