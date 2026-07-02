#!/usr/bin/env python3
"""Status LEDs for the recorder, plus a standalone readiness check for debugging.

Three LEDs, BCM GPIO numbering, wired active-high (GPIO -> 330Ω -> LED+ -> LED- -> GND):
    blue  (GPIO 22) : internet connection up (on = online)
    green (GPIO 23) : camera ready (solid); BLINKING = camera ready but battery low
    red   (GPIO 24) : camera NOT ready (not reachable)

Blue (internet) is independent of green/red (camera): e.g. online but no camera -> blue + red.
The LEDs are only INDICATORS — they never start or stop recording.

Standalone debug modes:

    python3 -m recorder.status_leds            # watch readiness + battery, drive the LEDs
    python3 -m recorder.status_leds --test     # light each LED in turn to check the wiring

Needs gpiozero: sudo apt-get install -y python3-gpiozero python3-lgpio
(If your LEDs are wired active-low, create them with LED(pin, active_high=False).)
"""

from __future__ import annotations

import argparse
import socket
import threading
import time

from recorder.camera_osc import OneXCamera

BLUE_PIN = 22
GREEN_PIN = 23
RED_PIN = 24

CAMERA_HOST = "192.168.42.1"
BATTERY_LOW = 0.15  # camera battery fraction (0..1) below which the green LED blinks


def internet_ok(timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=timeout):
            return True
    except OSError:
        return False


def read_camera(camera: OneXCamera) -> tuple[bool, float | None]:
    """Return (reachable, battery_fraction). battery is None if unknown."""
    try:
        state = camera.get_state().get("state", {})
        return True, state.get("batteryLevel")
    except Exception:
        return False, None


class StatusLeds:
    def __init__(self, enabled: bool = True) -> None:
        self._internet = False
        self._camera_ok = False
        self._battery_low = False
        self._green_mode: str | None = None
        self.blue = self.green = self.red = None
        self.enabled = False
        if enabled:
            try:
                from gpiozero import LED

                self.blue = LED(BLUE_PIN)
                self.green = LED(GREEN_PIN)
                self.red = LED(RED_PIN)
                self.enabled = True
            except Exception as exc:
                print(f"[leds] GPIO/LEDs unavailable ({exc}); continuing without LEDs.")
        self._refresh()

    @staticmethod
    def _set(led, on: bool) -> None:
        if led is not None:
            led.on() if on else led.off()

    def _apply_green(self) -> None:
        if not self._camera_ok:
            mode = "off"
        elif self._battery_low:
            mode = "blink"
        else:
            mode = "on"
        if mode == self._green_mode:
            return
        self._green_mode = mode
        if self.green is None:
            return
        if mode == "off":
            self.green.off()
        elif mode == "on":
            self.green.on()
        else:
            self.green.blink(on_time=0.4, off_time=0.4)

    def _refresh(self) -> None:
        self._set(self.blue, self._internet)
        self._set(self.red, not self._camera_ok)
        self._apply_green()

    def set_recording(self, recording: bool) -> None:
        # Blue now shows internet status; recording is no longer shown on an LED.
        return

    def set_status(self, internet: bool, camera_ok: bool, battery_low: bool = False) -> None:
        self._internet = internet
        self._camera_ok = camera_ok
        self._battery_low = battery_low
        self._refresh()

    def close(self) -> None:
        for led in (self.blue, self.green, self.red):
            if led is not None:
                led.off()
                led.close()


class ReadinessMonitor(threading.Thread):
    """Background thread: keeps green/red in sync with readiness and logs the battery."""

    def __init__(self, leds: StatusLeds, camera: OneXCamera, interval: float = 3.0, battery_low: float = BATTERY_LOW) -> None:
        super().__init__(daemon=True)
        self.leds = leds
        self.camera = camera
        self.interval = interval
        self.battery_low = battery_low
        self.ready = False
        self.camera_ok = False
        self.battery: float | None = None
        self._stop = threading.Event()

    def run(self) -> None:
        last_ready: bool | None = None
        last_bucket: int | None = None
        last_cam: bool | None = None
        while not self._stop.is_set():
            inet = internet_ok()
            cam, battery = read_camera(self.camera)
            ready = inet and cam
            low = bool(cam and battery is not None and battery < self.battery_low)
            self.ready = ready
            self.camera_ok = cam
            self.battery = battery
            self.leds.set_status(inet, cam, battery_low=low)

            if cam != last_cam:
                print(f"[status] camera {'found' if cam else 'not reachable'} at {self.camera.host}")
                last_cam = cam
            if ready != last_ready:
                print(f"[status] {'READY' if ready else 'NOT READY'} "
                      f"(internet {'ok' if inet else 'down'}, camera {'ok' if cam else 'down'})")
                last_ready = ready
            if battery is not None:
                bucket = round(battery * 100 / 5) * 5
                if bucket != last_bucket:
                    print(f"[status] camera battery {int(battery * 100)}%" + ("  — LOW, green blinking" if low else ""))
                    last_bucket = bucket

            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


def _test_leds(leds: StatusLeds) -> None:
    for led in (leds.blue, leds.green, leds.red):
        if led is not None:
            led.off()
    for name, led in (("blue (GPIO22)", leds.blue), ("green (GPIO23)", leds.green), ("red (GPIO24)", leds.red)):
        if led is None:
            print(f"{name}: no GPIO — skipped")
            continue
        print(f"{name}: ON")
        led.on()
        time.sleep(1)
        led.off()
    print("LED test done.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=CAMERA_HOST, help="camera IP (default 192.168.42.1)")
    parser.add_argument("--test", action="store_true", help="light each LED in turn, then exit")
    args = parser.parse_args()

    leds = StatusLeds()
    if args.test:
        _test_leds(leds)
        leds.close()
        return 0

    camera = OneXCamera(args.host)
    print("blue = internet · green = camera ready (blinks = battery low) · red = camera not ready. Ctrl+C to quit.\n")
    last = None
    try:
        while True:
            inet = internet_ok()
            cam, battery = read_camera(camera)
            ready = inet and cam
            low = bool(cam and battery is not None and battery < BATTERY_LOW)
            leds.set_status(inet, cam, battery_low=low)
            bucket = None if battery is None else round(battery * 100 / 5) * 5
            if (inet, cam, bucket) != last:
                batt = f"{int(battery * 100)}%" if battery is not None else "?"
                print(f"internet: {'OK' if inet else 'DOWN'}   camera: {'OK' if cam else 'DOWN'}   "
                      f"battery: {batt}   -> {'READY' if ready else 'NOT READY'}" + ("  (LOW BATTERY)" if low else ""))
                last = (inet, cam, bucket)
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        leds.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
