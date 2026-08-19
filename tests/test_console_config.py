"""Tests for the console's job-configuration endpoints."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

_spec = importlib.util.spec_from_file_location(
    "control_panel_server_config",
    PROJECT_ROOT / "apps" / "control-panel" / "server.py",
)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

CONFIG_BLOCK = {
    "fields": [
        {
            "key": "idle_seconds",
            "label": "Idle",
            "type": "integer",
            "min": 30,
            "max": 3600,
            "default": 210,
            "unit": "s",
        }
    ],
    "presets": [
        {"label": "Short", "values": {"idle_seconds": 210}},
        {"label": "Long", "values": {"idle_seconds": 930}},
    ],
}


def registry(config=CONFIG_BLOCK) -> dict:
    return {
        "demo": {
            "name": "demo",
            "title": "Demo",
            "description": "",
            "runner": ["jobs/demo/runner.py"],
            "config": config,
        }
    }


class ConfigEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.jobs = server.JobSupervisor()
        self._values = mock.patch.object(
            server, "values_path", lambda name: self.root / f"{name}.json"
        )
        self._values.start()

    def tearDown(self) -> None:
        self._values.stop()
        self._directory.cleanup()

    def test_config_reports_schema_and_resolved_values(self) -> None:
        with mock.patch.object(server.JobSupervisor, "registry", staticmethod(registry)):
            payload = self.jobs.config("demo")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema"]["fields"][0]["key"], "idle_seconds")
        self.assertEqual(payload["values"]["idle_seconds"], 210)
        self.assertEqual(payload["values"]["max_cycles"], 0)
        self.assertEqual(payload["stored"], {})
        self.assertEqual(
            [preset["label"] for preset in payload["schema"]["presets"]],
            ["Short", "Long"],
        )

    def test_unknown_job_is_refused(self) -> None:
        with mock.patch.object(server.JobSupervisor, "registry", staticmethod(registry)):
            self.assertEqual(self.jobs.config("nope")["error"], "UnknownJob")
            self.assertEqual(
                self.jobs.set_config("nope", {"idle_seconds": 300})["error"],
                "UnknownJob",
            )

    def test_setting_a_value_stores_only_the_delta(self) -> None:
        with mock.patch.object(server.JobSupervisor, "registry", staticmethod(registry)):
            payload = self.jobs.set_config("demo", {"idle_seconds": 930})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stored"], {"idle_seconds": 930})
        self.assertEqual(payload["values"]["idle_seconds"], 930)
        self.assertEqual(payload["values"]["poll_seconds"], 3.0)
        written = json.loads((self.root / "demo.json").read_text(encoding="utf-8"))
        self.assertEqual(written, {"idle_seconds": 930})

    def test_a_value_outside_its_bounds_never_reaches_the_job(self) -> None:
        with mock.patch.object(server.JobSupervisor, "registry", staticmethod(registry)):
            payload = self.jobs.set_config("demo", {"idle_seconds": 5})
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "InvalidValue")
        self.assertFalse((self.root / "demo.json").exists())

    def test_an_undeclared_setting_is_refused(self) -> None:
        with mock.patch.object(server.JobSupervisor, "registry", staticmethod(registry)):
            payload = self.jobs.set_config("demo", {"nope": 1})
        self.assertEqual(payload["error"], "InvalidValue")

    def test_edits_merge_rather_than_replace(self) -> None:
        with mock.patch.object(server.JobSupervisor, "registry", staticmethod(registry)):
            self.jobs.set_config("demo", {"idle_seconds": 930})
            payload = self.jobs.set_config("demo", {"max_cycles": 3})
        self.assertEqual(payload["stored"], {"idle_seconds": 930, "max_cycles": 3})

    def test_a_job_with_no_config_block_still_gets_the_builtins(self) -> None:
        with mock.patch.object(
            server.JobSupervisor, "registry", staticmethod(lambda: registry(None))
        ):
            payload = self.jobs.config("demo")
        self.assertTrue(payload["ok"])
        self.assertEqual(
            sorted(payload["values"]), ["max_cycles", "max_seconds", "poll_seconds"]
        )

    def test_one_malformed_schema_does_not_hide_the_other_jobs(self) -> None:
        broken = {"fields": [{"key": "x", "type": "integer", "default": 1}]}
        with mock.patch.object(
            server.JobSupervisor, "registry", staticmethod(lambda: registry(broken))
        ):
            self.assertEqual(self.jobs.config("demo")["error"], "InvalidConfigSchema")
            rows = {row["name"]: row for row in self.jobs.status()}
        self.assertIn("demo", rows)
        self.assertFalse(rows["demo"]["configurable"])

    def test_job_rows_say_whether_a_job_can_be_configured(self) -> None:
        with mock.patch.object(server.JobSupervisor, "registry", staticmethod(registry)):
            rows = {row["name"]: row for row in self.jobs.status()}
        self.assertTrue(rows["demo"]["configurable"])

    def test_notification_routes_are_strict_and_declarative(self) -> None:
        declared = registry()
        declared["demo"]["notifications"] = {
            "completed": {
                "type": "streambot.demo.completed",
                "artifact": "optional",
            }
        }
        with mock.patch.object(
            server.JobSupervisor, "registry", staticmethod(lambda: declared)
        ):
            self.assertEqual(
                server.JobSupervisor.notification_routes(),
                {
                    "demo": {
                        "completed": {
                            "type": "streambot.demo.completed",
                            "artifact": "optional",
                        }
                    }
                },
            )

        declared["demo"]["notifications"]["completed"]["extra"] = "unsafe"
        with mock.patch.object(
            server.JobSupervisor, "registry", staticmethod(lambda: declared)
        ):
            self.assertEqual(server.JobSupervisor.notification_routes(), {})


class ResolveMetricTests(unittest.TestCase):
    def test_analysis_time_reaches_the_panel(self) -> None:
        now = int(time.time())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "flow-log.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for event in [
                    {"t": now - 10, "event": "start"},
                    {
                        "t": now - 5,
                        "event": "click",
                        "element": "replay",
                        "score": 0.99,
                        "resolve_ms": 12.0,
                        "act_ms": 100.0,
                    },
                    {
                        "t": now - 3,
                        "event": "click",
                        "element": "confirm",
                        "score": 0.99,
                        "resolve_ms": 18.0,
                        "act_ms": 120.0,
                    },
                ]:
                    handle.write(json.dumps(event) + "\n")
            reader = server.FlowLogReader(path)
            reader.poll()
            metrics = reader.metrics()
        self.assertEqual(metrics["resolve_ms"], 15)
        self.assertEqual(metrics["act_ms"], 110)


if __name__ == "__main__":
    unittest.main()
