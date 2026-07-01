#!/usr/bin/env python3
"""Flatten a dual-fisheye Insta360 ONE X photo, using ffmpeg's v360 filter.

The ONE X saves a photo as TWO fisheye circles in one JPEG. This converts it to:
  - an equirectangular panorama (the whole 360 in one flat 2:1 image), and/or
  - one or more flat (rectilinear, "normal-looking") views aimed with a yaw angle.

No Insta360 SDK needed (that targets iOS/Android/x86, not the Pi's ARM) — ffmpeg does it:
    sudo apt-get install -y ffmpeg

    python3 recorder/dewarp.py photo.jpg                 # equirect + flat views at yaw 0 and 180
    python3 recorder/dewarp.py photo.jpg --fov 205       # tune the input fisheye FOV per camera
    python3 recorder/dewarp.py photo.jpg --views 90,270  # aim the flat views elsewhere
    python3 recorder/dewarp.py photo.jpg --equirect-only

Outputs are written next to the input as <name>_equirect.jpg and <name>_flat_yawNNN.jpg.
Tune --fov (try 190-210) until the seam/edges look right for your camera body.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def _ffmpeg(vf: str, src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", vf, "-frames:v", "1", str(dst)],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("photo", help="dual-fisheye JPEG from the ONE X")
    parser.add_argument("--fov", type=float, default=200.0, help="input per-lens fisheye FOV in degrees (try 190-210)")
    parser.add_argument("--views", default="0,180", help="comma-separated yaw angles for flat views (deg)")
    parser.add_argument("--out-fov", type=float, default=100.0, help="output field of view for flat views (deg)")
    parser.add_argument("--pitch", type=float, default=0.0, help="tilt for flat views (deg; negative looks down)")
    parser.add_argument("--flat-size", default="1600x1600", help="flat view size WxH")
    parser.add_argument("--pano-size", default="5760x2880", help="equirectangular size WxH")
    parser.add_argument("--equirect-only", action="store_true", help="only produce the equirectangular image")
    parser.add_argument("--flat-only", action="store_true", help="only produce the flat views")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found. Install it: sudo apt-get install -y ffmpeg")

    src = Path(args.photo).expanduser()
    if not src.is_file():
        sys.exit(f"No such file: {src}")

    made = []

    if not args.flat_only:
        pw, ph = args.pano_size.lower().split("x")
        dst = src.with_name(f"{src.stem}_equirect.jpg")
        vf = f"v360=input=dfisheye:output=e:ih_fov={args.fov}:iv_fov={args.fov}:w={pw}:h={ph}"
        _ffmpeg(vf, src, dst)
        made.append(dst)
        print(f"equirectangular -> {dst.name}")

    if not args.equirect_only:
        fw, fh = args.flat_size.lower().split("x")
        for token in args.views.split(","):
            token = token.strip()
            if not token:
                continue
            yaw = float(token)
            dst = src.with_name(f"{src.stem}_flat_yaw{int(yaw)}.jpg")
            vf = (
                f"v360=input=dfisheye:output=flat:ih_fov={args.fov}:iv_fov={args.fov}"
                f":yaw={yaw}:pitch={args.pitch}:h_fov={args.out_fov}:v_fov={args.out_fov}:w={fw}:h={fh}"
            )
            _ffmpeg(vf, src, dst)
            made.append(dst)
            print(f"flat view yaw={yaw:g}° -> {dst.name}")

    print(f"\nDone — {len(made)} file(s) next to the input. If the seam/edges look wrong, retune --fov (190-210).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
