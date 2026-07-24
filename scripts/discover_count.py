#!/usr/bin/env python3
"""Count visible Sunshine hosts without exposing discovered metadata."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"

if sys.prefix == sys.base_prefix:
    if not VENV_PYTHON.is_file():
        raise SystemExit("Project environment is missing; run ./scripts/bootstrap.sh first")
    os.execv(VENV_PYTHON, [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

from moonlight_python import MoonlightClient


def run() -> dict[str, object]:
    """Return a metadata-free discovery summary."""

    previous_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="streambot-discovery-") as temp_dir:
            client = MoonlightClient(config_dir=Path(temp_dir) / "identity")
            captured_stdout = io.StringIO()
            captured_stderr = io.StringIO()
            with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(
                captured_stderr
            ):
                servers = client.discover(timeout=5.0)

            return {
                "status": "PASS",
                "visible_host_count": len(servers),
                "host_metadata_exposed": False,
                "pairing_actions": 0,
                "stream_actions": 0,
                "remote_input_actions": 0,
            }
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))

