#!/usr/bin/env python3
"""Tørrkjøring av GPS-utløseren — UTEN kamera.

Leser GPS live, måler avstanden til nærmeste hentested (mål), oppdager nærmeste
passering, og lagrer koordinaten der et bilde VILLE blitt tatt til fil — så testen
kan godkjennes i ettertid (selv om skjermen går tom for strøm).

Ingen kamera er involvert. Dette bekrefter bare at utløser-logikken treffer riktig
sted på ekte GPS-data, før vi kobler den til photo_session.

To moduser:
  --mode hentested   (standard) utløs ved NÆRMESTE PASSERING av et hentested. Vi trigger ikke
                     på «0 m» (som aldri nås), men når avstanden slutter å synke og begynner å
                     øke igjen, innenfor en port (--gate-m). (Når det ekte kameraet kobles inn,
                     trykker vi ~1.8 s FØR dette punktet så lukkeren fyrer akkurat her.)
  --mode streetview  ta bilde med jevne METER-mellomrom langs ruta, som Google Street View-bilen
                     (--interval-m, standard 10 m), uavhengig av hvor hentestedene er.

Kjør:
    python3 -m recorder.trigger_preview                          # hentested: testkoordinaten
    python3 -m recorder.trigger_preview --target 59.9279,10.8259
    python3 -m recorder.trigger_preview --gate-m 25              # større slingringsmonn
    python3 -m recorder.trigger_preview --targets-csv hentesteder_001.csv
    python3 -m recorder.trigger_preview --mode streetview                 # bilde hver 10. meter
    python3 -m recorder.trigger_preview --mode streetview --interval-m 15
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


@dataclass
class PhotoEvent:
    snap: Snapshot
    reason: str            # "hentested" eller "streetview"
    distance_m: float      # hentested: avstand til målet; streetview: distanse siden forrige bilde
    target: Target | None = None


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

    def update(self, fix: GpsFix, stamp: str) -> PhotoEvent | None:
        target, dist = nearest(self.targets, fix.latitude, fix.longitude)
        self.last_target, self.last_dist = target, dist
        snap = Snapshot(stamp, fix.latitude, fix.longitude, fix.speed_mps, fix.course_deg)

        if dist > self.gate_m:
            event = None
            if self.engaged is not None and not self.fired and self.min_snap is not None:
                event = PhotoEvent(self.min_snap, "hentested", self.min_dist, self.engaged)  # forlot porten
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
            return PhotoEvent(self.min_snap, "hentested", self.min_dist, self.engaged)
        self.trend = "skutt" if self.fired else "nær"
        return None


class IntervalTrigger:
    """Street View-modus: tar bilde med jevne meter-mellomrom langs ruta. Akkumulerer kjørt
    distanse (haversine mellom fikser) og utløser hver `interval_m`. GPS-drift i ro filtreres
    bort med en fart-/steg-terskel."""

    def __init__(self, interval_m: float, min_move_mps: float = 0.5,
                 jitter_floor_m: float = 1.5, teleport_ceiling_m: float = 100.0) -> None:
        self.interval_m = interval_m
        self.min_move_mps = min_move_mps
        self.jitter_floor_m = jitter_floor_m
        self.teleport_ceiling_m = teleport_ceiling_m
        self.prev_lat: float | None = None
        self.prev_lon: float | None = None
        self.accum = 0.0                 # distanse siden forrige bilde
        self.total = 0.0                 # total kjørt distanse
        self._dist_at_last_photo = 0.0

    def update(self, fix: GpsFix, stamp: str) -> PhotoEvent | None:
        snap = Snapshot(stamp, fix.latitude, fix.longitude, fix.speed_mps, fix.course_deg)
        if self.prev_lat is None:
            self.prev_lat, self.prev_lon = fix.latitude, fix.longitude
            return PhotoEvent(snap, "streetview", 0.0, None)  # første bilde ved start, som SV-bilen

        step = haversine_m(self.prev_lat, self.prev_lon, fix.latitude, fix.longitude)
        self.prev_lat, self.prev_lon = fix.latitude, fix.longitude
        moving = (
            (fix.speed_mps is not None and fix.speed_mps >= self.min_move_mps)
            or (fix.speed_mps is None and step >= self.jitter_floor_m)
        )
        if 0 < step <= self.teleport_ceiling_m and moving:
            self.accum += step
            self.total += step

        if self.accum >= self.interval_m:
            self.accum -= self.interval_m
            since = self.total - self._dist_at_last_photo
            self._dist_at_last_photo = self.total
            return PhotoEvent(snap, "streetview", since, None)
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


def run(args: argparse.Namespace, trigger: "ApproachTrigger | IntervalTrigger", targets: list[Target]) -> None:
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

    streetview = args.mode == "streetview"
    print(f"Spor:                        {track_path}")
    print(f"Utløsere (simulerte bilder): {trig_path}")
    if streetview:
        print(f"Modus: streetview · bilde hver {args.interval_m:.0f} m · Ctrl+C for å stoppe.\n")
    else:
        print(f"Modus: hentested · {len(targets)} mål · port {args.gate_m:.0f} m · Ctrl+C for å stoppe.\n")

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
        next_tick = max(next_tick + args.tick, now)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        if not fix.has_position():
            print(f"[{stamp}] ingen fix — venter på satellitter")
            continue

        event = trigger.update(fix, stamp)

        if streetview:
            track_w.writerow([stamp, f"{fix.latitude:.7f}", f"{fix.longitude:.7f}", fix.fix_quality,
                              fix.satellites or "", _fmt(fix.speed_mps, 2), _fmt(fix.course_deg, 1), "", ""])
        else:
            track_w.writerow([stamp, f"{fix.latitude:.7f}", f"{fix.longitude:.7f}", fix.fix_quality,
                              fix.satellites or "", _fmt(fix.speed_mps, 2), _fmt(fix.course_deg, 1),
                              trigger.last_target.label, f"{trigger.last_dist:.1f}"])
        track_f.flush()

        if event is not None:
            photos += 1
            tgt = event.target
            trig_w.writerow([event.snap.stamp, f"{event.snap.lat:.7f}", f"{event.snap.lon:.7f}",
                             f"{event.distance_m:.1f}",
                             f"{tgt.lat:.7f}" if tgt else "", f"{tgt.lon:.7f}" if tgt else "",
                             tgt.label if tgt else event.reason,
                             _fmt(event.snap.speed_mps, 2), _fmt(event.snap.course_deg, 1)])
            trig_f.flush()
            if tgt is not None:
                print(f"    📸 (simulert) bilde: {event.snap.lat:.6f}, {event.snap.lon:.6f}  —  {event.distance_m:.1f} m fra «{tgt.label[:30]}»  [lagret]")
            else:
                print(f"    📸 (simulert) bilde: {event.snap.lat:.6f}, {event.snap.lon:.6f}  —  etter {event.distance_m:.0f} m  [lagret]")

        speed_s = f"{fix.speed_mps:.1f} m/s" if fix.speed_mps is not None else "?"
        if streetview:
            to_next = max(0.0, args.interval_m - trigger.accum)
            print(f"[{stamp}] {fix.latitude:.6f},{fix.longitude:.6f} | kjørt {trigger.total:6.0f} m | "
                  f"neste om {to_next:4.0f} m | fart {speed_s:>8} | bilder={photos}")
        else:
            ttca = time_to_closest(fix, trigger.last_target, trigger.last_dist)
            ttca_s = f"{ttca:.1f}s" if ttca is not None else "—"
            print(f"[{stamp}] {fix.latitude:.6f},{fix.longitude:.6f} | {trigger.last_target.label[:22]:22} "
                  f"{trigger.last_dist:6.1f} m | {trigger.trend:11} | fart {speed_s:>8} | t→nærmest {ttca_s:>5} | bilder={photos}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tørrkjøring av GPS-utløseren (uten kamera)")
    parser.add_argument("--mode", choices=["hentested", "streetview"], default="hentested",
                        help="hentested = utløs ved nærmeste passering av et mål; streetview = bilde med jevne meter-mellomrom")
    parser.add_argument("--port", default=DEFAULT_PORT, help="serieport (standard /dev/serial0)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="baudrate (standard 115200)")
    parser.add_argument("--target", default=None, help='hentested: ett mål som "lat,lon" (standard testkoordinaten)')
    parser.add_argument("--targets-csv", type=Path, default=None, help="hentested: hentesteder-CSV (semikolon; Breddegrad/Lengdegrad)")
    parser.add_argument("--gate-m", type=float, default=DEFAULT_GATE_M, help="hentested: maks avstand for å regne en passering (m)")
    parser.add_argument("--interval-m", type=float, default=10.0, help="streetview: meter mellom hvert bilde (standard 10)")
    parser.add_argument("--tick", type=float, default=1.0, help="sekunder mellom hver oppdatering/logglinje")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"mappe for logger (standard {DEFAULT_OUT_DIR})")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.mode == "streetview":
        trigger: "ApproachTrigger | IntervalTrigger" = IntervalTrigger(args.interval_m)
        targets: list[Target] = []
    else:
        targets = load_targets(args)
        trigger = ApproachTrigger(targets, args.gate_m)
    try:
        run(args, trigger, targets)
    except KeyboardInterrupt:
        print("\nStopper ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
