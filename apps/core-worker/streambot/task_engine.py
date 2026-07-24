"""Declarative task engine binding SceneFacts to verified actions.

`TaskEngine` replaces both the signal-only workflow engine and the ad-hoc
snapshot loop: each step waits for a scene condition over rich `SceneFacts`,
performs at most one action through the injected dispatcher, then verifies an
explicit postcondition — the expected next scene, not merely the disappearance
of the previous one — under a bounded deadline with bounded retries.

Ported semantics:
- from the workflow engine: load-time rigor (terminal reachability, unknown
  reference rejection), stable idempotency keys, bounded retries, cancellation,
  bounded metadata-only event history;
- from the interaction coordinator: single in-flight action lease, stability
  gating before dispatch, fail-closed release on any dispatcher error.

Task definitions are validated by the packaged JSON Schema (`task.schema.json`)
executed through `jsonschema`, plus semantic checks the schema cannot express.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .models import RunOutcome
from .scene import ControlFact, SceneFacts

_SCHEMA_PATH = Path(__file__).with_name("task.schema.json")

SUCCESS = "@success"
FAILURE = "@failure"
_TERMINALS = {SUCCESS, FAILURE}


class TaskDefinitionError(ValueError):
    """Raised when a task definition fails schema or semantic validation."""


class TaskError(RuntimeError):
    """Raised when the engine cannot safely continue."""


class TaskDispatcher(Protocol):
    """Input boundary required by the task engine (implemented over SafeInputDriver)."""

    def click(self, x: int, y: int, idempotency_key: str) -> None: ...

    def key(self, key: str, hold_seconds: float, idempotency_key: str) -> None: ...

    def drag(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_seconds: float,
        idempotency_key: str,
    ) -> None: ...

    def scroll(self, clicks: int, idempotency_key: str) -> None: ...

    def type_text(self, text: str, idempotency_key: str) -> None: ...

    def release_all(self) -> None: ...


class TaskState(StrEnum):
    WAITING = "waiting"
    VERIFYING = "verifying"
    DONE = "done"


@dataclass(frozen=True)
class TaskEvent:
    """Metadata-only engine event (no text, coordinates, or frames)."""

    sequence: int
    event_type: str
    step: str
    retry_count: int = 0
    error_type: str | None = None


@dataclass(frozen=True)
class TaskSnapshot:
    """Current engine state without scene content."""

    step: str
    state: TaskState
    outcome: RunOutcome | None
    event_count: int
    actions_dispatched: int


def load_task_definition(path: str | Path) -> dict[str, Any]:
    """Load a task definition, executing the packaged JSON Schema, failing closed."""

    import jsonschema

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TaskDefinitionError(f"task definition is unreadable: {error}") from error
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda err: list(err.absolute_path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path) or "task"
        raise TaskDefinitionError(f"{location}: {first.message}")
    validate_task_semantics(raw)
    return raw


def validate_task_semantics(
    definition: Mapping[str, Any], scene_ids: set[str] | None = None
) -> None:
    """Cross-reference checks: targets exist, terminals reachable, scenes known."""

    steps: Mapping[str, Any] = definition["steps"]
    if definition["entry"] not in steps:
        raise TaskDefinitionError(f"entry step '{definition['entry']}' is undefined")

    def _check_target(step_id: str, field: str, target: str | None) -> None:
        if target is None:
            return
        if target not in _TERMINALS and target not in steps:
            raise TaskDefinitionError(
                f"step '{step_id}' {field} target '{target}' is undefined"
            )

    referenced_scenes: set[str] = set()
    for step_id, step in steps.items():
        _check_target(step_id, "on_success", step["on_success"])
        _check_target(step_id, "on_timeout", step.get("on_timeout"))
        _check_target(step_id, "on_failure", step.get("on_failure"))
        when = step["when"]
        if not when:
            raise TaskDefinitionError(f"step '{step_id}' when must not be empty")
        if "scene" in when and "scene_any" in when:
            raise TaskDefinitionError(
                f"step '{step_id}' when must not use both scene and scene_any"
            )
        if "min_stability" in when and not ("scene" in when or "scene_any" in when):
            raise TaskDefinitionError(
                f"step '{step_id}' min_stability requires scene or scene_any"
            )
        # Every step whose wait condition can fail needs a bounded deadline,
        # matching the retired workflow engine's per-state deadline rigor.
        if when.get("always") is not True:
            if step.get("wait_timeout_seconds") is None:
                raise TaskDefinitionError(
                    f"step '{step_id}' requires wait_timeout_seconds"
                )
            if step.get("on_timeout") is None:
                raise TaskDefinitionError(
                    f"step '{step_id}' requires on_timeout for its wait deadline"
                )
        referenced_scenes.update(when.get("scene_any", ()))
        if "scene" in when:
            referenced_scenes.add(when["scene"])
        verify = step["verify"]
        scene_keys = [key for key in ("scene", "scene_any", "scene_not") if key in verify]
        if "immediate" in verify and scene_keys:
            raise TaskDefinitionError(
                f"step '{step_id}' verify must not mix immediate with scene checks"
            )
        if "immediate" not in verify and not scene_keys:
            raise TaskDefinitionError(
                f"step '{step_id}' verify requires a scene expectation or immediate"
            )
        if "immediate" not in verify and step.get("on_timeout") is None:
            raise TaskDefinitionError(
                f"step '{step_id}' requires on_timeout for its scene verification"
            )
        if "scene" in verify:
            referenced_scenes.add(verify["scene"])
        referenced_scenes.update(verify.get("scene_any", ()))
        if "scene_not" in verify:
            referenced_scenes.add(verify["scene_not"])
        action = step["action"]
        if action["kind"] in {"set-var", "add-var"}:
            if "immediate" not in verify:
                raise TaskDefinitionError(
                    f"step '{step_id}' variable actions require immediate verification"
                )
            if action["kind"] == "add-var" and not isinstance(action["value"], int):
                raise TaskDefinitionError(
                    f"step '{step_id}' add-var requires an integer value"
                )

    for scene_id, target in definition.get("interrupts", {}).items():
        referenced_scenes.add(scene_id)
        if target not in steps:
            raise TaskDefinitionError(
                f"interrupt for scene '{scene_id}' targets undefined step '{target}'"
            )

    if scene_ids is not None:
        unknown = referenced_scenes - scene_ids
        if unknown:
            raise TaskDefinitionError(
                f"task references unknown scenes: {sorted(unknown)}"
            )

    # Every step must be able to reach a terminal outcome.
    adjacency: dict[str, set[str]] = {}
    for step_id, step in steps.items():
        targets = {
            step["on_success"],
            step.get("on_timeout"),
            step.get("on_failure"),
        }
        adjacency[step_id] = {target for target in targets if target is not None}
    reaches_terminal: set[str] = set()
    changed = True
    while changed:
        changed = False
        for step_id, targets in adjacency.items():
            if step_id in reaches_terminal:
                continue
            if targets & _TERMINALS or targets & reaches_terminal:
                reaches_terminal.add(step_id)
                changed = True
    unreachable = set(steps) - reaches_terminal
    if unreachable:
        raise TaskDefinitionError(
            f"steps cannot reach a terminal outcome: {sorted(unreachable)}"
        )


def _condition_matches(
    when: Mapping[str, Any], facts: SceneFacts, variables: Mapping[str, Any]
) -> bool:
    if "scene" in when and facts.scene_id != when["scene"]:
        return False
    if "scene_any" in when and facts.scene_id not in when["scene_any"]:
        return False
    if ("scene" in when or "scene_any" in when or "min_stability" in when) and (
        facts.stability < int(when.get("min_stability", 1))
    ):
        return False
    if "has_control" in when and not any(
        control.control_id == when["has_control"] for control in facts.controls
    ):
        return False
    if "has_control_text" in when:
        needle = str(when["has_control_text"]).casefold()
        if not any(
            control.text is not None and needle in control.text.casefold()
            for control in facts.controls
        ):
            return False
    for name, expected in when.get("var_equals", {}).items():
        if variables.get(name) != expected:
            return False
    for name, bound in when.get("var_less_than", {}).items():
        value = variables.get(name)
        if not isinstance(value, int) or value >= int(bound):
            return False
    return True


def _verify_matches(verify: Mapping[str, Any], facts: SceneFacts) -> bool:
    if "immediate" in verify:
        return True
    if facts.stability < int(verify.get("min_stability", 1)) and facts.scene_id is not None:
        return False
    if "scene" in verify:
        return facts.scene_id == verify["scene"]
    if "scene_any" in verify:
        return facts.scene_id in verify["scene_any"]
    return facts.scene_id != verify["scene_not"]


class TaskEngine:
    """Drive one declarative task over successive `SceneFacts`."""

    def __init__(
        self,
        definition: Mapping[str, Any],
        dispatcher: TaskDispatcher,
        *,
        scene_ids: set[str] | None = None,
        stream_size: tuple[int, int] = (1280, 720),
        clock: Callable[[], float] = time.monotonic,
        event_history_limit: int = 256,
    ) -> None:
        validate_task_semantics(definition, scene_ids)
        self._task = str(definition["task"])
        self._steps: Mapping[str, Any] = definition["steps"]
        self._interrupts: Mapping[str, str] = dict(definition.get("interrupts", {}))
        self._variables: dict[str, Any] = dict(definition.get("variables", {}))
        self._dispatcher = dispatcher
        self._stream_size = stream_size
        self._clock = clock
        self._step_id = str(definition["entry"])
        self._state = TaskState.WAITING
        self._entered_at = clock()
        self._acted_at = 0.0
        self._outcome: RunOutcome | None = None
        self._retry_count = 0
        self._visit_counts: dict[str, int] = {}
        self._actions_dispatched = 0
        self._events: deque[TaskEvent] = deque(maxlen=event_history_limit)
        self._event_count = 0

    # ---------------------------------------------------------------- events

    def _record(
        self, event_type: str, *, retry_count: int = 0, error_type: str | None = None
    ) -> None:
        self._event_count += 1
        self._events.append(
            TaskEvent(
                sequence=self._event_count,
                event_type=event_type,
                step=self._step_id,
                retry_count=retry_count,
                error_type=error_type,
            )
        )

    def events(self) -> tuple[TaskEvent, ...]:
        return tuple(self._events)

    def snapshot(self) -> TaskSnapshot:
        return TaskSnapshot(
            step=self._step_id,
            state=self._state,
            outcome=self._outcome,
            event_count=self._event_count,
            actions_dispatched=self._actions_dispatched,
        )

    @property
    def variables(self) -> Mapping[str, Any]:
        return dict(self._variables)

    # ----------------------------------------------------------------- flow

    def cancel(self) -> TaskSnapshot:
        if self._outcome is None:
            self._outcome = RunOutcome.CANCELLED
            self._state = TaskState.DONE
            self._record("cancelled")
            self._dispatcher.release_all()
        return self.snapshot()

    def _finish(self, outcome: RunOutcome) -> None:
        self._outcome = outcome
        self._state = TaskState.DONE
        self._record("succeeded" if outcome is RunOutcome.SUCCESS else "failed")

    def _enter(self, target: str) -> None:
        if target == SUCCESS:
            self._finish(RunOutcome.SUCCESS)
            return
        if target == FAILURE:
            self._finish(RunOutcome.FAILURE)
            return
        self._step_id = target
        self._state = TaskState.WAITING
        self._entered_at = self._clock()
        self._retry_count = 0

    def _fail_closed(self, error: Exception) -> None:
        self._record("fail_closed", error_type=type(error).__name__)
        try:
            self._dispatcher.release_all()
        finally:
            self._finish(RunOutcome.FAILURE)

    def tick(self, facts: SceneFacts) -> TaskSnapshot:
        """Advance the task by at most one action from one facts sample."""

        if self._state is TaskState.DONE:
            return self.snapshot()
        if self._state is TaskState.WAITING:
            self._tick_waiting(facts)
        elif self._state is TaskState.VERIFYING:
            self._tick_verifying(facts)
        return self.snapshot()

    # -------------------------------------------------------------- waiting

    def _tick_waiting(self, facts: SceneFacts) -> None:
        step = self._steps[self._step_id]
        interrupt_target = (
            self._interrupts.get(facts.scene_id) if facts.scene_id else None
        )
        if (
            interrupt_target is not None
            and interrupt_target != self._step_id
            and not _condition_matches(step["when"], facts, self._variables)
        ):
            self._record("interrupt")
            self._enter(interrupt_target)
            step = self._steps[self._step_id]
        if not _condition_matches(step["when"], facts, self._variables):
            timeout = step.get("wait_timeout_seconds")
            if timeout is not None and self._clock() - self._entered_at >= timeout:
                target = step.get("on_timeout")
                if target is None:
                    raise TaskError(
                        f"step '{self._step_id}' wait timed out without on_timeout"
                    )
                self._record("wait_timeout")
                self._enter(target)
            return
        self._perform_action(step, facts)

    def _perform_action(self, step: Mapping[str, Any], facts: SceneFacts) -> None:
        action = step["action"]
        visit = self._visit_counts.get(self._step_id, 0)
        # The visit counter makes every logical attempt's key unique; the input
        # driver's completed-key suppression downstream is what deduplicates a
        # re-sent key after a partial multi-action failure.
        key = f"{self._task}:{self._step_id}:{visit}"
        if self._retry_count:
            key = f"{key}:retry{self._retry_count}"
        try:
            dispatched = self._dispatch(action, facts, key)
        except Exception as error:
            self._fail_closed(error)
            return
        if dispatched:
            self._actions_dispatched += 1
        self._visit_counts[self._step_id] = visit + 1
        self._record("acted", retry_count=self._retry_count)
        self._begin_verification(step)

    def _begin_verification(self, step: Mapping[str, Any]) -> None:
        self._state = TaskState.VERIFYING
        self._acted_at = self._clock()

    def _resolve_control(
        self, action: Mapping[str, Any], facts: SceneFacts
    ) -> ControlFact | None:
        if "control" in action:
            for control in facts.controls:
                if control.control_id == action["control"]:
                    return control
            return None
        if "control_text" in action:
            needle = str(action["control_text"]).casefold()
            for control in facts.controls:
                if control.text is not None and needle in control.text.casefold():
                    return control
            return None
        if facts.recommended_id is None:
            return None
        for control in facts.controls:
            if control.control_id == facts.recommended_id:
                return control
        return None

    def _resolve_point(
        self, action: Mapping[str, Any], key: str
    ) -> tuple[int, int]:
        width, height = self._stream_size
        if key in action:
            x, y = (int(v) for v in action[key])
        else:
            fx, fy = (float(v) for v in action[f"{key}_norm"])
            x = int(round(fx * width))
            y = int(round(fy * height))
        if not (0 <= x < width and 0 <= y < height):
            raise TaskError(f"{key} point is outside the stream")
        return x, y

    def _dispatch(
        self, action: Mapping[str, Any], facts: SceneFacts, key: str
    ) -> bool:
        kind = action["kind"]
        if kind == "none":
            return False
        if kind == "set-var":
            self._variables[str(action["name"])] = action["value"]
            return False
        if kind == "add-var":
            name = str(action["name"])
            current = self._variables.get(name, 0)
            if not isinstance(current, int):
                raise TaskError(f"variable '{name}' is not an integer")
            self._variables[name] = current + int(action["value"])
            return False
        if kind == "dispatch":
            control = self._resolve_control(action, facts)
            if control is None:
                raise TaskError("dispatch target control is not present")
            width, height = self._stream_size
            if not (0 <= control.x < width and 0 <= control.y < height):
                raise TaskError("dispatch target is outside the stream")
            self._dispatcher.click(control.x, control.y, key)
            return True
        if kind == "key":
            self._dispatcher.key(
                str(action["key"]), float(action.get("hold_seconds", 0.0)), key
            )
            return True
        if kind == "drag":
            start = self._resolve_point(action, "from")
            end = self._resolve_point(action, "to")
            self._dispatcher.drag(
                start, end, float(action.get("duration_seconds", 0.5)), key
            )
            return True
        if kind == "scroll":
            self._dispatcher.scroll(int(action["clicks"]), key)
            return True
        if kind == "type-text":
            self._dispatcher.type_text(str(action["text"]), key)
            return True
        raise TaskError(f"unsupported action kind '{kind}'")

    # ------------------------------------------------------------ verifying

    def _tick_verifying(self, facts: SceneFacts) -> None:
        step = self._steps[self._step_id]
        verify = step["verify"]
        if _verify_matches(verify, facts):
            self._record("verified", retry_count=self._retry_count)
            self._enter(step["on_success"])
            return
        elapsed = self._clock() - self._acted_at
        retry_after = float(step.get("retry_after_seconds", 0.0))
        max_retries = int(step.get("max_retries", 0))
        if (
            max_retries > 0
            and self._retry_count < max_retries
            and retry_after > 0
            and elapsed >= retry_after
        ):
            self._retry_count += 1
            self._record("retry", retry_count=self._retry_count)
            self._state = TaskState.WAITING
            # The wait deadline restarts for the retry so time spent verifying
            # cannot silently consume the granted attempt.
            self._entered_at = self._clock()
            return
        if elapsed >= float(verify["timeout_seconds"]):
            target = step.get("on_failure") or step.get("on_timeout")
            if target is None:
                raise TaskError(
                    f"step '{self._step_id}' verification timed out without a target"
                )
            self._record("verify_timeout", retry_count=self._retry_count)
            self._enter(target)


__all__ = [
    "FAILURE",
    "SUCCESS",
    "TaskDefinitionError",
    "TaskDispatcher",
    "TaskEngine",
    "TaskError",
    "TaskEvent",
    "TaskSnapshot",
    "TaskState",
    "load_task_definition",
    "validate_task_semantics",
]
