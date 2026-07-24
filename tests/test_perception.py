"""Synthetic fixture tests for visual perception."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

from streambot.config import (
    AutomationProfile,
    ConfigurationError,
    PerceptionSettings,
    PredicateSettings,
    RegionSettings,
)
from streambot.perception import (
    OpenCvTemplateMatcher,
    PerceptionEngine,
    PerceptionError,
)


def profile_value() -> dict[str, object]:
    return {
        "name": "perception-fixture",
        "perception": {
            "regions": [
                {"name": "button", "x": 2, "y": 1, "width": 4, "height": 3},
                {"name": "label", "x": 0, "y": 4, "width": 6, "height": 2},
            ],
            "predicates": [
                {
                    "name": "anchor",
                    "type": "pixel",
                    "region": "button",
                    "x": 1,
                    "y": 1,
                    "bgr": [200, 100, 50],
                    "tolerance": 2,
                },
                {
                    "name": "button_color",
                    "type": "color",
                    "region": "button",
                    "bgr": [200, 100, 50],
                    "tolerance": 2,
                    "minimum_fraction": 0.9,
                },
                {
                    "name": "button_template",
                    "type": "template",
                    "region": "button",
                    "template": "button-normal",
                    "threshold": 0.99,
                },
                {
                    "name": "label_text",
                    "type": "ocr",
                    "region": "label",
                    "contains": "ready",
                },
            ],
            "signals": [
                {
                    "name": "ready",
                    "operator": "all",
                    "predicates": [
                        "anchor",
                        "button_color",
                        "button_template",
                        "label_text",
                    ],
                },
                {
                    "name": "visible",
                    "operator": "any",
                    "predicates": ["anchor", "label_text"],
                },
                {
                    "name": "anchor_missing",
                    "operator": "not",
                    "predicates": ["anchor"],
                },
            ],
        },
    }


def fixture_frame() -> np.ndarray:
    frame = np.zeros((6, 8, 3), dtype=np.uint8)
    frame[1:4, 2:6] = [200, 100, 50]
    return frame


class FakeOcr:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def recognize(self, image: np.ndarray) -> str:
        self.calls += 1
        return self.text


class PerceptionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = AutomationProfile.from_mapping(profile_value())
        self.frame = fixture_frame()
        self.template = self.frame[1:4, 2:6].copy()

    def test_all_predicate_types_and_composition_pass(self) -> None:
        ocr = FakeOcr("System READY now")
        engine = PerceptionEngine(
            self.profile.perception,
            templates={"button-normal": self.template},
            ocr=ocr,
        )

        result = engine.evaluate(self.frame)

        self.assertTrue(all(result.predicates.values()))
        self.assertTrue(result.signals["ready"])
        self.assertTrue(result.signals["visible"])
        self.assertFalse(result.signals["anchor_missing"])
        self.assertAlmostEqual(result.scores["button_color"], 1.0)
        self.assertAlmostEqual(result.scores["button_template"], 1.0)
        self.assertEqual(ocr.calls, 1)

    def test_negative_thresholds_produce_false_results(self) -> None:
        frame = self.frame.copy()
        frame[1:4, 2:6] = [0, 0, 0]
        engine = PerceptionEngine(
            self.profile.perception,
            templates={"button-normal": self.template},
            ocr=FakeOcr("not ready yet"),
        )

        result = engine.evaluate(frame)

        self.assertFalse(result.predicates["anchor"])
        self.assertFalse(result.predicates["button_color"])
        self.assertFalse(result.predicates["button_template"])
        self.assertTrue(result.predicates["label_text"])
        self.assertFalse(result.signals["ready"])

    def test_missing_template_fails_closed(self) -> None:
        engine = PerceptionEngine(self.profile.perception, ocr=FakeOcr("ready"))

        with self.assertRaisesRegex(PerceptionError, "unavailable"):
            engine.evaluate(self.frame)

    def test_missing_ocr_adapter_fails_closed(self) -> None:
        engine = PerceptionEngine(
            self.profile.perception, templates={"button-normal": self.template}
        )

        with self.assertRaisesRegex(PerceptionError, "OCR adapter"):
            engine.evaluate(self.frame)

    def test_region_outside_frame_is_rejected(self) -> None:
        value = profile_value()
        value["stream"] = {"width": 320, "height": 240}
        value["perception"]["regions"][0]["x"] = 7
        profile = AutomationProfile.from_mapping(value)
        engine = PerceptionEngine(profile.perception)

        with self.assertRaisesRegex(PerceptionError, "frame bounds"):
            engine.evaluate(self.frame)

    def test_non_image_input_is_rejected(self) -> None:
        engine = PerceptionEngine(self.profile.perception)

        with self.assertRaisesRegex(PerceptionError, "uint8 BGR"):
            engine.evaluate(np.zeros((6, 8), dtype=np.uint8))

    def test_ocr_negative_result_is_false(self) -> None:
        engine = PerceptionEngine(
            self.profile.perception,
            templates={"button-normal": self.template},
            ocr=FakeOcr("waiting"),
        )

        result = engine.evaluate(self.frame)

        self.assertFalse(result.predicates["label_text"])
        self.assertFalse(result.signals["ready"])

    def test_invalid_matcher_score_fails_closed(self) -> None:
        class InvalidMatcher:
            def score(self, image: np.ndarray, template: np.ndarray) -> float:
                return float("nan")

        engine = PerceptionEngine(
            self.profile.perception,
            templates={"button-normal": self.template},
            matcher=InvalidMatcher(),
            ocr=FakeOcr("ready"),
        )

        with self.assertRaisesRegex(PerceptionError, "invalid score"):
            engine.evaluate(self.frame)

    def test_directly_constructed_incomplete_predicate_fails_closed(self) -> None:
        settings = PerceptionSettings(
            regions=(RegionSettings("region", 0, 0, 1, 1),),
            predicates=(PredicateSettings("pixel", "pixel", "region"),),
        )

        with self.assertRaisesRegex(PerceptionError, "incomplete"):
            PerceptionEngine(settings).evaluate(np.zeros((1, 1, 3), dtype=np.uint8))


class PerceptionConfigurationTests(unittest.TestCase):
    def test_unknown_region_reference_is_rejected(self) -> None:
        value = profile_value()
        value["perception"]["predicates"][0]["region"] = "missing"

        with self.assertRaisesRegex(ConfigurationError, "unknown region"):
            AutomationProfile.from_mapping(value)

    def test_duplicate_predicate_names_are_rejected(self) -> None:
        value = profile_value()
        value["perception"]["predicates"][1]["name"] = "anchor"

        with self.assertRaisesRegex(ConfigurationError, "duplicate"):
            AutomationProfile.from_mapping(value)

    def test_not_signal_requires_one_predicate(self) -> None:
        value = profile_value()
        value["perception"]["signals"][2]["predicates"].append("label_text")

        with self.assertRaisesRegex(ConfigurationError, "exactly one"):
            AutomationProfile.from_mapping(value)

    def test_unknown_predicate_field_is_rejected(self) -> None:
        value = profile_value()
        value["perception"]["predicates"][0]["secret"] = "value"

        with self.assertRaisesRegex(ConfigurationError, "unknown keys"):
            AutomationProfile.from_mapping(value)

    def test_region_outside_stream_is_rejected_during_loading(self) -> None:
        value = profile_value()
        value["stream"] = {"width": 320, "height": 240}
        value["perception"]["regions"][0].update({"x": 319, "width": 2})

        with self.assertRaisesRegex(ConfigurationError, "stream bounds"):
            AutomationProfile.from_mapping(value)

    def test_pixel_outside_region_is_rejected_during_loading(self) -> None:
        value = profile_value()
        value["perception"]["predicates"][0]["x"] = 4

        with self.assertRaisesRegex(ConfigurationError, "region bounds"):
            AutomationProfile.from_mapping(value)


class OpenCvAdapterTests(unittest.TestCase):
    def test_missing_opencv_has_a_sanitized_error(self) -> None:
        with patch("streambot.perception.importlib.import_module", side_effect=ImportError):
            with self.assertRaisesRegex(PerceptionError, "unavailable"):
                OpenCvTemplateMatcher()

    def test_adapter_converts_normalized_difference_to_similarity(self) -> None:
        class FakeCv2:
            TM_SQDIFF_NORMED = 1

            @staticmethod
            def matchTemplate(image, template, method):
                self.assertEqual(method, 1)
                return np.asarray([[0.25]], dtype=np.float32)

            @staticmethod
            def minMaxLoc(result):
                return 0.25, 0.25, (0, 0), (0, 0)

        with patch(
            "streambot.perception.importlib.import_module", return_value=FakeCv2()
        ):
            matcher = OpenCvTemplateMatcher()

        score = matcher.score(
            np.zeros((2, 2, 3), dtype=np.uint8),
            np.zeros((1, 1, 3), dtype=np.uint8),
        )
        self.assertAlmostEqual(score, 0.75)


if __name__ == "__main__":
    unittest.main()
