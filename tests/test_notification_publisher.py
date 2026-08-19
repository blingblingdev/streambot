"""Tests for the Streambot-owned Coconut Shell publisher."""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

from streambot.notification_publisher import (  # noqa: E402
    CoconutShellClient,
    CoconutShellError,
    HourlyNotificationPublisher,
    PublisherConfig,
    PublisherConfigurationError,
    SnapshotError,
    StreambotNotificationSnapshot,
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
        self.assertEqual(config.source_token, "")

    def test_enabled_configuration_requires_https_and_a_bounded_token(self) -> None:
        values = {
            "STREAMBOT_HOURLY_PUBLISHER_ENABLED": "true",
            "COCONUT_SHELL_BASE_URL": "https://coconut.example.test",
            "COCONUT_SHELL_STREAMBOT_SOURCE_TOKEN": "cshp_v1_" + "x" * 43,
        }
        with mock.patch.dict(os.environ, values, clear=True):
            config = PublisherConfig.from_environment()
        self.assertTrue(config.enabled)
        self.assertNotIn("x" * 43, repr(config))

        values["COCONUT_SHELL_BASE_URL"] = "http://remote.example.test"
        with mock.patch.dict(os.environ, values, clear=True):
            with self.assertRaises(PublisherConfigurationError):
                PublisherConfig.from_environment()


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
        self.requests = []

        def opener(request, timeout):
            self.requests.append((request, timeout))
            if request.full_url.endswith("/api/v1/media"):
                return Response({"id": "00000000-0000-4000-8000-000000000001"}, 201)
            if request.full_url.endswith("/api/v1/notifications"):
                return Response({"id": "event-1", "state": "pending"}, 202)
            if request.full_url.endswith("/api/v1/sources/heartbeat"):
                return Response({"status": "accepted"})
            return Response({"id": "event-1", "state": "succeeded"})

        self.config = PublisherConfig(
            enabled=True,
            base_url="https://coconut.example.test",
            source_token="cshp_v1_" + "x" * 43,
        )
        self.client = CoconutShellClient(self.config, opener=opener)

    def test_client_uploads_and_submits_the_exact_contract(self) -> None:
        media_id = self.client.upload_media(b"\xff\xd8\xfffixture", "image/jpeg")
        accepted = self.client.submit(snapshot(), "hour:2026-08-19T10", [media_id])
        terminal = self.client.status(accepted["id"])
        self.client.heartbeat(
            {
                "state": "succeeded",
                "reason": "",
                "last_bucket": "hour:2026-08-19T10",
                "last_notification_id": "00000000-0000-4000-8000-000000000001",
            }
        )
        self.assertEqual(terminal["state"], "succeeded")
        notification_request = self.requests[1][0]
        payload = json.loads(notification_request.data)
        self.assertEqual(payload["type"], "streambot.hourly_status")
        self.assertEqual(payload["idempotency_key"], "hour:2026-08-19T10")
        self.assertEqual(payload["media_ids"], [media_id])
        self.assertEqual(
            notification_request.get_header("Authorization"),
            "Bearer cshp_v1_" + "x" * 43,
        )
        heartbeat_payload = json.loads(self.requests[3][0].data)
        self.assertEqual(heartbeat_payload["state"], "healthy")
        self.assertEqual(heartbeat_payload["last_outcome"], "succeeded")

    def test_client_errors_never_include_response_or_credentials(self) -> None:
        def failing(_request, timeout):
            self.assertEqual(timeout, self.config.request_timeout)
            raise urllib.error.URLError("private network detail")

        client = CoconutShellClient(self.config, opener=failing)
        with self.assertRaises(CoconutShellError) as raised:
            client.submit(snapshot(), "hour:2026-08-19T10", [])
        message = str(raised.exception)
        self.assertNotIn("private", message)
        self.assertNotIn(self.config.source_token, message)


class FakeClient:
    def __init__(self, *, fail_first_submit: bool = False) -> None:
        self.uploads = 0
        self.submissions = []
        self.status_calls = 0
        self.heartbeats = []
        self.fail_first_submit = fail_first_submit

    def upload_media(self, contents, content_type):
        self.uploads += 1
        return "media-1"

    def submit(self, value, key, media_ids):
        self.submissions.append((value, key, list(media_ids)))
        if self.fail_first_submit and len(self.submissions) == 1:
            raise CoconutShellError("safe failure")
        return {"id": "event-1", "state": "pending"}

    def status(self, notification_id):
        self.status_calls += 1
        return {"id": notification_id, "state": "succeeded"}

    def heartbeat(self, publisher_status):
        self.heartbeats.append(dict(publisher_status))
        return {"status": "accepted"}


class HourlyPublisherTests(unittest.TestCase):
    def config(self) -> PublisherConfig:
        return PublisherConfig(
            enabled=True,
            base_url="https://coconut.example.test",
            source_token="cshp_v1_" + "x" * 43,
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
        self.assertEqual(client.uploads, 1)
        self.assertEqual(client.status_calls, 1)
        self.assertEqual(
            [submission[1] for submission in client.submissions],
            ["hour:2026-08-19T10", "hour:2026-08-19T10"],
        )

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


if __name__ == "__main__":
    unittest.main()
