#!/usr/bin/env python3
"""Toggle start/stop on a USB button press (e.g. a mouse click) — standalone test.

Run on the Raspberry Pi with a USB mouse (or USB button/footswitch) plugged in:

    python3 recorder/button_toggle.py            # auto-detect a mouse button
    python3 recorder/button_toggle.py --list     # list input devices
    python3 recorder/button_toggle.py --device /dev/input/event3 --key BTN_LEFT

Press the button once -> prints "START recording", press again -> "STOP recording",
and so on. This verifies button handling on its own, with no camera involved; the
same toggle later drives the real recording loop.

Prerequisites:
    sudo apt-get install -y python3-evdev
Reading input events needs permission: add your user to the 'input' group
(`sudo usermod -aG input $USER`, then log out/in) or run with sudo for a quick test.
"""

from __future__ import annotations

import argparse
import sys

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:
    sys.exit("python-evdev is not installed. Run: sudo apt-get install -y python3-evdev")


def _open(path: str):
    try:
        return InputDevice(path)
    except (PermissionError, OSError):
        return None


def list_input_devices() -> None:
    found = False
    for path in list_devices():
        device = _open(path)
        if device is None:
            print(f"{path}  (cannot open — permission?)")
            continue
        found = True
        has_button = ecodes.BTN_LEFT in device.capabilities().get(ecodes.EV_KEY, [])
        print(f"{device.path}  {device.name!r}  {'[mouse button]' if has_button else ''}")
    if not found:
        print("No readable input devices. Add your user to the 'input' group or run with sudo.")


def find_mouse_device():
    for path in list_devices():
        device = _open(path)
        if device is not None and ecodes.BTN_LEFT in device.capabilities().get(ecodes.EV_KEY, []):
            return device
    return None


def listen(device: InputDevice, key_code: int, key_name: str) -> None:
    print(f"Listening on {device.path} ({device.name!r}) for {key_name}.")
    print("Press the button to toggle. Ctrl+C to quit.\n")

    recording = False
    for event in device.read_loop():
        if event.type == ecodes.EV_KEY and event.code == key_code and event.value == 1:
            recording = not recording
            print("START recording" if recording else "STOP recording")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list input devices and exit")
    parser.add_argument("--device", help="input device path, e.g. /dev/input/event3")
    parser.add_argument("--key", default="BTN_LEFT", help="button/key to listen for (default BTN_LEFT)")
    args = parser.parse_args()

    if args.list:
        list_input_devices()
        return 0

    key_code = ecodes.ecodes.get(args.key)
    if key_code is None:
        print(f"Unknown key name {args.key!r}. Examples: BTN_LEFT, BTN_RIGHT, KEY_ENTER.")
        return 1

    if args.device:
        device = _open(args.device)
        if device is None:
            print(f"Cannot open {args.device} (permission?). Add user to 'input' group or use sudo.")
            return 1
    else:
        device = find_mouse_device()
        if device is None:
            print("No mouse-like device found. Use --list to see devices, then pass --device.")
            return 1

    try:
        listen(device, key_code, args.key)
    except PermissionError:
        print(f"Permission denied reading {device.path}. Add your user to 'input' group or run with sudo.")
        return 1
    except KeyboardInterrupt:
        print("\nBye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
