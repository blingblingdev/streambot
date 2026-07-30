"""Declarative, target-agnostic control-surface scanning.

A control manifest declares, per layout, the clickable controls and which one is
recommended. `ManifestControlScanner` turns the manifest plus a registry of
candidate extractors into `Control` values. Per-target onboarding becomes data
(the manifest) plus a small set of named extractors, rather than bespoke
branching in a scanner.

Layout detection stays with the target's perception (which layout is visible);
this component owns only control extraction and recommendation from the manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping


RECOMMEND_RULES = frozenset({"static-index", "none", "by-id", "context"})


class ManifestError(ValueError):
    """Raised when a control manifest violates the schema."""


@dataclass(frozen=True)
class Control:
    """One currently visible control in stream coordinates (metadata only)."""

    control_id: str
    action_kind: str
    x: int
    y: int
    confidence: float = 1.0


# An extractor maps (frame, params, context) to zero or more raw candidates.
# Each candidate is (index, x, y, confidence); index is None for a single
# control or an integer suffix when a layout yields several like controls.
Candidate = tuple[int | None, int, int, float]
Extractor = Callable[[Any, Mapping[str, Any], Mapping[str, Any]], "tuple[Candidate, ...]"]


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{path} must be an object")
    return value


def _strict_keys(
    data: Mapping[str, Any], *, required: set[str], optional: set[str], path: str
) -> None:
    keys = set(data.keys())
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ManifestError(f"{path} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ManifestError(f"{path} has unknown keys: {', '.join(sorted(unknown))}")


def _fixed_point_extractor(
    frame: Any, params: Mapping[str, Any], context: Mapping[str, Any]
) -> tuple[Candidate, ...]:
    """Built-in extractor: one control at a declared stream-space point."""

    return ((None, int(params["x"]), int(params["y"]), float(params.get("confidence", 1.0))),)


def _each_point_extractor(
    frame: Any, params: Mapping[str, Any], context: Mapping[str, Any]
) -> tuple[Candidate, ...]:
    """Built-in extractor: one indexed control per candidate point.

    The candidate points are supplied by the target through `context[source]` as
    an iterable of (x, y) pairs, keeping target-specific detection out of the
    platform. Confidence is a per-layout constant.

    `start_index` numbers the produced controls. A layout whose numbering
    depends on what is on screen (a scrollable rail that shows items 1-5 in one
    position and 3-6 in another) names a context key with `start_index_source`
    instead; the target reports the first visible item's number there and the
    fixed `start_index` becomes the fallback.
    """

    points = context.get(params["source"], ())
    confidence = float(params.get("confidence", 1.0))
    start = int(params.get("start_index", 0))
    source_key = params.get("start_index_source")
    if source_key is not None:
        dynamic = context.get(str(source_key))
        if dynamic is not None:
            start = int(dynamic)
    return tuple(
        (start + offset, int(x), int(y), confidence)
        for offset, (x, y) in enumerate(points)
    )


def _context_point_extractor(
    frame: Any, params: Mapping[str, Any], context: Mapping[str, Any]
) -> tuple[Candidate, ...]:
    """Built-in extractor: one control at a single point supplied by context.

    The target detects the point and places it in `context[source]` as an (x, y)
    pair. Unlike `each-point` the control id is not indexed.
    """

    point = context.get(params["source"])
    if point is None:
        return ()
    x, y = point
    return ((None, int(x), int(y), float(params.get("confidence", 1.0))),)


BUILTIN_EXTRACTORS: dict[str, Extractor] = {
    "fixed-point": _fixed_point_extractor,
    "each-point": _each_point_extractor,
    "context-point": _context_point_extractor,
}

# Declarative frame-based extractors (locate points from the frame, not context).
from .extractors import FRAME_EXTRACTORS  # noqa: E402

BUILTIN_EXTRACTORS.update(FRAME_EXTRACTORS)


def load_control_manifest(path: str | Path) -> dict[str, Any]:
    """Load and strictly validate a control manifest, failing closed."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    data = _require_mapping(raw, "manifest")
    _strict_keys(
        data, required={"schema_version", "target", "layouts"}, optional=set(), path="manifest"
    )
    if data["schema_version"] != 1:
        raise ManifestError("manifest schema_version must be 1")
    if not isinstance(data["target"], str) or not data["target"]:
        raise ManifestError("manifest target must be a non-empty string")
    layouts = _require_mapping(data["layouts"], "manifest.layouts")
    if not layouts:
        raise ManifestError("manifest.layouts must declare at least one layout")
    for layout_id, layout in layouts.items():
        _validate_layout(str(layout_id), layout)
    return dict(data)


def _validate_layout(layout_id: str, layout: object) -> None:
    node = _require_mapping(layout, f"layout '{layout_id}'")
    _strict_keys(
        node,
        required={"controls", "recommend"},
        optional={"detect", "observation_mode"},
        path=f"layout '{layout_id}'",
    )
    controls = node["controls"]
    if not isinstance(controls, list) or not controls:
        raise ManifestError(f"layout '{layout_id}' controls must be a non-empty array")
    seen_ids: set[str] = set()
    for index, control in enumerate(controls):
        cnode = _require_mapping(control, f"layout '{layout_id}' control[{index}]")
        _strict_keys(
            cnode,
            required={"id", "action_kind", "extractor"},
            optional=set(),
            path=f"layout '{layout_id}' control[{index}]",
        )
        control_id = cnode["id"]
        if not isinstance(control_id, str) or not control_id:
            raise ManifestError(
                f"layout '{layout_id}' control[{index}] id must be a non-empty string"
            )
        if control_id in seen_ids:
            raise ManifestError(
                f"layout '{layout_id}' has a duplicate control id '{control_id}'"
            )
        seen_ids.add(control_id)
        if not isinstance(cnode["action_kind"], str) or not cnode["action_kind"]:
            raise ManifestError(
                f"layout '{layout_id}' control '{control_id}' action_kind must be a non-empty string"
            )
        extractor = _require_mapping(
            cnode["extractor"], f"layout '{layout_id}' control '{control_id}' extractor"
        )
        if not isinstance(extractor.get("kind"), str) or not extractor["kind"]:
            raise ManifestError(
                f"layout '{layout_id}' control '{control_id}' extractor requires a kind"
            )
        if extractor["kind"] in {"each-point", "context-point"} and (
            not isinstance(extractor.get("source"), str) or not extractor["source"]
        ):
            raise ManifestError(
                f"layout '{layout_id}' control '{control_id}' {extractor['kind']} extractor requires a source"
            )
        if extractor["kind"] == "color-blob":
            region = extractor.get("region")
            bgr = extractor.get("bgr")
            if not (isinstance(region, (list, tuple)) and len(region) == 4):
                raise ManifestError(
                    f"layout '{layout_id}' control '{control_id}' color-blob extractor requires a [x,y,w,h] region"
                )
            if not (isinstance(bgr, (list, tuple)) and len(bgr) == 3):
                raise ManifestError(
                    f"layout '{layout_id}' control '{control_id}' color-blob extractor requires a [b,g,r] color"
                )
    recommend = _require_mapping(node["recommend"], f"layout '{layout_id}' recommend")
    rule = recommend.get("rule")
    if rule not in RECOMMEND_RULES:
        raise ManifestError(
            f"layout '{layout_id}' recommend.rule must be one of {sorted(RECOMMEND_RULES)}"
        )
    if rule == "static-index":
        index_value = recommend.get("index")
        if not isinstance(index_value, int) or isinstance(index_value, bool) or index_value < 0:
            raise ManifestError(
                f"layout '{layout_id}' static-index recommend requires a non-negative integer index"
            )
    elif rule == "by-id":
        if not isinstance(recommend.get("id"), str) or not recommend["id"]:
            raise ManifestError(f"layout '{layout_id}' by-id recommend requires an id")
    elif rule == "context":
        if not isinstance(recommend.get("key"), str) or not recommend["key"]:
            raise ManifestError(f"layout '{layout_id}' context recommend requires a key")


class ManifestControlScanner:
    """Produce controls and a recommendation for a detected layout from data."""

    def __init__(
        self,
        manifest: Mapping[str, Any],
        extractors: Mapping[str, Extractor] | None = None,
    ) -> None:
        self._layouts: Mapping[str, Any] = manifest["layouts"]
        self._extractors: dict[str, Extractor] = dict(BUILTIN_EXTRACTORS)
        if extractors:
            self._extractors.update(extractors)
        for layout_id, layout in self._layouts.items():
            for control in layout["controls"]:
                kind = control["extractor"]["kind"]
                if kind not in self._extractors:
                    raise ManifestError(
                        f"layout '{layout_id}' references unknown extractor kind '{kind}'"
                    )

    def has_layout(self, layout_id: str) -> bool:
        return layout_id in self._layouts

    def controls(
        self, layout_id: str, frame: Any, context: Mapping[str, Any] | None = None
    ) -> tuple[Control, ...]:
        layout = self._layouts.get(layout_id)
        if layout is None:
            return ()
        ctx = context or {}
        produced: list[Control] = []
        for control in layout["controls"]:
            base_id = control["id"]
            action_kind = control["action_kind"]
            spec = control["extractor"]
            extractor = self._extractors[spec["kind"]]
            params = {key: value for key, value in spec.items() if key != "kind"}
            for index, x, y, confidence in extractor(frame, params, ctx):
                control_id = base_id if index is None else f"{base_id}-{index}"
                produced.append(Control(control_id, action_kind, int(x), int(y), float(confidence)))
        return tuple(produced)

    def recommend(
        self,
        layout_id: str,
        controls: tuple[Control, ...],
        context: Mapping[str, Any] | None = None,
    ) -> str | None:
        layout = self._layouts.get(layout_id)
        if layout is None:
            return None
        recommend = layout["recommend"]
        rule = recommend["rule"]
        if rule == "none":
            return None
        if rule == "static-index":
            index = int(recommend["index"])
            if 0 <= index < len(controls):
                return controls[index].control_id
            return None
        if rule == "by-id":
            target = recommend["id"]
            return target if any(c.control_id == target for c in controls) else None
        if rule == "context":
            return (context or {}).get(recommend["key"])
        return None
