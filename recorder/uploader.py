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
    jobs: "queue.Queue", camera: OneXCamera, staging: Path, remote: str, remote_path: str, keep_local: bool
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
