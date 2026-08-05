"""Tests for the single-file chart history store."""

from __future__ import annotations

import importlib.util
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

_spec = importlib.util.spec_from_file_location(
    "control_panel_server_metrics",
    PROJECT_ROOT / "apps" / "control-panel" / "server.py",
)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

NOW = int(time.time())


def look(t: int, perceive=60.0, resolve=5.0) -> dict:
    return {"event": "perceive", "t": t, "perceive_ms": perceive, "resolve_ms": resolve}

def click(t: int, score=0.98, act=200.0) -> dict:
    return {
        "event": "click", "t": t, "element": "x", "score": score,
        "perceive_ms": 70.0, "resolve_ms": 9.0, "act_ms": act,
    }


class MetricsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.store = server.MetricsStore(Path(self._directory.name) / "metrics.db")

    def tearDown(self) -> None:
        self.store.close()
        self._directory.cleanup()

    def ingest(self, job: str, events: list[dict]) -> None:
        rows = []
        for event in events:
            rows.extend(self.store.rows_from_event(job, event))
        self.store.write(rows)

    # ------------------------------------------------------------ semantics

    def test_missing_buckets_read_as_zero_not_as_absence(self) -> None:
        # One look at the start of the window, silence after: the grid is
        # complete and the silence is zeros.
        start = NOW - 600
        self.ingest("a", [look(start + 5)])
        history = self.store.history("a", start, NOW)
        series = history["series"]["perceive"]
        expected = (history["end"] - history["start"]) // history["step"]
        self.assertEqual(len(series), expected)
        # The one sample sits near the head; everything after it is zero.
        self.assertTrue(any(value > 0 for value in series[:6]))
        self.assertEqual(sum(1 for value in series if value > 0), 1)
        self.assertEqual(series[-1], 0.0)
        self.assertEqual(len(history["series"]["cpm"]), expected)

    def test_the_grid_is_aligned_and_described(self) -> None:
        history = self.store.history("a", NOW - 3600, NOW)
        self.assertEqual(history["start"] % history["step"], 0)
        for name in ("perceive", "resolve", "act", "score", "cpm"):
            self.assertEqual(
                len(history["series"][name]),
                (history["end"] - history["start"]) // history["step"],
            )

    def test_values_average_within_a_bucket(self) -> None:
        start = ((NOW - 600) // 10) * 10
        self.ingest("a", [look(start, perceive=40.0), look(start + 1, perceive=80.0)])
        history = self.store.history("a", start, start + 60)
        self.assertEqual(history["series"]["perceive"][0], 60.0)

    def test_clicks_become_a_per_minute_rate(self) -> None:
        start = ((NOW - 300) // 60) * 60
        self.ingest("a", [click(start + i) for i in range(0, 30, 10)])  # 3 clicks
        history = self.store.history("a", start, start + 60)
        step = history["step"]
        total = sum(v * step / 60.0 for v in history["series"]["cpm"])
        self.assertAlmostEqual(total, 3.0, places=3)

    def test_reingesting_the_same_log_does_not_double_anything(self) -> None:
        start = NOW - 600
        events = [look(start + i * 2) for i in range(30)] + [click(start + 40)]
        self.ingest("a", events)
        before = self.store.history("a", start, NOW)
        self.ingest("a", events)  # a cold console re-reads the whole file
        after = self.store.history("a", start, NOW)
        self.assertEqual(before["series"], after["series"])

    def test_jobs_do_not_bleed_into_each_other(self) -> None:
        start = NOW - 600
        self.ingest("a", [look(start + 5, perceive=50.0)])
        self.ingest("b", [look(start + 5, perceive=500.0)])
        a = self.store.history("a", start, NOW)["series"]["perceive"]
        self.assertTrue(all(v <= 50.0 for v in a))

    def test_history_survives_reopening_the_file(self) -> None:
        start = NOW - 600
        self.ingest("a", [look(start + 5)])
        path = self.store.path
        self.store.close()
        reopened = server.MetricsStore(path)
        try:
            series = reopened.history("a", start, NOW)["series"]["perceive"]
            self.assertTrue(any(value > 0 for value in series))
        finally:
            reopened.close()

    def test_points_older_than_retention_are_dropped(self) -> None:
        ancient = NOW - server.MetricsStore.RETENTION_SECONDS - 3600
        self.ingest("a", [look(ancient)])
        self.store._last_retention = 0.0  # force the sweep on the next write
        self.ingest("a", [look(NOW - 10)])
        with self.store._lock:
            remaining = self.store._db.execute(
                "select count(*) from points where t < ?",
                (NOW - server.MetricsStore.RETENTION_SECONDS,),
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_history_ingested_after_the_rollup_advanced_is_still_visible(self) -> None:
        # The stopped-job case: job A ran weeks ago; the store's rollup has
        # long since advanced past those buckets (driven by job B's live
        # queries); only then is A's old log ingested for the first time. A
        # wide window on A must still show it.
        self.ingest("b", [look(NOW - 60)])
        self.store.history("b", NOW - 7 * 24 * 3600, NOW)  # advances the watermark
        week_ago = NOW - 7 * 24 * 3600 + 600
        self.ingest("a", [look(week_ago + i * 2, perceive=75.0) for i in range(100)])
        wide = self.store.history("a", NOW - 14 * 24 * 3600, NOW)
        self.assertGreater(wide["step"], 60)  # wide enough to use the rollup
        self.assertTrue(
            any(v > 0 for v in wide["series"]["perceive"]),
            "backfilled history vanished behind the rollup watermark",
        )

    # ---------------------------------------------------------------- speed

    def test_a_thirty_day_window_answers_fast_from_the_rollup(self) -> None:
        # A month of continuous running, queried at full width: this is the
        # case the rollup exists for, and the reason a page load stays quick.
        day = 24 * 3600
        rows = []
        for d in range(30):
            base = NOW - (30 - d) * day
            rows.extend(
                ("a", "perceive", base + s, 60.0) for s in range(0, day, 20)
            )
        self.store.write(rows)
        t0 = time.perf_counter()
        history = self.store.history("a", NOW - 30 * day, NOW)
        first_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        self.store.history("a", NOW - 30 * day, NOW)
        warm_ms = (time.perf_counter() - t0) * 1000
        buckets = (history["end"] - history["start"]) // history["step"]
        self.assertLessEqual(buckets, server.MetricsStore.TARGET_BUCKETS * 2)
        # First call folds a month into the rollup; after that it must be
        # interactive.
        self.assertLess(first_ms, 5000, f"rollup build took {first_ms:.0f}ms")
        self.assertLess(warm_ms, 150, f"warm 30-day query took {warm_ms:.0f}ms")

    def test_short_windows_answer_from_raw_quickly(self) -> None:
        rows = [("a", "perceive", NOW - 3600 + i * 2, 60.0) for i in range(1800)]
        self.store.write(rows)
        t0 = time.perf_counter()
        history = self.store.history("a", NOW - 3600, NOW)
        ms = (time.perf_counter() - t0) * 1000
        self.assertLess(ms, 100, f"1h query took {ms:.0f}ms")
        self.assertLess(history["step"], server.MetricsStore.ROLLUP_STEP)


class HistoryEndpointShapeTests(unittest.TestCase):
    def test_the_reader_hands_the_store_what_an_event_carries(self) -> None:
        rows = server.MetricsStore.rows_from_event("j", click(NOW))
        series = {row[1] for row in rows}
        self.assertEqual(series, {"perceive", "resolve", "act", "click", "score"})
        rows = server.MetricsStore.rows_from_event("j", look(NOW))
        self.assertEqual({row[1] for row in rows}, {"perceive", "resolve"})
        self.assertEqual(server.MetricsStore.rows_from_event("j", {"event": "doing"}), [])


if __name__ == "__main__":
    unittest.main()
