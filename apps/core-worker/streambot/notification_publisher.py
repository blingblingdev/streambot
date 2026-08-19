"""Publish one normalized Streambot platform snapshot to Coconut Shell.

The control panel remains the only collector. This module accepts the
allowlisted status values the panel already computed; it never scans jobs,
processes, logs, manifests, or control sockets on its own.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

TYPE_KEY = "streambot.hourly_status"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_MEDIA_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_METADATA_BYTES = 16 * 1024
TERMINAL_STATES = {"succeeded", "suppressed", "failed", "ambiguous"}
SOURCE_TOKEN_PATTERN = re.compile(r"^cshp_v1_[A-Za-z0-9_-]{43}$")


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
    heartbeat_interval: float = 30.0

    @classmethod
    def from_environment(cls) -> "PublisherConfig":
        enabled = _environment_flag(
            "STREAMBOT_COCONUT_SHELL_PUBLISHER_ENABLED", False
        )
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
        if not _valid_source_token(source_token):
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
        if not _valid_source_token(config.source_token):
            raise PublisherConfigurationError("publisher client source token is invalid")
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
        return self.submit_event(
            TYPE_KEY,
            idempotency_key,
            snapshot.collected_at,
            snapshot.payload(),
            media_ids,
        )

    def submit_event(
        self,
        type_key: str,
        idempotency_key: str,
        occurred_at: datetime,
        data: dict[str, Any],
        media_ids: list[str],
    ) -> dict[str, Any]:
        if (
            not re.fullmatch(r"^streambot\.[a-z0-9_.]{1,100}$", type_key)
            or not _valid_identifier(idempotency_key)
            or not isinstance(data, dict)
            or len(media_ids) > 4
            or any(not _valid_identifier(media_id) for media_id in media_ids)
        ):
            raise CoconutShellError("notification event is invalid")
        body = json.dumps(
            {
                "type": type_key,
                "idempotency_key": idempotency_key,
                "occurred_at": _rfc3339(occurred_at),
                "renderer_version": 1,
                "data": data,
                "media_ids": media_ids,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > 64 * 1024:
            raise CoconutShellError("notification event is too large")
        return self._request(
            "POST", "/api/v1/notifications", body, "application/json; charset=utf-8"
        )

    def status(self, notification_id: str) -> dict[str, Any]:
        if not _valid_identifier(notification_id):
            raise CoconutShellError("Coconut Shell notification identifier is invalid")
        path = "/api/v1/notifications/" + urllib.parse.quote(notification_id, safe="")
        return self._request("GET", path, None, None)

    def heartbeat(self, publisher_status: dict[str, Any]) -> dict[str, Any]:
        outcome = publisher_status.get("state")
        state = (
            "healthy"
            if outcome in {"ready", "idle_skip", "succeeded", "suppressed"}
            else "degraded"
        )
        body = json.dumps(
            {
                "state": state,
                "reason": str(publisher_status.get("reason") or "")[:160],
                "last_bucket": publisher_status.get("last_bucket") or "",
                "last_notification_id": publisher_status.get("last_notification_id") or "",
                "last_outcome": outcome or "degraded",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return self._request(
            "POST",
            "/api/v1/sources/heartbeat",
            body,
            "application/json; charset=utf-8",
        )

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
        if config.enabled and (
            config.request_timeout <= 0
            or config.terminal_timeout <= 0
            or config.poll_interval <= 0
            or config.submit_attempts < 1
            or config.submit_attempts > 10
            or config.heartbeat_interval < 5
        ):
            raise PublisherConfigurationError("publisher timing configuration is invalid")
        self.config = config
        self._snapshot_provider = snapshot_provider
        self._capture_provider = capture_provider
        self._client = client or (CoconutShellClient(config) if config.enabled else None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._health_provider: Callable[[], dict[str, Any]] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "state": "disabled" if not config.enabled else "ready",
            "last_bucket": None,
            "last_notification_id": None,
            "last_observed_at": None,
            "reason": "Publisher is disabled." if not config.enabled else "",
            "heartbeat_state": "disabled" if not config.enabled else "pending",
            "heartbeat_at": None,
        }

    def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="coconut-shell-hourly-publisher", daemon=True
        )
        self._heartbeat_thread = threading.Thread(
            target=self._run_heartbeats,
            name="coconut-shell-publisher-heartbeat",
            daemon=True,
        )
        self._thread.start()
        self._heartbeat_thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        heartbeat_thread = self._heartbeat_thread
        if heartbeat_thread is not None and heartbeat_thread is not threading.current_thread():
            heartbeat_thread.join(timeout=5.0)

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def include_health(self, provider: Callable[[], dict[str, Any]]) -> None:
        """Include another publisher lane in the shared platform heartbeat."""

        self._health_provider = provider

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

    def _run_heartbeats(self) -> None:
        while not self._stop.is_set():
            self._send_heartbeat()
            if self._stop.wait(self.config.heartbeat_interval):
                return

    def _send_heartbeat(self) -> None:
        if self._client is None:
            return
        status = self.status()
        if self._health_provider is not None:
            peer = self._health_provider()
            if peer.get("state") not in {
                "ready",
                "succeeded",
                "suppressed",
                "ignored",
                "disabled",
            }:
                status["state"] = "degraded"
                status["reason"] = str(peer.get("reason") or "")[:160]
        now = self._clock().astimezone(timezone.utc)
        try:
            self._client.heartbeat(status)
        except CoconutShellError:
            heartbeat_state = "degraded"
        else:
            heartbeat_state = "confirmed"
        with self._status_lock:
            self._status["heartbeat_state"] = heartbeat_state
            self._status["heartbeat_at"] = _rfc3339(now)

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
            value["heartbeat_state"] = self._status.get("heartbeat_state")
            value["heartbeat_at"] = self._status.get("heartbeat_at")
            self._status = value
        return dict(value)


class EventNotificationPublisher:
    """Deliver declarative job events collected by the Streambot console."""

    BATCH_SIZE = 100

    def __init__(
        self,
        config: PublisherConfig,
        event_provider: Callable[[int, int], list[dict[str, Any]]],
        latest_event_id: Callable[[], int],
        cursor_provider: Callable[[], int | None],
        cursor_writer: Callable[[int], None],
        routes_provider: Callable[[], dict[str, dict[str, dict[str, str]]]],
        notification_dir: Path,
        *,
        client: CoconutShellClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._event_provider = event_provider
        self._latest_event_id = latest_event_id
        self._cursor_provider = cursor_provider
        self._cursor_writer = cursor_writer
        self._routes_provider = routes_provider
        self._notification_dir = Path(notification_dir)
        self._client = client or (CoconutShellClient(config) if config.enabled else None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "state": "disabled" if not config.enabled else "ready",
            "last_event_id": None,
            "last_notification_id": None,
            "reason": "Publisher is disabled." if not config.enabled else "",
        }

    def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="coconut-shell-event-publisher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def run_once(self) -> dict[str, Any]:
        if not self.config.enabled or self._client is None:
            return self._record("disabled", reason="Publisher is disabled.")
        cursor = self._cursor_provider()
        if cursor is None:
            cursor = self._latest_event_id()
            self._cursor_writer(cursor)
            return self._record(
                "ready",
                event_id=cursor,
                reason="Initialized without historical event backfill.",
            )
        routes = self._routes_provider()
        events = self._event_provider(cursor, self.BATCH_SIZE)
        if not events:
            return self._record("ready", event_id=cursor)
        for event in events:
            rowid = event.get("rowid")
            if not isinstance(rowid, int) or rowid <= cursor:
                return self._record(
                    "degraded", event_id=cursor, reason="Event cursor input is invalid."
                )
            if event.get("event") != "notification":
                self._cursor_writer(rowid)
                cursor = rowid
                continue
            outcome = self._deliver_event(event, routes)
            if outcome[0] in {"succeeded", "suppressed", "ignored"}:
                self._cursor_writer(rowid)
                cursor = rowid
                self._record(
                    outcome[0],
                    event_id=rowid,
                    notification_id=outcome[1],
                    reason=outcome[2],
                )
                continue
            return self._record(
                outcome[0],
                event_id=rowid,
                notification_id=outcome[1],
                reason=outcome[2],
            )
        return self.status()

    def _deliver_event(
        self,
        event: dict[str, Any],
        routes: dict[str, dict[str, dict[str, str]]],
    ) -> tuple[str, str | None, str]:
        job = event.get("job")
        kind = event.get("notification_kind")
        event_id = event.get("event_id")
        data = event.get("data")
        occurred = event.get("t")
        route = (
            routes.get(job, {}).get(kind)
            if isinstance(job, str) and isinstance(kind, str)
            else None
        )
        if (
            route is None
            or re.fullmatch(r"^[0-9a-f]{32}$", event_id or "") is None
            or not isinstance(data, dict)
            or not isinstance(occurred, (int, float))
        ):
            return "ignored", None, "Invalid or unmapped notification event was skipped."
        media_ids: list[str] = []
        artifact_id = event.get("artifact_id")
        artifact_policy = route["artifact"]
        artifact_paths: tuple[Path, Path] | None = None
        artifact_reason = ""
        if artifact_id is not None:
            if artifact_policy == "none":
                return "ignored", None, "Unexpected notification artifact was skipped."
            try:
                contents, content_type, artifact_paths = self._load_artifact(artifact_id)
                media_ids.append(self._client.upload_media(contents, content_type))
            except CoconutShellError:
                if artifact_policy == "required":
                    return "degraded", None, "Required notification artifact was unavailable."
                artifact_reason = "Optional notification artifact was unavailable."
        elif artifact_policy == "required":
            return "degraded", None, "Required notification artifact was unavailable."
        result: dict[str, Any] | None = None
        for attempt in range(self.config.submit_attempts):
            try:
                result = self._client.submit_event(
                    route["type"],
                    event_id,
                    datetime.fromtimestamp(float(occurred), timezone.utc),
                    data,
                    media_ids,
                )
                break
            except (CoconutShellError, OSError, OverflowError, ValueError):
                if attempt + 1 == self.config.submit_attempts:
                    return (
                        "degraded",
                        None,
                        "Coconut Shell did not confirm notification acceptance.",
                    )
                if self._stop.wait(min(2**attempt, 4)):
                    return "stopped", None, "Publisher stopped during retry."
        assert result is not None
        notification_id = result.get("id")
        state = result.get("state")
        if not _valid_identifier(notification_id) or not isinstance(state, str):
            return "degraded", None, "Coconut Shell acceptance response was invalid."
        deadline = time.monotonic() + self.config.terminal_timeout
        while state not in TERMINAL_STATES and time.monotonic() < deadline:
            if self._stop.wait(self.config.poll_interval):
                return (
                    "stopped",
                    notification_id,
                    "Publisher stopped while awaiting terminal status.",
                )
            try:
                result = self._client.status(notification_id)
            except CoconutShellError:
                continue
            state = result.get("state")
            if not isinstance(state, str):
                state = ""
        if state in {"succeeded", "suppressed"}:
            self._write_ack(event_id, state, notification_id)
            if artifact_paths is not None:
                for path in artifact_paths:
                    path.unlink(missing_ok=True)
            return state, notification_id, artifact_reason
        if state in {"failed", "ambiguous"}:
            return (
                state,
                notification_id,
                "Coconut Shell reported a terminal delivery failure.",
            )
        return "degraded", notification_id, "Coconut Shell status polling timed out."

    def _load_artifact(self, artifact_id: str) -> tuple[bytes, str, tuple[Path, Path]]:
        if re.fullmatch(r"^[0-9a-f]{32}$", artifact_id or "") is None:
            raise CoconutShellError("notification artifact is invalid")
        artifact_dir = self._notification_dir / "artifacts"
        metadata_path = artifact_dir / f"{artifact_id}.json"
        try:
            artifact_directory = artifact_dir.lstat()
            if not stat.S_ISDIR(artifact_directory.st_mode) or stat.S_ISLNK(
                artifact_directory.st_mode
            ):
                raise ValueError
            metadata = json.loads(
                _read_regular_file(
                    metadata_path, MAX_ARTIFACT_METADATA_BYTES
                ).decode("utf-8")
            )
            filename = metadata["filename"]
            content_type = metadata["content_type"]
            data_path = artifact_dir / filename
            if (
                not isinstance(filename, str)
                or filename not in {f"{artifact_id}.jpg", f"{artifact_id}.png"}
                or metadata.get("id") != artifact_id
                or content_type not in {"image/jpeg", "image/png"}
            ):
                raise ValueError
            contents = _read_regular_file(data_path, MAX_MEDIA_BYTES)
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise CoconutShellError("notification artifact is unavailable") from None
        if not contents:
            raise CoconutShellError("notification artifact is invalid")
        return contents, content_type, (data_path, metadata_path)

    def _write_ack(self, event_id: str, state: str, notification_id: str) -> None:
        ack_dir = self._notification_dir / "acks"
        ack_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ack_dir, 0o700)
        horizon = time.time() - 30 * 24 * 3600
        for held in ack_dir.glob("[0-9a-f]" * 32 + ".json"):
            try:
                if held.stat().st_mtime < horizon:
                    held.unlink(missing_ok=True)
            except OSError:
                continue
        path = ack_dir / f"{event_id}.json"
        temporary = ack_dir / f".{event_id}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "event_id": event_id,
                    "state": state,
                    "notification_id": notification_id,
                    "confirmed_at": _rfc3339(self._clock()),
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                self._record(
                    "degraded", reason="Notification event publisher failed safely."
                )
            if self._stop.wait(1.0):
                return

    def _record(
        self,
        state: str,
        *,
        event_id: int | None = None,
        notification_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        value = {
            "state": state,
            "last_event_id": event_id,
            "last_notification_id": notification_id,
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


def _valid_source_token(value: str) -> bool:
    return SOURCE_TOKEN_PATTERN.fullmatch(value) is not None


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _hour_bucket(value: datetime) -> str:
    return "hour:" + value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def _read_regular_file(path: Path, limit: int) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise OSError("no-follow file access is unavailable")
    flags = os.O_RDONLY | no_follow
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > limit:
            raise OSError("notification artifact is not a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            contents = source.read(limit + 1)
        if not contents or len(contents) > limit:
            raise OSError("notification artifact exceeds its bound")
        return contents
    finally:
        os.close(descriptor)
