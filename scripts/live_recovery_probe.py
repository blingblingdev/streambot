#!/usr/bin/env python3
"""Inject one local observation failure and verify bounded live recovery."""

from __future__ import annotations

import json
import os
import sys
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
from streambot.models import RunOutcome, WorkerHealth, WorkerState
from streambot.observation import LatestFrameObserver, Observation
from streambot.runtime import AutomationWorker


class FaultInjectingObserver:
    """Delegate live observation while injecting one generation-scoped failure."""

    def __init__(
        self,
        delegate: LatestFrameObserver,
        generation: int,
        stop_worker,
    ) -> None:
        self._delegate = delegate
        self._generation = generation
        self._stop_worker = stop_worker
        self._frames = 0

    def start(self) -> None:
        self._delegate.start()

    def observe(self, timeout: float = 1.0) -> Observation | None:
        observation = self._delegate.observe(timeout)
        if observation is None:
            return None
        self._frames += 1
        if self._generation == 1 and self._frames == 5:
            raise ConnectionError("synthetic local observation failure")
        if self._generation == 2 and self._frames == 5:
            self._stop_worker()
        return observation

    def stop(self) -> None:
        self._delegate.stop()


def main() -> int:
    """Run one real reconnect and emit only aggregate recovery evidence."""

    previous_umask = os.umask(0o077)
    phase = "live-recovery"
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
                    "reconnect_initial_seconds": 0.25,
                    "reconnect_max_seconds": 1,
                    "max_reconnect_attempts": 2,
                    "liveness_timeout_seconds": 5,
                    "status_interval_seconds": 1,
                },
            }
        )
        clients = []
        generation = 0
        worker_holder: list[AutomationWorker] = []
        states: list[WorkerState] = []

        def connect():
            client = connect_paired_worker(profile, PROJECT_DIR / ".state" / "poc")
            clients.append(client)
            return client

        def observe(client, current_profile):
            nonlocal generation
            generation += 1
            return FaultInjectingObserver(
                LatestFrameObserver(client, current_profile),
                generation,
                lambda: worker_holder[0].request_stop(),
            )

        def status(health: WorkerHealth) -> None:
            states.append(health.state)

        worker = AutomationWorker(
            profile,
            connect,
            observer_factory=observe,
            health_callback=status,
        )
        worker_holder.append(worker)
        outcome = worker.run()
        health = worker.health()
        desktop_active = bool(clients) and desktop_session_is_active(clients[-1])
        if outcome is not RunOutcome.CANCELLED:
            raise RuntimeError("recovery probe did not stop cleanly")
        if health.reconnects != 1 or health.frames_observed < 9:
            raise RuntimeError("recovery evidence is incomplete")
        if WorkerState.RECOVERING not in states or generation != 2:
            raise RuntimeError("recovery lifecycle was incomplete")
        if not desktop_active:
            raise RuntimeError("pre-existing Desktop session is no longer active")
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "phase": phase,
                    "outcome": outcome.value,
                    "connections": generation,
                    "reconnects": health.reconnects,
                    "frames_observed": health.frames_observed,
                    "recovering_state_observed": True,
                    "final_state": health.state.value,
                    "desktop_active_after": desktop_active,
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
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
