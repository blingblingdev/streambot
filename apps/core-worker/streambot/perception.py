"""Composable visual perception over in-memory BGR frames."""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from typing import Mapping, Protocol

import numpy as np

from .config import PerceptionSettings, PredicateSettings, RegionSettings


class PerceptionError(RuntimeError):
    """Raised when perception cannot safely evaluate its configuration."""


class OcrAdapter(Protocol):
    """Optional OCR adapter contract."""

    def recognize(self, image: np.ndarray) -> str:
        """Return recognized text for one in-memory region."""


class TemplateMatcher(Protocol):
    """Template matching adapter contract."""

    def score(self, image: np.ndarray, template: np.ndarray) -> float:
        """Return a normalized similarity score between zero and one."""


class AlignedNumpyTemplateMatcher:
    """Efficient deterministic matcher for a template-aligned region."""

    def score(self, image: np.ndarray, template: np.ndarray) -> float:
        if image.shape != template.shape:
            raise PerceptionError("aligned template shape does not match region")
        if image.dtype != np.uint8 or template.dtype != np.uint8:
            raise PerceptionError("template inputs must use uint8 pixels")
        difference = np.abs(image.astype(np.int16) - template.astype(np.int16))
        return float(1.0 - difference.mean() / 255.0)


class OpenCvTemplateMatcher:
    """Optional OpenCV adapter that searches a template within a region."""

    def __init__(self) -> None:
        try:
            self._cv2 = importlib.import_module("cv2")
        except ImportError as error:
            raise PerceptionError("OpenCV template matching is unavailable") from error

    def score(self, image: np.ndarray, template: np.ndarray) -> float:
        if image.dtype != np.uint8 or template.dtype != np.uint8:
            raise PerceptionError("template inputs must use uint8 pixels")
        if (
            image.ndim != template.ndim
            or image.shape[2:] != template.shape[2:]
            or any(
            template.shape[index] > image.shape[index] for index in range(2)
            )
        ):
            raise PerceptionError("template does not fit within region")
        result = self._cv2.matchTemplate(image, template, self._cv2.TM_SQDIFF_NORMED)
        _minimum, maximum, _minimum_location, _maximum_location = self._cv2.minMaxLoc(
            result
        )
        del maximum
        return float(1.0 - _minimum)


@dataclass(frozen=True)
class PerceptionResult:
    """Metadata-only visual predicate and signal outcomes."""

    predicates: Mapping[str, bool]
    signals: Mapping[str, bool]
    scores: Mapping[str, float]


class PerceptionEngine:
    """Evaluate configured predicates without retaining the source frame."""

    def __init__(
        self,
        settings: PerceptionSettings,
        *,
        templates: Mapping[str, np.ndarray] | None = None,
        matcher: TemplateMatcher | None = None,
        ocr: OcrAdapter | None = None,
    ) -> None:
        self._settings = settings
        self._templates = dict(templates or {})
        self._matcher = matcher or AlignedNumpyTemplateMatcher()
        self._ocr = ocr

    @staticmethod
    def _extract(frame: np.ndarray, region: RegionSettings) -> np.ndarray:
        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise PerceptionError("frame must be a uint8 BGR image")
        right = region.x + region.width
        bottom = region.y + region.height
        if right > frame.shape[1] or bottom > frame.shape[0]:
            raise PerceptionError(f"region {region.name} exceeds frame bounds")
        return frame[region.y:bottom, region.x:right]

    @staticmethod
    def _matches_color(
        pixels: np.ndarray, bgr: tuple[int, int, int], tolerance: int
    ) -> np.ndarray:
        target = np.asarray(bgr, dtype=np.int16)
        difference = np.abs(pixels.astype(np.int16) - target)
        return np.all(difference <= tolerance, axis=-1)

    def _evaluate_predicate(
        self, predicate: PredicateSettings, image: np.ndarray
    ) -> tuple[bool, float | None]:
        if predicate.kind == "pixel":
            if predicate.x is None or predicate.y is None or predicate.bgr is None:
                raise PerceptionError(f"pixel predicate {predicate.name} is incomplete")
            if predicate.x >= image.shape[1] or predicate.y >= image.shape[0]:
                raise PerceptionError(f"pixel predicate {predicate.name} is out of bounds")
            matched = self._matches_color(
                image[predicate.y, predicate.x][None, :],
                predicate.bgr,
                predicate.tolerance,
            )
            return bool(matched[0]), None
        if predicate.kind == "color":
            if predicate.bgr is None or predicate.minimum_fraction is None:
                raise PerceptionError(f"color predicate {predicate.name} is incomplete")
            fraction = float(
                self._matches_color(image, predicate.bgr, predicate.tolerance).mean()
            )
            return fraction >= predicate.minimum_fraction, fraction
        if predicate.kind == "template":
            if predicate.template is None or predicate.threshold is None:
                raise PerceptionError(
                    f"template predicate {predicate.name} is incomplete"
                )
            template = self._templates.get(predicate.template)
            if template is None:
                raise PerceptionError(f"template {predicate.template} is unavailable")
            score = self._matcher.score(image, template)
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise PerceptionError("template matcher returned an invalid score")
            return score >= predicate.threshold, score
        if predicate.kind == "ocr":
            if predicate.contains is None:
                raise PerceptionError(f"OCR predicate {predicate.name} is incomplete")
            if self._ocr is None:
                raise PerceptionError("OCR adapter is unavailable")
            text = self._ocr.recognize(image)
            if not isinstance(text, str):
                raise PerceptionError("OCR adapter returned an invalid result")
            expected = predicate.contains
            if not predicate.case_sensitive:
                text = text.casefold()
                expected = expected.casefold()
            return expected in text, None
        raise PerceptionError(f"predicate {predicate.name} has an unsupported type")

    def evaluate(self, frame: np.ndarray) -> PerceptionResult:
        """Evaluate one frame and return no image or recognized text."""

        regions = {
            region.name: self._extract(frame, region)
            for region in self._settings.regions
        }
        predicate_results: dict[str, bool] = {}
        scores: dict[str, float] = {}
        for predicate in self._settings.predicates:
            result, score = self._evaluate_predicate(
                predicate, regions[predicate.region]
            )
            predicate_results[predicate.name] = result
            if score is not None:
                scores[predicate.name] = score

        signal_results: dict[str, bool] = {}
        for signal in self._settings.signals:
            values = [predicate_results[name] for name in signal.predicates]
            if signal.operator == "all":
                signal_results[signal.name] = all(values)
            elif signal.operator == "any":
                signal_results[signal.name] = any(values)
            else:
                signal_results[signal.name] = not values[0]
        return PerceptionResult(
            predicates=predicate_results,
            signals=signal_results,
            scores=scores,
        )
