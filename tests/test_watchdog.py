"""The stall watchdog: silence becomes an event, and only once."""

from __future__ import annotations

import unittest

from streambot.watchdog import (
    DEFAULT_TIMEOUT_SECONDS,
    ENV_TIMEOUT,
    StallWatchdog,
    configured_timeout,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StallWatchdogTests(unittest.TestCase):
    def _watchdog(self, timeout: float = 60.0):
        clock = FakeClock()
        fired: list[tuple[float, str]] = []
        watchdog = StallWatchdog(
            timeout_seconds=timeout,
            on_stall=lambda idle, what: fired.append((idle, what)),
            clock=clock,
        )
        return watchdog, clock, fired

    def test_quiet_shorter_than_the_timeout_is_not_a_stall(self) -> None:
        watchdog, clock, fired = self._watchdog(60.0)
        clock.advance(59.0)
        self.assertFalse(watchdog.check())
        self.assertEqual(fired, [])

    def test_silence_past_the_timeout_fires_once(self) -> None:
        watchdog, clock, fired = self._watchdog(60.0)
        clock.advance(61.0)
        self.assertTrue(watchdog.check())
        self.assertEqual(len(fired), 1)
        idle, what = fired[0]
        self.assertGreaterEqual(idle, 60.0)
        self.assertEqual(what, "start")

        # Still silent, still fired: an alarm that repeats every tick is an
        # alarm people stop reading.
        clock.advance(300.0)
        self.assertFalse(watchdog.check())
        self.assertEqual(len(fired), 1)

    def test_work_re_arms_it(self) -> None:
        watchdog, clock, fired = self._watchdog(60.0)
        clock.advance(61.0)
        watchdog.check()
        watchdog.touch("click")
        clock.advance(30.0)
        self.assertFalse(watchdog.check())
        clock.advance(31.0)
        self.assertTrue(watchdog.check())
        self.assertEqual(len(fired), 2)
        self.assertEqual(fired[1][1], "click")

    def test_touching_keeps_it_quiet_indefinitely(self) -> None:
        watchdog, clock, fired = self._watchdog(60.0)
        for _ in range(20):
            clock.advance(30.0)
            watchdog.touch("draw")
            self.assertFalse(watchdog.check())
        self.assertEqual(fired, [])

    def test_a_callback_that_raises_does_not_stop_the_job(self) -> None:
        clock = FakeClock()

        def explode(_idle: float, _what: str) -> None:
            raise RuntimeError("the alarm itself is broken")

        watchdog = StallWatchdog(timeout_seconds=10.0, on_stall=explode, clock=clock)
        clock.advance(11.0)
        self.assertTrue(watchdog.check())  # reported, and did not propagate

    def test_a_zero_timeout_disarms_it(self) -> None:
        watchdog, clock, fired = self._watchdog(0.0)
        self.assertFalse(watchdog.armed)
        clock.advance(10_000.0)
        self.assertFalse(watchdog.check())
        self.assertEqual(fired, [])

    def test_idle_seconds_reports_the_gap(self) -> None:
        watchdog, clock, _fired = self._watchdog(60.0)
        clock.advance(42.0)
        self.assertAlmostEqual(watchdog.idle_seconds(), 42.0)


class ConfiguredTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = None

    def tearDown(self) -> None:
        import os

        os.environ.pop(ENV_TIMEOUT, None)

    def test_default_when_unset(self) -> None:
        import os

        os.environ.pop(ENV_TIMEOUT, None)
        self.assertEqual(configured_timeout(), DEFAULT_TIMEOUT_SECONDS)

    def test_environment_sets_it(self) -> None:
        import os

        os.environ[ENV_TIMEOUT] = "45"
        self.assertEqual(configured_timeout(), 45.0)

    def test_nonsense_falls_back_rather_than_arming_wrongly(self) -> None:
        import os

        os.environ[ENV_TIMEOUT] = "soon"
        self.assertEqual(configured_timeout(), DEFAULT_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
