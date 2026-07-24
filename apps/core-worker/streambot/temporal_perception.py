"""Bounded cross-frame perception for declared flashing interaction regions."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
import math
import time
from typing import Callable, Mapping

import numpy as np

from .events import ActionCandidate
from .observation import Observation
from .perception_service import Detection, ModeRequest, ObservationMode


class TemporalManifestError(ValueError):
    """Raised when a temporal interaction manifest is unsafe or incomplete."""


class FlickerPhase(StrEnum):
    BASELINE = "baseline"
    RISING = "rising"
    HIGH = "high"
    FALLING = "falling"
    REJECTED = "rejected"


@dataclass(frozen=True)
class TemporalCandidateSettings:
    candidate_id: str
    action_kind: str
    region: tuple[int, int, int, int]
    click_point: tuple[int, int]
    high_threshold: float = 0.16
    low_threshold: float = 0.07
    minimum_high_samples: int = 2
    event_ttl_ms: int = 400
    feedback: str = "candidate-cleared-or-scene-changed"
    retry_limit: int = 1
    priority: int = 0


@dataclass(frozen=True)
class TemporalSceneSettings:
    scene_id: str
    scene_evidence: str
    deadline_ms: int
    candidates: tuple[TemporalCandidateSettings, ...]
    control_regions: tuple[tuple[int, int, int, int], ...] = ()
    ambiguity_margin: float = 0.04
    camera_cut_threshold: float = 0.10
    baseline_samples: int = 3
    history_size: int = 6


@dataclass(frozen=True)
class SceneContext:
    scene_id: str
    epoch: int
    entered_at: float


@dataclass(frozen=True)
class FlickerResult:
    scene_id: str
    candidate_id: str
    frame_number: int
    observed_at: float
    flicker_score: float
    spatial_concentration: float
    global_motion_score: float
    phase: FlickerPhase
    stable: bool
    confidence: float


def _number(value: object, path: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TemporalManifestError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise TemporalManifestError(f"{path} is outside its safe range")
    return result


def _region(value: object, path: str, width: int, height: int) -> tuple[int, int, int, int]:
    if not isinstance(value, list) or len(value) != 4:
        raise TemporalManifestError(f"{path} must contain four integers")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise TemporalManifestError(f"{path} must contain four integers")
    x, y, region_width, region_height = value
    if region_width < 1 or region_height < 1:
        raise TemporalManifestError(f"{path} must have positive dimensions")
    if x < 0 or y < 0 or x + region_width > width or y + region_height > height:
        raise TemporalManifestError(f"{path} is outside stream bounds")
    return x, y, region_width, region_height


def _overlap(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def load_temporal_scenes(
    value: object, *, stream_width: int, stream_height: int
) -> tuple[TemporalSceneSettings, ...]:
    """Parse and validate target-local temporal scenes without accepting extras."""

    if not isinstance(value, list):
        raise TemporalManifestError("temporal_scenes must be a list")
    scenes: list[TemporalSceneSettings] = []
    scene_ids: set[str] = set()
    for scene_index, raw_scene in enumerate(value):
        path = f"temporal_scenes[{scene_index}]"
        if not isinstance(raw_scene, Mapping):
            raise TemporalManifestError(f"{path} must be an object")
        allowed = {
            "scene_id", "scene_evidence", "observation_mode", "deadline_ms",
            "candidates", "control_regions", "ambiguity_margin",
            "camera_cut_threshold", "baseline_samples", "history_size",
        }
        if set(raw_scene) - allowed:
            raise TemporalManifestError(f"{path} contains unknown fields")
        scene_id = raw_scene.get("scene_id")
        evidence = raw_scene.get("scene_evidence")
        if not isinstance(scene_id, str) or not scene_id or scene_id in scene_ids:
            raise TemporalManifestError(f"{path}.scene_id is invalid or duplicated")
        if not isinstance(evidence, str) or not evidence:
            raise TemporalManifestError(f"{path}.scene_evidence is required")
        if raw_scene.get("observation_mode") != "urgent":
            raise TemporalManifestError(f"{path} must use urgent observation mode")
        deadline = int(_number(raw_scene.get("deadline_ms"), f"{path}.deadline_ms", 100, 30000))
        raw_candidates = raw_scene.get("candidates")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise TemporalManifestError(f"{path}.candidates must be non-empty")
        candidates: list[TemporalCandidateSettings] = []
        candidate_ids: set[str] = set()
        for candidate_index, raw_candidate in enumerate(raw_candidates):
            candidate_path = f"{path}.candidates[{candidate_index}]"
            if not isinstance(raw_candidate, Mapping):
                raise TemporalManifestError(f"{candidate_path} must be an object")
            candidate_allowed = {
                "candidate_id", "action_kind", "region", "click_point", "detector",
                "high_threshold", "low_threshold", "minimum_high_samples",
                "event_ttl_ms", "feedback", "retry_limit", "priority",
            }
            if set(raw_candidate) - candidate_allowed:
                raise TemporalManifestError(f"{candidate_path} contains unknown fields")
            candidate_id = raw_candidate.get("candidate_id")
            action_kind = raw_candidate.get("action_kind")
            if not isinstance(candidate_id, str) or not candidate_id or candidate_id in candidate_ids:
                raise TemporalManifestError(f"{candidate_path}.candidate_id is invalid")
            if action_kind not in {"click", "wait-for-timeout"}:
                raise TemporalManifestError(f"{candidate_path}.action_kind is unsupported")
            if raw_candidate.get("detector") != "temporal-flicker":
                raise TemporalManifestError(f"{candidate_path}.detector is unsupported")
            region = _region(raw_candidate.get("region"), f"{candidate_path}.region", stream_width, stream_height)
            click = raw_candidate.get("click_point")
            if not isinstance(click, list) or len(click) != 2 or any(
                isinstance(item, bool) or not isinstance(item, int) for item in click
            ):
                raise TemporalManifestError(f"{candidate_path}.click_point is invalid")
            click_point = int(click[0]), int(click[1])
            x, y, region_width, region_height = region
            if not (x <= click_point[0] < x + region_width and y <= click_point[1] < y + region_height):
                raise TemporalManifestError(f"{candidate_path}.click_point is outside its region")
            high = _number(raw_candidate.get("high_threshold", 0.16), f"{candidate_path}.high_threshold", 0.01, 1.0)
            low = _number(raw_candidate.get("low_threshold", 0.07), f"{candidate_path}.low_threshold", 0.0, 0.99)
            if low >= high:
                raise TemporalManifestError(f"{candidate_path} thresholds lack hysteresis")
            feedback = raw_candidate.get("feedback", "candidate-cleared-or-scene-changed")
            if feedback not in {"candidate-cleared-or-scene-changed", "scene-changed", "completion-overlay"}:
                raise TemporalManifestError(f"{candidate_path}.feedback is unsupported")
            candidate = TemporalCandidateSettings(
                candidate_id=candidate_id,
                action_kind=action_kind,
                region=region,
                click_point=click_point,
                high_threshold=high,
                low_threshold=low,
                minimum_high_samples=int(_number(raw_candidate.get("minimum_high_samples", 2), f"{candidate_path}.minimum_high_samples", 1, 5)),
                event_ttl_ms=int(_number(raw_candidate.get("event_ttl_ms", 400), f"{candidate_path}.event_ttl_ms", 100, 2000)),
                feedback=feedback,
                retry_limit=int(_number(raw_candidate.get("retry_limit", 1), f"{candidate_path}.retry_limit", 0, 1)),
                priority=int(_number(raw_candidate.get("priority", 0), f"{candidate_path}.priority", 0, 100)),
            )
            for existing in candidates:
                if _overlap(existing.region, candidate.region) and existing.priority == candidate.priority:
                    raise TemporalManifestError(f"{candidate_path} overlaps without explicit priority")
            candidates.append(candidate)
            candidate_ids.add(candidate_id)
        controls = tuple(
            _region(item, f"{path}.control_regions", stream_width, stream_height)
            for item in raw_scene.get("control_regions", [])
        )
        scenes.append(
            TemporalSceneSettings(
                scene_id=scene_id,
                scene_evidence=evidence,
                deadline_ms=deadline,
                candidates=tuple(candidates),
                control_regions=controls,
                ambiguity_margin=_number(raw_scene.get("ambiguity_margin", 0.04), f"{path}.ambiguity_margin", 0.0, 0.5),
                camera_cut_threshold=_number(raw_scene.get("camera_cut_threshold", 0.10), f"{path}.camera_cut_threshold", 0.01, 1.0),
                baseline_samples=int(_number(raw_scene.get("baseline_samples", 3), f"{path}.baseline_samples", 3, 10)),
                history_size=int(_number(raw_scene.get("history_size", 6), f"{path}.history_size", 3, 12)),
            )
        )
        scene_ids.add(scene_id)
    return tuple(scenes)


@dataclass
class _CandidateState:
    history: deque[np.ndarray]
    phase: FlickerPhase = FlickerPhase.BASELINE
    high_samples: int = 0
    previous_mask: np.ndarray | None = None


class TemporalFlickerDetector:
    """Detect concentrated cross-frame highlights in declared candidate ROIs."""

    def __init__(self) -> None:
        self._epoch = -1
        self._scene_id: str | None = None
        self._states: dict[str, _CandidateState] = {}
        self._global_history: deque[np.ndarray] = deque(maxlen=6)
        self._last_results: dict[str, FlickerResult] = {}

    def reset(self, epoch: int) -> None:
        self._epoch = epoch
        self._scene_id = None
        self._states.clear()
        self._global_history.clear()
        self._last_results.clear()

    @staticmethod
    def _luminance(frame: np.ndarray) -> np.ndarray:
        return (
            frame[:, :, 0].astype(np.float32) * 0.114
            + frame[:, :, 1].astype(np.float32) * 0.587
            + frame[:, :, 2].astype(np.float32) * 0.299
        )

    @classmethod
    def _downsample(cls, frame: np.ndarray, maximum_edge: int = 64) -> np.ndarray:
        luminance = cls._luminance(frame)
        factor = max(1, math.ceil(max(luminance.shape) / maximum_edge))
        height = luminance.shape[0] // factor
        width = luminance.shape[1] // factor
        trimmed = luminance[: height * factor, : width * factor]
        return trimmed.reshape(height, factor, width, factor).mean(axis=(1, 3))

    @classmethod
    def _global_grid(cls, frame: np.ndarray) -> np.ndarray:
        luminance = cls._luminance(frame)
        height, width = luminance.shape
        trimmed = luminance[: height - height % 9, : width - width % 16]
        return trimmed.reshape(9, trimmed.shape[0] // 9, 16, trimmed.shape[1] // 16).mean(axis=(1, 3))

    @staticmethod
    def _concentration(mask: np.ndarray) -> float:
        ys, xs = np.nonzero(mask)
        if not len(xs):
            return 0.0
        box_area = (int(xs.max()) - int(xs.min()) + 1) * (int(ys.max()) - int(ys.min()) + 1)
        return float(len(xs) / max(1, box_area))

    def observe(
        self,
        observation: Observation,
        scene: SceneContext,
        settings: TemporalSceneSettings,
    ) -> tuple[FlickerResult, ...]:
        if observation.data.ndim != 3 or observation.data.shape[2] != 3:
            raise ValueError("temporal detector requires a BGR image")
        if scene.scene_id != settings.scene_id:
            raise ValueError("scene context does not match temporal settings")
        if self._epoch != scene.epoch or self._scene_id != scene.scene_id:
            self.reset(scene.epoch)
            self._scene_id = scene.scene_id
            self._global_history = deque(maxlen=settings.history_size)
        global_grid = self._global_grid(observation.data)
        global_score = 0.0
        if self._global_history:
            global_background = np.median(np.stack(self._global_history), axis=0)
            global_score = float(np.mean(np.abs(global_grid - global_background)) / 255.0)
        if global_score >= settings.camera_cut_threshold:
            self._states.clear()
            self._global_history.clear()
            self._global_history.append(global_grid)
            return tuple(
                FlickerResult(
                    settings.scene_id,
                    candidate.candidate_id,
                    observation.frame_number,
                    observation.observed_at,
                    0.0,
                    0.0,
                    global_score,
                    FlickerPhase.REJECTED,
                    False,
                    0.0,
                )
                for candidate in settings.candidates
            )
        self._global_history.append(global_grid)

        results: list[FlickerResult] = []
        for candidate in settings.candidates:
            state = self._states.setdefault(
                candidate.candidate_id,
                _CandidateState(deque(maxlen=settings.history_size)),
            )
            x, y, width, height = candidate.region
            sample = self._downsample(observation.data[y : y + height, x : x + width])
            if len(state.history) < settings.baseline_samples:
                state.history.append(sample)
                result = FlickerResult(
                    settings.scene_id, candidate.candidate_id, observation.frame_number,
                    observation.observed_at, 0.0, 0.0, global_score,
                    FlickerPhase.BASELINE, False, 0.0,
                )
                results.append(result)
                self._last_results[candidate.candidate_id] = result
                continue
            background = np.median(np.stack(state.history), axis=0)
            difference = np.abs(sample - background)
            changed = difference >= 14.0
            changed_fraction = float(changed.mean())
            mean_change = float(difference.mean() / 255.0)
            concentration = self._concentration(changed)
            score = max(0.0, mean_change + changed_fraction * 0.45 - global_score * 1.5)
            overlap = 1.0
            if state.previous_mask is not None and changed.any():
                union = np.logical_or(state.previous_mask, changed).sum()
                overlap = float(np.logical_and(state.previous_mask, changed).sum() / max(1, union))
            if score >= candidate.high_threshold and concentration >= 0.20:
                state.high_samples = state.high_samples + 1 if overlap >= 0.15 else 1
                state.phase = (
                    FlickerPhase.HIGH
                    if state.high_samples >= candidate.minimum_high_samples
                    else FlickerPhase.RISING
                )
                state.previous_mask = changed
            elif score <= candidate.low_threshold:
                state.phase = FlickerPhase.FALLING if state.high_samples else FlickerPhase.BASELINE
                state.high_samples = 0
                state.previous_mask = None
                state.history.append(sample)
            stable = state.phase is FlickerPhase.HIGH
            confidence = float(
                np.clip(
                    0.45 * min(1.0, score / candidate.high_threshold)
                    + 0.35 * concentration
                    + 0.20 * (1.0 - min(1.0, global_score / settings.camera_cut_threshold)),
                    0.0,
                    1.0,
                )
            ) if stable else 0.0
            result = FlickerResult(
                settings.scene_id, candidate.candidate_id, observation.frame_number,
                observation.observed_at, score, concentration, global_score,
                state.phase, stable, confidence,
            )
            results.append(result)
            self._last_results[candidate.candidate_id] = result

        stable_results = sorted(
            (item for item in results if item.stable),
            key=lambda item: item.flicker_score,
            reverse=True,
        )
        if len(stable_results) >= 2:
            first, second = stable_results[:2]
            first_priority = next(item.priority for item in settings.candidates if item.candidate_id == first.candidate_id)
            second_priority = next(item.priority for item in settings.candidates if item.candidate_id == second.candidate_id)
            if first_priority == second_priority and first.flicker_score - second.flicker_score <= settings.ambiguity_margin:
                ambiguous = {first.candidate_id, second.candidate_id}
                results = [
                    FlickerResult(
                        item.scene_id, item.candidate_id, item.frame_number,
                        item.observed_at, item.flicker_score,
                        item.spatial_concentration, item.global_motion_score,
                        FlickerPhase.REJECTED, False, 0.0,
                    ) if item.candidate_id in ambiguous else item
                    for item in results
                ]
        return tuple(results)

    def detection_for(
        self,
        result: FlickerResult,
        settings: TemporalSceneSettings,
    ) -> Detection | None:
        """Convert only the latest stable unambiguous result into one event draft."""

        if not result.stable or result.phase is not FlickerPhase.HIGH:
            return None
        candidate = next(
            (item for item in settings.candidates if item.candidate_id == result.candidate_id),
            None,
        )
        if candidate is None:
            return None
        return Detection(
            "action-ready",
            settings.scene_id,
            f"temporal-{settings.scene_id}-{candidate.candidate_id}",
            result.confidence,
            (
                ActionCandidate(
                    candidate.candidate_id,
                    candidate.action_kind,
                    candidate.click_point[0],
                    candidate.click_point[1],
                ),
            ),
            expiry_seconds=candidate.event_ttl_ms / 1000.0,
        )


class TemporalPerceptionAdapter:
    """Route verified scene context into the detector without per-frame OCR."""

    def __init__(
        self,
        scenes: tuple[TemporalSceneSettings, ...],
        scene_router: Callable[[Observation], str | None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.scenes = {scene.scene_id: scene for scene in scenes}
        self.scene_router = scene_router
        self.clock = clock
        self.detector = TemporalFlickerDetector()
        self._epoch = 0
        self._active_scene_id: str | None = None
        self._entered_at = 0.0
        self._completed_epoch = -1
        self._timeout_emitted_epoch = -1

    def reset(self) -> None:
        self._epoch += 1
        self._active_scene_id = None
        self._completed_epoch = -1
        self._timeout_emitted_epoch = -1
        self.detector.reset(self._epoch)

    def _route(self, observation: Observation) -> TemporalSceneSettings | None:
        scene_id = self.scene_router(observation)
        if scene_id is None:
            if self._active_scene_id is not None:
                self._epoch += 1
                self.detector.reset(self._epoch)
            self._active_scene_id = None
            return None
        settings = self.scenes.get(scene_id)
        if settings is None:
            return None
        if scene_id != self._active_scene_id:
            self._epoch += 1
            self._active_scene_id = scene_id
            self._entered_at = observation.observed_at
            self._completed_epoch = -1
            self._timeout_emitted_epoch = -1
            self.detector.reset(self._epoch)
        return settings

    def preferred_mode(self, observation: Observation) -> ModeRequest:
        settings = self._route(observation)
        if settings is None or self._completed_epoch == self._epoch:
            return ModeRequest(ObservationMode.VIDEO)
        elapsed = max(0.0, observation.observed_at - self._entered_at)
        remaining = settings.deadline_ms / 1000.0 - elapsed
        if remaining <= 0:
            return ModeRequest(ObservationMode.INTERACTIVE)
        return ModeRequest(ObservationMode.URGENT, urgent_seconds=remaining)

    def detect(self, observation: Observation) -> tuple[Detection, ...]:
        settings = self._route(observation)
        if settings is None or self._completed_epoch == self._epoch:
            return ()
        if observation.observed_at - self._entered_at >= settings.deadline_ms / 1000.0:
            if self._timeout_emitted_epoch == self._epoch:
                return ()
            self._timeout_emitted_epoch = self._epoch
            self._completed_epoch = self._epoch
            return (
                Detection(
                    "temporal-timeout",
                    settings.scene_id,
                    f"temporal-timeout-{settings.scene_id}-{self._epoch}",
                    0.0,
                    expiry_seconds=1.0,
                ),
            )
        context = SceneContext(
            settings.scene_id, self._epoch, self._entered_at
        )
        results = self.detector.observe(observation, context, settings)
        stable = [item for item in results if item.stable]
        if len(stable) > 1 or any(
            item.phase is FlickerPhase.REJECTED and item.flicker_score > 0
            for item in results
        ):
            return (
                Detection(
                    "ambiguous-flicker",
                    settings.scene_id,
                    f"temporal-ambiguous-{settings.scene_id}-{self._epoch}",
                    0.0,
                    expiry_seconds=0.5,
                ),
            )
        if len(stable) != 1:
            return ()
        detection = self.detector.detection_for(stable[0], settings)
        if detection is None:
            return ()
        self._completed_epoch = self._epoch
        return (detection,)
