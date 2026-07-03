#!/usr/bin/env python3
"""Autonom, GPS-styrt opptaks-kontroller (kjøres på boot som 360logger-drive).

Regler (bekreftet med bruker):
  • Start opptak først når GPS har beveget seg > 10 m (fra stillstand).
  • Aldri film uten GPS — mister vi fix, stopper opptaket umiddelbart.
  • Etter 3 min i ro: stopp og vent til man beveger seg igjen (> 10 m).
  • Roter opptaket i ~10-minutters biter; hver bit lastes ned til Pi-en og SLETTES
    fra kameraet, så SD-kortet (256 GB) ikke fylles.
  • Internett trengs ikke for opptak (opplasting skjer i kø-tjenesten).

Hver bit blir en `~/360-drives/drive_<tid>/`-mappe (video + gps_track.csv + session.json)
som kø-tjenesten (process_queue) plukker opp: uttrekk hver 3 m → flat → sladd → last opp
→ slett rå.

    python3 -m recorder.auto_record            # kjør kontrolleren (som tjenesten gjør)
    python3 -m recorder.auto_record --segment-min 10 --stationary-min 3
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import shutil
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import serial
except ImportError as exc:
    raise SystemExit("pyserial er ikke installert. Kjør: sudo apt-get install -y python3-serial") from exc

from recorder.camera_osc import DEFAULT_HOST, OneXCamera, OscError
from recorder.drive_session import GPS_TRACK_COLUMNS, _fmt, _lens_siblings
from recorder.gps_logger import DEFAULT_BAUD, DEFAULT_PORT, GpsFix, update_fix
from recorder.trigger_preview import haversine_m

DEFAULT_OUT_DIR = Path.home() / "360-drives"
_MOVE_SPEED_MPS = 0.7      # over dette regnes bilen som i bevegelse (~2.5 km/t)
_JITTER_FLOOR_M = 2.0      # hvis fart mangler: steg må være så stort for å telle som bevegelse
_GPS_TIMEOUT_S = 5.0       # ingen gyldig fix på så lenge = GPS tapt
_SD_MIN_FREE_BYTES = 2 * 1024 ** 3  # under dette: pause opptak til plass frigjøres


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GpsReader(threading.Thread):
    """Leser NMEA kontinuerlig og holder siste fix + tidspunkt for siste GYLDIGE fix."""

    def __init__(self, port: str, baud: int) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._fix = GpsFix()
        self._last_valid_mono = 0.0

    def latest(self) -> tuple[GpsFix, float]:
        with self._lock:
            return copy.copy(self._fix), self._last_valid_mono

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        stream: "serial.Serial | None" = None
        while not self._stop.is_set():
            if stream is None:
                try:
                    stream = serial.Serial(self.port, self.baud, timeout=1.0)
                except serial.SerialException:
                    self._stop.wait(2.0)
                    continue
            try:
                raw = stream.readline()
            except serial.SerialException:
                stream.close()
                stream = None
                continue
            if raw:
                line = raw.decode("ascii", errors="replace").strip()
                if line.startswith("$"):
                    with self._lock:
                        update_fix(self._fix, line)
                        if self._fix.fix_quality > 0 and self._fix.has_position():
                            self._last_valid_mono = time.monotonic()
        if stream is not None:
            stream.close()


class RecordDecider:
    """Ren tilstandsmaskin: gitt en fix (eller None hvis GPS mangler) og tiden nå,
    returner en handling: 'start' | 'stop:gps_lost' | 'stop:stationary' | 'rotate' | 'none'."""

    def __init__(self, start_move_m: float, stationary_stop_s: float, segment_max_s: float,
                 move_speed_mps: float = _MOVE_SPEED_MPS, jitter_floor_m: float = _JITTER_FLOOR_M) -> None:
        self.start_move_m = start_move_m
        self.stationary_stop_s = stationary_stop_s
        self.segment_max_s = segment_max_s
        self.move_speed_mps = move_speed_mps
        self.jitter_floor_m = jitter_floor_m
        self.recording = False
        self.moved_since_idle = 0.0
        self.prev_lat: float | None = None
        self.prev_lon: float | None = None
        self.last_moving = 0.0
        self.segment_start = 0.0

    def force_idle(self) -> None:
        self.recording = False
        self.moved_since_idle = 0.0
        self.prev_lat = self.prev_lon = None

    def _is_moving(self, fix: GpsFix, step_m: float) -> bool:
        if fix.speed_mps is not None:
            return fix.speed_mps >= self.move_speed_mps
        return step_m >= self.jitter_floor_m

    def update(self, fix: GpsFix | None, now: float) -> str:
        if fix is None:  # GPS mangler
            if self.recording:
                self.force_idle()
                return "stop:gps_lost"
            self.prev_lat = self.prev_lon = None
            return "none"

        step = 0.0
        if self.prev_lat is not None:
            step = haversine_m(self.prev_lat, self.prev_lon, fix.latitude, fix.longitude)
        moving = self._is_moving(fix, step)
        self.prev_lat, self.prev_lon = fix.latitude, fix.longitude

        if not self.recording:
            if moving:
                self.moved_since_idle += step
            if self.moved_since_idle >= self.start_move_m:
                self.recording = True
                self.moved_since_idle = 0.0
                self.last_moving = now
                self.segment_start = now
                return "start"
            return "none"

        if moving:
            self.last_moving = now
        if now - self.last_moving >= self.stationary_stop_s:
            self.force_idle()
            return "stop:stationary"
        if now - self.segment_start >= self.segment_max_s:
            self.segment_start = now
            return "rotate"
        return "none"


def _sd_ok(camera: OneXCamera) -> bool:
    free = camera.free_space_bytes()
    return free is None or free >= _SD_MIN_FREE_BYTES


def _start_segment(camera: OneXCamera, out_dir: Path) -> dict:
    stamp = _utc_now().strftime("%Y%m%dT%H%M%S")
    seg_dir = out_dir / f"drive_{stamp}"
    suffix = 1
    while seg_dir.exists():
        suffix += 1
        seg_dir = out_dir / f"drive_{stamp}_{suffix}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    try:
        camera.set_video_mode()
        video_start = _utc_now()
        camera.start_capture()
        handle = (seg_dir / "gps_track.csv").open("w", newline="", encoding="utf-8")
        writer = csv.writer(handle)
        writer.writerow(GPS_TRACK_COLUMNS)
        handle.flush()
    except Exception:
        shutil.rmtree(seg_dir, ignore_errors=True)  # ikke etterlat en tom mappe hvis start feiler
        raise
    print(f"● OPPTAK START {seg_dir.name} ({video_start.isoformat()})")
    return {"dir": seg_dir, "start": video_start, "handle": handle, "writer": writer, "rows": 0}


def _write_gps_row(seg: dict, fix: GpsFix) -> None:
    seg["writer"].writerow([
        _utc_now().strftime("%Y-%m-%dT%H:%M:%S"),
        _fmt(fix.latitude, 7), _fmt(fix.longitude, 7),
        _fmt(fix.altitude_m, 1), _fmt(fix.course_deg, 1), _fmt(fix.speed_mps, 2),
        fix.fix_quality, fix.satellites if fix.satellites is not None else "",
    ])
    seg["handle"].flush()
    seg["rows"] += 1


def _finalize_segment(camera: OneXCamera, seg: dict, reason: str, delete_from_camera: bool) -> None:
    seg["handle"].flush()
    seg["handle"].close()
    video_stop = _utc_now()
    try:
        file_urls = _lens_siblings(camera.stop_capture())
    except OscError as exc:
        print(f"  feil ved stopp: {exc}")
        file_urls = []

    downloaded_urls: list[str] = []
    video_files: list[str] = []
    for url in file_urls:
        name = url.rsplit("/", 1)[-1] or "video.mp4"
        dest = seg["dir"] / name
        try:
            camera.download(url, dest)
            if dest.is_file() and dest.stat().st_size > 0:
                downloaded_urls.append(url)
                video_files.append(name)
        except Exception as exc:
            print(f"  klarte ikke hente {name}: {exc}")

    if not video_files:
        # Ingen video kom ned (rå ligger fortsatt trygt på kameraet). Ikke skriv session.json
        # (da ville kø-tjenesten .error-et den) og ikke etterlat en foreldreløs GPS-mappe.
        print(f"  ADVARSEL: ingen videofiler lastet ned for {seg['dir'].name} — fjerner mappa "
              "(rå beholdes på kameraet)")
        shutil.rmtree(seg["dir"], ignore_errors=True)
        return

    # Slett fra kameraet KUN de filene som er bekreftet nedlastet (SD-kortet holdes tomt).
    if delete_from_camera and downloaded_urls:
        try:
            camera.delete(downloaded_urls)
        except OscError as exc:
            print(f"  klarte ikke slette fra kamera: {exc}")

    session = {
        "video_start_utc": seg["start"].isoformat(),
        "video_stop_utc": video_stop.isoformat(),
        "duration_s": round((video_stop - seg["start"]).total_seconds(), 1),
        "stop_reason": reason,
        "clock": "Pi system clock (UTC); gps_track.csv uses the SAME clock",
        "gps_track": "gps_track.csv",
        "gps_rows": seg["rows"],
        "video_files": video_files,
        "deleted_from_camera": bool(delete_from_camera and downloaded_urls),
    }
    tmp = seg["dir"] / ".session.json.tmp"
    tmp.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(seg["dir"] / "session.json")
    print(f"■ OPPTAK STOPP {seg['dir'].name} ({reason}) — {len(video_files)} videofil(er), {seg['rows']} GPS-rader")


def run(args: argparse.Namespace) -> int:
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    camera = OneXCamera(args.host)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    reader = GpsReader(args.port, args.baud)
    reader.start()
    decider = RecordDecider(args.start_move_m, args.stationary_min * 60.0, args.segment_min * 60.0)

    print(f"Autonom opptak-kontroller · start ved {args.start_move_m:.0f} m · stopp etter "
          f"{args.stationary_min:g} min i ro · bit {args.segment_min:g} min · Ctrl+C for å stoppe")

    seg: dict | None = None
    next_tick = time.monotonic()
    try:
        while not stop_event.is_set():
            now = time.monotonic()
            try:
                fix, last_valid = reader.latest()
                gps_ok = fix.fix_quality > 0 and fix.has_position() and (now - last_valid) < _GPS_TIMEOUT_S
                action = decider.update(fix if gps_ok else None, now)

                if action == "start":
                    if _sd_ok(camera):
                        seg = _start_segment(camera, args.out_dir)
                    else:
                        print("SD-kortet er nesten fullt — venter med å starte opptak")
                        decider.force_idle()
                elif action.startswith("stop") and seg is not None:
                    _finalize_segment(camera, seg, action, not args.keep_on_camera)
                    seg = None
                elif action == "rotate" and seg is not None:
                    _finalize_segment(camera, seg, "rotate", not args.keep_on_camera)
                    seg = _start_segment(camera, args.out_dir) if _sd_ok(camera) else None
                    if seg is None:
                        print("SD-kortet er nesten fullt — pauser opptak")
                        decider.force_idle()

                if seg is not None and gps_ok:
                    _write_gps_row(seg, fix)
            except Exception as exc:  # én feil (kamera nede, disk, osv.) skal ALDRI drepe 24/7-løkka
                print(f"LØKKE-FEIL: {exc} — stopper opptaket, nullstiller og fortsetter")
                if seg is not None:
                    try:
                        _finalize_segment(camera, seg, "error", not args.keep_on_camera)
                    except Exception:
                        pass
                    seg = None
                decider.force_idle()

            next_tick = max(next_tick + args.interval, now)
            stop_event.wait(max(0.0, next_tick - time.monotonic()))
    finally:
        if seg is not None:
            print("Avslutter — fullfører siste bit ...")
            _finalize_segment(camera, seg, "shutdown", not args.keep_on_camera)
        reader.stop()
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autonom GPS-styrt opptaks-kontroller (ONE X)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="kamera-IP (standard 192.168.42.1)")
    parser.add_argument("--port", default=DEFAULT_PORT, help="GPS-serieport (standard /dev/serial0)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="GPS-baudrate (standard 115200)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"mappe for økter (standard {DEFAULT_OUT_DIR})")
    parser.add_argument("--start-move-m", type=float, default=10.0, help="start opptak etter så mange meter bevegelse (standard 10)")
    parser.add_argument("--stationary-min", type=float, default=3.0, help="stopp etter så mange minutter i ro (standard 3)")
    parser.add_argument("--segment-min", type=float, default=10.0, help="roter opptaket i biter på så mange minutter (standard 10)")
    parser.add_argument("--interval", type=float, default=1.0, help="sekunder mellom hver GPS-vurdering (standard 1)")
    parser.add_argument("--keep-on-camera", action="store_true", help="ikke slett videofilene fra kameraet etter nedlasting")
    return parser.parse_args()


def main() -> int:
    return run(_parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
