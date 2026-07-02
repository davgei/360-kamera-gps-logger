#!/usr/bin/env python3
"""Les NMEA fra GPS-en over Pi-ens UART og logg breddegrad/lengdegrad hvert sekund.

Steg 3, oppkobling: verifiser at TBS M10Q (u-blox M10) er koblet til Pi-en og får
posisjon. Skriver en live statuslinje hvert sekund og legger til en CSV-rad per
sekund i en fil per økt under ~/360-gps-logs/.

Kobling (TBS M10Q -> Raspberry Pi 4, UART, 3.3V logikk — ingen nivåomformer trengs):
    modul VCC -> Pi 5V   (fysisk pin 2 eller 4)   [modulen regulerer selv 5V -> 3.3V]
    modul GND -> Pi GND  (fysisk pin 6)
    modul Tx  -> Pi RXD  (GPIO15, fysisk pin 10)
    modul Rx  -> Pi TXD  (GPIO14, fysisk pin 8)
    modul SCL/SDA -> ikke koblet (det er det innebygde kompasset, ikke GPS-en)

Skru på UART-en først (én gang):
    sudo raspi-config nonint do_serial_hw 0      # seriell maskinvare PÅ
    sudo raspi-config nonint do_serial_cons 1    # seriell innloggingskonsoll AV
    printf 'enable_uart=1\ndtoverlay=disable-bt\n' | sudo tee -a /boot/firmware/config.txt
    sudo systemctl disable hciuart
    sudo usermod -aG dialout "$USER"             # les serieporten uten sudo — logg ut/inn
    sudo reboot

Kjør:
    python3 -m recorder.gps_logger               # /dev/serial0 @ 115200
    python3 -m recorder.gps_logger --raw         # skriv også rå NMEA (feilsøk kobling/baud)
    python3 -m recorder.gps_logger --baud 9600   # hvis 115200 gir tomt (u-blox fabrikkstandard)
"""

from __future__ import annotations

import argparse
import csv
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

try:
    import serial
except ImportError as exc:
    raise SystemExit("pyserial er ikke installert. Kjør: sudo apt-get install -y python3-serial") from exc


DEFAULT_PORT = "/dev/serial0"
DEFAULT_BAUD = 115200
DEFAULT_LOG_DIR = Path.home() / "360-gps-logs"

_FIX_LABELS: dict[int, str] = {
    0: "ingen fix",
    1: "GPS",
    2: "DGPS",
    4: "RTK fast",
    5: "RTK flyt",
    6: "estimert",
}

CSV_COLUMNS: tuple[str, ...] = ("timestamp", "lat", "lon", "fix_quality", "satellites", "altitude_m")


@dataclass
class GpsFix:
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    fix_quality: int = 0
    satellites: int | None = None
    utc_time: str | None = None

    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def checksum_ok(sentence: str) -> bool:
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    body, _, checksum = sentence[1:].partition("*")
    computed = 0
    for char in body:
        computed ^= ord(char)
    try:
        return computed == int(checksum[:2], 16)
    except ValueError:
        return False


def _to_degrees(raw: str, hemisphere: str) -> float | None:
    """Konverter NMEA ddmm.mmmm / dddmm.mmmm + N/S/E/W til desimalgrader."""
    if not raw or "." not in raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    degrees = int(value // 100)
    minutes = value - degrees * 100
    decimal = degrees + minutes / 60.0
    if hemisphere in ("S", "W"):
        decimal = -decimal
    return decimal


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _as_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def update_fix(fix: GpsFix, sentence: str) -> None:
    """Oppdater fix ut fra én NMEA-setning. Filtrerer på setnings-TYPE (GGA/RMC), ikke
    talker — M10 sender $GNGGA/$GNRMC (fler-konstellasjon), ikke $GPxxx."""
    if not checksum_ok(sentence):
        return
    fields = sentence.split("*")[0].split(",")
    kind = fields[0][-3:]
    if kind == "GGA" and len(fields) >= 10:
        lat = _to_degrees(fields[2], fields[3])
        lon = _to_degrees(fields[4], fields[5])
        if lat is not None and lon is not None:
            fix.latitude = lat
            fix.longitude = lon
        fix.utc_time = fields[1] or fix.utc_time
        fix.fix_quality = _as_int(fields[6]) or 0
        fix.satellites = _as_int(fields[7])
        fix.altitude_m = _as_float(fields[9])
    elif kind == "RMC" and len(fields) >= 7:
        lat = _to_degrees(fields[3], fields[4])
        lon = _to_degrees(fields[5], fields[6])
        if lat is not None and lon is not None:
            fix.latitude = lat
            fix.longitude = lon
        fix.utc_time = fields[1] or fix.utc_time


def _open_csv(log_dir: Path, started: datetime) -> tuple[TextIO, "csv._writer", Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"gps_log_{started.strftime('%Y%m%dT%H%M%S')}.csv"
    handle = path.open("w", newline="", encoding="utf-8")
    writer = csv.writer(handle)
    writer.writerow(CSV_COLUMNS)
    handle.flush()
    return handle, writer, path


def _fmt(value: float | None, decimals: int = 6) -> str:
    return "" if value is None else f"{value:.{decimals}f}"


def _emit(fix: GpsFix, writer: "csv._writer | None", handle: TextIO | None) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    label = _FIX_LABELS.get(fix.fix_quality, str(fix.fix_quality))
    position = f"{fix.latitude:.6f}, {fix.longitude:.6f}" if fix.has_position() else "ingen posisjon (venter på satellitter)"
    sats = fix.satellites if fix.satellites is not None else "?"
    print(f"[{stamp}] {label} | sats={sats} | {position}")
    if writer is not None and handle is not None:
        writer.writerow([stamp, _fmt(fix.latitude), _fmt(fix.longitude), fix.fix_quality, fix.satellites or "", _fmt(fix.altitude_m, 1)])
        handle.flush()


def _log_loop(args: argparse.Namespace, writer: "csv._writer | None", handle: TextIO | None) -> None:
    fix = GpsFix()
    stream: "serial.Serial | None" = None
    next_tick = time.monotonic()
    while True:
        if stream is None:
            try:
                stream = serial.Serial(args.port, args.baud, timeout=1.0)
                print(f"Åpnet {args.port} @ {args.baud} baud")
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
            if args.raw and line:
                print(line)
            if line.startswith("$"):
                update_fix(fix, line)
        now = time.monotonic()
        if now >= next_tick:
            _emit(fix, writer, handle)
            next_tick = max(next_tick + args.interval, now)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GPS-logger for Raspberry Pi (TBS M10Q / u-blox M10 over UART)")
    parser.add_argument("--port", default=DEFAULT_PORT, help="serieport (standard /dev/serial0)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="baudrate (standard 115200; prøv 9600 hvis tomt)")
    parser.add_argument("--interval", type=float, default=1.0, help="sekunder mellom hver loggrad (standard 1.0)")
    parser.add_argument("--raw", action="store_true", help="skriv også rå NMEA-linjer (feilsøk kobling/baud)")
    parser.add_argument("--no-csv", action="store_true", help="ikke skriv CSV-fil, bare skjerm")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help=f"mappe for CSV-logger (standard {DEFAULT_LOG_DIR})")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    handle: TextIO | None = None
    writer: "csv._writer | None" = None
    if not args.no_csv:
        handle, writer, path = _open_csv(args.log_dir, datetime.now(timezone.utc))
        print(f"Logger til {path}")
    print("Ctrl+C for å stoppe.\n")

    try:
        _log_loop(args, writer, handle)
    except KeyboardInterrupt:
        print("\nStopper ...")
    finally:
        if handle is not None:
            handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
