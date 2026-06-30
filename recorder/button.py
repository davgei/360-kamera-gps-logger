"""Minimal USB-button listener via python-evdev (e.g. a mouse left-click)."""

from __future__ import annotations

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


def wait_for_presses(device: InputDevice, key_code: int) -> Iterator[None]:
    """Yield once per button-down event for the given key code (blocks between events)."""
    for event in device.read_loop():
        if event.type == ecodes.EV_KEY and event.code == key_code and event.value == 1:
            yield None
