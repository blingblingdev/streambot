"""Tests for the target-agnostic control plane platform component."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest

import numpy as np

from streambot.control_plane import (
    DEFAULT_SOCKET_PATH,
    PersistentControlPlane,
    send_control_command,
)
from streambot.observation import Observation


class FakeInputs:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def execute(self, action: str, key: str) -> None:
        self.calls.append(("execute", action, key))

    def execute_position(self, x: int, y: int, key: str) -> None:
        self.calls.append(("position", x, y, key))

    def execute_glide(self, x: int, y: int, key: str) -> None:
        self.calls.append(("glide", x, y, key))

    def execute_text(self, text: str, key: str) -> None:
        self.calls.append(("text", text, key))


class ControlPlaneTests(unittest.TestCase):
    def test_default_socket_path_is_generic(self) -> None:
        path = str(DEFAULT_SOCKET_PATH)
        self.assertTrue(path.endswith("control.sock"))
        self.assertIn(".state", path)

    def test_status_reports_latest_frame_and_page_state(self) -> None:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            plane.start()
            try:
                frame = np.zeros((8, 12, 3), dtype=np.uint8)
                plane.publish_observation(Observation(21, time.monotonic(), frame))
                plane.publish_page_state(
                    21,
                    {
                        "primary_layout": "phone-reply-prompt",
                        "matches": ["phone-reply-prompt"],
                        "actionable": True,
                    },
                )
                status = send_control_command(plane.socket_path, "status")
            finally:
                plane.close()
        self.assertTrue(status["ok"])
        self.assertEqual(status["frame_number"], 21)
        self.assertEqual(status["page_state"]["primary_layout"], "phone-reply-prompt")

    def test_controls_query_omits_coordinates(self) -> None:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            plane.start()
            try:
                plane.publish_page_state(
                    9,
                    {
                        "primary_layout": "pause-menu",
                        "recommended_control_id": "go",
                        "controls": [
                            {"control_id": "go", "action_kind": "click", "x": 100, "y": 200, "confidence": 1.0},
                        ],
                    },
                )
                result = send_control_command(plane.socket_path, "controls")
            finally:
                plane.close()
        self.assertTrue(result["ok"])
        self.assertEqual(result["recommended_control_id"], "go")
        self.assertEqual(len(result["controls"]), 1)
        control = result["controls"][0]
        self.assertEqual(control["control_id"], "go")
        self.assertEqual(control["action_kind"], "click")
        self.assertNotIn("x", control)
        self.assertNotIn("y", control)

    def test_dispatch_named_control_clicks_its_point(self) -> None:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            inputs = FakeInputs()
            plane.start()
            plane.publish_page_state(
                9,
                {
                    "primary_layout": "pause-menu",
                    "controls": [
                        {"control_id": "go", "action_kind": "click", "x": 100, "y": 200, "confidence": 1.0},
                    ],
                },
            )
            response: dict[str, object] = {}

            def request() -> None:
                response.update(
                    send_control_command(
                        plane.socket_path, "dispatch", arguments={"control_id": "go"}
                    )
                )

            thread = Thread(target=request)
            thread.start()
            deadline = time.monotonic() + 1.0
            while thread.is_alive() and time.monotonic() < deadline:
                plane.execute_pending(inputs)
                time.sleep(0.005)
            thread.join(timeout=1.0)
            try:
                self.assertTrue(response["ok"])
                self.assertEqual(response["control_id"], "go")
                self.assertEqual(inputs.calls[0][:3], ("glide", 100, 200))
                self.assertEqual(inputs.calls[1][1], "click")
            finally:
                plane.close()

    def test_dispatch_unknown_control_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            inputs = FakeInputs()
            plane.start()
            plane.publish_page_state(9, {"primary_layout": "pause-menu", "controls": []})
            response: dict[str, object] = {}

            def request() -> None:
                response.update(
                    send_control_command(
                        plane.socket_path, "dispatch", arguments={"control_id": "missing"}
                    )
                )

            thread = Thread(target=request)
            thread.start()
            deadline = time.monotonic() + 1.0
            while thread.is_alive() and time.monotonic() < deadline:
                plane.execute_pending(inputs)
                time.sleep(0.005)
            thread.join(timeout=1.0)
            try:
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"], "UnknownControl")
                self.assertEqual(inputs.calls, [])
            finally:
                plane.close()

    def test_click_command_is_serialized_to_the_input_owner(self) -> None:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            inputs = FakeInputs()
            plane.start()
            response: dict[str, object] = {}

            def request() -> None:
                response.update(
                    send_control_command(
                        plane.socket_path, "click", arguments={"x": 100, "y": 200}
                    )
                )

            thread = Thread(target=request)
            thread.start()
            deadline = time.monotonic() + 1.0
            while thread.is_alive() and time.monotonic() < deadline:
                plane.execute_pending(inputs)
                time.sleep(0.005)
            thread.join(timeout=1.0)
            try:
                self.assertTrue(response["ok"])
                self.assertEqual(inputs.calls[0][:3], ("glide", 100, 200))
                self.assertEqual(inputs.calls[1][1], "click")
                self.assertEqual(plane.status()["commands_completed"], 1)
            finally:
                plane.close()


    def test_connect_and_disconnect_forward_to_the_worker(self) -> None:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            calls: list[str] = []
            plane.set_connection_controls(
                lambda: calls.append("detach"), lambda: calls.append("attach")
            )
            plane.start()
            try:
                disconnect = send_control_command(plane.socket_path, "disconnect")
                connect = send_control_command(plane.socket_path, "connect")
            finally:
                plane.close()
        self.assertTrue(disconnect["ok"])
        self.assertTrue(connect["ok"])
        self.assertEqual(calls, ["detach", "attach"])

    def test_connection_commands_fail_closed_when_unwired(self) -> None:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            plane.start()
            try:
                response = send_control_command(plane.socket_path, "disconnect")
            finally:
                plane.close()
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "ConnectionControlUnavailable")


if __name__ == "__main__":
    unittest.main()


class TraceCommandTests(unittest.TestCase):
    """A path, not a straight line: one press, many moves, one release."""

    def _run(self, arguments: dict) -> tuple[dict, list]:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            inputs = FakeInputs()
            plane.start()
            response: dict[str, object] = {}

            def request() -> None:
                response.update(
                    send_control_command(plane.socket_path, "trace", arguments=arguments)
                )

            thread = Thread(target=request)
            thread.start()
            deadline = time.monotonic() + 5.0
            while thread.is_alive() and time.monotonic() < deadline:
                plane.execute_pending(inputs)
                time.sleep(0.005)
            thread.join(timeout=2.0)
            plane.close()
            return response, inputs.calls

    def test_the_button_is_held_for_the_whole_path(self) -> None:
        response, calls = self._run(
            {"points": [[10, 10], [20, 30], [40, 25]], "duration_seconds": 0.2}
        )
        self.assertTrue(response["ok"])
        actions = [call for call in calls if call[0] == "execute"]
        self.assertEqual([call[1] for call in actions], ["mouse-down", "mouse-up"])
        moves = [call for call in calls if call[0] == "position"]
        self.assertEqual([(call[1], call[2]) for call in moves], [(10, 10), (20, 30), (40, 25)])

    def test_a_path_needs_at_least_two_points(self) -> None:
        response, calls = self._run({"points": [[10, 10]]})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "TooFewPoints")
        self.assertEqual([call for call in calls if call[0] == "execute"], [])


class DoubleClickTests(unittest.TestCase):
    """Two clicks at one point, close enough to read as one gesture."""

    def test_it_glides_once_and_clicks_twice(self) -> None:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            inputs = FakeInputs()
            plane.start()
            response: dict[str, object] = {}

            def request() -> None:
                response.update(
                    send_control_command(
                        plane.socket_path,
                        "double-click",
                        arguments={"x": 40, "y": 50, "gap_seconds": 0.02},
                    )
                )

            thread = Thread(target=request)
            thread.start()
            deadline = time.monotonic() + 3.0
            while thread.is_alive() and time.monotonic() < deadline:
                plane.execute_pending(inputs)
                time.sleep(0.005)
            thread.join(timeout=1.0)
            plane.close()

        self.assertTrue(response["ok"])
        self.assertEqual(inputs.calls[0][:3], ("glide", 40, 50))
        self.assertEqual([c[1] for c in inputs.calls if c[0] == "execute"], ["click", "click"])


class TypeCommandTests(unittest.TestCase):
    """Typing exists because an address cannot be clicked."""

    def test_each_character_is_a_key_down_and_up(self) -> None:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            inputs = FakeInputs()
            plane.start()
            response: dict[str, object] = {}

            def request() -> None:
                response.update(
                    send_control_command(
                        plane.socket_path, "type", arguments={"text": "ab1"}
                    )
                )

            thread = Thread(target=request)
            thread.start()
            deadline = time.monotonic() + 3.0
            while thread.is_alive() and time.monotonic() < deadline:
                plane.execute_pending(inputs)
                time.sleep(0.005)
            thread.join(timeout=1.0)
            plane.close()

        self.assertTrue(response["ok"])
        self.assertEqual([c[1] for c in inputs.calls], ["ab1"])


class SlowInputs(FakeInputs):
    """Inputs whose glide outlasts the requester's patience."""

    def execute_glide(self, x: int, y: int, key: str) -> None:
        time.sleep(0.4)
        super().execute_glide(x, y, key)


class CommandTimeoutTests(unittest.TestCase):
    """A timed-out action must never quietly execute later.

    The requester is told CommandTimeout and will retry; if the abandoned
    command then runs anyway, the gesture lands twice on the host.
    """

    def test_a_command_abandoned_while_queued_is_never_executed(self) -> None:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            plane.action_wait_seconds = 0.15
            inputs = FakeInputs()
            plane.start()
            try:
                # No executor drains the queue, so the command sits queued
                # past the wait and the requester is told CommandTimeout.
                response = send_control_command(
                    plane.socket_path, "click", arguments={"x": 10, "y": 20}
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"], "CommandTimeout")
                # Draining afterwards must skip it, not click.
                plane.execute_pending(inputs)
                self.assertEqual(inputs.calls, [])
            finally:
                plane.close()
            journal = (Path(directory) / "operations.jsonl").read_text()
            self.assertIn("CommandTimeout", journal)
            self.assertIn("AbandonedBeforeExecution", journal)

    def test_a_command_still_running_at_timeout_journals_its_outcome(self) -> None:
        with TemporaryDirectory() as directory:
            plane = PersistentControlPlane(Path(directory) / "control.sock")
            plane.action_wait_seconds = 0.15
            inputs = SlowInputs()
            plane.start()
            plane.start_executor(inputs)
            try:
                response = send_control_command(
                    plane.socket_path, "click", arguments={"x": 10, "y": 20}
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"], "CommandTimeout")
                # The gesture cannot be recalled mid-flight; it completes.
                deadline = time.monotonic() + 2.0
                while not inputs.calls and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(inputs.calls[0][:3], ("glide", 10, 20))
            finally:
                plane.close()
            # The audit trail records both the timeout verdict the requester
            # saw and what actually happened afterwards.
            journal = (Path(directory) / "operations.jsonl").read_text()
            self.assertIn("CommandTimeout", journal)
            self.assertIn("late_completion", journal)
