"""Tests for the job -> console event interface."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from streambot.job_events import JobEvents


class JobEventsTests(unittest.TestCase):
    def _read(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_events_land_where_the_console_reads_them(self) -> None:
        with TemporaryDirectory() as directory:
            events = JobEvents("poly-bridge", jobs_dir=Path(directory))
            events.start()
            self.assertEqual(
                events.path, Path(directory) / "poly-bridge" / "flow-log.jsonl"
            )
            self.assertTrue(events.path.is_file())

    def test_time_is_in_seconds_not_milliseconds(self) -> None:
        # The console computes uptime as int(time.time()) minus this value;
        # milliseconds made it report minus fifty-six thousand years.
        with TemporaryDirectory() as directory:
            events = JobEvents("j", jobs_dir=Path(directory))
            events.start()
            written = self._read(events.path)[0]["t"]
            self.assertLess(abs(written - int(time.time())), 5)

    def test_each_kind_is_recorded_with_its_fields(self) -> None:
        with TemporaryDirectory() as directory:
            events = JobEvents("j", jobs_dir=Path(directory))
            events.start()
            events.doing("building 2-3", members=57)
            events.clicked(x=640, y=420)
            events.cycle(completed=3)
            events.problem("no guide matched", level="2-3")
            kinds = [row["event"] for row in self._read(events.path)]
            self.assertEqual(kinds, ["start", "doing", "click", "cycle", "job-error"])
            doing = self._read(events.path)[1]
            self.assertEqual(doing["what"], "building 2-3")
            self.assertEqual(doing["members"], 57)

    def test_a_log_that_cannot_be_written_does_not_stop_the_job(self) -> None:
        # The log explains the work; it does not gate it.
        events = JobEvents("j", jobs_dir=Path("/proc/nonexistent-and-unwritable"))
        events.start()
        events.doing("still working")


if __name__ == "__main__":
    unittest.main()
