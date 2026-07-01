#!/usr/bin/env python3
"""Continuous recording session for the Insta360 ONE X.

A mouse-button press starts recording; the next press stops it. Each finished
clip's two .mp4 files are downloaded into one folder (clip_<timestamp>/) and
uploaded to Google Drive with rclone, keeping the pair together. Download and
upload run in a background thread so the next clip can start immediately. The
program runs until Ctrl+C (then it waits for pending uploads to finish).

Run from the repo root:

    python3 -m recorder.record_session
    python3 -m recorder.record_session --keep-local
    python3 -m recorder.record_session --remote gdrive --remote-path 360-footage

Prerequisites on the Pi:
    sudo apt-get install -y python3-evdev rclone
    rclone config                              # create a Google Drive remote (see README)
    python3 recorder/connect_camera_wifi.py    # join the camera WiFi
"""

from __future__ import annotations

import argparse
import datetime
import queue
import shutil
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path

from evdev import ecodes

from recorder.button import acquire_button_device, wait_for_presses
from recorder.camera_osc import OneXCamera, OscError
from recorder.status_leds import ReadinessMonitor, StatusLeds
from recorder.uploader import require_rclone, upload_worker


# require_rclone and upload_worker now live in recorder/uploader.py (shared with photo_session).


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="192.168.42.1", help="camera IP (default 192.168.42.1)")
    parser.add_argument("--remote", default="gdrive", help="rclone remote name (default gdrive)")
    parser.add_argument("--remote-path", default="360-footage", help="folder on the remote (default 360-footage)")
    parser.add_argument("--staging", default="~/360-clips", help="local folder for clips before upload")
    parser.add_argument("--device", help="input device path, e.g. /dev/input/event4")
    parser.add_argument("--key", default="BTN_LEFT", help="button to listen for (default BTN_LEFT)")
    parser.add_argument("--keep-local", action="store_true", help="keep the local copy after upload")
    parser.add_argument("--no-leds", action="store_true", help="run without the status LEDs")
    args = parser.parse_args()

    require_rclone(args.remote)

    key_code = ecodes.ecodes.get(args.key)
    if key_code is None:
        sys.exit(f"Unknown key name {args.key!r}. Examples: BTN_LEFT, BTN_RIGHT, KEY_ENTER.")

    camera = OneXCamera(args.host)
    info = None
    try:
        info = camera.get_info()
    except urllib.error.URLError:
        pass
    if info:
        print(f"Camera: {info.get('model', '?')} (firmware {info.get('firmwareVersion', '?')})")
    else:
        print(
            f"Camera not reachable at {args.host} yet — the session keeps looking every ~3 s "
            "(red LED). Join the camera WiFi; recording start is blocked until it is found. Ctrl+C to quit."
        )

    leds = StatusLeds(enabled=not args.no_leds)
    monitor = ReadinessMonitor(leds, camera)
    monitor.camera_ok = info is not None
    monitor.start()

    staging = Path(args.staging).expanduser()
    staging.mkdir(parents=True, exist_ok=True)

    jobs: queue.Queue = queue.Queue()
    worker = threading.Thread(
        target=upload_worker, args=(jobs, camera, staging, args.remote, args.remote_path, args.keep_local), daemon=True
    )
    worker.start()

    for leftover in sorted(p for p in staging.glob("clip_*") if p.is_dir() and any(p.iterdir())):
        print(f"Found leftover clip {leftover.name} from a previous run — queuing for upload.")
        jobs.put((leftover.name, []))

    recording = False
    start_time = datetime.datetime.now()

    def handle_press() -> None:
        nonlocal recording, start_time
        try:
            if not recording:
                if not monitor.camera_ok:
                    print("Camera not reachable — can't start recording. (Red light: check the camera WiFi.)")
                    return
                camera.set_video_mode()
                camera.start_capture()
                recording = True
                start_time = datetime.datetime.now()
                leds.set_recording(True)
                print("● Recording... (press again to stop)")
            else:
                file_urls = camera.stop_capture()
                recording = False
                leds.set_recording(False)
                clip_name = "clip_" + start_time.strftime("%Y%m%d_%H%M%S")
                if file_urls:
                    jobs.put((clip_name, file_urls))
                    print(f"■ Stopped — queued {clip_name} ({len(file_urls)} files) for download + upload.")
                else:
                    print("■ Stopped, but the camera returned no files.")
        except OscError as exc:
            print(f"Camera error: {exc}")
        except urllib.error.URLError as exc:
            print(f"Lost camera connection ({exc.reason}). Is the Pi still on the camera WiFi?")

    print("\nReady. Press the mouse button to START, press again to STOP. Ctrl+C to quit.\n")

    try:
        while True:
            device = acquire_button_device(key_code, args.device)
            print(f"Button: {device.name!r} at {device.path}, key {args.key}")
            try:
                for _ in wait_for_presses(device, key_code):
                    handle_press()
            except OSError as exc:
                print(f"Lost the button device ({exc}) — looking for it again (retry ~3 s).")
                continue
    except KeyboardInterrupt:
        print("\nCtrl+C — stopping.")
        leds.set_recording(False)
        if recording:
            try:
                file_urls = camera.stop_capture()
                if file_urls:
                    clip_name = "clip_" + start_time.strftime("%Y%m%d_%H%M%S")
                    jobs.put((clip_name, file_urls))
                    print(f"Saved final clip {clip_name}.")
            except (OscError, urllib.error.URLError) as exc:
                print(f"(could not stop the camera cleanly: {exc})")
    finally:
        monitor.stop()
        jobs.put(None)
        print("Finishing the current upload and any queued clips — please wait. (Ctrl+C again to abandon; local files are always kept.)")
        try:
            worker.join()
        except KeyboardInterrupt:
            print("Abandoned pending work. Any downloaded files remain in the staging folder.")
        leds.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
