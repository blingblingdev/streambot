"""Strict configuration schema for a headless automation worker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ConfigurationError(ValueError):
    """Raised when an automation profile violates the schema."""


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{path} must be an object")
    return value


def _strict_keys(
    data: Mapping[str, Any], *, required: set[str], optional: set[str], path: str
) -> None:
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing:
        raise ConfigurationError(f"{path} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ConfigurationError(f"{path} has unknown keys: {', '.join(sorted(unknown))}")


def _integer(value: object, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{path} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{path} must be between {minimum} and {maximum}")
    return value


def _number(value: object, path: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{path} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ConfigurationError(f"{path} must be between {minimum} and {maximum}")
    return result


@dataclass(frozen=True)
class StreamSettings:
    """Requested Moonlight stream parameters."""

    width: int = 1280
    height: int = 720
    fps: int = 15
    bitrate_kbps: int = 4000
    codec: str = "h264"

    @classmethod
    def from_mapping(cls, value: object) -> "StreamSettings":
        data = _mapping(value, "stream")
        allowed = {"width", "height", "fps", "bitrate_kbps", "codec"}
        _strict_keys(data, required=set(), optional=allowed, path="stream")
        codec = data.get("codec", "h264")
        if codec != "h264":
            raise ConfigurationError("stream.codec must be h264")
        return cls(
            width=_integer(data.get("width", 1280), "stream.width", 320, 7680),
            height=_integer(data.get("height", 720), "stream.height", 240, 4320),
            fps=_integer(data.get("fps", 15), "stream.fps", 1, 120),
            bitrate_kbps=_integer(
                data.get("bitrate_kbps", 4000),
                "stream.bitrate_kbps",
                500,
                100000,
            ),
            codec=codec,
        )


@dataclass(frozen=True)
class ObservationSettings:
    """Frame conversion and automation sampling settings."""

    sample_fps: float = 2.0
    decoder: str = "videotoolbox"
    software_fallback: bool = True

    @classmethod
    def from_mapping(cls, value: object) -> "ObservationSettings":
        data = _mapping(value, "observation")
        allowed = {"sample_fps", "decoder", "software_fallback"}
        _strict_keys(data, required=set(), optional=allowed, path="observation")
        decoder = data.get("decoder", "videotoolbox")
        if decoder not in {"videotoolbox", "software"}:
            raise ConfigurationError("observation.decoder is unsupported")
        fallback = data.get("software_fallback", True)
        if not isinstance(fallback, bool):
            raise ConfigurationError("observation.software_fallback must be a boolean")
        return cls(
            sample_fps=_number(
                data.get("sample_fps", 2.0), "observation.sample_fps", 0.1, 30.0
            ),
            decoder=decoder,
            software_fallback=fallback,
        )


def _name(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{path} must be a non-empty string")
    return value.strip()


def _bgr(value: object, path: str) -> tuple[int, int, int]:
    if not isinstance(value, list) or len(value) != 3:
        raise ConfigurationError(f"{path} must contain three channels")
    return tuple(
        _integer(channel, f"{path}[{index}]", 0, 255)
        for index, channel in enumerate(value)
    )


@dataclass(frozen=True)
class RegionSettings:
    """Absolute pixel rectangle within the configured stream frame."""

    name: str
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_mapping(cls, value: object, index: int) -> "RegionSettings":
        path = f"perception.regions[{index}]"
        data = _mapping(value, path)
        _strict_keys(
            data,
            required={"name", "x", "y", "width", "height"},
            optional=set(),
            path=path,
        )
        return cls(
            name=_name(data["name"], f"{path}.name"),
            x=_integer(data["x"], f"{path}.x", 0, 7679),
            y=_integer(data["y"], f"{path}.y", 0, 4319),
            width=_integer(data["width"], f"{path}.width", 1, 7680),
            height=_integer(data["height"], f"{path}.height", 1, 4320),
        )


@dataclass(frozen=True)
class PredicateSettings:
    """One typed visual predicate evaluated against a named region."""

    name: str
    kind: str
    region: str
    x: int | None = None
    y: int | None = None
    bgr: tuple[int, int, int] | None = None
    tolerance: int = 0
    minimum_fraction: float | None = None
    template: str | None = None
    threshold: float | None = None
    contains: str | None = None
    case_sensitive: bool = False

    @classmethod
    def from_mapping(cls, value: object, index: int) -> "PredicateSettings":
        path = f"perception.predicates[{index}]"
        data = _mapping(value, path)
        base = {"name", "type", "region"}
        kind = data.get("type")
        if kind == "pixel":
            required = base | {"x", "y", "bgr"}
            optional = {"tolerance"}
        elif kind == "color":
            required = base | {"bgr", "minimum_fraction"}
            optional = {"tolerance"}
        elif kind == "template":
            required = base | {"template", "threshold"}
            optional = set()
        elif kind == "ocr":
            required = base | {"contains"}
            optional = {"case_sensitive"}
        else:
            raise ConfigurationError(f"{path}.type is unsupported")
        _strict_keys(data, required=required, optional=optional, path=path)

        case_sensitive = data.get("case_sensitive", False)
        if not isinstance(case_sensitive, bool):
            raise ConfigurationError(f"{path}.case_sensitive must be a boolean")
        return cls(
            name=_name(data["name"], f"{path}.name"),
            kind=kind,
            region=_name(data["region"], f"{path}.region"),
            x=(
                _integer(data["x"], f"{path}.x", 0, 7679)
                if "x" in data
                else None
            ),
            y=(
                _integer(data["y"], f"{path}.y", 0, 4319)
                if "y" in data
                else None
            ),
            bgr=_bgr(data["bgr"], f"{path}.bgr") if "bgr" in data else None,
            tolerance=_integer(
                data.get("tolerance", 0), f"{path}.tolerance", 0, 255
            ),
            minimum_fraction=(
                _number(
                    data["minimum_fraction"],
                    f"{path}.minimum_fraction",
                    0.0,
                    1.0,
                )
                if "minimum_fraction" in data
                else None
            ),
            template=(
                _name(data["template"], f"{path}.template")
                if "template" in data
                else None
            ),
            threshold=(
                _number(data["threshold"], f"{path}.threshold", 0.0, 1.0)
                if "threshold" in data
                else None
            ),
            contains=(
                _name(data["contains"], f"{path}.contains")
                if "contains" in data
                else None
            ),
            case_sensitive=case_sensitive,
        )


@dataclass(frozen=True)
class SignalSettings:
    """Boolean composition of named predicates."""

    name: str
    operator: str
    predicates: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: object, index: int) -> "SignalSettings":
        path = f"perception.signals[{index}]"
        data = _mapping(value, path)
        _strict_keys(
            data,
            required={"name", "operator", "predicates"},
            optional=set(),
            path=path,
        )
        operator = data["operator"]
        if operator not in {"all", "any", "not"}:
            raise ConfigurationError(f"{path}.operator is unsupported")
        values = data["predicates"]
        if not isinstance(values, list) or not values:
            raise ConfigurationError(f"{path}.predicates must be a non-empty list")
        predicates = tuple(
            _name(item, f"{path}.predicates[{item_index}]")
            for item_index, item in enumerate(values)
        )
        if operator == "not" and len(predicates) != 1:
            raise ConfigurationError(f"{path}.not requires exactly one predicate")
        return cls(
            name=_name(data["name"], f"{path}.name"),
            operator=operator,
            predicates=predicates,
        )


@dataclass(frozen=True)
class PerceptionSettings:
    """Regions, predicates, and composed visual signals."""

    regions: tuple[RegionSettings, ...] = ()
    predicates: tuple[PredicateSettings, ...] = ()
    signals: tuple[SignalSettings, ...] = ()

    @classmethod
    def from_mapping(cls, value: object) -> "PerceptionSettings":
        data = _mapping(value, "perception")
        _strict_keys(
            data,
            required=set(),
            optional={"regions", "predicates", "signals"},
            path="perception",
        )
        raw_regions = data.get("regions", [])
        raw_predicates = data.get("predicates", [])
        raw_signals = data.get("signals", [])
        for raw, path in (
            (raw_regions, "perception.regions"),
            (raw_predicates, "perception.predicates"),
            (raw_signals, "perception.signals"),
        ):
            if not isinstance(raw, list):
                raise ConfigurationError(f"{path} must be a list")
        result = cls(
            regions=tuple(
                RegionSettings.from_mapping(item, index)
                for index, item in enumerate(raw_regions)
            ),
            predicates=tuple(
                PredicateSettings.from_mapping(item, index)
                for index, item in enumerate(raw_predicates)
            ),
            signals=tuple(
                SignalSettings.from_mapping(item, index)
                for index, item in enumerate(raw_signals)
            ),
        )
        result.validate_references()
        return result

    def validate_references(self) -> None:
        region_names = [region.name for region in self.regions]
        predicate_names = [predicate.name for predicate in self.predicates]
        signal_names = [signal.name for signal in self.signals]
        for values, path in (
            (region_names, "perception.regions"),
            (predicate_names, "perception.predicates"),
            (signal_names, "perception.signals"),
        ):
            if len(values) != len(set(values)):
                raise ConfigurationError(f"{path} contains duplicate names")
        known_regions = set(region_names)
        known_predicates = set(predicate_names)
        for predicate in self.predicates:
            if predicate.region not in known_regions:
                raise ConfigurationError(
                    f"predicate {predicate.name} references an unknown region"
                )
        for signal in self.signals:
            if not set(signal.predicates) <= known_predicates:
                raise ConfigurationError(
                    f"signal {signal.name} references an unknown predicate"
                )


@dataclass(frozen=True)
class InputActionSettings:
    """One strictly bounded input action."""

    name: str
    kind: str
    dx: int | None = None
    dy: int | None = None
    x: int | None = None
    y: int | None = None
    button: str | None = None
    operation: str | None = None
    clicks: int | None = None
    key_code: int | None = None
    modifiers: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: object, index: int) -> "InputActionSettings":
        path = f"actions[{index}]"
        data = _mapping(value, path)
        base = {"name", "type"}
        kind = data.get("type")
        if kind == "mouse_move":
            required = base | {"dx", "dy"}
            optional = set()
        elif kind == "mouse_position":
            required = base | {"x", "y"}
            optional = set()
        elif kind == "mouse_button":
            required = base | {"button"}
            optional = {"operation"}
        elif kind == "scroll":
            required = base | {"clicks"}
            optional = set()
        elif kind == "key":
            required = base | {"key_code"}
            optional = {"operation", "modifiers"}
        else:
            raise ConfigurationError(f"{path}.type is unsupported")
        _strict_keys(data, required=required, optional=optional, path=path)

        operation = data.get("operation")
        button = data.get("button")
        modifiers: tuple[str, ...] = ()
        if kind == "mouse_button":
            if button not in {"left", "middle", "right", "x1", "x2"}:
                raise ConfigurationError(f"{path}.button is unsupported")
            operation = operation or "click"
            if operation not in {"click", "press", "release"}:
                raise ConfigurationError(f"{path}.operation is unsupported")
        elif kind == "key":
            operation = operation or "tap"
            if operation not in {"tap", "down", "up"}:
                raise ConfigurationError(f"{path}.operation is unsupported")
            raw_modifiers = data.get("modifiers", [])
            if not isinstance(raw_modifiers, list):
                raise ConfigurationError(f"{path}.modifiers must be a list")
            modifiers = tuple(
                _name(item, f"{path}.modifiers[{modifier_index}]")
                for modifier_index, item in enumerate(raw_modifiers)
            )
            if not set(modifiers) <= {"shift", "ctrl", "alt", "meta"}:
                raise ConfigurationError(f"{path}.modifiers contains unsupported values")
            if len(modifiers) != len(set(modifiers)):
                raise ConfigurationError(f"{path}.modifiers contains duplicates")

        dx = _integer(data["dx"], f"{path}.dx", -32767, 32767) if "dx" in data else None
        dy = _integer(data["dy"], f"{path}.dy", -32767, 32767) if "dy" in data else None
        if kind == "mouse_move" and dx == 0 and dy == 0:
            raise ConfigurationError(f"{path} mouse movement cannot be zero")
        clicks = (
            _integer(data["clicks"], f"{path}.clicks", -127, 127)
            if "clicks" in data
            else None
        )
        if kind == "scroll" and clicks == 0:
            raise ConfigurationError(f"{path}.clicks cannot be zero")
        return cls(
            name=_name(data["name"], f"{path}.name"),
            kind=kind,
            dx=dx,
            dy=dy,
            x=_integer(data["x"], f"{path}.x", 0, 7679) if "x" in data else None,
            y=_integer(data["y"], f"{path}.y", 0, 4319) if "y" in data else None,
            button=button,
            operation=operation,
            clicks=clicks,
            key_code=(
                _integer(data["key_code"], f"{path}.key_code", 1, 255)
                if "key_code" in data
                else None
            ),
            modifiers=modifiers,
        )


@dataclass(frozen=True)
class TransitionSettings:
    """Guarded transition and its declarative action identifiers."""

    name: str
    signal: str
    equals: bool
    target: str
    actions: tuple[str, ...] = ()
    idempotency_key: str | None = None
    max_retries: int = 0
    failure_state: str | None = None

    @classmethod
    def from_mapping(
        cls, value: object, state_index: int, transition_index: int
    ) -> "TransitionSettings":
        path = f"workflow.states[{state_index}].transitions[{transition_index}]"
        data = _mapping(value, path)
        _strict_keys(
            data,
            required={"name", "signal", "target"},
            optional={
                "equals",
                "actions",
                "idempotency_key",
                "max_retries",
                "failure_state",
            },
            path=path,
        )
        equals = data.get("equals", True)
        if not isinstance(equals, bool):
            raise ConfigurationError(f"{path}.equals must be a boolean")
        raw_actions = data.get("actions", [])
        if not isinstance(raw_actions, list):
            raise ConfigurationError(f"{path}.actions must be a list")
        actions = tuple(
            _name(action, f"{path}.actions[{index}]")
            for index, action in enumerate(raw_actions)
        )
        idempotency_key = data.get("idempotency_key")
        max_retries = _integer(
            data.get("max_retries", 0), f"{path}.max_retries", 0, 20
        )
        failure_state = data.get("failure_state")
        if actions:
            idempotency_key = _name(idempotency_key, f"{path}.idempotency_key")
            failure_state = _name(failure_state, f"{path}.failure_state")
        elif any(
            key in data
            for key in ("idempotency_key", "max_retries", "failure_state")
        ):
            raise ConfigurationError(
                f"{path} cannot configure retries without actions"
            )
        return cls(
            name=_name(data["name"], f"{path}.name"),
            signal=_name(data["signal"], f"{path}.signal"),
            equals=equals,
            target=_name(data["target"], f"{path}.target"),
            actions=actions,
            idempotency_key=idempotency_key,
            max_retries=max_retries,
            failure_state=failure_state,
        )


@dataclass(frozen=True)
class WorkflowStateSettings:
    """One state with guarded transitions or a terminal outcome."""

    name: str
    transitions: tuple[TransitionSettings, ...] = ()
    timeout_seconds: float | None = None
    timeout_state: str | None = None
    terminal: str | None = None

    @classmethod
    def from_mapping(cls, value: object, index: int) -> "WorkflowStateSettings":
        path = f"workflow.states[{index}]"
        data = _mapping(value, path)
        _strict_keys(
            data,
            required={"name"},
            optional={
                "transitions",
                "timeout_seconds",
                "timeout_state",
                "terminal",
            },
            path=path,
        )
        terminal = data.get("terminal")
        if terminal is not None and terminal not in {"success", "failure"}:
            raise ConfigurationError(f"{path}.terminal is unsupported")
        raw_transitions = data.get("transitions", [])
        if not isinstance(raw_transitions, list):
            raise ConfigurationError(f"{path}.transitions must be a list")
        has_timeout = "timeout_seconds" in data or "timeout_state" in data
        if has_timeout and not {
            "timeout_seconds",
            "timeout_state",
        } <= data.keys():
            raise ConfigurationError(f"{path} requires both timeout fields")
        timeout_seconds = (
            _number(
                data["timeout_seconds"], f"{path}.timeout_seconds", 0.01, 86400.0
            )
            if has_timeout
            else None
        )
        timeout_state = (
            _name(data["timeout_state"], f"{path}.timeout_state")
            if has_timeout
            else None
        )
        transitions = tuple(
            TransitionSettings.from_mapping(item, index, transition_index)
            for transition_index, item in enumerate(raw_transitions)
        )
        if terminal is not None and (transitions or has_timeout):
            raise ConfigurationError(f"{path} terminal state cannot transition")
        if terminal is None and not has_timeout:
            raise ConfigurationError(f"{path} non-terminal state requires a timeout")
        names = [transition.name for transition in transitions]
        if len(names) != len(set(names)):
            raise ConfigurationError(f"{path}.transitions contains duplicate names")
        guards = [(transition.signal, transition.equals) for transition in transitions]
        if len(guards) != len(set(guards)):
            raise ConfigurationError(f"{path}.transitions contains ambiguous guards")
        return cls(
            name=_name(data["name"], f"{path}.name"),
            transitions=transitions,
            timeout_seconds=timeout_seconds,
            timeout_state=timeout_state,
            terminal=terminal,
        )


@dataclass(frozen=True)
class WorkflowSettings:
    """Validated declarative state-machine configuration."""

    initial_state: str
    states: tuple[WorkflowStateSettings, ...]
    event_history_limit: int = 1000

    @classmethod
    def from_mapping(
        cls,
        value: object,
        perception: PerceptionSettings,
        actions: tuple[InputActionSettings, ...],
    ) -> "WorkflowSettings":
        data = _mapping(value, "workflow")
        _strict_keys(
            data,
            required={"initial_state", "states"},
            optional={"event_history_limit"},
            path="workflow",
        )
        raw_states = data["states"]
        if not isinstance(raw_states, list) or not raw_states:
            raise ConfigurationError("workflow.states must be a non-empty list")
        result = cls(
            initial_state=_name(data["initial_state"], "workflow.initial_state"),
            states=tuple(
                WorkflowStateSettings.from_mapping(item, index)
                for index, item in enumerate(raw_states)
            ),
            event_history_limit=_integer(
                data.get("event_history_limit", 1000),
                "workflow.event_history_limit",
                10,
                10000,
            ),
        )
        result.validate_references(perception, actions)
        return result

    def validate_references(
        self,
        perception: PerceptionSettings,
        actions: tuple[InputActionSettings, ...],
    ) -> None:
        state_names = [state.name for state in self.states]
        if len(state_names) != len(set(state_names)):
            raise ConfigurationError("workflow.states contains duplicate names")
        known_states = set(state_names)
        if self.initial_state not in known_states:
            raise ConfigurationError("workflow.initial_state is unknown")
        known_signals = {signal.name for signal in perception.signals}
        known_actions = {action.name for action in actions}
        terminal_outcomes = {state.terminal for state in self.states if state.terminal}
        if terminal_outcomes != {"success", "failure"}:
            raise ConfigurationError(
                "workflow requires success and failure terminal states"
            )
        idempotency_keys: list[str] = []
        for state in self.states:
            if state.timeout_state is not None and state.timeout_state not in known_states:
                raise ConfigurationError(
                    f"state {state.name} references an unknown timeout state"
                )
            for transition in state.transitions:
                if transition.signal not in known_signals:
                    raise ConfigurationError(
                        f"transition {transition.name} references an unknown signal"
                    )
                for target in (transition.target, transition.failure_state):
                    if target is not None and target not in known_states:
                        raise ConfigurationError(
                            f"transition {transition.name} references an unknown state"
                        )
                if transition.idempotency_key is not None:
                    idempotency_keys.append(transition.idempotency_key)
                if not set(transition.actions) <= known_actions:
                    raise ConfigurationError(
                        f"transition {transition.name} references an unknown action"
                    )
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise ConfigurationError("workflow contains duplicate idempotency keys")

        terminal_states = {state.name for state in self.states if state.terminal}
        predecessors: dict[str, set[str]] = {name: set() for name in known_states}
        for state in self.states:
            targets = {state.timeout_state} if state.timeout_state is not None else set()
            for transition in state.transitions:
                targets.add(transition.target)
                if transition.failure_state is not None:
                    targets.add(transition.failure_state)
            for target in targets:
                predecessors[target].add(state.name)
        can_reach_terminal = set(terminal_states)
        pending = list(terminal_states)
        while pending:
            target = pending.pop()
            for predecessor in predecessors[target]:
                if predecessor not in can_reach_terminal:
                    can_reach_terminal.add(predecessor)
                    pending.append(predecessor)
        unreachable = known_states - can_reach_terminal
        if unreachable:
            raise ConfigurationError(
                "workflow states cannot reach a terminal: "
                + ", ".join(sorted(unreachable))
            )


@dataclass(frozen=True)
class SafetySettings:
    """Non-negotiable session and input boundaries."""

    preserve_existing_desktop: bool = True
    dry_run: bool = True
    max_actions_per_minute: int = 30

    @classmethod
    def from_mapping(cls, value: object) -> "SafetySettings":
        data = _mapping(value, "safety")
        allowed = {"preserve_existing_desktop", "dry_run", "max_actions_per_minute"}
        _strict_keys(data, required=set(), optional=allowed, path="safety")
        preserve = data.get("preserve_existing_desktop", True)
        dry_run = data.get("dry_run", True)
        if preserve is not True:
            raise ConfigurationError("safety.preserve_existing_desktop must be true")
        if not isinstance(dry_run, bool):
            raise ConfigurationError("safety.dry_run must be a boolean")
        return cls(
            preserve_existing_desktop=True,
            dry_run=dry_run,
            max_actions_per_minute=_integer(
                data.get("max_actions_per_minute", 30),
                "safety.max_actions_per_minute",
                1,
                600,
            ),
        )


@dataclass(frozen=True)
class RuntimeSettings:
    """Reconnect, liveness, and status timing for a long-running worker."""

    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    max_reconnect_attempts: int = 5
    liveness_timeout_seconds: float = 10.0
    status_interval_seconds: float = 5.0
    # Poll cadence while the environment is not ready (host asleep, no
    # Desktop session). Environmental waits are patient and unbounded, so
    # they poll slowly instead of following the reconnect backoff.
    environment_poll_seconds: float = 10.0

    @classmethod
    def from_mapping(cls, value: object) -> "RuntimeSettings":
        data = _mapping(value, "runtime")
        allowed = {
            "reconnect_initial_seconds",
            "reconnect_max_seconds",
            "max_reconnect_attempts",
            "liveness_timeout_seconds",
            "status_interval_seconds",
            "environment_poll_seconds",
        }
        _strict_keys(data, required=set(), optional=allowed, path="runtime")
        result = cls(
            reconnect_initial_seconds=_number(
                data.get("reconnect_initial_seconds", 1.0),
                "runtime.reconnect_initial_seconds",
                0.01,
                60.0,
            ),
            reconnect_max_seconds=_number(
                data.get("reconnect_max_seconds", 30.0),
                "runtime.reconnect_max_seconds",
                0.01,
                300.0,
            ),
            max_reconnect_attempts=_integer(
                data.get("max_reconnect_attempts", 5),
                "runtime.max_reconnect_attempts",
                0,
                100,
            ),
            liveness_timeout_seconds=_number(
                data.get("liveness_timeout_seconds", 10.0),
                "runtime.liveness_timeout_seconds",
                0.5,
                300.0,
            ),
            status_interval_seconds=_number(
                data.get("status_interval_seconds", 5.0),
                "runtime.status_interval_seconds",
                0.1,
                300.0,
            ),
            environment_poll_seconds=_number(
                data.get("environment_poll_seconds", 10.0),
                "runtime.environment_poll_seconds",
                0.5,
                600.0,
            ),
        )
        if result.reconnect_max_seconds < result.reconnect_initial_seconds:
            raise ConfigurationError(
                "runtime.reconnect_max_seconds must not be smaller than the initial delay"
            )
        return result


@dataclass(frozen=True)
class AutomationProfile:
    """Top-level immutable worker configuration."""

    name: str
    stream: StreamSettings
    observation: ObservationSettings
    perception: PerceptionSettings
    actions: tuple[InputActionSettings, ...]
    workflow: WorkflowSettings | None
    safety: SafetySettings
    runtime: RuntimeSettings

    @classmethod
    def from_mapping(cls, value: object) -> "AutomationProfile":
        data = _mapping(value, "profile")
        _strict_keys(
            data,
            required={"name"},
            optional={
                "stream",
                "observation",
                "perception",
                "actions",
                "workflow",
                "safety",
                "runtime",
            },
            path="profile",
        )
        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError("profile.name must be a non-empty string")
        perception = PerceptionSettings.from_mapping(data.get("perception", {}))
        raw_actions = data.get("actions", [])
        if not isinstance(raw_actions, list):
            raise ConfigurationError("actions must be a list")
        actions = tuple(
            InputActionSettings.from_mapping(item, index)
            for index, item in enumerate(raw_actions)
        )
        action_names = [action.name for action in actions]
        if len(action_names) != len(set(action_names)):
            raise ConfigurationError("actions contains duplicate names")
        profile = cls(
            name=name.strip(),
            stream=StreamSettings.from_mapping(data.get("stream", {})),
            observation=ObservationSettings.from_mapping(
                data.get("observation", {})
            ),
            perception=perception,
            actions=actions,
            workflow=(
                WorkflowSettings.from_mapping(data["workflow"], perception, actions)
                if "workflow" in data
                else None
            ),
            safety=SafetySettings.from_mapping(data.get("safety", {})),
            runtime=RuntimeSettings.from_mapping(data.get("runtime", {})),
        )
        profile.validate_perception_bounds()
        profile.validate_action_bounds()
        return profile

    def validate_perception_bounds(self) -> None:
        """Reject regions and pixel predicates outside configured dimensions."""

        regions = {region.name: region for region in self.perception.regions}
        for region in self.perception.regions:
            if (
                region.x + region.width > self.stream.width
                or region.y + region.height > self.stream.height
            ):
                raise ConfigurationError(
                    f"region {region.name} exceeds configured stream bounds"
                )
        for predicate in self.perception.predicates:
            if predicate.kind != "pixel":
                continue
            region = regions[predicate.region]
            if (
                predicate.x is None
                or predicate.y is None
                or predicate.x >= region.width
                or predicate.y >= region.height
            ):
                raise ConfigurationError(
                    f"pixel predicate {predicate.name} exceeds region bounds"
                )

    def validate_action_bounds(self) -> None:
        """Reject absolute coordinates outside the configured stream plane."""

        for action in self.actions:
            if action.kind != "mouse_position":
                continue
            if (
                action.x is None
                or action.y is None
                or action.x >= self.stream.width
                or action.y >= self.stream.height
            ):
                raise ConfigurationError(
                    f"mouse position action {action.name} exceeds stream bounds"
                )


def load_profile(path: str | Path) -> AutomationProfile:
    """Load and validate a JSON automation profile."""

    profile_path = Path(path)
    try:
        value = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError("profile could not be loaded") from error
    return AutomationProfile.from_mapping(value)
