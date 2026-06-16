#!/usr/bin/env python3
"""Read-only SteamVR / Lighthouse diagnostic probe.

Discovers what the deeper SteamVR channels expose on a rig WITHOUT changing
anything on the devices, and captures the read-only per-device stats
(usbstats / sensorcheck / errors / ...). Run it once on a problem site and send
back the output file.

The lighthouse_console interaction lives in the shared, whitelisted
`lighthouse_stats` module (single source of truth for read-only commands). This
script adds the CLI, an optional experimental DriverDebugRequest pass, and a
keep-the-window-open wrapper.

Usage
-----
    steamvr_probe.exe                 # capture read-only lighthouse stats
    steamvr_probe.exe --console PATH   # point at a specific lighthouse_console
    steamvr_probe.exe --driver-debug   # also try OpenVR DriverDebugRequest
"""
import argparse
import datetime
import os
import sys

# Allow running from tools/ as a script or as a frozen exe.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lighthouse_stats as lh  # noqa: E402

# Optional, experimental, read-ish strings for IVRSystem::DriverDebugRequest.
# The lighthouse driver decides what these mean; unknown ones typically return
# empty. Kept minimal and informational on purpose.
DRIVER_DEBUG_CANDIDATES = ["GetSerialNumber", "version", "stats", "debug"]


def _base_dir():
    """Directory to write output into: next to the exe when frozen, else here."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def probe_driver_debug():
    """Optional: try IVRSystem::DriverDebugRequest on each tracked device."""
    lines = []
    try:
        import openvr
    except Exception as exc:
        return f"[probe] openvr not importable: {exc}\n"
    vr = None
    try:
        vr = openvr.init(openvr.VRApplication_Background)
        for idx in range(openvr.k_unMaxTrackedDeviceCount):
            cls = vr.getTrackedDeviceClass(idx)
            if cls == openvr.TrackedDeviceClass_Invalid:
                continue
            serial = ""
            try:
                serial = vr.getStringTrackedDeviceProperty(
                    idx, openvr.Prop_SerialNumber_String)
            except Exception:
                pass
            lines.append(f"\n-- device {idx} class={cls} serial={serial} --")
            for req in DRIVER_DEBUG_CANDIDATES:
                try:
                    resp = vr.driverDebugRequest(idx, req, 4096)
                except Exception as exc:
                    resp = f"<error: {exc}>"
                lines.append(f"  driverDebugRequest({req!r}) -> {resp!r}")
    except Exception as exc:
        lines.append(f"[probe] openvr init/enumerate failed: {exc}")
    finally:
        try:
            if vr is not None:
                openvr.shutdown()
        except Exception:
            pass
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", help="path to lighthouse_console.exe")
    ap.add_argument("--driver-debug", action="store_true",
                    help="also try OpenVR DriverDebugRequest (experimental)")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    out = ["SteamVR / Lighthouse read-only probe",
           f"generated: {datetime.datetime.now().isoformat()}",
           f"platform : {sys.platform}"]
    if sys.platform != "win32":
        out.append("[probe] note: lighthouse_console is Windows-only; "
                   "run this on the rig for real output.")

    out.append("\n=== Steam roots searched ===")
    out.extend(lh.steam_roots())

    console = lh.find_lighthouse_console(args.console)
    out.append("\n=== lighthouse_console ===")
    if console:
        out.append(f"found: {console}")
        out.append("--- output ---")
        out.append(lh.run_capture(console, args.timeout))
    else:
        out.append("NOT FOUND. Pass --console <path> if SteamVR is installed "
                   "somewhere unusual.")

    out.append("\n=== DriverDebugRequest ===")
    out.append(probe_driver_debug() if args.driver_debug
               else "skipped (pass --driver-debug to enable)")

    text = "\n".join(out) + "\n"
    print(text)
    log_dir = os.path.join(_base_dir(), "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(log_dir, f"steamvr_probe_{stamp}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[probe] written to {path}")
    except Exception as exc:
        print(f"[probe] could NOT write output file: {exc}")


def _entry():
    """Surface any crash and keep the window open for double-clicks."""
    try:
        main()
    except Exception:
        import traceback
        print("\n[probe] CRASHED:")
        traceback.print_exc()
    finally:
        if getattr(sys, "frozen", False):
            try:
                input("\nDone. Press Enter to close this window...")
            except Exception:
                pass


if __name__ == "__main__":
    _entry()
