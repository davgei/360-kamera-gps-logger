#!/usr/bin/env python3
"""Tørrkjøring av GPS-utløseren — UTEN kamera.

Leser GPS live, måler avstanden til nærmeste hentested (mål), oppdager nærmeste
passering, og lagrer koordinaten der et bilde VILLE blitt tatt til fil — så testen
kan godkjennes i ettertid (selv om skjermen går tom for strøm).

Ingen kamera er involvert. Dette bekrefter bare at utløser-logikken treffer riktig
sted på ekte GPS-data, før vi kobler den til photo_session.

Logikk: vi trigger ikke på «0 m» (som aldri nås), men på NÆRMESTE PASSERING — når
avstanden slutter å synke og begynner å øke igjen, innenfor en port (--gate-m).
Koordinaten som lagres er selve nærmeste-passering-punktet. (Når det ekte kameraet
kobles inn, trykker vi ~1.8 s FØR dette punktet så lukkeren fyrer akkurat her.)

Kjør:
    python3 -m recorder.trigger_preview                          # mål = testkoordinaten
    python3 -m recorder.trigger_preview --target 59.9279,10.8259
    python3 -m recorder.trigger_preview --gate-m 25              # større slingringsmonn
    python3 -m recorder.trigger_preview --targets-csv hentesteder_001.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

try:
    import serial
except ImportError as exc:
    raise SystemExit("pyserial er ikke installert. Kjør: sudo apt-get install -y python3-serial") from exc

from recorder.gps_logger import DEFAULT_BAUD, DEFAULT_PORT, GpsFix, update_fix

DEFAULT_TARGET: tuple[float, float] = (59.927870, 10.825903)
DEFAULT_GATE_M = 20.0
DEFAULT_OUT_DIR = Path.home() / "360-gps-logs"
_EARTH_RADIUS_M = 6_371_000.0
_MIN_SPEED_FOR_HEADING = 1.0  # m/s — under dette er GPS-kursen for støyete å stole på
_PASS_HYSTERESIS_M = 2.0      # avstanden må øke så mye igjen før passeringen regnes som fullført


@dataclass
class Target:
    lat: float
    lon: float
    label: str


@dataclass
class Snapshot:
    stamp: str
    lat: float
    lon: float
    speed_mps: float | None
    course_deg: float | None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def nearest(targets: list[Target], lat: float, lon: float) -> tuple[Target, float]:
    best = targets[0]
    best_d = haversine_m(lat, lon, best.lat, best.lon)
    for t in targets[1:]:
        d = haversine_m(lat, lon, t.lat, t.lon)
        if d < best_d:
            best, best_d = t, d
    return best, best_d


def time_to_closest(fix: GpsFix, target: Target, dist_m: float) -> float | None:
    """Anslått sekunder til nærmeste passering, ut fra fart + kurs. None hvis for sakte."""
    if fix.speed_mps is None or fix.speed_mps < _MIN_SPEED_FOR_HEADING or fix.course_deg is None:
        return None
    to_target = bearing_deg(fix.latitude, fix.longitude, target.lat, target.lon)
    angle = math.radians((to_target - fix.course_deg + 180) % 360 - 180)
    along_track = dist_m * math.cos(angle)  # positiv = målet er foran oss
    if along_track <= 0:
        return None
    return along_track / fix.speed_mps


class ApproachTrigger:
    """Oppdager nærmeste passering av et mål innenfor porten (gate). Returnerer en
    Snapshot når et (simulert) bilde skal tas."""

    def __init__(self, targets: list[Target], gate_m: float, hysteresis_m: float = _PASS_HYSTERESIS_M) -> None:
        self.targets = targets
        self.gate_m = gate_m
        self.hysteresis_m = hysteresis_m
        self.engaged: Target | None = None
        self.min_dist = float("inf")
        self.min_snap: Snapshot | None = None
        self.fired = False
        self.last_target: Target | None = None
        self.last_dist: float | None = None
        self.trend = "utenfor"

    def update(self, fix: GpsFix, stamp: str) -> tuple[Snapshot, Target, float] | None:
        target, dist = nearest(self.targets, fix.latitude, fix.longitude)
        self.last_target, self.last_dist = target, dist
        snap = Snapshot(stamp, fix.latitude, fix.longitude, fix.speed_mps, fix.course_deg)

        if dist > self.gate_m:
            event = None
            if self.engaged is not None and not self.fired and self.min_snap is not None:
                event = (self.min_snap, self.engaged, self.min_dist)  # forlot porten — lagre nærmeste vi kom
            self.engaged, self.min_dist, self.min_snap, self.fired = None, float("inf"), None, False
            self.trend = "utenfor"
            return event

        if self.engaged is None or self.engaged.label != target.label:
            self.engaged, self.min_dist, self.min_snap, self.fired = target, dist, snap, False

        if dist < self.min_dist:
            self.min_dist, self.min_snap = dist, snap
            self.trend = "nærmer seg"
            return None
        if not self.fired and dist > self.min_dist + self.hysteresis_m:
            self.fired = True
            self.trend = "PASSERT"
            return (self.min_snap, self.engaged, self.min_dist)
        self.trend = "skutt" if self.fired else "nær"
        return None


def load_targets(args: argparse.Namespace) -> list[Target]:
    if args.targets_csv:
        return _load_targets_csv(args.targets_csv)
    if args.target:
        lat_s, lon_s = args.target.split(",")
        return [Target(float(lat_s), float(lon_s), "mål")]
    return [Target(DEFAULT_TARGET[0], DEFAULT_TARGET[1], "testmål")]


def _load_targets_csv(path: Path) -> list[Target]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp1252")  # norske kommune-CSV-er er ofte Windows-1252
    reader = csv.reader(text.splitlines(), delimiter=";")
    header = next(reader)
    index = {name: i for i, name in enumerate(header)}
    lat_i, lon_i = index.get("Breddegrad"), index.get("Lengdegrad")
    adr_i, id_i = index.get("adresse"), index.get("Beholderid")
    if lat_i is None or lon_i is None:
        raise SystemExit("Fant ikke kolonnene Breddegrad/Lengdegrad i CSV-en.")
    targets: list[Target] = []
    for row in reader:
        try:
            lat = float(row[lat_i].replace(",", "."))
            lon = float(row[lon_i].replace(",", "."))
        except (ValueError, IndexError):
            continue
        label = ""
        if adr_i is not None and adr_i < len(row):
            label = row[adr_i]
        if not label and id_i is not None and id_i < len(row):
            label = row[id_i]
        targets.append(Target(lat, lon, label or "hentested"))
    if not targets:
        raise SystemExit(f"Ingen gyldige koordinater i {path}")
    return targets


def _fmt(value: float | None, decimals: int) -> str:
    return "" if value is None else f"{value:.{decimals}f}"


def run(args: argparse.Namespace, targets: list[Target]) -> None:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    track_path = args.out_dir / f"track_{started.strftime('%Y%m%dT%H%M%S')}.csv"
    trig_path = args.out_dir / f"triggers_{started.strftime('%Y%m%dT%H%M%S')}.csv"
    track_f = track_path.open("w", newline="", encoding="utf-8")
    trig_f = trig_path.open("w", newline="", encoding="utf-8")
    track_w = csv.writer(track_f)
    trig_w = csv.writer(trig_f)
    track_w.writerow(["timestamp", "lat", "lon", "fix_quality", "satellites", "speed_mps", "course_deg", "nearest", "distance_m"])
    trig_w.writerow(["timestamp", "lat", "lon", "distance_m", "target_lat", "target_lon", "target", "speed_mps", "course_deg"])
    track_f.flush()
    trig_f.flush()

    print(f"Spor:                       {track_path}")
    print(f"Utløsere (simulerte bilder): {trig_path}")
    print(f"{len(targets)} mål · port {args.gate_m:.0f} m · Ctrl+C for å stoppe.\n")

    trigger = ApproachTrigger(targets, args.gate_m)
    fix = GpsFix()
    stream: "serial.Serial | None" = None
    next_tick = time.monotonic()
    photos = 0

    while True:
        if stream is None:
            try:
                stream = serial.Serial(args.port, args.baud, timeout=1.0)
            except serial.SerialException as exc:
                print(f"Får ikke åpnet {args.port}: {exc}. Prøver igjen om 2 s ...")
                time.sleep(2.0)
                continue
        try:
            raw = stream.readline()
        except serial.SerialException:
            print("Mistet serieporten — prøver å koble til igjen ...")
            stream.close()
            stream = None
            continue
        if raw:
            line = raw.decode("ascii", errors="replace").strip()
            if line.startswith("$"):
                update_fix(fix, line)

        now = time.monotonic()
        if now < next_tick:
            continue
        next_tick = max(next_tick + args.interval, now)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        if not fix.has_position():
            print(f"[{stamp}] ingen fix — venter på satellitter")
            continue

        event = trigger.update(fix, stamp)
        target, dist = trigger.last_target, trigger.last_dist
        track_w.writerow([stamp, f"{fix.latitude:.7f}", f"{fix.longitude:.7f}", fix.fix_quality,
                          fix.satellites or "", _fmt(fix.speed_mps, 2), _fmt(fix.course_deg, 1),
                          target.label, f"{dist:.1f}"])
        track_f.flush()

        if event is not None:
            snap, tgt, min_dist = event
            photos += 1
            trig_w.writerow([snap.stamp, f"{snap.lat:.7f}", f"{snap.lon:.7f}", f"{min_dist:.1f}",
                             f"{tgt.lat:.7f}", f"{tgt.lon:.7f}", tgt.label,
                             _fmt(snap.speed_mps, 2), _fmt(snap.course_deg, 1)])
            trig_f.flush()
            print(f"    📸 (simulert) bilde: {snap.lat:.6f}, {snap.lon:.6f}  —  {min_dist:.1f} m fra «{tgt.label[:30]}»  [lagret]")

        ttca = time_to_closest(fix, target, dist)
        ttca_s = f"{ttca:.1f}s" if ttca is not None else "—"
        speed_s = f"{fix.speed_mps:.1f} m/s" if fix.speed_mps is not None else "?"
        print(f"[{stamp}] {fix.latitude:.6f},{fix.longitude:.6f} | {target.label[:22]:22} {dist:6.1f} m | "
              f"{trigger.trend:11} | fart {speed_s:>8} | t→nærmest {ttca_s:>5} | bilder={photos}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tørrkjøring av GPS-utløseren (uten kamera)")
    parser.add_argument("--port", default=DEFAULT_PORT, help="serieport (standard /dev/serial0)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="baudrate (standard 115200)")
    parser.add_argument("--target", default=None, help='ett mål som "lat,lon" (standard testkoordinaten)')
    parser.add_argument("--targets-csv", type=Path, default=None, help="hentesteder-CSV (semikolon; Breddegrad/Lengdegrad)")
    parser.add_argument("--gate-m", type=float, default=DEFAULT_GATE_M, help="port: maks avstand for å regne en passering (m)")
    parser.add_argument("--interval", type=float, default=1.0, help="sekunder mellom hver oppdatering")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"mappe for logger (standard {DEFAULT_OUT_DIR})")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    targets = load_targets(args)
    try:
        run(args, targets)
    except KeyboardInterrupt:
        print("\nStopper ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
