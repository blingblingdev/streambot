"""Fixture tests for the unified declarative scene engine."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from streambot.ocr import OcrLine
from streambot.scene import (
    ControlFact,
    SceneEngine,
    SceneError,
    SceneManifestError,
    load_scene_manifest,
)


def _frame(height: int = 720, width: int = 1280) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


def _write_manifest(directory: str, manifest: dict) -> Path:
    path = Path(directory) / "scene-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _manifest(scenes: dict) -> dict:
    return {"schema_version": 2, "target": "fixture", "scenes": scenes}


def _color_scene(bgr, region=(0, 0, 100, 100), **extra) -> dict:
    scene = {
        "detect": {
            "predicates": [
                {
                    "kind": "color",
                    "region": list(region),
                    "bgr": list(bgr),
                    "tolerance": 10,
                    "minimum_fraction": 0.5,
                }
            ]
        }
    }
    scene.update(extra)
    return scene


class FakeLineOcr:
    """Deterministic line OCR double: maps exact region shapes to lines."""

    def __init__(self, lines: tuple[OcrLine, ...]) -> None:
        self.lines = lines
        self.calls = 0

    def recognize_lines(self, image: np.ndarray) -> tuple[OcrLine, ...]:
        self.calls += 1
        return self.lines


def _line(text: str, x: float, y: float, w: float = 40.0, h: float = 16.0, confidence: float = 0.9) -> OcrLine:
    return OcrLine(
        box=((x, y), (x + w, y), (x + w, y + h), (x, y + h)),
        text=text,
        confidence=confidence,
    )


class ManifestLoadingTests(unittest.TestCase):
    def test_valid_manifest_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(
                directory, _manifest({"blue": _color_scene((255, 0, 0))})
            )
            manifest = load_scene_manifest(path)
        self.assertEqual(manifest["target"], "fixture")

    def test_rejects_missing_detect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(
                directory,
                _manifest({"broken": {"controls": []}}),
            )
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)

    def test_rejects_string_detect_escape_hatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(
                directory,
                _manifest({"escape": {"detect": "target_python_function"}}),
            )
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)

    def test_rejects_unknown_predicate_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(
                directory,
                _manifest(
                    {
                        "bad": {
                            "detect": {
                                "predicates": [
                                    {"kind": "magic", "region": [0, 0, 10, 10]}
                                ]
                            }
                        }
                    }
                ),
            )
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)

    def test_rejects_unknown_extractor_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scene = _color_scene((255, 0, 0))
            scene["controls"] = [
                {"id": "x", "action_kind": "click", "extractor": {"kind": "wild"}}
            ]
            path = _write_manifest(directory, _manifest({"bad": scene}))
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)

    def test_rejects_duplicate_control_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scene = _color_scene((255, 0, 0))
            scene["controls"] = [
                {
                    "id": "same",
                    "action_kind": "click",
                    "extractor": {"kind": "fixed-point", "point": [10, 10]},
                },
                {
                    "id": "same",
                    "action_kind": "click",
                    "extractor": {"kind": "fixed-point", "point": [20, 20]},
                },
            ]
            path = _write_manifest(directory, _manifest({"bad": scene}))
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)

    def test_rejects_region_without_pixels_or_norm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(
                directory,
                _manifest(
                    {
                        "bad": {
                            "detect": {
                                "predicates": [
                                    {
                                        "kind": "color",
                                        "bgr": [0, 0, 255],
                                        "minimum_fraction": 0.5,
                                    }
                                ]
                            }
                        }
                    }
                ),
            )
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)

    def test_rejects_static_index_out_of_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scene = _color_scene((255, 0, 0))
            scene["controls"] = [
                {
                    "id": "only",
                    "action_kind": "click",
                    "extractor": {"kind": "fixed-point", "point": [10, 10]},
                }
            ]
            scene["recommend"] = {"rule": "static-index", "index": 5}
            path = _write_manifest(directory, _manifest({"bad": scene}))
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)

    def test_rejects_temporal_candidate_without_click_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(
                directory,
                _manifest(
                    {
                        "bad": {
                            "detect": {
                                "predicates": [
                                    {
                                        "kind": "temporal-flicker",
                                        "candidates": [
                                            {"id": "c", "region": [0, 0, 32, 32]}
                                        ],
                                    }
                                ]
                            }
                        }
                    }
                ),
            )
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)


class ReviewRegressionTests(unittest.TestCase):
    def test_static_index_allows_dynamic_extractor_expansion(self) -> None:
        scene = _color_scene((255, 0, 0))
        scene["controls"] = [
            {
                "id": "choice",
                "action_kind": "click",
                "extractor": {
                    "kind": "color-blob",
                    "region": [0, 0, 100, 100],
                    "bgr": [10, 200, 250],
                },
            }
        ]
        scene["recommend"] = {"rule": "static-index", "index": 1}
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(directory, _manifest({"ok": scene}))
            manifest = load_scene_manifest(path)  # must not raise
        self.assertIn("ok", manifest["scenes"])

    def test_temporal_candidate_rejects_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(
                directory,
                _manifest(
                    {
                        "bad": {
                            "detect": {
                                "predicates": [
                                    {
                                        "kind": "temporal-flicker",
                                        "candidates": [
                                            {
                                                "id": "c",
                                                "region": [0, 0, 32, 32],
                                                "click_point": [10, 10],
                                                "high_treshold": 0.5,
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    }
                ),
            )
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)

    def test_two_temporal_predicates_per_scene_rejected(self) -> None:
        predicate = {
            "kind": "temporal-flicker",
            "candidates": [
                {"id": "c", "region": [0, 0, 32, 32], "click_point": [10, 10]}
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(
                directory,
                _manifest(
                    {"bad": {"detect": {"predicates": [predicate, dict(predicate)]}}}
                ),
            )
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)

    def test_pixel_norm_offsets_scale_with_resolution(self) -> None:
        manifest = _manifest(
            {
                "dot": {
                    "detect": {
                        "predicates": [
                            {
                                "kind": "pixel",
                                "region_norm": [0.0, 0.0, 0.5, 0.5],
                                "x_norm": 0.5,
                                "y_norm": 0.5,
                                "bgr": [0, 255, 0],
                                "tolerance": 4,
                            }
                        ]
                    }
                }
            }
        )
        engine = SceneEngine(manifest)
        for height, width in ((720, 1280), (1080, 1920)):
            frame = _frame(height, width)
            # Region is the top-left quadrant; the probe sits at its center.
            frame[
                int(round(0.5 * (height // 2 - 1))),
                int(round(0.5 * (width // 2 - 1))),
            ] = (0, 255, 0)
            self.assertEqual(engine.observe(frame, 1).scene_id, "dot")


class DetectionTests(unittest.TestCase):
    def test_color_detection_classifies_scene(self) -> None:
        engine = SceneEngine(_manifest({"blue": _color_scene((255, 0, 0))}))
        frame = _frame()
        frame[0:100, 0:100] = (255, 0, 0)
        facts = engine.observe(frame, 1)
        self.assertEqual(facts.scene_id, "blue")
        self.assertGreaterEqual(facts.scores["blue[0]"], 0.5)

    def test_no_match_returns_none_scene(self) -> None:
        engine = SceneEngine(_manifest({"blue": _color_scene((255, 0, 0))}))
        facts = engine.observe(_frame(), 1)
        self.assertIsNone(facts.scene_id)
        self.assertEqual(facts.controls, ())
        self.assertEqual(facts.stability, 0)

    def test_pixel_detection(self) -> None:
        manifest = _manifest(
            {
                "dot": {
                    "detect": {
                        "predicates": [
                            {
                                "kind": "pixel",
                                "region": [10, 10, 20, 20],
                                "x": 5,
                                "y": 5,
                                "bgr": [0, 255, 0],
                                "tolerance": 4,
                            }
                        ]
                    }
                }
            }
        )
        engine = SceneEngine(manifest)
        frame = _frame()
        frame[15, 15] = (0, 255, 0)
        self.assertEqual(engine.observe(frame, 1).scene_id, "dot")
        self.assertIsNone(engine.observe(_frame(), 2).scene_id)

    def test_template_detection(self) -> None:
        template = np.full((20, 20, 3), 200, dtype=np.uint8)
        manifest = _manifest(
            {
                "logo": {
                    "detect": {
                        "predicates": [
                            {
                                "kind": "template",
                                "region": [100, 100, 20, 20],
                                "template": "logo",
                                "threshold": 0.95,
                            }
                        ]
                    }
                }
            }
        )
        engine = SceneEngine(manifest, templates={"logo": template})
        frame = _frame()
        frame[100:120, 100:120] = 200
        self.assertEqual(engine.observe(frame, 1).scene_id, "logo")
        self.assertIsNone(engine.observe(_frame(), 2).scene_id)

    def test_missing_template_fails_at_construction(self) -> None:
        manifest = _manifest(
            {
                "logo": {
                    "detect": {
                        "predicates": [
                            {
                                "kind": "template",
                                "region": [0, 0, 20, 20],
                                "template": "absent",
                                "threshold": 0.9,
                            }
                        ]
                    }
                }
            }
        )
        with self.assertRaises(SceneManifestError):
            SceneEngine(manifest)

    def test_ocr_contains_detection(self) -> None:
        manifest = _manifest(
            {
                "menu": {
                    "detect": {
                        "predicates": [
                            {
                                "kind": "ocr-contains",
                                "region": [0, 0, 200, 50],
                                "contains": "Settings",
                            }
                        ]
                    }
                }
            }
        )
        ocr = FakeLineOcr((_line("settings menu", 10, 10),))
        engine = SceneEngine(manifest, ocr=ocr)
        self.assertEqual(engine.observe(_frame(), 1).scene_id, "menu")

    def test_ocr_locate_detection_records_score(self) -> None:
        manifest = _manifest(
            {
                "choice": {
                    "detect": {
                        "predicates": [
                            {
                                "kind": "ocr-locate",
                                "region": [0, 0, 400, 400],
                                "match_any": ["continue", "start"],
                                "min_confidence": 0.5,
                            }
                        ]
                    }
                }
            }
        )
        ocr = FakeLineOcr((_line("Continue", 50, 60, confidence=0.88),))
        engine = SceneEngine(manifest, ocr=ocr)
        facts = engine.observe(_frame(), 1)
        self.assertEqual(facts.scene_id, "choice")
        self.assertAlmostEqual(facts.scores["choice[0]"], 0.88)
        self.assertEqual(len(facts.ocr_lines), 1)

    def test_ocr_without_adapter_fails_closed(self) -> None:
        manifest = _manifest(
            {
                "menu": {
                    "detect": {
                        "predicates": [
                            {
                                "kind": "ocr-contains",
                                "region": [0, 0, 200, 50],
                                "contains": "x",
                            }
                        ]
                    }
                }
            }
        )
        engine = SceneEngine(manifest)
        with self.assertRaises(SceneError):
            engine.observe(_frame(), 1)

    def test_all_operator_short_circuits_before_ocr(self) -> None:
        manifest = _manifest(
            {
                "gated": {
                    "detect": {
                        "operator": "all",
                        "predicates": [
                            {
                                "kind": "ocr-contains",
                                "region": [0, 0, 200, 50],
                                "contains": "x",
                            },
                            {
                                "kind": "color",
                                "region": [0, 0, 100, 100],
                                "bgr": [255, 0, 0],
                                "minimum_fraction": 0.5,
                            },
                        ],
                    }
                }
            }
        )
        ocr = FakeLineOcr(())
        engine = SceneEngine(manifest, ocr=ocr)
        engine.observe(_frame(), 1)  # cheap color predicate fails first
        self.assertEqual(ocr.calls, 0)

    def test_not_operator(self) -> None:
        manifest = _manifest(
            {
                "dark": {
                    "detect": {
                        "operator": "not",
                        "predicates": [
                            {
                                "kind": "color",
                                "region": [0, 0, 100, 100],
                                "bgr": [255, 255, 255],
                                "minimum_fraction": 0.5,
                            }
                        ],
                    }
                }
            }
        )
        engine = SceneEngine(manifest)
        self.assertEqual(engine.observe(_frame(), 1).scene_id, "dark")

    def test_priority_orders_classification(self) -> None:
        low = _color_scene((0, 255, 0))
        high = _color_scene((0, 255, 0), priority=10)
        engine = SceneEngine(_manifest({"low": low, "high": high}))
        frame = _frame()
        frame[0:100, 0:100] = (0, 255, 0)
        self.assertEqual(engine.observe(frame, 1).scene_id, "high")

    def test_stability_counts_consecutive_scene_frames(self) -> None:
        engine = SceneEngine(_manifest({"blue": _color_scene((255, 0, 0))}))
        frame = _frame()
        frame[0:100, 0:100] = (255, 0, 0)
        self.assertEqual(engine.observe(frame, 1).stability, 1)
        self.assertEqual(engine.observe(frame, 2).stability, 2)
        self.assertEqual(engine.observe(_frame(), 3).stability, 0)
        self.assertEqual(engine.observe(frame, 4).stability, 1)

    def test_reset_clears_stability(self) -> None:
        engine = SceneEngine(_manifest({"blue": _color_scene((255, 0, 0))}))
        frame = _frame()
        frame[0:100, 0:100] = (255, 0, 0)
        engine.observe(frame, 1)
        engine.reset()
        self.assertEqual(engine.observe(frame, 2).stability, 1)


class NormalizedCoordinateTests(unittest.TestCase):
    def _norm_manifest(self) -> dict:
        return _manifest(
            {
                "banner": {
                    "detect": {
                        "predicates": [
                            {
                                "kind": "color",
                                "region_norm": [0.0, 0.0, 0.1, 0.1],
                                "bgr": [0, 0, 255],
                                "tolerance": 10,
                                "minimum_fraction": 0.5,
                            }
                        ]
                    },
                    "controls": [
                        {
                            "id": "ok",
                            "action_kind": "click",
                            "extractor": {
                                "kind": "fixed-point",
                                "point_norm": [0.5, 0.5],
                            },
                        }
                    ],
                    "recommend": {"rule": "by-id", "id": "ok"},
                }
            }
        )

    def test_same_manifest_supports_720p_and_1080p(self) -> None:
        engine = SceneEngine(self._norm_manifest())
        for height, width in ((720, 1280), (1080, 1920)):
            frame = _frame(height, width)
            frame[0 : height // 10, 0 : width // 10] = (0, 0, 255)
            facts = engine.observe(frame, 1)
            self.assertEqual(facts.scene_id, "banner")
            control = facts.controls[0]
            self.assertEqual((control.x, control.y), (width // 2, height // 2))
            self.assertEqual(facts.recommended_id, "ok")

    def test_absolute_region_out_of_bounds_fails_closed(self) -> None:
        engine = SceneEngine(
            _manifest({"big": _color_scene((255, 0, 0), region=(0, 0, 2000, 100))})
        )
        with self.assertRaises(SceneError):
            engine.observe(_frame(), 1)


class ExtractorTests(unittest.TestCase):
    def test_fixed_point_and_static_index_recommend(self) -> None:
        scene = _color_scene((255, 0, 0))
        scene["controls"] = [
            {
                "id": "confirm",
                "action_kind": "click",
                "extractor": {"kind": "fixed-point", "point": [640, 360]},
            }
        ]
        scene["recommend"] = {"rule": "static-index", "index": 0}
        engine = SceneEngine(_manifest({"blue": scene}))
        frame = _frame()
        frame[0:100, 0:100] = (255, 0, 0)
        facts = engine.observe(frame, 1)
        self.assertEqual(
            facts.controls, (ControlFact("confirm", "click", 640, 360, 1.0),)
        )
        self.assertEqual(facts.recommended_id, "confirm")

    def test_color_blob_extractor_finds_indexed_blobs(self) -> None:
        scene = _color_scene((255, 0, 0))
        scene["controls"] = [
            {
                "id": "choice",
                "action_kind": "click",
                "extractor": {
                    "kind": "color-blob",
                    "region": [200, 200, 400, 200],
                    "bgr": [10, 200, 250],
                    "tolerance": 8,
                    "min_area": 50,
                },
            }
        ]
        engine = SceneEngine(_manifest({"blue": scene}))
        frame = _frame()
        frame[0:100, 0:100] = (255, 0, 0)
        frame[250:270, 250:280] = (10, 200, 250)
        frame[250:270, 450:480] = (10, 200, 250)
        facts = engine.observe(frame, 1)
        ids = [control.control_id for control in facts.controls]
        self.assertEqual(ids, ["choice-0", "choice-1"])
        self.assertLess(facts.controls[0].x, facts.controls[1].x)

    def test_template_grid_extractor(self) -> None:
        template = np.full((16, 16, 3), 220, dtype=np.uint8)
        scene = _color_scene((255, 0, 0))
        scene["controls"] = [
            {
                "id": "cell",
                "action_kind": "click",
                "extractor": {
                    "kind": "template-grid",
                    "region": [0, 200, 600, 200],
                    "template": "cell",
                    "threshold": 0.9,
                },
            }
        ]
        engine = SceneEngine(_manifest({"blue": scene}), templates={"cell": template})
        frame = _frame()
        frame[0:100, 0:100] = (255, 0, 0)
        frame[240:256, 100:116] = 220
        frame[240:256, 300:316] = 220
        facts = engine.observe(frame, 1)
        self.assertEqual(len(facts.controls), 2)
        self.assertEqual(facts.controls[0].control_id, "cell-0")
        self.assertEqual(facts.controls[0].y, 248)

    def test_ocr_line_extractor_attaches_text_and_by_text_recommend(self) -> None:
        scene = {
            "detect": {
                "predicates": [
                    {
                        "kind": "ocr-locate",
                        "region": [0, 0, 1280, 720],
                        "match_any": ["option"],
                    }
                ]
            },
            "controls": [
                {
                    "id": "opt",
                    "action_kind": "click",
                    "extractor": {
                        "kind": "ocr-line",
                        "region": [0, 0, 1280, 720],
                        "match_any": ["Option"],
                    },
                }
            ],
            "recommend": {"rule": "by-text", "text": "Option B"},
        }
        ocr = FakeLineOcr(
            (
                _line("Option A", 100, 100),
                _line("Option B", 100, 200),
            )
        )
        engine = SceneEngine(_manifest({"choices": scene}), ocr=ocr)
        facts = engine.observe(_frame(), 1)
        self.assertEqual(facts.scene_id, "choices")
        self.assertEqual(len(facts.controls), 2)
        self.assertEqual(facts.controls[0].text, "Option A")
        self.assertEqual(facts.controls[0].x, 120)
        self.assertEqual(facts.controls[0].y, 108)
        self.assertEqual(facts.recommended_id, "opt-1")
        # OCR ran once for the shared region despite detector + extractor use.
        self.assertEqual(ocr.calls, 1)

    def test_each_point_and_context_point_from_external_context(self) -> None:
        scene = _color_scene((255, 0, 0))
        scene["controls"] = [
            {
                "id": "node",
                "action_kind": "click",
                "extractor": {"kind": "each-point", "source": "nodes"},
            },
            {
                "id": "back",
                "action_kind": "click",
                "extractor": {"kind": "context-point", "source": "back_point"},
            },
        ]
        engine = SceneEngine(_manifest({"blue": scene}))
        frame = _frame()
        frame[0:100, 0:100] = (255, 0, 0)
        facts = engine.observe(
            frame,
            1,
            context={"nodes": ((10, 20), (30, 40)), "back_point": (50, 60)},
        )
        ids = [control.control_id for control in facts.controls]
        self.assertEqual(ids, ["node-0", "node-1", "back"])
        empty = engine.observe(frame, 2)
        self.assertEqual(empty.controls, ())

    def test_context_recommend_requires_existing_control(self) -> None:
        scene = _color_scene((255, 0, 0))
        scene["controls"] = [
            {
                "id": "a",
                "action_kind": "click",
                "extractor": {"kind": "fixed-point", "point": [1, 1]},
            }
        ]
        scene["recommend"] = {"rule": "context", "key": "pick"}
        engine = SceneEngine(_manifest({"blue": scene}))
        frame = _frame()
        frame[0:100, 0:100] = (255, 0, 0)
        self.assertEqual(
            engine.observe(frame, 1, context={"pick": "a"}).recommended_id, "a"
        )
        self.assertIsNone(
            engine.observe(frame, 2, context={"pick": "missing"}).recommended_id
        )
        self.assertIsNone(engine.observe(frame, 3).recommended_id)


class TemporalDetectorTests(unittest.TestCase):
    def _temporal_manifest(self) -> dict:
        return _manifest(
            {
                "phone-reply": {
                    "detect": {
                        "predicates": [
                            {
                                "kind": "temporal-flicker",
                                "candidates": [
                                    {
                                        "id": "reply",
                                        "region": [600, 300, 64, 64],
                                        "click_point": [632, 332],
                                        "high_threshold": 0.12,
                                        "low_threshold": 0.05,
                                        "minimum_high_samples": 2,
                                    }
                                ],
                                "baseline_samples": 3,
                            }
                        ]
                    },
                    "controls": [
                        {
                            "id": "tap",
                            "action_kind": "click",
                            "extractor": {"kind": "temporal-candidate"},
                        }
                    ],
                    "recommend": {"rule": "by-id", "id": "tap-reply"},
                }
            }
        )

    def test_flicker_reaches_stable_high_and_extracts_candidate(self) -> None:
        engine = SceneEngine(self._temporal_manifest())
        quiet = _frame()
        flash = _frame()
        flash[300:364, 600:664] = (255, 255, 255)
        matched = None
        for frame_number in range(1, 4):
            facts = engine.observe(quiet, frame_number)
            self.assertIsNone(facts.scene_id)
        for frame_number in range(4, 10):
            facts = engine.observe(flash, frame_number)
            if facts.scene_id is not None:
                matched = facts
                break
        self.assertIsNotNone(matched)
        self.assertEqual(matched.scene_id, "phone-reply")
        self.assertEqual(matched.controls[0].control_id, "tap-reply")
        self.assertEqual((matched.controls[0].x, matched.controls[0].y), (632, 332))
        self.assertEqual(matched.recommended_id, "tap-reply")

    def test_reset_clears_temporal_history(self) -> None:
        engine = SceneEngine(self._temporal_manifest())
        quiet = _frame()
        flash = _frame()
        flash[300:364, 600:664] = (255, 255, 255)
        for frame_number in range(1, 4):
            engine.observe(quiet, frame_number)
        engine.reset()
        # After reset the baseline must be rebuilt; the first flash frames
        # cannot immediately produce a stable high phase.
        facts = engine.observe(flash, 10)
        self.assertIsNone(facts.scene_id)


class ExtensionRegistryTests(unittest.TestCase):
    def _extension_manifest(self) -> dict:
        return _manifest(
            {
                "custom": {
                    "detect": {
                        "predicates": [{"kind": "x-marker", "level": 30}]
                    },
                    "controls": [
                        {
                            "id": "spot",
                            "action_kind": "click",
                            "extractor": {"kind": "x-spots"},
                        }
                    ],
                    "recommend": {"rule": "static-index", "index": 0},
                }
            }
        )

    def test_registered_extension_detector_and_extractor(self) -> None:
        def detector(ctx, params):
            return bool(ctx.frame.mean() > float(params["level"])), 0.7

        def extractor(ctx, params):
            return ((0, 40, 50, 0.9, "label"), ("b", 60, 70, 0.8, None))

        engine = SceneEngine(
            self._extension_manifest(),
            extra_detectors={"x-marker": detector},
            extra_extractors={"x-spots": extractor},
        )
        bright = np.full((720, 1280, 3), 90, dtype=np.uint8)
        facts = engine.observe(bright, 1)
        self.assertEqual(facts.scene_id, "custom")
        self.assertEqual(
            [c.control_id for c in facts.controls], ["spot-0", "spot-b"]
        )
        self.assertEqual(facts.controls[0].text, "label")
        self.assertIsNone(engine.observe(_frame(), 2).scene_id)

    def test_unregistered_extension_kind_fails_closed(self) -> None:
        with self.assertRaises(SceneManifestError):
            SceneEngine(self._extension_manifest())

    def test_extension_kind_requires_prefix(self) -> None:
        with self.assertRaises(SceneManifestError):
            SceneEngine(
                _manifest({"blue": _color_scene((255, 0, 0))}),
                extra_detectors={"marker": lambda ctx, params: (True, None)},
            )


class SanitizationTests(unittest.TestCase):
    def test_sanitized_summary_contains_no_text(self) -> None:
        scene = {
            "detect": {
                "predicates": [
                    {
                        "kind": "ocr-locate",
                        "region": [0, 0, 1280, 720],
                        "match_any": ["secret"],
                    }
                ]
            },
            "controls": [
                {
                    "id": "btn",
                    "action_kind": "click",
                    "extractor": {
                        "kind": "ocr-line",
                        "region": [0, 0, 1280, 720],
                    },
                }
            ],
        }
        ocr = FakeLineOcr((_line("secret words", 10, 10),))
        engine = SceneEngine(_manifest({"s": scene}), ocr=ocr)
        facts = engine.observe(_frame(), 1)
        summary = facts.sanitized_summary()
        payload = json.dumps(summary)
        self.assertNotIn("secret", payload)
        self.assertEqual(summary["scene_id"], "s")
        self.assertEqual(summary["ocr_line_count"], 1)
        self.assertEqual(summary["control_ids"], ["btn-0"])


class ColorMaskPredicateTest(unittest.TestCase):
    """The channel/diff/gray mask predicate added for the A4 migration."""

    @staticmethod
    def _scene(rules, region=(0, 0, 100, 100), **fractions) -> dict:
        predicate = {"kind": "color-mask", "region": list(region), "rules": rules}
        predicate.update(fractions)
        return {
            "detect": {"predicates": [predicate]},
            "controls": [
                {
                    "id": "ok",
                    "action_kind": "click",
                    "extractor": {"kind": "fixed-point", "point": [10, 10]},
                }
            ],
            "recommend": {"rule": "by-id", "id": "ok"},
        }

    def test_channel_and_diff_rules_match_fraction(self) -> None:
        scene = self._scene(
            [{"channel": "b", "gt": 170}, {"diff": ["b", "r"], "gt": 50}],
            min_fraction=0.5,
        )
        engine = SceneEngine(_manifest({"s": scene}))
        frame = _frame()
        frame[0:100, 0:60] = (200, 0, 20)  # blue-dominant: passes both rules
        facts = engine.observe(frame, 1)
        self.assertEqual(facts.scene_id, "s")
        self.assertAlmostEqual(facts.scores["s[0]"], 0.6, places=3)

    def test_diff_rule_survives_uint8_wraparound(self) -> None:
        # b - r is negative here; uint8 arithmetic would wrap to a large
        # positive value and match. The predicate must evaluate in int16.
        scene = self._scene([{"diff": ["b", "r"], "gt": 50}], min_fraction=0.01)
        engine = SceneEngine(_manifest({"s": scene}))
        frame = _frame()
        frame[:, :] = (10, 0, 200)  # red-dominant everywhere
        self.assertIsNone(engine.observe(frame, 1).scene_id)

    def test_gray_rule_and_below_fraction(self) -> None:
        scene = self._scene([{"gray": True, "gt": 180}], below_fraction=0.5)
        engine = SceneEngine(_manifest({"s": scene}))
        bright = _frame()
        bright[:, :] = (200, 200, 200)
        self.assertIsNone(engine.observe(bright, 1).scene_id)
        self.assertEqual(engine.observe(_frame(), 2).scene_id, "s")

    def test_lt_rule_matches_dark_channel(self) -> None:
        scene = self._scene(
            [{"channel": "g", "lt": 50}, {"channel": "r", "gt": 150}],
            min_fraction=0.9,
        )
        engine = SceneEngine(_manifest({"s": scene}))
        frame = _frame()
        frame[:, :] = (0, 10, 200)
        self.assertEqual(engine.observe(frame, 1).scene_id, "s")

    def test_schema_rejects_missing_fraction_bound(self) -> None:
        scene = self._scene([{"channel": "b", "gt": 170}])
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(directory, _manifest({"s": scene}))
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)

    def test_schema_rejects_rule_without_threshold(self) -> None:
        scene = self._scene([{"channel": "b"}], min_fraction=0.5)
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(directory, _manifest({"s": scene}))
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)

    def test_schema_rejects_unknown_channel(self) -> None:
        scene = self._scene([{"channel": "a", "gt": 10}], min_fraction=0.5)
        with tempfile.TemporaryDirectory() as directory:
            path = _write_manifest(directory, _manifest({"s": scene}))
            with self.assertRaises(SceneManifestError):
                load_scene_manifest(path)


if __name__ == "__main__":
    unittest.main()
