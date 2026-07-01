#!/usr/bin/env python3
"""Upload any locally-staged folders to Google Drive, retrying until the network is up.

Runs at boot (via 360logger-upload.service) and can be run by hand any time to flush photos that
were captured while offline. Uses rclone — run as the same user that ran `rclone config`.

    python3 -m recorder.upload_pending
    python3 -m recorder.upload_pending --staging ~/360-photos --remote gdrive --remote-path 360-photos
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from recorder.uploader import flush_uploads, require_rclone


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--staging", default="~/360-photos", help="local folder holding staged uploads")
    parser.add_argument("--remote", default="gdrive", help="rclone remote name (default gdrive)")
    parser.add_argument("--remote-path", default="360-photos", help="folder on the remote (default 360-photos)")
    parser.add_argument("--keep-local", action="store_true", help="keep local copies after upload")
    parser.add_argument("--retries", type=int, default=20, help="retry attempts while the network is down")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between retries")
    args = parser.parse_args()

    require_rclone(args.remote)
    staging = Path(args.staging).expanduser()

    for attempt in range(1, args.retries + 1):
        uploaded, failed = flush_uploads(staging, args.remote, args.remote_path, args.keep_local)
        if failed == 0:
            print(f"Done — {uploaded} folder(s) uploaded." if uploaded else "Nothing pending to upload.")
            return 0
        print(f"{failed} folder(s) still pending (attempt {attempt}/{args.retries}) — retrying in {int(args.interval)} s ...")
        time.sleep(args.interval)

    print("Gave up after retries; pending folders remain and will retry on the next boot/run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
