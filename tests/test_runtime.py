"""Tests for reconnecting worker orchestration and safe health output."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from streambot.config import AutomationProfile, ConfigurationError
from streambot.connection import DesktopSessionInactive, NoHostVisible
from streambot.models import RunOutcome, WorkerHealth, WorkerState
from streambot.observation import Observation
from streambot.runtime import AutomationWorker, health_payload
from streambot.runtime import PersistentPerceptionRuntime
from streambot.coordinator import InteractionCoordinator, InteractionPolicy
from streambot.events import ActionCandidate
from streambot.perception_service import Detection, PerceptionBroker, PerceptionScheduler


class FakeTransport:
    is_connected = True

    def mouse_move(self, dx: int, dy: int) -> int:
        return 0

    def mouse_position(self, x: int, y: int, width: int, height: int) -> int:
        return 0

    def mouse_button(self, action: int, button: int) -> int:
        return 0

    def keyboard(self, key_code: int, action: int, modifiers: int) -> int:
        return 0

    def scroll(self, clicks: int) -> int:
        return 0


class FakeObserver:
    def __init__(
        self,
        frames: list[Observation | None | BaseException],
        *,
        start_error: BaseException | None = None,
        on_observe=None,
    ) -> None:
        self.frames = list(frames)
        self.start_error = start_error
        self.on_observe = on_observe
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1
        if self.start_error is not None:
            raise self.start_error

    def observe(self, timeout: float = 1.0) -> Observation | None:
        if self.on_observe is not None:
            self.on_observe()
        if not self.frames:
            return None
        value = self.frames.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def stop(self) -> None:
        self.stopped += 1


def ready_observation() -> Observation:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[:10, :10] = (0, 200, 0)
    return Observation(frame_number=1, observed_at=0.0, data=frame)


class AutomationWorkerTests(unittest.TestCase):
    def test_persistent_perception_keeps_one_connection_across_action_feedback(self) -> None:
        class FakeClock:
            now = 0.2

            def __call__(self):
                return self.now

        class Adapter:
            emitted = False

            def reset(self):
                self.emitted = False

            def detect(self, current):
                if self.emitted or not bool(current.data[0, 0, 0]):
                    return ()
                self.emitted = True
                return (
                    Detection(
                        "action-ready",
                        "choice",
                        "layout",
                        1.0,
                        (ActionCandidate("best", "click", 100, 200),),
                        expiry_seconds=5.0,
                    ),
                )

        class Dispatcher:
            def __init__(self, inputs):
                self.inputs = inputs

            def dispatch(self, candidate, key):
                self.inputs.execute_position(candidate.x, candidate.y, f"{key}-position")
                self.inputs.execute("click", f"{key}-click")

            def release_all(self):
                self.inputs.release_all()

        clock = FakeClock()
        profile = AutomationProfile.from_mapping(
            {
                "name": "persistent",
                "actions": [{"name": "click", "type": "mouse_button", "button": "left"}],
                "safety": {"dry_run": True, "preserve_existing_desktop": True},
            }
        )
        frames = []
        for number, visible in enumerate((True, False, False), start=1):
            data = np.zeros((720, 1280, 3), dtype=np.uint8)
            data[0, 0, 0] = int(visible)
            frames.append(Observation(number, clock.now, data))
        observer = FakeObserver(frames)
        runtimes = []

        def build_persistent(inputs):
            broker = PerceptionBroker(clock=clock)
            scheduler = PerceptionScheduler(Adapter(), broker, clock=clock)
            coordinator = InteractionCoordinator(
                Dispatcher(inputs),
                (InteractionPolicy("choice", ("click",), feedback_timeout_seconds=5.0),),
                stream_width=1280,
                stream_height=720,
                clock=clock,
            )
            runtime = PersistentPerceptionRuntime(
                scheduler, coordinator, lambda current: bool(current.data[0, 0, 0])
            )
            runtimes.append(runtime)
            return runtime

        worker = AutomationWorker(
            profile,
            lambda: object(),
            observer_factory=lambda _client, _profile: observer,
            transport_factory=lambda _client: FakeTransport(),
            persistent_perception_factory=build_persistent,
        )

        def stop_after_frames():
            clock.now += 0.2
            if not observer.frames:
                worker.request_stop()

        observer.on_observe = stop_after_frames
        self.assertEqual(worker.run(), RunOutcome.CANCELLED)
        self.assertEqual(observer.started, 1)
        self.assertEqual(observer.stopped, 1)
        self.assertEqual(worker.health().actions_sent, 0)
        self.assertGreaterEqual(runtimes[0].coordinator.metrics.actions_confirmed, 1)

    def test_reconnects_after_transport_failure_and_preserves_workflow(self) -> None:
        value = json.loads(
            Path("profiles/workflow-example.json").read_text(encoding="utf-8")
        )
        value["runtime"] = {
            "reconnect_initial_seconds": 0.1,
            "reconnect_max_seconds": 1,
            "max_reconnect_attempts": 2,
        }
        value["safety"] = {
            "preserve_existing_desktop": True,
            "dry_run": False,
            "max_actions_per_minute": 30,
        }
        profile = AutomationProfile.from_mapping(value)
        observers = [
            FakeObserver([ConnectionError("synthetic disconnect")]),
            FakeObserver([ready_observation()]),
        ]
        waits: list[float] = []
        statuses: list[WorkerHealth] = []
        worker = AutomationWorker(
            profile,
            lambda: object(),
            observer_factory=lambda _client, _profile: observers.pop(0),
            transport_factory=lambda _client: FakeTransport(),
            health_callback=statuses.append,
            wait=lambda delay: waits.append(delay) or False,
        )

        outcome = worker.run()

        self.assertEqual(outcome, RunOutcome.SUCCESS)
        self.assertEqual(waits, [0.1])
        self.assertEqual(worker.health().reconnects, 1)
        self.assertEqual(worker.health().frames_observed, 1)
        self.assertEqual(worker.health().actions_sent, 2)
        self.assertEqual(worker.health().state, WorkerState.STOPPED)
        self.assertTrue(any(item.state is WorkerState.RECOVERING for item in statuses))

    def test_retry_exhaustion_uses_bounded_exponential_backoff(self) -> None:
        profile = AutomationProfile.from_mapping(
            {
                "name": "fail",
                "runtime": {
                    "reconnect_initial_seconds": 0.5,
                    "reconnect_max_seconds": 0.75,
                    "max_reconnect_attempts": 2,
                },
            }
        )
        waits: list[float] = []
        observers: list[FakeObserver] = []

        def make_observer(_client: object, _profile: AutomationProfile) -> FakeObserver:
            observer = FakeObserver([], start_error=ConnectionError("synthetic"))
            observers.append(observer)
            return observer

        worker = AutomationWorker(
            profile,
            lambda: object(),
            observer_factory=make_observer,
            transport_factory=lambda _client: FakeTransport(),
            wait=lambda delay: waits.append(delay) or False,
        )

        self.assertEqual(worker.run(), RunOutcome.FAILURE)
        self.assertEqual(waits, [0.5, 0.75])
        self.assertEqual(worker.health().reconnects, 3)
        self.assertEqual(worker.health().state, WorkerState.FAILED)
        self.assertEqual(worker.health().last_error_type, "ConnectionError")
        self.assertTrue(all(observer.stopped == 1 for observer in observers))

    def test_healthy_frames_reset_only_the_consecutive_failure_budget(self) -> None:
        profile = AutomationProfile.from_mapping(
            {
                "name": "intermittent",
                "runtime": {
                    "reconnect_initial_seconds": 0.1,
                    "reconnect_max_seconds": 1,
                    "max_reconnect_attempts": 1,
                },
            }
        )
        frame = ready_observation()
        observers = [
            FakeObserver([frame, ConnectionError("first")]),
            FakeObserver([frame, ConnectionError("second")]),
            FakeObserver([]),
        ]
        waits: list[float] = []
        worker = AutomationWorker(
            profile,
            lambda: object(),
            observer_factory=lambda _client, _profile: observers.pop(0),
            transport_factory=lambda _client: FakeTransport(),
            wait=lambda delay: waits.append(delay) or False,
        )
        observers[-1].on_observe = worker.request_stop

        self.assertEqual(worker.run(), RunOutcome.CANCELLED)
        self.assertEqual(waits, [0.1, 0.1])
        self.assertEqual(worker.health().reconnects, 2)
        self.assertEqual(worker.health().frames_observed, 2)

    def test_graceful_stop_cancels_and_cleans_up(self) -> None:
        profile = AutomationProfile.from_mapping({"name": "continuous"})
        observer = FakeObserver([])
        worker = AutomationWorker(
            profile,
            lambda: object(),
            observer_factory=lambda _client, _profile: observer,
            transport_factory=lambda _client: FakeTransport(),
        )
        observer.on_observe = worker.request_stop

        self.assertEqual(worker.run(), RunOutcome.CANCELLED)
        self.assertEqual(worker.health().state, WorkerState.STOPPED)
        self.assertEqual(observer.stopped, 1)

    def test_environmental_wait_never_consumes_the_reconnect_budget(self) -> None:
        profile = AutomationProfile.from_mapping(
            {
                "name": "waiting",
                "runtime": {
                    "max_reconnect_attempts": 1,
                    "environment_poll_seconds": 7.5,
                },
            }
        )
        attempts = {"count": 0}

        def factory() -> object:
            attempts["count"] += 1
            if attempts["count"] <= 3:
                raise DesktopSessionInactive()
            return object()

        observer = FakeObserver([ready_observation()])
        waits: list[float] = []
        statuses: list[WorkerHealth] = []
        worker = AutomationWorker(
            profile,
            factory,
            observer_factory=lambda _client, _profile: observer,
            transport_factory=lambda _client: FakeTransport(),
            health_callback=statuses.append,
            wait=lambda delay: waits.append(delay) or False,
        )
        observer.on_observe = worker.request_stop

        outcome = worker.run()

        # Three environmental waits, then a clean connection: the budget of
        # one reconnect attempt was never touched and the worker never failed.
        self.assertEqual(outcome, RunOutcome.CANCELLED)
        self.assertEqual(waits, [7.5, 7.5, 7.5])
        self.assertEqual(worker.health().reconnects, 0)
        self.assertEqual(worker.health().frames_observed, 1)
        self.assertEqual(
            worker.health().last_error_code, "desktop_session_inactive"
        )
        self.assertTrue(any(item.state is WorkerState.WAITING for item in statuses))
        self.assertFalse(any(item.state is WorkerState.FAILED for item in statuses))

    def test_environmental_wait_stops_promptly_on_request(self) -> None:
        profile = AutomationProfile.from_mapping({"name": "waiting-stop"})

        def factory() -> object:
            raise NoHostVisible()

        worker = AutomationWorker(
            profile,
            factory,
            observer_factory=lambda _client, _profile: FakeObserver([]),
            transport_factory=lambda _client: FakeTransport(),
            wait=lambda _delay: True,  # stop requested during the wait
        )

        self.assertEqual(worker.run(), RunOutcome.CANCELLED)
        self.assertEqual(worker.health().state, WorkerState.STOPPED)
        self.assertEqual(worker.health().last_error_code, "no_host_visible")
        self.assertEqual(worker.health().reconnects, 0)

    def test_liveness_timeout_enters_recovery(self) -> None:
        class FakeClock:
            now = 0.0

            def __call__(self) -> float:
                return self.now

        clock = FakeClock()
        profile = AutomationProfile.from_mapping(
            {
                "name": "liveness",
                "runtime": {
                    "liveness_timeout_seconds": 0.5,
                    "max_reconnect_attempts": 0,
                },
            }
        )
        observer = FakeObserver([], on_observe=lambda: setattr(clock, "now", 1.0))
        worker = AutomationWorker(
            profile,
            lambda: object(),
            observer_factory=lambda _client, _profile: observer,
            transport_factory=lambda _client: FakeTransport(),
            clock=clock,
        )

        self.assertEqual(worker.run(), RunOutcome.FAILURE)
        self.assertEqual(worker.health().last_error_type, "TimeoutError")


class RuntimeSchemaTests(unittest.TestCase):
    def test_runtime_defaults_and_cross_field_validation(self) -> None:
        profile = AutomationProfile.from_mapping({"name": "defaults"})
        self.assertEqual(profile.runtime.max_reconnect_attempts, 5)
        self.assertEqual(profile.runtime.environment_poll_seconds, 10.0)
        with self.assertRaises(ConfigurationError):
            AutomationProfile.from_mapping(
                {
                    "name": "invalid",
                    "runtime": {
                        "reconnect_initial_seconds": 2,
                        "reconnect_max_seconds": 1,
                    },
                }
            )

    def test_health_payload_is_a_complete_allowlist(self) -> None:
        payload = health_payload(
            WorkerHealth(
                state=WorkerState.RECOVERING,
                frames_observed=4,
                actions_sent=2,
                reconnects=1,
                last_error_type="ConnectionError",
                last_error_code="no_host_visible",
            )
        )

        self.assertEqual(
            set(payload),
            {
                "state",
                "frames_observed",
                "actions_sent",
                "reconnects",
                "last_error_type",
                "last_error_code",
            },
        )
        self.assertNotIn("address", json.dumps(payload).casefold())


if __name__ == "__main__":
    unittest.main()
