"""Tests for the Streambot-owned Coconut Shell publisher."""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

from streambot.notification_publisher import (  # noqa: E402
    CoconutShellClient,
    CoconutShellError,
    EventNotificationPublisher,
    HourlyNotificationPublisher,
    PublisherConfig,
    PublisherConfigurationError,
    SnapshotError,
    StreambotNotificationSnapshot,
    _message_for,
)

NOW = datetime(2026, 8, 19, 10, 23, 45, tzinfo=timezone.utc)


def snapshot(running: bool = True) -> StreambotNotificationSnapshot:
    jobs = [
        {
            "name": "pilot",
            "title": "Pilot",
            "running": running,
            "pid": 321 if running else None,
            "metrics": {"cycles": 4},
            "events": [],
        },
        {
            "name": "poly-bridge",
            "title": "Poly Bridge",
            "running": False,
            "pid": None,
            "metrics": None,
            "events": [],
        },
    ]
    return StreambotNotificationSnapshot.from_status(
        {"connection": {"state": "observing", "frame_age_ms": 20}}, jobs, NOW
    )


def completed_data() -> dict:
    return {
        "level": "1-1",
        "complete": True,
        "placed_count": 8,
        "planned_count": 8,
        "missing_count": 0,
        "missing_summary": "",
        "duration_seconds": 95.0,
    }


def assistance_data() -> dict:
    return {"job": "pilot", "outcome": "abstained", "page_key": "map-1", "attempt_count": 2}


class Response:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self._payload[:limit]


class PublisherConfigTests(unittest.TestCase):
    def test_disabled_is_the_fail_closed_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            config = PublisherConfig.from_environment()
        self.assertFalse(config.enabled)

    def test_enabled_configuration_uses_only_the_absolute_cli(self) -> None:
        values = {
            "STREAMBOT_COCONUT_SHELL_PUBLISHER_ENABLED": "true",
        }
        with mock.patch.dict(os.environ, values, clear=True):
            config = PublisherConfig.from_environment()
        self.assertTrue(config.enabled)
        self.assertTrue(config.cli_path.is_absolute())


class SnapshotTests(unittest.TestCase):
    def test_snapshot_uses_only_normalized_platform_status(self) -> None:
        value = snapshot()
        self.assertEqual(value.registered_count, 2)
        self.assertEqual(value.worker_state, "observing")
        self.assertEqual(
            value.payload(),
            {
                "collected_at": "2026-08-19T10:23:45Z",
                "registered_count": 2,
                "running_count": 1,
                "running_jobs": [
                    {"key": "pilot", "display_name": "Pilot", "pid": 321}
                ],
            },
        )
        encoded = json.dumps(value.payload())
        self.assertNotIn("metrics", encoded)
        self.assertNotIn("events", encoded)

    def test_snapshot_rejects_unbounded_or_unsafe_running_jobs(self) -> None:
        with self.assertRaises(SnapshotError):
            StreambotNotificationSnapshot.from_status(
                {},
                [
                    {
                        "name": "unsafe/name\n",
                        "title": "Unsafe",
                        "running": True,
                        "pid": 1,
                    }
                ],
                NOW,
            )


class CoconutShellClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls = []

        def runner(arguments, **options):
            self.calls.append((arguments, options))
            kind = "cycle" if "cycle" in arguments else "cli" if "--version" in arguments else "notification"
            payload = {"ok": True, "kind": kind, "state": "accepted" if kind == "cycle" else "succeeded"}
            if kind == "notification":
                payload["notification_id"] = "event-1"
            if kind == "cli":
                payload["version"] = "test-revision"
            return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        self.config = PublisherConfig(enabled=True)
        self.client = CoconutShellClient(self.config, runner=runner)

    def test_client_submits_the_exact_native_contract_through_cli(self) -> None:
        with TemporaryDirectory() as directory:
            image = Path(directory) / "snapshot.jpg"
            image.write_bytes(b"\xff\xd8\xfffixture")
            accepted = self.client.submit(snapshot(), "hour:2026-08-19T10", [image])
        terminal = self.client.status(accepted["id"])
        self.assertEqual(terminal["state"], "succeeded")
        arguments, options = self.calls[0]
        payload = json.loads(options["input"])
        self.assertEqual(arguments[:2], [str(self.config.cli_path), "publish"])
        self.assertEqual(payload["contract"], "native-feishu-v1")
        self.assertEqual(payload["source"], "streambot")
        self.assertEqual(payload["type"], "streambot.hourly_status")
        self.assertEqual(payload["idempotency_key"], "hour:2026-08-19T10")
        self.assertEqual(payload["message"]["msg_type"], "interactive")
        self.assertEqual(list(payload["images"]), ["image_1"])

    def test_client_reports_an_hourly_cycle_through_cli(self) -> None:
        self.client.report_cycle(
            bucket="hour:2026-08-19T10",
            observed_at=NOW,
            outcome="silent",
        )

        arguments, options = self.calls[0]
        self.assertEqual(arguments[:2], [str(self.config.cli_path), "cycle"])
        payload = json.loads(options["input"])
        self.assertEqual(payload["source"], "streambot")
        self.assertEqual(payload["type"], "streambot.hourly_status")
        self.assertEqual(payload["cycle_key"], "hour:2026-08-19T10")
        self.assertEqual(payload["outcome"], "silent")
        self.assertEqual(payload["expected_next_at"], "2026-08-19T11:00:00Z")

    def test_client_errors_never_include_process_detail(self) -> None:
        def failing(_arguments, **_options):
            raise OSError("private network detail")

        client = CoconutShellClient(self.config, runner=failing)
        with self.assertRaises(CoconutShellError) as raised:
            client.submit(snapshot(), "hour:2026-08-19T10", [])
        message = str(raised.exception)
        self.assertNotIn("private", message)

    def test_every_streambot_type_builds_its_native_card(self) -> None:
        fixtures = {
            "streambot.hourly_status": snapshot().payload(),
            "streambot.poly_bridge.completed": completed_data(),
            "streambot.poly_bridge.stalled": {"idle_seconds": 90, "threshold_seconds": 60, "last_command": "click"},
            "streambot.pilot.assistance_required": assistance_data(),
            "streambot.marketplace_match": {"item": "Rare material", "observed_price": 90, "operator": "<=", "threshold": 100},
        }
        for type_key, data in fixtures.items():
            with self.subTest(type_key=type_key):
                message, title, summary = _message_for(type_key, data, False)
                self.assertEqual(message["msg_type"], "interactive")
                self.assertEqual(message["card"]["header"]["title"]["content"], title)
                self.assertTrue(summary)


class FakeClient:
    def __init__(self, *, fail_first_submit: bool = False) -> None:
        self.uploads = 0
        self.submissions = []
        self.status_calls = 0
        self.heartbeats = []
        self.cycles = []
        self.fail_first_submit = fail_first_submit

    def submit(self, value, key, image_paths):
        self.submissions.append((value, key, list(image_paths)))
        if self.fail_first_submit and len(self.submissions) == 1:
            raise CoconutShellError("safe failure")
        return {"id": "event-1", "state": "pending"}

    def submit_event(self, type_key, key, occurred_at, data, image_paths):
        self.submissions.append((type_key, key, occurred_at, data, list(image_paths)))
        if self.fail_first_submit and len(self.submissions) == 1:
            raise CoconutShellError("safe failure")
        return {"id": "event-1", "state": "pending"}

    def status(self, notification_id):
        self.status_calls += 1
        return {"id": notification_id, "state": "succeeded"}

    def heartbeat(self, publisher_status):
        self.heartbeats.append(dict(publisher_status))
        return {"status": "accepted"}

    def report_cycle(self, **cycle):
        self.cycles.append(cycle)
        return {"id": "4" * 32, "replayed": False}


class HourlyPublisherTests(unittest.TestCase):
    def config(self) -> PublisherConfig:
        return PublisherConfig(
            enabled=True,
            poll_interval=0.001,
            terminal_timeout=1,
            submit_attempts=2,
        )

    def test_idle_tick_records_a_successful_skip_without_network(self) -> None:
        client = FakeClient()
        publisher = HourlyNotificationPublisher(
            self.config(), lambda _now: snapshot(False), lambda: None, client=client
        )
        result = publisher.run_once(NOW)
        self.assertEqual(result["state"], "idle_skip")
        self.assertEqual(result["last_bucket"], "hour:2026-08-19T10")
        self.assertEqual(client.submissions, [])
        self.assertEqual(client.cycles[0]["outcome"], "silent")

    def test_running_tick_retries_with_one_stable_key_and_polls_terminal_status(self) -> None:
        client = FakeClient(fail_first_submit=True)
        publisher = HourlyNotificationPublisher(
            self.config(),
            lambda _now: snapshot(True),
            lambda: (b"\xff\xd8\xfffixture", "image/jpeg"),
            client=client,
        )
        publisher._stop.wait = mock.Mock(return_value=False)
        result = publisher.run_once(NOW)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(len(client.submissions[0][2]), 1)
        self.assertEqual(client.status_calls, 1)
        self.assertEqual(
            [submission[1] for submission in client.submissions],
            ["hour:2026-08-19T10", "hour:2026-08-19T10"],
        )
        self.assertEqual(client.cycles[0]["outcome"], "notification_accepted")

    def test_optional_snapshot_failure_does_not_block_text_delivery(self) -> None:
        client = FakeClient()

        def failed_capture():
            raise OSError("private path")

        publisher = HourlyNotificationPublisher(
            self.config(), lambda _now: snapshot(True), failed_capture, client=client
        )
        result = publisher.run_once(NOW)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(client.submissions[0][2], [])
        self.assertEqual(result["reason"], "Optional snapshot was unavailable.")

    def test_publisher_reports_an_independent_heartbeat(self) -> None:
        client = FakeClient()
        publisher = HourlyNotificationPublisher(
            self.config(), lambda _now: snapshot(False), lambda: None, client=client,
            clock=lambda: NOW,
        )
        publisher.run_once(NOW)
        publisher._send_heartbeat()
        self.assertEqual(client.heartbeats[0]["state"], "idle_skip")
        self.assertEqual(publisher.status()["heartbeat_state"], "confirmed")
        self.assertEqual(publisher.status()["heartbeat_at"], "2026-08-19T10:23:45Z")

    def test_heartbeat_degrades_when_event_lane_is_unhealthy(self) -> None:
        client = FakeClient()
        publisher = HourlyNotificationPublisher(
            self.config(),
            lambda _now: snapshot(False),
            lambda: None,
            client=client,
            clock=lambda: NOW,
        )
        publisher.run_once(NOW)
        publisher.include_health(
            lambda: {"state": "failed", "reason": "Terminal delivery failed."}
        )
        publisher._send_heartbeat()
        self.assertEqual(client.heartbeats[0]["state"], "degraded")
        self.assertEqual(
            client.heartbeats[0]["reason"], "Terminal delivery failed."
        )


class EventPublisherTests(unittest.TestCase):
    def config(self) -> PublisherConfig:
        return PublisherConfig(
            enabled=True,
            poll_interval=0.001,
            terminal_timeout=1,
            submit_attempts=1,
        )

    def test_first_enable_initializes_without_historical_backfill(self) -> None:
        cursor = {"value": None}
        publisher = EventNotificationPublisher(
            self.config(),
            lambda _after, _limit: [],
            lambda: 42,
            lambda: cursor["value"],
            lambda value: cursor.update(value=value),
            lambda: {},
            Path("/unused"),
            client=FakeClient(),
        )
        result = publisher.run_once()
        self.assertEqual(cursor["value"], 42)
        self.assertEqual(result["state"], "ready")
        self.assertIn("without historical", result["reason"])

    def test_event_advances_only_after_terminal_success_and_writes_ack(self) -> None:
        cursor = {"value": 0}
        event_id = "a" * 32
        event = {
            "rowid": 1,
            "job": "poly-bridge",
            "event": "notification",
            "notification_kind": "completed",
            "event_id": event_id,
            "data": completed_data(),
            "t": int(NOW.timestamp()),
        }
        client = FakeClient()
        with TemporaryDirectory() as directory:
            publisher = EventNotificationPublisher(
                self.config(),
                lambda after, _limit: [event] if after < 1 else [],
                lambda: 1,
                lambda: cursor["value"],
                lambda value: cursor.update(value=value),
                lambda: {
                    "poly-bridge": {
                        "completed": {
                            "type": "streambot.poly_bridge.completed",
                            "artifact": "none",
                        }
                    }
                },
                Path(directory),
                client=client,
                clock=lambda: NOW,
            )
            result = publisher.run_once()
            ack = json.loads(
                (Path(directory) / "acks" / f"{event_id}.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(cursor["value"], 1)
        self.assertEqual(ack["state"], "succeeded")
        self.assertEqual(client.submissions[0][0], "streambot.poly_bridge.completed")

    def test_valid_optional_artifact_uploads_and_is_removed_after_success(self) -> None:
        cursor = {"value": 0}
        event_id = "b" * 32
        event = {
            "rowid": 1,
            "job": "poly-bridge",
            "event": "notification",
            "notification_kind": "completed",
            "event_id": event_id,
            "artifact_id": event_id,
            "data": completed_data(),
            "t": int(NOW.timestamp()),
        }
        client = FakeClient()
        with TemporaryDirectory() as directory:
            notification_dir = Path(directory)
            artifact_dir = notification_dir / "artifacts"
            artifact_dir.mkdir()
            data_path = artifact_dir / f"{event_id}.png"
            metadata_path = artifact_dir / f"{event_id}.json"
            data_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            metadata_path.write_text(
                json.dumps(
                    {
                        "id": event_id,
                        "filename": data_path.name,
                        "content_type": "image/png",
                    }
                ),
                encoding="utf-8",
            )
            publisher = EventNotificationPublisher(
                self.config(),
                lambda _after, _limit: [event],
                lambda: 1,
                lambda: cursor["value"],
                lambda value: cursor.update(value=value),
                lambda: {
                    "poly-bridge": {
                        "completed": {
                            "type": "streambot.poly_bridge.completed",
                            "artifact": "optional",
                        }
                    }
                },
                notification_dir,
                client=client,
                clock=lambda: NOW,
            )
            result = publisher.run_once()
            self.assertFalse(data_path.exists())
            self.assertFalse(metadata_path.exists())
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(cursor["value"], 1)
        self.assertEqual(len(client.submissions[0][-1]), 1)

    def test_uploaded_artifact_is_reused_after_acceptance_failure_and_restart(self) -> None:
        cursor = {"value": 0}
        event_id = "e" * 32
        event = {
            "rowid": 1,
            "job": "pilot",
            "event": "notification",
            "notification_kind": "assistance_required",
            "event_id": event_id,
            "artifact_id": event_id,
            "data": assistance_data(),
            "t": int(NOW.timestamp()),
        }
        routes = lambda: {
            "pilot": {
                "assistance_required": {
                    "type": "streambot.pilot.assistance_required",
                    "artifact": "required",
                }
            }
        }
        client = FakeClient(fail_first_submit=True)
        with TemporaryDirectory() as directory:
            notification_dir = Path(directory)
            artifact_dir = notification_dir / "artifacts"
            artifact_dir.mkdir()
            data_path = artifact_dir / f"{event_id}.png"
            metadata_path = artifact_dir / f"{event_id}.json"
            data_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            metadata_path.write_text(json.dumps({"id": event_id, "filename": data_path.name, "content_type": "image/png"}), encoding="utf-8")

            first = EventNotificationPublisher(self.config(), lambda _after, _limit: [event], lambda: 1, lambda: cursor["value"], lambda value: cursor.update(value=value), routes, notification_dir, client=client, clock=lambda: NOW)
            self.assertEqual(first.run_once()["state"], "degraded")
            self.assertTrue(data_path.exists())

            client.fail_first_submit = False
            restarted = EventNotificationPublisher(self.config(), lambda _after, _limit: [event], lambda: 1, lambda: cursor["value"], lambda value: cursor.update(value=value), routes, notification_dir, client=client, clock=lambda: NOW)
            self.assertEqual(restarted.run_once()["state"], "succeeded")
            self.assertEqual(len(client.submissions[-1][-1]), 1)
            self.assertFalse(data_path.exists())

    def test_unsafe_optional_artifact_degrades_to_text_without_following_symlink(self) -> None:
        cursor = {"value": 0}
        event_id = "c" * 32
        event = {
            "rowid": 1,
            "job": "poly-bridge",
            "event": "notification",
            "notification_kind": "completed",
            "event_id": event_id,
            "artifact_id": event_id,
            "data": completed_data(),
            "t": int(NOW.timestamp()),
        }
        client = FakeClient()
        with TemporaryDirectory() as directory:
            notification_dir = Path(directory)
            artifact_dir = notification_dir / "artifacts"
            artifact_dir.mkdir()
            outside = notification_dir / "outside.json"
            outside.write_text('{"private":"must not be read"}', encoding="utf-8")
            (artifact_dir / f"{event_id}.json").symlink_to(outside)
            publisher = EventNotificationPublisher(
                self.config(),
                lambda _after, _limit: [event],
                lambda: 1,
                lambda: cursor["value"],
                lambda value: cursor.update(value=value),
                lambda: {
                    "poly-bridge": {
                        "completed": {
                            "type": "streambot.poly_bridge.completed",
                            "artifact": "optional",
                        }
                    }
                },
                notification_dir,
                client=client,
            )
            result = publisher.run_once()
        self.assertEqual(result["state"], "succeeded")
        self.assertIn("Optional", result["reason"])
        self.assertEqual(cursor["value"], 1)
        self.assertEqual(client.submissions[0][-1], [])

    def test_missing_required_artifact_blocks_delivery_and_cursor(self) -> None:
        cursor = {"value": 0}
        event_id = "d" * 32
        event = {
            "rowid": 1,
            "job": "pilot",
            "event": "notification",
            "notification_kind": "assistance_required",
            "event_id": event_id,
            "artifact_id": event_id,
            "data": assistance_data(),
            "t": int(NOW.timestamp()),
        }
        client = FakeClient()
        with TemporaryDirectory() as directory:
            publisher = EventNotificationPublisher(
                self.config(),
                lambda _after, _limit: [event],
                lambda: 1,
                lambda: cursor["value"],
                lambda value: cursor.update(value=value),
                lambda: {
                    "pilot": {
                        "assistance_required": {
                            "type": "streambot.pilot.assistance_required",
                            "artifact": "required",
                        }
                    }
                },
                Path(directory),
                client=client,
            )
            result = publisher.run_once()
        self.assertEqual(result["state"], "degraded")
        self.assertIn("Required", result["reason"])
        self.assertEqual(cursor["value"], 0)
        self.assertEqual(client.submissions, [])

    def test_unmapped_event_is_skipped_without_network(self) -> None:
        cursor = {"value": 0}
        client = FakeClient()
        publisher = EventNotificationPublisher(
            self.config(),
            lambda _after, _limit: [
                {
                    "rowid": 1,
                    "job": "unknown",
                    "event": "notification",
                    "notification_kind": "completed",
                    "event_id": "a" * 32,
                    "data": {},
                    "t": int(NOW.timestamp()),
                }
            ],
            lambda: 1,
            lambda: cursor["value"],
            lambda value: cursor.update(value=value),
            lambda: {},
            Path("/unused"),
            client=client,
        )
        result = publisher.run_once()
        self.assertEqual(result["state"], "ignored")
        self.assertEqual(cursor["value"], 1)
        self.assertEqual(client.submissions, [])


if __name__ == "__main__":
    unittest.main()
