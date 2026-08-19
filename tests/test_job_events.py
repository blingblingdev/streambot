"""Tests for the job -> console event interface."""

from __future__ import annotations

import json
import os
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

    def test_an_oversized_log_rotates_when_a_new_session_starts(self) -> None:
        # The console's store keeps the thirty-day archive; the jsonl is
        # transport. A new session on a bloated file starts clean — and only
        # a session boundary may truncate, so the console never re-reads old
        # lines under new numbering.
        with TemporaryDirectory() as directory:
            events = JobEvents("j", jobs_dir=Path(directory))
            events.start()
            events.doing("filling the log")
            with open(events.path, "a", encoding="utf-8") as handle:
                handle.write("x" * (JobEvents.ROTATE_BYTES + 1) + "\n")
            events.start()
            rows = self._read(events.path)
        self.assertEqual([row["event"] for row in rows], ["start"])

    def test_a_modest_log_accumulates_across_sessions(self) -> None:
        with TemporaryDirectory() as directory:
            events = JobEvents("j", jobs_dir=Path(directory))
            events.start()
            events.cycle(completed=1)
            events.start()
            kinds = [row["event"] for row in self._read(events.path)]
        self.assertEqual(kinds, ["start", "cycle", "start"])

    def test_a_log_that_cannot_be_written_does_not_stop_the_job(self) -> None:
        # The log explains the work; it does not gate it.
        events = JobEvents("j", jobs_dir=Path("/proc/nonexistent-and-unwritable"))
        events.start()
        events.doing("still working")

    def test_notification_event_is_bounded_and_strict(self) -> None:
        with TemporaryDirectory() as directory:
            events = JobEvents("j", jobs_dir=Path(directory))
            event_id = events.notification("completed", {"count": 3})
            self.assertIsNotNone(event_id)
            row = self._read(events.path)[0]
            self.assertEqual(row["event"], "notification")
            self.assertEqual(row["notification_kind"], "completed")
            self.assertEqual(row["event_id"], event_id)
            self.assertEqual(row["data"], {"count": 3})
            self.assertIsNone(events.notification("Invalid.Kind", {}))
            self.assertIsNone(events.notification("completed", {"raw": b"bytes"}))
            self.assertIsNone(
                events.notification("completed", {"value": "x" * (20 * 1024)})
            )

    def test_artifact_spool_is_private_and_ack_requires_terminal_success(self) -> None:
        with TemporaryDirectory() as directory:
            previous = os.environ.get("STREAMBOT_NOTIFICATION_DIR")
            os.environ["STREAMBOT_NOTIFICATION_DIR"] = directory
            try:
                events = JobEvents("j", jobs_dir=Path(directory) / "jobs")
                artifact_id = events.store_notification_artifact(b"jpeg", "image/jpeg")
                self.assertIsNotNone(artifact_id)
                artifact_dir = Path(directory) / "artifacts"
                metadata = json.loads(
                    (artifact_dir / f"{artifact_id}.json").read_text(encoding="utf-8")
                )
                data_path = artifact_dir / metadata["filename"]
                self.assertEqual(data_path.read_bytes(), b"jpeg")
                self.assertEqual(data_path.stat().st_mode & 0o777, 0o600)

                event_id = events.notification(
                    "completed", {"count": 3}, artifact_id=artifact_id
                )
                self.assertFalse(events.notification_confirmed(event_id or ""))
                ack_dir = Path(directory) / "acks"
                ack_dir.mkdir(mode=0o700)
                (ack_dir / f"{event_id}.json").write_text(
                    json.dumps({"event_id": event_id, "state": "failed"}),
                    encoding="utf-8",
                )
                self.assertFalse(events.notification_confirmed(event_id or ""))
                (ack_dir / f"{event_id}.json").write_text(
                    json.dumps({"event_id": event_id, "state": "succeeded"}),
                    encoding="utf-8",
                )
                self.assertTrue(events.notification_confirmed(event_id or ""))
            finally:
                if previous is None:
                    os.environ.pop("STREAMBOT_NOTIFICATION_DIR", None)
                else:
                    os.environ["STREAMBOT_NOTIFICATION_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
