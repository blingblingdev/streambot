#!/usr/bin/env python3
"""Run a network-free validation of the headless streaming dependencies."""

from __future__ import annotations

import importlib.metadata
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"

if sys.prefix == sys.base_prefix:
    if not VENV_PYTHON.is_file():
        raise SystemExit("Project environment is missing; run ./scripts/bootstrap.sh first")
    os.execv(VENV_PYTHON, [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

import numpy as np
from PIL import Image
from moonlight_python import MoonlightClient


EXPECTED_PRIVATE_MODE = 0o600
IDENTITY_FILES = ("key.pem", "cert.pem", "unique_id")


def file_mode(path: Path) -> int:
    """Return only the permission bits for a path."""

    return stat.S_IMODE(path.stat().st_mode)


def run() -> dict[str, object]:
    """Exercise identity creation and an in-memory frame conversion."""

    previous_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="streambot-self-check-") as temp_dir:
            identity_dir = Path(temp_dir) / "identity"
            MoonlightClient(config_dir=identity_dir)

            identity_modes = {
                name: oct(file_mode(identity_dir / name)) for name in IDENTITY_FILES
            }
            invalid_modes = {
                name: mode
                for name, mode in identity_modes.items()
                if int(mode, 8) != EXPECTED_PRIVATE_MODE
            }
            if invalid_modes:
                raise RuntimeError(f"Identity files are not private: {invalid_modes}")

            frame = np.zeros((72, 128, 3), dtype=np.uint8)
            frame[18:54, 32:96] = (40, 180, 240)
            image = Image.fromarray(frame, mode="RGB")
            image_path = Path(temp_dir) / "synthetic-frame.png"
            image.save(image_path)
            loaded = np.asarray(Image.open(image_path).convert("RGB"))

            if loaded.shape != frame.shape or not np.array_equal(loaded, frame):
                raise RuntimeError("Synthetic frame round trip failed")

            return {
                "status": "PASS",
                "network_actions": 0,
                "remote_input_actions": 0,
                "moonlight_python_version": importlib.metadata.version(
                    "moonlight-python"
                ),
                "python_version": sys.version.split()[0],
                "identity_file_modes": identity_modes,
                "synthetic_frame_shape": list(loaded.shape),
            }
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
