"""Regression tests for bounded probe result contracts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import live_probe


class PairPhaseTests(unittest.TestCase):
    def test_manual_pair_phase_returns_sanitized_success_metadata(self) -> None:
        class FakeClient:
            def pair(self, *, server: object, pin: str) -> None:
                self.paired = (server, pin)

        client = FakeClient()
        server = SimpleNamespace(
            hostname="synthetic",
            address="synthetic",
            https_port=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            for name in ("key.pem", "cert.pem", "unique_id"):
                (state_dir / name).write_text("synthetic", encoding="utf-8")
            with (
                patch.object(live_probe, "STATE_DIR", state_dir),
                patch.object(live_probe, "make_client", return_value=client),
                patch.object(live_probe, "discover_one", return_value=server),
                patch.object(live_probe, "confirm"),
                patch.object(live_probe, "request_pin", return_value="0000"),
                patch.object(live_probe, "notify"),
                patch.object(live_probe.webbrowser, "open"),
            ):
                result = live_probe.pair_phase()

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["phase"], "pair")
        self.assertFalse(result["host_metadata_exposed"])
        self.assertEqual(result["remote_input_actions"], 0)


if __name__ == "__main__":
    unittest.main()
