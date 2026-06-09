# Vive Tracker Dongle / RF Diagnostic Monitor

A debugging tool for VR-teleoperation rigs that use HTC Vive trackers. It
isolates **radio / dongle** failures from **optical / lighthouse** failures and
attributes every dropout to the **specific dongle** a tracker is paired with —
so you can prove, with data, statements like *"the floor-mounted dongle drops
10× more packets than the elevated one."*

Your existing green/red monitor already tells you when the lighthouses can't see
a tracker. This tool covers the half nobody instruments: the 2.4 GHz link
between each tracker and its USB dongle.

---

## Why the old script couldn't prove anything

The Gemini batch script measured "occlusion rate" as simply `!bPoseIsValid`.
That flag goes false for **both** failure modes — a lighthouse occlusion and a
radio dropout look identical to it — so it can't tell a dongle problem from a
lighthouse problem. It then printed a hard-coded verdict ("floor placement is
causing ground reflection") regardless of what the data showed. It also
regenerated a Python file from `echo` lines inside a `.bat`, which is fragile
and depended on `pip install` at runtime (a non-starter behind your firewall).

## How this tool separates the two failure modes

Everything comes from SteamVR via OpenVR — no extra hardware:

| Signal (per tracker, per sample) | Meaning | Blamed on |
| --- | --- | --- |
| `bDeviceIsConnected == False` | radio link is **down** | **Dongle / RF** |
| connected, but `unPacketNum` stops advancing | radio **starvation** (soft) | **Dongle / RF** |
| connected, but `TrackingResult_Running_OutOfRange` | radio fine, can't see lighthouses | **Lighthouse / optical** |
| `Prop_ConnectedWirelessDongle_String` | which dongle the tracker is paired to | enables **per-dongle** roll-up |

The crucial idea: **optical loss is only counted while the radio is connected.**
If the link is down we attribute it to RF, not to the lighthouses. And because
we know each tracker's dongle serial, we aggregate dropouts **per dongle** and
rank them — that ranking is the evidence.

## Outputs

1. **Live console**, grouped by dongle, showing for each tracker: RF-down %,
   hard dropout count, stall count, longest dropout, optical out-of-range %,
   effective update rate (Hz), and battery.
2. **CSV time-series** (`logs/<site>_<timestamp>_series.csv`) — one row per
   tracker per second. Open in Excel/pandas to graph and compare runs/sites.
3. **Event log** (`logs/<site>_<timestamp>_events.log`) — timestamped
   activate/deactivate (dongle drop) events plus the final verdict.
4. **Verdict** on exit — per-dongle ranking, a best-vs-worst dongle comparison,
   and a per-tracker RF-vs-lighthouse classification.

---

## Build (do this once, on a machine with internet)

Your SteamVR PC is firewalled, so build the `.exe` elsewhere and copy the single
file across. The `.exe` needs **no internet** at runtime — it only talks to
SteamVR over local IPC.

```bat
build_exe.bat
```

This produces `dist\vive_dongle_monitor.exe`. Copy that one file to the SteamVR
PC anywhere you like.

> Why a bundled `.exe`? `openvr`'s wheel already contains `openvr_api.dll`, and
> PyInstaller's `--collect-all openvr` packs it in, so there is nothing to
> download or install on the locked-down machine.

## Run (on the SteamVR PC)

Start SteamVR and power on the trackers first, then:

```bat
vive_dongle_monitor.exe --site "WarehouseA-bay3" --duration 300
```

- `--site` labels the run (used in log filenames and the CSV) — use the site +
  location so runs are comparable later.
- `--duration` auto-stops after N seconds (0 = run until `Ctrl+C`).
- `--log-dir` changes where logs go (default `logs\`).
- `--no-log` for console-only.

If you have Python + `openvr` on a test box, you can skip the build:
`run_from_source.bat --site test`.

---

## The protocol that proves the floor-dongle hypothesis

To turn suspicion into evidence, hold everything else constant and change only
the dongle placement between two runs:

1. **Baseline (floor):** dongles where they are now (on the floor). Run for a
   fixed window with the robot/operators doing a representative motion:
   `vive_dongle_monitor.exe --site "bay3-FLOOR" --duration 300`
2. **Treatment (elevated):** move the dongles onto USB extension cables, ~1.5–2 m
   high, clear of metal/floor and spaced apart. Repeat the *same* motion:
   `vive_dongle_monitor.exe --site "bay3-ELEVATED" --duration 300`
3. **Compare** the two `*_series.csv` files (RF-down %, dropouts, stalls). If the
   floor run shows materially higher RF loss while optical % stays similar, the
   dongles — not the lighthouses — are the problem. The on-exit verdict already
   ranks dongles within a single run; across runs, compare the CSVs.

Because the tool labels each tracker with its dongle serial, you can even run a
single mixed session with one dongle on the floor and one elevated and let the
per-dongle ranking make the comparison for you.

---

## Things to check that the lighthouse-only view never showed

These are common Vive RF killers worth inspecting at each site, especially the
one with 30+ robots:

- **USB 3.0 interference.** USB3 ports, cables and hubs radiate broadband noise
  right in the 2.4 GHz band and are the classic cause of dongle dropouts. Put
  dongles on **USB 2.0** ports, on shielded extension cables, away from USB3
  cabling and external drives.
- **Floor placement** = ground attenuation + multipath + body shielding every
  time an operator/robot passes. Elevate dongles ~1.5–2 m, ideally at tracker
  height with line of sight.
- **Dongle spacing.** Bunched dongles desensitise each other. Space them ≥0.3 m
  apart (HTC's own guidance), not stacked in a hub.
- **2.4 GHz congestion** from 30+ trackers + Wi-Fi/Bluetooth in one room raises
  the noise floor. Each tracker/dongle pair is a separate link, but congestion
  still costs you margin. Your RF-shielding curtains help; verify they actually
  sit between dongle clusters, not just between lighthouses.
- **Battery / contacts.** A tracker low on battery or with dirty pogo contacts
  drops its link; the tool surfaces battery % per tracker.
- **Pairing drift.** Confirm each tracker is paired to a *nearby* dongle, not one
  across the room — the dongle serial column tells you which it actually uses.

---

## Files

| File | Purpose |
| --- | --- |
| `vive_dongle_monitor.py` | the monitor (live view + CSV/event logs + verdict) |
| `build_exe.bat` | build `dist\vive_dongle_monitor.exe` on an internet machine |
| `run_from_source.bat` | run from source on a dev/test PC |
| `requirements.txt` | `openvr` (runtime) + `pyinstaller` (build) |

## Notes & limitations

- `unPacketNum` (the stall detector) reflects host-side input-state updates; it's
  a good *relative* indicator of radio starvation, not a calibrated packet
  counter. The authoritative RF signal is `bDeviceIsConnected` (hard dropouts),
  which the verdict leans on.
- Requires SteamVR running. It connects as a background app and does not
  interfere with an active teleoperation session, so you can run it alongside
  normal operation.
- Tested against the standard `openvr` Python bindings; works with Vive trackers
  (1.0/2.0/3.0) and controllers.
