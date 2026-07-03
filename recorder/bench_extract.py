#!/usr/bin/env python3
"""Mål hvor fort Pi-en henter ut videobilder — dekodings-fart for uttrekks-steget.

Uttrekket dekoder videoen i én gjennomgang og plukker rammer ved hvert 3 m-punkt.
Kostnaden er i praksis dekodings-farten. Denne måler den på en ekte videofil, og
sier om uttrekket «gjemmer seg» bak prosesseringen (~2,9 s per punkt, 1 utsnitt) —
eller blir en flaskehals (kan skje hvis 5.7K HEVC faller til treg programvare-dekoding).

Kjør på Pi-en, pek på en ekte videofil (fra drive_session/record_clip):
    python3 -m recorder.bench_extract --video ~/360-drives/drive_.../VID_....mp4
    python3 -m recorder.bench_extract --video VID_....mp4 --seconds 20
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_CODEC_RE = re.compile(r"Video:\s*([a-zA-Z0-9]+)")
_PROCESS_S_PER_POINT = 2.9  # flatting + sladding for 1 utsnitt (målt: ~2,9 s)


def _probe(video: Path) -> tuple[float, str]:
    """Hent (varighet i sek, kodek) fra ffmpeg stderr (ffprobe trengs ikke)."""
    proc = subprocess.run(["ffmpeg", "-i", str(video)], capture_output=True, text=True)
    dur = 0.0
    match = _DUR_RE.search(proc.stderr)
    if match:
        hours, minutes, seconds = match.groups()
        dur = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    codec_match = _CODEC_RE.search(proc.stderr)
    return dur, codec_match.group(1) if codec_match else "?"


def _time_decode(video: Path, seconds: float) -> float:
    """Dekod de første `seconds` sekundene (kun video) til /dev/null og mål veggtiden.

    `-map 0:v:0 -an` hopper over lydsporet — ONE X-lyden gir «Invalid number of
    channels» og er uansett irrelevant her; vi måler videodekoding."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-t", str(seconds),
           "-map", "0:v:0", "-an", "-f", "null", "-"]
    start = time.monotonic()
    subprocess.run(cmd, check=True)
    return time.monotonic() - start


def _time_extract(video: Path, seconds: float, fps: float, out_dir: Path) -> tuple[float, int]:
    """Trekk ut rammer ved `fps` fra de første `seconds`, mål tid + antall (inkl. JPEG-skriving)."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-t", str(seconds),
           "-map", "0:v:0", "-an", "-vf", f"fps={fps}", "-q:v", "3", str(out_dir / "f_%04d.jpg")]
    start = time.monotonic()
    subprocess.run(cmd, check=True)
    elapsed = time.monotonic() - start
    return elapsed, len(list(out_dir.glob("f_*.jpg")))


def main() -> int:
    args = _parse_args()
    if shutil.which("ffmpeg") is None:
        print("Fant ikke ffmpeg. Installer: sudo apt-get install -y ffmpeg")
        return 1
    video = Path(args.video).expanduser()
    if not video.is_file():
        print(f"Fant ikke videofil: {video}")
        return 1

    duration, codec = _probe(video)
    bench_s = min(args.seconds, duration) if duration > 0 else args.seconds
    print(f"Video: {video.name}  ·  {duration:.1f} s  ·  kodek {codec}")
    print(f"Måler på de første {bench_s:.0f} s ...\n")

    decode_s = _time_decode(video, bench_s)
    factor = bench_s / decode_s if decode_s > 0 else 0.0

    with tempfile.TemporaryDirectory(prefix="bench_extract_") as tmp:
        extract_s, frames = _time_extract(video, bench_s, args.fps, Path(tmp))
    per_out_frame = extract_s / frames if frames else 0.0

    print("=== Resultat ===")
    print(f"CPU-kjerner: {os.cpu_count()}")
    print(f"Dekoding: {bench_s:.0f} s video på {decode_s:.1f} s  →  {factor:.2f}× sanntid")
    print(f"Uttrekk ({args.fps:g} fps): {frames} rammer på {extract_s:.1f} s  →  {per_out_frame:.2f} s per ramme")

    print(f"\nHva det betyr (prosessering = ~{_PROCESS_S_PER_POINT:g} s per punkt, 1 utsnitt):")
    for kmh in (15, 30, 50):
        speed = kmh / 3.6
        video_per_point = 3.0 / speed
        extract_per_point = video_per_point / factor if factor else float("inf")
        verdict = "gjemmer seg bak prosessering ✓" if extract_per_point < _PROCESS_S_PER_POINT else "blir flaskehals ✗"
        print(f"   {kmh:2d} km/t: ~{extract_per_point:.2f} s uttrekk per 3 m-punkt  →  {verdict}")
    print("\n(Overlapper vi uttrekk med flatting/sladding på hver sin kjerne, teller kun den tregeste.)")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark: videodekoding/uttrekk på Pi-en")
    parser.add_argument("--video", required=True, help="ekte videofil å måle på")
    parser.add_argument("--seconds", type=float, default=30.0, help="hvor mange sekunder å måle på (standard 30)")
    parser.add_argument("--fps", type=float, default=2.0, help="uttrekksrate for måling nr. 2 (standard 2)")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
