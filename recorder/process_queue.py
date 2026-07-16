#!/usr/bin/env python3
"""Kø-tjeneste: prosesser kjøre-økter fortløpende, last opp sladdede bilder, slett rå video.

Kjører 24/7 (systemd-tjeneste 360logger-process). Ser i ~/360-drives/ etter FERDIGE økter
(de har en session.json — drive_session skriver den til slutt, så pågående opptak hoppes over),
eldste først, og for hver:
  1. kjør process_drive (GPS-modus, begge linser) → <økt>/blurred/
  2. last opp blurred/ til Drive (rclone move — kun de sladdede bildene)
  3. last opp gps_track.csv + session.json til samme Drive-mappe (session.json SIST —
     PC-en/dekningskartet bruker den som «økta er komplett på Drive»-markør)
  4. når opplasting er BEKREFTET: slett rå video, marker økta .done

Ledig tid (kveld/helg) drenerer køen automatisk. Rå video slettes ALDRI før opplasting er
bekreftet. Markører i økt-mappa: .processed (bilder laget), .done (lastet opp + rå slettet),
.meta_uploaded (gps_track.csv + session.json ligger på Drive), .error (prosessering ga
0 bilder / feilet — hoppes over, men beholdes for inspeksjon). Økter som ble lastet opp
FØR metadata-opplastingen fantes, etterfylles automatisk (.done uten .meta_uploaded).

    python3 -m recorder.process_queue                 # kjør løkka (som tjenesten gjør)
    python3 -m recorder.process_queue --once          # ta én runde og avslutt (test)
    python3 -m recorder.process_queue --no-upload     # prosesser + marker done, ikke last opp (test)
    python3 -m recorder.process_queue --keep-raw      # ikke slett rå video etter opplasting
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

from recorder.uploader import RCLONE_FLAGS

DEFAULT_DRIVES_DIR = Path.home() / "360-drives"


def _ready_drives(drives_dir: Path) -> list[Path]:
    """Ferdige, uprosesserte økter (har session.json, ikke .done/.error), eldste først."""
    if not drives_dir.exists():
        return []
    return sorted(
        d for d in drives_dir.iterdir()
        if d.is_dir() and (d / "session.json").is_file()
        and not (d / ".done").exists() and not (d / ".error").exists()
    )


def _run_process_drive(drive: Path, args: argparse.Namespace) -> int:
    """Kjør process_drive på økt-mappa (GPS-modus). Returner antall sladdede bilder, -1 ved feil."""
    blurred = drive / "blurred"
    result = subprocess.run([
        sys.executable, "-m", "recorder.process_drive", "--drive", str(drive),
        "--spacing-m", str(args.spacing_m), "--fov", str(args.fov), "--scale", args.scale,
        "--flat-size", args.flat_size, "--rot-10", args.rot_10, "--rot-00", args.rot_00,
    ])
    if result.returncode != 0:
        return -1
    return len(list(blurred.glob("*.jpg"))) if blurred.exists() else 0


def _upload_blurred(blurred: Path, remote: str, remote_path: str, drive_name: str) -> bool:
    """rclone move blurred/ → remote/remote_path/<drive_name>. True ved bekreftet opplasting.
    'move' laster opp og sletter lokalt først etter suksess. Detached så Ctrl+C ikke dreper den."""
    target = f"{remote}:{remote_path}/{drive_name}"
    print(f"[{drive_name}] laster opp sladdede bilder → {target} ...")
    try:
        subprocess.run(["rclone", "move", str(blurred), target, *RCLONE_FLAGS],
                       check=True, start_new_session=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False  # rclone mangler / feilet / offline → behandles som «prøv igjen» (rå beholdes)


def _upload_metadata(drive: Path, remote: str, remote_path: str) -> bool:
    """Last opp gps_track.csv + session.json til øktas Drive-mappe. session.json lastes opp
    SIST fordi PC-en tolker den som «alt for denne økta ligger på Drive»."""
    target = f"{remote}:{remote_path}/{drive.name}"
    for name in ("gps_track.csv", "session.json"):
        source = drive / name
        if not source.is_file():
            continue
        try:
            subprocess.run(["rclone", "copyto", str(source), f"{target}/{name}", *RCLONE_FLAGS],
                           check=True, start_new_session=True)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return False
    return True


def _backfill_metadata(drives_dir: Path, remote: str, remote_path: str) -> None:
    """Etterfyll gps_track.csv + session.json for økter som alt er lastet opp (.done) men
    mangler .meta_uploaded — dvs. økter fra før metadata-opplastingen fantes, eller der den
    feilet. Stopper ved første feil (offline) og prøver igjen neste runde."""
    if not drives_dir.exists():
        return
    pending = sorted(
        d for d in drives_dir.iterdir()
        if d.is_dir() and (d / ".done").exists() and not (d / ".meta_uploaded").exists()
        and (d / "session.json").is_file()
    )
    for drive in pending:
        if not _upload_metadata(drive, remote, remote_path):
            print(f"[{drive.name}] etterfylling av GPS-logg feilet (offline?) — prøver igjen senere")
            return
        (drive / ".meta_uploaded").touch()
        print(f"[{drive.name}] GPS-logg + session.json etterfylt til Drive")


def _delete_raw(drive: Path) -> int:
    removed = 0
    for video in drive.glob("VID_*.mp4"):
        try:
            video.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _handle_drive(drive: Path, args: argparse.Namespace) -> str:
    """Prosesser + (evt.) last opp + slett rå. Returnerer 'done' | 'error' | 'retry'."""
    blurred = drive / "blurred"

    if not (drive / ".processed").exists():
        print(f"\n=== {drive.name}: prosesserer ===")
        count = _run_process_drive(drive, args)
        if count <= 0:
            (drive / ".error").write_text("process_drive ga 0 bilder eller feilet\n", encoding="utf-8")
            print(f"[{drive.name}] FEIL — markert .error (hoppes over)")
            return "error"
        (drive / ".processed").write_text(f"{count} bilder\n", encoding="utf-8")

    if args.no_upload:
        (drive / ".done").touch()
        print(f"[{drive.name}] prosessert (ingen opplasting, --no-upload)")
        return "done"

    images = list(blurred.glob("*.jpg")) if blurred.exists() else []
    if not images:
        (drive / ".error").write_text("ingen sladdede bilder å laste opp\n", encoding="utf-8")
        return "error"

    if not _upload_blurred(blurred, args.remote, args.remote_path, drive.name):
        print(f"[{drive.name}] opplasting feilet (offline?) — beholder alt, prøver igjen senere")
        return "retry"

    # rclone move sletter kun kilde-filer etter bekreftet overføring; ligger det likevel bilder
    # igjen etter exit 0, ble ikke alt lastet opp → IKKE slett rå video.
    leftover = list(blurred.glob("*.jpg")) if blurred.exists() else []
    if leftover:
        print(f"[{drive.name}] {len(leftover)} bilder ligger igjen etter opplasting — beholder rå video, prøver igjen")
        return "retry"

    try:
        blurred.rmdir()
    except OSError:
        pass

    # Bildene er bekreftet oppe — feiler metadata-opplastingen nå, markeres økta likevel
    # .done (rå kan trygt slettes) og GPS-loggen etterfylles av _backfill_metadata senere.
    if _upload_metadata(drive, args.remote, args.remote_path):
        (drive / ".meta_uploaded").touch()
    else:
        print(f"[{drive.name}] GPS-logg-opplasting feilet — etterfylles ved en senere runde")

    if not args.keep_raw:
        removed = _delete_raw(drive)
        print(f"[{drive.name}] slettet {removed} rå videofil(er)")
    (drive / ".done").touch()
    print(f"[{drive.name}] FERDIG (lastet opp + ryddet)")
    return "done"


def _acquire_lock(drives_dir: Path):
    """Enkelt-instans-lås (Linux flock) så systemd-tjenesten og en manuell kjøring ikke tar
    samme økt samtidig. Returnerer et håndtak som må holdes åpent, eller None hvis en annen
    instans allerede kjører. På ikke-Linux (testkjøring) hoppes låsing over."""
    try:
        import fcntl
    except ImportError:
        return object()
    drives_dir.mkdir(parents=True, exist_ok=True)
    handle = open(drives_dir / ".queue.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _rclone_ready(remote: str) -> bool:
    if shutil.which("rclone") is None:
        return False
    result = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
    return f"{remote}:" in result.stdout.split()


def main() -> int:
    args = _parse_args()
    # Ikke krasj (og krasj-loop under systemd) hvis rclone ikke er satt opp ennå: prosesser
    # likevel, opplasting prøves på nytt til det er konfigurert. Rå slettes ALDRI før opplasting.
    if not args.no_upload and not _rclone_ready(args.remote):
        print(f"ADVARSEL: rclone-remote '{args.remote}:' er ikke konfigurert (kjør: rclone config). "
              "Prosesserer likevel; opplasting prøves på nytt til det virker.")
    if _acquire_lock(args.drives_dir) is None:
        print("En annen process_queue kjører allerede (lås holdt) — avslutter.")
        return 0
    print(f"Kø: {args.drives_dir}  ·  scan hvert {args.scan_interval:.0f}s"
          f"{'  ·  én runde' if args.once else '  ·  Ctrl+C for å stoppe'}")

    while True:
        drives = _ready_drives(args.drives_dir)
        if drives:
            for drive in drives:
                try:
                    status = _handle_drive(drive, args)
                except Exception as exc:  # én dårlig økt skal ikke drepe hele 24/7-løkka
                    (drive / ".error").write_text(f"uventet feil: {exc}\n", encoding="utf-8")
                    print(f"[{drive.name}] UVENTET FEIL: {exc} — markert .error, går videre")
                    continue
                if status == "retry":
                    break  # nettet nede — vent til neste runde i stedet for å spinne
        elif not args.once:
            print(f"(ingen nye økter — venter {args.scan_interval:.0f}s)")
        if not args.no_upload:
            _backfill_metadata(args.drives_dir, args.remote, args.remote_path)
        if args.once:
            break
        time.sleep(args.scan_interval)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kø-tjeneste: prosesser økter → last opp sladdet → slett rå")
    parser.add_argument("--drives-dir", type=Path, default=DEFAULT_DRIVES_DIR, help=f"mappe med kjøre-økter (standard {DEFAULT_DRIVES_DIR})")
    parser.add_argument("--remote", default="gdrive", help="rclone-remote (standard gdrive)")
    parser.add_argument("--remote-path", default="360-streetview", help="mappe på Drive (standard 360-streetview)")
    parser.add_argument("--spacing-m", type=float, default=3.0, help="meter mellom hvert bilde (standard 3)")
    parser.add_argument("--fov", type=float, default=200.0, help="input fisheye-FOV (kalibrer; standard 200)")
    parser.add_argument("--scale", default="640x640", help="deface deteksjonsskala (standard 640x640)")
    parser.add_argument("--flat-size", default="1920x1920", help="størrelse per utsnitt (standard 1920x1920)")
    parser.add_argument("--rot-10", default="cw", choices=["cw", "ccw", "none"], help="rotasjon for _10_-linsa")
    parser.add_argument("--rot-00", default="ccw", choices=["cw", "ccw", "none"], help="rotasjon for _00_-linsa")
    parser.add_argument("--scan-interval", type=float, default=60.0, help="sekunder mellom hver skanning av køen")
    parser.add_argument("--once", action="store_true", help="ta én runde gjennom køen og avslutt")
    parser.add_argument("--no-upload", action="store_true", help="prosesser + marker done, men ikke last opp (test)")
    parser.add_argument("--keep-raw", action="store_true", help="ikke slett rå video etter opplasting")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
