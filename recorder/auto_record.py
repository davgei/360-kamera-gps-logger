#!/usr/bin/env python3
"""Autonom, GPS-styrt opptaks-kontroller (kjøres på boot som 360logger-drive).

Regler (bekreftet med bruker):
  • Start opptak først når GPS har beveget seg > 10 m (fra stillstand).
  • Aldri film uten GPS — mister vi fix, stopper opptaket umiddelbart.
  • Etter 3 min i ro: stopp og vent til man beveger seg igjen (> 10 m).
  • Roter opptaket i ~10-minutters biter; hver bit lastes ned til Pi-en og SLETTES
    fra kameraet, så SD-kortet (256 GB) ikke fylles.
  • Internett trengs ikke for opptak (opplasting skjer i kø-tjenesten).

Nedlastingen skjer i BAKGRUNNEN mens neste bit filmes (verifisert mulig med
recorder/overlap_test på kameraet): ved bit-grensa stoppes opptaket, neste bit startes
umiddelbart (~1-2 s hull), og en `pending.json` skrives i bit-mappa. En bakgrunnstråd
(PendingDownloader) plukker opp pending-mapper, laster ned begge linser, sletter fra
kameraet (kun bekreftet nedlastede filer) og skriver session.json. Disk-tilstanden er
kilden, så avbrutte nedlastinger gjenopptas automatisk ved neste oppstart.

Merk: kameraet skriver video (~15 MB/s) raskere enn kamera-WiFi leverer under opptak
(~6.5 MB/s), så nedlastingen ligger på etterskudd under sammenhengende kjøring og tar
igjen når bilen står i ro (3-min-stopp, lunsj, natt). SD-vakten pauser opptak hvis
kameraets kort har mindre enn ~15 GB fritt (én bit + margin).

Hver ferdig bit blir en `~/360-drives/drive_<tid>/`-mappe (video + gps_track.csv +
session.json) som kø-tjenesten (process_queue) plukker opp: uttrekk hver 3 m → flat →
sladd → last opp → slett rå.

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
import socket
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
from recorder.status_leds import StatusLeds
from recorder.trigger_preview import haversine_m

DEFAULT_OUT_DIR = Path.home() / "360-drives"
_MOVE_SPEED_MPS = 0.7      # over dette regnes bilen som i bevegelse (~2.5 km/t)
_JITTER_FLOOR_M = 2.0      # hvis fart mangler: steg må være så stort for å telle som bevegelse
_GPS_TIMEOUT_S = 5.0       # ingen gyldig fix på så lenge = GPS tapt
_SD_MIN_FREE_BYTES = 15 * 1024 ** 3  # én 10-min bit er ~9-10 GB (målt ~15 MB/s) — under dette: pause
_CAM_CHECK_S = 5.0         # hvor ofte kamera-tilgjengelighet sjekkes (rask socket-test)


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


def _camera_reachable(host: str, port: int = 80, timeout: float = 1.5) -> bool:
    """Rask sjekk (uten å henge på OSC-timeout): svarer kameraets HTTP-port?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _start_segment(camera: OneXCamera, out_dir: Path) -> dict:
    stamp = _utc_now().strftime("%Y%m%dT%H%M%S")
    seg_dir = out_dir / f"drive_{stamp}"
    suffix = 1
    while seg_dir.exists():
        suffix += 1
        seg_dir = out_dir / f"drive_{stamp}_{suffix}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    try:
        try:
            camera.set_video_mode()
            video_start = _utc_now()
            camera.start_capture()
        except OscError:
            # Kameraet kan stå fast i et gammelt opptak (f.eks. en stopp som timet ut under
            # bakgrunnsnedlasting) — stopp det og prøv én gang til.
            try:
                stuck = camera.stop_capture()
                if stuck:
                    print(f"  (stoppet et fastlåst opptak — {len(stuck)} fil(er) blir liggende på kameraets SD)")
            except OscError:
                pass
            time.sleep(1.0)
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


def _close_segment(camera: OneXCamera, seg: dict, reason: str) -> None:
    """RASK avslutning (i hovedløkka, ~1 s): lukk GPS-fila, stopp opptaket, skriv pending.json.
    Selve nedlastingen gjør PendingDownloader i bakgrunnen — neste bit kan starte umiddelbart."""
    seg["handle"].flush()
    seg["handle"].close()
    video_stop = _utc_now()
    if not _camera_reachable(camera.host):
        print(f"  kamera ikke nåbart — kan ikke stoppe/hente {seg['dir'].name}; fjerner mappa")
        shutil.rmtree(seg["dir"], ignore_errors=True)
        return
    try:
        file_urls = _lens_siblings(camera.stop_capture())
    except OscError as exc:
        print(f"  feil ved stopp: {exc}")
        file_urls = []
    if not file_urls:
        shutil.rmtree(seg["dir"], ignore_errors=True)
        return
    pending = {
        "video_start_utc": seg["start"].isoformat(),
        "video_stop_utc": video_stop.isoformat(),
        "duration_s": round((video_stop - seg["start"]).total_seconds(), 1),
        "stop_reason": reason,
        "gps_rows": seg["rows"],
        "urls": file_urls,
        "attempts": 0,
    }
    tmp = seg["dir"] / ".pending.json.tmp"
    tmp.write_text(json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(seg["dir"] / "pending.json")
    print(f"■ OPPTAK STOPP {seg['dir'].name} ({reason}) — {len(file_urls)} fil(er) hentes i bakgrunnen")


class PendingDownloader(threading.Thread):
    """Bakgrunnsnedlaster: plukker biter med pending.json (eldste først), laster ned begge
    linser MENS neste bit filmes (verifisert med overlap_test), sletter fra kameraet (kun
    bekreftet nedlastede filer) og skriver session.json — først da ser kø-tjenesten økta.
    Disk-tilstanden er kilden: avbrutte/feilede nedlastinger plukkes opp igjen automatisk,
    også etter omstart."""

    MAX_ATTEMPTS = 5  # forsøk der kameraet SVARER men nedlasting feiler; kamera-av teller ikke

    def __init__(self, camera: OneXCamera, out_dir: Path, keep_on_camera: bool,
                 scan_interval: float = 5.0) -> None:
        super().__init__(daemon=True)
        self.camera = camera
        self.out_dir = out_dir
        self.keep_on_camera = keep_on_camera
        self.scan_interval = scan_interval
        self._stop = threading.Event()
        self.nudge = threading.Event()

    def pending_dirs(self) -> list[Path]:
        if not self.out_dir.exists():
            return []
        return sorted(d for d in self.out_dir.iterdir() if d.is_dir() and (d / "pending.json").is_file())

    def stop(self) -> None:
        self._stop.set()
        self.nudge.set()

    def run(self) -> None:
        while not self._stop.is_set():
            progressed = False
            for seg_dir in self.pending_dirs():
                if self._stop.is_set():
                    break
                progressed = self._process(seg_dir) or progressed
            if not progressed:
                self.nudge.wait(self.scan_interval)
                self.nudge.clear()

    def _process(self, seg_dir: Path) -> bool:
        try:
            job = json.loads((seg_dir / "pending.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not _camera_reachable(self.camera.host):
            return False  # kamera av (parkert/uten strøm) — filene ligger trygt på SD, prøv senere

        urls = job.get("urls", [])
        downloaded: list[str] = []
        files: list[str] = []
        for url in urls:
            name = url.rsplit("/", 1)[-1] or "video.mp4"
            dest = seg_dir / name
            if dest.is_file():  # finnes = verifisert komplett (vi laster til .part og omdøper til slutt)
                downloaded.append(url)
                files.append(name)
                continue
            # Last ned til .part og omdøp KUN etter verifisert komplett nedlasting — dør prosessen
            # midt i (strømkutt/SIGKILL), blir en halvferdig .part aldri tolket som ekte video,
            # og originalen på kameraet slettes ikke.
            part = seg_dir / (name + ".part")
            try:
                self.camera.download(url, part)
                part.replace(dest)
                downloaded.append(url)
                files.append(name)
                print(f"  [nedlaster] hentet {name} ({dest.stat().st_size / 1e6:.0f} MB)")
            except Exception as exc:
                part.unlink(missing_ok=True)
                print(f"  [nedlaster] {name}: {exc}")

        if len(files) < len(urls):
            job["attempts"] = int(job.get("attempts", 0)) + 1
            if job["attempts"] < self.MAX_ATTEMPTS:
                tmp = seg_dir / ".pending.json.tmp"
                tmp.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(seg_dir / "pending.json")
                return bool(files)
            print(f"  [nedlaster] gir opp resten av {seg_dir.name} etter {job['attempts']} forsøk")

        if not files:
            print(f"  [nedlaster] ingen filer for {seg_dir.name} — fjerner mappa (rå evt. igjen på kameraet)")
            shutil.rmtree(seg_dir, ignore_errors=True)
            return True

        if not self.keep_on_camera and downloaded:
            try:
                self.camera.delete(downloaded)
            except OscError as exc:
                print(f"  [nedlaster] klarte ikke slette fra kamera: {exc}")

        session = {
            "video_start_utc": job.get("video_start_utc"),
            "video_stop_utc": job.get("video_stop_utc"),
            "duration_s": job.get("duration_s"),
            "stop_reason": job.get("stop_reason"),
            "clock": "Pi system clock (UTC); gps_track.csv uses the SAME clock",
            "gps_track": "gps_track.csv",
            "gps_rows": job.get("gps_rows", 0),
            "video_files": files,
            "deleted_from_camera": bool(not self.keep_on_camera and downloaded),
        }
        tmp = seg_dir / ".session.json.tmp"
        tmp.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(seg_dir / "session.json")
        (seg_dir / "pending.json").unlink(missing_ok=True)
        print(f"  [nedlaster] ✔ {seg_dir.name} klar ({len(files)} videofil(er))")
        return True


def run(args: argparse.Namespace) -> int:
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())  # systemd-stopp → ryddig avslutning
    # SIGINT (Ctrl+C) håndteres IKKE her — la den heve KeyboardInterrupt, så den kan BRYTE en
    # blokkerende kamera-nedlasting. (En flagg-håndterer her gjorde at Ctrl+C ikke stoppet den.)

    camera = OneXCamera(args.host)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    reader = GpsReader(args.port, args.baud)
    reader.start()
    decider = RecordDecider(args.start_move_m, args.stationary_min * 60.0, args.segment_min * 60.0)
    leds = StatusLeds(enabled=not args.no_leds)  # blå = GPS · grønn = filmer · rød = kamera ikke nåbart
    downloader = PendingDownloader(camera, args.out_dir, args.keep_on_camera)
    downloader.start()  # plukker også opp pending-biter fra en tidligere kjøring

    print(f"Autonom opptak-kontroller · start ved {args.start_move_m:.0f} m · stopp etter "
          f"{args.stationary_min:g} min i ro · bit {args.segment_min:g} min · Ctrl+C for å stoppe")
    print("LED: blå = GPS-fix · grønn = filmer · rød = kamera ikke nåbart")
    leftover = len(downloader.pending_dirs())
    if leftover:
        print(f"({leftover} bit(er) fra tidligere venter på nedlasting — hentes i bakgrunnen)")

    seg: dict | None = None
    camera_ok = False
    next_tick = time.monotonic()
    last_status = 0.0
    last_cam_check = 0.0
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
                    _close_segment(camera, seg, action)
                    seg = None
                    downloader.nudge.set()  # bilen står nå — perfekt tid å laste ned
                elif action == "rotate" and seg is not None:
                    _close_segment(camera, seg, "rotate")
                    seg = _start_segment(camera, args.out_dir) if _sd_ok(camera) else None
                    downloader.nudge.set()  # nedlastingen skjer i bakgrunnen mens neste bit filmes
                    if seg is None:
                        print("SD-kortet er nesten fullt — pauser opptak til nedlastingen frigjør plass")
                        decider.force_idle()

                if seg is not None and gps_ok:
                    _write_gps_row(seg, fix)

                if now - last_status >= 5.0:  # heartbeat så man ser tilstanden i terminal/journal
                    last_status = now
                    pend = len(downloader.pending_dirs())
                    queue_note = f" · {pend} bit(er) i nedlastingskø" if pend else ""
                    if seg is not None:
                        print(f"  ● opptak {seg['dir'].name} · {seg['rows']} GPS-rader{queue_note}")
                    else:
                        sats = fix.satellites if fix.satellites is not None else "?"
                        print(f"  … venter · GPS {'OK' if gps_ok else 'nei'} (sats={sats}) · "
                              f"beveget {decider.moved_since_idle:.0f}/{decider.start_move_m:.0f} m{queue_note}")

                # Sjekk kamera-tilgjengelighet jevnlig — også UNDER opptak, ellers merkes det ikke
                # om kameraet skrur seg av mens vi «filmer» (da forble rød av). Rask socket-test.
                if now - last_cam_check >= _CAM_CHECK_S:
                    last_cam_check = now
                    camera_ok = _camera_reachable(args.host)
                leds.set_drive(gps_ok, seg is not None, camera_ok)
            except Exception as exc:  # én feil (kamera nede, disk, osv.) skal ALDRI drepe 24/7-løkka
                print(f"LØKKE-FEIL: {exc} — stopper opptaket, nullstiller og fortsetter")
                if seg is not None:
                    try:
                        _close_segment(camera, seg, "error")
                    except Exception:
                        pass
                    seg = None
                camera_ok = False
                decider.force_idle()
                leds.set_drive(False, False, camera_ok)

            next_tick = max(next_tick + args.interval, now)
            stop_event.wait(max(0.0, next_tick - time.monotonic()))
    except KeyboardInterrupt:
        print("\nStopper (Ctrl+C) ...")
    finally:
        if seg is not None:
            print("Avslutter — stopper siste bit ...")
            try:
                _close_segment(camera, seg, "shutdown")
                downloader.nudge.set()
            except Exception as exc:
                print(f"  (klarte ikke stoppe siste bit: {exc})")
        pending = downloader.pending_dirs()
        if pending:
            # Gi pågående nedlasting en sjanse, men ikke heng: pending.json gjør at resten
            # hentes automatisk ved neste oppstart (filene ligger trygt på kameraets SD).
            print(f"Venter inntil 60 s på {len(pending)} nedlasting(er) — resten hentes ved neste oppstart ...")
            deadline = time.monotonic() + 60.0
            while downloader.pending_dirs() and time.monotonic() < deadline:
                time.sleep(2.0)
            left = len(downloader.pending_dirs())
            if left:
                print(f"  ({left} bit(er) gjenstår — hentes automatisk neste gang)")
        downloader.stop()
        reader.stop()
        leds.close()
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
    parser.add_argument("--no-leds", action="store_true", help="ikke driv status-LED-ene")
    return parser.parse_args()


def main() -> int:
    return run(_parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
