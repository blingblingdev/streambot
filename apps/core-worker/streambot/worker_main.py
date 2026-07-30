"""Shared bootstrap for every long-running worker process.

This module is the single place that wires a worker to its stream
connection. Entry scripts (the game-agnostic core worker and any job-specific
worker) stay thin: they parse arguments, build their control plane and
perception factory, and delegate here. No script outside ``streambot``
may construct its own connection: that keeps connection behavior — typed
failure classification, the active-Desktop-session requirement, and patient
environmental waiting — identical for every worker.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Callable, Protocol

from .config import AutomationProfile
from .connection import connect_paired_worker
from .input import SafeInputDriver
from .models import RunOutcome, WorkerHealth
from .runtime import AutomationWorker, health_payload


class WorkerControlPlane(Protocol):
    """Minimal control-plane surface the shared bootstrap drives."""

    def publish_health(self, payload: dict[str, object]) -> None: ...
    def start(self) -> None: ...


def run_worker_process(
    profile: AutomationProfile,
    state_dir: Path,
    *,
    host: str | None,
    control: WorkerControlPlane,
    persistent_perception_factory: Callable[[SafeInputDriver], object],
    max_runtime_seconds: float = 0.0,
) -> int:
    """Run one supervised worker until completion or a stop signal.

    Owns the connection factory (paired identity, typed failure
    classification, required active Desktop session), health publication,
    signal wiring, and the metadata-only result line. Returns the process
    exit code. The caller keeps ownership of ``control`` cleanup and any
    additional resources.
    """

    if max_runtime_seconds < 0:
        raise ValueError("max runtime must be non-negative")

    def emit_health(health: WorkerHealth) -> None:
        payload = {"type": "health", **health_payload(health)}
        control.publish_health(payload)
        print(json.dumps(payload, sort_keys=True), flush=True)

    worker = AutomationWorker(
        profile,
        # Session policy: join an active Desktop session, launch Desktop on
        # an idle host, wait (typed) while another application's session is
        # active. Quitting host sessions is forbidden unconditionally.
        lambda: connect_paired_worker(
            profile, state_dir, host=host, manage_desktop_session=True
        ),
        health_callback=emit_health,
        persistent_perception_factory=persistent_perception_factory,
    )

    connection_controls = getattr(control, "set_connection_controls", None)
    if callable(connection_controls):
        # Operator stream detach/attach over IPC (console buttons). Optional:
        # a control plane without the setter simply has no such commands.
        connection_controls(worker.request_detach, worker.request_attach)

    def request_stop(_signum=None, _frame=None) -> None:
        worker.request_stop()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if max_runtime_seconds:
        signal.setitimer(signal.ITIMER_REAL, max_runtime_seconds)
        signal.signal(signal.SIGALRM, request_stop)
    control.start()
    outcome = worker.run()
    print(
        json.dumps(
            {
                "type": "result",
                "outcome": outcome.value,
                "sensitive_details_exposed": False,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if outcome in {RunOutcome.SUCCESS, RunOutcome.CANCELLED} else 1
