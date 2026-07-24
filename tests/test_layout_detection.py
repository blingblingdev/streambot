"""Tests for declarative, manifest-driven layout detection."""

from __future__ import annotations

import unittest

import numpy as np

from streambot.control_surface import ManifestError
from streambot.layout_detection import LayoutDetector


def _frame() -> np.ndarray:
    return np.zeros((720, 1280, 3), dtype=np.uint8)


def _color_layout(bgr, region=(0, 0, 100, 100), minimum_fraction=0.5, operator="all"):
    return {
        "detect": {
            "operator": operator,
            "predicates": [
                {
                    "kind": "color",
                    "region": list(region),
                    "bgr": list(bgr),
                    "tolerance": 10,
                    "minimum_fraction": minimum_fraction,
                }
            ],
        }
    }


def _manifest(layouts):
    return {"schema_version": 1, "target": "fixture", "layouts": layouts}


class LayoutDetectionTests(unittest.TestCase):
    def test_classifies_by_color_region(self) -> None:
        detector = LayoutDetector(_manifest({"blue": _color_layout((255, 0, 0))}))
        frame = _frame()
        frame[0:100, 0:100] = (255, 0, 0)  # blue region
        self.assertEqual(detector.classify(frame), "blue")

    def test_no_match_returns_none(self) -> None:
        detector = LayoutDetector(_manifest({"blue": _color_layout((255, 0, 0))}))
        self.assertIsNone(detector.classify(_frame()))  # all black

    def test_priority_first_declared_layout_wins(self) -> None:
        detector = LayoutDetector(
            _manifest({"a": _color_layout((0, 255, 0)), "b": _color_layout((0, 255, 0))})
        )
        frame = _frame()
        frame[0:100, 0:100] = (0, 255, 0)
        self.assertEqual(detector.declarative_layouts, ("a", "b"))
        self.assertEqual(detector.classify(frame), "a")

    def test_string_detect_is_runtime_escape_hatch(self) -> None:
        detector = LayoutDetector(
            _manifest({"runtime": {"detect": "some_target_python_detector"}})
        )
        self.assertEqual(detector.declarative_layouts, ())
        self.assertIsNone(detector.classify(_frame()))

    def test_not_operator_matches_absence(self) -> None:
        detector = LayoutDetector(
            _manifest({"not-blue": _color_layout((255, 0, 0), operator="not")})
        )
        self.assertEqual(detector.classify(_frame()), "not-blue")  # black is not blue
        frame = _frame()
        frame[0:100, 0:100] = (255, 0, 0)
        self.assertIsNone(detector.classify(frame))  # now blue -> not-blue is false

    def test_pixel_predicate(self) -> None:
        layout = {
            "detect": {
                "predicates": [
                    {
                        "kind": "pixel",
                        "region": [640, 360, 1, 1],
                        "x": 0,
                        "y": 0,
                        "bgr": [10, 20, 30],
                        "tolerance": 2,
                    }
                ]
            }
        }
        detector = LayoutDetector(_manifest({"dot": layout}))
        frame = _frame()
        frame[360, 640] = (10, 20, 30)
        self.assertEqual(detector.classify(frame), "dot")

    def test_malformed_detect_fails_closed(self) -> None:
        # color predicate missing minimum_fraction
        bad = {"detect": {"predicates": [{"kind": "color", "region": [0, 0, 10, 10], "bgr": [1, 2, 3]}]}}
        with self.assertRaises(ManifestError):
            LayoutDetector(_manifest({"bad": bad}))

    def test_bad_region_fails_closed(self) -> None:
        bad = {"detect": {"predicates": [{"kind": "color", "region": [0, 0], "bgr": [1, 2, 3], "minimum_fraction": 0.1}]}}
        with self.assertRaises(ManifestError):
            LayoutDetector(_manifest({"bad": bad}))


if __name__ == "__main__":
    unittest.main()
