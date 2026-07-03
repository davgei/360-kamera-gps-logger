#!/usr/bin/env python3
"""Prosesser én kjøre-økt: hent ut bilder fra videoen → flat ut → sladd. (Kjernen i pipelinen.)

To måter å velge rammer på:
  --interval-s 2   ta en ramme hvert 2. sekund av videoen — INGEN GPS (for testing).
  --spacing-m 3    ett bilde per 3 m langs gps_track.csv (den ekte pipelinen).

For hver valgte ramme, fra HVER linse-fil: hent ramma → flat ut (enkelt-fisheye) → sladd
med deface. Sladdede bilder legges i <out>/. Rå video røres ikke her (opplasting + sletting
kommer i kø-tjenesten senere).

    # test uten GPS på en video du har:
    python3 -m recorder.process_drive --video ~/360-drives/drive_.../VID_..._10_...mp4 --interval-s 2

    # hele økta (begge linser) med GPS:
    python3 -m recorder.process_drive --drive ~/360-drives/drive_20260703T071100 --spacing-m 3
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from recorder.dewarp import flatten_views
from recorder.trigger_preview import haversine_m

_FF = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _find_videos(drive_dir: Path) -> list[Path]:
    return sorted(p for p in drive_dir.glob("VID_*.mp4"))


def _probe_duration_s(video: Path) -> float:
    """Videoens lengde i sekunder (fra ffmpeg stderr; 0.0 hvis ukjent)."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(video)], capture_output=True, text=True)
    match = _DUR_RE.search(proc.stderr)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _interval_extract(video: Path, interval_s: float, out_dir: Path) -> list[Path]:
    """Én ffmpeg-gjennomgang: ta ut en ramme hvert interval_s sekund (kun video)."""
    pattern = str(out_dir / f"{video.stem}_%05d.jpg")
    fps = 1.0 / interval_s  # desimal — «1/2.0» er ugyldig for ffmpeg sin fps-parser
    subprocess.run(_FF + ["-i", str(video), "-map", "0:v:0", "-an",
                          "-vf", f"fps={fps:.6g}", "-q:v", "3", pattern], check=True)
    return sorted(out_dir.glob(f"{video.stem}_*.jpg"))


def _seek_extract(video: Path, seconds: list[float], out_dir: Path) -> list[Path]:
    """Hent én ramme per tidspunkt (input-seek — rask, keyframe-nær)."""
    frames: list[Path] = []
    failed = 0
    for idx, t in enumerate(seconds):
        dst = out_dir / f"{video.stem}_{idx:05d}.jpg"
        result = subprocess.run(_FF + ["-ss", f"{t:.3f}", "-i", str(video), "-map", "0:v:0", "-an",
                                       "-frames:v", "1", "-q:v", "3", str(dst)])
        if result.returncode == 0 and dst.is_file():
            frames.append(dst)
        else:
            failed += 1
    if failed:
        print(f"  {failed} rammer kunne ikke hentes (nær videoslutt / keyframe)")
    return frames


def _gps_point_seconds(drive_dir: Path, spacing_m: float, camera_offset_s: float) -> list[float]:
    """Videosekunder for hvert `spacing_m` langs gps_track.csv, via session.json-starttid."""
    try:
        session = json.loads((drive_dir / "session.json").read_text(encoding="utf-8"))
        # video_start er UTC (samme klokke som gps_track.csv sine naive UTC-tider) → naiv subtraksjon.
        video_start = datetime.fromisoformat(session["video_start_utc"]).replace(tzinfo=None)
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"Kunne ikke lese video_start_utc fra {drive_dir / 'session.json'}: {exc}")

    rows: list[tuple[datetime, float, float]] = []
    with (drive_dir / "gps_track.csv").open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                stamp = datetime.fromisoformat(row["timestamp"])
                rows.append((stamp, float(row["lat"]), float(row["lon"])))
            except (ValueError, KeyError):
                continue

    seconds: list[float] = []
    accumulated = spacing_m  # ta med første punkt
    for i, (stamp, lat, lon) in enumerate(rows):
        if i > 0:
            accumulated += haversine_m(rows[i - 1][1], rows[i - 1][2], lat, lon)
        if accumulated >= spacing_m:
            accumulated = 0.0
            video_s = (stamp - video_start).total_seconds() + camera_offset_s
            if video_s >= 0:
                seconds.append(video_s)
    return seconds


def _blur(frames: list[Path], scale: str | None, thresh: float) -> list[Path]:
    """Sladd alle rammer i ETT deface-kall (modell lastet én gang)."""
    if not frames:
        return []
    cmd = ["deface", "--thresh", str(thresh)]
    if scale:
        cmd += ["--scale", scale]
    cmd += [str(f) for f in frames]
    subprocess.run(cmd, check=True)
    return [f.with_name(f"{f.stem}_anonymized.jpg") for f in frames]


def _process_one_video(video: Path, args: argparse.Namespace, gps_seconds: list[float] | None,
                       work: Path, out_dir: Path) -> int:
    if not video.is_file():
        print(f"  hopper over {video.name} (fila finnes ikke)")
        return 0
    print(f"\n— {video.name} —")
    t0 = time.monotonic()
    if gps_seconds is None:
        raw = _interval_extract(video, args.interval_s, work)
    else:
        duration = _probe_duration_s(video)
        times = [t for t in gps_seconds if t <= duration] if duration > 0 else gps_seconds
        dropped = len(gps_seconds) - len(times)
        if dropped:
            print(f"  {dropped} GPS-punkter er etter videoslutt ({duration:.0f} s) — hoppet over")
        raw = _seek_extract(video, times, work)
    print(f"  hentet {len(raw)} rammer på {time.monotonic() - t0:.1f} s")
    if not raw:
        print(f"  ADVARSEL: 0 rammer fra {video.name} — sjekk GPS-tider / videolengde / --camera-offset-s")
        return 0

    t0 = time.monotonic()
    flats: list[Path] = []
    for frame in raw:
        flats += flatten_views(frame, proj=args.proj, out_fov=args.out_fov, views="0",
                               rotate="none", fov=args.fov, flat_size=args.flat_size,
                               input_kind="fisheye", quiet=True)
    print(f"  flatet {len(flats)} utsnitt på {time.monotonic() - t0:.1f} s")

    t0 = time.monotonic()
    blurred = _blur(flats, args.scale, args.thresh)
    print(f"  sladdet {len(blurred)} på {time.monotonic() - t0:.1f} s")

    saved = 0
    for path in blurred:
        if path.is_file():
            shutil.move(str(path), str(out_dir / path.name))
            saved += 1
    return saved


def main() -> int:
    args = _parse_args()
    for tool in ("ffmpeg", "deface"):
        if shutil.which(tool) is None:
            hint = "sudo apt-get install -y ffmpeg" if tool == "ffmpeg" else "pipx install deface"
            return _fail(f"Fant ikke '{tool}'. Installer: {hint}")

    if args.drive:
        drive_dir = Path(args.drive).expanduser()
        videos = _find_videos(drive_dir)
        if not videos:
            return _fail(f"Fant ingen VID_*.mp4 i {drive_dir}")
    elif args.video:
        drive_dir = None
        videos = [Path(args.video).expanduser()]
        if not videos[0].is_file():
            return _fail(f"Fant ikke video: {videos[0]}")
    else:
        return _fail("Oppgi enten --drive <mappe> eller --video <fil>.")

    gps_seconds: list[float] | None = None
    if args.interval_s:
        print(f"Modus: intervall (hvert {args.interval_s:g}. sekund, uten GPS)")
    elif drive_dir is not None:
        gps_seconds = _gps_point_seconds(drive_dir, args.spacing_m, args.camera_offset_s)
        if not gps_seconds:
            return _fail("Ingen gyldige GPS-punkt (tom/ugyldig gps_track.csv, eller --spacing-m for stor).")
        print(f"Modus: GPS ({args.spacing_m:g} m) → {len(gps_seconds)} punkter")
    else:
        return _fail("GPS-modus krever --drive (trenger gps_track.csv + session.json). "
                     "For en enkelt video, bruk --interval-s.")

    out_dir = Path(args.out).expanduser() if args.out else (drive_dir or videos[0].parent) / "blurred"
    out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="process_drive_") as tmp:
        for video in videos:
            total += _process_one_video(video, args, gps_seconds, Path(tmp), out_dir)

    print(f"\n=== Ferdig ===")
    print(f"{total} sladdede bilder → {out_dir}  (på {time.monotonic() - started:.1f} s)")
    if total == 0:
        print("ADVARSEL: ingen bilder produsert — se advarslene over.")
    return 0


def _fail(message: str) -> int:
    print(message)
    return 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prosesser én kjøre-økt: uttrekk → flat → sladd")
    source = parser.add_argument_group("kilde")
    source.add_argument("--drive", default=None, help="kjøre-mappe (VID_*.mp4 + gps_track.csv + session.json)")
    source.add_argument("--video", default=None, help="én enkelt videofil (typisk med --interval-s)")
    parser.add_argument("--interval-s", type=float, default=None, help="test uten GPS: ramme hvert N. sekund")
    parser.add_argument("--spacing-m", type=float, default=3.0, help="GPS-modus: meter mellom hvert bilde (standard 3)")
    parser.add_argument("--camera-offset-s", type=float, default=0.0, help="synk-finjustering: lukker-forsinkelse i sek")
    parser.add_argument("--flat-size", default="1920x1920", help="størrelse per utsnitt (standard 1920x1920)")
    parser.add_argument("--scale", default="640x640", help="deface deteksjonsskala (standard 640x640; tom = full)")
    parser.add_argument("--proj", default="pannini", help="projeksjon (standard pannini)")
    parser.add_argument("--fov", type=float, default=200.0, help="input fisheye-FOV i grader — KALIBRER mot et flatet bilde (standard 200)")
    parser.add_argument("--out-fov", type=float, default=190.0, help="out-fov per utsnitt (standard 190)")
    parser.add_argument("--thresh", type=float, default=0.2, help="deface deteksjonsterskel (standard 0.2)")
    parser.add_argument("--out", default=None, help="mappe for sladdede bilder (standard <drive>/blurred)")
    args = parser.parse_args()
    if args.scale and args.scale.lower() in ("", "none", "full"):
        args.scale = None
    return args


if __name__ == "__main__":
    raise SystemExit(main())
