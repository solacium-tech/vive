# Vive Tracker Link Monitor: User Guide

A field tool for checking the health of HTC Vive tracker links during a
session. While your trackers are running it answers one question: **is anything
wrong with the radio (dongle) or optical (lighthouse) link right now, and which
dongle is to blame?** It also saves a report you can compare from one run to the
next.

> This is a standalone guide meant to be handed out. It covers running a
> session, reading the results, and the files the tool produces.

## 1. How it works (in one paragraph)

The tool attaches to **SteamVR** (via OpenVR) and polls every tracked device
about 250 times a second. For each tracker it measures two **independent**
failure modes, and never lets them overlap:

- **Radio link** is the wireless link between a tracker and its **USB dongle**.
  It counts as lost when the device reports disconnected, or when its input
  packet counter stalls. A radio problem points at the **dongle** or its
  placement.
- **Lighthouse (optical)** is the line of sight between a tracker and the **base
  stations**. It counts (only while the radio link is up) when the tracker is
  connected but cannot get a clean optical fix: out of range, running on its IMU
  only (position lost), or reporting an invalid pose. A foot tracker covered by
  clothing is a typical cause. An optical problem points at **occlusion or
  range** to the base stations, not the dongle.

Each tracker is mapped to the dongle it is paired with, so results can be
aggregated **per dongle**, which is what makes a single bad dongle obvious.

## 2. Requirements

- A **Windows PC** with **SteamVR installed**.
- **SteamVR running and active**, meaning a headset is detected or a
  Null/headless driver is enabled. The app talks to SteamVR, not to the
  trackers directly.
- The **Vive trackers powered on** (the trackers themselves, not just the
  headset) with their **USB dongles plugged in**.
- No network connection, accounts, or extra drivers are required.

> **If Windows blocks the download** (SmartScreen or Smart App Control): the
> `.exe` is unsigned. Right click it, choose **Properties**, tick **Unblock**,
> click **OK**, or briefly turn Smart App Control off, then launch it again.

When the app opens, the **title bar** ends with `build <date>` (for example
`build 2026-06-10h`). Quote that build when reporting anything, so it is clear
which version produced a result.

## 3. Run a session

### Step 1: launch and enter a location label

Double click `vive_dongle_gui.exe`. You will see the idle screen.

*(Screenshot: `01-start.png`, the start screen.)*

1. Type a **location label** in the box at the top (for example `bay3-floor`).
   It is required, and it names the report files so different runs and
   locations can be told apart and compared.
2. Click **Start monitoring** (green, top right).

The state chip changes from `IDLE` to `CONNECTING` to **`MONITORING`** once it
has attached to SteamVR. If SteamVR is not up yet, the app retries for a few
seconds and then shows a dialog telling you exactly what to fix.

### Step 2: read the live view

Every tracker appears grouped under the **dongle it is paired with**, with two
verdicts side by side: **Radio link (to dongle)** and **Lighthouse (line of
sight)**. Each shows the percentage of time lost, plus radio drops, update rate,
battery and current link state.

*(Screenshot: `02-live-healthy.png`, everything healthy, with a green banner.)*

*(Screenshot: `03-live-problem.png`, a failing dongle. The trackers on one
dongle are red with many drops while the others stay green. The banner names the
failing dongle and the Event log timestamps every drop and reconnect.)*

The colour, and which **side** it is on, tells you where to look:

| Colour | Meaning |
| ------ | ------- |
| 🟢 Green | Healthy |
| 🟠 Amber | Degraded, keep an eye on it |
| 🔴 Red | Failing, needs attention |

- A problem in the **Radio link** column points at the **dongle** or its
  placement.
- A problem in the **Lighthouse** column points at **line of sight** to the
  base stations.

Let the session run long enough to be representative. Move the rig through the
poses and the area you actually care about before stopping.

> **Watch the Event log for "NOT UPDATING" and "WIRELESS DROP".** If a tracker
> stays connected but its pose stops changing, the app raises a `NOT UPDATING`
> alert. That usually means the radio link is starved rather than cleanly
> dropped, which points at **2.4GHz interference** (for example dongles plugged
> straight into the workstation or a USB 3.0 hub). `WIRELESS DROP` is SteamVR's
> own dongle-level "lost the radio link" signal for that tracker. Both are
> strong hints to move that dongle onto an extension, away from the PC.

> **Live figures are "right now", not the whole run.** The colours and the
> "% lost" in the table reflect roughly the **last 30 seconds**, so a tracker
> moved back into range clears from red to green within about 30 seconds instead
> of staying flagged all session. The **saved report** uses the **full session**
> figures, which is what you want for comparing runs.

### Step 3: stop and save the report

Click **Stop and save report** (red, top right). The app:

1. Confirms on screen (a green banner plus a pop up) that the report was saved.
2. Opens a **summary window** with the full conclusion.

*(Screenshot: `04-summary.png`, the run summary.)*

The summary leads with a colour coded verdict, ranks the **dongles worst radio
link first**, and gives a concrete **recommendation** when one dongle clearly
stands out (for example, raise that dongle 1.5 to 2 m on a USB extension, clear
of the floor, metal and USB 3.0 ports, then re-run and compare).

## 4. Two features worth knowing

### Hide devices that aren't part of the run

Some devices show up in SteamVR but aren't what you are measuring. The common
case is **controllers used only for room setup** and then switched off, which
would otherwise sit in the table as **DOWN** for the whole session and skew the
numbers.

**Right click the device's row and choose "Hide".** A hidden device is:

- removed from the table,
- excluded from the live stats, the per dongle aggregates, the per second CSV
  **and the saved report**, and
- ignored by the "not in use" pause check (below), so a powered off controller
  cannot trip it.

A **"Hidden devices: N"** link appears under the colour legend; click it to
bring them all back. Hiding applies to the current session.

### "Not in use" is not a fault

If the **headset is switched off or taken off your head**, or **all (non hidden)
trackers are powered down**, the app treats it as the operator stopping, not a
radio fault. The state chip shows **`PAUSED (not in use)`**, the banner goes
grey, and that idle time is **not counted** against the loss figures and raises
**no drop alarms**. It resumes automatically the moment the headset goes back on
or a tracker powers up.
A brief grace period ignores momentary blips, but counting stops immediately so
the pause never pollutes the numbers.

This is why the recommended workflow is: start the run with everything on, do
your room setup, switch the setup controllers off and **Hide** them, then carry
on. The report will then reflect only the trackers you care about, for only the
time they were actually in use.

## 5. Naming your trackers (optional)

Trackers are identified by serial (for example `LHR-9F8E7D01`). To show friendly
names instead, the tool writes a plain text **`tracker_names.txt`** next to the
executable the first time it sees your trackers.

It is a normal text file, so **double click to open it in Notepad** (no Excel
and no Microsoft login). Put one device per line as `SERIAL = friendly name`:

```
# Tracker names. Edit this in Notepad and save.
# One device per line:   SERIAL = friendly name
LHR-9F8E7D01 = left-foot
LHR-2A41F0AB = right-foot
```

Save it, and the names appear in the table, the report and the CSV on the next
run. Lines starting with `#` are ignored, and a comma, tab or colon works as the
separator too. (An older `tracker_names.csv`, if present, is still read.)

## 6. Where the files go

Everything is written to a **`logs`** folder created next to the executable. Use
the **Open report** and **Open reports folder** buttons in the status bar.

| File | What it is |
| ---- | ---------- |
| `<label>_<timestamp>_report.txt` | The same content as the summary window, in plain text. |
| `<label>_<timestamp>_series.csv` | One row per tracker per second, for Excel, pandas, Grafana, and so on. |
| `<label>_<timestamp>_events.log` | Every radio drop and reconnect with a full timestamp. |

### CSV columns

The `*_1s` columns are values for that one second interval (what time series
tools want); the `*_total` columns are cumulative since the run started.

| Column | Meaning |
| ------ | ------- |
| `timestamp`, `epoch_s` | ISO time and Unix epoch for the row. |
| `site` | The location label you entered. |
| `tracker`, `tracker_name` | Serial and the friendly name (if set). |
| `model`, `dongle` | Tracker model and the paired dongle ID. |
| `connected` | 1 if the radio link was up at sample time, else 0. |
| `rf_down_pct_1s` | Percentage of this second the radio link was down. |
| `optical_oor_pct_1s` | Percentage of this second connected but out of range. |
| `drops_1s`, `stalls_1s` | Radio drops and packet stalls in this second. |
| `rf_loss_pct_total` | Cumulative radio loss percentage for the run. |
| `optical_loss_pct_total` | Cumulative optical (out of range) loss percentage. |
| `drops_total`, `stalls_total` | Cumulative radio drops and stalls. |
| `longest_disconnect_s` | Longest single radio dropout so far. |
| `update_hz` | Input rate; blank for body trackers (see note below). |
| `battery_pct` | Battery level, if reported. |
| `not_updating_pct_total` | % of connected time the pose was frozen (see note). |
| `not_updating_events_total` | How many times the pose froze while connected. |
| `wireless_drops_total` | Dongle-level wireless link drops reported by SteamVR. |

> **About `update_hz`.** It is derived from the device's **input** packet
> counter, which only advances when something is wired into the tracker's
> accessory pins (buttons, a trigger, and so on). A bare Vive tracker sends no
> input, so this stays blank even while it is tracking perfectly. For that
> reason it is kept in the CSV only and is **not** shown in the live table, and
> it never affects the radio or lighthouse figures, which come from the
> connection state rather than this counter.

## 7. Acting on the results

- **One dongle far worse than the rest** means a placement problem on that
  dongle. Raise it on a USB extension, about 1.5 to 2 m up, away from the floor,
  metal, and USB 3.0 ports and cables, then run again and compare the radio loss
  percentages.
- **Radio healthy but lighthouse red or amber** is not a dongle issue. Check
  **line of sight** to the base stations (occlusion, range, reflective
  surfaces).
- **Everything green** means the links were solid for that session. Keep the
  saved CSV as your baseline for the next comparison.

## 8. Quick troubleshooting

| Symptom | What to do |
| ------- | ---------- |
| "Start monitoring" then an error dialog | SteamVR isn't running or active. Start SteamVR (a headset detected or a Null driver) and try again. |
| A tracker sits as **DOWN** all session | If it is not part of the run (for example a setup controller), right click it and choose **Hide**. Otherwise it is a genuine radio drop, so check that dongle. |
| Windows blocked the `.exe` | Right click it, choose Properties, tick **Unblock**, or disable Smart App Control briefly. |
| `tracker_names.txt` opens in Excel | It shouldn't, because it is a `.txt`. If your PC is set to open `.txt` in Excel, right click it, choose Open with, then **Notepad**. |
| Everything reads as paused or "not in use" | The headset is off or all trackers are powered down. Put the headset on or power a tracker, and it resumes automatically. |
