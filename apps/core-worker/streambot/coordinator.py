"""Serialized single-owner coordination for perception-driven input."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import time
from typing import Callable, Protocol

from .events import ActionCandidate, PerceptionEvent


class CoordinatorState(StrEnum):
    OBSERVING = "observing"
    ACTION_PENDING = "action-pending"
    ACTING = "acting"
    VERIFYING = "verifying"
    RETRYING = "retrying"
    FAILED = "failed"


@dataclass(frozen=True)
class InteractionPolicy:
    """Allowlisted behavior and feedback bounds for one semantic scene."""

    scene_id: str
    allowed_action_kinds: tuple[str, ...]
    minimum_confidence: float = 0.55
    retry_allowed: bool = False
    retry_after_seconds: float = 0.6
    feedback_timeout_seconds: float = 1.5
    clear_samples: int = 2
    recover_after_timeout: bool = False

    def __post_init__(self) -> None:
        if not self.scene_id or not self.allowed_action_kinds:
            raise ValueError("interaction policy identity is required")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValueError("minimum confidence must be bounded")
        if self.retry_after_seconds <= 0 or self.feedback_timeout_seconds <= 0:
            raise ValueError("feedback timing bounds must be positive")
        if self.clear_samples < 1:
            raise ValueError("clear sample count must be positive")


class ActionDispatcher(Protocol):
    def dispatch(self, candidate: ActionCandidate, idempotency_key: str) -> None: ...
    def release_all(self) -> None: ...


@dataclass
class CoordinatorMetrics:
    actions_attempted: int = 0
    actions_confirmed: int = 0
    actions_retried: int = 0
    actions_failed_closed: int = 0
    actions_suppressed: int = 0
    dispatch_latency_count: int = 0
    dispatch_latency_seconds_total: float = 0.0
    feedback_latency_count: int = 0
    feedback_latency_seconds_total: float = 0.0


class InteractionCoordinator:
    """Consume at most one leased action and verify it frame by frame."""

    def __init__(
        self,
        dispatcher: ActionDispatcher,
        policies: tuple[InteractionPolicy, ...],
        *,
        stream_width: int,
        stream_height: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dispatcher = dispatcher
        self.policies = {policy.scene_id: policy for policy in policies}
        self.stream_width = stream_width
        self.stream_height = stream_height
        self.clock = clock
        self.metrics = CoordinatorMetrics()
        self.workflow_epoch = 0
        self.state = CoordinatorState.OBSERVING
        self.last_committed_action_frame = -1
        self._event: PerceptionEvent | None = None
        self._candidate: ActionCandidate | None = None
        self._policy: InteractionPolicy | None = None
        self._idempotency_key = ""
        self._started_at = 0.0
        self._clear_samples = 0
        self._retried = False

    @property
    def is_idle(self) -> bool:
        return self.state is CoordinatorState.OBSERVING

    @property
    def active_scene_id(self) -> str | None:
        return None if self._event is None else self._event.scene_id

    def reset(self) -> None:
        self.dispatcher.release_all()
        self.workflow_epoch += 1
        self.state = CoordinatorState.OBSERVING
        # Frame numbers restart low after a reconnect; a stale committed-frame
        # watermark would reject every subsequent action until it is exceeded.
        self.last_committed_action_frame = -1
        self._event = None
        self._candidate = None
        self._policy = None
        self._clear_samples = 0
        self._retried = False

    def _reject(self) -> bool:
        self.metrics.actions_suppressed += 1
        return False

    def accept(self, event: PerceptionEvent) -> bool:
        """Lease and dispatch one fresh allowlisted action event."""

        now = self.clock()
        if event.event_type != "action-ready" or not self.is_idle:
            return self._reject()
        if event.workflow_epoch != self.workflow_epoch or event.is_expired(now):
            return self._reject()
        if event.frame_number <= self.last_committed_action_frame:
            return self._reject()
        policy = self.policies.get(event.scene_id)
        if policy is None or event.confidence < policy.minimum_confidence:
            return self._reject()
        if len(event.candidates) != 1:
            return self._reject()
        candidate = event.candidates[0]
        if candidate.action_kind not in policy.allowed_action_kinds:
            return self._reject()
        if candidate.x is not None and not (
            0 <= candidate.x < self.stream_width and 0 <= candidate.y < self.stream_height
        ):
            return self._reject()

        self.state = CoordinatorState.ACTION_PENDING
        self._event = event
        self._candidate = candidate
        self._policy = policy
        self._idempotency_key = (
            f"perception-{self.workflow_epoch}-{event.sequence}-{candidate.candidate_id}"
        )
        self._started_at = now
        self._clear_samples = 0
        self._retried = False
        self.state = CoordinatorState.ACTING
        if candidate.action_kind != "wait-for-timeout":
            try:
                self.dispatcher.dispatch(candidate, self._idempotency_key)
            except Exception:
                self.fail_closed()
                return False
            self.metrics.actions_attempted += 1
        self.metrics.dispatch_latency_count += 1
        self.metrics.dispatch_latency_seconds_total += max(0.0, now - event.emitted_at)
        self.state = CoordinatorState.VERIFYING
        return True

    def observe_feedback(self, frame_number: int, original_layout_visible: bool) -> bool:
        """Advance non-blocking clear, retry, and timeout feedback."""

        if self.state not in {CoordinatorState.VERIFYING, CoordinatorState.RETRYING}:
            return False
        event = self._event
        policy = self._policy
        candidate = self._candidate
        if event is None or policy is None or candidate is None:
            self.fail_closed()
            return False
        now = self.clock()
        if not original_layout_visible:
            self._clear_samples += 1
            if self._clear_samples >= policy.clear_samples:
                self.metrics.actions_confirmed += 1
                self.metrics.feedback_latency_count += 1
                self.metrics.feedback_latency_seconds_total += max(
                    0.0, now - self._started_at
                )
                self.last_committed_action_frame = event.frame_number
                self.workflow_epoch += 1
                self.state = CoordinatorState.OBSERVING
                self._event = None
                return True
            return False
        self._clear_samples = 0
        elapsed = now - self._started_at
        # Event expiry protects initial acceptance. Once leased, the interaction
        # policy timeout owns feedback and retry timing.
        if elapsed >= policy.feedback_timeout_seconds:
            if policy.recover_after_timeout:
                self._recover_after_timeout(event.frame_number)
            else:
                self.fail_closed()
            return False
        if (
            candidate.action_kind != "wait-for-timeout"
            and policy.retry_allowed
            and not self._retried
            and elapsed >= policy.retry_after_seconds
        ):
            self.state = CoordinatorState.RETRYING
            try:
                self.dispatcher.dispatch(candidate, f"{self._idempotency_key}-retry")
            except Exception:
                self.fail_closed()
                return False
            self.metrics.actions_attempted += 1
            self.metrics.actions_retried += 1
            self._retried = True
            self.state = CoordinatorState.VERIFYING
            return False
        return False

    def fail_closed(self) -> None:
        self.state = CoordinatorState.FAILED
        self.metrics.actions_failed_closed += 1
        self.dispatcher.release_all()
        self._event = None
        self._candidate = None
        self._policy = None

    def _recover_after_timeout(self, frame_number: int) -> None:
        """Release a fixed-layout lease so a fresh observation can retry it."""

        self.metrics.actions_failed_closed += 1
        self.dispatcher.release_all()
        self.last_committed_action_frame = frame_number
        self.workflow_epoch += 1
        self.state = CoordinatorState.OBSERVING
        self._event = None
        self._candidate = None
        self._policy = None
        self._clear_samples = 0
        self._retried = False
