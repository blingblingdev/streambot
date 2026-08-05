"""Tests for worker-side element registration, analysis and the audit record."""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import numpy as np

from streambot.control_plane import PersistentControlPlane, send_control_command
from streambot.observation import Observation
from streambot.operations import OperationJournal

TEMPLATE_SIZE = 24


def control(tint: tuple[int, int, int]) -> np.ndarray:
    b, g, r = tint
    template = np.zeros((TEMPLATE_SIZE, TEMPLATE_SIZE, 3), dtype=np.uint8)
    template[:, :] = (b // 4, g // 4, r // 4)
    template[10:14, 2:22] = (b, g, r)
    template[2:22, 10:14] = (b, g, r)
    return template


GOLD = (40, 180, 220)


def frame_with_control(x: int = 100, y: int = 50) -> np.ndarray:
    frame = np.full((200, 320, 3), 12, dtype=np.uint8)
    patch = control(GOLD)
    frame[y : y + TEMPLATE_SIZE, x : x + TEMPLATE_SIZE] = patch
    return frame


def write_target(directory: Path) -> Path:
    """A minimal recorded target: one screen, one element, one template."""

    assets = directory / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    np.save(assets / "start.npy", control(GOLD), allow_pickle=False)
    declaration = {
        "schema_version": 1,
        "screens": {"home": {"anchors": [{"template": "start", "y_band": [40, 80]}]}},
        "elements": {"start": {"template": "start", "screen": "home", "y_band": [40, 80]}},
    }
    path = directory / "elements.json"
    path.write_text(json.dumps(declaration), encoding="utf-8")
    return path


class PlaneFixture:
    """Shared setup. Not a TestCase, so its tests are not collected twice."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.declaration = write_target(self.root / "job")
        self.journal_path = self.root / "operations.jsonl"
        self.plane = PersistentControlPlane(
            self.root / "control.sock",
            journal=OperationJournal(self.journal_path),
        )
        self.plane.start()
        self.plane.publish_observation(
            Observation(7, time.monotonic(), frame_with_control())
        )

    def tearDown(self) -> None:
        self.plane.close()
        self._directory.cleanup()

    def _send(self, command: str, **kwargs):
        return send_control_command(self.plane.socket_path, command, **kwargs)

    def _register(self, job: str = "demo"):
        return self._send(
            "register-elements",
            job=job,
            arguments={"declaration_path": str(self.declaration)},
        )

    def _journal(self) -> list[dict]:
        if not self.journal_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.journal_path.read_text(encoding="utf-8").splitlines()
            if line
        ]


class AnalyzeCommandTests(PlaneFixture, unittest.TestCase):
    def test_registration_reports_what_was_adopted(self) -> None:
        response = self._register()
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["elements"], ["start"])
        self.assertEqual(response["screens"], ["home"])
        self.assertEqual(response["templates"], 1)

    def test_registration_requires_a_job_name(self) -> None:
        response = self._send(
            "register-elements", arguments={"declaration_path": str(self.declaration)}
        )
        self.assertEqual(response["error"], "MissingJob")

    def test_malformed_declaration_is_refused_at_registration(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"screens": {}, "elements": {}}), encoding="utf-8")
        response = self._send(
            "register-elements", job="demo", arguments={"declaration_path": str(bad)}
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "InvalidDeclaration")
        self.assertEqual(
            [entry["op"] for entry in self._journal()], ["register"]
        )
        self.assertFalse(self._journal()[0]["ok"])

    def test_analyze_before_registration_fails_closed(self) -> None:
        response = self._send("analyze", job="demo")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "NoElementsRegistered")

    def test_analyze_locates_the_control_and_reports_its_cost(self) -> None:
        self._register()
        response = self._send("analyze", job="demo")
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["screen"], "home")
        self.assertEqual(response["frame_number"], 7)
        self.assertEqual(
            [instance["center"] for instance in response["instances"]], [[112, 62]]
        )
        self.assertGreaterEqual(response["classify_ms"], 0.0)
        self.assertGreaterEqual(response["resolve_ms"], 0.0)

    def test_analyze_rejects_an_oversized_element_list(self) -> None:
        self._register()
        response = self._send(
            "analyze", job="demo", arguments={"elements": ["start"] * 64}
        )
        self.assertEqual(response["error"], "TooManyElements")

    def test_analyze_rejects_an_unknown_element(self) -> None:
        self._register()
        response = self._send("analyze", job="demo", arguments={"elements": ["nope"]})
        self.assertEqual(response["error"], "InvalidRequest")

    def test_registration_is_scoped_to_the_job_that_registered(self) -> None:
        self._register(job="demo")
        self.assertEqual(
            self._send("analyze", job="other")["error"], "NoElementsRegistered"
        )

    def test_registrations_do_not_survive_the_session(self) -> None:
        # Session state, deliberately: a worker that has been restarted must
        # not still be matching against templates a job has since re-recorded.
        self._register()
        self.assertEqual(self.plane.registered_jobs(), ["demo"])
        self.plane.close()
        self.assertEqual(self.plane.registered_jobs(), [])

    def test_a_new_session_starts_with_nothing_registered(self) -> None:
        self._register()
        fresh = PersistentControlPlane(self.root / "fresh.sock")
        fresh.start()
        try:
            self.assertEqual(fresh.registered_jobs(), [])
            self.assertEqual(
                send_control_command(fresh.socket_path, "analyze", job="demo")["error"],
                "NoElementsRegistered",
            )
        finally:
            fresh.close()

    def test_looking_never_counts_as_activity(self) -> None:
        # A job frozen while polling the screen must still trip the stall
        # alarm, so neither analysis nor registration may touch the watchdog.
        self._register()
        idle_before = self.plane.idle_seconds
        time.sleep(0.05)
        self._send("analyze", job="demo")
        self.assertGreaterEqual(self.plane.idle_seconds, idle_before)

    def test_analysis_does_not_delay_a_concurrent_status(self) -> None:
        # The design rests on this: analysis runs on the calling connection's
        # own thread, so it can never make the console's status wait.
        self._register()
        big = np.full((1200, 1900, 3), 12, dtype=np.uint8)
        big[600:624, 900:924] = control(GOLD)
        self.plane.publish_observation(Observation(9, time.monotonic(), big))

        done: list[float] = []

        def analyze() -> None:
            started = time.perf_counter()
            self._send("analyze", job="demo")
            done.append(time.perf_counter() - started)

        worker = Thread(target=analyze)
        worker.start()
        try:
            started = time.perf_counter()
            status = send_control_command(self.plane.socket_path, "status")
            status_seconds = time.perf_counter() - started
        finally:
            worker.join(timeout=10.0)
        self.assertTrue(status["ok"])
        self.assertLess(status_seconds, 1.0, "status waited on the analysis")


class OperationRecordTests(PlaneFixture, unittest.TestCase):
    def test_every_operation_is_recorded_with_its_job_and_cost(self) -> None:
        self._register()
        self._send("analyze", job="demo")
        entries = self._journal()
        self.assertEqual([entry["op"] for entry in entries], ["register", "analyze"])
        for entry in entries:
            self.assertEqual(entry["job"], "demo")
            self.assertTrue(entry["ok"])
            self.assertIn("ms", entry)
        analysis = entries[-1]
        self.assertEqual(analysis["screen"], "home")
        self.assertEqual(analysis["found"][0]["element"], "start")
        self.assertEqual(analysis["found"][0]["center"], [112, 62])
        self.assertEqual(analysis["frame_number"], 7)

    def test_a_look_is_recorded_as_an_observation(self) -> None:
        plane = PersistentControlPlane(
            self.root / "export.sock",
            allow_frame_export=True,
            journal=OperationJournal(self.journal_path),
        )
        plane.start()
        try:
            plane.publish_observation(
                Observation(11, time.monotonic(), frame_with_control())
            )
            response = send_control_command(
                plane.socket_path,
                "snapshot",
                job="demo",
                arguments={"output": str(self.root / "frame.jpg")},
            )
        finally:
            plane.close()
        self.assertTrue(response["ok"])
        observe = [entry for entry in self._journal() if entry["op"] == "observe"]
        self.assertEqual(len(observe), 1)
        self.assertEqual(observe[0]["frame_number"], 11)

    def test_the_record_cannot_carry_pixels(self) -> None:
        journal = OperationJournal(self.root / "guard.jsonl")
        journal.record(
            "analyze",
            job="demo",
            frame=np.zeros((4, 4, 3), dtype=np.uint8),
            blob=b"\x00\x01",
            screen="home",
        )
        line = json.loads((self.root / "guard.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(line["screen"], "home")
        self.assertNotIn("frame", line)
        self.assertNotIn("blob", line)

    def test_a_failed_write_never_reaches_the_caller(self) -> None:
        # The journal explains the work; it must never be what stops it.
        journal = OperationJournal(self.root / "nope" / "x" / "deep.jsonl")
        journal.path = Path("/proc/definitely/not/writable.jsonl")
        journal.record("analyze", job="demo")


if __name__ == "__main__":
    unittest.main()
