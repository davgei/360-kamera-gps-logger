#!/usr/bin/env python3
"""Mål hvor raskt Pi-en flater ut + sladder — for å avgjøre om alt-på-Pi får plass i nattevinduet.

Sladdingen (deface) er den tunge, usikre delen. Denne kjører deface på et realistisk antall
bilder (modellen lastes én gang, som i den ekte natt-batchen) og regner ut hvor mange bilder
Pi-en rekker per time/natt — så vi ser om «sladd på Pi, last opp kun sladdet» er praktisk.

Kjør på Pi-en (deface + ffmpeg må være installert):
    python3 -m recorder.bench_blur                      # syntetisk testbilde 2880x2880
    python3 -m recorder.bench_blur --image flatt.jpg    # bruk et ekte flatt bilde (mest representativt)
    python3 -m recorder.bench_blur --raw foto.jpg       # flat ut et rått ONE X-foto først (måler også flatting)
    python3 -m recorder.bench_blur --count 30
    python3 -m recorder.bench_blur --scale 640x360      # rask deteksjon (lavere presisjon på små ansikter)

deface: pipx install deface     ·     ffmpeg: sudo apt-get install -y ffmpeg
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from recorder.dewarp import flatten_views

_FRAME_COUNTS = (1000, 2500, 5000, 10000)


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_synthetic(path: Path, size: str) -> None:
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=1",
          "-frames:v", "1", str(path)])


def _time_deface(images: list[Path], thresh: float, scale: str | None) -> float:
    cmd = ["deface", "--thresh", str(thresh)]
    if scale:
        cmd += ["--scale", scale]
    cmd += [str(p) for p in images]
    start = time.monotonic()
    _run(cmd)
    return time.monotonic() - start


def _prepare_base_image(args: argparse.Namespace, work: Path) -> tuple[Path, float | None]:
    """Returner (flatt bilde å sladde, sekunder brukt på flatting eller None)."""
    if args.image:
        return Path(args.image).expanduser(), None
    if args.raw:
        raw = Path(args.raw).expanduser()
        start = time.monotonic()
        views = flatten_views(raw, proj=args.proj, out_fov=args.out_fov, flat_size=args.size, quiet=True)
        flatten_s = time.monotonic() - start
        return views[0], flatten_s
    synthetic = work / "synthetic.jpg"
    _make_synthetic(synthetic, args.size)
    return synthetic, None


def _report(per_frame_blur: float, flatten_s: float | None, count: int, night_hours: float) -> None:
    per_flat = (flatten_s / 2.0) if flatten_s is not None else None  # flatten lager 2 utsnitt
    total = per_frame_blur + (per_flat or 0.0)
    print("\n=== Resultat ===")
    print(f"CPU-kjerner: {os.cpu_count()}")
    print(f"Sladd (deface): {per_frame_blur:.2f} s per bilde  (snitt over {count}, modell lastet én gang)")
    if per_flat is not None:
        print(f"Flatting (ffmpeg): {per_flat:.2f} s per utsnitt")
    print(f"Per bilde (flat + sladd): ~{total:.2f} s")

    if total <= 0:
        return
    per_hour = 3600.0 / total
    print(f"\n=> ~{per_hour:.0f} bilder/time. På {night_hours:.0f} t natt: ~{per_hour * night_hours:.0f} bilder.")
    print("Bilder per dag (rute i meter ÷ 3) vs. tid Pi-en trenger:")
    for frames in _FRAME_COUNTS:
        hours = frames * total / 3600.0
        fits = "✓ får plass" if hours <= night_hours else "✗ for mye"
        print(f"   {frames:6d} bilder  ->  {hours:5.1f} t   {fits}")
    print("\nTips: --scale 640x360 gjør deface mye raskere (lavere presisjon på små ansikter).")


def main() -> int:
    args = _parse_args()
    for tool in ("deface", "ffmpeg"):
        if shutil.which(tool) is None:
            hint = "pipx install deface" if tool == "deface" else "sudo apt-get install -y ffmpeg"
            return _fail(f"Fant ikke '{tool}'. Installer: {hint}")

    with tempfile.TemporaryDirectory(prefix="bench_blur_") as tmp:
        work = Path(tmp)
        base, flatten_s = _prepare_base_image(args, work)
        if not base.is_file():
            return _fail(f"Fant ikke bilde: {base}")

        copies = [work / f"frame_{i:04d}.jpg" for i in range(args.count)]
        for dst in copies:
            shutil.copyfile(base, dst)

        print(f"Sladder {args.count} kopier av {base.name} "
              f"({'skala ' + args.scale if args.scale else 'full oppløsning'}) ...")
        elapsed = _time_deface(copies, args.thresh, args.scale)
        per_frame_blur = elapsed / args.count
        _report(per_frame_blur, flatten_s, args.count, args.night_hours)
    return 0


def _fail(message: str) -> int:
    print(message)
    return 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark: flatting + ansiktssladding på Pi-en")
    parser.add_argument("--image", default=None, help="et ekte flatt bilde å sladde (mest representativt)")
    parser.add_argument("--raw", default=None, help="rått ONE X dual-fisheye foto — flates ut først (måler flatting)")
    parser.add_argument("--count", type=int, default=20, help="antall bilder å sladde i målingen (standard 20)")
    parser.add_argument("--size", default="2880x2880", help="størrelse på syntetisk/flatt bilde (standard 2880x2880)")
    parser.add_argument("--proj", default="pannini", help="projeksjon for flatting av --raw (standard pannini)")
    parser.add_argument("--out-fov", type=float, default=190.0, help="out-fov for flatting av --raw (standard 190)")
    parser.add_argument("--thresh", type=float, default=0.2, help="deface deteksjonsterskel (standard 0.2)")
    parser.add_argument("--scale", default=None, help="deface deteksjonsoppløsning WxH, f.eks. 640x360 (raskere)")
    parser.add_argument("--night-hours", type=float, default=8.0, help="tilgjengelig nattevindu i timer (standard 8)")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
