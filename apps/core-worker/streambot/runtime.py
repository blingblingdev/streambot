"""Recovering long-running orchestration for headless stream automation."""

from __future__ import annotations

import os
import sys
import time
from threading import Event, RLock
from typing import Callable, Mapping, Protocol

import numpy as np

from .config import AutomationProfile
from .connection import ConnectFailure
from .decision import WorkflowEngine
from .input import InputTransport, MoonlightCffiTransport, SafeInputDriver
from .models import RunOutcome, WorkerHealth, WorkerState
from .observation import LatestFrameObserver, Observation
from .perception import OcrAdapter, PerceptionEngine, TemplateMatcher
from .coordinator import InteractionCoordinator
from .perception_service import PerceptionScheduler


class ObservationSource(Protocol):
    """Lifecycle and frame surface required by the worker."""

    def start(self) -> None: ...
    def observe(self, timeout: float = 1.0) -> Observation | None: ...
    def stop(self) -> None: ...


class PersistentPerceptionRuntime:
    """Advance perception and serialized input on one worker connection."""

    def __init__(
        self,
        scheduler: PerceptionScheduler,
        coordinator: InteractionCoordinator,
        layout_visible: Callable[[Observation], bool],
        resume_playback: Callable[[], None] | None = None,
        interactive_visible: Callable[[Observation], bool] | None = None,
        playback_interval_seconds: float = 1.0,
    ) -> None:
        self.scheduler = scheduler
        self.coordinator = coordinator
        self.layout_visible = layout_visible
        self.resume_playback = resume_playback
        self.interactive_visible = interactive_visible or (lambda _observation: False)
        self.playback_interval_seconds = playback_interval_seconds
        self._playback_started = False
        self._next_playback_at = 0.0

    def reset(self) -> None:
        """Clear connection-scoped layouts, events, and action leases."""

        self.scheduler.reset()
        self.coordinator.reset()
        self._playback_started = False
        self._next_playback_at = 0.0

    def advance(self, observation: Observation) -> None:
        """Consume one latest observation without blocking for feedback."""

        now = self.scheduler.clock()
        if (
            self.resume_playback is not None
            and self.coordinator.is_idle
            and not self.interactive_visible(observation)
            and now >= self._next_playback_at
        ):
            self.resume_playback()
            self._playback_started = True
            self._next_playback_at = now + self.playback_interval_seconds
        if not self.coordinator.is_idle:
            confirmed = self.coordinator.observe_feedback(
                observation.frame_number, self.layout_visible(observation)
            )
            if confirmed and self.resume_playback is not None:
                self.resume_playback()
                self._next_playback_at = now + self.playback_interval_seconds
        if not self.coordinator.is_idle:
            return
        self.scheduler.process(
            observation, workflow_epoch=self.coordinator.workflow_epoch
        )
        event = self.scheduler.broker.mailbox.pop(self.scheduler.clock())
        if event is not None and event.event_type == "action-ready":
            self.coordinator.accept(event)


class ReconnectableInputTransport:
    """Stable input boundary whose live transport changes after reconnects."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._transport: InputTransport | None = None

    def attach(self, transport: InputTransport) -> None:
        with self._lock:
            self._transport = transport

    def detach(self) -> None:
        with self._lock:
            self._transport = None

    def _require(self) -> InputTransport:
        transport = self._transport
        if transport is None:
            raise RuntimeError("input transport is detached")
        return transport

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return bool(self._transport and self._transport.is_connected)

    def mouse_move(self, dx: int, dy: int) -> int:
        with self._lock:
            return self._require().mouse_move(dx, dy)

    def mouse_position(self, x: int, y: int, width: int, height: int) -> int:
        with self._lock:
            return self._require().mouse_position(x, y, width, height)

    def mouse_button(self, action: int, button: int) -> int:
        with self._lock:
            return self._require().mouse_button(action, button)

    def keyboard(self, key_code: int, action: int, modifiers: int) -> int:
        with self._lock:
            return self._require().keyboard(key_code, action, modifiers)

    def scroll(self, clicks: int) -> int:
        with self._lock:
            return self._require().scroll(clicks)


def health_payload(health: WorkerHealth) -> dict[str, object]:
    """Return the complete allowlisted metadata-only public health schema."""

    return {
        "state": health.state.value,
        "frames_observed": health.frames_observed,
        "actions_sent": health.actions_sent,
        "reconnects": health.reconnects,
        "last_error_type": health.last_error_type,
        "last_error_code": health.last_error_code,
    }


class AutomationWorker:
    """Observe, perceive, decide, act, and reconnect without host mutation."""

    def __init__(
        self,
        profile: AutomationProfile,
        connection_factory: Callable[[], object],
        *,
        observer_factory: Callable[[object, AutomationProfile], ObservationSource]
        | None = None,
        transport_factory: Callable[[object], InputTransport] | None = None,
        templates: Mapping[str, np.ndarray] | None = None,
        matcher: TemplateMatcher | None = None,
        ocr: OcrAdapter | None = None,
        health_callback: Callable[[WorkerHealth], None] | None = None,
        persistent_perception: PersistentPerceptionRuntime | None = None,
        persistent_perception_factory: Callable[
            [SafeInputDriver], PersistentPerceptionRuntime
        ]
        | None = None,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self._profile = profile
        self._connection_factory = connection_factory
        self._observer_factory = observer_factory or (
            lambda client, current: LatestFrameObserver(client, current)
        )
        self._transport_factory = transport_factory or self._native_transport
        self._perception = PerceptionEngine(
            profile.perception, templates=templates, matcher=matcher, ocr=ocr
        )
        self._transport = ReconnectableInputTransport()
        self._inputs = SafeInputDriver(
            profile.actions, profile.safety, profile.stream, self._transport
        )
        self._engine = (
            WorkflowEngine(profile.workflow, self._inputs)
            if profile.workflow is not None
            else None
        )
        if persistent_perception is not None and persistent_perception_factory is not None:
            raise ValueError("persistent perception must have one construction path")
        self._health_callback = health_callback
        self._persistent_perception = (
            persistent_perception_factory(self._inputs)
            if persistent_perception_factory is not None
            else persistent_perception
        )
        self._clock = clock
        self._stop = Event()
        self._detach_requested = Event()
        self._attach_requested = Event()
        self._wait = wait or self._stop.wait
        self._lock = RLock()
        self._state = WorkerState.STOPPED
        self._frames_observed = 0
        self._reconnects = 0
        self._last_error_type: str | None = None
        self._last_error_code: str | None = None
        self._next_status_at = 0.0

    @staticmethod
    def _native_transport(client: object) -> InputTransport:
        return MoonlightCffiTransport(
            lambda: bool(
                getattr(client, "_session", None)
                and getattr(client._session, "is_connected", False)
            )
        )

    def request_stop(self) -> None:
        """Request graceful cancellation from a signal handler or controller."""

        self._stop.set()

    def request_detach(self) -> None:
        """Drop the stream connection but keep the worker (and IPC) alive.

        An operator decision, not a failure: the current connection is torn
        down through the normal cleanup path (held input released, observer
        stopped) and the worker parks in ``detached`` until reattached or
        stopped. No reconnect budget is consumed.
        """

        self._detach_requested.set()

    def request_attach(self) -> None:
        """Leave the detached state and reconnect the stream."""

        self._attach_requested.set()

    def health(self) -> WorkerHealth:
        """Return an allowlisted snapshot with no target or frame content."""

        with self._lock:
            return WorkerHealth(
                state=self._state,
                frames_observed=self._frames_observed,
                actions_sent=self._inputs.protocol_events_sent,
                reconnects=self._reconnects,
                last_error_type=self._last_error_type,
                last_error_code=self._last_error_code,
            )

    def _set_state(self, state: WorkerState, *, force_status: bool = True) -> None:
        with self._lock:
            self._state = state
        self._emit_status(force=force_status)

    def _emit_status(self, *, force: bool = False) -> None:
        if self._health_callback is None:
            return
        now = self._clock()
        if not force and now < self._next_status_at:
            return
        self._next_status_at = now + self._profile.runtime.status_interval_seconds
        try:
            self._health_callback(self.health())
        except Exception:
            return

    def _record_error(self, error: BaseException) -> None:
        with self._lock:
            self._last_error_type = type(error).__name__
            self._last_error_code = (
                error.code if isinstance(error, ConnectFailure) else None
            )
        if os.environ.get("MOONLIGHT_DEBUG_TRACE"):
            # Local debugging only: tracebacks carry no frames, addresses, or
            # identity material, and this stays off unless explicitly set.
            import traceback

            traceback.print_exception(error, file=sys.stderr)

    def _cleanup_attempt(self, observer: ObservationSource | None) -> BaseException | None:
        cleanup_error: BaseException | None = None
        try:
            if self._transport.is_connected:
                self._inputs.release_all()
        except BaseException as error:
            cleanup_error = error
        finally:
            self._transport.detach()
        if observer is not None:
            try:
                observer.stop()
            except BaseException as error:
                cleanup_error = cleanup_error or error
        return cleanup_error

    def _run_connection(self) -> RunOutcome | None:
        observer: ObservationSource | None = None
        attempt_error: BaseException | None = None
        outcome: RunOutcome | None = None
        try:
            client = self._connection_factory()
            observer = self._observer_factory(client, self._profile)
            observer.start()
            self._transport.attach(self._transport_factory(client))
            if self._inputs.held_input_count:
                self._inputs.release_all()
            if self._persistent_perception is not None:
                self._persistent_perception.reset()
            self._set_state(WorkerState.OBSERVING)
            last_frame_at = self._clock()
            while not self._stop.is_set() and not self._detach_requested.is_set():
                observation = observer.observe(timeout=1.0)
                now = self._clock()
                if observation is None:
                    if now - last_frame_at >= self._profile.runtime.liveness_timeout_seconds:
                        raise TimeoutError("observation liveness deadline exceeded")
                    self._emit_status()
                    continue
                last_frame_at = now
                with self._lock:
                    self._frames_observed += 1
                if self._persistent_perception is not None:
                    self._persistent_perception.advance(observation)
                elif self._engine is not None:
                    signals = self._perception.evaluate(observation.data).signals
                    self._set_state(WorkerState.ACTING, force_status=False)
                    snapshot = self._engine.tick(signals)
                    self._set_state(WorkerState.OBSERVING, force_status=False)
                    if snapshot.outcome is not None:
                        outcome = snapshot.outcome
                        break
                self._emit_status()
        except BaseException as error:
            attempt_error = error
        if self._persistent_perception is not None:
            try:
                self._persistent_perception.reset()
            except BaseException as error:
                attempt_error = attempt_error or error
        cleanup_error = self._cleanup_attempt(observer)
        if attempt_error is None:
            attempt_error = cleanup_error
        if attempt_error is not None:
            raise attempt_error
        return outcome

    def run(self) -> RunOutcome:
        """Run until workflow completion, cancellation, or retry exhaustion."""

        self._set_state(WorkerState.STARTING)
        consecutive_failures = 0
        while not self._stop.is_set():
            if self._detach_requested.is_set():
                # Operator detach: park with no connection until reattach or
                # stop. Like environmental waiting, this consumes no budget.
                consecutive_failures = 0
                self._attach_requested.clear()
                self._set_state(WorkerState.DETACHED)
                while not self._stop.is_set() and not self._attach_requested.wait(0.5):
                    self._emit_status()
                self._detach_requested.clear()
                if self._stop.is_set():
                    break
                self._set_state(WorkerState.STARTING)
            frames_before_attempt = self.health().frames_observed
            try:
                outcome = self._run_connection()
            except Exception as error:
                self._record_error(error)
                if isinstance(error, ConnectFailure) and error.environmental:
                    # The world is not ready (host asleep, no Desktop
                    # session). This is not a worker failure: wait patiently
                    # without consuming the reconnect budget, and connect as
                    # soon as the environment comes back.
                    consecutive_failures = 0
                    self._set_state(WorkerState.WAITING)
                    if self._wait(self._profile.runtime.environment_poll_seconds):
                        break
                    self._set_state(WorkerState.STARTING)
                    continue
                with self._lock:
                    self._reconnects += 1
                if self.health().frames_observed > frames_before_attempt:
                    consecutive_failures = 0
                consecutive_failures += 1
                if consecutive_failures > self._profile.runtime.max_reconnect_attempts:
                    self._set_state(WorkerState.FAILED)
                    return RunOutcome.FAILURE
                self._set_state(WorkerState.RECOVERING)
                delay = min(
                    self._profile.runtime.reconnect_max_seconds,
                    self._profile.runtime.reconnect_initial_seconds
                    * (2 ** (consecutive_failures - 1)),
                )
                if self._wait(delay):
                    break
                self._set_state(WorkerState.STARTING)
                continue
            if outcome is not None:
                if outcome is RunOutcome.FAILURE:
                    self._set_state(WorkerState.FAILED)
                else:
                    self._set_state(WorkerState.STOPPED)
                return outcome
            if not self._stop.is_set():
                if self._detach_requested.is_set():
                    # The connection ended because a detach was requested;
                    # the loop top parks the worker. Not an error.
                    continue
                self._record_error(RuntimeError("connection ended without outcome"))
                continue

        if self._engine is not None:
            self._engine.cancel()
        try:
            if self._transport.is_connected:
                self._inputs.release_all()
        finally:
            self._transport.detach()
        self._set_state(WorkerState.STOPPED)
        return RunOutcome.CANCELLED
