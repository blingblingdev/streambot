"""Let a job tell the console what it is doing.

The control panel reads `JOBS_ROOT/<name>/flow-log.jsonl` and builds its
event stream and its counters from it. Before this existed each job wrote
that file by hand, and the Poly Bridge job got it wrong in two ways at once:
it logged to its own file instead, so the panel showed a job that had done
nothing — correctly, since nothing reached the stream it reads — and when it
did write there, it wrote milliseconds where the console computes uptime
against `int(time.time())`, so the panel reported an uptime of minus fifty-six
thousand years.

Both are the kind of mistake a shared interface should make impossible.

    events = JobEvents("poly-bridge")
    events.start()
    events.doing("building 2-3", members=57)
    events.clicked(x=640, y=420)
    events.cycle(completed=3)

`doing` is the one to reach for while working: it says, in one line, what the
job is on right now, and the panel shows it as the job's current activity.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_JOBS_DIR = Path(
    os.environ.get(
        "STREAMBOT_JOBS_DIR",
        str(Path.home() / "Codes" / "Private" / "lolita" / "streambot-jobs" / "jobs"),
    )
)

# The kinds the console understands. `click` feeds its click counter and rate,
# `cycle` its cycle count, `start` resets the session totals. Anything else is
# carried through to the event stream as-is and shown as a line.
CLICK = "click"
CYCLE = "cycle"
START = "start"
NOTIFICATION = "notification"
NOTIFICATION_ID = re.compile(r"^[0-9a-f]{32}$")
NOTIFICATION_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_NOTIFICATION_DATA_BYTES = 16 * 1024
MAX_NOTIFICATION_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_NOTIFICATION_ARTIFACTS = 256
NOTIFICATION_ARTIFACT_RETENTION_SECONDS = 7 * 24 * 3600


class JobEvents:
    """Append-only writer for the stream the console reads.

    Writing never raises. A job that cannot log should carry on working: the
    log exists to explain the work, not to gate it.
    """

    # A new session on top of a log this big starts a fresh file instead of
    # growing it. The console persists every event into its thirty-day store
    # as it tails, so the jsonl is transport plus a recent buffer, not the
    # archive — rotation costs nothing the store has seen.
    ROTATE_BYTES = 8 * 1024 * 1024

    def __init__(self, job_name: str, jobs_dir: Path | None = None) -> None:
        self.job_name = job_name
        root = Path(jobs_dir) if jobs_dir else DEFAULT_JOBS_DIR
        self.path = root / job_name / "flow-log.jsonl"

    @property
    def notification_dir(self) -> Path:
        configured = os.environ.get("STREAMBOT_NOTIFICATION_DIR", "").strip()
        if configured:
            return Path(configured).expanduser()
        streambot_home = Path(
            os.environ.get(
                "STREAMBOT_HOME",
                str(Path.home() / "Codes" / "Private" / "lolita" / "streambot"),
            )
        ).expanduser()
        return streambot_home / ".state" / "control-panel" / "notifications"

    def _prune_notification_artifacts(self, artifact_dir: Path) -> bool:
        """Remove expired spool entries and enforce a hard metadata bound."""

        try:
            now = time.time()
            metadata_paths = sorted(
                artifact_dir.glob("[0-9a-f]" * 32 + ".json"),
                key=lambda path: path.stat().st_mtime,
            )
            expired = [
                path
                for path in metadata_paths
                if now - path.stat().st_mtime > NOTIFICATION_ARTIFACT_RETENTION_SECONDS
            ]
            for metadata_path in expired:
                artifact_id = metadata_path.stem
                metadata_path.unlink(missing_ok=True)
                (artifact_dir / f"{artifact_id}.jpg").unlink(missing_ok=True)
                (artifact_dir / f"{artifact_id}.png").unlink(missing_ok=True)
            remaining = len(metadata_paths) - len(expired)
            return remaining < MAX_NOTIFICATION_ARTIFACTS
        except OSError:
            return False

    def _rotate_if_bloated(self) -> None:
        """Truncate an oversized log, only ever between sessions.

        Between sessions is the one safe moment: the console's tail sees the
        shrink, resets, and re-reads a file that contains nothing but the new
        session — no half-session for its counters to misread, and no old
        lines to re-ingest under new numbering (which would duplicate them
        in the event store).
        """

        try:
            if self.path.stat().st_size > self.ROTATE_BYTES:
                self.path.write_text("")
        except Exception:
            pass

    def emit(self, kind: str, **fields: Any) -> bool:
        """Write one event. Seconds, because that is what the console reads."""

        line = {"event": kind, "t": int(time.time()), **fields}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            return True
        except Exception:
            return False

    def store_notification_artifact(
        self, contents: bytes, content_type: str = "image/jpeg"
    ) -> str | None:
        """Store bounded image evidence for the platform publisher.

        The job receives only an opaque identifier. The publisher validates,
        uploads, and removes the private spool files after terminal delivery.
        """

        extensions = {"image/jpeg": ".jpg", "image/png": ".png"}
        if (
            not isinstance(contents, bytes)
            or not contents
            or len(contents) > MAX_NOTIFICATION_ARTIFACT_BYTES
            or content_type not in extensions
        ):
            return None
        artifact_id = uuid.uuid4().hex
        artifact_dir = self.notification_dir / "artifacts"
        data_path = artifact_dir / f"{artifact_id}{extensions[content_type]}"
        metadata_path = artifact_dir / f"{artifact_id}.json"
        temporary_data = artifact_dir / f".{artifact_id}.data.tmp"
        temporary_metadata = artifact_dir / f".{artifact_id}.meta.tmp"
        try:
            artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(artifact_dir, 0o700)
            if not self._prune_notification_artifacts(artifact_dir):
                return None
            with temporary_data.open("xb") as handle:
                handle.write(contents)
            os.chmod(temporary_data, 0o600)
            temporary_metadata.write_text(
                json.dumps(
                    {
                        "id": artifact_id,
                        "content_type": content_type,
                        "filename": data_path.name,
                        "created_at": int(time.time()),
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.chmod(temporary_metadata, 0o600)
            temporary_data.replace(data_path)
            temporary_metadata.replace(metadata_path)
        except Exception:
            for path in (temporary_data, temporary_metadata, data_path, metadata_path):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
            return None
        return artifact_id

    def notification(
        self,
        kind: str,
        data: dict[str, Any],
        *,
        event_id: str | None = None,
        artifact_id: str | None = None,
    ) -> str | None:
        """Append one typed notification request to the shared event stream."""

        event_id = event_id or uuid.uuid4().hex
        if (
            NOTIFICATION_KIND.fullmatch(kind) is None
            or NOTIFICATION_ID.fullmatch(event_id) is None
            or not isinstance(data, dict)
            or (
                artifact_id is not None
                and NOTIFICATION_ID.fullmatch(artifact_id) is None
            )
        ):
            return None
        try:
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        if len(encoded.encode("utf-8")) > MAX_NOTIFICATION_DATA_BYTES:
            return None
        fields: dict[str, Any] = {
            "notification_kind": kind,
            "event_id": event_id,
            "data": data,
        }
        if artifact_id is not None:
            fields["artifact_id"] = artifact_id
        return event_id if self.emit(NOTIFICATION, **fields) else None

    def notification_confirmed(self, event_id: str) -> bool:
        """Return true only after Coconut Shell confirms a terminal success."""

        if NOTIFICATION_ID.fullmatch(event_id) is None:
            return False
        path = self.notification_dir / "acks" / f"{event_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("event_id") == event_id
            and payload.get("state") in {"succeeded", "suppressed"}
        )

    def start(self, **fields: Any) -> None:
        """Mark a new session; the console resets its totals here."""

        self._rotate_if_bloated()
        self.emit(START, job=self.job_name, **fields)

    def doing(self, what: str, **fields: Any) -> None:
        """Say what the job is working on right now."""

        self.emit("doing", what=what, **fields)

    def clicked(self, **fields: Any) -> None:
        """One input the console should count."""

        self.emit(CLICK, **fields)

    def cycle(self, **fields: Any) -> None:
        """One unit of work finished — a level, a pass, a sweep."""

        self.emit(CYCLE, **fields)

    def problem(self, what: str, **fields: Any) -> None:
        """Something went wrong and the job is carrying on or giving up."""

        self.emit("job-error", what=what, **fields)
