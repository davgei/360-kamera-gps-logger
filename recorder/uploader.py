"""Background download+upload worker shared by the recording and photo sessions.

Each job is (name, [file_urls]); the worker downloads each URL from the camera into
staging/<name>/ and uploads that folder to the rclone remote (move by default, so the
local copy is deleted only after a confirmed upload). rclone runs detached so a terminal
Ctrl+C does not kill an in-flight upload.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

from recorder.camera_osc import OneXCamera


def require_rclone(remote: str) -> None:
    if shutil.which("rclone") is None:
        sys.exit("rclone not found. Install it: sudo apt-get install -y rclone")
    result = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
    if f"{remote}:" not in result.stdout.split():
        sys.exit(
            f'rclone remote "{remote}:" is not configured. Run: rclone config '
            f"(create a Google Drive remote named '{remote}'; see recorder/README.md). "
            f"Configured remotes: {result.stdout.replace(chr(10), ' ').strip() or 'none'}"
        )


def upload_worker(
    jobs: "queue.Queue", camera: OneXCamera, staging: Path, remote: str, remote_path: str,
    keep_local: bool, process=None,
) -> None:
    while True:
        job = jobs.get()
        if job is None:
            jobs.task_done()
            break
        name, file_urls = job
        item_dir = staging / name
        item_dir.mkdir(parents=True, exist_ok=True)
        try:
            for url in file_urls or []:
                filename = url.rsplit("/", 1)[-1]
                print(f"[{name}] downloading {filename} ...")
                camera.download(url, item_dir / filename)
            if process is not None:
                try:
                    process(item_dir)
                except Exception as exc:
                    print(f"[{name}] processing failed ({exc}) — uploading what we have")
            if not any(item_dir.iterdir()):
                print(f"[{name}] no files to upload — skipping.")
                continue
            target = f"{remote}:{remote_path}/{name}"
            action = "copy" if keep_local else "move"
            print(f"[{name}] uploading to {target} ...")
            subprocess.run(
                ["rclone", action, str(item_dir), target,
                 "--contimeout", "30s", "--timeout", "300s",
                 "--retries", "3", "--low-level-retries", "10"],
                check=True,
                start_new_session=True,
            )
            print(f"[{name}] uploaded.")
            if not keep_local:
                try:
                    item_dir.rmdir()
                except OSError:
                    pass
        except urllib.error.URLError as exc:
            print(f"[{name}] download failed ({exc.reason}) — files kept at {item_dir}")
        except subprocess.CalledProcessError as exc:
            print(f"[{name}] upload failed (rclone exit {exc.returncode}) — files kept at {item_dir}")
        finally:
            jobs.task_done()


RCLONE_FLAGS = ["--contimeout", "30s", "--timeout", "300s", "--retries", "3", "--low-level-retries", "10"]


def _rclone_upload(folder: Path, remote: str, remote_path: str, keep_local: bool) -> bool:
    """rclone the folder to the remote. Returns True on success. Detached so Ctrl+C doesn't kill it."""
    target = f"{remote}:{remote_path}/{folder.name}"
    action = "copy" if keep_local else "move"
    try:
        subprocess.run(["rclone", action, str(folder), target, *RCLONE_FLAGS], check=True, start_new_session=True)
        if not keep_local:
            try:
                folder.rmdir()
            except OSError:
                pass
        return True
    except subprocess.CalledProcessError:
        return False


def flush_uploads(staging: Path, remote: str, remote_path: str, keep_local: bool = False) -> tuple:
    """Upload every READY folder in staging (skips .tmp_* still being written). Folders that fail
    (e.g. no internet) are left in place for a later retry. Returns (uploaded, failed)."""
    if not staging.exists():
        return (0, 0)
    ok = failed = 0
    for folder in sorted(staging.iterdir()):
        if not folder.is_dir() or folder.name.startswith(".tmp"):
            continue
        if not any(folder.iterdir()):
            try:
                folder.rmdir()
            except OSError:
                pass
            continue
        print(f"[{folder.name}] uploading to {remote}:{remote_path}/{folder.name} ...")
        if _rclone_upload(folder, remote, remote_path, keep_local):
            print(f"[{folder.name}] uploaded.")
            ok += 1
        else:
            print(f"[{folder.name}] upload failed (offline?) — kept locally, will retry.")
            failed += 1
    return (ok, failed)


def stage_job(job, camera: OneXCamera, staging: Path, process=None) -> None:
    """Download + process one job into a READY folder. Writes into .tmp_<name> and renames to
    <name> only when complete, so the uploader never sees a half-written folder."""
    name, file_urls = job
    tmp = staging / f".tmp_{name}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for url in file_urls or []:
            filename = url.rsplit("/", 1)[-1]
            print(f"[{name}] downloading {filename} ...")
            camera.download(url, tmp / filename)
    except urllib.error.URLError as exc:
        print(f"[{name}] download failed ({exc.reason}) — skipping")
        shutil.rmtree(tmp, ignore_errors=True)
        return
    if process is not None:
        try:
            process(tmp)
        except Exception as exc:
            print(f"[{name}] processing failed ({exc})")
    if not any(tmp.iterdir()):
        shutil.rmtree(tmp, ignore_errors=True)
        return
    ready = staging / name
    if ready.exists():
        shutil.rmtree(ready, ignore_errors=True)
    tmp.rename(ready)
    print(f"[{name}] ready for upload.")


def process_upload_worker(jobs, camera, staging, remote, remote_path, keep_local, process=None,
                          flush_interval: float = 30.0) -> None:
    """Single worker: stage each job, then upload all ready folders. When idle it retries pending
    uploads every flush_interval seconds — so files captured offline upload once the net returns."""
    flush_uploads(staging, remote, remote_path, keep_local)  # upload anything left from before
    while True:
        try:
            job = jobs.get(timeout=flush_interval)
        except queue.Empty:
            flush_uploads(staging, remote, remote_path, keep_local)
            continue
        try:
            if job is None:
                break
            stage_job(job, camera, staging, process)
        finally:
            jobs.task_done()
        flush_uploads(staging, remote, remote_path, keep_local)
    flush_uploads(staging, remote, remote_path, keep_local)
