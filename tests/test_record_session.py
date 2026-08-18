"""Tests for the demonstration recorder's keep/drop decisions."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

_spec = importlib.util.spec_from_file_location(
    "record_session", PROJECT_ROOT / "scripts" / "record_session.py"
)
record_session = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(record_session)


def flat(value: float) -> np.ndarray:
    return np.full((90, 160), value, dtype=np.float32)


class FrameKeeperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keeper = record_session.FrameKeeper(
            diff_threshold=0.012, heartbeat_seconds=3.0
        )

    def test_the_first_frame_is_always_kept(self) -> None:
        reason, _ = self.keeper.decide(flat(100.0), now=0.0)
        self.assertEqual(reason, "first")

    def test_a_still_screen_is_dropped(self) -> None:
        self.keeper.decide(flat(100.0), now=0.0)
        self.assertIsNone(self.keeper.decide(flat(100.5), now=1.0))

    def test_a_real_change_is_kept_with_its_score(self) -> None:
        self.keeper.decide(flat(100.0), now=0.0)
        verdict = self.keeper.decide(flat(140.0), now=1.0)
        self.assertIsNotNone(verdict)
        reason, diff = verdict
        self.assertEqual(reason, "change")
        self.assertAlmostEqual(diff, 40.0 / 255.0, places=3)

    def test_quiet_stretches_still_leave_heartbeat_frames(self) -> None:
        # A loading screen is part of the story: its duration must be
        # visible in the timeline even though nothing on it changes.
        self.keeper.decide(flat(100.0), now=0.0)
        self.assertIsNone(self.keeper.decide(flat(100.0), now=2.9))
        verdict = self.keeper.decide(flat(100.0), now=3.1)
        self.assertEqual(verdict[0], "heartbeat")
        # The heartbeat resets the clock; the next quiet frame drops again.
        self.assertIsNone(self.keeper.decide(flat(100.0), now=4.0))

    def test_change_is_measured_against_the_last_kept_frame(self) -> None:
        # A slow fade must not slip under the threshold one step at a time:
        # drift accumulates against the kept frame, not the previous sample.
        self.keeper.decide(flat(100.0), now=0.0)
        self.assertIsNone(self.keeper.decide(flat(102.0), now=0.5))
        verdict = self.keeper.decide(flat(104.0), now=1.0)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict[0], "change")


if __name__ == "__main__":
    unittest.main()
