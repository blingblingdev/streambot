"""Deterministic declarative automation state machine."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from .config import TransitionSettings, WorkflowSettings, WorkflowStateSettings
from .models import RunOutcome


class ActionExecutor(Protocol):
    """Action boundary required by the decision engine."""

    def execute(self, action: str, idempotency_key: str) -> None:
        """Execute or suppress one action using the supplied stable key."""


@dataclass(frozen=True)
class TransitionEvent:
    """Metadata-only state-machine event."""

    sequence: int
    event_type: str
    state: str
    transition: str | None = None
    target: str | None = None
    retry_count: int = 0
    action_count: int = 0
    error_type: str | None = None


@dataclass(frozen=True)
class EngineSnapshot:
    """Current state without observations, actions, or credentials."""

    state: str
    outcome: RunOutcome | None
    event_count: int
    completed_idempotency_keys: int


class WorkflowEngine:
    """Evaluate signals, execute bounded actions, and commit state transitions."""

    def __init__(
        self,
        settings: WorkflowSettings,
        executor: ActionExecutor | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if any(
            transition.actions
            for state in settings.states
            for transition in state.transitions
        ) and executor is None:
            raise ValueError("workflow actions require an executor")
        self._settings = settings
        self._states = {state.name: state for state in settings.states}
        self._executor = executor
        self._clock = clock
        self._state = settings.initial_state
        self._entered_at = clock()
        self._outcome: RunOutcome | None = None
        self._cancelled = False
        self._retry_counts: dict[str, int] = {}
        self._completed_keys: set[str] = set()
        self._events: deque[TransitionEvent] = deque(
            maxlen=settings.event_history_limit
        )
        self._event_count = 0
        self._apply_terminal_state()

    def _record(
        self,
        event_type: str,
        *,
        transition: TransitionSettings | None = None,
        target: str | None = None,
        retry_count: int = 0,
        error_type: str | None = None,
    ) -> None:
        self._event_count += 1
        self._events.append(
            TransitionEvent(
                sequence=self._event_count,
                event_type=event_type,
                state=self._state,
                transition=transition.name if transition is not None else None,
                target=target,
                retry_count=retry_count,
                action_count=len(transition.actions) if transition is not None else 0,
                error_type=error_type,
            )
        )

    def _apply_terminal_state(self) -> None:
        terminal = self._states[self._state].terminal
        if terminal == "success":
            self._outcome = RunOutcome.SUCCESS
        elif terminal == "failure":
            self._outcome = RunOutcome.FAILURE

    def _enter(self, target: str) -> None:
        self._state = target
        self._entered_at = self._clock()
        self._apply_terminal_state()

    def _execute_transition(self, transition: TransitionSettings) -> None:
        key = transition.idempotency_key
        if transition.actions and key in self._completed_keys:
            self._record(
                "duplicate_suppressed", transition=transition, target=transition.target
            )
            if transition.target != self._state:
                self._enter(transition.target)
            return
        try:
            if transition.actions:
                if self._executor is None or key is None:
                    raise RuntimeError("action executor is unavailable")
                for index, action in enumerate(transition.actions):
                    self._executor.execute(action, f"{key}:{index}")
        except Exception as error:
            if key is None or transition.failure_state is None:
                raise
            retry_count = self._retry_counts.get(key, 0) + 1
            self._retry_counts[key] = retry_count
            exhausted = retry_count > transition.max_retries
            self._record(
                "retry_exhausted" if exhausted else "retry",
                transition=transition,
                target=transition.failure_state if exhausted else self._state,
                retry_count=retry_count,
                error_type=type(error).__name__,
            )
            if exhausted:
                self._enter(transition.failure_state)
            return
        if key is not None:
            self._completed_keys.add(key)
            self._retry_counts.pop(key, None)
        self._record("transition", transition=transition, target=transition.target)
        self._enter(transition.target)

    def tick(self, signals: Mapping[str, bool]) -> EngineSnapshot:
        """Advance at most one transition from a metadata-only signal map."""

        if any(not isinstance(value, bool) for value in signals.values()):
            raise ValueError("signals must contain booleans")
        if self._outcome is not None or self._cancelled:
            return self.snapshot()
        state = self._states[self._state]
        if (
            state.timeout_seconds is not None
            and self._clock() - self._entered_at >= state.timeout_seconds
        ):
            if state.timeout_state is None:
                raise RuntimeError("state timeout target is unavailable")
            self._record("timeout", target=state.timeout_state)
            self._enter(state.timeout_state)
            return self.snapshot()
        for transition in state.transitions:
            if transition.signal not in signals:
                continue
            if signals[transition.signal] is transition.equals:
                self._execute_transition(transition)
                break
        return self.snapshot()

    def cancel(self) -> EngineSnapshot:
        """Cancel further decisions without executing another action."""

        if self._outcome is None and not self._cancelled:
            self._cancelled = True
            self._outcome = RunOutcome.CANCELLED
            self._record("cancelled")
        return self.snapshot()

    def snapshot(self) -> EngineSnapshot:
        return EngineSnapshot(
            state=self._state,
            outcome=self._outcome,
            event_count=self._event_count,
            completed_idempotency_keys=len(self._completed_keys),
        )

    def events(self) -> tuple[TransitionEvent, ...]:
        """Return the bounded metadata-only event history."""

        return tuple(self._events)
