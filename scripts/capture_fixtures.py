#!/usr/bin/env python3
"""Capture bounded frame fixtures from the running persistent worker.

Requests snapshots through the worker's IPC socket (no second Moonlight
client) and stores them under the gitignored `.fixtures/<label>/` directory
for offline manifest replay. Capture is explicit, bounded, and refuses to run
when the worker does not expose frame export.

Usage:
  scripts/capture_fixtures.py --socket .state/poc/target-control.sock \
      --label phone-reply --count 5 --interval-seconds 1.0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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

sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

from streambot.control_plane import send_control_command  # noqa: E402

FIXTURES_ROOT = PROJECT_ROOT / ".fixtures"
MAX_COUNT = 60


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    args = parser.parse_args()

    if not (1 <= args.count <= MAX_COUNT):
        print(f"count must be between 1 and {MAX_COUNT}", file=sys.stderr)
        return 2
    label = args.label.strip().lower()
    if not label or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in label):
        print("label must be lowercase kebab-case", file=sys.stderr)
        return 2

    target_dir = FIXTURES_ROOT / label
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    captured = []
    for index in range(args.count):
        output = target_dir / f"pending-{index}.png"
        response = send_control_command(
            args.socket, "snapshot", arguments={"output": str(output)}
        )
        if not response.get("ok"):
            print(json.dumps({"ok": False, "error": response.get("error")}))
            return 1
        frame_number = response["frame_number"]
        final = target_dir / f"frame-{frame_number}.png"
        output.replace(final)
        final.chmod(0o600)
        captured.append(frame_number)
        if index + 1 < args.count:
            time.sleep(max(0.1, args.interval_seconds))
    print(
        json.dumps(
            {
                "ok": True,
                "label": label,
                "captured": len(captured),
                "frame_numbers": captured,
                "directory": str(target_dir),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
