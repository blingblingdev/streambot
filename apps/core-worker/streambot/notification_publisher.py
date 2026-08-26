"""Publish one normalized Streambot platform snapshot to Coconut Shell.

The control panel remains the only collector. This module accepts the
allowlisted status values the panel already computed; it never scans jobs,
processes, logs, manifests, or control sockets on its own.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

TYPE_KEY = "streambot.hourly_status"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_MEDIA_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_METADATA_BYTES = 16 * 1024
TERMINAL_STATES = {"succeeded", "suppressed", "failed", "ambiguous"}
SOURCE_KEY = "streambot"
DEFAULT_CLI_PATH = Path.home() / ".local" / "bin" / "coconut-shell"


class PublisherConfigurationError(ValueError):
    """The publisher configuration is incomplete or unsafe."""


class CoconutShellError(RuntimeError):
    """A secret-safe Coconut Shell request failure."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class SnapshotError(ValueError):
    """The control-plane status cannot form a bounded notification snapshot."""


@dataclass(frozen=True)
class PublisherConfig:
    enabled: bool = False
    cli_path: Path = DEFAULT_CLI_PATH
    include_snapshot: bool = True
    request_timeout: float = 10.0
    terminal_timeout: float = 180.0
    poll_interval: float = 2.0
    submit_attempts: int = 3
    heartbeat_interval: float = 30.0

    @classmethod
    def from_environment(
        cls, installation_config: Path | None = None
    ) -> "PublisherConfig":
        del installation_config
        enabled = _environment_flag(
            "STREAMBOT_COCONUT_SHELL_PUBLISHER_ENABLED", False
        )
        include_snapshot = _environment_flag(
            "STREAMBOT_HOURLY_PUBLISHER_INCLUDE_SNAPSHOT", True
        )
        if not enabled:
            return cls(enabled=False, include_snapshot=include_snapshot)
        return cls(
            enabled=True,
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
    """Process adapter for the standalone Coconut Shell CLI."""

    def __init__(
        self,
        config: PublisherConfig,
        *,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        if not config.enabled:
            raise PublisherConfigurationError("publisher client requires enabled configuration")
        if not config.cli_path.is_absolute():
            raise PublisherConfigurationError("publisher CLI path must be absolute")
        self._cli_path = config.cli_path
        self._timeout = config.request_timeout
        self._runner = runner

    def submit(
        self,
        snapshot: StreambotNotificationSnapshot,
        idempotency_key: str,
        image_paths: list[Path],
    ) -> dict[str, Any]:
        return self.submit_event(
            TYPE_KEY,
            idempotency_key,
            snapshot.collected_at,
            snapshot.payload(),
            image_paths,
        )

    def submit_event(
        self,
        type_key: str,
        idempotency_key: str,
        occurred_at: datetime,
        data: dict[str, Any],
        image_paths: list[Path],
    ) -> dict[str, Any]:
        if (
            not re.fullmatch(r"^streambot\.[a-z0-9_.]{1,100}$", type_key)
            or not _valid_identifier(idempotency_key)
            or not isinstance(data, dict)
            or len(image_paths) > 4
        ):
            raise CoconutShellError("notification event is invalid")
        occurred_at = occurred_at.astimezone(timezone.utc)
        message, title, summary = _message_for(type_key, data, bool(image_paths))
        images: dict[str, str] = {}
        for index, path in enumerate(image_paths):
            if not path.is_absolute():
                raise CoconutShellError("notification image path is invalid")
            images[f"image_{index + 1}"] = str(path)
        if images:
            message = _attach_images(message, tuple(images))
        payload = {
                "contract": "native-feishu-v1",
                "source": SOURCE_KEY,
                "type": type_key,
                "idempotency_key": idempotency_key,
                "occurred_at": _rfc3339(occurred_at),
                "audit": {"title": title, "summary": summary},
                "message": message,
                "images": images,
                "tasks": [],
            }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(body) > 64 * 1024:
            raise CoconutShellError("notification event is too large")
        result = self._run("publish", payload)
        if result.get("kind") != "notification" or not isinstance(result.get("notification_id"), str):
            raise CoconutShellError("Coconut Shell response is invalid")
        return {"id": result["notification_id"], "state": result.get("state")}

    def status(self, notification_id: str) -> dict[str, Any]:
        if not _valid_identifier(notification_id):
            raise CoconutShellError("Coconut Shell notification identifier is invalid")
        result = self._run("status", None, notification_id)
        if result.get("kind") != "notification":
            raise CoconutShellError("Coconut Shell response is invalid")
        return {"id": result.get("notification_id", notification_id), "state": result.get("state")}

    def heartbeat(self, _publisher_status: dict[str, Any]) -> dict[str, Any]:
        result = self._run("--version", None)
        return {"status": "accepted", "revision": result.get("version", "")}

    def report_cycle(
        self,
        *,
        bucket: str,
        observed_at: datetime,
        outcome: str,
        notification_expected: bool = False,
        failure_code: str = "",
    ) -> dict[str, Any]:
        if outcome not in {"silent", "notification_expected", "notification_accepted", "failed"}:
            raise CoconutShellError("producer cycle outcome is invalid")
        observed_at = observed_at.astimezone(timezone.utc)
        next_hour = observed_at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        payload = {
                "source": SOURCE_KEY,
                "type": TYPE_KEY,
                "cycle_key": bucket,
                "started_at": _rfc3339(observed_at),
                "completed_at": _rfc3339(observed_at),
                "outcome": outcome,
                "expected_next_at": _rfc3339(next_hour),
                "grace_seconds": 5 * 60,
                "notification_idempotency_key": bucket if notification_expected else "",
                "failure_code": failure_code,
            }
        result = self._run("cycle", payload)
        if result.get("kind") != "cycle" or result.get("state") != "accepted":
            raise CoconutShellError("Coconut Shell response is invalid")
        return {"id": result.get("cycle_id", ""), "replayed": bool(result.get("replayed"))}

    def _run(self, command: str, payload: dict[str, Any] | None, argument: str | None = None) -> dict[str, Any]:
        arguments = [str(self._cli_path), command]
        if argument is not None:
            arguments.append(argument)
        if command not in {"--version", "status"}:
            arguments.append("--quiet")
        try:
            completed = self._runner(
                arguments,
                input=json.dumps(payload, separators=(",", ":")) if payload is not None else None,
                text=True,
                capture_output=True,
                timeout=max(self._timeout, 1.0) + 180.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise CoconutShellError("Coconut Shell request did not confirm a response") from None
        if len(completed.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise CoconutShellError("Coconut Shell response is too large")
        try:
            decoded = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise CoconutShellError("Coconut Shell response is invalid") from None
        if completed.returncode != 0 or not isinstance(decoded, dict) or decoded.get("ok") is not True:
            code = ""
            if isinstance(decoded, dict):
                error = decoded.get("error")
                if isinstance(error, dict) and _valid_identifier(error.get("code")):
                    code = error["code"]
            raise CoconutShellError("Coconut Shell response is invalid", code=code)
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
            try:
                self._client.report_cycle(
                    bucket=bucket,
                    observed_at=now,
                    outcome="failed",
                    failure_code="snapshot_invalid",
                )
            except CoconutShellError:
                pass
            return self._record(
                "degraded", bucket, now, reason="Control-plane snapshot validation failed."
            )
        if not snapshot.running_jobs:
            try:
                self._client.report_cycle(
                    bucket=bucket,
                    observed_at=now,
                    outcome="silent",
                )
            except CoconutShellError:
                return self._record(
                    "degraded",
                    bucket,
                    now,
                    reason="Coconut Shell did not confirm the silent producer cycle.",
                )
            return self._record(
                "idle_skip", bucket, now, reason="No registered job is running."
            )

        image_paths: list[Path] = []
        temporary_image: Path | None = None
        attachment_reason = ""
        if self.config.include_snapshot:
            try:
                captured = self._capture_provider()
                if captured is not None:
                    contents, content_type = captured
                    temporary_image = _write_temporary_image(contents, content_type)
                    image_paths.append(temporary_image)
            except Exception:
                attachment_reason = "Optional snapshot was unavailable."

        result: dict[str, Any] | None = None
        for attempt in range(self.config.submit_attempts):
            try:
                result = self._client.submit(snapshot, bucket, image_paths)
                break
            except CoconutShellError as error:
                if attempt + 1 == self.config.submit_attempts:
                    try:
                        self._client.report_cycle(
                            bucket=bucket,
                            observed_at=now,
                            outcome="notification_expected",
                            notification_expected=True,
                        )
                    except CoconutShellError:
                        pass
                    _unlink_optional(temporary_image)
                    return self._record(
                        "degraded",
                        bucket,
                        now,
                        reason=_coconut_shell_failure_reason(
                            "Coconut Shell did not confirm notification acceptance.", error
                        ),
                    )
                if self._stop.wait(min(2**attempt, 4)):
                    _unlink_optional(temporary_image)
                    return self._record(
                        "stopped", bucket, now, reason="Publisher stopped during retry."
                    )
        _unlink_optional(temporary_image)
        assert result is not None
        notification_id = result.get("id")
        state = result.get("state")
        if not _valid_identifier(notification_id) or not isinstance(state, str):
            return self._record(
                "degraded", bucket, now, reason="Coconut Shell acceptance response was invalid."
            )
        try:
            self._client.report_cycle(
                bucket=bucket,
                observed_at=now,
                outcome="notification_accepted",
                notification_expected=True,
            )
        except CoconutShellError:
            return self._record(
                "degraded",
                bucket,
                now,
                notification_id=notification_id,
                reason="Coconut Shell accepted the notification but not its producer cycle.",
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
            observed_at = self._clock().astimezone(timezone.utc)
            self.run_once(max(observed_at, next_hour))

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
        image_paths: list[Path] = []
        artifact_id = event.get("artifact_id")
        artifact_policy = route["artifact"]
        artifact_paths: tuple[Path, ...] | None = None
        artifact_reason = ""
        if artifact_id is not None:
            if artifact_policy == "none":
                return "ignored", None, "Unexpected notification artifact was skipped."
            try:
                _contents, _content_type, loaded_paths = self._load_artifact(artifact_id)
            except CoconutShellError:
                if artifact_policy == "required":
                    return "degraded", None, "Required notification artifact was unavailable."
                artifact_reason = "Optional notification artifact was unavailable."
            else:
                artifact_paths = loaded_paths
                image_paths.append(loaded_paths[0])
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
                    image_paths,
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
                try:
                    for path in artifact_paths:
                        path.unlink(missing_ok=True)
                except OSError:
                    return (
                        "degraded",
                        notification_id,
                        "Confirmed notification media cleanup is incomplete.",
                    )
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

    def _artifact_paths(self, artifact_id: str) -> tuple[Path, ...]:
        artifact_dir = self._notification_dir / "artifacts"
        return (
            artifact_dir / f"{artifact_id}.jpg",
            artifact_dir / f"{artifact_id}.png",
            artifact_dir / f"{artifact_id}.json",
        )

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


def _write_temporary_image(contents: bytes, content_type: str) -> Path:
    if not contents or len(contents) > MAX_MEDIA_BYTES or content_type not in {"image/jpeg", "image/png"}:
        raise CoconutShellError("notification snapshot is invalid")
    suffix = ".jpg" if content_type == "image/jpeg" else ".png"
    descriptor, raw_path = tempfile.mkstemp(prefix="streambot-coconut-shell-", suffix=suffix)
    path = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _unlink_optional(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _coconut_shell_failure_reason(prefix: str, error: CoconutShellError) -> str:
    if error.code:
        return f"{prefix.rstrip('.')} ({error.code})."
    return prefix


def _attach_images(message: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    card = dict(message["card"])
    elements = list(card["elements"])
    for name in names:
        elements.append({
            "tag": "img",
            "img_key": f"coconut://image/{name}",
            "alt": {"tag": "plain_text", "content": "Streambot evidence"},
        })
    card["elements"] = elements
    return {"msg_type": "interactive", "card": card}


def _message_for(type_key: str, data: dict[str, Any], has_image: bool) -> tuple[dict[str, Any], str, str]:
    try:
        if type_key == "streambot.hourly_status":
            running = data["running_jobs"]
            names = [job["display_name"] for job in running]
            count = _integer(data["running_count"])
            if count <= 0 or count != len(names):
                raise ValueError
            summary = f"{count} {'job is' if count == 1 else 'jobs are'} currently running under Streambot: {_summarize_names(names)}."
            title = "Streambot hourly status"
            return _standard_card(
                title, "Platform job activity report", "blue", "Active",
                [("Running", str(count)), ("Registered", str(data["registered_count"])),
                 ("Collected", str(data["collected_at"])), ("Snapshot", "Attached" if has_image else "Not attached")],
                summary, "Collected once by the Streambot control plane; Coconut Shell owns delivery.",
            ), title, summary
        if type_key == "streambot.poly_bridge.completed":
            level = _required_text(data["level"], 64)
            placed, planned, missing = _integer(data["placed_count"]), _integer(data["planned_count"]), _integer(data["missing_count"])
            if not isinstance(data["complete"], bool):
                raise ValueError
            complete = data["complete"]
            if planned <= 0 or placed < 0 or placed > planned or missing != planned - placed or (complete and missing):
                raise ValueError
            title = f"Poly Bridge {level}"
            summary = f"Level {level} was saved with {placed} of {planned} planned members placed."
            missing_text = _optional_text(data.get("missing_summary"), 160) or "None"
            return _standard_card(
                title, "Saved build result", "green" if complete else "orange", "Complete" if complete else "Partial",
                [("Placed", f"{placed}/{planned}"), ("Missing", missing_text),
                 ("Duration", _format_duration(_number(data["duration_seconds"]))), ("Snapshot", "Optional evidence")],
                summary, "Collected by the Streambot Poly Bridge job; delivered by Coconut Shell.",
            ), title, summary
        if type_key == "streambot.poly_bridge.stalled":
            idle, threshold = _integer(data["idle_seconds"]), _integer(data["threshold_seconds"])
            if idle <= 0 or threshold <= 0 or idle < threshold:
                raise ValueError
            last_command = _optional_text(data.get("last_command"), 120) or "Unavailable"
            title = "Poly Bridge stalled"
            summary = "The job may still be alive, but no input has reached the game beyond the configured threshold."
            return _standard_card(
                title, "No game input reached the worker", "red", "Intervention required",
                [("Idle", _format_duration(idle)), ("Threshold", _format_duration(threshold)),
                 ("Last command", last_command), ("Profile", "Operations")],
                summary, "Inspect the Streambot control plane before restarting or intervening.",
            ), title, summary
        if type_key == "streambot.pilot.assistance_required":
            job = _required_text(data["job"], 64)
            outcome = data["outcome"]
            attempts = _integer(data["attempt_count"])
            if outcome not in {"abstained", "gave-up", "timeout"} or not 0 <= attempts <= 20:
                raise ValueError
            page_key = _required_text(data["page_key"], 120)
            title = "Pilot assistance required"
            summary = "The automated resolver could not confirm progress and returned the page for operator review."
            return _standard_card(
                title, job, "orange", "Parked",
                [("Outcome", outcome), ("Attempts", str(attempts)), ("Page key", page_key), ("Input", "Paused")],
                summary, "Teach the resolution through the existing Pilot workflow; no restart is required.",
            ), title, summary
        if type_key == "streambot.marketplace_match":
            item = _required_text(data["item"], 120)
            observed, threshold = _integer(data["observed_price"]), _integer(data["threshold"])
            operator = data["operator"]
            if observed < 0 or threshold < 0 or operator not in {"<", "<=", ">", ">=", "=="} or not _condition(observed, operator, threshold):
                raise ValueError
            title = "Marketplace watch matched"
            summary = f"{item} was observed at {observed} and matched the configured {operator} {threshold} condition."
            return _standard_card(
                title, item, "green", "Matched",
                [("Item", item), ("Observed", str(observed)), ("Condition", f"{operator} {threshold}"), ("Action", "Review manually")],
                summary, "Observation only; Streambot never purchases marketplace items.",
            ), title, summary
    except (KeyError, TypeError, ValueError, OverflowError):
        raise CoconutShellError("notification event is invalid") from None
    raise CoconutShellError("notification event type is unsupported")


def _standard_card(
    title: str, subtitle: str, template: str, status: str,
    facts: list[tuple[str, str]], outcome: str, note: str,
) -> dict[str, Any]:
    fields = [{"is_short": True, "text": {"tag": "lark_md", "content": f"**{_escape_md(label)}**\n{_escape_md(value)}"}}
              for label, value in [("Status", status), *facts]]
    return {"msg_type": "interactive", "card": {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content": subtitle}},
            {"tag": "div", "fields": fields},
            {"tag": "div", "text": {"tag": "plain_text", "content": outcome}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": note}]},
        ],
    }}


def _required_text(value: Any, limit: int) -> str:
    if not _valid_text(value, limit):
        raise ValueError
    return value


def _escape_md(value: str) -> str:
    value = value.replace("<", "＜").replace(">", "＞")
    return re.sub(r"([\\*_\[\]()`])", r"\\\1", value)


def _optional_text(value: Any, limit: int) -> str:
    if value in {None, ""}:
        return ""
    return _required_text(value, limit)


def _format_duration(seconds: float) -> str:
    if seconds <= 0 or seconds > 7 * 24 * 60 * 60:
        return "Unavailable"
    return f"{seconds:.0f} seconds" if seconds < 60 else f"{seconds / 60:.1f} minutes"


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    return value


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    return float(value)


def _summarize_names(names: list[str]) -> str:
    return ", ".join(names) if len(names) <= 5 else f"{', '.join(names[:5])}, and {len(names) - 5} more"


def _condition(observed: int, operator: str, threshold: int) -> bool:
    return {"<": observed < threshold, "<=": observed <= threshold, ">": observed > threshold,
            ">=": observed >= threshold, "==": observed == threshold}[operator]
