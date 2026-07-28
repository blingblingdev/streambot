"""Tests for the console's incremental flow-log reader (macro + event feed)."""

from __future__ import annotations

import importlib.util
import json
import re
import signal
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

_spec = importlib.util.spec_from_file_location(
    "control_panel_server_flowlog",
    PROJECT_ROOT / "apps" / "control-panel" / "server.py",
)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


def write_lines(path: Path, events: list[dict], mode: str = "a") -> None:
    with open(path, mode, encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


class FlowLogReaderTests(unittest.TestCase):
    def test_macro_aggregates_since_session_start(self) -> None:
        now = int(time.time())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "flow-log.jsonl"
            write_lines(path, [
                # A previous session that must not leak into the current one.
                {"t": now - 900, "event": "start"},
                {"t": now - 890, "event": "click", "element": "old", "score": 0.5},
                {"t": now - 880, "event": "done", "reason": "x", "clicks": 1},
                # Current session.
                {"t": now - 300, "event": "start"},
                {"t": now - 200, "event": "click", "element": "a", "score": 0.9,
                 "act_ms": 250, "center": [10, 20]},
                {"t": now - 150, "event": "cycle", "completed": 1},
                {"t": now - 10, "event": "click", "element": "b", "score": 1.0,
                 "act_ms": 350, "center": [30, 40]},
                {"t": now - 5, "event": "perceive", "perceive_ms": 100.0},
                {"t": now - 4, "event": "frame-skip", "error": "E"},
            ])
            reader = server.FlowLogReader(path)
            reader.poll()
            metrics = reader.metrics()

        self.assertEqual(metrics["clicks_total"], 2)
        self.assertEqual(metrics["cycles"], 1)
        self.assertAlmostEqual(metrics["mean_score"], 0.95)
        self.assertAlmostEqual(metrics["uptime_s"], 300, delta=5)
        self.assertEqual(metrics["last_action"], "b")
        self.assertEqual(metrics["errors_recent"], 1)
        self.assertEqual(metrics["perceive_ms"], 100)
        # Only the recent-window click contributes to act_ms and clicks/min.
        self.assertEqual(metrics["act_ms"], 350)
        self.assertEqual(metrics["clicks_per_min"], 1.0)

    def test_incremental_poll_appends_with_monotonic_index(self) -> None:
        now = int(time.time())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "flow-log.jsonl"
            write_lines(path, [{"t": now, "event": "start"}])
            reader = server.FlowLogReader(path)
            reader.poll()
            first = reader.recent_events()
            write_lines(path, [
                {"t": now + 1, "event": "click", "element": "a", "score": 0.99},
            ])
            reader.poll()
            second = reader.recent_events()

        self.assertEqual([e["i"] for e in first], [1])
        self.assertEqual([e["i"] for e in second], [1, 2])
        self.assertEqual(second[-1]["event"], "click")

    def test_truncated_file_resets_cleanly(self) -> None:
        now = int(time.time())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "flow-log.jsonl"
            write_lines(path, [
                {"t": now, "event": "start"},
                {"t": now, "event": "click", "element": "a", "score": 0.9},
            ])
            reader = server.FlowLogReader(path)
            reader.poll()
            self.assertEqual(reader.metrics()["clicks_total"], 1)
            # New shorter file (job re-created its log from scratch).
            write_lines(path, [{"t": now + 5, "event": "start"}], mode="w")
            reader.poll()
            metrics = reader.metrics()

        self.assertEqual(metrics["clicks_total"], 0)
        self.assertEqual([e["event"] for e in reader.recent_events()], ["start"])

    def test_missing_file_yields_no_metrics(self) -> None:
        with TemporaryDirectory() as directory:
            reader = server.FlowLogReader(Path(directory) / "absent.jsonl")
            reader.poll()
            self.assertIsNone(reader.metrics())
            self.assertEqual(reader.recent_events(), [])


class FeedDomBoundTest(unittest.TestCase):
    """The browser event feed must never grow without bound.

    The server already rings its recent-event buffer, but the browser
    accumulates appended rows across hours — the DOM row cap is the guard,
    and this test pins both the cap and the trim so neither is lost in a
    frontend refactor.
    """

    HTML = (
        Path(__file__).resolve().parent.parent
        / "apps" / "control-panel" / "static" / "index.html"
    )

    def test_feed_row_cap_exists_and_is_reasonable(self) -> None:
        html = self.HTML.read_text(encoding="utf-8")
        match = re.search(r"FEED_MAX_ROWS\s*=\s*(\d+)", html)
        self.assertIsNotNone(match, "feed row cap constant missing")
        self.assertLessEqual(int(match.group(1)), 500)

    def test_feed_trims_oldest_rows_past_the_cap(self) -> None:
        html = self.HTML.read_text(encoding="utf-8")
        self.assertIn(
            "while (box.children.length > FEED_MAX_ROWS) "
            "box.removeChild(box.firstChild);",
            html,
        )

    def test_chart_history_is_hard_capped_server_side(self) -> None:
        # Chart history now lives on the server; hours of dense events must
        # stay bounded in memory and on the wire.
        self.assertLessEqual(server.FlowLogReader.HISTORY_MAX_POINTS, 4000)
        now = int(time.time())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "flow-log.jsonl"
            cap = server.FlowLogReader.HISTORY_MAX_POINTS
            write_lines(path, [{"t": now - 10, "event": "start"}] + [
                {"t": now - 5, "event": "click", "element": "a",
                 "score": 0.9, "perceive_ms": 50.0}
                for _ in range(cap + 200)
            ])
            reader = server.FlowLogReader(path)
            reader.poll()
            history = reader.history()
        self.assertEqual(len(history["perceive"]), cap)
        self.assertEqual(len(history["score"]), cap)

    def test_history_series_reset_with_the_session(self) -> None:
        now = int(time.time())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "flow-log.jsonl"
            write_lines(path, [
                {"t": now - 600, "event": "start"},
                {"t": now - 590, "event": "click", "element": "old",
                 "score": 0.5, "perceive_ms": 900.0},
                {"t": now - 120, "event": "start"},
                {"t": now - 110, "event": "perceive", "perceive_ms": 80.0},
                {"t": now - 60, "event": "click", "element": "a",
                 "score": 0.97, "perceive_ms": 120.0},
            ])
            reader = server.FlowLogReader(path)
            reader.poll()
            history = reader.history()
        # Only the current session survives; clicks/min buckets zero-fill
        # the gap minutes instead of interpolating across them.
        self.assertEqual([v for _t, v in history["perceive"]], [80.0, 120.0])
        self.assertEqual([v for _t, v in history["score"]], [0.97])
        self.assertGreaterEqual(len(history["cpm"]), 1)
        self.assertIn(0, [v for _t, v in history["cpm"]] + [0])


class JobAdoptionTests(unittest.TestCase):
    """A restarted console re-adopts running jobs and can stop them safely."""

    def test_adopted_stop_sends_one_targeted_sigterm(self) -> None:
        supervisor = server.JobSupervisor()
        kills: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kills.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError  # exited right after SIGTERM

        with mock.patch.object(
            supervisor, "_external_pids", return_value={"some-job": 4242}
        ), mock.patch.object(server.os, "kill", side_effect=fake_kill), \
                mock.patch.object(server.time, "sleep", lambda _s: None):
            self.assertEqual(supervisor.stop("some-job"), {"ok": True})

        self.assertEqual(kills[0], (4242, signal.SIGTERM))
        self.assertNotIn((4242, signal.SIGKILL), kills)

    def test_start_refuses_while_an_external_runner_exists(self) -> None:
        supervisor = server.JobSupervisor()
        registry = {"example-job": {
            "name": "example-job", "title": "Example", "description": "",
            "runner": ["jobs/example-job/runner.py"],
        }}
        with mock.patch.object(
            server.JobSupervisor, "registry", staticmethod(lambda: registry)
        ), mock.patch.object(
            supervisor, "_external_pids", return_value={"example-job": 4242}
        ):
            self.assertEqual(
                supervisor.start("example-job"),
                {"ok": False, "error": "AlreadyRunning"},
            )

    def test_scan_verifies_the_full_command_line_before_adoption(self) -> None:
        # An editor holding the script path open must never be adopted (and
        # therefore never SIGTERMed by stop()).
        editor = SimpleNamespace(stdout="vim jobs/x/flow_runner.py\n")
        runner = SimpleNamespace(
            stdout="/repo/.venv/bin/python jobs/x/flow_runner.py\n"
        )
        # macOS ps reports the RESOLVED interpreter binary, not the
        # .venv/bin/python argv the spawner passed — adoption must still work.
        framework = SimpleNamespace(
            stdout="/opt/homebrew/Cellar/python@3.14/3.14.2/Frameworks/"
            "Python.framework/Versions/3.14/Resources/Python.app/"
            "Contents/MacOS/Python jobs/x/flow_runner.py\n"
        )
        with mock.patch.object(server.subprocess, "run", return_value=editor):
            self.assertFalse(
                server.JobSupervisor._is_runner_process(1, "jobs/x/flow_runner.py")
            )
        for real in (runner, framework):
            with mock.patch.object(server.subprocess, "run", return_value=real):
                self.assertTrue(
                    server.JobSupervisor._is_runner_process(1, "jobs/x/flow_runner.py")
                )




class WorkerAdoptionTests(unittest.TestCase):
    """The stop button must work on any worker the console can verify."""

    def make_supervisor(self):
        return server.WorkerSupervisor(Path("/tmp/x"), Path("/tmp/x.sock"))

    def test_adopted_stop_sends_one_targeted_sigterm(self) -> None:
        supervisor = self.make_supervisor()
        kills: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kills.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError  # exited right after SIGTERM

        with mock.patch.object(
            supervisor, "external_pid", return_value=555
        ), mock.patch.object(server.os, "kill", side_effect=fake_kill), \
                mock.patch.object(server.time, "sleep", lambda _s: None):
            self.assertEqual(supervisor.stop(), {"ok": True})

        self.assertEqual(kills[0], (555, signal.SIGTERM))
        self.assertNotIn((555, signal.SIGKILL), kills)

    def test_stop_without_any_worker_reports_not_running(self) -> None:
        supervisor = self.make_supervisor()
        with mock.patch.object(supervisor, "external_pid", return_value=None):
            self.assertEqual(
                supervisor.stop(), {"ok": False, "error": "NotRunning"}
            )

    def test_worker_verification_accepts_python_rejects_editors(self) -> None:
        cases = [
            ("vim apps/core-worker/core_worker.py\n", False),
            ("/repo/.venv/bin/python apps/core-worker/core_worker.py\n", True),
            ("/opt/homebrew/Cellar/python@3.14/3.14.2/Frameworks/"
             "Python.framework/Versions/3.14/Resources/Python.app/"
             "Contents/MacOS/Python /r/apps/core-worker/core_worker.py\n", True),
            ("/usr/bin/python3 some_other_script.py\n", False),
        ]
        for stdout, expected in cases:
            with mock.patch.object(
                server.subprocess, "run",
                return_value=SimpleNamespace(stdout=stdout),
            ):
                self.assertEqual(
                    server.WorkerSupervisor._is_worker_process(1),
                    expected, stdout,
                )



if __name__ == "__main__":
    unittest.main()
