"""Publish one normalized Streambot platform snapshot to Coconut Shell.

The control panel remains the only collector. This module accepts the
allowlisted status values the panel already computed; it never scans jobs,
processes, logs, manifests, or control sockets on its own.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

TYPE_KEY = "streambot.hourly_status"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_MEDIA_BYTES = 8 * 1024 * 1024
TERMINAL_STATES = {"succeeded", "suppressed", "failed", "ambiguous"}


class PublisherConfigurationError(ValueError):
    """The publisher configuration is incomplete or unsafe."""


class CoconutShellError(RuntimeError):
    """A secret-safe Coconut Shell request failure."""


class SnapshotError(ValueError):
    """The control-plane status cannot form a bounded notification snapshot."""


@dataclass(frozen=True)
class PublisherConfig:
    enabled: bool = False
    base_url: str = ""
    source_token: str = field(default="", repr=False)
    include_snapshot: bool = True
    request_timeout: float = 10.0
    terminal_timeout: float = 180.0
    poll_interval: float = 2.0
    submit_attempts: int = 3

    @classmethod
    def from_environment(cls) -> "PublisherConfig":
        enabled = _environment_flag("STREAMBOT_HOURLY_PUBLISHER_ENABLED", False)
        include_snapshot = _environment_flag(
            "STREAMBOT_HOURLY_PUBLISHER_INCLUDE_SNAPSHOT", True
        )
        base_url = os.environ.get("COCONUT_SHELL_BASE_URL", "").strip()
        source_token = os.environ.get(
            "COCONUT_SHELL_STREAMBOT_SOURCE_TOKEN", ""
        ).strip()
        if not enabled:
            return cls(enabled=False, include_snapshot=include_snapshot)
        _validate_base_url(base_url)
        if (
            len(source_token) < 32
            or len(source_token) > 512
            or any(character.isspace() or ord(character) < 32 for character in source_token)
        ):
            raise PublisherConfigurationError(
                "Coconut Shell Streambot source token is missing or invalid"
            )
        return cls(
            enabled=True,
            base_url=base_url.rstrip("/"),
            source_token=source_token,
            include_snapshot=include_snapshot,
        )


@dataclass(frozen=True)
class RunningJob:
    key: str
    display_name: str
    pid: int

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "display_name": self.display_name, "pid": self.pid}


@dataclass(frozen=True)
class StreambotNotificationSnapshot:
    collected_at: datetime
    registered_count: int
    running_jobs: tuple[RunningJob, ...]
    worker_state: str | None
    worker_frame_age_ms: int | float | None

    @classmethod
    def from_status(
        cls,
        worker_status: dict[str, Any],
        jobs: list[dict[str, Any]],
        collected_at: datetime | None = None,
    ) -> "StreambotNotificationSnapshot":
        now = (collected_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if len(jobs) > 256:
            raise SnapshotError("registered job count exceeds the platform bound")
        running: list[RunningJob] = []
        seen: set[str] = set()
        for row in jobs:
            if not row.get("running"):
                continue
            key = row.get("name")
            display_name = row.get("title")
            pid = row.get("pid")
            if (
                not _valid_text(key, 64)
                or not _valid_text(display_name, 80)
                or not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or key in seen
            ):
                raise SnapshotError("running job status is invalid")
            seen.add(key)
            running.append(RunningJob(key=key, display_name=display_name, pid=pid))
        if len(running) > 32:
            raise SnapshotError("running job count exceeds the notification bound")
        connection = worker_status.get("connection")
        if not isinstance(connection, dict):
            connection = {}
        worker_state = connection.get("state")
        if worker_state is not None and not _valid_text(worker_state, 64):
            worker_state = None
        frame_age = connection.get("frame_age_ms")
        if not isinstance(frame_age, (int, float)) or isinstance(frame_age, bool):
            frame_age = None
        return cls(
            collected_at=now,
            registered_count=len(jobs),
            running_jobs=tuple(running),
            worker_state=worker_state,
            worker_frame_age_ms=frame_age,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "collected_at": _rfc3339(self.collected_at),
            "registered_count": self.registered_count,
            "running_count": len(self.running_jobs),
            "running_jobs": [job.as_dict() for job in self.running_jobs],
        }


class CoconutShellClient:
    """Small authenticated client for the versioned producer API."""

    def __init__(
        self,
        config: PublisherConfig,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if not config.enabled:
            raise PublisherConfigurationError("publisher client requires enabled configuration")
        _validate_base_url(config.base_url)
        self._base_url = config.base_url.rstrip("/")
        self._source_token = config.source_token
        self._timeout = config.request_timeout
        self._opener = opener or urllib.request.urlopen

    def upload_media(self, contents: bytes, content_type: str) -> str:
        if (
            not contents
            or len(contents) > MAX_MEDIA_BYTES
            or content_type not in {"image/jpeg", "image/png"}
        ):
            raise CoconutShellError("notification snapshot is invalid")
        response = self._request("POST", "/api/v1/media", contents, content_type)
        media_id = response.get("id")
        if not _valid_identifier(media_id):
            raise CoconutShellError("Coconut Shell media response is invalid")
        return media_id

    def submit(
        self,
        snapshot: StreambotNotificationSnapshot,
        idempotency_key: str,
        media_ids: list[str],
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "type": TYPE_KEY,
                "idempotency_key": idempotency_key,
                "occurred_at": _rfc3339(snapshot.collected_at),
                "renderer_version": 1,
                "data": snapshot.payload(),
                "media_ids": media_ids,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return self._request(
            "POST", "/api/v1/notifications", body, "application/json; charset=utf-8"
        )

    def status(self, notification_id: str) -> dict[str, Any]:
        if not _valid_identifier(notification_id):
            raise CoconutShellError("Coconut Shell notification identifier is invalid")
        path = "/api/v1/notifications/" + urllib.parse.quote(notification_id, safe="")
        return self._request("GET", path, None, None)

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        content_type: str | None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            self._base_url + path,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self._source_token,
                "X-Request-ID": str(uuid.uuid4()),
            },
        )
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = getattr(response, "status", 200)
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise CoconutShellError(
                f"Coconut Shell request failed with HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError):
            raise CoconutShellError("Coconut Shell request did not confirm a response") from None
        if status < 200 or status >= 300:
            raise CoconutShellError(
                f"Coconut Shell request failed with HTTP {status}"
            )
        if len(data) > MAX_RESPONSE_BYTES:
            raise CoconutShellError("Coconut Shell response is too large")
        try:
            decoded = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CoconutShellError("Coconut Shell response is invalid") from None
        if not isinstance(decoded, dict):
            raise CoconutShellError("Coconut Shell response is invalid")
        return decoded


class HourlyNotificationPublisher:
    """Schedule current-hour snapshots without backfilling missed hours."""

    def __init__(
        self,
        config: PublisherConfig,
        snapshot_provider: Callable[[datetime], StreambotNotificationSnapshot],
        capture_provider: Callable[[], tuple[bytes, str] | None],
        *,
        client: CoconutShellClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._snapshot_provider = snapshot_provider
        self._capture_provider = capture_provider
        self._client = client or (CoconutShellClient(config) if config.enabled else None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "state": "disabled" if not config.enabled else "ready",
            "last_bucket": None,
            "last_notification_id": None,
            "last_observed_at": None,
            "reason": "Publisher is disabled." if not config.enabled else "",
        }

    def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="coconut-shell-hourly-publisher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def run_once(self, collected_at: datetime | None = None) -> dict[str, Any]:
        now = (collected_at or self._clock()).astimezone(timezone.utc)
        bucket = _hour_bucket(now)
        if not self.config.enabled or self._client is None:
            return self._record("disabled", bucket, now, reason="Publisher is disabled.")
        try:
            snapshot = self._snapshot_provider(now)
        except Exception:
            return self._record(
                "degraded", bucket, now, reason="Control-plane snapshot validation failed."
            )
        if not snapshot.running_jobs:
            return self._record(
                "idle_skip", bucket, now, reason="No registered job is running."
            )

        media_ids: list[str] = []
        attachment_reason = ""
        if self.config.include_snapshot:
            try:
                captured = self._capture_provider()
                if captured is not None:
                    contents, content_type = captured
                    media_ids.append(self._client.upload_media(contents, content_type))
            except Exception:
                attachment_reason = "Optional snapshot was unavailable."

        result: dict[str, Any] | None = None
        for attempt in range(self.config.submit_attempts):
            try:
                result = self._client.submit(snapshot, bucket, media_ids)
                break
            except CoconutShellError:
                if attempt + 1 == self.config.submit_attempts:
                    return self._record(
                        "degraded",
                        bucket,
                        now,
                        reason="Coconut Shell did not confirm notification acceptance.",
                    )
                if self._stop.wait(min(2**attempt, 4)):
                    return self._record(
                        "stopped", bucket, now, reason="Publisher stopped during retry."
                    )
        assert result is not None
        notification_id = result.get("id")
        state = result.get("state")
        if not _valid_identifier(notification_id) or not isinstance(state, str):
            return self._record(
                "degraded", bucket, now, reason="Coconut Shell acceptance response was invalid."
            )
        deadline = time.monotonic() + self.config.terminal_timeout
        while state not in TERMINAL_STATES and time.monotonic() < deadline:
            if self._stop.wait(self.config.poll_interval):
                return self._record(
                    "stopped",
                    bucket,
                    now,
                    notification_id=notification_id,
                    reason="Publisher stopped while awaiting terminal status.",
                )
            try:
                result = self._client.status(notification_id)
            except CoconutShellError:
                continue
            state = result.get("state")
            if not isinstance(state, str):
                state = ""
        if state in {"succeeded", "suppressed"}:
            return self._record(
                state,
                bucket,
                now,
                notification_id=notification_id,
                reason=attachment_reason,
            )
        if state in {"failed", "ambiguous"}:
            return self._record(
                state,
                bucket,
                now,
                notification_id=notification_id,
                reason="Coconut Shell reported a terminal delivery failure.",
            )
        return self._record(
            "degraded",
            bucket,
            now,
            notification_id=notification_id,
            reason="Coconut Shell status polling timed out.",
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            now = self._clock().astimezone(timezone.utc)
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            if self._stop.wait(max((next_hour - now).total_seconds(), 0.001)):
                return
            self.run_once(self._clock())

    def _record(
        self,
        state: str,
        bucket: str,
        observed_at: datetime,
        *,
        notification_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        value = {
            "state": state,
            "last_bucket": bucket,
            "last_notification_id": notification_id,
            "last_observed_at": _rfc3339(observed_at),
            "reason": reason,
        }
        with self._status_lock:
            self._status = value
        return dict(value)


def _environment_flag(name: str, fallback: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return fallback
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise PublisherConfigurationError(f"{name} must be a boolean")


def _validate_base_url(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    is_loopback_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    if (
        not value
        or (parsed.scheme != "https" and not is_loopback_http)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PublisherConfigurationError("Coconut Shell base URL is invalid")


def _valid_text(value: Any, limit: int) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and 0 < len(value) <= limit
        and all(ord(character) >= 32 and character != "\x7f" for character in value)
    )


def _valid_identifier(value: Any) -> bool:
    return _valid_text(value, 128) and not any(character.isspace() for character in value)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _hour_bucket(value: datetime) -> str:
    return "hour:" + value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
