"""Unified declarative scene engine: frame -> SceneFacts.

One pipeline replaces the split perception stacks: a scene manifest declares,
per scene, how the scene is recognized (detector predicates), which controls it
exposes (extractors), and which control is recommended. `SceneEngine` executes
the manifest against in-memory BGR frames and returns `SceneFacts` — rich typed
facts (OCR lines with geometry, extracted controls with labels, scores,
stability) for internal consumers. Sanitization to metadata-only happens at the
external boundary via `SceneFacts.sanitized_summary`, not inside the pipeline.

The manifest is validated by the packaged JSON Schema
(`scene-manifest.schema.json`) executed through `jsonschema`; there is no
hand-rolled structural validator and no string escape hatch: every scene must
declare an executable detect group.

All manifest coordinates accept absolute stream pixels (`region`, `point`,
`click_point`, pixel `x`/`y`) or normalized fractions (`region_norm`,
`point_norm`, `click_point_norm`, `x_norm`/`y_norm`), so a manifest survives a
stream-resolution change. The exception is `template` predicates and
`template-grid` extractors: template pixels are resolution-bound by nature, so
manifests relying on templates must be recalibrated per stream size.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from .extractors import color_blob as _color_blob_extractor
from .observation import Observation
from .ocr import OcrLine
from .perception import AlignedNumpyTemplateMatcher, TemplateMatcher
from .temporal_perception import (
    FlickerPhase,
    SceneContext,
    TemporalCandidateSettings,
    TemporalFlickerDetector,
    TemporalSceneSettings,
)


class SceneManifestError(ValueError):
    """Raised when a scene manifest fails schema or semantic validation."""


class SceneError(RuntimeError):
    """Raised when the scene engine cannot safely evaluate a frame."""


class LineOcrAdapter:
    """Adapter contract: recognize a BGR region into geometric text lines."""

    def recognize_lines(self, image: np.ndarray) -> tuple[OcrLine, ...]:  # pragma: no cover - protocol
        raise NotImplementedError


@dataclass(frozen=True)
class OcrFact:
    """One recognized text line in stream coordinates."""

    text: str
    confidence: float
    center: tuple[int, int]
    box: tuple[int, int, int, int]


@dataclass(frozen=True)
class ControlFact:
    """One extracted control in stream coordinates, with its label if known."""

    control_id: str
    action_kind: str
    x: int
    y: int
    confidence: float = 1.0
    text: str | None = None


@dataclass(frozen=True)
class SceneFacts:
    """Rich typed result of one frame scan (internal dataflow, not output)."""

    frame_number: int
    scene_id: str | None
    controls: tuple[ControlFact, ...] = ()
    recommended_id: str | None = None
    ocr_lines: tuple[OcrFact, ...] = ()
    scores: Mapping[str, float] = field(default_factory=dict)
    stability: int = 0
    signature: str = ""

    def sanitized_summary(self) -> dict[str, Any]:
        """Metadata-only view for health output, logs, and IPC responses."""

        return {
            "frame_number": self.frame_number,
            "scene_id": self.scene_id,
            "control_ids": [control.control_id for control in self.controls],
            "recommended_id": self.recommended_id,
            "ocr_line_count": len(self.ocr_lines),
            "stability": self.stability,
            "signature": self.signature,
        }


_SCHEMA_PATH = Path(__file__).with_name("scene-manifest.schema.json")
_DETECTOR_COST = {
    "pixel": 0,
    "color": 1,
    "color-mask": 1,
    "template": 2,
    "temporal-flicker": 3,
    "ocr-contains": 4,
    "ocr-locate": 4,
}
_EXTENSION_COST = 3

# Extension contracts for genuinely novel target mechanics. Kinds must use the
# "x-" prefix and be registered at engine construction; a manifest referencing
# an unregistered extension kind fails closed. Detectors return
# (matched, score-or-None); extractors return (suffix-or-None, x, y,
# confidence, text-or-None) candidates. Both receive the per-frame scan
# context (frame, cached OCR access, external context).
ExtensionDetector = Callable[["_ScanContext", Mapping[str, Any]], "tuple[bool, float | None]"]
ExtensionExtractor = Callable[
    ["_ScanContext", Mapping[str, Any]],
    "tuple[tuple[int | str | None, int, int, float, str | None], ...]",
]


def _validate_extensions(
    scenes: Mapping[str, Any],
    detector_kinds: set[str],
    extractor_kinds: set[str],
) -> None:
    for scene_id, scene in scenes.items():
        for predicate in scene["detect"]["predicates"]:
            kind = predicate["kind"]
            if kind.startswith("x-") and kind not in detector_kinds:
                raise SceneManifestError(
                    f"scene '{scene_id}' references unregistered detector '{kind}'"
                )
        for control in scene.get("controls", []):
            kind = control["extractor"]["kind"]
            if kind.startswith("x-") and kind not in extractor_kinds:
                raise SceneManifestError(
                    f"scene '{scene_id}' references unregistered extractor '{kind}'"
                )


def load_scene_manifest(path: str | Path) -> dict[str, Any]:
    """Load a scene manifest, executing the packaged JSON Schema, failing closed."""

    import jsonschema

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SceneManifestError(f"scene manifest is unreadable: {error}") from error
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda err: list(err.absolute_path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path) or "manifest"
        raise SceneManifestError(f"{location}: {first.message}")
    _validate_semantics(raw)
    return raw


def _validate_semantics(manifest: Mapping[str, Any]) -> None:
    """Cross-field checks the structural schema cannot express."""

    for scene_id, scene in manifest["scenes"].items():
        controls = scene.get("controls", [])
        seen: set[str] = set()
        temporal_count = sum(
            1
            for predicate in scene["detect"]["predicates"]
            if predicate["kind"] == "temporal-flicker"
        )
        has_temporal_detect = temporal_count > 0
        if temporal_count > 1:
            raise SceneManifestError(
                f"scene '{scene_id}' declares more than one temporal-flicker predicate"
            )
        if (
            scene["detect"].get("operator", "all") == "not"
            and len(scene["detect"]["predicates"]) != 1
        ):
            raise SceneManifestError(
                f"scene '{scene_id}' detect 'not' requires exactly one predicate"
            )
        for predicate in scene["detect"]["predicates"]:
            if predicate["kind"] == "temporal-flicker":
                for candidate in predicate["candidates"]:
                    if "click_point" not in candidate and "click_point_norm" not in candidate:
                        raise SceneManifestError(
                            f"scene '{scene_id}' temporal candidate"
                            f" '{candidate['id']}' requires a click point"
                        )
        for control in controls:
            if control["id"] in seen:
                raise SceneManifestError(
                    f"scene '{scene_id}' has a duplicate control id '{control['id']}'"
                )
            seen.add(control["id"])
            if control["extractor"]["kind"] == "temporal-candidate" and not has_temporal_detect:
                raise SceneManifestError(
                    f"scene '{scene_id}' uses a temporal-candidate extractor"
                    " without a temporal-flicker detect predicate"
                )
        recommend = scene.get("recommend", {"rule": "none"})
        single_candidate_kinds = {"fixed-point", "context-point"}
        all_single = controls and all(
            control["extractor"]["kind"] in single_candidate_kinds
            for control in controls
        )
        # Indexed extractors expand one declared control into many at runtime,
        # so the load-time bound only applies to all-single-candidate scenes;
        # the runtime recommend path still bounds-checks every index.
        if (
            recommend["rule"] == "static-index"
            and all_single
            and recommend["index"] >= len(controls)
        ):
            raise SceneManifestError(
                f"scene '{scene_id}' static-index recommend index is out of range"
            )


def _resolve_region(
    spec: Mapping[str, Any], shape: tuple[int, int]
) -> tuple[int, int, int, int]:
    height, width = shape
    if "region" in spec:
        x, y, w, h = (int(v) for v in spec["region"])
    else:
        fx, fy, fw, fh = (float(v) for v in spec["region_norm"])
        x = int(round(fx * width))
        y = int(round(fy * height))
        w = max(1, int(round(fw * width)))
        h = max(1, int(round(fh * height)))
    if w < 1 or h < 1 or x < 0 or y < 0 or x + w > width or y + h > height:
        raise SceneError(f"region {x, y, w, h} exceeds frame bounds {width}x{height}")
    return x, y, w, h


def _resolve_point(
    spec: Mapping[str, Any],
    shape: tuple[int, int],
    *,
    key: str = "point",
) -> tuple[int, int]:
    height, width = shape
    if key in spec:
        x, y = (int(v) for v in spec[key])
    else:
        fx, fy = (float(v) for v in spec[f"{key}_norm"])
        x = int(round(fx * width))
        y = int(round(fy * height))
    if not (0 <= x < width and 0 <= y < height):
        raise SceneError(f"point {x, y} exceeds frame bounds {width}x{height}")
    return x, y


class _ScanContext:
    """Per-frame state shared by detectors and extractors (OCR cached once)."""

    def __init__(
        self,
        frame: np.ndarray,
        frame_number: int,
        *,
        ocr: LineOcrAdapter | None,
        external: Mapping[str, Any],
    ) -> None:
        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise SceneError("scene engine requires a uint8 BGR frame")
        self.frame = frame
        self.frame_number = frame_number
        self.shape: tuple[int, int] = (frame.shape[0], frame.shape[1])
        self.external = external
        self.ocr_facts: list[OcrFact] = []
        self.temporal_points: dict[str, tuple[tuple[str, int, int, float], ...]] = {}
        self._ocr = ocr
        self._ocr_cache: dict[tuple[int, int, int, int], tuple[OcrFact, ...]] = {}

    def ocr_region(self, region: tuple[int, int, int, int]) -> tuple[OcrFact, ...]:
        if region in self._ocr_cache:
            return self._ocr_cache[region]
        if self._ocr is None:
            raise SceneError("scene manifest uses OCR but no OCR adapter is injected")
        x, y, w, h = region
        lines = self._ocr.recognize_lines(self.frame[y : y + h, x : x + w])
        facts: list[OcrFact] = []
        for line in lines:
            xs = [point[0] for point in line.box]
            ys = [point[1] for point in line.box]
            left = int(round(min(xs))) + x
            top = int(round(min(ys))) + y
            box_width = max(1, int(round(max(xs) - min(xs))))
            box_height = max(1, int(round(max(ys) - min(ys))))
            facts.append(
                OcrFact(
                    text=line.text,
                    confidence=float(line.confidence),
                    center=(left + box_width // 2, top + box_height // 2),
                    box=(left, top, box_width, box_height),
                )
            )
        facts.sort(key=lambda fact: (fact.box[1], fact.box[0]))
        result = tuple(facts)
        self._ocr_cache[region] = result
        self.ocr_facts.extend(result)
        return result


def _matches_color(pixels: np.ndarray, bgr: list[int], tolerance: int) -> np.ndarray:
    target = np.asarray(bgr, dtype=np.int16)
    difference = np.abs(pixels.astype(np.int16) - target)
    return np.all(difference <= tolerance, axis=-1)


def _text_matches(
    text: str, needles: list[str], *, case_sensitive: bool
) -> bool:
    haystack = text if case_sensitive else text.casefold()
    for needle in needles:
        needle_cmp = needle if case_sensitive else needle.casefold()
        if needle_cmp in haystack:
            return True
    return False


class _TemporalPredicateState:
    """Stateful wrapper turning the flicker detector into a detector kind."""

    def __init__(self, scene_id: str, spec: Mapping[str, Any]) -> None:
        self.scene_id = scene_id
        self.spec = spec
        self.detector = TemporalFlickerDetector()
        self._settings_cache: dict[tuple[int, int], TemporalSceneSettings] = {}

    def settings(self, shape: tuple[int, int]) -> TemporalSceneSettings:
        cached = self._settings_cache.get(shape)
        if cached is not None:
            return cached
        candidates = tuple(
            TemporalCandidateSettings(
                candidate_id=str(candidate["id"]),
                action_kind="click",
                region=_resolve_region(candidate, shape),
                click_point=_resolve_point(candidate, shape, key="click_point"),
                high_threshold=float(candidate.get("high_threshold", 0.16)),
                low_threshold=float(candidate.get("low_threshold", 0.07)),
                minimum_high_samples=int(candidate.get("minimum_high_samples", 2)),
            )
            for candidate in self.spec["candidates"]
        )
        settings = TemporalSceneSettings(
            scene_id=self.scene_id,
            scene_evidence="declarative-detect",
            deadline_ms=4000,
            candidates=candidates,
            ambiguity_margin=float(self.spec.get("ambiguity_margin", 0.04)),
            camera_cut_threshold=float(self.spec.get("camera_cut_threshold", 0.10)),
            baseline_samples=int(self.spec.get("baseline_samples", 3)),
            history_size=int(self.spec.get("history_size", 6)),
        )
        self._settings_cache[shape] = settings
        return settings


class SceneEngine:
    """Execute a scene manifest against frames and produce `SceneFacts`."""

    def __init__(
        self,
        manifest: Mapping[str, Any],
        *,
        templates: Mapping[str, np.ndarray] | None = None,
        matcher: TemplateMatcher | None = None,
        ocr: LineOcrAdapter | None = None,
        clock: Callable[[], float] = time.monotonic,
        extra_detectors: Mapping[str, ExtensionDetector] | None = None,
        extra_extractors: Mapping[str, ExtensionExtractor] | None = None,
    ) -> None:
        self._scenes: Mapping[str, Any] = manifest["scenes"]
        self._templates = dict(templates or {})
        self._matcher = matcher or AlignedNumpyTemplateMatcher()
        self._ocr = ocr
        self._clock = clock
        self._extra_detectors = dict(extra_detectors or {})
        self._extra_extractors = dict(extra_extractors or {})
        for name in self._extra_detectors:
            if not name.startswith("x-"):
                raise SceneManifestError(
                    f"extension detector '{name}' must use the x- prefix"
                )
        for name in self._extra_extractors:
            if not name.startswith("x-"):
                raise SceneManifestError(
                    f"extension extractor '{name}' must use the x- prefix"
                )
        _validate_extensions(
            self._scenes, set(self._extra_detectors), set(self._extra_extractors)
        )
        self._epoch = 0
        self._last_scene_id: str | None = None
        self._stability = 0
        order = sorted(
            self._scenes.items(),
            key=lambda item: (-int(item[1].get("priority", 0)),),
        )
        self._order: tuple[str, ...] = tuple(scene_id for scene_id, _scene in order)
        self._temporal: dict[tuple[str, int], _TemporalPredicateState] = {}
        for scene_id, scene in self._scenes.items():
            for index, predicate in enumerate(scene["detect"]["predicates"]):
                if predicate["kind"] == "temporal-flicker":
                    self._temporal[(scene_id, index)] = _TemporalPredicateState(
                        scene_id, predicate
                    )
        missing = {
            spec["template"]
            for scene in self._scenes.values()
            for spec in scene["detect"]["predicates"]
            if spec["kind"] == "template"
        } | {
            control["extractor"]["template"]
            for scene in self._scenes.values()
            for control in scene.get("controls", [])
            if control["extractor"]["kind"] == "template-grid"
        }
        missing -= set(self._templates)
        if missing:
            raise SceneManifestError(
                f"manifest references unavailable templates: {sorted(missing)}"
            )

    def add_scene(self, scene_id: str, scene: Mapping[str, Any]) -> None:
        """Add one validated scene at runtime (novel-scene resolution cache).

        The fragment is validated against the packaged schema by wrapping it in
        a minimal manifest, then merged; classification order is rebuilt so
        priorities keep working. Existing scene ids cannot be replaced.
        """

        import jsonschema

        if scene_id in self._scenes:
            raise SceneManifestError(f"scene '{scene_id}' already exists")
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        wrapper = {
            "schema_version": 2,
            "target": "fragment",
            "scenes": {scene_id: scene},
        }
        validator = jsonschema.Draft7Validator(schema)
        errors = sorted(
            validator.iter_errors(wrapper), key=lambda err: list(err.absolute_path)
        )
        if errors:
            raise SceneManifestError(errors[0].message)
        _validate_semantics(wrapper)
        _validate_extensions(
            wrapper["scenes"], set(self._extra_detectors), set(self._extra_extractors)
        )
        scenes = dict(self._scenes)
        scenes[scene_id] = scene
        self._scenes = scenes
        order = sorted(
            self._scenes.items(),
            key=lambda item: (-int(item[1].get("priority", 0)),),
        )
        self._order = tuple(sid for sid, _scene in order)
        for index, predicate in enumerate(scene["detect"]["predicates"]):
            if predicate["kind"] == "temporal-flicker":
                self._temporal[(scene_id, index)] = _TemporalPredicateState(
                    scene_id, predicate
                )

    @property
    def scene_ids(self) -> tuple[str, ...]:
        return tuple(self._order)

    # ------------------------------------------------------------------ scan

    def reset(self, epoch: int | None = None) -> None:
        """Clear cross-frame state (reconnect, workflow epoch change)."""

        self._epoch = self._epoch + 1 if epoch is None else epoch
        self._last_scene_id = None
        self._stability = 0
        for state in self._temporal.values():
            state.detector.reset(self._epoch)

    def observe(
        self,
        frame: np.ndarray,
        frame_number: int,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> SceneFacts:
        """Classify the frame, extract the scene's controls, and recommend."""

        ctx = _ScanContext(
            frame, frame_number, ocr=self._ocr, external=dict(context or {})
        )
        scores: dict[str, float] = {}
        matched: str | None = None
        for scene_id in self._order:
            if self._detect(scene_id, ctx, scores):
                matched = scene_id
                break

        if matched == self._last_scene_id and matched is not None:
            self._stability += 1
        else:
            self._stability = 1 if matched is not None else 0
        self._last_scene_id = matched

        controls: tuple[ControlFact, ...] = ()
        recommended: str | None = None
        if matched is not None:
            controls = self._extract_controls(matched, ctx)
            recommended = self._recommend(matched, controls, ctx)
        signature = self._signature(matched, controls)
        return SceneFacts(
            frame_number=frame_number,
            scene_id=matched,
            controls=controls,
            recommended_id=recommended,
            ocr_lines=tuple(ctx.ocr_facts),
            scores=scores,
            stability=self._stability,
            signature=signature,
        )

    # -------------------------------------------------------------- detectors

    def _detect(
        self, scene_id: str, ctx: _ScanContext, scores: dict[str, float]
    ) -> bool:
        detect = self._scenes[scene_id]["detect"]
        operator = detect.get("operator", "all")
        indexed = list(enumerate(detect["predicates"]))
        indexed.sort(
            key=lambda item: (
                _DETECTOR_COST.get(item[1]["kind"], _EXTENSION_COST),
                item[0],
            )
        )
        results: list[bool] = []
        for index, predicate in indexed:
            value, score = self._evaluate_predicate(scene_id, index, predicate, ctx)
            if score is not None:
                scores[f"{scene_id}[{index}]"] = score
            results.append(value)
            if operator == "all" and not value:
                return False
            if operator == "any" and value:
                return True
        if operator == "all":
            return bool(results) and all(results)
        if operator == "any":
            return False
        return not results[0]

    def _evaluate_predicate(
        self,
        scene_id: str,
        index: int,
        predicate: Mapping[str, Any],
        ctx: _ScanContext,
    ) -> tuple[bool, float | None]:
        kind = predicate["kind"]
        if kind.startswith("x-"):
            matched, score = self._extra_detectors[kind](ctx, predicate)
            return bool(matched), score
        if kind == "temporal-flicker":
            return self._evaluate_temporal(scene_id, index, predicate, ctx)
        if kind == "pixel":
            x, y, w, h = _resolve_region(predicate, ctx.shape)
            px = (
                int(predicate["x"])
                if "x" in predicate
                else int(round(float(predicate["x_norm"]) * (w - 1)))
            )
            py = (
                int(predicate["y"])
                if "y" in predicate
                else int(round(float(predicate["y_norm"]) * (h - 1)))
            )
            if px >= w or py >= h:
                raise SceneError(
                    f"scene '{scene_id}' pixel predicate is outside its region"
                )
            pixel = ctx.frame[y + py, x + px][None, :]
            matched = _matches_color(
                pixel, predicate["bgr"], int(predicate.get("tolerance", 0))
            )
            return bool(matched[0]), None
        if kind == "color":
            x, y, w, h = _resolve_region(predicate, ctx.shape)
            fraction = float(
                _matches_color(
                    ctx.frame[y : y + h, x : x + w],
                    predicate["bgr"],
                    int(predicate.get("tolerance", 0)),
                ).mean()
            )
            return fraction >= float(predicate["minimum_fraction"]), fraction
        if kind == "color-mask":
            x, y, w, h = _resolve_region(predicate, ctx.shape)
            region = ctx.frame[y : y + h, x : x + w].astype(np.int16)
            mask = np.ones(region.shape[:2], dtype=bool)
            channels = {"b": region[:, :, 0], "g": region[:, :, 1], "r": region[:, :, 2]}
            for rule in predicate["rules"]:
                if "channel" in rule:
                    plane = channels[rule["channel"]]
                elif "diff" in rule:
                    first, second = rule["diff"]
                    plane = channels[first] - channels[second]
                else:  # "gray": mean over channels, schema-guaranteed
                    plane = region.mean(axis=2)
                if "gt" in rule:
                    mask &= plane > float(rule["gt"])
                if "lt" in rule:
                    mask &= plane < float(rule["lt"])
            fraction = float(mask.mean())
            passed = True
            if "min_fraction" in predicate:
                passed = passed and fraction >= float(predicate["min_fraction"])
            if "below_fraction" in predicate:
                passed = passed and fraction < float(predicate["below_fraction"])
            return passed, fraction
        if kind == "template":
            x, y, w, h = _resolve_region(predicate, ctx.shape)
            template = self._templates[predicate["template"]]
            score = self._matcher.score(ctx.frame[y : y + h, x : x + w], template)
            if not 0.0 <= score <= 1.0:
                raise SceneError("template matcher returned an invalid score")
            return score >= float(predicate["threshold"]), score
        if kind == "ocr-contains":
            region = _resolve_region(predicate, ctx.shape)
            lines = ctx.ocr_region(region)
            minimum = float(predicate.get("min_confidence", 0.0))
            case_sensitive = bool(predicate.get("case_sensitive", False))
            hit = any(
                line.confidence >= minimum
                and _text_matches(
                    line.text, [predicate["contains"]], case_sensitive=case_sensitive
                )
                for line in lines
            )
            return hit, None
        if kind == "ocr-locate":
            region = _resolve_region(predicate, ctx.shape)
            lines = ctx.ocr_region(region)
            minimum = float(predicate.get("min_confidence", 0.0))
            case_sensitive = bool(predicate.get("case_sensitive", False))
            located = [
                line
                for line in lines
                if line.confidence >= minimum
                and _text_matches(
                    line.text, list(predicate["match_any"]), case_sensitive=case_sensitive
                )
            ]
            best = max((line.confidence for line in located), default=0.0)
            return bool(located), best
        raise SceneError(f"unsupported detector kind '{kind}'")

    def _evaluate_temporal(
        self,
        scene_id: str,
        index: int,
        predicate: Mapping[str, Any],
        ctx: _ScanContext,
    ) -> tuple[bool, float | None]:
        state = self._temporal[(scene_id, index)]
        settings = state.settings(ctx.shape)
        observation = Observation(
            frame_number=ctx.frame_number,
            observed_at=self._clock(),
            data=ctx.frame,
        )
        results = state.detector.observe(
            observation,
            SceneContext(scene_id=scene_id, epoch=self._epoch, entered_at=0.0),
            settings,
        )
        stable = [
            result
            for result in results
            if result.stable and result.phase is FlickerPhase.HIGH
        ]
        if stable:
            by_id = {
                candidate.candidate_id: candidate for candidate in settings.candidates
            }
            ctx.temporal_points[scene_id] = tuple(
                (
                    result.candidate_id,
                    by_id[result.candidate_id].click_point[0],
                    by_id[result.candidate_id].click_point[1],
                    result.confidence,
                )
                for result in stable
            )
        best = max((result.flicker_score for result in results), default=0.0)
        return bool(stable), best

    # -------------------------------------------------------------- extract

    def _extract_controls(
        self, scene_id: str, ctx: _ScanContext
    ) -> tuple[ControlFact, ...]:
        produced: list[ControlFact] = []
        for control in self._scenes[scene_id].get("controls", []):
            spec = control["extractor"]
            kind = spec["kind"]
            base_id = control["id"]
            action_kind = control["action_kind"]
            confidence = float(spec.get("confidence", 1.0))
            if kind.startswith("x-"):
                for suffix, x, y, ext_confidence, text in self._extra_extractors[kind](
                    ctx, spec
                ):
                    control_id = base_id if suffix is None else f"{base_id}-{suffix}"
                    produced.append(
                        ControlFact(
                            control_id,
                            action_kind,
                            int(x),
                            int(y),
                            min(confidence, float(ext_confidence)),
                            text=text,
                        )
                    )
            elif kind == "fixed-point":
                x, y = _resolve_point(spec, ctx.shape)
                produced.append(ControlFact(base_id, action_kind, x, y, confidence))
            elif kind == "context-point":
                point = ctx.external.get(spec["source"])
                if point is not None:
                    produced.append(
                        ControlFact(
                            base_id, action_kind, int(point[0]), int(point[1]), confidence
                        )
                    )
            elif kind == "each-point":
                start = int(spec.get("start_index", 0))
                # A rail whose numbering depends on its scroll position reports
                # the first visible item's number through this context key; see
                # control_surface._each_point_extractor for the same contract.
                start_source = spec.get("start_index_source")
                if start_source is not None:
                    dynamic = ctx.external.get(str(start_source))
                    if dynamic is not None:
                        start = int(dynamic)
                for offset, point in enumerate(ctx.external.get(spec["source"], ())):
                    produced.append(
                        ControlFact(
                            f"{base_id}-{start + offset}",
                            action_kind,
                            int(point[0]),
                            int(point[1]),
                            confidence,
                        )
                    )
            elif kind == "color-blob":
                params = dict(spec)
                params["region"] = list(_resolve_region(spec, ctx.shape))
                params.pop("region_norm", None)
                for idx, x, y, blob_confidence in _color_blob_extractor(
                    ctx.frame, params, ctx.external
                ):
                    produced.append(
                        ControlFact(
                            f"{base_id}-{idx}", action_kind, x, y, blob_confidence
                        )
                    )
            elif kind == "template-grid":
                for idx, x, y, grid_confidence in self._template_grid(spec, ctx):
                    produced.append(
                        ControlFact(
                            f"{base_id}-{idx}", action_kind, x, y, grid_confidence
                        )
                    )
            elif kind == "ocr-line":
                region = _resolve_region(spec, ctx.shape)
                lines = ctx.ocr_region(region)
                minimum = float(spec.get("min_confidence", 0.0))
                case_sensitive = bool(spec.get("case_sensitive", False))
                needles = list(spec.get("match_any", []))
                selected = [
                    line
                    for line in lines
                    if line.confidence >= minimum
                    and (
                        not needles
                        or _text_matches(
                            line.text, needles, case_sensitive=case_sensitive
                        )
                    )
                ]
                maximum = spec.get("max_candidates")
                if maximum is not None:
                    selected = selected[: int(maximum)]
                for idx, line in enumerate(selected):
                    produced.append(
                        ControlFact(
                            f"{base_id}-{idx}",
                            action_kind,
                            line.center[0],
                            line.center[1],
                            min(confidence, line.confidence),
                            text=line.text,
                        )
                    )
            elif kind == "temporal-candidate":
                for candidate_id, x, y, temporal_confidence in ctx.temporal_points.get(
                    scene_id, ()
                ):
                    produced.append(
                        ControlFact(
                            f"{base_id}-{candidate_id}",
                            action_kind,
                            x,
                            y,
                            min(confidence, temporal_confidence),
                        )
                    )
            else:  # unreachable behind schema validation; keep failing closed
                raise SceneError(f"unsupported extractor kind '{kind}'")
        return tuple(produced)

    def _template_grid(
        self, spec: Mapping[str, Any], ctx: _ScanContext
    ) -> tuple[tuple[int, int, int, float], ...]:
        try:
            import cv2
        except ImportError as error:  # pragma: no cover - cv2 ships with rapidocr
            raise SceneError("template-grid requires OpenCV") from error
        x, y, w, h = _resolve_region(spec, ctx.shape)
        template = self._templates[spec["template"]]
        if template.shape[0] > h or template.shape[1] > w:
            raise SceneError("template-grid template does not fit its region")
        # Normalized squared difference keeps flat (single-color) templates
        # well-defined, unlike correlation-based scores.
        result = 1.0 - cv2.matchTemplate(
            ctx.frame[y : y + h, x : x + w], template, cv2.TM_SQDIFF_NORMED
        )
        result = np.nan_to_num(result, nan=0.0, posinf=0.0, neginf=0.0)
        threshold = float(spec["threshold"])
        min_distance = int(
            spec.get("min_distance", max(template.shape[0], template.shape[1]))
        )
        candidates = [
            (float(result[py, px]), int(px), int(py))
            for py, px in zip(*np.nonzero(result >= threshold))
        ]
        candidates.sort(reverse=True)
        kept: list[tuple[float, int, int]] = []
        for score, px, py in candidates:
            if all(
                abs(px - kx) >= min_distance or abs(py - ky) >= min_distance
                for _score, kx, ky in kept
            ):
                kept.append((score, px, py))
        kept.sort(key=lambda item: (item[2], item[1]))
        maximum = spec.get("max_candidates")
        if maximum is not None:
            kept = kept[: int(maximum)]
        confidence_cap = float(spec.get("confidence", 1.0))
        return tuple(
            (
                index,
                x + px + template.shape[1] // 2,
                y + py + template.shape[0] // 2,
                min(confidence_cap, max(0.0, min(1.0, score))),
            )
            for index, (score, px, py) in enumerate(kept)
        )

    # ------------------------------------------------------------- recommend

    def _recommend(
        self,
        scene_id: str,
        controls: tuple[ControlFact, ...],
        ctx: _ScanContext,
    ) -> str | None:
        recommend = self._scenes[scene_id].get("recommend", {"rule": "none"})
        rule = recommend["rule"]
        if rule == "none":
            return None
        if rule == "static-index":
            index = int(recommend["index"])
            return controls[index].control_id if index < len(controls) else None
        if rule == "by-id":
            target = recommend["id"]
            return target if any(c.control_id == target for c in controls) else None
        if rule == "by-text":
            case_sensitive = bool(recommend.get("case_sensitive", False))
            for control in controls:
                if control.text is not None and _text_matches(
                    control.text, [recommend["text"]], case_sensitive=case_sensitive
                ):
                    return control.control_id
            return None
        if rule == "context":
            value = ctx.external.get(recommend["key"])
            if value is None:
                return None
            return value if any(c.control_id == value for c in controls) else None
        raise SceneError(f"unsupported recommend rule '{rule}'")

    # ------------------------------------------------------------- signature

    @staticmethod
    def _signature(scene_id: str | None, controls: tuple[ControlFact, ...]) -> str:
        digest = hashlib.sha256()
        digest.update((scene_id or "").encode("utf-8"))
        for control in controls:
            digest.update(
                f"|{control.control_id}@{control.x},{control.y}:{control.text or ''}".encode(
                    "utf-8"
                )
            )
        return digest.hexdigest()[:16]


__all__ = [
    "ControlFact",
    "ExtensionDetector",
    "ExtensionExtractor",
    "LineOcrAdapter",
    "OcrFact",
    "SceneEngine",
    "SceneError",
    "SceneFacts",
    "SceneManifestError",
    "load_scene_manifest",
]
