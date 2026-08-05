"""Tests for the system-event narration derived from status snapshots."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

_spec = importlib.util.spec_from_file_location(
    "control_panel_server_syslog",
    PROJECT_ROOT / "apps" / "control-panel" / "server.py",
)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


def snapshot(
    *,
    pid=None,
    owned=True,
    socket=False,
    state=None,
    reconnects=0,
    ipc_error=None,
    error=None,
    code=None,
):
    return {
        "worker": {"pid": pid, "owned_by_console": owned, "socket_present": socket},
        "connection": {
            "state": state,
            "reconnects": reconnects,
            "ipc_error": ipc_error,
            "last_error_type": error,
            "last_error_code": code,
        },
    }


class SystemEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events = server.SystemEvents()

    def texts(self) -> list[str]:
        return [event["text"] for event in self.events.tail()]

    def test_the_first_snapshot_narrates_only_running_jobs(self) -> None:
        # There is no "before" yet, so nothing has changed — but a job already
        # running when the console opens is worth knowing about.
        self.events.observe(snapshot(pid=7, socket=True, state="observing"),
                            [{"name": "a", "running": True, "pid": 42}])
        self.assertEqual(self.texts(), ["job a started · pid 42"])

    def test_a_steady_state_produces_nothing(self) -> None:
        steady = snapshot(pid=7, socket=True, state="observing", reconnects=2)
        self.events.observe(steady, [])
        self.events.observe(steady, [])
        self.events.observe(steady, [])
        self.assertEqual(self.texts(), [])

    def test_the_stream_dropping_and_recovering_is_narrated(self) -> None:
        self.events.observe(snapshot(pid=7, socket=True, state="observing"), [])
        self.events.observe(snapshot(pid=7, socket=True, state="recovering"), [])
        self.events.observe(
            snapshot(pid=7, socket=True, state="recovering", reconnects=1), []
        )
        self.events.observe(
            snapshot(pid=7, socket=True, state="observing", reconnects=1), []
        )
        self.assertEqual(
            self.texts(),
            [
                "stream observing → recovering",
                "stream reconnect #1",
                "stream recovering → observing",
            ],
        )

    def test_worker_lifecycle_distinguishes_started_from_adopted(self) -> None:
        self.events.observe(snapshot(), [])
        self.events.observe(snapshot(pid=7, owned=True, socket=True), [])
        self.events.observe(snapshot(), [])
        self.events.observe(snapshot(pid=9, owned=False, socket=True), [])
        self.assertEqual(
            self.texts(),
            [
                "worker started · pid 7",
                "IPC socket up",
                "worker exited (was pid 7)",
                "IPC socket gone",
                "worker adopted · pid 9",
                "IPC socket up",
            ],
        )

    def test_ipc_silence_and_recovery_are_narrated(self) -> None:
        self.events.observe(snapshot(pid=7, socket=True, state="observing"), [])
        self.events.observe(
            snapshot(pid=7, socket=True, state="observing", ipc_error="TimeoutError"),
            [],
        )
        self.events.observe(snapshot(pid=7, socket=True, state="observing"), [])
        self.assertEqual(
            self.texts(), ["IPC not responding (TimeoutError)", "IPC recovered"]
        )

    def test_a_worker_error_is_reported_once_with_its_code(self) -> None:
        self.events.observe(snapshot(pid=7, state="recovering"), [])
        errored = snapshot(
            pid=7, state="recovering", error="ConnectFailure", code="host_unreachable"
        )
        self.events.observe(errored, [])
        self.events.observe(errored, [])
        self.assertEqual(
            self.texts(), ["worker error: ConnectFailure (host_unreachable)"]
        )

    def test_job_start_and_stop_are_narrated(self) -> None:
        self.events.observe(snapshot(pid=7), [])
        self.events.observe(snapshot(pid=7), [{"name": "a", "running": True, "pid": 42}])
        self.events.observe(snapshot(pid=7), [])
        self.assertEqual(
            self.texts(), ["job a started · pid 42", "job a stopped"]
        )

    def test_the_ring_stays_bounded(self) -> None:
        for index in range(server.SystemEvents.MAX_EVENTS + 50):
            self.events.observe(snapshot(pid=7, state=f"s{index}"), [])
        self.assertLessEqual(len(self.events.tail(10_000)), server.SystemEvents.MAX_EVENTS)


if __name__ == "__main__":
    unittest.main()
