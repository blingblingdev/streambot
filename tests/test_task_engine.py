"""Deterministic scenario tests for the declarative task engine."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from streambot.models import RunOutcome
from streambot.scene import ControlFact, SceneFacts
from streambot.task_engine import (
    TaskDefinitionError,
    TaskEngine,
    TaskState,
    load_task_definition,
    validate_task_semantics,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeDispatcher:
    def __init__(self, *, fail_clicks: bool = False) -> None:
        self.calls: list[tuple] = []
        self.released = 0
        self.fail_clicks = fail_clicks

    def click(self, x: int, y: int, idempotency_key: str) -> None:
        if self.fail_clicks:
            raise RuntimeError("transport rejected")
        self.calls.append(("click", x, y, idempotency_key))

    def key(self, key: str, hold_seconds: float, idempotency_key: str) -> None:
        self.calls.append(("key", key, hold_seconds, idempotency_key))

    def drag(self, start, end, duration_seconds, idempotency_key) -> None:
        self.calls.append(("drag", start, end, duration_seconds, idempotency_key))

    def scroll(self, clicks: int, idempotency_key: str) -> None:
        self.calls.append(("scroll", clicks, idempotency_key))

    def type_text(self, text: str, idempotency_key: str) -> None:
        self.calls.append(("type", text, idempotency_key))

    def release_all(self) -> None:
        self.released += 1


def _facts(
    scene_id: str | None,
    *,
    stability: int = 2,
    controls: tuple[ControlFact, ...] = (),
    recommended: str | None = None,
    frame_number: int = 1,
) -> SceneFacts:
    return SceneFacts(
        frame_number=frame_number,
        scene_id=scene_id,
        controls=controls,
        recommended_id=recommended,
        stability=stability,
    )


def _click_step(scene: str, control: str, next_scene: str, on_success: str) -> dict:
    return {
        "when": {"scene": scene, "min_stability": 2},
        "wait_timeout_seconds": 30,
        "action": {"kind": "dispatch", "control": control},
        "verify": {"scene": next_scene, "timeout_seconds": 3},
        "on_timeout": "@failure",
        "on_success": on_success,
    }


def _definition(steps: dict, *, entry: str, **extra) -> dict:
    definition = {
        "schema_version": 1,
        "task": "fixture",
        "entry": entry,
        "steps": steps,
    }
    definition.update(extra)
    return definition


BUTTON = ControlFact("ok", "click", 100, 200)


class ValidationTests(unittest.TestCase):
    def test_load_valid_definition_from_file(self) -> None:
        definition = _definition(
            {"start": _click_step("menu", "ok", "playing", "@success")}, entry="start"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.json"
            path.write_text(json.dumps(definition), encoding="utf-8")
            loaded = load_task_definition(path)
        self.assertEqual(loaded["task"], "fixture")

    def test_rejects_undefined_entry(self) -> None:
        definition = _definition(
            {"start": _click_step("menu", "ok", "playing", "@success")}, entry="ghost"
        )
        with self.assertRaises(TaskDefinitionError):
            validate_task_semantics(definition)

    def test_rejects_undefined_target(self) -> None:
        definition = _definition(
            {"start": _click_step("menu", "ok", "playing", "missing-step")},
            entry="start",
        )
        with self.assertRaises(TaskDefinitionError):
            validate_task_semantics(definition)

    def test_rejects_steps_that_cannot_reach_terminal(self) -> None:
        def _loop_step(next_step: str) -> dict:
            return {
                "when": {"always": True},
                "action": {"kind": "none"},
                "verify": {"immediate": True, "timeout_seconds": 1},
                "on_success": next_step,
            }

        definition = _definition(
            {"loop-a": _loop_step("loop-b"), "loop-b": _loop_step("loop-a")},
            entry="loop-a",
        )
        with self.assertRaises(TaskDefinitionError):
            validate_task_semantics(definition)

    def test_rejects_scene_verification_without_on_timeout(self) -> None:
        step = _click_step("menu", "ok", "playing", "@success")
        del step["on_timeout"]
        definition = _definition({"start": step}, entry="start")
        with self.assertRaises(TaskDefinitionError):
            validate_task_semantics(definition)

    def test_rejects_unknown_scene_references_when_scene_ids_given(self) -> None:
        definition = _definition(
            {"start": _click_step("menu", "ok", "playing", "@success")}, entry="start"
        )
        with self.assertRaises(TaskDefinitionError):
            validate_task_semantics(definition, scene_ids={"menu"})

    def test_rejects_interrupt_to_undefined_step(self) -> None:
        definition = _definition(
            {"start": _click_step("menu", "ok", "playing", "@success")},
            entry="start",
            interrupts={"popup": "ghost"},
        )
        with self.assertRaises(TaskDefinitionError):
            validate_task_semantics(definition)

    def test_rejects_missing_wait_deadline_for_conditional_when(self) -> None:
        step = _click_step("menu", "ok", "playing", "@success")
        del step["wait_timeout_seconds"]
        definition = _definition({"start": step}, entry="start")
        with self.assertRaises(TaskDefinitionError):
            validate_task_semantics(definition)

    def test_rejects_min_stability_without_scene(self) -> None:
        definition = _definition(
            {
                "start": {
                    "when": {"min_stability": 5},
                    "action": {"kind": "none"},
                    "verify": {"immediate": True, "timeout_seconds": 1},
                    "on_success": "@success",
                }
            },
            entry="start",
        )
        with self.assertRaises(TaskDefinitionError):
            validate_task_semantics(definition)

    def test_rejects_variable_action_without_immediate_verify(self) -> None:
        definition = _definition(
            {
                "start": {
                    "when": {"always": True},
                    "action": {"kind": "set-var", "name": "n", "value": 1},
                    "verify": {"scene": "menu", "timeout_seconds": 1},
                    "on_timeout": "@failure",
                    "on_success": "@success",
                }
            },
            entry="start",
        )
        with self.assertRaises(TaskDefinitionError):
            validate_task_semantics(definition)


class ExecutionTests(unittest.TestCase):
    def _engine(self, definition, dispatcher=None, clock=None) -> TaskEngine:
        return TaskEngine(
            definition,
            dispatcher or FakeDispatcher(),
            clock=clock or FakeClock(),
        )

    def test_click_and_verify_success(self) -> None:
        dispatcher = FakeDispatcher()
        engine = self._engine(
            _definition(
                {"start": _click_step("menu", "ok", "playing", "@success")},
                entry="start",
            ),
            dispatcher,
        )
        menu = _facts("menu", controls=(BUTTON,))
        engine.tick(menu)
        self.assertEqual(dispatcher.calls, [("click", 100, 200, "fixture:start:0")])
        snapshot = engine.tick(_facts("playing"))
        self.assertEqual(snapshot.outcome, RunOutcome.SUCCESS)
        self.assertEqual(snapshot.actions_dispatched, 1)

    def test_waits_for_stability_before_acting(self) -> None:
        dispatcher = FakeDispatcher()
        engine = self._engine(
            _definition(
                {"start": _click_step("menu", "ok", "playing", "@success")},
                entry="start",
            ),
            dispatcher,
        )
        engine.tick(_facts("menu", stability=1, controls=(BUTTON,)))
        self.assertEqual(dispatcher.calls, [])
        engine.tick(_facts("menu", stability=2, controls=(BUTTON,)))
        self.assertEqual(len(dispatcher.calls), 1)

    def test_verification_timeout_routes_to_on_timeout(self) -> None:
        clock = FakeClock()
        engine = self._engine(
            _definition(
                {"start": _click_step("menu", "ok", "playing", "@success")},
                entry="start",
            ),
            clock=clock,
        )
        engine.tick(_facts("menu", controls=(BUTTON,)))
        clock.advance(3.5)
        snapshot = engine.tick(_facts("menu", controls=(BUTTON,)))
        self.assertEqual(snapshot.outcome, RunOutcome.FAILURE)

    def test_bounded_retry_then_success(self) -> None:
        clock = FakeClock()
        dispatcher = FakeDispatcher()
        step = _click_step("menu", "ok", "playing", "@success")
        step["max_retries"] = 1
        step["retry_after_seconds"] = 1
        engine = self._engine(
            _definition({"start": step}, entry="start"), dispatcher, clock
        )
        menu = _facts("menu", controls=(BUTTON,))
        engine.tick(menu)
        clock.advance(1.2)
        engine.tick(menu)  # verify fails, moves to retry
        engine.tick(menu)  # re-dispatch
        self.assertEqual(
            [call[3] for call in dispatcher.calls],
            ["fixture:start:0", "fixture:start:1:retry1"],
        )
        snapshot = engine.tick(_facts("playing"))
        self.assertEqual(snapshot.outcome, RunOutcome.SUCCESS)

    def test_retry_restarts_wait_deadline(self) -> None:
        clock = FakeClock()
        dispatcher = FakeDispatcher()
        step = _click_step("menu", "ok", "playing", "@success")
        step["wait_timeout_seconds"] = 2
        step["max_retries"] = 1
        step["retry_after_seconds"] = 1
        engine = self._engine(
            _definition({"start": step}, entry="start"), dispatcher, clock
        )
        menu = _facts("menu", controls=(BUTTON,))
        engine.tick(menu)  # dispatch at t=0
        clock.advance(2.5)  # beyond the original wait deadline
        engine.tick(menu)  # verify fails -> retry granted, wait deadline restarts
        snapshot = engine.tick(menu)  # must re-dispatch, not wait_timeout
        self.assertIsNone(snapshot.outcome)
        self.assertEqual(len(dispatcher.calls), 2)

    def test_retry_exhaustion_fails_via_timeout_target(self) -> None:
        clock = FakeClock()
        dispatcher = FakeDispatcher()
        step = _click_step("menu", "ok", "playing", "@success")
        step["max_retries"] = 1
        step["retry_after_seconds"] = 1
        engine = self._engine(
            _definition({"start": step}, entry="start"), dispatcher, clock
        )
        menu = _facts("menu", controls=(BUTTON,))
        engine.tick(menu)
        clock.advance(1.2)
        engine.tick(menu)
        engine.tick(menu)
        clock.advance(3.2)
        snapshot = engine.tick(menu)
        self.assertEqual(snapshot.outcome, RunOutcome.FAILURE)
        self.assertEqual(len(dispatcher.calls), 2)

    def test_dispatcher_error_fails_closed_and_releases(self) -> None:
        dispatcher = FakeDispatcher(fail_clicks=True)
        engine = self._engine(
            _definition(
                {"start": _click_step("menu", "ok", "playing", "@success")},
                entry="start",
            ),
            dispatcher,
        )
        snapshot = engine.tick(_facts("menu", controls=(BUTTON,)))
        self.assertEqual(snapshot.outcome, RunOutcome.FAILURE)
        self.assertEqual(dispatcher.released, 1)

    def test_missing_dispatch_control_fails_closed(self) -> None:
        dispatcher = FakeDispatcher()
        engine = self._engine(
            _definition(
                {"start": _click_step("menu", "absent", "playing", "@success")},
                entry="start",
            ),
            dispatcher,
        )
        snapshot = engine.tick(_facts("menu", controls=(BUTTON,)))
        self.assertEqual(snapshot.outcome, RunOutcome.FAILURE)
        self.assertEqual(dispatcher.released, 1)

    def test_dispatch_by_control_text(self) -> None:
        dispatcher = FakeDispatcher()
        step = {
            "when": {"scene": "choices", "min_stability": 1},
            "wait_timeout_seconds": 30,
            "action": {"kind": "dispatch", "control_text": "option b"},
            "verify": {"scene_not": "choices", "timeout_seconds": 3},
            "on_timeout": "@failure",
            "on_success": "@success",
        }
        engine = self._engine(_definition({"start": step}, entry="start"), dispatcher)
        controls = (
            ControlFact("opt-0", "click", 10, 10, text="Option A"),
            ControlFact("opt-1", "click", 10, 60, text="Option B"),
        )
        engine.tick(_facts("choices", controls=controls))
        self.assertEqual(dispatcher.calls[0][:3], ("click", 10, 60))

    def test_dispatch_recommended(self) -> None:
        dispatcher = FakeDispatcher()
        step = {
            "when": {"scene": "choices", "min_stability": 1},
            "wait_timeout_seconds": 30,
            "action": {"kind": "dispatch", "recommended": True},
            "verify": {"scene_not": "choices", "timeout_seconds": 3},
            "on_timeout": "@failure",
            "on_success": "@success",
        }
        engine = self._engine(_definition({"start": step}, entry="start"), dispatcher)
        controls = (ControlFact("a", "click", 1, 1), ControlFact("b", "click", 2, 2))
        engine.tick(_facts("choices", controls=controls, recommended="b"))
        self.assertEqual(dispatcher.calls[0][:3], ("click", 2, 2))

    def test_variable_counter_loop_with_branching(self) -> None:
        dispatcher = FakeDispatcher()
        definition = _definition(
            {
                "act": {
                    "when": {"scene": "round", "var_less_than": {"done": 3}},
                    "wait_timeout_seconds": 30,
                    "action": {"kind": "dispatch", "control": "ok"},
                    "verify": {"scene": "cleared", "timeout_seconds": 3},
                    "on_timeout": "@failure",
                    "on_success": "count",
                },
                "count": {
                    "when": {"always": True},
                    "action": {"kind": "add-var", "name": "done", "value": 1},
                    "verify": {"immediate": True, "timeout_seconds": 1},
                    "on_success": "route",
                },
                "route": {
                    "when": {"always": True},
                    "action": {"kind": "none"},
                    "verify": {"immediate": True, "timeout_seconds": 1},
                    "on_success": "check",
                },
                "check": {
                    "when": {"scene": "round", "var_equals": {"done": 3}},
                    "wait_timeout_seconds": 0.1,
                    "action": {"kind": "none"},
                    "verify": {"immediate": True, "timeout_seconds": 1},
                    "on_timeout": "act",
                    "on_success": "@success",
                },
            },
            entry="act",
            variables={"done": 0},
        )
        clock = FakeClock()
        engine = TaskEngine(definition, dispatcher, clock=clock)
        round_facts = _facts("round", controls=(ControlFact("ok", "click", 5, 5),))
        cleared = _facts("cleared")
        for _ in range(3):
            engine.tick(round_facts)  # dispatch
            engine.tick(cleared)  # verified -> count
            engine.tick(cleared)  # count action (immediate)
            engine.tick(cleared)  # count verified -> route
            engine.tick(cleared)  # route action
            engine.tick(cleared)  # route verified -> check
            clock.advance(0.2)
            engine.tick(round_facts)  # check: when fails (done<3) -> timeout -> act
        snapshot = engine.tick(round_facts)
        self.assertEqual(engine.variables["done"], 3)
        self.assertEqual(snapshot.outcome, RunOutcome.SUCCESS)
        self.assertEqual(len([c for c in dispatcher.calls if c[0] == "click"]), 3)

    def test_interrupt_routes_to_handler_step(self) -> None:
        dispatcher = FakeDispatcher()
        definition = _definition(
            {
                "start": _click_step("menu", "ok", "playing", "@success"),
                "close-popup": {
                    "when": {"scene": "popup", "min_stability": 1},
                    "wait_timeout_seconds": 30,
                    "action": {"kind": "dispatch", "control": "close"},
                    "verify": {"scene_not": "popup", "timeout_seconds": 3},
                    "on_timeout": "@failure",
                    "on_success": "start",
                },
            },
            entry="start",
            interrupts={"popup": "close-popup"},
        )
        engine = self._engine(definition, dispatcher)
        popup = _facts(
            "popup", stability=1, controls=(ControlFact("close", "click", 9, 9),)
        )
        engine.tick(popup)
        self.assertEqual(dispatcher.calls[0][:3], ("click", 9, 9))
        engine.tick(_facts("menu", controls=(BUTTON,)))  # popup gone -> back at start
        engine.tick(_facts("menu", controls=(BUTTON,)))
        self.assertEqual(dispatcher.calls[1][:3], ("click", 100, 200))

    def test_wait_timeout_routes_when_condition_never_matches(self) -> None:
        clock = FakeClock()
        step = _click_step("menu", "ok", "playing", "@success")
        step["wait_timeout_seconds"] = 2
        step["on_timeout"] = "@failure"
        engine = self._engine(_definition({"start": step}, entry="start"), clock=clock)
        engine.tick(_facts("other"))
        clock.advance(2.5)
        snapshot = engine.tick(_facts("other"))
        self.assertEqual(snapshot.outcome, RunOutcome.FAILURE)

    def test_cancel_releases_input_and_is_terminal(self) -> None:
        dispatcher = FakeDispatcher()
        engine = self._engine(
            _definition(
                {"start": _click_step("menu", "ok", "playing", "@success")},
                entry="start",
            ),
            dispatcher,
        )
        snapshot = engine.cancel()
        self.assertEqual(snapshot.outcome, RunOutcome.CANCELLED)
        self.assertEqual(dispatcher.released, 1)
        after = engine.tick(_facts("menu", controls=(BUTTON,)))
        self.assertEqual(after.outcome, RunOutcome.CANCELLED)
        self.assertEqual(dispatcher.calls, [])

    def test_key_drag_scroll_type_actions(self) -> None:
        dispatcher = FakeDispatcher()
        steps = {
            "press": {
                "when": {"always": True},
                "action": {"kind": "key", "key": "q", "hold_seconds": 0.2},
                "verify": {"immediate": True, "timeout_seconds": 1},
                "on_success": "drag",
            },
            "drag": {
                "when": {"always": True},
                "action": {
                    "kind": "drag",
                    "from_norm": [0.5, 0.5],
                    "to": [100, 100],
                    "duration_seconds": 0.4,
                },
                "verify": {"immediate": True, "timeout_seconds": 1},
                "on_success": "scroll",
            },
            "scroll": {
                "when": {"always": True},
                "action": {"kind": "scroll", "clicks": -3},
                "verify": {"immediate": True, "timeout_seconds": 1},
                "on_success": "type",
            },
            "type": {
                "when": {"always": True},
                "action": {"kind": "type-text", "text": "hello"},
                "verify": {"immediate": True, "timeout_seconds": 1},
                "on_success": "@success",
            },
        }
        engine = self._engine(_definition(steps, entry="press"), dispatcher)
        facts = _facts(None, stability=0)
        for _ in range(8):
            engine.tick(facts)
        kinds = [call[0] for call in dispatcher.calls]
        self.assertEqual(kinds, ["key", "drag", "scroll", "type"])
        drag_call = dispatcher.calls[1]
        self.assertEqual(drag_call[1], (640, 360))
        self.assertEqual(drag_call[2], (100, 100))

    def test_events_are_metadata_only(self) -> None:
        dispatcher = FakeDispatcher()
        engine = self._engine(
            _definition(
                {"start": _click_step("menu", "ok", "playing", "@success")},
                entry="start",
            ),
            dispatcher,
        )
        engine.tick(_facts("menu", controls=(BUTTON,)))
        engine.tick(_facts("playing"))
        payload = json.dumps([event.__dict__ for event in engine.events()])
        self.assertNotIn("100", payload.replace('"sequence": 1', ""))
        for event in engine.events():
            self.assertIsNone(event.error_type)


if __name__ == "__main__":
    unittest.main()
