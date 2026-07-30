"""Metadata-only runtime result models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WorkerState(StrEnum):
    """Externally reportable worker lifecycle state."""

    STARTING = "starting"
    OBSERVING = "observing"
    ACTING = "acting"
    RECOVERING = "recovering"
    WAITING = "waiting"
    DETACHED = "detached"
    STOPPED = "stopped"
    FAILED = "failed"


class RunOutcome(StrEnum):
    """Terminal automation outcome."""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WorkerHealth:
    """Safe health snapshot that contains no target metadata or frames."""

    state: WorkerState
    frames_observed: int = 0
    actions_sent: int = 0
    reconnects: int = 0
    last_error_type: str | None = None
    # Short allowlisted ConnectFailure code (e.g. "desktop_session_inactive")
    # so operators see why a connection cannot proceed; never free-form text.
    last_error_code: str | None = None
