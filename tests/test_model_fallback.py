"""Fixture proof for the bounded novel-scene model fallback."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from streambot.model_fallback import NovelSceneFallback
from streambot.scene import SceneEngine


def _manifest() -> dict:
    return {
        "schema_version": 2,
        "target": "fixture",
        "scenes": {
            "known": {
                "detect": {
                    "predicates": [
                        {
                            "kind": "color",
                            "region": [0, 0, 50, 50],
                            "bgr": [255, 0, 0],
                            "tolerance": 10,
                            "minimum_fraction": 0.5,
                        }
                    ]
                }
            }
        },
    }


def _novel_fragment() -> dict:
    return {
        "scene_id": "novel-green",
        "scene": {
            "detect": {
                "predicates": [
                    {
                        "kind": "color",
                        "region": [0, 0, 50, 50],
                        "bgr": [0, 255, 0],
                        "tolerance": 10,
                        "minimum_fraction": 0.5,
                    }
                ]
            },
            "controls": [
                {
                    "id": "ack",
                    "action_kind": "click",
                    "extractor": {"kind": "fixed-point", "point": [640, 360]},
                }
            ],
            "recommend": {"rule": "by-id", "id": "ack"},
        },
    }


class FakeResolver:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = 0

    def resolve(self, frame):
        self.calls += 1
        return self.result


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _green_frame() -> np.ndarray:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[0:50, 0:50] = (0, 255, 0)
    return frame


class ModelFallbackTests(unittest.TestCase):
    def test_novel_scene_resolved_once_then_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "novel-scenes.json"
            engine = SceneEngine(_manifest())
            resolver = FakeResolver(_novel_fragment())
            fallback = NovelSceneFallback(
                engine,
                resolver,
                enabled=True,
                cache_path=cache,
                min_unknown_streak=3,
                min_interval_seconds=0.0,
            )
            frame = _green_frame()
            resolved = None
            for number in range(1, 6):
                facts = engine.observe(frame, number)
                resolved = fallback.observe(facts, frame) or resolved
                if resolved:
                    break
            self.assertEqual(resolved, "novel-green")
            self.assertEqual(resolver.calls, 1)
            # Afterwards the scene is classified deterministically.
            facts = engine.observe(frame, 10)
            self.assertEqual(facts.scene_id, "novel-green")
            self.assertEqual(facts.recommended_id, "ack")
            self.assertEqual(fallback.observe(facts, frame), None)
            self.assertEqual(resolver.calls, 1)
            # And the fragment is persisted for the next engine instance.
            cached = json.loads(cache.read_text(encoding="utf-8"))
            self.assertIn("novel-green", cached["scenes"])

    def test_cache_reload_requires_no_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "novel-scenes.json"
            first_engine = SceneEngine(_manifest())
            NovelSceneFallback(
                first_engine,
                FakeResolver(_novel_fragment()),
                enabled=True,
                cache_path=cache,
                min_unknown_streak=1,
                min_interval_seconds=0.0,
            ).observe(first_engine.observe(_green_frame(), 1), _green_frame())

            second_engine = SceneEngine(_manifest())
            NovelSceneFallback(
                second_engine, None, enabled=False, cache_path=cache
            )
            facts = second_engine.observe(_green_frame(), 1)
            self.assertEqual(facts.scene_id, "novel-green")

    def test_disabled_fallback_never_consults(self) -> None:
        engine = SceneEngine(_manifest())
        resolver = FakeResolver(_novel_fragment())
        fallback = NovelSceneFallback(engine, resolver, enabled=False)
        frame = _green_frame()
        for number in range(1, 20):
            fallback.observe(engine.observe(frame, number), frame)
        self.assertEqual(resolver.calls, 0)

    def test_rate_limits_and_consultation_cap(self) -> None:
        engine = SceneEngine(_manifest())
        resolver = FakeResolver(None)  # resolver cannot resolve
        clock = FakeClock()
        fallback = NovelSceneFallback(
            engine,
            resolver,
            enabled=True,
            min_unknown_streak=1,
            min_interval_seconds=30.0,
            max_consultations=2,
            clock=clock,
        )
        frame = _green_frame()
        fallback.observe(engine.observe(frame, 1), frame)
        self.assertEqual(resolver.calls, 1)
        fallback.observe(engine.observe(frame, 2), frame)  # within interval
        self.assertEqual(resolver.calls, 1)
        self.assertEqual(fallback.metrics.rate_limited, 1)
        clock.now += 31
        fallback.observe(engine.observe(frame, 3), frame)
        self.assertEqual(resolver.calls, 2)
        clock.now += 31
        fallback.observe(engine.observe(frame, 4), frame)  # cap reached
        self.assertEqual(resolver.calls, 2)

    def test_invalid_fragment_rejected(self) -> None:
        engine = SceneEngine(_manifest())
        resolver = FakeResolver({"scene_id": "bad", "scene": {"detect": "python!"}})
        fallback = NovelSceneFallback(
            engine, resolver, enabled=True, min_unknown_streak=1,
            min_interval_seconds=0.0,
        )
        frame = _green_frame()
        fallback.observe(engine.observe(frame, 1), frame)
        self.assertEqual(fallback.metrics.rejected_fragments, 1)
        self.assertIsNone(engine.observe(frame, 2).scene_id)


if __name__ == "__main__":
    unittest.main()
