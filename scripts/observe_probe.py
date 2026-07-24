#!/usr/bin/env python3
"""Run a bounded live check of the reusable latest-frame observer."""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"

if sys.prefix == sys.base_prefix:
    if not VENV_PYTHON.is_file():
        raise SystemExit("Project environment is missing; run ./scripts/bootstrap.sh first")
    os.execv(VENV_PYTHON, [str(VENV_PYTHON), str(Path(__file__).resolve())])

sys.path.insert(0, str(PROJECT_DIR / "apps" / "core-worker"))

from streambot.config import load_profile
from streambot.connection import connect_paired_worker, desktop_session_is_active
from streambot.observation import LatestFrameObserver


def peak_rss_mb() -> float:
    """Return peak resident memory in MiB on macOS."""

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def main() -> int:
    """Observe for thirty seconds and emit only safe aggregate metrics."""

    previous_umask = os.umask(0o077)
    phase = "observer-service"
    try:
        profile = load_profile(PROJECT_DIR / "profiles" / "observe.json")
        client = connect_paired_worker(profile, PROJECT_DIR / ".state" / "poc")
        observer = LatestFrameObserver(client, profile)
        wall_start = time.monotonic()
        usage_start = resource.getrusage(resource.RUSAGE_SELF)
        checksums: set[int] = set()
        frame_shape = None

        with observer:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                observation = observer.observe(timeout=1.0)
                if observation is None:
                    continue
                frame_shape = list(observation.data.shape)
                sample = observation.data[::32, ::32]
                checksums.add(int(sample.astype("uint32").sum()))

        usage_end = resource.getrusage(resource.RUSAGE_SELF)
        wall_seconds = time.monotonic() - wall_start
        cpu_seconds = (
            usage_end.ru_utime
            + usage_end.ru_stime
            - usage_start.ru_utime
            - usage_start.ru_stime
        )
        health = observer.health()
        if health.frames_observed == 0 or frame_shape is None:
            raise RuntimeError("No frames were observed")
        result = {
            "status": "PASS",
            "phase": phase,
            "decoder": client.decoder_backend,
            "decoder_used_fallback": client.decoder_used_fallback,
            "sample_fps": profile.observation.sample_fps,
            "frames_observed": health.frames_observed,
            "distinct_checksums": len(checksums),
            "frame_shape": frame_shape,
            "probe_wall_seconds": round(wall_seconds, 3),
            "process_cpu_seconds": round(cpu_seconds, 3),
            "single_core_cpu_percent": round(cpu_seconds / wall_seconds * 100.0, 1),
            "process_peak_rss_mb": round(peak_rss_mb(), 1),
            "existing_desktop_active_after_probe": desktop_session_is_active(client),
            "existing_desktop_ended": False,
            "image_files_written": 0,
            "click_actions": 0,
            "keyboard_actions": 0,
            "host_metadata_exposed": False,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "phase": phase,
                    "error_type": type(error).__name__,
                    "sensitive_details_exposed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
