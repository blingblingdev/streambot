"""The control plane notices silence, and counts only real operations."""

from __future__ import annotations

import unittest
from pathlib import Path

from streambot.control_plane import PersistentControlPlane


class Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ControlPlaneStallTests(unittest.TestCase):
    def _plane(self, clock: Clock) -> PersistentControlPlane:
        plane = PersistentControlPlane(Path("/tmp/does-not-need-to-exist.sock"))
        # Drive the watchdog from the test's own clock; no thread, no sleeping.
        plane._watchdog._clock = clock
        plane._watchdog._last_touch = clock()
        return plane

    def test_silence_past_the_timeout_is_reported_once(self) -> None:
        clock = Clock()
        plane = self._plane(clock)
        seen: list[tuple[float, str]] = []
        plane.set_stall_alarm(lambda idle, what: seen.append((idle, what)), 120.0)

        clock.advance(119.0)
        self.assertFalse(plane._watchdog.check())
        clock.advance(2.0)
        self.assertTrue(plane._watchdog.check())
        self.assertEqual(len(seen), 1)
        self.assertGreaterEqual(seen[0][0], 120.0)

        clock.advance(600.0)
        self.assertFalse(plane._watchdog.check())
        self.assertEqual(len(seen), 1, "an alarm repeating every tick is not read")

    def test_the_timeout_is_configurable(self) -> None:
        clock = Clock()
        plane = self._plane(clock)
        plane.set_stall_alarm(lambda _i, _w: None, 30.0)
        self.assertEqual(plane._watchdog.timeout_seconds, 30.0)
        clock.advance(31.0)
        self.assertTrue(plane._watchdog.check())

    def test_an_operation_re_arms_it(self) -> None:
        clock = Clock()
        plane = self._plane(clock)
        seen: list[tuple[float, str]] = []
        plane.set_stall_alarm(lambda idle, what: seen.append((idle, what)), 60.0)

        clock.advance(61.0)
        plane._watchdog.check()
        plane._watchdog.touch("click")
        clock.advance(59.0)
        self.assertFalse(plane._watchdog.check())
        self.assertEqual(len(seen), 1)

    def test_idle_seconds_is_visible(self) -> None:
        clock = Clock()
        plane = self._plane(clock)
        clock.advance(12.0)
        self.assertAlmostEqual(plane.idle_seconds, 12.0)

    def test_an_alarm_that_raises_does_not_stop_the_plane(self) -> None:
        clock = Clock()
        plane = self._plane(clock)

        def explode(_idle: float, _what: str) -> None:
            raise RuntimeError("broken alarm")

        plane.set_stall_alarm(explode, 10.0)
        clock.advance(11.0)
        self.assertTrue(plane._watchdog.check())  # swallowed, not propagated

    def test_snapshots_are_not_activity(self) -> None:
        """Watching the screen is not the same as doing something to it.

        A job that has frozen usually keeps polling the screen. If a snapshot
        counted as work the alarm would never fire on exactly the failure it
        exists to catch.
        """

        self.assertNotIn("snapshot", PersistentControlPlane.ACTION_COMMANDS)
        self.assertNotIn("status", PersistentControlPlane.ACTION_COMMANDS)
        for command in ("click", "point", "trace", "escape", "type"):
            self.assertIn(command, PersistentControlPlane.ACTION_COMMANDS)


if __name__ == "__main__":
    unittest.main()
