"""Tests for strict automation profile validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from streambot.config import AutomationProfile, ConfigurationError, load_profile


class AutomationProfileTests(unittest.TestCase):
    def test_defaults_are_safe(self) -> None:
        profile = AutomationProfile.from_mapping({"name": "test"})

        self.assertEqual(profile.observation.sample_fps, 2.0)
        self.assertEqual(profile.observation.decoder, "videotoolbox")
        self.assertTrue(profile.safety.preserve_existing_desktop)
        self.assertTrue(profile.safety.dry_run)

    def test_existing_session_preservation_cannot_be_disabled(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "must be true"):
            AutomationProfile.from_mapping(
                {
                    "name": "unsafe",
                    "safety": {"preserve_existing_desktop": False},
                }
            )

    def test_unknown_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "unknown keys"):
            AutomationProfile.from_mapping({"name": "test", "address": "hidden"})

    def test_sample_rate_is_bounded(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "between"):
            AutomationProfile.from_mapping(
                {"name": "test", "observation": {"sample_fps": 1000}}
            )

    def test_example_profile_loads(self) -> None:
        profile = load_profile(Path("profiles/observe.json"))

        self.assertEqual(profile.name, "observe")
        self.assertEqual(profile.stream.width, 1280)

    def test_perception_example_profile_loads(self) -> None:
        profile = load_profile(Path("profiles/perception-example.json"))

        self.assertEqual(len(profile.perception.predicates), 4)
        self.assertEqual(profile.perception.signals[0].name, "ready")

    def test_invalid_json_has_sanitized_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text("not-json", encoding="utf-8")

            with self.assertRaisesRegex(ConfigurationError, "could not be loaded"):
                load_profile(path)

    def test_json_round_trip_fixture(self) -> None:
        value = json.loads(Path("profiles/observe.json").read_text(encoding="utf-8"))
        profile = AutomationProfile.from_mapping(value)

        self.assertFalse(profile.safety.dry_run is False)


if __name__ == "__main__":
    unittest.main()
