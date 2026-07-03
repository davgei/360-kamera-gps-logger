#!/usr/bin/env python3
"""Ta ett stillbilde med ONE X og lagre det RÅTT (dual-fisheye) til fil.

Ingen flatting, sladding eller opplasting — bare råbildet. Nyttig for test/feilsøk
og for å mate dewarp (flatting) og bench_blur (sladdefart). Krever at Pi-en er på
kameraets WiFi (192.168.42.1).

    python3 -m recorder.take_photo                 # lagrer foto_<tid>.jpg
    python3 -m recorder.take_photo -o foto.jpg
    python3 -m recorder.take_photo --no-warmup     # hopp over oppvarmingsskuddet (første skudd blir da tregt)
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from recorder.camera_osc import DEFAULT_HOST, OneXCamera, OscError


def main() -> int:
    parser = argparse.ArgumentParser(description="Ta ett rått ONE X-foto og lagre til fil")
    parser.add_argument("-o", "--out", type=Path, default=None, help="filnavn (standard foto_<tid>.jpg)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="kamera-IP (standard 192.168.42.1)")
    parser.add_argument("--no-warmup", action="store_true", help="hopp over oppvarmingsskuddet")
    args = parser.parse_args()

    out = args.out or Path(f"foto_{datetime.now().strftime('%Y%m%dT%H%M%S')}.jpg")
    camera = OneXCamera(args.host)
    try:
        info = camera.get_info()
        print(f"Kamera: {info.get('model', '?')} (firmware {info.get('firmwareVersion', '?')})")
    except Exception as exc:
        print(f"Får ikke kontakt med kameraet på {args.host}: {exc}")
        print("Er Pi-en på kameraets WiFi? Kjør evt.: python3 recorder/connect_camera_wifi.py")
        return 1

    if not args.no_warmup:
        print("Varmer opp (ett kasteskudd som ikke lagres) ...")
        camera.warm_up()
    print("Tar bilde ...")
    try:
        url = camera.take_picture()
    except OscError as exc:
        print(f"Klarte ikke ta bilde: {exc}")
        return 1

    camera.download(url, out)
    print(f"Lagret råbilde: {out}")
    print("Neste: flat ut med  python3 recorder/dewarp.py "
          f"{out} --proj pannini --out-fov 190  → så sladd utsnittene med deface.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
