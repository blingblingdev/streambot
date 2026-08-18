#!/usr/bin/env python3
"""Record an operator demonstration through the worker's snapshot channel.

The operator performs the task by hand on the host; this script watches the
same stream through the already-connected worker and keeps a bounded,
change-driven sequence of JPEG frames plus a timestamp index. The recording
is analysis material for turning a demonstration into a job, and its
keyframes double as that job's first template fixtures.

Observation only: nothing here sends input. Frames land under the gitignored
`.fixtures/recordings/<label>/` with an `index.jsonl` beside them:

  scripts/record_session.py --socket .state/poc/core-control.sock \
      --label workshop-refill --max-seconds 600

Stop early with Ctrl-C; the summary line is printed either way. A frame is
kept when it differs enough from the last kept frame, and every few seconds
regardless, so both the actions and the waits between them survive with
their durations.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

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
from PIL import Image  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

from streambot.control_plane import send_control_command  # noqa: E402

RECORDINGS_ROOT = PROJECT_ROOT / ".fixtures" / "recordings"
MAX_SECONDS_CAP = 3600
MAX_KEPT_FRAMES = 2400

# The diff is computed on a small grayscale thumbnail so JPEG noise and the
# stream's dithering do not read as change; the threshold is the fraction of
# full-scale brightness the average pixel must move.
THUMB_SIZE = (160, 90)


class FrameKeeper:
    """Decide which frames of a steady stream are worth keeping.

    Pure decision logic, separated from IPC and disk so it can be tested:
    feed it small grayscale float arrays and clock values, it answers with
    a reason to keep ("first", "change", "heartbeat") or None.
    """

    def __init__(
        self, *, diff_threshold: float = 0.012, heartbeat_seconds: float = 3.0
    ) -> None:
        self.diff_threshold = diff_threshold
        self.heartbeat_seconds = heartbeat_seconds
        self._last_kept: np.ndarray | None = None
        self._last_kept_at = 0.0

    def decide(self, thumb: np.ndarray, now: float) -> tuple[str, float] | None:
        """Return (reason, diff) to keep this frame, or None to drop it."""

        if self._last_kept is None:
            self._keep(thumb, now)
            return ("first", 1.0)
        diff = float(np.mean(np.abs(thumb - self._last_kept))) / 255.0
        if diff >= self.diff_threshold:
            self._keep(thumb, now)
            return ("change", diff)
        if now - self._last_kept_at >= self.heartbeat_seconds:
            self._keep(thumb, now)
            return ("heartbeat", diff)
        return None

    def _keep(self, thumb: np.ndarray, now: float) -> None:
        self._last_kept = thumb.astype(np.float32)
        self._last_kept_at = now


def thumb_of(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(
            image.convert("L").resize(THUMB_SIZE, Image.BILINEAR), dtype=np.float32
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--fps", type=float, default=4.0)
    parser.add_argument("--diff-threshold", type=float, default=0.012)
    parser.add_argument("--heartbeat-seconds", type=float, default=3.0)
    args = parser.parse_args()

    label = args.label.strip().lower()
    if not label or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in label):
        print("label must be lowercase kebab-case", file=sys.stderr)
        return 2
    if not (1 <= args.max_seconds <= MAX_SECONDS_CAP):
        print(f"max-seconds must be between 1 and {MAX_SECONDS_CAP}", file=sys.stderr)
        return 2
    if not (0.5 <= args.fps <= 8.0):
        print("fps must be between 0.5 and 8", file=sys.stderr)
        return 2

    target_dir = RECORDINGS_ROOT / label
    if target_dir.exists() and any(target_dir.iterdir()):
        print(f"{target_dir} already holds a recording; pick a new label", file=sys.stderr)
        return 2
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    keeper = FrameKeeper(
        diff_threshold=args.diff_threshold, heartbeat_seconds=args.heartbeat_seconds
    )
    index_path = target_dir / "index.jsonl"
    pending = target_dir / "pending.jpg"
    interval = 1.0 / args.fps
    started = time.time()
    seen = kept = 0
    error: str | None = None

    with index_path.open("a", encoding="utf-8") as index:
        while not stopping:
            now = time.time()
            if now - started >= args.max_seconds:
                break
            if kept >= MAX_KEPT_FRAMES:
                error = "frame budget exhausted"
                break
            response = send_control_command(
                args.socket,
                "snapshot",
                arguments={"output": str(pending)},
                job=f"recording-{label}",
            )
            if not response.get("ok"):
                error = str(response.get("error"))
                break
            seen += 1
            verdict = keeper.decide(thumb_of(pending), now)
            if verdict is None:
                pending.unlink(missing_ok=True)
            else:
                reason, diff = verdict
                name = f"t{now:.2f}-f{response['frame_number']}.jpg"
                final = target_dir / name
                pending.replace(final)
                final.chmod(0o600)
                kept += 1
                index.write(
                    json.dumps(
                        {
                            "t": round(now, 2),
                            "frame_number": response["frame_number"],
                            "file": name,
                            "reason": reason,
                            "diff": round(diff, 4),
                        }
                    )
                    + "\n"
                )
                index.flush()
            time.sleep(max(0.0, interval - (time.time() - now)))

    pending.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "ok": error is None,
                "label": label,
                "seconds": round(time.time() - started, 1),
                "frames_seen": seen,
                "frames_kept": kept,
                "directory": str(target_dir),
                **({"error": error} if error else {}),
            }
        )
    )
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
