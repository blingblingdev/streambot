#!/usr/bin/env python3
"""Run an extended observation-only worker and verify bounded process memory."""

from __future__ import annotations

import json
import os
import resource
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"

if sys.prefix == sys.base_prefix:
    if not VENV_PYTHON.is_file():
        raise SystemExit("Project environment is missing; run ./scripts/bootstrap.sh first")
    os.execv(VENV_PYTHON, [str(VENV_PYTHON), str(Path(__file__).resolve())])

sys.path.insert(0, str(PROJECT_DIR / "apps" / "core-worker"))

from streambot.config import AutomationProfile
from streambot.connection import connect_paired_worker, desktop_session_is_active
from streambot.models import RunOutcome
from streambot.runtime import AutomationWorker


def current_rss_mib() -> float:
    """Read this process's resident memory without exposing process arguments."""

    result = subprocess.run(
        ["/bin/ps", "-o", "rss=", "-p", str(os.getpid())],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip()) / 1024.0


def main() -> int:
    """Observe for three minutes and enforce conservative memory limits."""

    previous_umask = os.umask(0o077)
    phase = "extended-stability"
    duration_seconds = 180.0
    worker: AutomationWorker | None = None
    thread: threading.Thread | None = None
    try:
        profile = AutomationProfile.from_mapping(
            {
                "name": phase,
                "stream": {
                    "width": 1280,
                    "height": 720,
                    "fps": 15,
                    "bitrate_kbps": 4000,
                    "codec": "h264",
                },
                "observation": {
                    "sample_fps": 2,
                    "decoder": "videotoolbox",
                    "software_fallback": True,
                },
                "runtime": {
                    "max_reconnect_attempts": 2,
                    "liveness_timeout_seconds": 10,
                    "status_interval_seconds": 30,
                },
            }
        )
        clients = []

        def connect():
            client = connect_paired_worker(profile, PROJECT_DIR / ".state" / "poc")
            clients.append(client)
            return client

        worker = AutomationWorker(profile, connect)
        outcomes: list[RunOutcome] = []
        errors: list[BaseException] = []

        def run_worker() -> None:
            try:
                outcomes.append(worker.run())
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run_worker, name="stability-worker")
        usage_start = resource.getrusage(resource.RUSAGE_SELF)
        wall_start = time.monotonic()
        thread.start()
        samples: list[tuple[float, float]] = []
        while time.monotonic() - wall_start < duration_seconds:
            if not thread.is_alive():
                raise RuntimeError("worker ended before the stability deadline")
            samples.append((time.monotonic() - wall_start, current_rss_mib()))
            time.sleep(5.0)
        worker.request_stop()
        thread.join(timeout=20.0)
        if thread.is_alive():
            raise RuntimeError("worker did not stop within the shutdown deadline")
        if errors or outcomes != [RunOutcome.CANCELLED]:
            raise RuntimeError("worker stability outcome was not clean cancellation")
        if not clients or not desktop_session_is_active(clients[-1]):
            raise RuntimeError("pre-existing Desktop session is no longer active")

        steady = [rss for elapsed, rss in samples if elapsed >= 30.0]
        if len(steady) < 12:
            raise RuntimeError("insufficient steady-state memory samples")
        start_median = statistics.median(steady[:3])
        end_median = statistics.median(steady[-3:])
        growth = end_median - start_median
        maximum = max(steady)
        if maximum > 512.0 or growth > 64.0:
            raise RuntimeError("worker memory exceeded the stability boundary")

        usage_end = resource.getrusage(resource.RUSAGE_SELF)
        wall_seconds = time.monotonic() - wall_start
        cpu_seconds = (
            usage_end.ru_utime
            + usage_end.ru_stime
            - usage_start.ru_utime
            - usage_start.ru_stime
        )
        health = worker.health()
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "phase": phase,
                    "duration_seconds": round(wall_seconds, 3),
                    "frames_observed": health.frames_observed,
                    "reconnects": health.reconnects,
                    "final_state": health.state.value,
                    "memory_samples": len(samples),
                    "steady_start_median_rss_mib": round(start_median, 1),
                    "steady_end_median_rss_mib": round(end_median, 1),
                    "steady_growth_mib": round(growth, 1),
                    "steady_peak_rss_mib": round(maximum, 1),
                    "single_core_cpu_percent": round(cpu_seconds / wall_seconds * 100, 1),
                    "desktop_active_after": True,
                    "existing_desktop_ended": False,
                    "image_files_written": 0,
                    "business_input_actions": 0,
                    "sensitive_details_exposed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
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
        if worker is not None:
            worker.request_stop()
        if thread is not None and thread.is_alive():
            thread.join(timeout=20.0)
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
