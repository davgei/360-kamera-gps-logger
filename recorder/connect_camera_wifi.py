#!/usr/bin/env python3
"""Connect the Pi to the Insta360 camera's WiFi, asking for the password in the
terminal. The password is typed at runtime and is never stored in the repo.

Uses NetworkManager (nmcli). The camera connection is set to NOT become the
default route, so the Pi keeps internet/TeamViewer on ethernet (eth0) while it
talks to the camera over WiFi (camera is then reachable at 192.168.42.1).

    python3 recorder/connect_camera_wifi.py              # auto-detect "ONE X ..." network
    python3 recorder/connect_camera_wifi.py --ssid "ONE X 123456"
    python3 recorder/connect_camera_wifi.py --list       # scan and list nearby networks
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from getpass import getpass

CAMERA_IP = "192.168.42.1"


def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def require_nmcli() -> None:
    if shutil.which("nmcli") is None:
        sys.exit(
            "nmcli (NetworkManager) not found. It is the default on Raspberry Pi OS Bookworm; "
            "otherwise install it with: sudo apt-get install -y network-manager"
        )


def scan_ssids() -> list[str]:
    result = _run(["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "yes"])
    ssids: list[str] = []
    for line in result.stdout.splitlines():
        name = line.strip()
        if name and name not in ssids:
            ssids.append(name)
    return ssids


def pick_ssid(explicit: str | None) -> str:
    if explicit:
        return explicit
    candidates = [s for s in scan_ssids() if "one x" in s.lower()]
    if len(candidates) == 1:
        answer = input(f'Found camera network "{candidates[0]}". Use it? [Y/n] ').strip().lower()
        if answer in ("", "y", "yes"):
            return candidates[0]
    elif candidates:
        print("Camera-like networks found:")
        for name in candidates:
            print(f"  - {name}")
    ssid = input("Enter the camera WiFi name (SSID): ").strip()
    if not ssid:
        sys.exit("No SSID given.")
    return ssid


def connect(ssid: str, password: str) -> None:
    print(f'Connecting to "{ssid}" ...')
    result = _run(["nmcli", "device", "wifi", "connect", ssid, "password", password])
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        lowered = message.lower()
        if "not authorized" in lowered or "permission" in lowered:
            sys.exit(f"Not authorized to manage WiFi. Try: sudo python3 {sys.argv[0]}")
        if "secrets were required" in lowered or "802-11-wireless-security" in lowered:
            sys.exit("Connection failed — wrong password?")
        if "no network with ssid" in lowered or "not found" in lowered:
            sys.exit(
                f'Network "{ssid}" not found. If the camera is on 5 GHz (channel 36), the Pi hides '
                "5 GHz until the WiFi country is set: sudo raspi-config nonint do_wifi_country NO"
            )
        sys.exit(f"Connection failed: {message}")

    # Keep the default route on ethernet so internet/TeamViewer stay up.
    _run(["nmcli", "connection", "modify", ssid, "ipv4.never-default", "yes", "ipv6.never-default", "yes"])
    _run(["nmcli", "connection", "up", ssid])
    print(f"Connected. Camera should be reachable at {CAMERA_IP}.")
    print("Next: python3 recorder/probe_camera.py")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ssid", help="camera WiFi name (skips auto-detect/prompt)")
    parser.add_argument("--list", action="store_true", help="scan and list nearby WiFi networks, then exit")
    args = parser.parse_args()

    require_nmcli()

    if args.list:
        for ssid in scan_ssids():
            print(ssid)
        return 0

    ssid = pick_ssid(args.ssid)
    password = getpass(f'Password for "{ssid}": ')
    if not password:
        sys.exit("No password given.")
    connect(ssid, password)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
