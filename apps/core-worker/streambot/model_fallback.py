"""Bounded model fallback for genuinely novel scenes.

Deterministic first: the scene engine classifies every known scene from data.
Only when classification fails repeatedly may an optional, explicitly enabled
resolver be consulted with the current frame. Every successful resolution is a
scene-manifest fragment that is written to a persistent cache and added to the
running engine, so the model is asked at most once per novel scene — a research
accelerator, not a runtime dependency.

Safety bounds: the fallback is off unless a resolver is injected AND `enabled`
is true; consultations are rate-limited; the frame leaves the process only
through the injected resolver, never through this module's own I/O; cached
fragments are validated by the same executed schema as authored scenes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import numpy as np

from .scene import SceneEngine, SceneFacts, SceneManifestError


class SceneResolver(Protocol):
    """Contract for a vision-capable resolver of unknown frames."""

    def resolve(self, frame: np.ndarray) -> Mapping[str, Any] | None:
        """Return {"scene_id": str, "scene": <manifest fragment>} or None."""


@dataclass
class FallbackMetrics:
    consultations: int = 0
    resolutions: int = 0
    rejected_fragments: int = 0
    rate_limited: int = 0


class NovelSceneFallback:
    """Consult a resolver for persistently unknown frames and cache results."""

    def __init__(
        self,
        engine: SceneEngine,
        resolver: SceneResolver | None,
        *,
        enabled: bool = False,
        cache_path: Path | None = None,
        min_unknown_streak: int = 5,
        min_interval_seconds: float = 30.0,
        max_consultations: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min_unknown_streak < 1:
            raise ValueError("min_unknown_streak must be positive")
        self._engine = engine
        self._resolver = resolver
        self._enabled = enabled and resolver is not None
        self._cache_path = cache_path
        self._min_unknown_streak = min_unknown_streak
        self._min_interval_seconds = min_interval_seconds
        self._max_consultations = max_consultations
        self._clock = clock
        self._unknown_streak = 0
        self._last_consulted_at: float | None = None
        self.metrics = FallbackMetrics()
        if cache_path is not None and cache_path.exists():
            self._load_cache(cache_path)

    def _load_cache(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        for scene_id, scene in data.get("scenes", {}).items():
            try:
                self._engine.add_scene(scene_id, scene)
            except SceneManifestError:
                # A cache entry the current engine rejects is dropped rather
                # than trusted; it will be re-resolved when observed again.
                self.metrics.rejected_fragments += 1

    def _persist(self, scene_id: str, scene: Mapping[str, Any]) -> None:
        if self._cache_path is None:
            return
        data: dict[str, Any] = {"schema_version": 1, "scenes": {}}
        if self._cache_path.exists():
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        data.setdefault("scenes", {})[scene_id] = dict(scene)
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary = self._cache_path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self._cache_path)

    def observe(self, facts: SceneFacts, frame: np.ndarray) -> str | None:
        """Track unknown frames; consult the resolver when bounds allow.

        Returns the newly cached scene id when a resolution was accepted.
        """

        if facts.scene_id is not None:
            self._unknown_streak = 0
            return None
        self._unknown_streak += 1
        if not self._enabled or self._unknown_streak < self._min_unknown_streak:
            return None
        if self.metrics.consultations >= self._max_consultations:
            self.metrics.rate_limited += 1
            return None
        now = self._clock()
        if (
            self._last_consulted_at is not None
            and now - self._last_consulted_at < self._min_interval_seconds
        ):
            self.metrics.rate_limited += 1
            return None
        self._last_consulted_at = now
        self.metrics.consultations += 1
        assert self._resolver is not None  # _enabled implies a resolver
        result = self._resolver.resolve(frame)
        if result is None:
            return None
        scene_id = result.get("scene_id")
        scene = result.get("scene")
        if not isinstance(scene_id, str) or not scene_id or not isinstance(scene, Mapping):
            self.metrics.rejected_fragments += 1
            return None
        try:
            self._engine.add_scene(scene_id, scene)
        except SceneManifestError:
            self.metrics.rejected_fragments += 1
            return None
        self._persist(scene_id, scene)
        self.metrics.resolutions += 1
        self._unknown_streak = 0
        return scene_id


__all__ = ["FallbackMetrics", "NovelSceneFallback", "SceneResolver"]
