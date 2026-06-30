#!/usr/bin/env python3
"""Connection probe for an Insta360 ONE X over its WiFi access point.

Run this on the Raspberry Pi AFTER joining the camera's WiFi (the camera is then
reachable at 192.168.42.1). It uses only the Python standard library, so there is
nothing to pip-install.

It checks whether the camera answers the Open Spherical Camera (OSC) HTTP API,
which is the documented control path for the ONE X:

    GET  /osc/info   -> manufacturer / model / firmware / serial
    POST /osc/state  -> battery + capture state

and reports clearly whether OSC control looks usable. It does NOT record anything.

    python3 recorder/probe_camera.py
    python3 recorder/probe_camera.py --host 192.168.42.1
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

DEFAULT_HOST = "192.168.42.1"
TIMEOUT_SECONDS = 5


def _get(url: str) -> dict:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json;charset=utf-8",
            "X-XSRF-Protected": "1",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def probe(host: str) -> bool:
    base = f"http://{host}/osc"
    print(f"Probing Insta360 camera at {host} via the OSC HTTP API ...\n")

    try:
        info = _get(f"{base}/info")
    except urllib.error.URLError as exc:
        print(f"FAIL: could not reach {base}/info ({exc.reason}).")
        print("Are you joined to the camera's WiFi (192.168.42.x)? Is the camera on?")
        return False
    except Exception as exc:
        print(f"FAIL: unexpected error contacting the camera: {exc!r}")
        return False

    print("OK: camera answered /osc/info")
    for key in ("manufacturer", "model", "serialNumber", "firmwareVersion", "apiLevel"):
        if key in info:
            print(f"  {key}: {info[key]}")

    try:
        state = _post(f"{base}/state")
        camera_state = state.get("state", {})
        print("\nOK: camera answered /osc/state")
        if "fingerprint" in state:
            print(f"  fingerprint: {state['fingerprint']}")
        for key in ("batteryLevel", "_captureStatus", "captureStatus", "storageUri"):
            if key in camera_state:
                print(f"  {key}: {camera_state[key]}")
    except Exception as exc:
        print(f"\nWARN: /osc/state failed ({exc!r}). /osc/info worked, so OSC is partly reachable.")

    print("\nResult: OSC HTTP API is reachable — start/stop recording should be possible.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"camera IP (default {DEFAULT_HOST})")
    args = parser.parse_args()
    return 0 if probe(args.host) else 1


if __name__ == "__main__":
    raise SystemExit(main())
