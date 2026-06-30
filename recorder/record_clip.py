#!/usr/bin/env python3
"""Record a short video clip on the Insta360 ONE X via the OSC HTTP API.

Run on the Raspberry Pi AFTER it is joined to the camera's WiFi (camera at
192.168.42.1). The sequence is verified against Insta360's official OSC docs:

    setOptions captureMode=video  ->  startCapture  ->  wait  ->  stopCapture

stopCapture returns the recorded file URL(s), which this prints. The ONE X
records two .mp4 tracks per clip, so expect two URLs. Stdlib only — no install.

    python3 recorder/record_clip.py                 # 5-second clip
    python3 recorder/record_clip.py --seconds 10
    python3 recorder/record_clip.py --host 192.168.42.1
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "192.168.42.1"
DEFAULT_SECONDS = 5
TIMEOUT_SECONDS = 10

_HEADERS = {
    "Content-Type": "application/json;charset=utf-8",
    "Accept": "application/json",
    "X-XSRF-Protected": "1",
}


class OscError(Exception):
    pass


def _get(url: str) -> dict:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _execute(host: str, name: str, parameters: dict | None = None) -> dict:
    body: dict = {"name": name}
    if parameters is not None:
        body["parameters"] = parameters
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"http://{host}/osc/commands/execute", data=data, method="POST", headers=_HEADERS
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise OscError(f"{name}: HTTP {exc.code} {exc.reason}")
    if result.get("state") == "error":
        error = result.get("error", {})
        raise OscError(f"{name}: {error.get('code', 'error')} — {error.get('message', '')}")
    return result


def record(host: str, seconds: int) -> list[str]:
    print(f"Setting capture mode to video on {host} ...")
    _execute(host, "camera.setOptions", {"options": {"captureMode": "video"}})

    print("Starting recording ...")
    _execute(host, "camera.startCapture")

    print(f"Recording for {seconds} s ...")
    time.sleep(seconds)

    print("Stopping recording ...")
    result = _execute(host, "camera.stopCapture")
    return result.get("results", {}).get("fileUrls", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"camera IP (default {DEFAULT_HOST})")
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS, help=f"clip length in seconds (default {DEFAULT_SECONDS})")
    args = parser.parse_args()

    try:
        info = _get(f"http://{args.host}/osc/info")
        print(f"Camera: {info.get('model', '?')} (firmware {info.get('firmwareVersion', '?')})\n")
    except urllib.error.URLError as exc:
        print(f"Could not reach the camera at {args.host} ({exc.reason}).")
        print("Is the Pi joined to the camera's WiFi? Run: python3 recorder/connect_camera_wifi.py")
        return 1

    try:
        file_urls = record(args.host, args.seconds)
    except OscError as exc:
        print(f"\nCamera rejected a command: {exc}")
        if "unactivated" in str(exc).lower():
            print("The ONE X must be activated once in the official Insta360 app before the API can record.")
        return 1
    except urllib.error.URLError as exc:
        print(f"\nLost connection to the camera mid-sequence ({exc.reason}).")
        return 1

    if not file_urls:
        print("\nRecording stopped, but the camera returned no file URLs. Check the SD card / camera state.")
        return 1

    print("\nDone. Recorded file(s) on the camera SD card:")
    for url in file_urls:
        print(f"  {url}")
    print("\n(These live on the camera; downloading them to the Pi will be the next step.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
