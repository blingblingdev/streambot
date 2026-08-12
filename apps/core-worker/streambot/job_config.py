"""Settings an operator can change while the job keeps running.

Every tunable a job has — how long to idle, how many cycles to run, how often
to poll — used to be a module constant. Changing one meant editing code and
restarting, and a restart is not free: the job loses its place, and whatever it
was in the middle of has to be reached again.

So a job *declares* its settings in `job.json` and reads their values from this
module:

    "config": {
      "fields": [
        {"key": "idle_seconds", "label": "Idle", "type": "integer",
         "min": 30, "max": 3600, "default": 210, "unit": "s"}
      ],
      "presets": [{"label": "Short", "values": {"idle_seconds": 210}}]
    }

The declaration is versioned with the job; the values are not. Values live
under the worker's own state as one small file per job, written atomically by
whoever edits them and re-read by the job when the file changes. Nothing is
pushed: the job notices at a moment of its own choosing, which is what makes
this safe — a setting can never change halfway through an action.

Three fields exist for every job without being declared: `max_cycles` and
`max_seconds`, where **0 means unlimited**, and `poll_seconds`. A job should
not have to re-invent its own stop conditions.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import ConfigurationError, _integer, _mapping, _number, _strict_keys

MAX_FIELDS = 64
MAX_PRESETS = 16
MAX_TEXT_LENGTH = 200
# A multi-line field holds a list — one rule per line — so it needs more room
# than a single-line box, but still a bound the console can refuse past.
MAX_MULTILINE_TEXT_LENGTH = 4000

FIELD_TYPES = {"integer", "number", "boolean", "enum", "text"}


def default_values_dir() -> Path:
    """Where this machine keeps job settings: beside the worker's own state."""

    home = os.environ.get("STREAMBOT_HOME")
    root = Path(home) if home else Path(__file__).resolve().parents[3]
    return root / ".state" / "job-config"


@dataclass(frozen=True)
class ConfigField:
    """One declared setting."""

    key: str
    label: str
    type: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""
    help: str = ""
    multiline: bool = False

    @classmethod
    def from_mapping(cls, data: object, path: str) -> "ConfigField":
        mapping = _mapping(data, path)
        _strict_keys(
            mapping,
            required={"key", "type", "default"},
            optional={"label", "min", "max", "choices", "unit", "help", "multiline"},
            path=path,
        )
        key = mapping["key"]
        if not isinstance(key, str) or not key:
            raise ConfigurationError(f"{path}.key must be a non-empty string")
        kind = mapping["type"]
        if kind not in FIELD_TYPES:
            raise ConfigurationError(
                f"{path}.type must be one of: {', '.join(sorted(FIELD_TYPES))}"
            )
        minimum = mapping.get("min")
        maximum = mapping.get("max")
        choices: tuple[str, ...] = ()
        if kind == "enum":
            raw = mapping.get("choices")
            if not isinstance(raw, Sequence) or isinstance(raw, str) or not raw:
                raise ConfigurationError(f"{path}.choices must be a non-empty list")
            choices = tuple(str(choice) for choice in raw)
        multiline = bool(mapping.get("multiline", False))
        if multiline and kind != "text":
            raise ConfigurationError(f"{path}.multiline is only valid for a text field")
        if kind in {"integer", "number"}:
            if minimum is None or maximum is None:
                # A number without bounds is a number nobody can safely type
                # into a box: bounds are what let the console refuse nonsense
                # before it reaches a running job.
                raise ConfigurationError(f"{path} must declare min and max")
            minimum = float(_number(minimum, f"{path}.min", -1e12, 1e12))
            maximum = float(_number(maximum, f"{path}.max", -1e12, 1e12))
            if maximum < minimum:
                raise ConfigurationError(f"{path}.max must not be below min")
        field = cls(
            key=key,
            label=str(mapping.get("label", key)),
            type=str(kind),
            default=mapping["default"],
            minimum=minimum,
            maximum=maximum,
            choices=choices,
            unit=str(mapping.get("unit", "")),
            help=str(mapping.get("help", "")),
            multiline=multiline,
        )
        # A default that the field itself would reject is a declaration bug.
        field.coerce(field.default, f"{path}.default")
        return field

    def coerce(self, value: object, path: str | None = None) -> Any:
        """Return the value as this field's type, or raise ConfigurationError."""

        where = path or f"config.{self.key}"
        if self.type == "boolean":
            if not isinstance(value, bool):
                raise ConfigurationError(f"{where} must be true or false")
            return value
        if self.type == "enum":
            text = str(value)
            if text not in self.choices:
                raise ConfigurationError(
                    f"{where} must be one of: {', '.join(self.choices)}"
                )
            return text
        if self.type == "text":
            text = str(value)
            limit = MAX_MULTILINE_TEXT_LENGTH if self.multiline else MAX_TEXT_LENGTH
            if len(text) > limit:
                raise ConfigurationError(f"{where} is too long")
            return text
        if self.type == "integer":
            return _integer(value, where, int(self.minimum), int(self.maximum))
        return _number(value, where, float(self.minimum), float(self.maximum))

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "default": self.default,
        }
        if self.minimum is not None:
            payload["min"] = self.minimum
        if self.maximum is not None:
            payload["max"] = self.maximum
        if self.choices:
            payload["choices"] = list(self.choices)
        if self.unit:
            payload["unit"] = self.unit
        if self.help:
            payload["help"] = self.help
        if self.multiline:
            payload["multiline"] = True
        return payload


# Given to every job. A job that wants different bounds may redeclare the key
# with its own; a job that says nothing still gets working stop conditions.
BUILTIN_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        key="max_cycles",
        label="Cycles",
        type="integer",
        default=0,
        minimum=0,
        maximum=100_000,
        help="Stop after this many cycles. 0 runs until stopped.",
    ),
    ConfigField(
        key="max_seconds",
        label="Runtime",
        type="integer",
        default=0,
        minimum=0,
        maximum=86_400,
        unit="s",
        help="Stop after this long. 0 runs until stopped.",
    ),
    ConfigField(
        key="poll_seconds",
        label="Poll",
        type="number",
        default=3.0,
        minimum=0.2,
        maximum=60.0,
        unit="s",
        help="How often the job looks at the screen.",
    ),
)


@dataclass(frozen=True)
class ConfigPreset:
    """A named set of values an operator can apply in one go."""

    label: str
    values: dict[str, Any]

    @classmethod
    def from_mapping(cls, data: object, path: str) -> "ConfigPreset":
        mapping = _mapping(data, path)
        _strict_keys(mapping, required={"label", "values"}, optional=set(), path=path)
        values = _mapping(mapping["values"], f"{path}.values")
        return cls(label=str(mapping["label"]), values=dict(values))

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "values": dict(self.values)}


@dataclass(frozen=True)
class ConfigSchema:
    """A job's declared settings: the built-ins plus its own."""

    fields: tuple[ConfigField, ...]
    presets: tuple[ConfigPreset, ...] = ()

    @classmethod
    def from_manifest(cls, data: object, path: str = "job.config") -> "ConfigSchema":
        """Build from a `job.json` `config` block (absent means built-ins only)."""

        if data is None:
            return cls(fields=BUILTIN_FIELDS)
        mapping = _mapping(data, path)
        _strict_keys(mapping, required=set(), optional={"fields", "presets"}, path=path)
        raw_fields = mapping.get("fields", [])
        if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, str):
            raise ConfigurationError(f"{path}.fields must be a list")
        if len(raw_fields) > MAX_FIELDS:
            raise ConfigurationError(f"{path}.fields exceeds the field cap")
        declared = [
            ConfigField.from_mapping(entry, f"{path}.fields[{index}]")
            for index, entry in enumerate(raw_fields)
        ]
        seen: dict[str, ConfigField] = {field.key: field for field in BUILTIN_FIELDS}
        for field in declared:
            seen[field.key] = field  # a job may tighten a built-in's bounds
        ordered = tuple(declared) + tuple(
            field for field in BUILTIN_FIELDS if field.key not in
            {declared_field.key for declared_field in declared}
        )
        del seen

        raw_presets = mapping.get("presets", [])
        if not isinstance(raw_presets, Sequence) or isinstance(raw_presets, str):
            raise ConfigurationError(f"{path}.presets must be a list")
        if len(raw_presets) > MAX_PRESETS:
            raise ConfigurationError(f"{path}.presets exceeds the preset cap")
        presets = tuple(
            ConfigPreset.from_mapping(entry, f"{path}.presets[{index}]")
            for index, entry in enumerate(raw_presets)
        )
        by_key = {field.key: field for field in ordered}
        for index, preset in enumerate(presets):
            for key, value in preset.values.items():
                if key not in by_key:
                    raise ConfigurationError(
                        f"{path}.presets[{index}].values has an undeclared key: {key}"
                    )
                by_key[key].coerce(value, f"{path}.presets[{index}].values.{key}")
        return cls(fields=ordered, presets=presets)

    @property
    def by_key(self) -> dict[str, ConfigField]:
        return {field.key: field for field in self.fields}

    def defaults(self) -> dict[str, Any]:
        return {field.key: field.default for field in self.fields}

    def validate(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Coerce a set of values, rejecting unknown keys and bad ranges."""

        by_key = self.by_key
        unknown = set(values) - set(by_key)
        if unknown:
            raise ConfigurationError(
                f"unknown setting: {', '.join(sorted(unknown))}"
            )
        return {key: by_key[key].coerce(value) for key, value in values.items()}

    def resolve(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Defaults with the stored values applied over them.

        A stored value that no longer fits its field — a job tightened a bound,
        or removed a setting — is dropped rather than raised: the operator's
        old preference must not be able to stop the job from starting.
        """

        resolved = self.defaults()
        by_key = self.by_key
        for key, value in values.items():
            field = by_key.get(key)
            if field is None:
                continue
            try:
                resolved[key] = field.coerce(value)
            except ConfigurationError:
                continue
        return resolved

    def as_dict(self) -> dict[str, Any]:
        return {
            "fields": [field.as_dict() for field in self.fields],
            "presets": [preset.as_dict() for preset in self.presets],
        }


def values_path(job_name: str, values_dir: Path | None = None) -> Path:
    return (values_dir or default_values_dir()) / f"{job_name}.json"


def read_values(path: Path) -> dict[str, Any] | None:
    """Stored values, or None if they could not be read.

    None and `{}` mean different things and must not be confused: `{}` is an
    operator who cleared every setting, None is a file that is missing, broken,
    or caught mid-write. Treating the second as the first would silently revert
    a running job to its defaults.
    """

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dict(data) if isinstance(data, dict) else None


def write_values(path: Path, values: Mapping[str, Any]) -> None:
    """Replace the stored values atomically.

    A reader can be a job mid-poll, so the file must never be half-written:
    write a sibling and rename, which is atomic within a directory.
    """

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    )
    try:
        with handle:
            json.dump(dict(values), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


class JobConfig:
    """A job's live settings: declared schema, stored values, hot reload.

    The job calls `get` whenever it wants a setting and `poll` at a point where
    a change is safe to adopt. `poll` returns the keys that changed, so a job
    can react to a new value rather than merely use it next time.
    """

    def __init__(
        self,
        schema: ConfigSchema,
        path: Path,
        *,
        clock=time.monotonic,
    ) -> None:
        self.schema = schema
        self.path = Path(path)
        self._clock = clock
        self._mtime: int | None = None
        self._stored: dict[str, Any] = {}
        self._values: dict[str, Any] = schema.defaults()
        self.poll()

    @property
    def values(self) -> dict[str, Any]:
        return dict(self._values)

    def get(self, key: str) -> Any:
        return self._values[key]

    def poll(self) -> dict[str, Any]:
        """Adopt an edit made while the job is running; return what changed.

        Compares mtime rather than re-parsing every time, and keeps the values
        it has if the file is caught mid-write — the next poll will see the
        finished file.
        """

        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return {}
        if mtime == self._mtime:
            return {}
        stored = read_values(self.path)
        if stored is None:
            # Caught mid-write, or briefly unreadable. Keep what we have and
            # leave the recorded mtime alone so the next poll tries again.
            return {}
        self._mtime = mtime
        if stored == self._stored:
            return {}
        self._stored = stored
        updated = self.schema.resolve(stored)
        changed = {
            key: value
            for key, value in updated.items()
            if self._values.get(key) != value
        }
        self._values = updated
        return changed


__all__ = [
    "BUILTIN_FIELDS",
    "ConfigField",
    "ConfigPreset",
    "ConfigSchema",
    "JobConfig",
    "default_values_dir",
    "read_values",
    "values_path",
    "write_values",
]
