#!/usr/bin/env python3
"""Photo session for the Insta360 ONE X — a mouse-button click takes ONE still photo,
which is downloaded and uploaded to Google Drive. Runs until Ctrl+C.

Step 1 of the photo pivot: the mouse click stands in for the (later) GPS trigger, and
each click = one photo. No face blur yet (Step 2) and no GPS yet (Step 3).

    python3 -m recorder.photo_session
    python3 -m recorder.photo_session --keep-local --remote gdrive --remote-path 360-photos

Prerequisites on the Pi:
    sudo apt-get install -y python3-evdev rclone python3-gpiozero python3-lgpio
    rclone config                              # Google Drive remote (see README)
    python3 recorder/connect_camera_wifi.py    # join the camera WiFi
"""

from __future__ import annotations

import argparse
import datetime
import queue
import sys
import threading
import urllib.error
from pathlib import Path

from evdev import InputDevice, ecodes

from recorder.button import find_button_device, wait_for_presses
from recorder.camera_osc import OneXCamera, OscError
from recorder.status_leds import ReadinessMonitor, StatusLeds
from recorder.uploader import require_rclone, upload_worker


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="192.168.42.1", help="camera IP (default 192.168.42.1)")
    parser.add_argument("--remote", default="gdrive", help="rclone remote name (default gdrive)")
    parser.add_argument("--remote-path", default="360-photos", help="folder on the remote (default 360-photos)")
    parser.add_argument("--staging", default="~/360-photos", help="local folder for photos before upload")
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
    try:
        info = camera.get_info()
    except urllib.error.URLError as exc:
        sys.exit(
            f"Cannot reach the camera at {args.host} ({exc.reason}). "
            "Join the camera WiFi first: python3 recorder/connect_camera_wifi.py"
        )
    print(f"Camera: {info.get('model', '?')} (firmware {info.get('firmwareVersion', '?')})")
    try:
        camera.set_image_mode()
    except (OscError, urllib.error.URLError) as exc:
        print(f"Warning: could not set image mode now ({exc}); will still try to shoot.")

    device = InputDevice(args.device) if args.device else find_button_device(key_code)
    if device is None:
        sys.exit("No button device found. Plug in the mouse, or pass --device (see: python3 recorder/button_toggle.py --list).")
    print(f"Button: {device.name!r} at {device.path}, key {args.key}")

    leds = StatusLeds(enabled=not args.no_leds)
    monitor = ReadinessMonitor(leds, camera)
    monitor.camera_ok = True  # camera was just verified reachable via get_info()
    monitor.start()

    staging = Path(args.staging).expanduser()
    staging.mkdir(parents=True, exist_ok=True)

    jobs: queue.Queue = queue.Queue()
    worker = threading.Thread(
        target=upload_worker, args=(jobs, camera, staging, args.remote, args.remote_path, args.keep_local), daemon=True
    )
    worker.start()

    for leftover in sorted(p for p in staging.glob("photo_*") if p.is_dir() and any(p.iterdir())):
        print(f"Found leftover {leftover.name} from a previous run — queuing for upload.")
        jobs.put((leftover.name, []))

    print("\nReady. Click the mouse button to take a photo. Ctrl+C to quit.\n")

    try:
        for _ in wait_for_presses(device, key_code):
            if not monitor.camera_ok:
                print("Camera not reachable — can't take a photo. (Red light: check the camera WiFi.)")
                continue
            try:
                leds.set_recording(True)
                print("📷 Taking photo ...")
                file_url = camera.take_picture()
                name = "photo_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                if file_url:
                    jobs.put((name, [file_url]))
                    print(f"Captured {name} — queued for download + upload.")
                else:
                    print("Photo taken, but the camera returned no file URL.")
            except OscError as exc:
                print(f"Camera error: {exc}")
            except urllib.error.URLError as exc:
                print(f"Lost camera connection ({exc.reason}). Is the Pi still on the camera WiFi?")
            finally:
                leds.set_recording(False)
    except KeyboardInterrupt:
        print("\nCtrl+C — stopping.")
    finally:
        leds.set_recording(False)
        monitor.stop()
        jobs.put(None)
        print("Finishing pending downloads/uploads — please wait. (Ctrl+C again to abandon; local files are always kept.)")
        try:
            worker.join()
        except KeyboardInterrupt:
            print("Abandoned pending work. Any downloaded files remain in the staging folder.")
        leds.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
