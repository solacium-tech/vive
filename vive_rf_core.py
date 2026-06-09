#!/usr/bin/env python3
"""
Shared core for the Vive dongle / RF diagnostic tools.

Holds the OpenVR sampling logic, per-tracker statistics, per-dongle
aggregation, CSV/event logging, and the "identify dongle by unplugging it"
detector. Both the console tool (vive_dongle_monitor.py) and the GUI
(vive_dongle_gui.py) import from here so they always agree.

Key idea
--------
Each tracker reports the serial of the dongle it is paired with via
Prop_ConnectedWirelessDongle_String, so we always know *which dongle a
dropping tracker belongs to*. The dongles are not physically labelled, so
RFMonitor also watches for whole-dongle disconnects (every tracker on a
dongle losing bDeviceIsConnected in the same instant) which is exactly what
happens when you unplug that dongle - letting you label them physically.
"""

import csv
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

try:
    import openvr
except ImportError:  # pragma: no cover - openvr only present at runtime
    openvr = None


# --------------------------------------------------------------------------- #
# Tunables                                                                     #
# --------------------------------------------------------------------------- #
POLL_HZ = 250            # how often the sampling thread reads poses
CSV_PERIOD_S = 1.0       # how often a CSV time-series row is appended
BATTERY_PERIOD_S = 5.0   # battery telemetry changes slowly
STALL_MS = 60            # connected but no new packet for this long = "stall"

# Verdict thresholds (percent).
RF_LOSS_WARN = 0.5       # % of samples with the radio link DOWN
RF_LOSS_CRIT = 2.0
OPTICAL_WARN = 2.0       # % of connected samples out of lighthouse range
OPTICAL_CRIT = 8.0

# Severity keys returned by classify()/severity() - tools map these to colours.
HEALTHY = "healthy"
WARN = "warn"
CRIT = "crit"


def severity(value, warn, crit):
    if value >= crit:
        return CRIT
    if value >= warn:
        return WARN
    return HEALTHY


def tracking_result_name(tr):
    if openvr is None:
        return str(tr)
    names = {
        openvr.TrackingResult_Uninitialized: "Uninitialized",
        openvr.TrackingResult_Calibrating_InProgress: "Calibrating",
        openvr.TrackingResult_Calibrating_OutOfRange: "CalibOutOfRange",
        openvr.TrackingResult_Running_OK: "OK",
        openvr.TrackingResult_Running_OutOfRange: "OutOfRange",
    }
    return names.get(tr, str(tr))


# --------------------------------------------------------------------------- #
# Per-tracker statistics                                                       #
# --------------------------------------------------------------------------- #
class TrackerStat:
    def __init__(self, serial, model, dongle):
        self.serial = serial
        self.model = model
        self.dongle = dongle or "UNKNOWN"
        self.battery = None

        self.samples = 0
        self.connected_samples = 0
        self.pose_valid_samples = 0
        self.out_of_range_samples = 0   # connected but optically out of range
        self.rf_loss_samples = 0        # radio link down

        # Radio-starvation (input packet continuity) detector.
        self.last_packet = None
        self.last_packet_time = None
        self.packet_increments = 0
        self.stall_events = 0
        self._in_stall = False

        # Hard connect/disconnect (timestamped) tracking.
        self.was_connected = True
        self.disconnect_events = 0
        self.in_disconnect = False
        self.disconnect_start = None
        self.total_disconnect_s = 0.0
        self.longest_disconnect_s = 0.0
        self.connected_elapsed_s = 0.0

        self.last_connected = True       # for whole-dongle unplug detection
        self.first_seen = time.time()

    def update(self, pose, packet_num, now, dt):
        """Fold one sample in. Returns True on a connected->disconnected edge."""
        self.samples += 1
        connected = bool(pose.bDeviceIsConnected)
        dropped_edge = False

        if connected:
            self.connected_samples += 1
            self.connected_elapsed_s += dt
            if pose.bPoseIsValid:
                self.pose_valid_samples += 1
            if pose.eTrackingResult == openvr.TrackingResult_Running_OutOfRange:
                self.out_of_range_samples += 1

            if packet_num is not None:
                if self.last_packet is None:
                    self.last_packet = packet_num
                    self.last_packet_time = now
                elif packet_num != self.last_packet:
                    self.packet_increments += 1
                    self.last_packet = packet_num
                    self.last_packet_time = now
                    self._in_stall = False
                elif (self.last_packet_time is not None
                      and (now - self.last_packet_time) * 1000.0 > STALL_MS
                      and not self._in_stall):
                    self.stall_events += 1
                    self._in_stall = True
        else:
            self.rf_loss_samples += 1

        if not connected and self.was_connected:
            self.in_disconnect = True
            self.disconnect_start = now
            self.disconnect_events += 1
            dropped_edge = True
        elif connected and not self.was_connected:
            if self.disconnect_start is not None:
                dur = now - self.disconnect_start
                self.total_disconnect_s += dur
                self.longest_disconnect_s = max(self.longest_disconnect_s, dur)
            self.in_disconnect = False
            self.disconnect_start = None
        self.was_connected = connected
        self.last_connected = connected
        return dropped_edge

    def finalize(self, now):
        if self.in_disconnect and self.disconnect_start is not None:
            dur = now - self.disconnect_start
            self.total_disconnect_s += dur
            self.longest_disconnect_s = max(self.longest_disconnect_s, dur)

    @property
    def rf_loss_pct(self):
        return 100.0 * self.rf_loss_samples / self.samples if self.samples else 0.0

    @property
    def optical_loss_pct(self):
        c = self.connected_samples
        return 100.0 * self.out_of_range_samples / c if c else 0.0

    @property
    def update_hz(self):
        return (self.packet_increments / self.connected_elapsed_s
                if self.connected_elapsed_s > 0.2 else 0.0)

    def classify(self):
        """Return (label, severity_key, family) for the dominant problem."""
        rf = self.rf_loss_pct
        opt = self.optical_loss_pct
        if rf >= RF_LOSS_CRIT:
            return "RF / DONGLE FAILURE", CRIT, "rf"
        if rf >= RF_LOSS_WARN or self.stall_events > 0:
            return "RF / DONGLE WEAK", WARN, "rf"
        if opt >= OPTICAL_CRIT:
            return "LIGHTHOUSE BLOCKED", CRIT, "optic"
        if opt >= OPTICAL_WARN:
            return "LIGHTHOUSE MARGINAL", WARN, "optic"
        return "HEALTHY", HEALTHY, "ok"

    def to_row(self):
        label, sev, family = self.classify()
        return {
            "serial": self.serial,
            "model": self.model,
            "dongle": self.dongle,
            "label": label,
            "severity": sev,
            "family": family,
            "rf_loss_pct": self.rf_loss_pct,
            "optical_loss_pct": self.optical_loss_pct,
            "update_hz": self.update_hz,
            "battery": self.battery,
            "disconnect_events": self.disconnect_events,
            "stall_events": self.stall_events,
            "longest_disconnect_s": self.longest_disconnect_s,
            "connected": self.was_connected,
        }


# --------------------------------------------------------------------------- #
# Defensive property reads                                                     #
# --------------------------------------------------------------------------- #
def _get_str(vr, idx, prop):
    try:
        return vr.getStringTrackedDeviceProperty(idx, prop)
    except Exception:
        return ""


def _get_float(vr, idx, prop):
    try:
        return vr.getFloatTrackedDeviceProperty(idx, prop)
    except Exception:
        return None


def _is_tracked_class(cls):
    return cls in (openvr.TrackedDeviceClass_GenericTracker,
                   openvr.TrackedDeviceClass_Controller)


# --------------------------------------------------------------------------- #
# The monitor (runs OpenVR sampling on its own thread)                         #
# --------------------------------------------------------------------------- #
class RFMonitor(threading.Thread):
    """
    Samples SteamVR on a background thread. Consumers (console loop or GUI
    timer) call snapshot() to read a thread-safe copy and drain notifications.
    """

    def __init__(self, site="site", log_dir="logs", enable_log=True):
        super().__init__(daemon=True)
        self.site = site
        self.log_dir = log_dir
        self.enable_log = enable_log

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.ready = threading.Event()
        self.error = None

        self.trackers = {}            # serial -> TrackerStat
        self.index_serial = {}        # device index -> serial
        self.notifications = deque(maxlen=200)
        self.started_at = None
        self.stopped_at = None

        self._vr = None
        self._csv_file = None
        self._csv_writer = None
        self._event_file = None

    # -- logging helpers ---------------------------------------------------- #
    def _log_event(self, msg):
        line = f"{datetime.now().isoformat()}  {msg}"
        if self._event_file:
            self._event_file.write(line + "\n")
            self._event_file.flush()

    def _notify(self, kind, msg):
        """Queue a user-facing notification (also written to the event log)."""
        with self._lock:
            self.notifications.append((datetime.now(), kind, msg))
        self._log_event(f"{kind.upper()}  {msg}")

    # -- lifecycle ---------------------------------------------------------- #
    def stop(self):
        self._stop.set()

    def run(self):
        if openvr is None:
            self.error = "The 'openvr' package is not available."
            self.ready.set()
            return
        try:
            self._vr = openvr.init(openvr.VRApplication_Background)
        except Exception:
            self.error = ("Cannot connect to SteamVR. Is it running, with the "
                          "trackers powered on and paired?")
            self.ready.set()
            return

        self._open_logs()
        self.started_at = time.time()
        self.ready.set()

        poll_dt = 1.0 / POLL_HZ
        prev = self.started_at
        last_csv = 0.0
        last_battery = 0.0
        event = openvr.VREvent_t()

        try:
            while not self._stop.is_set():
                now = time.time()
                dt = now - prev
                prev = now

                self._drain_vr_events(event, now)
                self._sample(now, dt, last_battery, read_batt=(now - last_battery) >= BATTERY_PERIOD_S)
                if (now - last_battery) >= BATTERY_PERIOD_S:
                    last_battery = now
                if self._csv_writer and (now - last_csv) >= CSV_PERIOD_S:
                    last_csv = now
                    self._write_csv(now)

                time.sleep(poll_dt)
        finally:
            self._finish()

    # -- per-poll work ------------------------------------------------------ #
    def _drain_vr_events(self, event, now):
        while self._vr.pollNextEvent(event):
            idx = event.trackedDeviceIndex
            if event.eventType == openvr.VREvent_TrackedDeviceDeactivated:
                serial = self.index_serial.get(idx, f"idx{idx}")
                self._log_event(f"DEACTIVATED tracker={serial}")
            elif event.eventType == openvr.VREvent_TrackedDeviceActivated:
                st = self._ensure_tracker(idx, now)
                serial = st.serial if st else f"idx{idx}"
                self._log_event(f"ACTIVATED tracker={serial}")

    def _ensure_tracker(self, idx, now):
        cls = self._vr.getTrackedDeviceClass(idx)
        if not _is_tracked_class(cls):
            return None
        serial = self.index_serial.get(idx)
        if serial is None:
            serial = _get_str(self._vr, idx, openvr.Prop_SerialNumber_String)
            if not serial:
                return None
            self.index_serial[idx] = serial
        st = self.trackers.get(serial)
        if st is None:
            model = _get_str(self._vr, idx, openvr.Prop_ModelNumber_String)
            dongle = _get_str(self._vr, idx,
                              openvr.Prop_ConnectedWirelessDongle_String)
            st = TrackerStat(serial, model, dongle)
            with self._lock:
                self.trackers[serial] = st
            self._log_event(f"DEVICE SEEN tracker={serial} model={model} "
                            f"dongle={dongle or 'UNKNOWN'}")
        return st

    def _sample(self, now, dt, last_battery, read_batt):
        poses = self._vr.getDeviceToAbsoluteTrackingPose(
            openvr.TrackingUniverseRawAndUncalibrated, 0,
            openvr.k_unMaxTrackedDeviceCount)

        dropped_by_dongle = defaultdict(list)   # dongle -> [serials that dropped]
        with self._lock:
            for idx in range(openvr.k_unMaxTrackedDeviceCount):
                st = self._ensure_tracker(idx, now)
                if st is None:
                    continue
                packet_num = None
                ok, state = self._vr.getControllerState(idx)
                if ok:
                    packet_num = state.unPacketNum

                edge = st.update(poses[idx], packet_num, now, dt)
                if edge:
                    dropped_by_dongle[st.dongle].append(st.serial)

                if read_batt:
                    b = _get_float(self._vr, idx,
                                   openvr.Prop_DeviceBatteryPercentage_Float)
                    if b is not None:
                        st.battery = b * 100.0

            # Whole-dongle drop = likely physical unplug -> identify aid.
            dongle_members = defaultdict(list)
            for st in self.trackers.values():
                dongle_members[st.dongle].append(st)

        for dongle, dropped in dropped_by_dongle.items():
            members = dongle_members.get(dongle, [])
            all_down = members and all(not m.was_connected for m in members)
            tlist = ", ".join(sorted(dropped))
            if all_down and len(members) >= 1:
                self._notify("identify",
                             f"Dongle {dongle} went DOWN (all {len(members)} "
                             f"tracker(s) lost link at once) - if you just "
                             f"unplugged a dongle, THIS is it. Serves: {tlist}")
            else:
                self._notify("dropout",
                             f"RF dropout on dongle {dongle}: tracker(s) {tlist}")

    # -- CSV ---------------------------------------------------------------- #
    def _open_logs(self):
        if not self.enable_log:
            return
        os.makedirs(self.log_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(c for c in self.site if c.isalnum() or c in "-_")
        csv_path = os.path.join(self.log_dir, f"{safe}_{stamp}_series.csv")
        evt_path = os.path.join(self.log_dir, f"{safe}_{stamp}_events.log")
        self._csv_file = open(csv_path, "w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow([
            "timestamp", "site", "tracker", "model", "dongle",
            "rf_loss_pct", "optical_loss_pct", "update_hz", "battery_pct",
            "disconnect_events", "stall_events", "longest_disconnect_s",
            "connected",
        ])
        self._event_file = open(evt_path, "w", encoding="utf-8")
        self._event_file.write(
            f"# Vive dongle monitor event log  site={self.site}  "
            f"started={datetime.now().isoformat()}\n")
        self.csv_path = csv_path
        self.event_path = evt_path

    def _write_csv(self, now):
        ts = datetime.now().isoformat()
        with self._lock:
            rows = [st.to_row() for st in self.trackers.values()]
        for r in rows:
            self._csv_writer.writerow([
                ts, self.site, r["serial"], r["model"], r["dongle"],
                f"{r['rf_loss_pct']:.3f}", f"{r['optical_loss_pct']:.3f}",
                f"{r['update_hz']:.1f}",
                f"{r['battery']:.0f}" if r["battery"] is not None else "",
                r["disconnect_events"], r["stall_events"],
                f"{r['longest_disconnect_s']:.2f}",
                int(bool(r["connected"])),
            ])
        self._csv_file.flush()

    def _finish(self):
        self.stopped_at = time.time()
        with self._lock:
            for st in self.trackers.values():
                st.finalize(self.stopped_at)
        verdict = self.verdict_text()
        if self._event_file:
            self._event_file.write("\n" + verdict + "\n")
            self._event_file.close()
        if self._csv_file:
            self._csv_file.close()
        try:
            if self._vr is not None:
                openvr.shutdown()
        except Exception:
            pass

    # -- snapshot for consumers -------------------------------------------- #
    def snapshot(self):
        """Thread-safe view: rows, per-dongle aggregation, drained notifications."""
        with self._lock:
            rows = [st.to_row() for st in self.trackers.values()]
            notes = list(self.notifications)
            self.notifications.clear()
        by_dongle = defaultdict(list)
        for r in rows:
            by_dongle[r["dongle"]].append(r)
        aggregates = {}
        for dongle, group in by_dongle.items():
            aggregates[dongle] = {
                "count": len(group),
                "rf_loss_pct": sum(g["rf_loss_pct"] for g in group) / len(group),
                "optical_loss_pct": sum(g["optical_loss_pct"] for g in group) / len(group),
                "dropouts": sum(g["disconnect_events"] for g in group),
                "stalls": sum(g["stall_events"] for g in group),
            }
        elapsed = ((self.stopped_at or time.time()) - self.started_at
                   if self.started_at else 0.0)
        return {
            "rows": rows,
            "by_dongle": dict(by_dongle),
            "aggregates": aggregates,
            "notifications": notes,
            "elapsed": elapsed,
        }

    # -- verdict ------------------------------------------------------------ #
    def verdict_text(self):
        snap = self.snapshot()
        rows = snap["rows"]
        elapsed = snap["elapsed"]
        lines = ["=" * 78,
                 f" VERDICT   site: {self.site}   duration: {elapsed:.0f}s",
                 "=" * 78]
        if not rows:
            lines.append(" No trackers were observed.")
            return "\n".join(lines)

        agg = snap["aggregates"]
        ranked = sorted(agg.items(), key=lambda kv: kv[1]["rf_loss_pct"],
                        reverse=True)
        lines.append("")
        lines.append(" Per-dongle RF health (worst first):")
        lines.append(f"   {'dongle':<18}{'#trk':>5}{'RFloss%':>9}{'drops':>7}"
                     f"{'stalls':>8}{'optic%':>8}")
        for dongle, a in ranked:
            lines.append(f"   {dongle:<18}{a['count']:>5}{a['rf_loss_pct']:>9.2f}"
                         f"{a['dropouts']:>7}{a['stalls']:>8}"
                         f"{a['optical_loss_pct']:>8.2f}")

        if len(ranked) >= 2:
            worst_d, worst = ranked[0]
            best_d, best = ranked[-1]
            lines.append("")
            if worst["rf_loss_pct"] >= RF_LOSS_WARN and \
                    worst["rf_loss_pct"] >= 2 * max(best["rf_loss_pct"], 0.01):
                ratio = worst["rf_loss_pct"] / max(best["rf_loss_pct"], 0.01)
                lines.append(f" >> Dongle {worst_d} has {ratio:.1f}x the RF loss "
                             f"of the best dongle ({best_d}).")
                lines.append("    The radio link, not the lighthouses, is the "
                             "bottleneck on that dongle.")
                lines.append("    Elevate it ~1.5-2m on a USB extension, away "
                             "from the floor, metal,")
                lines.append("    USB3 ports/cables and other dongles.")
            else:
                lines.append(" >> No single dongle stands out on RF loss; "
                             "dropouts look evenly distributed.")

        lines.append("")
        lines.append(" Per-tracker classification:")
        for r in sorted(rows, key=lambda r: (r["dongle"], r["serial"])):
            lines.append(f"   {r['serial']:<16} dongle {r['dongle']:<16} "
                         f"{r['label']:<22} rf={r['rf_loss_pct']:.2f}% "
                         f"optic={r['optical_loss_pct']:.2f}% "
                         f"drops={r['disconnect_events']} "
                         f"stalls={r['stall_events']}")

        rf_n = sum(1 for r in rows if r["family"] == "rf")
        opt_n = sum(1 for r in rows if r["family"] == "optic")
        lines.append("")
        lines.append(f" Summary: {rf_n} tracker(s) with RF/dongle issues, "
                     f"{opt_n} with lighthouse issues, "
                     f"{len(rows) - rf_n - opt_n} healthy.")
        return "\n".join(lines)
