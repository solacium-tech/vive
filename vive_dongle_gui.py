#!/usr/bin/env python3
"""
Vive Tracker Dongle / RF Diagnostic Monitor - GUI
=================================================

A friendly window over the same engine as the console tool (vive_rf_core.py).
Trackers are grouped under the dongle they are paired to, colour-coded by
health, with an "identify" panel: unplug a dongle and the panel tells you which
serial (and which trackers) just dropped, so you can physically label it.

No internet required at runtime. Build the .exe off-site with build_exe.bat.
"""

import os
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

import vive_rf_core as core

# Row colours by severity.
COLOURS = {
    core.HEALTHY: ("#1b5e20", "#e8f5e9"),   # fg, bg
    core.WARN:    ("#7a4f01", "#fff3e0"),
    core.CRIT:    ("#b71c1c", "#ffebee"),
}
HEADER_BG = "#263238"
ACCENT = "#0277bd"


class App:
    def __init__(self, root):
        self.root = root
        self.mon = None
        self.notes = []
        root.title("Vive Dongle / RF Diagnostic")
        root.geometry("980x640")
        root.minsize(820, 520)

        self._build_topbar()
        self._build_table()
        self._build_identify_panel()
        self._build_statusbar()
        self._tick()

    # -- layout ------------------------------------------------------------- #
    def _build_topbar(self):
        bar = tk.Frame(self.root, bg=HEADER_BG)
        bar.pack(fill="x")
        tk.Label(bar, text="  Vive Dongle / RF Diagnostic", bg=HEADER_BG,
                 fg="white", font=("Segoe UI", 13, "bold")).pack(side="left",
                                                                 pady=8)
        self.start_btn = tk.Button(bar, text="▶ Start", width=10,
                                   command=self.start, bg="#2e7d32", fg="white",
                                   font=("Segoe UI", 10, "bold"), relief="flat")
        self.stop_btn = tk.Button(bar, text="■ Stop", width=10,
                                  command=self.stop, bg="#c62828", fg="white",
                                  font=("Segoe UI", 10, "bold"), relief="flat",
                                  state="disabled")
        self.stop_btn.pack(side="right", padx=(4, 10), pady=6)
        self.start_btn.pack(side="right", padx=4, pady=6)
        tk.Label(bar, text="Site:", bg=HEADER_BG, fg="white").pack(side="right")
        self.site_var = tk.StringVar(value="site")
        tk.Entry(bar, textvariable=self.site_var, width=20).pack(side="right",
                                                                 padx=6)

    def _build_table(self):
        frame = tk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        cols = ("status", "rf", "drops", "stalls", "optic", "rate", "batt",
                "link")
        heads = {"status": "Health", "rf": "RF loss %", "drops": "Dropouts",
                 "stalls": "Stalls", "optic": "Optical %", "rate": "Rate Hz",
                 "batt": "Battery", "link": "Link"}
        widths = {"status": 190, "rf": 80, "drops": 80, "stalls": 70,
                  "optic": 80, "rate": 80, "batt": 70, "link": 60}
        self.tree = ttk.Treeview(frame, columns=cols, show="tree headings",
                                 height=14)
        self.tree.heading("#0", text="Dongle / Tracker")
        self.tree.column("#0", width=240, anchor="w")
        for col in cols:
            self.tree.heading(col, text=heads[col])
            self.tree.column(col, width=widths[col],
                             anchor="center" if col != "status" else "w")
        for sev, (fg, bg) in COLOURS.items():
            self.tree.tag_configure(sev, foreground=fg, background=bg)
        self.tree.tag_configure("dongle", font=("Segoe UI", 10, "bold"),
                                background="#eceff1")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._dongle_nodes = {}

    def _build_identify_panel(self):
        frame = tk.LabelFrame(self.root, text=" Dongle identification  -  "
                              "unplug a dongle and watch which serial drops ",
                              font=("Segoe UI", 9, "bold"), fg=ACCENT)
        frame.pack(fill="x", padx=8, pady=4)
        self.id_log = scrolledtext.ScrolledText(frame, height=6, wrap="word",
                                                font=("Consolas", 9),
                                                state="disabled")
        self.id_log.pack(fill="x", padx=4, pady=4)
        self.id_log.tag_configure("identify", foreground="#0277bd",
                                  font=("Consolas", 9, "bold"))
        self.id_log.tag_configure("dropout", foreground="#ef6c00")

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg="#eceff1")
        bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Idle. Start SteamVR, power on "
                                       "trackers, then press Start.")
        tk.Label(bar, textvariable=self.status_var, bg="#eceff1",
                 anchor="w").pack(side="left", padx=8, pady=4)
        self.elapsed_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.elapsed_var, bg="#eceff1",
                 anchor="e").pack(side="right", padx=8)

    # -- control ------------------------------------------------------------ #
    def start(self):
        if self.mon:
            return
        self.mon = core.RFMonitor(site=self.site_var.get() or "site")
        self.mon.start()
        self.mon.ready.wait(timeout=10)
        if self.mon.error:
            messagebox.showerror("Cannot start", self.mon.error)
            self.mon = None
            return
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("Monitoring... unplug a dongle to identify it.")

    def stop(self):
        if not self.mon:
            return
        self.mon.stop()
        self.mon.join(timeout=5)
        verdict = self.mon.verdict_text()
        logdir = os.path.abspath(self.mon.log_dir)
        self.mon = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set(f"Stopped. Logs saved in {logdir}")
        self._show_verdict(verdict)

    def _show_verdict(self, text):
        win = tk.Toplevel(self.root)
        win.title("Verdict")
        win.geometry("760x520")
        box = scrolledtext.ScrolledText(win, wrap="none", font=("Consolas", 9))
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.config(state="disabled")

    # -- periodic refresh --------------------------------------------------- #
    def _tick(self):
        if self.mon:
            snap = self.mon.snapshot()
            self._update_table(snap)
            self._update_notes(snap["notifications"])
            self.elapsed_var.set(f"elapsed {snap['elapsed']:.0f}s   "
                                 f"{len(snap['rows'])} tracker(s)")
        self.root.after(500, self._tick)

    def _update_table(self, snap):
        seen = set()
        for dongle in sorted(snap["by_dongle"]):
            a = snap["aggregates"][dongle]
            node = self._dongle_nodes.get(dongle)
            d_text = f"Dongle {dongle}   ({a['count']} tracker(s))"
            d_vals = ("", f"{a['rf_loss_pct']:.2f}", a["dropouts"], a["stalls"],
                      f"{a['optical_loss_pct']:.2f}", "", "", "")
            if node is None:
                node = self.tree.insert("", "end", text=d_text, values=d_vals,
                                        open=True, tags=("dongle",))
                self._dongle_nodes[dongle] = node
            else:
                self.tree.item(node, text=d_text, values=d_vals)
            seen.add(node)

            for r in sorted(snap["by_dongle"][dongle], key=lambda r: r["serial"]):
                cid = f"{dongle}/{r['serial']}"
                batt = f"{r['battery']:.0f}%" if r["battery"] is not None else "?"
                vals = (r["label"], f"{r['rf_loss_pct']:.2f}",
                        r["disconnect_events"], r["stall_events"],
                        f"{r['optical_loss_pct']:.2f}", f"{r['update_hz']:.0f}",
                        batt, "UP" if r["connected"] else "DOWN")
                if self.tree.exists(cid):
                    self.tree.item(cid, values=vals, tags=(r["severity"],))
                else:
                    self.tree.insert(node, "end", iid=cid, text="  " + r["serial"],
                                     values=vals, tags=(r["severity"],))
                seen.add(cid)

    def _update_notes(self, notes):
        if not notes:
            return
        self.id_log.config(state="normal")
        for ts, kind, msg in notes:
            self.id_log.insert("end", f"{ts.strftime('%H:%M:%S')}  {msg}\n",
                               (kind,))
        self.id_log.see("end")
        self.id_log.config(state="disabled")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
