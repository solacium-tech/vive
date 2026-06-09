#!/usr/bin/env python3
"""
Vive Tracker Dongle / RF Diagnostic Monitor
===========================================

Purpose
-------
Teleoperation rigs that use HTC Vive trackers fail in two very different ways,
and they are easy to confuse:

  1. OPTICAL  - the tracker cannot see the lighthouses (occlusion, out of
                range, reflective surfaces). The existing green/red health
                monitor catches this.
  2. RF       - the tracker's 2.4GHz radio packets do not reach the USB
                dongle (weak link, ground attenuation, body shielding, USB3
                noise, 2.4GHz congestion from many robots, dongle on the
                floor). Almost nobody instruments this.

This tool isolates the RF / dongle failure mode from the optical one and,
crucially, attributes every dropout to the *specific dongle* a tracker is
paired with, so you can prove statements like "the floor-mounted dongle drops
10x more packets than the elevated one."

How the two failure modes are separated (all from OpenVR, no extra hardware)
---------------------------------------------------------------------------
For every tracked device each poll we read a TrackedDevicePose_t:

  * pose.bDeviceIsConnected == False
        -> the radio link is DOWN. This is a DONGLE / RF dropout.
  * connected, but the input packet number (unPacketNum) stops advancing
        -> radio starvation. Soft RF degradation that precedes a hard drop.
  * connected, but eTrackingResult == Running_OutOfRange
        -> radio is fine, the tracker just can't see the lighthouses.
           This is an OPTICAL / LIGHTHOUSE problem, NOT a dongle problem.

And the dongle a tracker is paired to:

  * Prop_ConnectedWirelessDongle_String -> dongle serial number.

We aggregate per tracker AND per dongle, log a timestamped CSV time-series and
an event log, and print a data-driven verdict at the end.

Runtime requirements
--------------------
  * SteamVR running, trackers powered on.
  * NO internet access required at runtime (talks to SteamVR over local IPC).
    Build the .exe on an internet-connected machine (see build_exe.bat) and
    copy the single file across to the firewalled PC.

Usage
-----
  vive_dongle_monitor.exe --site "WarehouseA-bay3" --duration 300
  python vive_dongle_monitor.py --site test --log-dir .\logs

  Ctrl+C stops early and still writes the summary.
"""

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

try:
    import openvr
except ImportError:
    sys.stderr.write(
        "\nERROR: the 'openvr' Python package is not available.\n"
        "If you are running the .exe this should never happen (it is bundled).\n"
        "If you are running from source: pip install openvr\n\n"
    )
    sys.exit(2)


# --------------------------------------------------------------------------- #
# Tunables                                                                     #
# --------------------------------------------------------------------------- #
POLL_HZ = 250            # how often we sample poses (fine-grained for RF)
REDRAW_HZ = 6            # how often we repaint the console
CSV_PERIOD_S = 1.0       # how often we append a CSV time-series row
BATTERY_PERIOD_S = 5.0   # battery telemetry changes slowly; don't spam reads
STALL_MS = 60            # connected but no new packet for this long = "stall"

# Verdict thresholds (percent of connected time / total time).
RF_LOSS_WARN = 0.5       # % of samples with the radio link DOWN
RF_LOSS_CRIT = 2.0
OPTICAL_WARN = 2.0       # % of connected samples out of lighthouse range
OPTICAL_CRIT = 8.0


# --------------------------------------------------------------------------- #
# Console helpers                                                              #
# --------------------------------------------------------------------------- #
def enable_ansi():
    """Enable ANSI/VT processing on Windows 10+ so colours + cursor work."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # STD_OUTPUT_HANDLE = -11, ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x4
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


CLEAR = "\033[H\033[J"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BOLD = "\033[1m"


def colour(text, code):
    return f"{code}{text}{RESET}"


# --------------------------------------------------------------------------- #
# Per-tracker state                                                           #
# --------------------------------------------------------------------------- #
class TrackerStat:
    def __init__(self, serial, model, dongle):
        self.serial = serial
        self.model = model
        self.dongle = dongle or "UNKNOWN"
        self.battery = None

        self.samples = 0              # total poses sampled
        self.connected_samples = 0    # radio link up
        self.pose_valid_samples = 0
        self.out_of_range_samples = 0  # connected but optically out of range
        self.rf_loss_samples = 0      # radio link down

        # Input-packet continuity (radio starvation detector).
        self.last_packet = None
        self.last_packet_time = None
        self.packet_increments = 0    # number of fresh packets seen
        self.stall_events = 0
        self._in_stall = False

        # Hard disconnect tracking (timestamped).
        self.was_connected = True
        self.disconnect_events = 0
        self.in_disconnect = False
        self.disconnect_start = None
        self.total_disconnect_s = 0.0
        self.longest_disconnect_s = 0.0
        self.connected_elapsed_s = 0.0  # wall time the link was up

        self.first_seen = time.time()

    # -- per-sample update -------------------------------------------------- #
    def update(self, pose, packet_num, now, dt):
        self.samples += 1
        connected = bool(pose.bDeviceIsConnected)

        if connected:
            self.connected_samples += 1
            self.connected_elapsed_s += dt

            if pose.bPoseIsValid:
                self.pose_valid_samples += 1

            # Optical health (radio is fine here, so any loss is the lighthouses)
            if pose.eTrackingResult == openvr.TrackingResult_Running_OutOfRange:
                self.out_of_range_samples += 1

            # Radio starvation: packet number should keep advancing.
            if packet_num is not None:
                if self.last_packet is None:
                    self.last_packet = packet_num
                    self.last_packet_time = now
                elif packet_num != self.last_packet:
                    self.packet_increments += 1
                    self.last_packet = packet_num
                    self.last_packet_time = now
                    self._in_stall = False
                else:
                    # No new packet. If it stays stale too long, count a stall.
                    if (self.last_packet_time is not None
                            and (now - self.last_packet_time) * 1000.0 > STALL_MS
                            and not self._in_stall):
                        self.stall_events += 1
                        self._in_stall = True
        else:
            self.rf_loss_samples += 1

        # Hard connect/disconnect edge detection (for timing + events).
        if not connected and self.was_connected:
            self.in_disconnect = True
            self.disconnect_start = now
            self.disconnect_events += 1
        elif connected and not self.was_connected:
            if self.disconnect_start is not None:
                dur = now - self.disconnect_start
                self.total_disconnect_s += dur
                self.longest_disconnect_s = max(self.longest_disconnect_s, dur)
            self.in_disconnect = False
            self.disconnect_start = None
        self.was_connected = connected

    def finalize(self, now):
        """Close out an in-progress disconnect when monitoring stops."""
        if self.in_disconnect and self.disconnect_start is not None:
            dur = now - self.disconnect_start
            self.total_disconnect_s += dur
            self.longest_disconnect_s = max(self.longest_disconnect_s, dur)

    # -- derived metrics ---------------------------------------------------- #
    @property
    def rf_loss_pct(self):
        return 100.0 * self.rf_loss_samples / self.samples if self.samples else 0.0

    @property
    def optical_loss_pct(self):
        # As a fraction of CONNECTED time: "when the radio was up, how often
        # was the tracker optically lost?"
        c = self.connected_samples
        return 100.0 * self.out_of_range_samples / c if c else 0.0

    @property
    def update_hz(self):
        return (self.packet_increments / self.connected_elapsed_s
                if self.connected_elapsed_s > 0.2 else 0.0)

    def classify(self):
        """Return (label, colour) describing the dominant problem."""
        rf = self.rf_loss_pct
        opt = self.optical_loss_pct
        if rf >= RF_LOSS_CRIT:
            return "RF / DONGLE FAILURE", RED
        if rf >= RF_LOSS_WARN or self.stall_events > 0:
            return "RF / DONGLE WEAK", YELLOW
        if opt >= OPTICAL_CRIT:
            return "LIGHTHOUSE BLOCKED", RED
        if opt >= OPTICAL_WARN:
            return "LIGHTHOUSE MARGINAL", YELLOW
        return "HEALTHY", GREEN


# --------------------------------------------------------------------------- #
# OpenVR property reads (defensive)                                           #
# --------------------------------------------------------------------------- #
def get_str(vr, idx, prop):
    try:
        return vr.getStringTrackedDeviceProperty(idx, prop)
    except Exception:
        return ""


def get_float(vr, idx, prop):
    try:
        return vr.getFloatTrackedDeviceProperty(idx, prop)
    except Exception:
        return None


def is_tracked_class(cls):
    return cls in (openvr.TrackedDeviceClass_GenericTracker,
                   openvr.TrackedDeviceClass_Controller)


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Vive tracker dongle / RF diagnostic monitor")
    ap.add_argument("--site", default="site",
                    help="label for this run, used in log filenames + CSV")
    ap.add_argument("--log-dir", default="logs",
                    help="directory for CSV time-series + event log")
    ap.add_argument("--duration", type=float, default=0,
                    help="auto-stop after N seconds (0 = run until Ctrl+C)")
    ap.add_argument("--no-log", action="store_true",
                    help="do not write any files, console only")
    args = ap.parse_args()

    enable_ansi()

    # --- connect to SteamVR ------------------------------------------------ #
    try:
        vr = openvr.init(openvr.VRApplication_Background)
    except Exception:
        sys.stderr.write(
            "\n" + colour("CANNOT CONNECT TO STEAMVR.", RED) + "\n"
            "  * Is SteamVR actually running (headset/Null driver active)?\n"
            "  * Are the trackers powered on and paired?\n\n")
        sys.exit(1)

    # --- prepare log files ------------------------------------------------- #
    csv_writer = csv_file = event_file = None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not args.no_log:
        os.makedirs(args.log_dir, exist_ok=True)
        safe_site = "".join(c for c in args.site if c.isalnum() or c in "-_")
        csv_path = os.path.join(args.log_dir, f"{safe_site}_{stamp}_series.csv")
        evt_path = os.path.join(args.log_dir, f"{safe_site}_{stamp}_events.log")
        csv_file = open(csv_path, "w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow([
            "timestamp", "site", "tracker", "model", "dongle",
            "rf_loss_pct", "optical_loss_pct", "update_hz", "battery_pct",
            "disconnect_events", "stall_events", "longest_disconnect_s",
            "connected", "pose_valid", "tracking_result",
        ])
        event_file = open(evt_path, "w", encoding="utf-8")
        event_file.write(f"# Vive dongle monitor event log  site={args.site}  "
                         f"started={datetime.now().isoformat()}\n")

    def log_event(msg):
        line = f"{datetime.now().isoformat()}  {msg}"
        if event_file:
            event_file.write(line + "\n")
            event_file.flush()

    log_event(f"MONITOR START  site={args.site}")

    # --- runtime state ----------------------------------------------------- #
    trackers = {}                 # serial -> TrackerStat
    index_serial = {}             # device index -> serial (cache)
    last_battery_read = 0.0
    last_redraw = 0.0
    last_csv = 0.0
    poll_dt = 1.0 / POLL_HZ
    start = time.time()
    prev = start
    event = openvr.VREvent_t()

    def ensure_tracker(vr, idx, now):
        """Map a device index to a TrackerStat, creating it on first sight."""
        cls = vr.getTrackedDeviceClass(idx)
        if not is_tracked_class(cls):
            return None
        serial = index_serial.get(idx)
        if serial is None:
            serial = get_str(vr, idx, openvr.Prop_SerialNumber_String)
            if not serial:
                return None
            index_serial[idx] = serial
        st = trackers.get(serial)
        if st is None:
            model = get_str(vr, idx, openvr.Prop_ModelNumber_String)
            dongle = get_str(vr, idx, openvr.Prop_ConnectedWirelessDongle_String)
            st = TrackerStat(serial, model, dongle)
            trackers[serial] = st
            log_event(f"DEVICE SEEN    tracker={serial} model={model} "
                      f"dongle={dongle or 'UNKNOWN'}")
        return st

    try:
        while True:
            now = time.time()
            dt = now - prev
            prev = now

            # --- SteamVR events (timestamped connect/disconnect) ----------- #
            while vr.pollNextEvent(event):
                idx = event.trackedDeviceIndex
                if event.eventType == openvr.VREvent_TrackedDeviceDeactivated:
                    serial = index_serial.get(idx, f"idx{idx}")
                    log_event(f"DEACTIVATED    tracker={serial} "
                              f"(radio link lost / powered off)")
                elif event.eventType == openvr.VREvent_TrackedDeviceActivated:
                    st = ensure_tracker(vr, idx, now)
                    serial = st.serial if st else f"idx{idx}"
                    log_event(f"ACTIVATED      tracker={serial} "
                              f"(radio link (re)established)")

            # --- pose sampling --------------------------------------------- #
            poses = vr.getDeviceToAbsoluteTrackingPose(
                openvr.TrackingUniverseRawAndUncalibrated, 0,
                openvr.k_unMaxTrackedDeviceCount)

            read_batt = (now - last_battery_read) >= BATTERY_PERIOD_S
            for idx in range(openvr.k_unMaxTrackedDeviceCount):
                st = ensure_tracker(vr, idx, now)
                if st is None:
                    continue
                packet_num = None
                ok, state = vr.getControllerState(idx)
                if ok:
                    packet_num = state.unPacketNum

                prev_disc = st.disconnect_events
                st.update(poses[idx], packet_num, now, dt)
                if st.disconnect_events > prev_disc:
                    log_event(f"RF DROPOUT     tracker={st.serial} "
                              f"dongle={st.dongle}")

                if read_batt:
                    b = get_float(vr, idx, openvr.Prop_DeviceBatteryPercentage_Float)
                    if b is not None:
                        st.battery = b * 100.0
            if read_batt:
                last_battery_read = now

            # --- CSV time-series ------------------------------------------- #
            if csv_writer and (now - last_csv) >= CSV_PERIOD_S:
                last_csv = now
                ts = datetime.now().isoformat()
                for st in trackers.values():
                    p = poses_index_for(st, index_serial)
                    pose = poses[p] if p is not None else None
                    tr = pose.eTrackingResult if pose else ""
                    csv_writer.writerow([
                        ts, args.site, st.serial, st.model, st.dongle,
                        f"{st.rf_loss_pct:.3f}", f"{st.optical_loss_pct:.3f}",
                        f"{st.update_hz:.1f}",
                        f"{st.battery:.0f}" if st.battery is not None else "",
                        st.disconnect_events, st.stall_events,
                        f"{st.longest_disconnect_s:.2f}",
                        int(pose.bDeviceIsConnected) if pose else "",
                        int(pose.bPoseIsValid) if pose else "",
                        tracking_result_name(tr),
                    ])
                csv_file.flush()

            # --- console repaint ------------------------------------------- #
            if (now - last_redraw) >= (1.0 / REDRAW_HZ):
                last_redraw = now
                render(trackers, args.site, now - start)

            # --- duration cap ---------------------------------------------- #
            if args.duration and (now - start) >= args.duration:
                break

            time.sleep(poll_dt)

    except KeyboardInterrupt:
        pass
    finally:
        end = time.time()
        for st in trackers.values():
            st.finalize(end)
        log_event(f"MONITOR STOP   duration={end - start:.1f}s")
        verdict = render_verdict(trackers, args.site, end - start)
        if event_file:
            event_file.write("\n" + verdict + "\n")
            event_file.close()
        if csv_file:
            csv_file.close()
        try:
            openvr.shutdown()
        except Exception:
            pass

        if not args.no_log:
            print(colour(f"\nLogs written to: {os.path.abspath(args.log_dir)}",
                         CYAN))
        if os.name == "nt":
            try:
                input("\nPress Enter to exit...")
            except EOFError:
                pass


def poses_index_for(st, index_serial):
    for idx, serial in index_serial.items():
        if serial == st.serial:
            return idx
    return None


def tracking_result_name(tr):
    names = {
        getattr(openvr, "TrackingResult_Uninitialized", -1): "Uninitialized",
        getattr(openvr, "TrackingResult_Calibrating_InProgress", -2): "Calibrating",
        getattr(openvr, "TrackingResult_Calibrating_OutOfRange", -3): "CalibOutOfRange",
        getattr(openvr, "TrackingResult_Running_OK", -4): "OK",
        getattr(openvr, "TrackingResult_Running_OutOfRange", -5): "OutOfRange",
    }
    return names.get(tr, str(tr))


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #
def render(trackers, site, elapsed):
    out = [CLEAR]
    out.append(colour("=" * 78, CYAN))
    out.append(colour(f" VIVE DONGLE / RF DIAGNOSTIC   site: {site}"
                      f"   elapsed: {elapsed:6.0f}s", BOLD))
    out.append(colour("=" * 78, CYAN))
    out.append(f"{DIM} Separating radio (dongle) loss from optical (lighthouse) "
               f"loss.  Ctrl+C to stop.{RESET}")
    out.append("")

    if not trackers:
        out.append(colour("  Waiting for trackers... (power them on)", YELLOW))
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()
        return

    # group by dongle so floor-vs-elevated comparisons jump out
    by_dongle = defaultdict(list)
    for st in trackers.values():
        by_dongle[st.dongle].append(st)

    for dongle in sorted(by_dongle):
        group = by_dongle[dongle]
        agg_rf = sum(s.rf_loss_pct for s in group) / len(group)
        agg_drop = sum(s.disconnect_events for s in group)
        head = (f" DONGLE {dongle}   "
                f"[{len(group)} tracker(s)]  "
                f"avg RF loss {agg_rf:5.2f}%   dropouts {agg_drop}")
        out.append(colour(head, BOLD))
        for st in sorted(group, key=lambda s: s.serial):
            label, col = st.classify()
            batt = f"{st.battery:3.0f}%" if st.battery is not None else "  ?"
            out.append(
                f"  {st.serial:<16} {colour(f'{label:<20}', col)}")
            out.append(
                f"    radio: {colour('DOWN ' + f'{st.rf_loss_pct:5.2f}%', _sev(st.rf_loss_pct, RF_LOSS_WARN, RF_LOSS_CRIT))}"
                f"  dropouts {st.disconnect_events:<3} stalls {st.stall_events:<3}"
                f"  longest {st.longest_disconnect_s:4.1f}s")
            out.append(
                f"    optic: {colour('OOR  ' + f'{st.optical_loss_pct:5.2f}%', _sev(st.optical_loss_pct, OPTICAL_WARN, OPTICAL_CRIT))}"
                f"  rate {st.update_hz:5.1f}Hz  batt {batt}")
        out.append("")

    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def _sev(value, warn, crit):
    if value >= crit:
        return RED
    if value >= warn:
        return YELLOW
    return GREEN


def render_verdict(trackers, site, duration):
    lines = []
    lines.append("=" * 78)
    lines.append(f" VERDICT   site: {site}   duration: {duration:.0f}s")
    lines.append("=" * 78)

    if not trackers:
        lines.append(" No trackers were observed.")
        text = "\n".join(lines)
        print("\n" + text)
        return text

    # Per-dongle aggregation: this is the proof.
    by_dongle = defaultdict(list)
    for st in trackers.values():
        by_dongle[st.dongle].append(st)

    dongle_rows = []
    for dongle, group in by_dongle.items():
        rf = sum(s.rf_loss_pct for s in group) / len(group)
        drops = sum(s.disconnect_events for s in group)
        stalls = sum(s.stall_events for s in group)
        opt = sum(s.optical_loss_pct for s in group) / len(group)
        dongle_rows.append((dongle, len(group), rf, drops, stalls, opt))
    dongle_rows.sort(key=lambda r: r[2], reverse=True)

    lines.append("")
    lines.append(" Per-dongle RF health (ranked worst first):")
    lines.append(f"   {'dongle':<18}{'#trk':>5}{'RFloss%':>9}{'drops':>7}"
                 f"{'stalls':>8}{'optic%':>8}")
    for dongle, n, rf, drops, stalls, opt in dongle_rows:
        lines.append(f"   {dongle:<18}{n:>5}{rf:>9.2f}{drops:>7}{stalls:>8}{opt:>8.2f}")

    # Compare best vs worst dongle to support / refute the floor hypothesis.
    if len(dongle_rows) >= 2:
        worst = dongle_rows[0]
        best = dongle_rows[-1]
        lines.append("")
        if worst[2] >= RF_LOSS_WARN and worst[2] >= 2 * max(best[2], 0.01):
            ratio = worst[2] / max(best[2], 0.01)
            lines.append(colour(
                f" >> Dongle {worst[0]} has {ratio:.1f}x the RF loss of the best "
                f"dongle ({best[0]}).", RED))
            lines.append(
                "    The radio link, not the lighthouses, is the bottleneck on "
                "that dongle.")
            lines.append(
                "    Check its placement: elevate it ~1.5-2m on a USB extension, "
                "away from")
            lines.append(
                "    the floor, metal, USB3 ports/cables and other dongles.")
        else:
            lines.append(
                " >> No single dongle stands out on RF loss; dropouts look "
                "evenly distributed.")

    # Per-tracker classification summary.
    lines.append("")
    lines.append(" Per-tracker classification:")
    for st in sorted(trackers.values(), key=lambda s: (s.dongle, s.serial)):
        label, _ = st.classify()
        lines.append(
            f"   {st.serial:<16} dongle {st.dongle:<16} {label:<22}"
            f" rf={st.rf_loss_pct:.2f}% optic={st.optical_loss_pct:.2f}%"
            f" drops={st.disconnect_events} stalls={st.stall_events}")

    rf_problem = [s for s in trackers.values()
                  if s.classify()[0].startswith("RF")]
    opt_problem = [s for s in trackers.values()
                   if s.classify()[0].startswith("LIGHTHOUSE")]
    lines.append("")
    lines.append(f" Summary: {len(rf_problem)} tracker(s) with RF/dongle issues, "
                 f"{len(opt_problem)} with lighthouse issues, "
                 f"{len(trackers) - len(rf_problem) - len(opt_problem)} healthy.")

    text = "\n".join(lines)
    print("\n" + text)
    return text


if __name__ == "__main__":
    main()
