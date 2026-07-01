"""Minimal USB-button listener via python-evdev (e.g. a mouse left-click)."""

from __future__ import annotations

import time
from typing import Iterator

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError as exc:
    raise SystemExit("python-evdev is not installed. Run: sudo apt-get install -y python3-evdev") from exc


def _open(path: str) -> InputDevice | None:
    try:
        return InputDevice(path)
    except (PermissionError, OSError):
        return None


def find_button_device(key_code: int = ecodes.BTN_LEFT) -> InputDevice | None:
    for path in list_devices():
        device = _open(path)
        if device is not None and key_code in device.capabilities().get(ecodes.EV_KEY, []):
            return device
    return None


def acquire_button_device(
    key_code: int = ecodes.BTN_LEFT, device_path: str | None = None, retry_interval: float = 3.0
) -> InputDevice:
    """Block until a usable input device is available, retrying every retry_interval seconds.

    Returns an InputDevice once found. Interruptible with Ctrl+C (raises KeyboardInterrupt).
    """
    announced = False
    while True:
        device = _open(device_path) if device_path else find_button_device(key_code)
        if device is not None:
            return device
        if not announced:
            target = device_path or "a mouse/button"
            print(f"Waiting for {target} to be plugged in (retry every ~{int(retry_interval)} s). Ctrl+C to quit.")
            announced = True
        time.sleep(retry_interval)


def wait_for_presses(device: InputDevice, key_code: int) -> Iterator[None]:
    """Yield once per button-down event for the given key code (blocks between events)."""
    for event in device.read_loop():
        if event.type == ecodes.EV_KEY and event.code == key_code and event.value == 1:
            yield None
