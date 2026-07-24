"""Tests for in-memory reversible-state calibration."""

from __future__ import annotations

import unittest

import numpy as np

from streambot.live_validation import (
    LiveCalibrationError,
    calibrate_reversible_region,
)
from streambot.perception import AlignedNumpyTemplateMatcher


class LiveCalibrationTests(unittest.TestCase):
    def test_selects_discriminative_tile_and_builds_valid_thresholds(self) -> None:
        closed = np.full((240, 320, 3), 30, dtype=np.uint8)
        opened = closed.copy()
        opened[120:240, 120:240] = 210
        closed_frames = [closed.copy(), closed.copy()]
        opened_frames = [opened.copy(), opened.copy()]

        result = calibrate_reversible_region(
            closed_frames,
            opened_frames,
            closed_frames,
            tile_size=120,
        )

        self.assertEqual((result.x, result.y), (120, 120))
        matcher = AlignedNumpyTemplateMatcher()
        closed_tile = closed[result.y : result.y + result.height, result.x : result.x + result.width]
        opened_tile = opened[result.y : result.y + result.height, result.x : result.x + result.width]
        self.assertGreaterEqual(
            matcher.score(closed_tile, result.closed_template),
            result.closed_threshold,
        )
        self.assertLess(
            matcher.score(opened_tile, result.closed_template),
            result.closed_threshold,
        )
        self.assertGreaterEqual(
            matcher.score(opened_tile, result.opened_template),
            result.opened_threshold,
        )

    def test_rejects_dynamic_states_without_separation(self) -> None:
        frames = [np.full((32, 32, 3), value, dtype=np.uint8) for value in (0, 40)]
        with self.assertRaises(LiveCalibrationError):
            calibrate_reversible_region(frames, frames, frames, tile_size=32)

    def test_rejects_invalid_frame_groups(self) -> None:
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        with self.assertRaises(LiveCalibrationError):
            calibrate_reversible_region([frame], [frame, frame], [frame, frame])


if __name__ == "__main__":
    unittest.main()
