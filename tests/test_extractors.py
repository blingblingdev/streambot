"""Tests for declarative frame-based candidate extractors (color-blob)."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from streambot.control_surface import (
    ManifestControlScanner,
    ManifestError,
    load_control_manifest,
)
from streambot.extractors import color_blob


def _frame() -> np.ndarray:
    return np.zeros((720, 1280, 3), dtype=np.uint8)


class ColorBlobTests(unittest.TestCase):
    def test_finds_and_orders_blobs_left_to_right(self) -> None:
        frame = _frame()
        frame[100:140, 200:240] = (255, 0, 0)  # blob A ~ (219, 119)
        frame[100:140, 400:440] = (255, 0, 0)  # blob B ~ (419, 119)
        params = {"region": [0, 0, 1280, 720], "bgr": [255, 0, 0], "tolerance": 10, "min_area": 100}
        result = color_blob(frame, params, {})
        self.assertEqual(len(result), 2)
        (i0, x0, y0, c0), (i1, x1, y1, c1) = result
        self.assertEqual((i0, i1), (0, 1))
        self.assertAlmostEqual(x0, 219, delta=3)
        self.assertAlmostEqual(x1, 419, delta=3)
        self.assertAlmostEqual(y0, 119, delta=3)
        self.assertEqual(c0, 1.0)

    def test_min_area_filters_noise(self) -> None:
        frame = _frame()
        frame[100:140, 200:240] = (0, 255, 0)  # big blob
        frame[10:12, 10:12] = (0, 255, 0)  # tiny 2x2 noise
        params = {"region": [0, 0, 1280, 720], "bgr": [0, 255, 0], "min_area": 100}
        result = color_blob(frame, params, {})
        self.assertEqual(len(result), 1)

    def test_scanner_drives_controls_from_color_blob(self) -> None:
        frame = _frame()
        frame[500:540, 256 - 20 : 256 + 20] = (0, 0, 255)  # red blob ~ x256
        frame[500:540, 640 - 20 : 640 + 20] = (0, 0, 255)  # red blob ~ x640
        manifest = {
            "schema_version": 1,
            "target": "fixture",
            "layouts": {
                "choices": {
                    "controls": [
                        {
                            "id": "choice",
                            "action_kind": "click",
                            "extractor": {
                                "kind": "color-blob",
                                "region": [0, 480, 1280, 100],
                                "bgr": [0, 0, 255],
                                "min_area": 100,
                            },
                        }
                    ],
                    "recommend": {"rule": "static-index", "index": 0},
                }
            },
        }
        scanner = ManifestControlScanner(manifest)
        controls = scanner.controls("choices", frame)
        self.assertEqual([c.control_id for c in controls], ["choice-0", "choice-1"])
        self.assertAlmostEqual(controls[0].x, 256, delta=3)
        self.assertAlmostEqual(controls[1].x, 640, delta=3)
        self.assertEqual(scanner.recommend("choices", controls), "choice-0")


class ColorBlobValidationTests(unittest.TestCase):
    def _reject(self, extractor) -> None:
        layout = {
            "controls": [{"id": "c", "action_kind": "click", "extractor": extractor}],
            "recommend": {"rule": "none"},
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "m.json"
            path.write_text(
                json.dumps({"schema_version": 1, "target": "t", "layouts": {"a": layout}}),
                encoding="utf-8",
            )
            with self.assertRaises(ManifestError):
                load_control_manifest(path)

    def test_rejects_color_blob_without_region(self) -> None:
        self._reject({"kind": "color-blob", "bgr": [1, 2, 3]})

    def test_rejects_color_blob_without_color(self) -> None:
        self._reject({"kind": "color-blob", "region": [0, 0, 10, 10]})


if __name__ == "__main__":
    unittest.main()
