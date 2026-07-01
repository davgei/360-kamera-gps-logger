#!/usr/bin/env python3
"""Flatten a dual-fisheye Insta360 ONE X photo, using ffmpeg's v360 filter.

The ONE X saves a photo as TWO fisheye circles in one JPEG. This converts it to:
  - an equirectangular panorama (the whole 360 in one flat 2:1 image), and/or
  - one or more per-lens views aimed by yaw. Default projection is half-equirect (he), which
    shows the FULL ~180° hemisphere of each lens without cropping. A truly flat (rectilinear)
    view looks straighter but MUST crop wide angles — use --proj flat --out-fov 100 for that.

No Insta360 SDK needed (that targets iOS/Android/x86, not the Pi's ARM) — ffmpeg does it:
    sudo apt-get install -y ffmpeg

    python3 recorder/dewarp.py photo.jpg                 # equirect + two half-equirect views (full hemisphere each)
    python3 recorder/dewarp.py photo.jpg --fov 205       # tune the input fisheye FOV per camera
    python3 recorder/dewarp.py photo.jpg --proj flat --out-fov 100   # rectilinear crop (straight lines, narrower)
    python3 recorder/dewarp.py photo.jpg --views 90,270  # aim the views elsewhere
    python3 recorder/dewarp.py photo.jpg --equirect-only

Outputs are written next to the input as <name>_equirect.jpg and <name>_<proj>_yawNNN.jpg.
Tune --fov (try 190-210) until the seam/edges look right; raise --flat-size for more resolution.
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


def flatten_views(src: Path, proj: str = "pannini", out_fov: float = 190.0, views: str = "0,180",
                  rotate: str = "cw,ccw", fov: float = 200.0, flat_size: str = "2880x2880",
                  pitch: float = 0.0, quiet: bool = False) -> list:
    """Produce flattened view(s) next to src (one JPEG per yaw in `views`). Returns the paths."""
    fw, fh = flat_size.lower().split("x")
    yaws = [t.strip() for t in views.split(",") if t.strip()]
    rotates = [t.strip().lower() for t in rotate.split(",")]
    transpose = {"cw": ",transpose=1", "ccw": ",transpose=2", "180": ",transpose=2,transpose=2"}
    made = []
    for i, token in enumerate(yaws):
        yaw = float(token)
        rot = rotates[i] if i < len(rotates) else ""
        extra = transpose.get(rot, "")
        dst = src.with_name(f"{src.stem}_{proj}_yaw{int(yaw)}.jpg")
        vf = (
            f"v360=input=dfisheye:output={proj}:ih_fov={fov}:iv_fov={fov}"
            f":yaw={yaw}:pitch={pitch}:h_fov={out_fov}:v_fov={out_fov}:w={fw}:h={fh}{extra}"
        )
        _ffmpeg(vf, src, dst)
        made.append(dst)
        if not quiet:
            label = f" rot={rot}" if rot else ""
            print(f"{proj} view yaw={yaw:g}°{label} -> {dst.name}")
    return made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("photo", help="dual-fisheye JPEG from the ONE X")
    parser.add_argument("--fov", type=float, default=200.0, help="input per-lens fisheye FOV in degrees (try 190-210)")
    parser.add_argument("--views", default="0,180", help="comma-separated yaw angles for flat views (deg)")
    parser.add_argument("--out-fov", type=float, default=180.0, help="output field of view per view (deg); ~180 for full hemisphere, ~100 for a flat/rectilinear crop")
    parser.add_argument("--pitch", type=float, default=0.0, help="tilt for flat views (deg; negative looks down)")
    parser.add_argument("--flat-size", default="2880x2880", help="per-view output size WxH")
    parser.add_argument("--pano-size", default="5760x2880", help="equirectangular size WxH")
    parser.add_argument("--equirect-only", action="store_true", help="only produce the equirectangular image")
    parser.add_argument("--flat-only", action="store_true", help="only produce the flat views")
    parser.add_argument("--rotate", default="cw,ccw", help="rotation per flat view: none|cw|ccw|180, comma-separated (matches --views)")
    parser.add_argument(
        "--proj", default="he", choices=["he", "sg", "pannini", "cylindrical", "flat", "fisheye"],
        help="view projection: he=half-equirect (full ~180° hemisphere per lens, default); sg/pannini/cylindrical=wide; flat=rectilinear (crops); fisheye=raw",
    )
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
        made += flatten_views(
            src, proj=args.proj, out_fov=args.out_fov, views=args.views,
            rotate=args.rotate, fov=args.fov, flat_size=args.flat_size, pitch=args.pitch,
        )

    print(f"\nDone — {len(made)} file(s) next to the input. If the seam/edges look wrong, retune --fov (190-210).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
