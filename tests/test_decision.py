"""Deterministic tests for declarative workflow decisions."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from streambot.config import AutomationProfile, ConfigurationError, load_profile
from streambot.decision import WorkflowEngine
from streambot.models import RunOutcome


def workflow_value() -> dict[str, object]:
    return json.loads(Path("profiles/workflow-example.json").read_text(encoding="utf-8"))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeExecutor:
    def __init__(self, failures: dict[str, int] | None = None) -> None:
        self.failures = dict(failures or {})
        self.attempts: list[tuple[str, str]] = []
        self.executed: list[tuple[str, str]] = []
        self.completed: set[str] = set()

    def execute(self, action: str, idempotency_key: str) -> None:
        if idempotency_key in self.completed:
            return
        self.attempts.append((action, idempotency_key))
        remaining = self.failures.get(action, 0)
        if remaining:
            self.failures[action] = remaining - 1
            raise RuntimeError("synthetic action failure")
        self.completed.add(idempotency_key)
        self.executed.append((action, idempotency_key))


class WorkflowEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        profile = load_profile("profiles/workflow-example.json")
        self.assertIsNotNone(profile.workflow)
        self.workflow = profile.workflow
        self.clock = FakeClock()

    def test_successful_guarded_transition_executes_once(self) -> None:
        executor = FakeExecutor()
        engine = WorkflowEngine(self.workflow, executor, clock=self.clock)

        waiting = engine.tick({"ready": False})
        completed = engine.tick({"ready": True})
        repeated = engine.tick({"ready": True})

        self.assertEqual(waiting.state, "waiting")
        self.assertEqual(completed.outcome, RunOutcome.SUCCESS)
        self.assertEqual(repeated.outcome, RunOutcome.SUCCESS)
        self.assertEqual(executor.executed, [("activate", "activate-ready:0")])
        self.assertEqual(engine.events()[-1].event_type, "transition")

    def test_timeout_enters_failure_terminal(self) -> None:
        engine = WorkflowEngine(self.workflow, FakeExecutor(), clock=self.clock)

        self.clock.advance(30.0)
        snapshot = engine.tick({})

        self.assertEqual(snapshot.state, "failed")
        self.assertEqual(snapshot.outcome, RunOutcome.FAILURE)
        self.assertEqual(engine.events()[-1].event_type, "timeout")

    def test_retry_exhaustion_is_bounded(self) -> None:
        executor = FakeExecutor({"activate": 3})
        engine = WorkflowEngine(self.workflow, executor, clock=self.clock)

        first = engine.tick({"ready": True})
        second = engine.tick({"ready": True})
        third = engine.tick({"ready": True})

        self.assertIsNone(first.outcome)
        self.assertIsNone(second.outcome)
        self.assertEqual(third.outcome, RunOutcome.FAILURE)
        self.assertEqual(
            [event.event_type for event in engine.events()],
            ["retry", "retry", "retry_exhausted"],
        )
        self.assertEqual(engine.events()[-1].retry_count, 3)
        self.assertEqual(engine.events()[-1].error_type, "RuntimeError")

    def test_partial_failure_uses_per_action_idempotency(self) -> None:
        value = workflow_value()
        value["actions"].insert(
            0,
            {
                "name": "prepare",
                "type": "mouse_move",
                "dx": 1,
                "dy": 0,
            },
        )
        transition = value["workflow"]["states"][0]["transitions"][0]
        transition["actions"] = ["prepare", "activate"]
        profile = AutomationProfile.from_mapping(value)
        executor = FakeExecutor({"activate": 1})
        engine = WorkflowEngine(profile.workflow, executor, clock=self.clock)

        engine.tick({"ready": True})
        completed = engine.tick({"ready": True})

        self.assertEqual(completed.outcome, RunOutcome.SUCCESS)
        self.assertEqual(
            executor.executed,
            [
                ("prepare", "activate-ready:0"),
                ("activate", "activate-ready:1"),
            ],
        )
        self.assertEqual(
            executor.attempts.count(("prepare", "activate-ready:0")), 1
        )
        self.assertEqual(
            executor.attempts.count(("activate", "activate-ready:1")), 2
        )

    def test_completed_transition_is_suppressed_when_state_loops(self) -> None:
        value = workflow_value()
        transition = value["workflow"]["states"][0]["transitions"][0]
        transition["target"] = "waiting"
        profile = AutomationProfile.from_mapping(value)
        executor = FakeExecutor()
        engine = WorkflowEngine(profile.workflow, executor, clock=self.clock)

        engine.tick({"ready": True})
        engine.tick({"ready": True})

        self.assertEqual(len(executor.executed), 1)
        self.assertEqual(engine.events()[-1].event_type, "duplicate_suppressed")
        self.assertEqual(engine.snapshot().completed_idempotency_keys, 1)

    def test_duplicate_self_transition_does_not_refresh_timeout(self) -> None:
        value = workflow_value()
        transition = value["workflow"]["states"][0]["transitions"][0]
        transition["target"] = "waiting"
        profile = AutomationProfile.from_mapping(value)
        engine = WorkflowEngine(profile.workflow, FakeExecutor(), clock=self.clock)

        engine.tick({"ready": True})
        self.clock.advance(10.0)
        engine.tick({"ready": True})
        self.clock.advance(20.0)
        timed_out = engine.tick({"ready": True})

        self.assertEqual(timed_out.outcome, RunOutcome.FAILURE)
        self.assertEqual(engine.events()[-1].event_type, "timeout")

    def test_missing_false_signal_does_not_trigger_transition(self) -> None:
        value = workflow_value()
        value["workflow"]["states"][0]["transitions"][0]["equals"] = False
        profile = AutomationProfile.from_mapping(value)
        executor = FakeExecutor()
        engine = WorkflowEngine(profile.workflow, executor, clock=self.clock)

        snapshot = engine.tick({})

        self.assertEqual(snapshot.state, "waiting")
        self.assertEqual(executor.executed, [])

    def test_cancellation_is_terminal_and_sends_no_action(self) -> None:
        executor = FakeExecutor()
        engine = WorkflowEngine(self.workflow, executor, clock=self.clock)

        cancelled = engine.cancel()
        after_tick = engine.tick({"ready": True})

        self.assertEqual(cancelled.outcome, RunOutcome.CANCELLED)
        self.assertEqual(after_tick.outcome, RunOutcome.CANCELLED)
        self.assertEqual(executor.executed, [])
        self.assertEqual(engine.events()[-1].event_type, "cancelled")

    def test_event_history_is_bounded_but_sequence_is_monotonic(self) -> None:
        value = workflow_value()
        value["workflow"]["event_history_limit"] = 10
        value["workflow"]["states"][0]["transitions"][0]["target"] = "waiting"
        profile = AutomationProfile.from_mapping(value)
        engine = WorkflowEngine(profile.workflow, FakeExecutor(), clock=self.clock)

        for _ in range(25):
            engine.tick({"ready": True})

        self.assertEqual(len(engine.events()), 10)
        self.assertEqual(engine.snapshot().event_count, 25)
        self.assertEqual(engine.events()[0].sequence, 16)
        self.assertEqual(engine.events()[-1].sequence, 25)

    def test_action_workflow_requires_an_executor(self) -> None:
        with self.assertRaisesRegex(ValueError, "require an executor"):
            WorkflowEngine(self.workflow, clock=self.clock)

    def test_signal_values_must_be_boolean(self) -> None:
        engine = WorkflowEngine(self.workflow, FakeExecutor(), clock=self.clock)

        with self.assertRaisesRegex(ValueError, "booleans"):
            engine.tick({"ready": 1})


class WorkflowConfigurationTests(unittest.TestCase):
    def test_example_profile_loads(self) -> None:
        profile = load_profile("profiles/workflow-example.json")

        self.assertEqual(profile.workflow.initial_state, "waiting")
        self.assertEqual(len(profile.workflow.states), 3)

    def test_unknown_signal_is_rejected(self) -> None:
        value = workflow_value()
        value["workflow"]["states"][0]["transitions"][0]["signal"] = "missing"

        with self.assertRaisesRegex(ConfigurationError, "unknown signal"):
            AutomationProfile.from_mapping(value)

    def test_non_terminal_state_requires_timeout(self) -> None:
        value = workflow_value()
        state = value["workflow"]["states"][0]
        del state["timeout_seconds"]
        del state["timeout_state"]

        with self.assertRaisesRegex(ConfigurationError, "requires a timeout"):
            AutomationProfile.from_mapping(value)

    def test_action_transition_requires_idempotency_and_failure_state(self) -> None:
        value = workflow_value()
        transition = value["workflow"]["states"][0]["transitions"][0]
        del transition["idempotency_key"]

        with self.assertRaisesRegex(ConfigurationError, "idempotency_key"):
            AutomationProfile.from_mapping(value)

    def test_duplicate_idempotency_keys_are_rejected(self) -> None:
        value = workflow_value()
        duplicate = copy.deepcopy(value["workflow"]["states"][0]["transitions"][0])
        duplicate["name"] = "duplicate"
        duplicate["equals"] = False
        value["workflow"]["states"][0]["transitions"].append(duplicate)

        with self.assertRaisesRegex(ConfigurationError, "duplicate idempotency"):
            AutomationProfile.from_mapping(value)

    def test_ambiguous_guards_are_rejected(self) -> None:
        value = workflow_value()
        duplicate = copy.deepcopy(value["workflow"]["states"][0]["transitions"][0])
        duplicate["name"] = "duplicate"
        duplicate["idempotency_key"] = "different-key"
        value["workflow"]["states"][0]["transitions"].append(duplicate)

        with self.assertRaisesRegex(ConfigurationError, "ambiguous guards"):
            AutomationProfile.from_mapping(value)

    def test_every_state_must_reach_a_terminal(self) -> None:
        value = workflow_value()
        value["workflow"]["states"].append(
            {
                "name": "isolated",
                "timeout_seconds": 1,
                "timeout_state": "isolated",
            }
        )

        with self.assertRaisesRegex(ConfigurationError, "cannot reach a terminal"):
            AutomationProfile.from_mapping(value)

    def test_success_and_failure_terminals_are_required(self) -> None:
        value = workflow_value()
        value["workflow"]["states"][-1]["terminal"] = "success"

        with self.assertRaisesRegex(ConfigurationError, "success and failure"):
            AutomationProfile.from_mapping(value)


if __name__ == "__main__":
    unittest.main()
