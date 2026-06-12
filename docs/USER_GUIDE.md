# Vive Tracker Link Monitor — User Guide

A field tool for checking the health of HTC Vive tracker links during a
session. It answers one question while your trackers are running: **is anything
wrong with the radio (dongle) or optical (lighthouse) link right now, and which
dongle is to blame?** — and saves a report you can compare run to run.

> This is a self-contained guide intended to be handed out. It covers running a
> session, reading the results, and the files the tool produces.

---

## 1. How it works (in one paragraph)

The tool attaches to **SteamVR** (via OpenVR) and polls every tracked device
~250 times a second. For each tracker it measures two **independent** failure
modes and never lets them overlap:

- **Radio link** — the wireless link between a tracker and its **USB dongle**.
  Counted as lost when the device reports disconnected, or when its input
  packet counter stalls. A radio problem points at the **dongle** or its
  placement.
- **Lighthouse (optical)** — line of sight between a tracker and the **base
  stations**. Counted (only while the radio link is up) when the tracker is
  connected but out of range. An optical problem points at **occlusion or
  range** to the base stations, not the dongle.

Each tracker is mapped to the dongle it is paired with, so results can be
aggregated **per dongle** — which is what makes a single bad dongle obvious.

---

## 2. Requirements

- A **Windows PC** with **SteamVR installed**.
- **SteamVR running and active** — a headset detected, or a Null/headless
  driver enabled. The app talks to SteamVR, not to the trackers directly.
- The **Vive trackers powered on** (the trackers themselves, not just the
  headset) with their **USB dongles plugged in**.
- No network connection, accounts, or extra drivers are required.

> **If Windows blocks the download** (SmartScreen / Smart App Control): the
> `.exe` is unsigned. Right-click it → **Properties** → tick **Unblock** →
> **OK**, or temporarily turn Smart App Control off, then launch it again.

When the app opens, the **title bar** ends with `build <date>` (for example
`build 2026-06-10h`). Quote that build when reporting anything, so it's clear
which version produced a result.

---

## 3. Run a session

### Step 1 — Launch and enter a location label

Double-click `vive_dongle_gui.exe`. You'll see the idle screen.

*(Screenshot: `01-start.png` — the start screen.)*

1. Type a **location label** in the box at the top (for example `bay3-floor`).
   It's required, and it's used to name the report files so different runs and
   locations can be told apart and compared.
2. Click **Start monitoring** (green, top right).

The state chip turns from `IDLE` → `CONNECTING` → **`● MONITORING`** once it has
attached to SteamVR. If SteamVR isn't up yet, the app retries for a few seconds
and then shows a dialog telling you exactly what to fix.

### Step 2 — Read the live view

Every tracker appears grouped under the **dongle it is paired with**, with two
verdicts side by side: **Radio link (to dongle)** and **Lighthouse (line of
sight)**, each showing the percentage of time lost, plus radio drops, update
rate, battery and current link state.

*(Screenshot: `02-live-healthy.png` — everything healthy; banner is green.)*

*(Screenshot: `03-live-problem.png` — a failing dongle. The trackers on one
dongle are red with many drops while the others stay green; the banner names
the failing dongle and the Event log timestamps every drop and reconnect.)*

The colour, and which **side** it's on, tells you where to look:

| Colour | Meaning |
| ------ | ------- |
| 🟢 Green | Healthy |
| 🟠 Amber | Degraded — keep an eye on it |
| 🔴 Red | Failing — needs attention |

- A problem in the **Radio link** column → the **dongle** or its placement.
- A problem in the **Lighthouse** column → **line of sight** to the base
  stations.

Let the session run long enough to be representative — move the rig through the
poses and the area you actually care about — before stopping.

> **Live figures are "right now", not the whole run.** The colours and the
> "% lost" in the table reflect roughly the **last 30 seconds**, so a tracker
> moved back into range clears from red to green within ~30 s instead of
> staying flagged all session. The **saved report** uses the **full-session**
> figures, which is what you want for comparing runs.

### Step 3 — Stop and save the report

Click **Stop and save report** (red, top right). The app:

1. Confirms on screen (green banner + a pop-up) that the report was saved, and
2. Opens a **summary window** with the full conclusion.

*(Screenshot: `04-summary.png` — the run summary.)*

The summary leads with a colour-coded verdict, ranks the **dongles worst radio
link first**, and — when one dongle clearly stands out — gives a concrete
**recommendation** (for example: raise that dongle 1.5–2 m on a USB extension,
clear of the floor, metal and USB 3.0 ports, then re-run and compare).

---

## 4. Two features worth knowing

### Hide devices that aren't part of the run

Some devices show up in SteamVR but aren't what you're measuring — most often
**controllers used only for room setup** and then switched off, which would
otherwise sit in the table as **DOWN** for the whole session and skew the
numbers.

**Right-click the device's row → "Hide".** A hidden device is:

- removed from the table,
- excluded from the live stats, the per-dongle aggregates, the per-second CSV
  **and the saved report**, and
- ignored by the "not in use" pause check (below), so a powered-off controller
  can't trip it.

A **"Hidden devices: N — click to show them again"** link appears under the
colour legend to bring them all back. Hiding applies to the current session.

### "Not in use" is not a fault

If the **headset is switched off** *or* **all (non-hidden) trackers are powered
down**, the app treats it as the operator stopping — not a radio fault. The
state chip shows **`❚❚ PAUSED (not in use)`**, the banner goes grey, and that
idle time is **not counted** against the loss figures and raises **no drop
alarms**. It resumes automatically the moment the headset goes back on or a
tracker powers up. (A brief grace period ignores momentary blips, but counting
stops immediately so the pause never pollutes the numbers.)

This is why the recommended workflow is: start the run with everything on, do
your room setup, switch the setup controllers off and **Hide** them, then carry
on — the report will reflect only the trackers you care about, for only the
time they were actually in use.

---

## 5. Naming your trackers (optional)

Trackers are identified by serial (e.g. `LHR-9F8E7D01`). To show friendly names
instead, the tool writes a plain-text **`tracker_names.txt`** next to the
executable the first time it sees your trackers.

It's a normal text file — **double-click to open it in Notepad** (no Excel and
no Microsoft login). Put one device per line as `SERIAL = friendly name`:

```
# Tracker names - edit this in Notepad and save.
# One device per line:   SERIAL = friendly name
LHR-9F8E7D01 = left-foot
LHR-2A41F0AB = right-foot
```

Save it, and the names appear in the table, the report and the CSV on the next
run. Lines starting with `#` are ignored, and `,`, tab or `:` work as
separators too. (An older `tracker_names.csv`, if present, is still read.)

---

## 6. Where the files go

Everything is written to a **`logs\`** folder created next to the executable.
Use the **Open report** and **Open reports folder** buttons in the status bar.

| File | What it is |
| ---- | ---------- |
| `<label>_<timestamp>_report.txt` | The same content as the summary window, in plain text. |
| `<label>_<timestamp>_series.csv` | One row per tracker per second — for Excel, pandas, Grafana, etc. |
| `<label>_<timestamp>_events.log` | Every radio drop and reconnect with a full timestamp. |

### CSV columns

`*_1s` columns are values for that one-second interval (what time-series tools
want); `*_total` columns are cumulative since the run started.

| Column | Meaning |
| ------ | ------- |
| `timestamp`, `epoch_s` | ISO time and Unix epoch for the row. |
| `site` | The location label you entered. |
| `tracker`, `tracker_name` | Serial and the friendly name (if set). |
| `model`, `dongle` | Tracker model and the paired dongle ID. |
| `connected` | 1 if the radio link was up at sample time, else 0. |
| `rf_down_pct_1s` | % of this second the radio link was down. |
| `optical_oor_pct_1s` | % of this second connected-but-out-of-range. |
| `drops_1s`, `stalls_1s` | Radio drops and packet stalls in this second. |
| `rf_loss_pct_total` | Cumulative radio loss % for the run. |
| `optical_loss_pct_total` | Cumulative optical (out-of-range) loss % for the run. |
| `drops_total`, `stalls_total` | Cumulative radio drops and stalls. |
| `longest_disconnect_s` | Longest single radio dropout so far. |
| `update_hz` | Recent update rate for the tracker. |
| `battery_pct` | Battery level, if reported. |

---

## 7. Acting on the results

- **One dongle far worse than the rest** → a placement problem on that dongle.
  Raise it on a USB extension, **1.5–2 m up**, away from the floor, metal, and
  USB 3.0 ports/cables, then run again and compare the radio-loss percentages.
- **Radio healthy but lighthouse red/amber** → not a dongle issue. Check **line
  of sight** to the base stations (occlusion, range, reflective surfaces).
- **Everything green** → the links were solid for that session. Keep the saved
  CSV as your baseline for the next comparison.

---

## 8. Quick troubleshooting

| Symptom | What to do |
| ------- | ---------- |
| "Start monitoring" then an error dialog | SteamVR isn't running/active. Start SteamVR (headset detected or Null driver) and try again. |
| A tracker sits as **DOWN** all session | If it's not part of the run (e.g. a setup controller), right-click → **Hide**. Otherwise it's a genuine radio drop — check that dongle. |
| Windows blocked the `.exe` | Right-click → Properties → **Unblock**, or disable Smart App Control briefly. |
| `tracker_names.txt` opens in Excel | It shouldn't — it's a `.txt`. If your PC is set to open `.txt` in Excel, right-click → Open with → **Notepad**. |
| Everything reads as paused / "not in use" | The headset is off or all trackers are powered down. Put the headset on or power a tracker; it resumes automatically. |
