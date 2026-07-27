"""Tests for the control panel's typed situation classification."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

_spec = importlib.util.spec_from_file_location(
    "control_panel_server",
    PROJECT_ROOT / "apps" / "control-panel" / "server.py",
)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


def classify(**overrides):
    base = dict(
        ipc_present=True,
        state="observing",
        last_error_code=None,
        bonjour=None,
        owned=True,
        socket_present=True,
    )
    base.update(overrides)
    return server.classify_situation(**base)


class ClassifySituationTests(unittest.TestCase):
    def test_observing_is_connected(self) -> None:
        self.assertEqual(classify(state="observing"), "connected")
        self.assertEqual(classify(state="acting"), "connected")

    def test_waiting_for_desktop_session_is_named_precisely(self) -> None:
        self.assertEqual(
            classify(state="waiting", last_error_code="desktop_session_inactive"),
            "waiting_desktop_session",
        )

    def test_no_desktop_session_is_never_blamed_on_permissions(self) -> None:
        # Regression: the old heuristic reported permission_blocked whenever
        # the host advertised while the worker was failing, even though the
        # actual cause was an inactive Desktop session.
        self.assertEqual(
            classify(
                state="waiting",
                last_error_code="desktop_session_inactive",
                bonjour=True,
            ),
            "waiting_desktop_session",
        )

    def test_busy_host_is_reported_as_host_busy(self) -> None:
        self.assertEqual(
            classify(state="waiting", last_error_code="host_session_busy"),
            "host_busy",
        )

    def test_invisible_host_with_system_visibility_suspects_permissions(self) -> None:
        self.assertEqual(
            classify(
                state="waiting", last_error_code="no_host_visible", bonjour=True
            ),
            "permission_blocked",
        )
        self.assertEqual(
            classify(
                state="waiting", last_error_code="host_unreachable", bonjour=True
            ),
            "permission_blocked",
        )

    def test_invisible_host_without_system_visibility_waits_for_host(self) -> None:
        self.assertEqual(
            classify(
                state="waiting", last_error_code="no_host_visible", bonjour=False
            ),
            "waiting_host",
        )
        self.assertEqual(
            classify(state="waiting", last_error_code=None, bonjour=None),
            "waiting_host",
        )

    def test_transient_states_are_connecting(self) -> None:
        self.assertEqual(classify(state="starting"), "connecting")
        self.assertEqual(classify(state="recovering"), "connecting")

    def test_terminal_and_process_states(self) -> None:
        self.assertEqual(classify(state="failed"), "failed")
        self.assertEqual(classify(state="stopped"), "stopped")
        self.assertEqual(
            classify(ipc_present=False, owned=True, socket_present=False),
            "starting",
        )
        self.assertEqual(
            classify(ipc_present=False, owned=False, socket_present=False),
            "stopped",
        )
        self.assertEqual(
            classify(ipc_present=False, owned=False, socket_present=True),
            "unknown",
        )


if __name__ == "__main__":
    unittest.main()
