#!/usr/bin/env python3
"""Kamera-test: kan vi laste ned forrige klipp MENS neste tar opp?

Avgjør om nedlastingen kan flyttes til bakgrunnen i auto_record (og dermed fjerne
dekningshullet ved hver bit-grense). Kun kamera — ingen GPS/LED. Sekvens:

  1. Start klipp A → film i ~5 s → stopp A.
  2. Start klipp B UMIDDELBART (målt: hvor stort er dekningshullet stopp→start?).
  3. Mens B tar opp: last ned A (målt fart), slett A fra kameraet, sjekk /osc/state.
  4. Stopp B → last ned B → sjekk at B er intakt (tok opp hele tiden).

Testklippene lastes ned til ~/360-test-overlap/ og slettes fra kameraet etter
bekreftet nedlasting (--keep-on-camera beholder dem). Avbrytes testen (Ctrl+C),
stoppes et evt. pågående opptak før avslutning.

    python3 -m recorder.overlap_test
    python3 -m recorder.overlap_test --seconds 8
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from recorder.camera_osc import DEFAULT_HOST, OneXCamera, OscError
from recorder.drive_session import _lens_siblings

DEFAULT_OUT_DIR = Path.home() / "360-test-overlap"


def _download_all(camera: OneXCamera, urls: list[str], dest_dir: Path) -> tuple[list[str], list[str], float]:
    """Last ned alle URL-er. Returnerer (ok-urls, feilede-urls, totale MB)."""
    ok: list[str] = []
    failed: list[str] = []
    total_mb = 0.0
    for url in urls:
        name = url.rsplit("/", 1)[-1] or "video.mp4"
        dest = dest_dir / name
        start = time.monotonic()
        try:
            camera.download(url, dest)
        except Exception as exc:
            print(f"     ✗ {name}: {exc}")
            failed.append(url)
            continue
        secs = max(time.monotonic() - start, 0.001)
        mb = dest.stat().st_size / 1e6
        total_mb += mb
        print(f"     ✓ {name}: {mb:.1f} MB på {secs:.1f} s ({mb / secs:.1f} MB/s)")
        ok.append(url)
    return ok, failed, total_mb


def _start_capture_with_retry(camera: OneXCamera) -> None:
    """Kameraet kan være opptatt et øyeblikk rett etter stopp — prøv igjen én gang,
    med video-modus satt på nytt (i tilfelle kameraet falt tilbake til bilde-modus)."""
    try:
        camera.start_capture()
    except OscError as exc:
        print(f"   (start feilet: {exc} — setter video-modus og prøver igjen om 1.5 s)")
        time.sleep(1.5)
        camera.set_video_mode()
        camera.start_capture()


def _run_test(camera: OneXCamera, args: argparse.Namespace, rec: dict) -> int:
    results: dict[str, str] = {}

    print(f"\n1) Starter klipp A og filmer {args.seconds:.0f} s ...")
    camera.set_video_mode()
    camera.start_capture()
    rec["on"] = True
    time.sleep(args.seconds)

    print("2) Stopper A og starter B umiddelbart ...")
    t_stop_call = time.monotonic()
    urls_a = _lens_siblings(camera.stop_capture())
    rec["on"] = False
    recording_b = False
    b_started = 0.0
    try:
        _start_capture_with_retry(camera)
        gap_s = time.monotonic() - t_stop_call
        recording_b = True
        rec["on"] = True
        b_started = time.monotonic()
        results["dekningshull (stopp A → B ruller)"] = f"{gap_s:.1f} s"
        print(f"   B tar opp nå — hullet var {gap_s:.1f} s")
    except OscError as exc:
        results["dekningshull (stopp A → B ruller)"] = f"B STARTET IKKE: {exc}"
        print(f"   B startet ikke: {exc}")

    label = "nedlasting under opptak" if recording_b else "nedlasting (B tar IKKE opp — testen sier lite)"
    print(f"3) Laster ned A ({len(urls_a)} filer) {'mens B tar opp' if recording_b else '(B startet ikke)'} ...")
    ok_a, failed_a, mb_a = _download_all(camera, urls_a, args.out_dir)
    if not urls_a:
        results[label] = "ingen fil-URL-er fra stopp (uventet)"
    elif ok_a and not failed_a:
        results[label] = f"OK ({mb_a:.0f} MB)"
    elif ok_a:
        results[label] = f"DELVIS ({len(ok_a)}/{len(urls_a)} filer)"
    else:
        results[label] = "FEILET"

    if ok_a and not args.keep_on_camera:
        print("4) Sletter A fra kameraet mens B tar opp ...")
        try:
            camera.delete(ok_a)
            results["sletting under opptak"] = "OK"
        except OscError as exc:
            results["sletting under opptak"] = f"FEILET: {exc}"
    else:
        print("4) (hopper over sletting)")
        results["sletting under opptak"] = "ikke testet"

    try:
        state = camera.get_state().get("state", {})
        battery = state.get("batteryLevel")
        results["kamera svarer under opptak"] = f"OK (batteri {int(battery * 100)}%)" if battery is not None else "OK"
    except Exception as exc:
        results["kamera svarer under opptak"] = f"NEI: {exc}"

    if recording_b:
        b_duration = time.monotonic() - b_started
        print(f"5) Stopper B (tok opp i {b_duration:.0f} s) og verifiserer ...")
        urls_b: list[str] = []
        try:
            urls_b = _lens_siblings(camera.stop_capture())
            rec["on"] = False
        except OscError as exc:
            results["klipp B intakt"] = f"stopp feilet: {exc}"
        if urls_b:
            ok_b, failed_b, mb_b = _download_all(camera, urls_b, args.out_dir)
            if ok_b and not failed_b and mb_a > 0 and args.seconds > 0:
                # Grov integritetssjekk: B bør ha ~samme datarate som A (±50 %).
                rate_a = mb_a / args.seconds
                rate_b = mb_b / max(b_duration, 0.001)
                intact = 0.5 * rate_a <= rate_b <= 1.5 * rate_a
                results["klipp B intakt"] = (
                    f"{'OK' if intact else 'AVVIK'} ({mb_b:.0f} MB / {b_duration:.0f} s ≈ {rate_b:.1f} MB/s; "
                    f"A var {rate_a:.1f} MB/s)"
                )
            elif ok_b:
                results["klipp B intakt"] = f"lastet ned ({mb_b:.0f} MB), rate ikke sjekket"
            else:
                results["klipp B intakt"] = "NEDLASTING FEILET"
            if ok_b and not args.keep_on_camera:
                try:
                    camera.delete(ok_b)
                except OscError as exc:
                    print(f"   klarte ikke slette B fra kameraet: {exc}")

    print("\n=== Resultat ===")
    for key, value in results.items():
        print(f"  {key}: {value}")
    download_ok = results.get("nedlasting under opptak", "").startswith("OK")
    delete_ok = results.get("sletting under opptak", "") == "OK"
    print("\nKonklusjon:")
    if not recording_b:
        print("  ? B startet aldri — testen fikk ikke målt nedlasting UNDER opptak. Kjør igjen.")
        return 0
    if download_ok and delete_ok:
        print("  ✓ Kameraet KAN levere + slette forrige klipp mens det tar opp →")
        print("    nedlastingen kan flyttes til bakgrunnen i auto_record (dekningshullet")
        print("    krymper til bare stopp→start-tiden over).")
    elif download_ok:
        print("  ✓ Nedlasting under opptak virker, men sletting gjorde ikke — bakgrunns-")
        print("    nedlasting er mulig; sletting må vente til neste bit-grense.")
    else:
        print("  ✗ Kameraet leverer ikke filer under opptak — nedlastingen må forbli")
        print("    ved bit-grensene (dagens oppførsel er riktig).")
    print(f"\nTestfilene ligger i {args.out_dir} (kan slettes).")
    return 0


def main() -> int:
    args = _parse_args()
    camera = OneXCamera(args.host)
    try:
        info = camera.get_info()
        print(f"Kamera: {info.get('model', '?')} (firmware {info.get('firmwareVersion', '?')})")
    except Exception as exc:
        print(f"Får ikke kontakt med kameraet på {args.host}: {exc}")
        print("Er Pi-en på kameraets WiFi? Kjør evt.: python3 recorder/connect_camera_wifi.py")
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rec = {"on": False}
    try:
        return _run_test(camera, args, rec)
    except KeyboardInterrupt:
        print("\nAvbrutt.")
        return 1
    finally:
        if rec["on"]:
            print("Rydder opp: stopper pågående opptak ...")
            try:
                camera.stop_capture()
                print("  (testklippet ligger igjen på kameraets SD)")
            except OscError as exc:
                print(f"  klarte ikke stoppe: {exc} — sjekk kameraet manuelt")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test: nedlasting/sletting fra ONE X mens det tar opp")
    parser.add_argument("--host", default=DEFAULT_HOST, help="kamera-IP (standard 192.168.42.1)")
    parser.add_argument("--seconds", type=float, default=5.0, help="lengde på klipp A (standard 5 s)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"mappe for testfiler (standard {DEFAULT_OUT_DIR})")
    parser.add_argument("--keep-on-camera", action="store_true", help="ikke slett testklippene fra kameraet")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
