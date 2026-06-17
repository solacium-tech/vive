#!/usr/bin/env python3
"""Core sampling engine for the Vive tracker RF diagnostics tools.

Polls SteamVR (via pyopenvr) on a background thread and maintains per-tracker
and per-dongle link statistics. Two failure modes are tracked separately:

  - radio:   pose.bDeviceIsConnected false, or input packet counter stalls
             (problem between tracker and its USB dongle)
  - optical: connected but the pose isn't a clean optical fix - out of range,
             rotation-only (position lost to IMU), or an invalid pose
             (problem between tracker and the base stations)

Optical loss is only accumulated while the radio link is up, so the two
figures never overlap. Each tracker is mapped to its paired dongle through
Prop_ConnectedWirelessDongle_String, which allows per-dongle aggregation.

Front-ends: vive_dongle_monitor.py (console), vive_dongle_gui.py (GUI).
"""

import csv
import os
import textwrap
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

try:
    import openvr
except ImportError:
    openvr = None


# Sampling configuration
POLL_HZ = 250
CSV_PERIOD_S = 1.0
BATTERY_PERIOD_S = 5.0
CENSUS_PERIOD_S = 1.0       # how often the full device census is rebuilt
STALL_MS = 60               # connected but no new input packet -> stall
# Connected and nominally tracking, but the pose is bit-for-bit frozen for
# longer than this: the host has stopped getting fresh radio updates even though
# the device hasn't formally disconnected. A heuristic for a starved/marginal
# radio link (e.g. 2.4GHz interference from a nearby workstation/USB3). A
# healthy tracker - even a still one - always shows tiny IMU/optical jitter, so
# a long freeze is the tell. Generous threshold to avoid false positives.
STALE_FREEZE_MS = 400

# Effective pose update rate (measure-only). A healthy tracker - even a still
# one - micro-changes its pose every frame, so it is never bit-identical frame
# to frame. A tracker that is not getting data at the full rate HOLDS the same
# pose for runs of frames. We therefore record, over a rolling window, the
# fraction of frames whose pose actually changed ("fresh %") and the resulting
# effective update rate. This generalises the NOT UPDATING freeze detector to
# catch PARTIAL starvation, and is the inverse of jitter: "is the tracker
# sending fresh data?". No alarm yet - this is a measurement to watch/compare.
UPDATE_WINDOW_S = 1.0

# The live view answers "is this link OK right now", so its colours and
# percentages are computed over a recent rolling window rather than the whole
# session. Without this, loss accumulated while a tracker was out of range
# keeps a recovered tracker flagged as failing for a long time, because the
# lifetime average only drifts back down slowly. The saved report still uses
# the full-session figures.
RECENT_WINDOW_S = 30

# "Not in use" detection. When the operator has stopped - the headset is off,
# or every tracker is powered down - their disconnection is not a radio fault,
# so we pause: those samples are not counted against the loss figures and the
# UI shows an idle state instead of flagging everything as failing. The pause
# is confirmed only after the condition has held for IDLE_GRACE_S, to ignore
# momentary blips, but counting stops immediately so the grace period never
# pollutes the report.
IDLE_GRACE_S = 3.0

# Classification thresholds (percent)
RF_LOSS_WARN = 0.5
RF_LOSS_CRIT = 2.0
# Operators want any optical dead zone flagged, because even ~1% vision loss
# causes IMU-coast drift (erratic movement). So amber triggers as soon as the
# column shows any loss at all (>=0.01%); red stays reserved for sustained loss.
OPTICAL_WARN = 0.01
OPTICAL_CRIT = 8.0

# Severity keys used by both front-ends
HEALTHY = "healthy"
WARN = "warn"
CRIT = "crit"


def severity(value, warn, crit):
    if value >= crit:
        return CRIT
    if value >= warn:
        return WARN
    return HEALTHY


def rf_verdict(loss_pct):
    """Per-domain radio verdict as (word, severity)."""
    sev = severity(loss_pct, RF_LOSS_WARN, RF_LOSS_CRIT)
    word = {HEALTHY: "Good", WARN: "Weak", CRIT: "FAILING"}[sev]
    return word, sev


def optical_verdict(loss_pct):
    """Per-domain lighthouse verdict as (word, severity)."""
    sev = severity(loss_pct, OPTICAL_WARN, OPTICAL_CRIT)
    word = {HEALTHY: "Good", WARN: "Marginal", CRIT: "BLOCKED"}[sev]
    return word, sev


class TrackerStat:
    """Accumulated link statistics for one tracked device."""

    def __init__(self, serial, model, dongle, name=""):
        self.serial = serial
        self.model = model
        self.dongle = dongle or "UNKNOWN"
        self.name = name
        self.battery = None

        self.samples = 0
        self.connected_samples = 0
        self.pose_valid_samples = 0
        self.out_of_range_samples = 0
        self.rf_loss_samples = 0

        # input packet continuity. Some trackers never advance the input
        # packet counter (no buttons), so we only use it for stall/rate
        # detection on devices where it is actually seen to advance.
        self.last_packet = None
        self.last_packet_time = None
        self.packet_increments = 0
        self.packet_ever_advanced = False
        self.stall_events = 0
        self._in_stall = False

        # pose-freeze ("not updating") detection: connected and nominally
        # tracking, but the pose stops changing -> host is not receiving fresh
        # radio data. A heuristic for a starved/marginal link.
        self.stale_events = 0
        self.stale_samples = 0
        # dongle-level wireless link drops, from SteamVR's own events
        self.wireless_disconnect_events = 0

        # effective pose update rate (is the tracker sending fresh data?)
        self._upd_window = deque()   # (timestamp, changed_bool) over last second
        self._upd_fresh = 0          # running count of 'changed' in the window
        self.fresh_pct = 100.0       # % of recent frames whose pose changed
        self.effective_hz = 0.0      # resulting pose updates/sec

        self._last_pose_key = None
        self._last_pose_change = None
        self._in_stale = False
        self._stale_edge = False

        # per-CSV-interval window counters (reset on every CSV row)
        self.win_samples = 0
        self.win_connected = 0
        self.win_rf_loss = 0
        self.win_oor = 0
        self.win_drops = 0
        self.win_stalls = 0

        # connect/disconnect edges
        self.was_connected = True
        self.disconnect_events = 0
        self.in_disconnect = False
        self.disconnect_start = None
        self.total_disconnect_s = 0.0
        self.longest_disconnect_s = 0.0
        self.connected_elapsed_s = 0.0

        # wall-clock (epoch) of first/last radio issue (drop or stall)
        self.first_issue_time = None
        self.first_issue_kind = ""
        self.last_issue_time = None

        # Rolling per-second buckets for the "recent" (live) figures. Each
        # completed second is (t_end, samples, rf_loss, connected, oor);
        # buckets older than RECENT_WINDOW_S are evicted.
        self._recent = deque()
        self._rb_start = None
        self._rb_samples = 0
        self._rb_rf = 0
        self._rb_conn = 0
        self._rb_oor = 0

        self.first_seen = time.time()

    def label_or_serial(self):
        return f"{self.name} ({self.serial})" if self.name else self.serial

    def _mark_issue(self, now, kind):
        if self.first_issue_time is None:
            self.first_issue_time = now
            self.first_issue_kind = kind
        self.last_issue_time = now

    def update(self, pose, packet_num, now, dt):
        """Fold in one sample. Returns 'drop'/'reconnect'/None for the edge."""
        self.samples += 1
        self.win_samples += 1
        connected = bool(pose.bDeviceIsConnected)
        oor = False
        edge = None

        if connected:
            self.connected_samples += 1
            self.win_connected += 1
            self.connected_elapsed_s += dt
            if pose.bPoseIsValid:
                self.pose_valid_samples += 1
            # Optical / line-of-sight loss while the radio link is up. As well
            # as the hard "out of range" state, this now counts position lost to
            # IMU-only (rotation-only fallback) and any invalid pose - e.g. a
            # foot tracker partly covered by clothing, where SteamVR keeps the
            # device connected but can no longer get a clean optical fix.
            pose_ok = (pose.bPoseIsValid
                       and pose.eTrackingResult
                       == openvr.TrackingResult_Running_OK)
            if not pose_ok:
                self.out_of_range_samples += 1
                self.win_oor += 1
                oor = True

            # Pose-freeze + effective-update-rate. A healthy tracker (even still)
            # changes its pose every frame; held/repeated frames mean the host
            # isn't getting fresh data. NOT UPDATING flags a full freeze; the
            # rolling fresh-% / effective-Hz below also catches partial
            # starvation (held runs shorter than the freeze threshold).
            if pose_ok:
                key = _pose_key(pose)
                pose_changed = (key is None) or (key != self._last_pose_key)
                if pose_changed:
                    self._last_pose_key = key
                    self._last_pose_change = now
                    self._in_stale = False
                elif (self._last_pose_change is not None
                      and (now - self._last_pose_change) * 1000.0
                      > STALE_FREEZE_MS):
                    self.stale_samples += 1
                    if not self._in_stale:
                        self.stale_events += 1
                        self._in_stale = True
                        self._stale_edge = True
                        self._mark_issue(now, "stale")
                # rolling window of changed/held frames
                self._upd_window.append((now, pose_changed))
                if pose_changed:
                    self._upd_fresh += 1
                while self._upd_window and now - self._upd_window[0][0] > \
                        UPDATE_WINDOW_S:
                    _, ch = self._upd_window.popleft()
                    if ch:
                        self._upd_fresh -= 1
                total = len(self._upd_window)
                if total:
                    self.fresh_pct = 100.0 * self._upd_fresh / total
                    span = now - self._upd_window[0][0]
                    self.effective_hz = (self._upd_fresh / span
                                         if span > 0 else 0.0)
            else:
                self._last_pose_key = None
                self._in_stale = False
                self._upd_window.clear()
                self._upd_fresh = 0

            if packet_num is not None:
                if self.last_packet is None:
                    self.last_packet = packet_num
                    self.last_packet_time = now
                elif packet_num != self.last_packet:
                    self.packet_increments += 1
                    self.packet_ever_advanced = True
                    self.last_packet = packet_num
                    self.last_packet_time = now
                    self._in_stall = False
                elif (self.packet_ever_advanced
                      and self.last_packet_time is not None
                      and (now - self.last_packet_time) * 1000.0 > STALL_MS
                      and not self._in_stall):
                    # Only meaningful for devices that DO stream a packet
                    # counter; otherwise a static counter is normal, not a fault.
                    self.stall_events += 1
                    self.win_stalls += 1
                    self._in_stall = True
                    self._mark_issue(now, "stall")
        else:
            self.rf_loss_samples += 1
            self.win_rf_loss += 1
            self._last_pose_key = None
            self._in_stale = False
            self._upd_window.clear()
            self._upd_fresh = 0

        if not connected and self.was_connected:
            self.in_disconnect = True
            self.disconnect_start = now
            self.disconnect_events += 1
            self.win_drops += 1
            edge = "drop"
            self._mark_issue(now, "drop")
        elif connected and not self.was_connected:
            if self.disconnect_start is not None:
                dur = now - self.disconnect_start
                self.total_disconnect_s += dur
                self.longest_disconnect_s = max(self.longest_disconnect_s, dur)
            self.in_disconnect = False
            self.disconnect_start = None
            edge = "reconnect"
        self.was_connected = connected
        self._update_recent(now, connected, oor)
        return edge

    def _update_recent(self, now, connected, oor):
        """Fold one sample into the rolling per-second window."""
        if self._rb_start is None:
            self._rb_start = now
        self._rb_samples += 1
        if connected:
            self._rb_conn += 1
            if oor:
                self._rb_oor += 1
        else:
            self._rb_rf += 1
        if now - self._rb_start >= 1.0:
            self._recent.append((now, self._rb_samples, self._rb_rf,
                                 self._rb_conn, self._rb_oor))
            self._rb_start = now
            self._rb_samples = self._rb_rf = self._rb_conn = self._rb_oor = 0
            cutoff = now - RECENT_WINDOW_S
            while self._recent and self._recent[0][0] < cutoff:
                self._recent.popleft()

    def _recent_totals(self):
        s, rf, conn, oor = (self._rb_samples, self._rb_rf,
                            self._rb_conn, self._rb_oor)
        for _, bs, brf, bconn, boor in self._recent:
            s += bs
            rf += brf
            conn += bconn
            oor += boor
        return s, rf, conn, oor

    @property
    def recent_rf_loss_pct(self):
        s, rf, _, _ = self._recent_totals()
        return 100.0 * rf / s if s else 0.0

    @property
    def recent_optical_loss_pct(self):
        _, _, conn, oor = self._recent_totals()
        return 100.0 * oor / conn if conn else 0.0

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
    def stale_pct(self):
        """% of connected time the pose was frozen (not updating)."""
        c = self.connected_samples
        return 100.0 * self.stale_samples / c if c else 0.0

    @property
    def update_hz(self):
        # Unknown if the device never advances its packet counter.
        if not self.packet_ever_advanced or self.connected_elapsed_s <= 0.2:
            return None
        return self.packet_increments / self.connected_elapsed_s

    @staticmethod
    def _classify(rf, opt, stalls):
        if rf >= RF_LOSS_CRIT:
            return "Radio failing (dongle)", CRIT, "rf"
        if rf >= RF_LOSS_WARN or stalls > 0:
            return "Radio weak (dongle)", WARN, "rf"
        if opt >= OPTICAL_CRIT:
            return "Lighthouse blocked", CRIT, "optic"
        if opt >= OPTICAL_WARN:
            return "Lighthouse marginal", WARN, "optic"
        return "Healthy", HEALTHY, "ok"

    def classify(self):
        """(label, severity, family) for the dominant problem over the run."""
        return self._classify(self.rf_loss_pct, self.optical_loss_pct,
                              self.stall_events)

    def classify_recent(self):
        """As classify() but over the recent window - drives the live view."""
        return self._classify(self.recent_rf_loss_pct,
                              self.recent_optical_loss_pct, 0)

    def to_row(self):
        label, sev, family = self.classify()
        recent_label, recent_sev, recent_family = self.classify_recent()
        return {
            "serial": self.serial,
            "name": self.name,
            "label_name": self.name or self.serial,
            "model": self.model,
            "dongle": self.dongle,
            "label": label,
            "severity": sev,
            "family": family,
            "rf_loss_pct": self.rf_loss_pct,
            "optical_loss_pct": self.optical_loss_pct,
            # Recent-window figures drive the live view so it tracks current
            # health; the cumulative figures above feed the saved report.
            "recent_rf_loss_pct": self.recent_rf_loss_pct,
            "recent_optical_loss_pct": self.recent_optical_loss_pct,
            "recent_label": recent_label,
            "recent_severity": recent_sev,
            "recent_family": recent_family,
            "update_hz": self.update_hz,
            "battery": self.battery,
            "disconnect_events": self.disconnect_events,
            "stall_events": self.stall_events,
            "stale_events": self.stale_events,
            "stale_pct": self.stale_pct,
            "wireless_disconnect_events": self.wireless_disconnect_events,
            "fresh_pct": self.fresh_pct,
            "effective_hz": self.effective_hz,
            "longest_disconnect_s": self.longest_disconnect_s,
            "connected": self.was_connected,
            "first_issue_time": self.first_issue_time,
            "first_issue_kind": self.first_issue_kind,
            "last_issue_time": self.last_issue_time,
        }

    def pop_window(self):
        """Return and reset the per-interval counters (one CSV row's worth)."""
        w = {
            "rf_down_pct": (100.0 * self.win_rf_loss / self.win_samples
                            if self.win_samples else 0.0),
            "oor_pct": (100.0 * self.win_oor / self.win_connected
                        if self.win_connected else 0.0),
            "drops": self.win_drops,
            "stalls": self.win_stalls,
        }
        self.win_samples = self.win_connected = 0
        self.win_rf_loss = self.win_oor = 0
        self.win_drops = self.win_stalls = 0
        return w


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


# Plain .txt so a double-click opens Notepad - no Excel or Microsoft login.
NAMES_FILE = "tracker_names.txt"
LEGACY_NAMES_FILE = "tracker_names.csv"


def _parse_names_text(lines):
    """Parse 'serial = name' lines (also accepts comma/tab/colon)."""
    names = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", ",", "\t", ":"):
            if sep in line:
                serial, name = line.split(sep, 1)
                break
        else:
            continue
        serial, name = serial.strip(), name.strip()
        if serial and name and serial.lower() != "serial":
            names[serial] = name
    return names


def load_tracker_names(path=NAMES_FILE):
    """Read a serial->name mapping the user maintains. Missing file is fine.

    Falls back to the older tracker_names.csv if only that exists, so names
    set up before the switch to .txt keep working.
    """
    candidates = [path]
    if path == NAMES_FILE:
        candidates.append(LEGACY_NAMES_FILE)
    for p in candidates:
        if p and os.path.isfile(p):
            try:
                with open(p, encoding="utf-8-sig") as f:
                    return _parse_names_text(f)
            except Exception:
                return {}
    return {}


def write_tracker_names_template(path, serials, existing=None):
    """Create a names template listing seen trackers, if it doesn't exist."""
    if os.path.isfile(path):
        return
    existing = existing or {}
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Tracker names. Edit this in Notepad and save.\n")
            f.write("# One device per line:   SERIAL = friendly name\n")
            f.write("# Lines starting with # are ignored; blank names are OK.\n")
            f.write("\n")
            for s in serials:
                f.write(f"{s} = {existing.get(s, '')}\n")
    except Exception:
        pass


def _pose_key(pose):
    """A hashable snapshot of the device's pose matrix, or None if unreadable.

    Used to detect a frozen (non-updating) pose: identical keys across samples
    mean the host received no new data for that device.
    """
    try:
        m = pose.mDeviceToAbsoluteTracking
        return (m[0][0], m[0][1], m[0][2], m[0][3],
                m[1][0], m[1][1], m[1][2], m[1][3],
                m[2][0], m[2][1], m[2][2], m[2][3])
    except Exception:
        return None


def _hmd_worn(vr, idx):
    """Is the headset on the user's head?

    SteamVR drives the HMD's activity level from its proximity sensor, so a
    headset that is powered but lifted off the head reports something other
    than UserInteraction. Returns True/False, or None when the runtime can't
    tell (older builds) so the caller can fall back to power-state only.
    """
    try:
        level = vr.getTrackedDeviceActivityLevel(idx)
    except Exception:
        return None
    worn = getattr(openvr, "k_EDeviceActivityLevel_UserInteraction", 1)
    return level == worn


def _class_name(cls):
    names = {
        openvr.TrackedDeviceClass_HMD: "Headset",
        openvr.TrackedDeviceClass_Controller: "Controller",
        openvr.TrackedDeviceClass_GenericTracker: "Tracker",
        openvr.TrackedDeviceClass_TrackingReference: "Base station",
        openvr.TrackedDeviceClass_DisplayRedirect: "Display redirect",
    }
    return names.get(cls, f"Other({cls})")


def fmt_clock(epoch):
    """Format an epoch timestamp as HH:MM:SS, or '-' if None."""
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def build_report_lines(site, snap, generated=None):
    """Build the run report as (tag, text) line pairs.

    Tags: title, meta, banner-healthy/-warn/-crit, h2, note, header,
    row-healthy/-warn/-crit, advice, plain. The text renderer below and the
    GUI summary window both consume this, so they always show the same
    report.
    """
    generated = generated or datetime.now()
    rows = snap["rows"]
    agg = snap["aggregates"]
    elapsed = snap["elapsed"]
    out = []

    def emit(tag, text, width=76):
        # prose lines are wrapped so the .txt reads cleanly in Notepad
        for line in textwrap.wrap(text, width) or [""]:
            out.append((tag, line))

    out.append(("title", "VIVE TRACKER LINK REPORT"))
    out.append(("meta", f"Location: {site}    "
                f"{generated.strftime('%d %b %Y, %H:%M')}    "
                f"monitored for {elapsed:.0f}s    "
                f"{len(rows)} tracker(s)"))
    out.append(("plain", ""))

    if not rows:
        emit("banner-warn", "No trackers were observed. Check SteamVR was "
             "running and trackers were powered on.")
        return out

    ranked = sorted(agg.items(), key=lambda kv: kv[1]["rf_loss_pct"],
                    reverse=True)
    worst_d, worst = ranked[0]
    _, rf_sev = rf_verdict(worst["rf_loss_pct"])

    if rf_sev == CRIT:
        emit("banner-crit",
             f"RADIO PROBLEM: dongle {worst_d} lost its radio link "
             f"{worst['rf_loss_pct']:.1f}% of the time "
             f"({worst['dropouts']} drops). The dongle, not the "
             f"lighthouses, is the bottleneck.")
    elif rf_sev == WARN:
        emit("banner-warn",
             f"Weak radio link on dongle {worst_d} "
             f"({worst['rf_loss_pct']:.1f}% loss). Worth checking its "
             f"position.")
    else:
        opt_bad = [r for r in rows if r["family"] == "optic"]
        if opt_bad:
            names = ", ".join(r["serial"] for r in opt_bad)
            emit("banner-warn", f"Radio links healthy. Lighthouse "
                 f"visibility issues on: {names}.")
        else:
            emit("banner-healthy", "All radio links and lighthouse "
                 "visibility healthy.")

    out.append(("plain", ""))
    out.append(("h2", "DONGLES (worst radio link first)"))
    emit("note", "Radio loss = tracker could not reach this dongle. "
         "Lighthouse loss = tracker could not see the base stations "
         "(not a dongle problem). All percentages are for the whole "
         "session; the live view showed roughly the last "
         f"{RECENT_WINDOW_S:.0f}s.")
    out.append(("header", f"  {'dongle':<18}{'trackers':>9}  "
                f"{'radio link':<22}{'radio drops':>11}{'stalls':>8}  "
                f"{'lighthouse':<22}"))
    for dongle, a in ranked:
        rw, rs = rf_verdict(a["rf_loss_pct"])
        ow, _ = optical_verdict(a["optical_loss_pct"])
        radio = f"{rw}  {a['rf_loss_pct']:.2f}% lost"
        optic = f"{ow}  {a['optical_loss_pct']:.2f}% lost"
        out.append((f"row-{rs}",
                    f"  {dongle:<18}{a['count']:>9}  {radio:<22}"
                    f"{a['dropouts']:>11}{a['stalls']:>8}  {optic:<22}"))

    if len(ranked) >= 2:
        best_d, best = ranked[-1]
        if worst["rf_loss_pct"] >= RF_LOSS_WARN and \
                worst["rf_loss_pct"] >= 2 * max(best["rf_loss_pct"], 0.01):
            ratio = worst["rf_loss_pct"] / max(best["rf_loss_pct"], 0.01)
            out.append(("plain", ""))
            emit("advice",
                 f"RECOMMENDATION: dongle {worst_d} has {ratio:.1f}x the "
                 f"radio loss of the best dongle ({best_d}). Check its "
                 f"position: raise it 1.5-2 m on a USB extension, clear of "
                 f"the floor, metal, USB 3.0 ports/cables and other "
                 f"dongles, then run this monitor again to compare.")

    out.append(("plain", ""))
    out.append(("h2", "TRACKERS (worst first)"))
    out.append(("header", f"  {'tracker':<22}{'dongle':<18}"
                f"{'radio link':<22}{'drops':>6}{'longest':>9}  "
                f"{'lighthouse':<22}{'battery':>8}"))
    for r in sorted(rows, key=lambda r: (-r["rf_loss_pct"], r["serial"])):
        rw, _ = rf_verdict(r["rf_loss_pct"])
        ow, _ = optical_verdict(r["optical_loss_pct"])
        batt = f"{r['battery']:.0f}%" if r["battery"] is not None else "?"
        radio = f"{rw}  {r['rf_loss_pct']:.2f}% lost"
        optic = f"{ow}  {r['optical_loss_pct']:.2f}% lost"
        out.append((f"row-{r['severity']}",
                    f"  {r['label_name'][:21]:<22}{r['dongle']:<18}{radio:<22}"
                    f"{r['disconnect_events']:>6}"
                    f"{r['longest_disconnect_s']:>8.1f}s  {optic:<22}"
                    f"{batt:>8}"))

    # When did each tracker first run into trouble?
    issues = [r for r in rows if r.get("first_issue_time")]
    if issues:
        out.append(("plain", ""))
        out.append(("h2", "FIRST ISSUE TIMES (when each tracker first "
                          "dropped or degraded)"))
        out.append(("header", f"  {'tracker':<22}{'dongle':<18}"
                    f"{'first issue':<12}{'type':<8}{'last issue':<12}"))
        for r in sorted(issues, key=lambda r: r["first_issue_time"]):
            sev = ("row-crit" if r["family"] == "rf" else "row-warn")
            out.append((sev,
                        f"  {r['label_name'][:21]:<22}{r['dongle']:<18}"
                        f"{fmt_clock(r['first_issue_time']):<12}"
                        f"{r['first_issue_kind']:<8}"
                        f"{fmt_clock(r['last_issue_time']):<12}"))

    out.append(("plain", ""))
    emit("note", "Raw data: the per-second CSV and the event log were "
         "saved in the same folder as this report. The event log lists every "
         "drop with its exact timestamp.")
    return out


def build_report_text(site, snap, generated=None):
    """Render the report as plain text (saved as the .txt report file)."""
    rule = "=" * 78
    lines = []
    for tag, text in build_report_lines(site, snap, generated):
        if tag == "title":
            lines += [rule, " " + text, rule]
        elif tag == "h2":
            lines += [" " + text, " " + "-" * 76]
        else:
            lines.append((" " + text).rstrip())
    return "\n".join(lines)


class RFMonitor(threading.Thread):
    """Background sampler. Consumers call snapshot() for a thread-safe view."""

    def __init__(self, site="site", log_dir="logs", enable_log=True,
                 names_path=NAMES_FILE):
        super().__init__(daemon=True)
        self.site = site
        self.log_dir = log_dir
        self.enable_log = enable_log
        self.names_path = names_path
        self.names = load_tracker_names(names_path)

        # Re-entrant: the sampling thread may call helpers that also lock.
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.ready = threading.Event()
        self.error = None

        self.trackers = {}        # serial -> TrackerStat
        self.index_serial = {}    # device index -> serial
        self.census = []          # every device SteamVR currently reports
        self._census_logged = False
        self._names_template_written = False
        self.notifications = deque(maxlen=200)
        self.started_at = None
        self.stopped_at = None

        # "Not in use" / paused state (headset or all trackers off).
        self.paused = False
        self.paused_reason = ""
        self._idle_since = None

        # Serials the operator has hidden as not relevant to this run (e.g.
        # controllers used only for room setup). Hidden devices are excluded
        # from the table, the stats/aggregates, the saved report, and the
        # "all trackers off" idle check.
        self.dismissed = set()

        self._vr = None
        self._csv_file = None
        self._csv_writer = None
        self._event_file = None

    def _log_event(self, msg):
        if self._event_file:
            self._event_file.write(f"{datetime.now().isoformat()}  {msg}\n")
            self._event_file.flush()

    def _notify(self, kind, msg):
        with self._lock:
            self.notifications.append((datetime.now(), kind, msg))
        self._log_event(f"{kind.upper()}  {msg}")

    def stop(self):
        self._stop.set()

    @staticmethod
    def _init_error_message(exc):
        """Turn an openvr.init failure into specific, actionable guidance."""
        detail = str(exc)
        # pyopenvr names the cause in the exception, e.g.
        # "VRInitError_Init_NoServerForBackgroundApp".
        if "NoServerForBackgroundApp" in detail or "Init_NoServer" in detail:
            tip = ("SteamVR is not running yet. Start SteamVR first, wait "
                   "until the headset shows as active, then press Start.")
        elif "HmdNotFound" in detail or "Init_HmdNotFound" in detail:
            tip = ("SteamVR is up but no headset is detected. Connect/turn on "
                   "the headset (or enable a Null/headless driver) so SteamVR "
                   "becomes active, then press Start.")
        elif "PathRegistry" in detail or "InstallationNotFound" in detail:
            tip = ("SteamVR could not be located on this PC. Open SteamVR "
                   "once from Steam, then try again.")
        else:
            tip = ("Could not attach to SteamVR. Make sure SteamVR is running "
                   "and active, then press Start.")
        return f"{tip}\n\n(Technical detail: {detail})"

    def run(self):
        if openvr is None:
            self.error = "The 'openvr' package is not available."
            self.ready.set()
            return

        # SteamVR may still be starting up, so retry the connection for a few
        # seconds rather than failing on the first attempt.
        deadline = time.time() + 8.0
        last_exc = None
        while True:
            try:
                self._vr = openvr.init(openvr.VRApplication_Background)
                break
            except Exception as exc:
                last_exc = exc
                if self._stop.is_set() or time.time() >= deadline:
                    self.error = self._init_error_message(last_exc)
                    self.ready.set()
                    return
                time.sleep(0.5)

        self._open_logs()
        self.started_at = time.time()
        self.ready.set()

        poll_dt = 1.0 / POLL_HZ
        prev = self.started_at
        last_csv = 0.0
        last_battery = 0.0
        last_census = 0.0
        event = openvr.VREvent_t()

        try:
            while not self._stop.is_set():
                now = time.time()
                dt = now - prev
                prev = now

                self._drain_vr_events(event, now)
                read_batt = (now - last_battery) >= BATTERY_PERIOD_S
                do_census = (now - last_census) >= CENSUS_PERIOD_S
                self._sample(now, dt, read_batt, do_census)
                if read_batt:
                    last_battery = now
                if do_census:
                    last_census = now
                if self._csv_writer and (now - last_csv) >= CSV_PERIOD_S:
                    last_csv = now
                    self._write_csv()

                time.sleep(poll_dt)
        finally:
            self._finish()

    def _drain_vr_events(self, event, now):
        # Dongle-level wireless link events, where the runtime exposes them.
        disc = getattr(openvr, "VREvent_WirelessDisconnect", None)
        recon = getattr(openvr, "VREvent_WirelessReconnect", None)
        while self._vr.pollNextEvent(event):
            et = event.eventType
            idx = event.trackedDeviceIndex
            if et == openvr.VREvent_TrackedDeviceDeactivated:
                serial = self.index_serial.get(idx, f"idx{idx}")
                self._log_event(f"DEACTIVATED tracker={serial}")
            elif et == openvr.VREvent_TrackedDeviceActivated:
                st = self._ensure_tracker(idx, now)
                serial = st.serial if st else f"idx{idx}"
                self._log_event(f"ACTIVATED tracker={serial}")
            elif disc is not None and et == disc:
                self._note_wireless(idx, dropped=True)
            elif recon is not None and et == recon:
                self._note_wireless(idx, dropped=False)

    def _note_wireless(self, idx, dropped):
        """Log SteamVR's own dongle-level wireless drop/reconnect for a device.
        Logged alongside (not instead of) the polled connection metrics, and
        suppressed while the rig is paused (devices powering down)."""
        if self.paused:
            return
        serial = self.index_serial.get(idx)
        if serial is None or serial in self.dismissed:
            return
        st = self.trackers.get(serial)
        label = st.label_or_serial() if st else serial
        dongle = st.dongle if st else "?"
        if dropped:
            if st is not None:
                st.wireless_disconnect_events += 1
            self._notify("alert",
                         f"WIRELESS DROP: {label} lost its dongle radio link "
                         f"(dongle {dongle})")
        else:
            self._notify("info",
                         f"WIRELESS OK: {label} dongle radio link restored "
                         f"(dongle {dongle})")

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
            st = TrackerStat(serial, model, dongle,
                             name=self.names.get(serial, ""))
            with self._lock:
                self.trackers[serial] = st
            self._log_event(f"DEVICE SEEN tracker={serial} model={model} "
                            f"dongle={dongle or 'UNKNOWN'} "
                            f"name={st.name or '-'}")
        return st

    def _sample(self, now, dt, read_batt, do_census):
        poses = self._vr.getDeviceToAbsoluteTrackingPose(
            openvr.TrackingUniverseRawAndUncalibrated, 0,
            openvr.k_unMaxTrackedDeviceCount)

        # Gather all per-device SteamVR data first, without holding our lock,
        # so the UI thread is never blocked on slow IPC property reads.
        census = [] if do_census else None
        updates = []   # (idx, packet_num, battery) for tracked devices
        hmd_seen = False
        hmd_connected = False
        hmd_worn = False
        tracked_seen = False
        tracked_connected = False
        for idx in range(openvr.k_unMaxTrackedDeviceCount):
            cls = self._vr.getTrackedDeviceClass(idx)
            if cls == openvr.TrackedDeviceClass_Invalid:
                continue
            connected = bool(poses[idx].bDeviceIsConnected)
            if cls == openvr.TrackedDeviceClass_HMD:
                hmd_seen = True
                if connected:
                    hmd_connected = True
                    # Taking the headset off the head (proximity off) counts as
                    # not in use, even though it stays powered/connected. If the
                    # runtime can't report it, fall back to "connected = worn".
                    worn = _hmd_worn(self._vr, idx)
                    if worn is None or worn:
                        hmd_worn = True
            if do_census:
                census.append({
                    "index": idx,
                    "class": _class_name(cls),
                    "serial": _get_str(self._vr, idx,
                                       openvr.Prop_SerialNumber_String),
                    "model": _get_str(self._vr, idx,
                                      openvr.Prop_ModelNumber_String),
                    "dongle": _get_str(
                        self._vr, idx,
                        openvr.Prop_ConnectedWirelessDongle_String),
                    "connected": connected,
                })
            if not _is_tracked_class(cls):
                continue
            # Hidden devices (e.g. setup-only controllers) don't count towards
            # the "all trackers off" idle check.
            if self.index_serial.get(idx) not in self.dismissed:
                tracked_seen = True
                tracked_connected = tracked_connected or connected
            ok, state = self._vr.getControllerState(idx)
            packet_num = state.unPacketNum if ok else None
            battery = None
            if read_batt:
                b = _get_float(self._vr, idx,
                               openvr.Prop_DeviceBatteryPercentage_Float)
                if b is not None:
                    battery = b * 100.0
            updates.append((idx, packet_num, battery))

        # "Not in use": the headset is off or taken off the head, or every
        # tracker is powered off. Either way it is the operator stopping, not a
        # radio fault, so we stop counting these samples (immediately, so the
        # grace period never pollutes the report) and surface an idle state.
        headset_off = hmd_seen and not hmd_worn
        trackers_known = tracked_seen or any(
            s not in self.dismissed for s in self.trackers)
        all_trackers_off = trackers_known and not tracked_connected
        raw_idle = headset_off or all_trackers_off
        if raw_idle:
            parts = []
            if headset_off:
                parts.append("headset off" if not hmd_connected
                             else "headset removed")
            if all_trackers_off:
                parts.append("all trackers off")
            reason = " and ".join(parts)
            if self._idle_since is None:
                self._idle_since = now
            confirmed = (now - self._idle_since) >= IDLE_GRACE_S
        else:
            reason = ""
            confirmed = False
            self._idle_since = None

        dropped_by_dongle = defaultdict(list)
        reconnected = []
        stale_started = []
        with self._lock:
            if raw_idle:
                # Keep each tracker's state in sync but do not fold these
                # samples in, and raise no drop alarms.
                for idx, packet_num, battery in updates:
                    st = self._ensure_tracker(idx, now)
                    if st is None:
                        continue
                    st.was_connected = bool(poses[idx].bDeviceIsConnected)
                    if battery is not None:
                        st.battery = battery
            else:
                for idx, packet_num, battery in updates:
                    st = self._ensure_tracker(idx, now)
                    if st is None:
                        continue
                    edge = st.update(poses[idx], packet_num, now, dt)
                    if st.serial not in self.dismissed:
                        if edge == "drop":
                            dropped_by_dongle[st.dongle].append(st.serial)
                        elif edge == "reconnect":
                            reconnected.append(
                                (st.label_or_serial(), st.dongle))
                        if st._stale_edge:
                            st._stale_edge = False
                            stale_started.append(
                                (st.label_or_serial(), st.dongle))
                    if battery is not None:
                        st.battery = battery

            if census is not None:
                self.census = census
                if not self._census_logged:
                    self._census_logged = True
                    for d in census:
                        self._log_event(
                            f"DEVICE class={d['class']} serial={d['serial']} "
                            f"model={d['model']} dongle={d['dongle'] or '-'}")

            dongle_members = defaultdict(list)
            for st in self.trackers.values():
                dongle_members[st.dongle].append(st)

            # Once trackers are known, drop a names template the user can edit
            # to label each serial (loaded automatically on the next run).
            if (self.enable_log and not self._names_template_written
                    and self.trackers):
                self._names_template_written = True
                write_tracker_names_template(
                    self.names_path, sorted(self.trackers), self.names)

        name_of = {st.serial: st.label_or_serial()
                   for st in self.trackers.values()}
        for dongle, dropped in dropped_by_dongle.items():
            members = dongle_members.get(dongle, [])
            all_down = members and all(not m.was_connected for m in members)
            tlist = ", ".join(name_of.get(s, s) for s in sorted(dropped))
            if all_down and len(members) > 1:
                self._notify("alert",
                             f"DROP: all {len(members)} trackers on dongle "
                             f"{dongle} lost their radio link at the same time "
                             f"({tlist})")
            else:
                self._notify("alert",
                             f"DROP: radio link lost - {tlist} "
                             f"(dongle {dongle})")
        for label, dongle in reconnected:
            self._notify("info",
                         f"RECONNECTED: radio link restored - {label} "
                         f"(dongle {dongle})")
        for label, dongle in stale_started:
            self._notify("alert",
                         f"NOT UPDATING: {label} is connected but its pose "
                         f"stopped updating for >{STALE_FREEZE_MS / 1000:.1f}s "
                         f"- possible radio starvation or 2.4GHz interference "
                         f"(dongle {dongle})")

        # Pause/resume transitions (announced only after the grace window so
        # the event log records the gap instead of a flurry of false drops).
        if raw_idle and confirmed:
            if not self.paused:
                self.paused = True
                self._notify("info",
                             f"PAUSED: {reason} - operator not using the rig; "
                             f"monitoring paused (this time is not counted)")
            self.paused_reason = reason
        elif not raw_idle and self.paused:
            self.paused = False
            self.paused_reason = ""
            self._notify("info", "RESUMED: activity detected - monitoring")

    def _open_logs(self):
        if not self.enable_log:
            return
        os.makedirs(self.log_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(c for c in self.site if c.isalnum() or c in "-_")
        csv_path = os.path.join(self.log_dir, f"{safe}_{stamp}_series.csv")
        evt_path = os.path.join(self.log_dir, f"{safe}_{stamp}_events.log")
        self.report_path = os.path.join(self.log_dir,
                                        f"{safe}_{stamp}_report.txt")
        self._csv_file = open(csv_path, "w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        # One row per tracker per interval. The *_1s columns are values for
        # that interval only (what time-series tools like Grafana want);
        # *_total columns are cumulative since the run started.
        self._csv_writer.writerow([
            "timestamp", "epoch_s", "site", "tracker", "tracker_name",
            "model", "dongle", "connected",
            "rf_down_pct_1s", "optical_oor_pct_1s", "drops_1s", "stalls_1s",
            "rf_loss_pct_total", "optical_loss_pct_total",
            "drops_total", "stalls_total", "longest_disconnect_s",
            "update_hz", "battery_pct",
            "not_updating_pct_total", "not_updating_events_total",
            "wireless_drops_total",
            "pose_fresh_pct", "effective_update_hz",
        ])
        self._event_file = open(evt_path, "w", encoding="utf-8")
        self._event_file.write(
            f"# RF diagnostics event log  site={self.site}  "
            f"started={datetime.now().isoformat()}\n")
        self.csv_path = csv_path
        self.event_path = evt_path

    def _write_csv(self):
        now = datetime.now()
        ts = now.isoformat()
        epoch = f"{now.timestamp():.3f}"
        with self._lock:
            pairs = [(st.to_row(), st.pop_window())
                     for st in self.trackers.values()
                     if st.serial not in self.dismissed]
        for r, w in pairs:
            self._csv_writer.writerow([
                ts, epoch, self.site, r["serial"], r["name"],
                r["model"], r["dongle"], int(bool(r["connected"])),
                f"{w['rf_down_pct']:.3f}", f"{w['oor_pct']:.3f}",
                w["drops"], w["stalls"],
                f"{r['rf_loss_pct']:.3f}", f"{r['optical_loss_pct']:.3f}",
                r["disconnect_events"], r["stall_events"],
                f"{r['longest_disconnect_s']:.2f}",
                f"{r['update_hz']:.1f}" if r["update_hz"] is not None else "",
                f"{r['battery']:.0f}" if r["battery"] is not None else "",
                f"{r['stale_pct']:.3f}", r["stale_events"],
                r["wireless_disconnect_events"],
                f"{r['fresh_pct']:.1f}", f"{r['effective_hz']:.0f}",
            ])
        self._csv_file.flush()

    def _finish(self):
        self.stopped_at = time.time()
        with self._lock:
            for st in self.trackers.values():
                st.finalize(self.stopped_at)
        report = build_report_text(self.site, self.snapshot())
        if self._event_file:
            self._event_file.write("\n" + report + "\n")
            self._event_file.close()
        if self._csv_file:
            self._csv_file.close()
        if getattr(self, "report_path", None):
            with open(self.report_path, "w", encoding="utf-8") as f:
                f.write(report)
        try:
            if self._vr is not None:
                openvr.shutdown()
        except Exception:
            pass

    def dismiss(self, serial):
        """Hide a device (by serial) as not relevant to this run."""
        with self._lock:
            self.dismissed.add(serial)

    def restore_all(self):
        """Un-hide every previously dismissed device."""
        with self._lock:
            self.dismissed.clear()

    def snapshot(self):
        """Thread-safe copy of current stats plus drained notifications."""
        with self._lock:
            rows = [st.to_row() for st in self.trackers.values()
                    if st.serial not in self.dismissed]
            dismissed = sorted(self.dismissed)
            notes = list(self.notifications)
            census = list(self.census)
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
                "recent_rf_loss_pct": sum(g["recent_rf_loss_pct"] for g in group) / len(group),
                "recent_optical_loss_pct": sum(g["recent_optical_loss_pct"] for g in group) / len(group),
                "dropouts": sum(g["disconnect_events"] for g in group),
                "stalls": sum(g["stall_events"] for g in group),
            }
        elapsed = ((self.stopped_at or time.time()) - self.started_at
                   if self.started_at else 0.0)
        class_counts = defaultdict(int)
        for d in census:
            class_counts[d["class"]] += 1
        return {
            "rows": rows,
            "by_dongle": dict(by_dongle),
            "aggregates": aggregates,
            "notifications": notes,
            "elapsed": elapsed,
            "census": census,
            "class_counts": dict(class_counts),
            "paused": self.paused,
            "paused_reason": self.paused_reason,
            "dismissed": dismissed,
        }

    def verdict_text(self):
        """Plain-text run report (same content as the saved report file)."""
        return build_report_text(self.site, self.snapshot())
