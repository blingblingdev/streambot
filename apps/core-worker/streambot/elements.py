"""Declarative element location over in-memory BGR frames.

This is the *locator*: given a frame and a declaration, decide which screen is
showing and return the on-screen instances of named elements — origin, centre
and score — so a caller can act on them. It is deliberately distinct from the
two vision surfaces the package already has:

- ``perception.py`` answers yes/no questions about fixed named regions
  (predicates and signals). It never says *where* something is.
- ``scene.py`` (``_template_grid``) searches a template within a manifest-
  declared region as part of the scene-fact engine.

Both score with ``TM_SQDIFF_NORMED``. This module uses ``TM_CCOEFF_NORMED``
plus a colour gate instead, because that combination was measured against the
real targets and the alternative was not:

- Controls pulse and glow. Ccoeff is brightness tolerant, sqdiff is not: one
  ready diamond scored 0.848 ccoeff against 0.745 sqdiff while its twin, the
  same control at a different point in the same animation, scored 1.0. A
  threshold that accepts both under sqdiff also accepts the wrong control.
- Ccoeff normalises brightness away, so a gold button and its blue twin can
  score alike (就位 0.848 vs 放弃 0.827). The colour gate puts that
  discrimination back: a match must also sit within a tolerance of the
  template's own interior mean(R-B)/mean(G-B). The signature comes from the
  template itself, so no element needs hand-tuned colour bounds.

An element may instead declare ``glyph: true``, which binarizes both the
template and the frame (``min(B,G,R) >= white_min``) before scoring. That is
for controls drawn as a white icon over whatever background the current scene
happens to put behind them: a raw BGR crop of such an icon is bound to the
background it was recorded on (one measured 1.0 on its source background and
0.65 on another), while the binary form scores on shape alone. Colour gating
is skipped on that path — binarization has already discarded colour.

Declarations are data, never code: a job ships a JSON file plus a directory of
``.npy`` templates, and this module validates both before the first match, so
a malformed declaration fails at load rather than mid-run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .config import (
    ConfigurationError,
    _integer,
    _mapping,
    _number,
    _strict_keys,
)

# Registration caps. These bound what a declaration can ask a long-lived
# process to hold and to search; they are generous for real control art and
# small enough that a malformed declaration cannot exhaust the host.
MAX_TEMPLATE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_TEMPLATE_BYTES = 32 * 1024 * 1024
MAX_ELEMENTS = 256
MAX_SCREENS = 64
MAX_ANCHORS_PER_SCREEN = 8

# Provenance keys the resolver ignores but a declaration may carry.
#
# What a template is, where it was recorded, what it was measured against and
# why a threshold is what it is: that belongs beside the data, not in a second
# file that drifts from it. They are named explicitly rather than allowing a
# free-form bag, so a typo in a real key ("treshold") is still an error.
DOC_KEYS_TOP = {"target", "notes", "resolution_policy", "frame_size"}
DOC_KEYS_ELEMENT = {
    "id",
    "notes",
    "screen_note",
    "template_note",
    "ocr_confirm",
    "calibration_reference",
    "isolation",
    "recorded_on",
    "source_frame",
}
DOC_KEYS_SCREEN = {"note", "screen_note", "notes"}

DEFAULT_THRESHOLD = 0.85
DEFAULT_CLASSIFY_THRESHOLD = 0.85
DEFAULT_CLASSIFY_LEAD = 0.10
DEFAULT_COLOR_TOLERANCE = 50.0
DEFAULT_WHITE_MIN = 190
DEFAULT_FALLBACK_COOLDOWN_SECONDS = 20.0
BAND_MARGIN = 8


def _name(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{path} must be a non-empty string")
    return value


def _band(value: object, path: str) -> tuple[int, int]:
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 2:
        raise ConfigurationError(f"{path} must be a [y0, y1] pair")
    y0 = _integer(value[0], f"{path}[0]", 0, 100_000)
    y1 = _integer(value[1], f"{path}[1]", 0, 100_000)
    if y1 <= y0:
        raise ConfigurationError(f"{path} must be increasing")
    return y0, y1


def _x_range(value: object, path: str) -> tuple[int, int]:
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 2:
        raise ConfigurationError(f"{path} must be an [x0, x1] pair")
    x0 = _integer(value[0], f"{path}[0]", 0, 100_000)
    x1 = _integer(value[1], f"{path}[1]", 0, 100_000)
    if x1 <= x0:
        raise ConfigurationError(f"{path} must be increasing")
    return x0, x1


@dataclass(frozen=True)
class MatchSettings:
    """Thresholds shared by every element in one declaration."""

    threshold: float = DEFAULT_THRESHOLD
    classify_threshold: float = DEFAULT_CLASSIFY_THRESHOLD
    classify_lead: float = DEFAULT_CLASSIFY_LEAD
    color_tolerance: float = DEFAULT_COLOR_TOLERANCE
    white_min: int = DEFAULT_WHITE_MIN
    fallback_cooldown_seconds: float = DEFAULT_FALLBACK_COOLDOWN_SECONDS

    @classmethod
    def from_mapping(cls, data: object, path: str) -> "MatchSettings":
        mapping = _mapping(data, path)
        _strict_keys(
            mapping,
            required=set(),
            optional={
                "threshold",
                "classify_threshold",
                "classify_lead",
                "color_tolerance",
                "white_min",
                "fallback_cooldown_seconds",
            },
            path=path,
        )
        return cls(
            threshold=_number(
                mapping.get("threshold", DEFAULT_THRESHOLD),
                f"{path}.threshold", 0.0, 1.0,
            ),
            classify_threshold=_number(
                mapping.get("classify_threshold", DEFAULT_CLASSIFY_THRESHOLD),
                f"{path}.classify_threshold", 0.0, 1.0,
            ),
            classify_lead=_number(
                mapping.get("classify_lead", DEFAULT_CLASSIFY_LEAD),
                f"{path}.classify_lead", 0.0, 1.0,
            ),
            color_tolerance=_number(
                mapping.get("color_tolerance", DEFAULT_COLOR_TOLERANCE),
                f"{path}.color_tolerance", 0.0, 255.0,
            ),
            white_min=_integer(
                mapping.get("white_min", DEFAULT_WHITE_MIN),
                f"{path}.white_min", 0, 255,
            ),
            fallback_cooldown_seconds=_number(
                mapping.get(
                    "fallback_cooldown_seconds", DEFAULT_FALLBACK_COOLDOWN_SECONDS
                ),
                f"{path}.fallback_cooldown_seconds", 0.0, 3600.0,
            ),
        )


@dataclass(frozen=True)
class ScreenAnchor:
    """One template whose presence in a band identifies a screen."""

    template: str
    y_band: tuple[int, int]
    min_score: float

    @classmethod
    def from_mapping(
        cls, data: object, path: str, settings: MatchSettings
    ) -> "ScreenAnchor":
        mapping = _mapping(data, path)
        _strict_keys(
            mapping,
            required={"template", "y_band"},
            optional={"min_score"},
            path=path,
        )
        return cls(
            template=_name(mapping["template"], f"{path}.template"),
            y_band=_band(mapping["y_band"], f"{path}.y_band"),
            min_score=_number(
                mapping.get("min_score", settings.classify_threshold),
                f"{path}.min_score", 0.0, 1.0,
            ),
        )


@dataclass(frozen=True)
class ElementSpec:
    """One locatable control."""

    element_id: str
    template: str
    screen: str
    y_band: tuple[int, int]
    threshold: float
    expected: int
    clickable: bool = True
    glyph: bool = False
    min_rb: float | None = None

    @classmethod
    def from_mapping(
        cls, element_id: str, data: object, path: str, settings: MatchSettings
    ) -> "ElementSpec":
        mapping = _mapping(data, path)
        _strict_keys(
            mapping,
            required={"template", "screen", "y_band"},
            optional={"threshold", "expected", "clickable", "glyph", "min_rb"}
            | DOC_KEYS_ELEMENT,
            path=path,
        )
        min_rb = mapping.get("min_rb")
        return cls(
            element_id=element_id,
            template=_name(mapping["template"], f"{path}.template"),
            screen=_name(mapping["screen"], f"{path}.screen"),
            y_band=_band(mapping["y_band"], f"{path}.y_band"),
            threshold=_number(
                mapping.get("threshold", settings.threshold),
                f"{path}.threshold", 0.0, 1.0,
            ),
            expected=_integer(
                mapping.get("expected", 1), f"{path}.expected", 0, 64
            ),
            clickable=bool(mapping.get("clickable", True)),
            glyph=bool(mapping.get("glyph", False)),
            min_rb=None if min_rb is None else _number(
                min_rb, f"{path}.min_rb", -255.0, 255.0
            ),
        )


@dataclass(frozen=True)
class RegionSpec:
    """A horizontal slice of the frame classified independently.

    Most targets are one application filling the frame and declare no regions
    at all, which yields the single implicit ``frame`` region. A target that
    shows two independent clients side by side declares one region per client:
    they are separate state machines, and a single whole-frame classification
    returns nothing whenever they disagree during a transition.
    """

    name: str
    x_range: tuple[int, int]

    @classmethod
    def from_mapping(cls, name: str, data: object, path: str) -> "RegionSpec":
        mapping = _mapping(data, path)
        _strict_keys(mapping, required={"x_range"}, optional=set(), path=path)
        return cls(name=name, x_range=_x_range(mapping["x_range"], f"{path}.x_range"))


@dataclass(frozen=True)
class Instance:
    """One located element instance. Metadata only — never pixels."""

    element: str
    origin: tuple[int, int]
    center: tuple[int, int]
    score: float
    region: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "element": self.element,
            "origin": list(self.origin),
            "center": list(self.center),
            "score": round(self.score, 3),
            "region": self.region,
        }


@dataclass(frozen=True)
class Analysis:
    """The outcome of one analyze call, including its own cost."""

    screens: dict[str, str | None]
    instances: list[Instance]
    classify_ms: float
    resolve_ms: float

    @property
    def screen(self) -> str | None:
        """The single screen every region agrees on, else None."""

        values = set(self.screens.values())
        return next(iter(values)) if len(values) == 1 else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "screen": self.screen,
            "screens": dict(self.screens),
            "instances": [instance.as_dict() for instance in self.instances],
            "classify_ms": round(self.classify_ms, 1),
            "resolve_ms": round(self.resolve_ms, 1),
        }


@dataclass(frozen=True)
class ElementDeclaration:
    """A validated element declaration with its templates loaded."""

    settings: MatchSettings
    regions: tuple[RegionSpec, ...]
    screens: dict[str, tuple[ScreenAnchor, ...]]
    elements: dict[str, ElementSpec]
    templates: dict[str, np.ndarray] = field(repr=False)

    @property
    def template_bytes(self) -> int:
        return sum(int(template.nbytes) for template in self.templates.values())

    def summary(self) -> dict[str, Any]:
        """Metadata-only description, safe for logs and IPC responses."""

        return {
            "regions": [region.name for region in self.regions],
            "screens": sorted(self.screens),
            "elements": sorted(self.elements),
            "templates": len(self.templates),
            "template_bytes": self.template_bytes,
        }


def _load_template(path: Path, name: str) -> np.ndarray:
    if not path.is_file():
        raise ConfigurationError(f"template {name} is missing at {path.name}")
    try:
        template = np.load(path, allow_pickle=False)
    except Exception as error:  # unreadable, truncated, or pickled payload
        raise ConfigurationError(
            f"template {name} could not be read: {type(error).__name__}"
        ) from error
    if template.dtype != np.uint8:
        raise ConfigurationError(f"template {name} must use uint8 pixels")
    if template.ndim == 3:
        if template.shape[2] != 3:
            raise ConfigurationError(f"template {name} must have three channels")
    elif template.ndim != 2:
        raise ConfigurationError(f"template {name} must be a 2D mask or a BGR image")
    if min(template.shape[:2]) < 2:
        raise ConfigurationError(f"template {name} is too small to match")
    if template.nbytes > MAX_TEMPLATE_BYTES:
        raise ConfigurationError(f"template {name} exceeds the size cap")
    return template


def load_declaration(
    path: str | Path, assets_dir: str | Path | None = None
) -> ElementDeclaration:
    """Load and validate a declaration and every template it names.

    ``assets_dir`` defaults to an ``assets`` directory beside the declaration,
    which is the layout every recorded target already uses.
    """

    import json

    declaration_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(declaration_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(
            f"declaration is unreadable: {type(error).__name__}"
        ) from error
    except ValueError as error:
        raise ConfigurationError(f"declaration is not valid JSON: {error}") from error

    data = _mapping(raw, "declaration")
    _strict_keys(
        data,
        required={"elements", "screens"},
        optional={"schema_version", "match", "regions"} | DOC_KEYS_TOP,
        path="declaration",
    )
    settings = MatchSettings.from_mapping(data.get("match", {}), "declaration.match")

    region_data = _mapping(data.get("regions", {}), "declaration.regions")
    if region_data:
        regions = tuple(
            RegionSpec.from_mapping(name, value, f"declaration.regions.{name}")
            for name, value in sorted(region_data.items())
        )
    else:
        # One application filling the frame: the implicit whole-frame region.
        regions = (RegionSpec(name="frame", x_range=(0, 0)),)

    screen_data = _mapping(data["screens"], "declaration.screens")
    if not screen_data:
        raise ConfigurationError("declaration.screens must declare a screen")
    if len(screen_data) > MAX_SCREENS:
        raise ConfigurationError("declaration.screens exceeds the screen cap")
    screens: dict[str, tuple[ScreenAnchor, ...]] = {}
    for screen, value in screen_data.items():
        screen_path = f"declaration.screens.{screen}"
        mapping = _mapping(value, screen_path)
        _strict_keys(
            mapping, required={"anchors"}, optional=DOC_KEYS_SCREEN, path=screen_path
        )
        anchors = mapping["anchors"]
        if not isinstance(anchors, Sequence) or isinstance(anchors, str) or not anchors:
            raise ConfigurationError(f"{screen_path}.anchors must be a non-empty list")
        if len(anchors) > MAX_ANCHORS_PER_SCREEN:
            raise ConfigurationError(f"{screen_path}.anchors exceeds the anchor cap")
        screens[_name(screen, screen_path)] = tuple(
            ScreenAnchor.from_mapping(anchor, f"{screen_path}.anchors[{index}]", settings)
            for index, anchor in enumerate(anchors)
        )

    raw_elements = data["elements"]
    if isinstance(raw_elements, Sequence) and not isinstance(raw_elements, str):
        # A recorded target lists its elements in the order they were found,
        # each carrying its own id. Accept that shape as well as a mapping.
        element_data = {}
        for index, entry in enumerate(raw_elements):
            entry_map = _mapping(entry, f"declaration.elements[{index}]")
            element_id = entry_map.get("id")
            if not isinstance(element_id, str) or not element_id:
                raise ConfigurationError(
                    f"declaration.elements[{index}].id must be a non-empty string"
                )
            if element_id in element_data:
                raise ConfigurationError(f"duplicate element: {element_id}")
            element_data[element_id] = entry_map
    else:
        element_data = _mapping(raw_elements, "declaration.elements")
    if not element_data:
        raise ConfigurationError("declaration.elements must declare an element")
    if len(element_data) > MAX_ELEMENTS:
        raise ConfigurationError("declaration.elements exceeds the element cap")
    elements: dict[str, ElementSpec] = {}
    for element_id, value in element_data.items():
        element_path = f"declaration.elements.{element_id}"
        spec = ElementSpec.from_mapping(
            _name(element_id, element_path), value, element_path, settings
        )
        if spec.screen not in screens:
            raise ConfigurationError(
                f"{element_path}.screen is not a declared screen: {spec.screen}"
            )
        elements[spec.element_id] = spec

    assets = (
        Path(assets_dir).expanduser().resolve()
        if assets_dir is not None
        else declaration_path.parent / "assets"
    )
    wanted = {spec.template for spec in elements.values()}
    wanted |= {
        anchor.template for anchors in screens.values() for anchor in anchors
    }
    templates: dict[str, np.ndarray] = {}
    total = 0
    for template_name in sorted(wanted):
        template = _load_template(assets / f"{template_name}.npy", template_name)
        total += int(template.nbytes)
        if total > MAX_TOTAL_TEMPLATE_BYTES:
            raise ConfigurationError("declaration exceeds the total template cap")
        templates[template_name] = template

    # A glyph element needs a 2D mask and a BGR element needs a colour crop:
    # catch the mismatch here rather than at the first match.
    for spec in elements.values():
        template = templates[spec.template]
        if spec.glyph and template.ndim != 2:
            raise ConfigurationError(
                f"declaration.elements.{spec.element_id} is glyph but its template is not a mask"
            )
        if not spec.glyph and template.ndim != 3:
            raise ConfigurationError(
                f"declaration.elements.{spec.element_id} needs a BGR template"
            )

    return ElementDeclaration(
        settings=settings,
        regions=regions,
        screens=screens,
        elements=elements,
        templates=templates,
    )


class ElementResolver:
    """Locate declared elements on frames. Fail closed, never guess.

    One resolver per declaration. Instances are cheap to hold and keep the
    band-fallback cooldown, so a long-lived process should keep one rather
    than rebuild it per frame.
    """

    def __init__(self, declaration: ElementDeclaration) -> None:
        self._declaration = declaration
        self._fallback_failed_at: dict[str, float] = {}

    @property
    def declaration(self) -> ElementDeclaration:
        return self._declaration

    # -------------------------------------------------------------- matching

    def _binarize(self, image: np.ndarray) -> np.ndarray:
        white_min = self._declaration.settings.white_min
        return (image.min(axis=2) >= white_min).astype(np.uint8) * 255

    def _match(self, frame: np.ndarray, template: np.ndarray) -> np.ndarray:
        import cv2

        if template.ndim == 2:  # glyph path: binarize the frame side too
            frame = self._binarize(frame)
        if (
            frame.shape[0] < template.shape[0]
            or frame.shape[1] < template.shape[1]
        ):
            return np.zeros((1, 1), dtype=np.float32)
        return np.nan_to_num(cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED))

    @staticmethod
    def _interior(image: np.ndarray) -> np.ndarray:
        h, w = image.shape[:2]
        return image[h // 5 : h - h // 5, w // 5 : w - w // 5]

    @classmethod
    def _color_signature(cls, image: np.ndarray) -> tuple[float, float]:
        box = cls._interior(image).astype(np.int16)
        b, g, r = box[:, :, 0], box[:, :, 1], box[:, :, 2]
        return float((r - b).mean()), float((g - b).mean())

    def _instances(
        self, frame: np.ndarray, template: np.ndarray, spec: ElementSpec
    ) -> list[tuple[float, int, int]]:
        """Return surviving (score, x, y) origins, non-max suppressed."""

        scores = self._match(frame, template)
        th, tw = template.shape[:2]
        binary = template.ndim == 2
        candidates = [
            (float(scores[y, x]), int(x), int(y))
            for y, x in zip(*np.nonzero(scores >= spec.threshold))
        ]
        candidates.sort(reverse=True)
        tolerance = self._declaration.settings.color_tolerance
        ref_rb, ref_gb = (
            (0.0, 0.0) if binary else self._color_signature(template)
        )
        kept: list[tuple[float, int, int]] = []
        for score, x, y in candidates:
            if any(abs(x - kx) < tw and abs(y - ky) < th for _s, kx, ky in kept):
                continue
            if not binary:
                # Ccoeff normalised brightness away; put the colour
                # discrimination back so a shape-alike in the wrong colour
                # (a disabled twin, a different action) cannot pass.
                rb, gb = self._color_signature(frame[y : y + th, x : x + tw])
                if abs(rb - ref_rb) > tolerance or abs(gb - ref_gb) > tolerance:
                    continue
                if spec.min_rb is not None and rb < spec.min_rb:
                    # An actionable-only lower bound: a control that has
                    # already been used desaturates but keeps its shape, and
                    # clicking it again does nothing.
                    continue
            kept.append((score, x, y))
        return kept

    # ---------------------------------------------------------- classification

    def _region_slice(self, frame: np.ndarray, region: RegionSpec) -> tuple[int, int]:
        x0, x1 = region.x_range
        if x1 <= x0:  # the implicit whole-frame region
            return 0, frame.shape[1]
        return max(0, x0), min(frame.shape[1], x1)

    def _screen_scores(
        self, frame: np.ndarray, x0: int, x1: int, full_height: bool
    ) -> tuple[dict[str, float], dict[str, float]]:
        scores: dict[str, float] = {}
        thresholds: dict[str, float] = {}
        for screen, anchors in self._declaration.screens.items():
            best = 0.0
            thresholds[screen] = min(anchor.min_score for anchor in anchors)
            for anchor in anchors:
                template = self._declaration.templates[anchor.template]
                if full_height:
                    band = frame[:, x0:x1]
                else:
                    y0, y1 = anchor.y_band
                    band = frame[max(0, y0) : y1 + template.shape[0], x0:x1]
                if (
                    band.shape[0] < template.shape[0]
                    or band.shape[1] < template.shape[1]
                ):
                    continue
                best = max(best, float(self._match(band, template).max()))
            scores[screen] = round(best, 3)
        return scores, thresholds

    def _decide(
        self, scores: dict[str, float], thresholds: dict[str, float]
    ) -> str | None:
        ordered = sorted(scores.items(), key=lambda item: -item[1])
        top_screen, top_score = ordered[0]
        lead = self._declaration.settings.classify_lead
        if top_score >= thresholds[top_screen] and (
            len(ordered) == 1 or top_score - ordered[1][1] >= lead
        ):
            return top_screen
        return None

    def _classify_region(self, frame: np.ndarray, region: RegionSpec) -> str | None:
        x0, x1 = self._region_slice(frame, region)
        scores, thresholds = self._screen_scores(frame, x0, x1, full_height=False)
        decided = self._decide(scores, thresholds)
        if decided is not None:
            return decided
        # Any-position fallback: anchors are searched in their recorded band
        # for speed. If none classify, re-search the full height so a shifted
        # layout is still recognised — a slow path, so a fallback that finds
        # nothing starts a cooldown. Whole phases of a run legitimately show
        # no known screen at all, and paying for a full-height sweep on every
        # poll of those would dominate the loop's latency.
        now = time.monotonic()
        cooldown = self._declaration.settings.fallback_cooldown_seconds
        failed_at = self._fallback_failed_at.get(region.name)
        if failed_at is None or now - failed_at >= cooldown:
            full_scores, full_thresholds = self._screen_scores(
                frame, x0, x1, full_height=True
            )
            decided = self._decide(full_scores, full_thresholds)
            if decided is not None:
                self._fallback_failed_at.pop(region.name, None)
                return decided
            self._fallback_failed_at[region.name] = now
        return None

    def classify(self, frame: np.ndarray) -> dict[str, str | None]:
        """Classify every declared region: ``{region: screen or None}``."""

        _validate_frame(frame)
        return {
            region.name: self._classify_region(frame, region)
            for region in self._declaration.regions
        }

    # --------------------------------------------------------------- resolving

    def resolve(
        self,
        frame: np.ndarray,
        element_id: str,
        screens: Mapping[str, str | None] | None = None,
    ) -> list[Instance]:
        """Resolve one element's live instances; ``[]`` when off-screen.

        An instance is returned only for a region whose own screen is the
        element's home screen, so a control still showing in one region
        resolves even when another region has already moved on.
        """

        _validate_frame(frame)
        try:
            spec = self._declaration.elements[element_id]
        except KeyError:
            raise ConfigurationError(f"unknown element: {element_id}") from None
        if screens is None:
            screens = self.classify(frame)
        template = self._declaration.templates[spec.template]
        active = [
            region
            for region in self._declaration.regions
            if screens.get(region.name) == spec.screen
        ]
        if not active:
            return []

        th, tw = template.shape[:2]
        y0, y1 = spec.y_band
        band_top = max(0, y0 - BAND_MARGIN)
        band_bottom = y1 + th + BAND_MARGIN
        results: list[Instance] = []
        for region in active:
            rx0, rx1 = self._region_slice(frame, region)
            found = self._instances(frame[band_top:band_bottom, rx0:rx1], template, spec)
            offset = band_top
            if not found:
                # The recorded band is a fast-path guess; if the control is
                # not there, search the region's full height before giving up.
                found = self._instances(frame[:, rx0:rx1], template, spec)
                offset = 0
            for score, x, y in found:
                origin = (x + rx0, y + offset)
                results.append(
                    Instance(
                        element=element_id,
                        origin=origin,
                        center=(origin[0] + tw // 2, origin[1] + th // 2),
                        score=score,
                        region=region.name,
                    )
                )
        return results

    def analyze(
        self, frame: np.ndarray, elements: Iterable[str] | None = None
    ) -> Analysis:
        """Classify once, then resolve the requested elements, with timings.

        Elements whose home screen is not showing are skipped rather than
        searched — the screen gate is what makes a cross-screen false positive
        structurally impossible instead of threshold-lucky, and skipping is
        also what keeps a poll cheap.
        """

        _validate_frame(frame)
        started = time.perf_counter()
        screens = self.classify(frame)
        classified_at = time.perf_counter()

        if elements is None:
            wanted = list(self._declaration.elements)
        else:
            wanted = list(elements)
            unknown = [name for name in wanted if name not in self._declaration.elements]
            if unknown:
                raise ConfigurationError(f"unknown element: {unknown[0]}")

        showing = {screen for screen in screens.values() if screen is not None}
        instances: list[Instance] = []
        for element_id in wanted:
            if self._declaration.elements[element_id].screen not in showing:
                continue
            instances.extend(self.resolve(frame, element_id, screens=screens))
        finished = time.perf_counter()
        return Analysis(
            screens=screens,
            instances=instances,
            classify_ms=(classified_at - started) * 1000.0,
            resolve_ms=(finished - classified_at) * 1000.0,
        )


def _validate_frame(frame: np.ndarray) -> None:
    if not isinstance(frame, np.ndarray):
        raise ConfigurationError("frame must be a numpy array")
    if frame.dtype != np.uint8:
        raise ConfigurationError("frame must use uint8 pixels")
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ConfigurationError("frame must be a BGR image")


__all__ = [
    "Analysis",
    "ElementDeclaration",
    "ElementResolver",
    "ElementSpec",
    "Instance",
    "MatchSettings",
    "RegionSpec",
    "ScreenAnchor",
    "load_declaration",
]
