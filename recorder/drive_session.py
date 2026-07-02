#!/usr/bin/env python3
"""Kjøre-opptak: film sammenhengende med ONE X + logg GPS + noter synk-tid.

Ett opptak per økt. Starter video ved oppstart, logger GPS i bakgrunnen, og ved
Ctrl+C stopper den opptaket, laster ned videofilene fra kameraet, og skriver en
`session.json` med synk-informasjon.

Synk-nøkkelen: både opptakets starttid og GPS-loggen bruker **Pi-ens klokke (UTC)**.
Da vet PC-siden nøyaktig hvor videoens t=0 ligger på GPS-tidslinja, og kan hente ut
ett bilde per 3. meter langs ruta (video-sekund = gps-tid − video_start_utc, ± en
liten fast kamera-forsinkelse som kalibreres én gang).

Alt lagres i ~/360-drives/drive_<tidspunkt>/:
    session.json      synk-info + filliste
    gps_track.csv     timestamp,lat,lon,altitude,heading,speed,fix_quality,satellites
    VID_*.mp4         videofilene lastet ned fra kameraet (ONE X gir én fil per linse)

Krever at Pi-en er på kameraets WiFi (192.168.42.1) og at GPS-en er koblet til.

Kjør:
    python3 -m recorder.drive_session
    python3 -m recorder.drive_session --no-download    # ikke last ned nå (hent fra SD-kort senere)
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import serial
except ImportError as exc:
    raise SystemExit("pyserial er ikke installert. Kjør: sudo apt-get install -y python3-serial") from exc

from recorder.camera_osc import DEFAULT_HOST, OneXCamera, OscError
from recorder.gps_logger import DEFAULT_BAUD, DEFAULT_PORT, GpsFix, update_fix

DEFAULT_OUT_DIR = Path.home() / "360-drives"
GPS_TRACK_COLUMNS: tuple[str, ...] = (
    "timestamp", "lat", "lon", "altitude", "heading", "speed", "fix_quality", "satellites",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(value: float | None, decimals: int) -> str:
    return "" if value is None else f"{value:.{decimals}f}"


class GpsTrackLogger(threading.Thread):
    """Bakgrunnstråd: leser NMEA og skriver én rad per sekund (med posisjon) til
    gps_track.csv, med Pi-ens UTC-klokke — samme klokke som opptakets starttid."""

    def __init__(self, port: str, baud: int, csv_path: Path, interval: float = 1.0) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.csv_path = csv_path
        self.interval = interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._fix = GpsFix()
        self.rows_written = 0

    def latest_fix(self) -> GpsFix:
        with self._lock:
            return copy.copy(self._fix)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        handle = self.csv_path.open("w", newline="", encoding="utf-8")
        writer = csv.writer(handle)
        writer.writerow(GPS_TRACK_COLUMNS)
        handle.flush()
        stream: "serial.Serial | None" = None
        next_tick = time.monotonic()
        try:
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
                now = time.monotonic()
                if now >= next_tick:
                    next_tick = max(next_tick + self.interval, now)
                    self._write_row(writer, handle)
        finally:
            if stream is not None:
                stream.close()
            handle.flush()
            handle.close()

    def _write_row(self, writer: "csv._writer", handle) -> None:
        fix = self.latest_fix()
        if not fix.has_position():
            return
        writer.writerow([
            _utc_now().strftime("%Y-%m-%dT%H:%M:%S"),
            _fmt(fix.latitude, 7), _fmt(fix.longitude, 7),
            _fmt(fix.altitude_m, 1), _fmt(fix.course_deg, 1), _fmt(fix.speed_mps, 2),
            fix.fix_quality, fix.satellites if fix.satellites is not None else "",
        ])
        handle.flush()
        self.rows_written += 1


def _download_videos(camera: OneXCamera, file_urls: list[str], session_dir: Path) -> list[str]:
    names: list[str] = []
    print(f"Laster ned {len(file_urls)} videofil(er) fra kameraet (kan ta tid — 5.7K er stort) ...")
    for url in file_urls:
        name = url.rsplit("/", 1)[-1] or "video.mp4"
        try:
            camera.download(url, session_dir / name)
            names.append(name)
            print(f"  hentet {name}")
        except Exception as exc:
            print(f"  klarte ikke hente {url}: {exc}")
    return names


def run(args: argparse.Namespace) -> int:
    camera = OneXCamera(args.host)
    try:
        info = camera.get_info()
        print(f"Kamera: {info.get('model', '?')} (firmware {info.get('firmwareVersion', '?')})")
    except Exception as exc:
        print(f"Får ikke kontakt med kameraet på {args.host}: {exc}")
        print("Er Pi-en på kameraets WiFi? Kjør evt.: python3 recorder/connect_camera_wifi.py")
        return 1

    started = _utc_now()
    session_dir = args.out_dir / f"drive_{started.strftime('%Y%m%dT%H%M%S')}"
    session_dir.mkdir(parents=True, exist_ok=True)
    print(f"Økt: {session_dir}")

    gps: GpsTrackLogger | None = None
    if args.no_gps:
        print("GPS AV (--no-gps) — da mangler synk mot posisjon; kun for opptaks-test.")
    else:
        gps = GpsTrackLogger(args.port, args.baud, session_dir / "gps_track.csv")
        gps.start()
        print(f"GPS-logg: {session_dir / 'gps_track.csv'}")

    try:
        camera.set_video_mode()
        video_start_utc = _utc_now()
        camera.start_capture()
    except OscError as exc:
        print(f"Klarte ikke starte opptak: {exc}")
        if gps is not None:
            gps.stop()
        return 1
    print(f"● OPPTAK STARTET {video_start_utc.isoformat()} — Ctrl+C for å stoppe\n")

    try:
        while True:
            time.sleep(3.0)
            elapsed = int((_utc_now() - video_start_utc).total_seconds())
            if gps is None:
                status = "GPS av"
            else:
                fix = gps.latest_fix()
                status = (f"{fix.latitude:.6f},{fix.longitude:.6f} sats={fix.satellites or '?'}"
                          if fix.has_position() else "ingen GPS-fix ennå")
            print(f"  ● opptak {elapsed} s | {status} | GPS-rader={gps.rows_written if gps else 0}")
    except KeyboardInterrupt:
        print("\nStopper opptak ...")

    video_stop_utc = _utc_now()
    try:
        file_urls = camera.stop_capture()
    except OscError as exc:
        print(f"Feil ved stopp av opptak: {exc}")
        file_urls = []
    if gps is not None:
        gps.stop()
        gps.join(timeout=5.0)

    if file_urls and not args.no_download:
        video_files = _download_videos(camera, file_urls, session_dir)
    else:
        video_files = [url.rsplit("/", 1)[-1] for url in file_urls]
        if file_urls:
            print("--no-download: videofilene er IKKE hentet. Hent dem fra kameraets SD-kort,"
                  " eller mens du er på kameraets WiFi via URL-ene i session.json.")

    session = {
        "video_start_utc": video_start_utc.isoformat(),
        "video_stop_utc": video_stop_utc.isoformat(),
        "duration_s": round((video_stop_utc - video_start_utc).total_seconds(), 1),
        "clock": "Pi system clock (UTC); gps_track.csv uses the SAME clock",
        "sync_note": "video frame 0 ~= video_start_utc + a small constant camera-start latency; "
                     "calibrate camera_time_offset_seconds once on the PC side",
        "gps_track": "gps_track.csv" if gps is not None else None,
        "gps_rows": gps.rows_written if gps is not None else 0,
        "video_files": video_files,
        "downloaded": bool(file_urls) and not args.no_download,
        "camera_file_urls": file_urls,
        "camera_host": args.host,
    }
    (session_dir / "session.json").write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nØkt lagret: {session_dir}")
    print(f"  session.json (synk) · gps_track.csv ({session['gps_rows']} rader) · {len(video_files)} videofil(er)")
    print("Neste: kopier hele mappa til PC-en (scp/USB) — der kjører vi 3 m-uttrekket.")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kjøre-opptak: video + GPS + synk (ONE X)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="kamera-IP (standard 192.168.42.1)")
    parser.add_argument("--port", default=DEFAULT_PORT, help="GPS-serieport (standard /dev/serial0)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="GPS-baudrate (standard 115200)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"mappe for økter (standard {DEFAULT_OUT_DIR})")
    parser.add_argument("--no-gps", action="store_true", help="ikke logg GPS (kun opptaks-test — da mangler synk)")
    parser.add_argument("--no-download", action="store_true", help="ikke last ned videofilene fra kameraet nå")
    return parser.parse_args()


def main() -> int:
    return run(_parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
