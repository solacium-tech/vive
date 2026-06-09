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

## The GUI

There are two front-ends over the same engine — a **GUI** (`vive_dongle_gui.exe`,
start here) and a **console** version (`vive_dongle_monitor.exe`, scriptable
with CLI flags). The GUI groups trackers under the dongle they're paired to,
colour-codes them by health (green/amber/red), and has an identification panel
at the bottom:

![GUI preview](docs/gui_preview.png)

In the example above, the three trackers on `D-FLOOR-7A21` (the floor dongle)
are red/amber with high RF loss and many dropouts, while the three on the
elevated `D-MAST-3C04` are green — exactly the picture that proves a dongle
placement problem rather than a lighthouse one.

## Which tracker is dropping to which dongle (and finding it physically)

Every tracker reports the serial of the dongle it's paired with
(`Prop_ConnectedWirelessDongle_String`), so the tool always knows *which
tracker is dropping and which dongle it belongs to* — that's the per-dongle
grouping you see above.

The dongles aren't physically labelled, though, so to map a **serial to a
physical USB stick**, use the built-in identify workflow:

1. Start the monitor (GUI or console).
2. Physically unplug one dongle.
3. Every tracker bound to it loses its link in the same instant, and the tool
   prints an **IDENTIFY** line: *"Dongle `D-FLOOR-7A21` went DOWN (all 3
   trackers lost link at once) — if you just unplugged a dongle, THIS is it.
   Serves: …"*
4. Label that stick, plug it back in, repeat for the next dongle.

No extra drivers or USB libraries required — it works purely from the link-loss
signal, so it's reliable behind the firewall.

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

This produces two standalone files in `dist\`:

- `dist\vive_dongle_gui.exe` — the friendly window (start here).
- `dist\vive_dongle_monitor.exe` — console version with CLI flags.

Copy whichever you want to the SteamVR PC, anywhere you like.

> Why a bundled `.exe`? `openvr`'s wheel already contains `openvr_api.dll`, and
> PyInstaller's `--collect-all openvr` packs it in, so there is nothing to
> download or install on the locked-down machine.

## Run on the SteamVR (Windows) PC — step by step

Your Google Drive → download → double-click plan is exactly right. The `.exe`
**never reaches out to the internet** (it only talks to the local SteamVR
process over local IPC), so the firewall won't block it.

1. On a machine **with internet**, run `build_exe.bat` and grab
   `dist\vive_dongle_gui.exe`.
2. Upload that one file to Google Drive, then on the SteamVR PC download it
   (e.g. to the Desktop).
3. **Start SteamVR** and make sure your trackers are powered on and tracking
   (the usual green icons).
4. **Double-click `vive_dongle_gui.exe`.**
   - Windows SmartScreen may show *"Windows protected your PC"* because the
     `.exe` isn't code-signed. Click **More info → Run anyway**. (This is the
     unsigned-binary warning, not a network/firewall block.)
   - If Windows Firewall ever pops up asking about network access, you can
     safely **Cancel/Deny** — the tool doesn't need the network.
5. In the window, type a **Site** label (e.g. `bay3-floor`) and click
   **▶ Start**. Trackers appear grouped under their dongles.
6. Let it run while operators do a representative motion. To label dongles,
   unplug one and watch the identification panel name it (see above).
7. Click **■ Stop** to finish — a verdict window appears and a timestamped CSV
   + event log are written to a `logs\` folder next to the `.exe`.

Prefer a terminal? Use the console build instead:

```bat
vive_dongle_monitor.exe --site "WarehouseA-bay3" --duration 300
```

- `--site` labels the run (used in log filenames and the CSV).
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
| `vive_dongle_gui.py` | the GUI front-end (tkinter, stdlib) |
| `vive_dongle_monitor.py` | the console front-end (CLI flags, scriptable) |
| `vive_rf_core.py` | shared engine: OpenVR sampling, stats, logging, verdict |
| `build_exe.bat` | build both `.exe`s on an internet machine |
| `run_from_source.bat` | run from source on a dev/test PC |
| `requirements.txt` | `openvr` (runtime) + `pyinstaller` (build) |
| `docs/gui_preview.png` | screenshot used in this README |

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
