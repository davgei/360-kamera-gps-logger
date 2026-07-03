#!/usr/bin/env python3
"""Kjør hele kjeden på ett ONE X-foto: flat ut + sladd BEGGE utsnitt (yaw0 + yaw180).

Dette er også prosesserings-enheten for ett 3 m-punkt i den kommende pipelinen:
ett råbilde → 2 flate utsnitt → 2 sladdede utsnitt. Måler tiden på hvert steg, så
totalen = tiden for ett punkt (2 utsnitt).

    python3 -m recorder.process_photo                      # bruker foto.jpg
    python3 -m recorder.process_photo --take               # ta et nytt bilde først (kamera-WiFi)
    python3 -m recorder.process_photo --input foto.jpg --flat-size 1920x1920 --scale 640x640
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from recorder.dewarp import flatten_views


def _capture(host: str, dest: Path) -> None:
    from recorder.camera_osc import OneXCamera, OscError

    camera = OneXCamera(host)
    try:
        info = camera.get_info()
    except Exception as exc:
        raise SystemExit(f"Får ikke kontakt med kameraet på {host}: {exc}\n"
                         "Er Pi-en på kameraets WiFi? python3 recorder/connect_camera_wifi.py")
    print(f"Kamera: {info.get('model', '?')} — varmer opp og tar bilde ...")
    camera.warm_up()
    try:
        url = camera.take_picture()
    except OscError as exc:
        raise SystemExit(f"Klarte ikke ta bilde: {exc}")
    camera.download(url, dest)
    print(f"Lagret råbilde: {dest}")


def _blur(views: list[Path], scale: str | None, thresh: float) -> list[Path]:
    """Sladd alle utsnitt i ETT deface-kall (modellen lastes én gang). Returnerer
    de sladdede filene (<navn>_anonymized.jpg ved siden av hvert utsnitt)."""
    cmd = ["deface", "--thresh", str(thresh)]
    if scale:
        cmd += ["--scale", scale]
    cmd += [str(v) for v in views]
    subprocess.run(cmd, check=True)
    return [v.with_name(f"{v.stem}_anonymized.jpg") for v in views]


def main() -> int:
    args = _parse_args()
    for tool in ("ffmpeg", "deface"):
        if shutil.which(tool) is None:
            hint = "sudo apt-get install -y ffmpeg" if tool == "ffmpeg" else "pipx install deface"
            return _fail(f"Fant ikke '{tool}'. Installer: {hint}")

    raw = Path(args.input).expanduser()
    if args.take:
        raw = Path(args.input).expanduser() if args.input else Path(f"foto_{datetime.now().strftime('%Y%m%dT%H%M%S')}.jpg")
        _capture(args.host, raw)
    if not raw.is_file():
        return _fail(f"Fant ikke bilde: {raw}. Ta ett med --take, eller pek på et med --input.")

    print(f"\n1) Flater ut {raw.name} → utsnitt ({args.flat_size}) ...")
    t0 = time.monotonic()
    views = flatten_views(raw, proj=args.proj, out_fov=args.out_fov, flat_size=args.flat_size, quiet=False)
    flatten_s = time.monotonic() - t0

    print(f"\n2) Sladder {len(views)} utsnitt (deface"
          f"{', skala ' + args.scale if args.scale else ', full oppløsning'}) ...")
    t0 = time.monotonic()
    blurred = _blur(views, args.scale, args.thresh)
    blur_s = time.monotonic() - t0

    n = len(views)
    total = flatten_s + blur_s
    print("\n=== Resultat ===")
    print(f"Flatting: {flatten_s:.2f} s  ({flatten_s / n:.2f} s/utsnitt)")
    print(f"Sladding: {blur_s:.2f} s  ({blur_s / n:.2f} s/utsnitt, modell lastet én gang)")
    print(f"Totalt for dette bildet (= ett 3 m-punkt, {n} utsnitt): {total:.2f} s")
    print("Ferdige, sladdede bilder:")
    for path in blurred:
        exists = "✓" if path.is_file() else "✗ (deface skrev ikke fila?)"
        print(f"   {path}   {exists}")
    print("\nSe på dem (sjekk at ansikter er dekket):")
    print("   " + " ; ".join(f"xdg-open {p.name}" for p in blurred))
    return 0


def _fail(message: str) -> int:
    print(message)
    return 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Full kjede på ett foto: flat ut + sladd begge utsnitt")
    parser.add_argument("--input", default="foto.jpg", help="rått dual-fisheye foto (standard foto.jpg)")
    parser.add_argument("--take", action="store_true", help="ta et nytt bilde med kameraet først")
    parser.add_argument("--host", default="192.168.42.1", help="kamera-IP for --take")
    parser.add_argument("--flat-size", default="1920x1920", help="størrelse per utsnitt (standard 1920x1920)")
    parser.add_argument("--scale", default="640x640", help="deface deteksjonsskala WxH (standard 640x640; tom = full)")
    parser.add_argument("--proj", default="pannini", help="projeksjon (standard pannini)")
    parser.add_argument("--out-fov", type=float, default=190.0, help="out-fov per utsnitt (standard 190)")
    parser.add_argument("--thresh", type=float, default=0.2, help="deface deteksjonsterskel (standard 0.2)")
    args = parser.parse_args()
    if args.scale and args.scale.lower() in ("", "none", "full"):
        args.scale = None
    return args


if __name__ == "__main__":
    raise SystemExit(main())
