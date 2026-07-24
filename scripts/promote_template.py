#!/usr/bin/env python3
"""Promote a fixture crop into a committed template asset.

Cuts a region from a captured fixture image and stores it as a
pickle-disabled `.npy` BGR array under the target's `assets/` directory,
recording provenance (source fixture basename, region, date, note) in
`assets/templates.json`. Committed template assets are how palette-fragile
detectors become structural: a new visual variant observed live is promoted
here as data instead of patched as Python.

Usage:
  scripts/promote_template.py jobs/<t> .fixtures/<set>/frame.png \
      --name end-icon-unreached --region X Y W H [--note "..."] [--preview out.png]
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

if sys.prefix == sys.base_prefix:
    if not VENV_PYTHON.is_file():
        raise SystemExit("Project environment is missing; run ./scripts/bootstrap.sh first")
    os.execv(
        VENV_PYTHON,
        [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
    )

import numpy as np  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="jobs/<target> directory")
    parser.add_argument("fixture", type=Path, help="source fixture image")
    parser.add_argument("--name", required=True, help="asset name (kebab-case)")
    parser.add_argument(
        "--region",
        nargs=4,
        type=int,
        required=True,
        metavar=("X", "Y", "W", "H"),
    )
    parser.add_argument("--note", default="")
    parser.add_argument(
        "--preview",
        type=Path,
        default=None,
        help="optionally write a PNG copy of the crop for visual inspection",
    )
    args = parser.parse_args()

    from PIL import Image

    with Image.open(args.fixture) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    frame = rgb[:, :, ::-1]
    x, y, w, h = args.region
    height, width = frame.shape[:2]
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
        raise SystemExit(f"region {args.region} outside frame {width}x{height}")
    crop = frame[y : y + h, x : x + w].copy()

    assets_dir = args.target / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_path = assets_dir / f"{args.name}.npy"
    np.save(asset_path, crop, allow_pickle=False)

    provenance_path = assets_dir / "templates.json"
    provenance = (
        json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance_path.is_file()
        else {"schema_version": 1, "templates": {}}
    )
    provenance["templates"][args.name] = {
        "source_fixture": args.fixture.name,
        "region": [x, y, w, h],
        "frame_size": [width, height],
        "promoted_on": _datetime.date.today().isoformat(),
        "note": args.note,
    }
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    if args.preview is not None:
        Image.fromarray(crop[:, :, ::-1]).save(args.preview)

    print(
        json.dumps(
            {
                "ok": True,
                "asset": str(asset_path.relative_to(args.target)),
                "shape": list(crop.shape),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
