#!/usr/bin/env python3
"""Target-agnostic core worker: frames in, input out, over the local IPC socket.

This is the reusable engine half of the "one core engine + hot-pluggable jobs"
split. It owns exactly one stream connection and does three things, none of
them target-specific:

  1. publishes the latest decoded frame so a job (or the console) can snapshot
     it over IPC;
  2. serializes external input commands (click / point / glide / keys) onto the
     single input-owning session;
  3. exposes health and the `report-scene` seam a job uses to publish its own
     detected controls for the console overlay.

It holds NO perception, NO OCR, NO scanner, and NO page-state of its own — a
job under `jobs/<name>/` brings that. Because the engine carries no target
content, the console overlay reflects whatever job is running (via
`report-scene`), never a baked-in target. Automation stays disabled here: jobs
drive input through external IPC commands, which the control plane only accepts
while automation is off.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_DIR / "apps" / "core-worker"))

from streambot.config import AutomationProfile
from streambot.control_plane import PersistentControlPlane
from streambot.input import SafeInputDriver
from streambot.observation import Observation
from streambot.worker_main import run_worker_process

# Target-agnostic socket: distinct from any single job's socket so the console
# supervises the shared engine, and from the platform default so tests are not
# disturbed. Jobs and the console point here.
DEFAULT_SOCKET_PATH = Path(".state/poc/core-control.sock")

# Low-cost automation baseline (720p15 H.264), matching the project default.
# Only the generic input map is declared; no perception, regions, or templates.
CORE_PROFILE = {
    "name": "core-worker",
    "stream": {
        "width": 1280,
        "height": 720,
        "fps": 15,
        "bitrate_kbps": 4000,
        "codec": "h264",
    },
    "observation": {
        "sample_fps": 8,
        "decoder": "videotoolbox",
        "software_fallback": True,
    },
    "safety": {
        "preserve_existing_desktop": True,
        "dry_run": False,
        "max_actions_per_minute": 180,
    },
    "actions": [
        {"name": "click", "type": "mouse_button", "button": "left"},
        {
            "name": "mouse-down",
            "type": "mouse_button",
            "button": "left",
            "operation": "press",
        },
        {
            "name": "mouse-up",
            "type": "mouse_button",
            "button": "left",
            "operation": "release",
        },
        {"name": "fast-forward", "type": "key", "key_code": 81},
        {"name": "escape", "type": "key", "key_code": 27},
        {"name": "backspace", "type": "key", "key_code": 8},
        {"name": "enter", "type": "key", "key_code": 13},
    ],
}


class FramePublishRuntime:
    """Publish the latest frame each tick; commands drain on the IPC executor.

    Implements the `reset()` / `advance(observation)` contract the
    `AutomationWorker` calls on its persistent-perception slot, but does no
    perception: `advance` only republishes the latest observation. External
    input commands are drained by `PersistentControlPlane.start_executor`, which
    runs off this frame loop so a multi-second glide never stalls frame
    publishing.
    """

    def __init__(self, control: PersistentControlPlane, inputs: SafeInputDriver) -> None:
        self._control = control
        self._executor_started = False
        # The command executor owns the same serialized input driver; start it
        # once (not per reconnect) so reconnects never leak drain threads.
        control.start_executor(inputs)
        self._executor_started = True

    def reset(self) -> None:
        """Connection-scoped state — none to clear for a frame-only runtime."""

    def advance(self, observation: Observation) -> None:
        self._control.publish_observation(observation)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the target-agnostic core worker (frames + input + IPC)"
    )
    parser.add_argument(
        "--state-dir", type=Path, default=PROJECT_DIR / ".state" / "poc"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MOONLIGHT_HOST"),
        help="connect to this host address directly instead of mDNS discovery; "
        "defaults to the MOONLIGHT_HOST environment variable",
    )
    parser.add_argument(
        "--control-socket",
        type=Path,
        default=PROJECT_DIR / DEFAULT_SOCKET_PATH,
        help="local Unix socket used by the console and hot-pluggable jobs",
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=0.0,
        help="stop after a positive bounded duration; zero runs until signalled",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_runtime_seconds < 0:
        raise SystemExit("max runtime must be non-negative")
    previous_umask = os.umask(0o077)
    # Frame export is enabled so the console and jobs can snapshot the latest
    # frame over IPC; frames are still never written by the worker itself.
    control = PersistentControlPlane(args.control_socket, allow_frame_export=True)
    try:
        return run_worker_process(
            AutomationProfile.from_mapping(CORE_PROFILE),
            args.state_dir,
            host=args.host,
            control=control,
            persistent_perception_factory=lambda inputs: FramePublishRuntime(
                control, inputs
            ),
            max_runtime_seconds=args.max_runtime_seconds,
        )
    finally:
        control.close()
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
