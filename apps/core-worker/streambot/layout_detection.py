"""Declarative layout detection built on the perception predicate engine.

A control-manifest layout may declare ``detect`` as a predicate object; this
module evaluates those predicates against a frame and returns the matching
layout id, making "which screen am I on" data-driven.

A layout whose ``detect`` is a string (documentation) or absent is NOT
classified here — it stays detected by the target's own perception. That is the
deliberate runtime escape hatch for detectors (OCR resolution, fingerprint,
connected-component extraction) that cannot be expressed as static data.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .config import PerceptionSettings, PredicateSettings, RegionSettings, SignalSettings
from .control_surface import ManifestError
from .perception import OcrAdapter, PerceptionEngine, TemplateMatcher


PREDICATE_KINDS = frozenset({"pixel", "color", "template", "ocr"})
OPERATORS = frozenset({"all", "any", "not"})


def _build_predicate(name: str, region: str, spec: Mapping[str, Any]) -> PredicateSettings:
    kind = spec.get("kind")
    if kind not in PREDICATE_KINDS:
        raise ManifestError(f"detect predicate '{name}' has an unsupported kind {kind!r}")
    tolerance = int(spec.get("tolerance", 0))
    case_sensitive = bool(spec.get("case_sensitive", False))
    try:
        if kind == "pixel":
            return PredicateSettings(
                name=name, kind="pixel", region=region,
                x=int(spec["x"]), y=int(spec["y"]),
                bgr=tuple(int(c) for c in spec["bgr"]),
                tolerance=tolerance, case_sensitive=case_sensitive,
            )
        if kind == "color":
            return PredicateSettings(
                name=name, kind="color", region=region,
                bgr=tuple(int(c) for c in spec["bgr"]),
                tolerance=tolerance,
                minimum_fraction=float(spec["minimum_fraction"]),
                case_sensitive=case_sensitive,
            )
        if kind == "template":
            return PredicateSettings(
                name=name, kind="template", region=region,
                template=str(spec["template"]), threshold=float(spec["threshold"]),
                tolerance=tolerance, case_sensitive=case_sensitive,
            )
        return PredicateSettings(
            name=name, kind="ocr", region=region,
            contains=str(spec["contains"]), tolerance=tolerance,
            case_sensitive=case_sensitive,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ManifestError(f"detect predicate '{name}' is malformed: {error}") from error


class LayoutDetector:
    """Classify a frame into a manifest layout from declarative ``detect`` predicates."""

    def __init__(
        self,
        manifest: Mapping[str, Any],
        *,
        templates: Mapping[str, np.ndarray] | None = None,
        matcher: TemplateMatcher | None = None,
        ocr: OcrAdapter | None = None,
    ) -> None:
        layouts = manifest.get("layouts", {})
        regions: list[RegionSettings] = []
        predicates: list[PredicateSettings] = []
        signals: list[SignalSettings] = []
        order: list[str] = []
        for layout_id, layout in layouts.items():
            detect = layout.get("detect")
            if not isinstance(detect, Mapping):
                continue  # string/absent detect -> runtime detection escape hatch
            operator = detect.get("operator", "all")
            if operator not in OPERATORS:
                raise ManifestError(
                    f"layout '{layout_id}' detect.operator must be one of {sorted(OPERATORS)}"
                )
            preds = detect.get("predicates")
            if not isinstance(preds, list) or not preds:
                raise ManifestError(
                    f"layout '{layout_id}' detect.predicates must be a non-empty array"
                )
            if operator == "not" and len(preds) != 1:
                raise ManifestError(
                    f"layout '{layout_id}' detect 'not' requires exactly one predicate"
                )
            names: list[str] = []
            for index, spec in enumerate(preds):
                if not isinstance(spec, Mapping):
                    raise ManifestError(
                        f"layout '{layout_id}' detect predicate[{index}] must be an object"
                    )
                region = spec.get("region")
                if not (isinstance(region, (list, tuple)) and len(region) == 4):
                    raise ManifestError(
                        f"layout '{layout_id}' detect predicate[{index}] requires a [x,y,w,h] region"
                    )
                name = f"__detect__{layout_id}__{index}"
                rx, ry, rw, rh = (int(v) for v in region)
                regions.append(RegionSettings(name=name, x=rx, y=ry, width=rw, height=rh))
                predicates.append(_build_predicate(name, name, spec))
                names.append(name)
            signals.append(SignalSettings(name=layout_id, operator=operator, predicates=tuple(names)))
            order.append(layout_id)
        self._order = order
        self._settings = PerceptionSettings(
            regions=tuple(regions), predicates=tuple(predicates), signals=tuple(signals)
        )
        self._engine = PerceptionEngine(
            self._settings, templates=templates, matcher=matcher, ocr=ocr
        )

    @property
    def declarative_layouts(self) -> tuple[str, ...]:
        """Layout ids that this detector can classify (object ``detect`` only)."""

        return tuple(self._order)

    def classify(self, frame: np.ndarray) -> str | None:
        """Return the highest-priority layout whose declarative detect matches."""

        if not self._order:
            return None
        signals = self._engine.evaluate(frame).signals
        for layout_id in self._order:
            if signals.get(layout_id):
                return layout_id
        return None
