#!/usr/bin/env python3
"""Local web control console for the streambot automation worker.

Run this ONCE from your own terminal (a launch context that holds macOS Local
Network permission). The console spawns and supervises the persistent worker as
a child process, so on macOS 15 the worker's local-network access is attributed
to this console's responsible code and inherits its grant — which is why
launching the worker from here succeeds where an unattributed automation shell
gets EHOSTUNREACH. See README.md for the full mechanism.

The console binds to 127.0.0.1 only, keeps no host address or credential on
disk, and reuses the existing IPC control plane (`.state/<worker>/...sock`) for
every worker interaction. It never opens its own stream connection.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

if sys.prefix == sys.base_prefix:
    if not VENV_PYTHON.is_file():
        raise SystemExit("Project environment is missing; run ./scripts/bootstrap.sh first")
    os.execv(VENV_PYTHON, [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

from streambot.config import ConfigurationError  # noqa: E402
from streambot.control_plane import send_control_command  # noqa: E402
from streambot.job_config import (  # noqa: E402
    ConfigSchema,
    read_values,
    values_path,
    write_values,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOG_DIR = PROJECT_ROOT / ".state" / "control-panel"

# What the console will serve out of `static/`. An allowlist rather than
# `mimetypes.guess_type`, so a stray file in the build output cannot turn the
# operator console into a general-purpose file server.
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

# Jobs can live outside this checkout (a separately managed jobs repository).
# STREAMBOT_JOBS_DIR (or --jobs-dir) points at the directory holding
# <name>/job.json entries; runner paths in job.json resolve against that
# directory's PARENT — the root of whichever repository the jobs live in —
# so a jobs repo keeps repo-root-relative runner paths exactly like this one.
JOBS_ROOT = Path(
    os.environ.get("STREAMBOT_JOBS_DIR", str(PROJECT_ROOT / "jobs"))
).expanduser().resolve()
# The console supervises the target-agnostic core engine, never a specific
# job's worker. The running job publishes its own scene via the `report-scene`
# IPC seam, so the overlay reflects whatever job is active — not a baked-in one.
WORKER_SCRIPT = PROJECT_ROOT / "apps" / "core-worker" / "core_worker.py"
UNUSED_OUTPUT = "/tmp/streambot-control-panel-unused.png"


class WorkerSupervisor:
    """Own at most one worker child process launched from this console."""

    def __init__(self, state_dir: Path, socket_path: Path) -> None:
        self.state_dir = state_dir
        self.socket_path = socket_path
        self._process: subprocess.Popen | None = None
        self._log_path: Path | None = None
        self._lock = threading.Lock()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return {"ok": False, "error": "AlreadyRunning"}
            if self.socket_path.exists():
                # Occupied only if a live worker process actually owns it. A
                # crashed or SIGKILLed worker never unlinks its socket, and
                # refusing on the file alone wedges the console until someone
                # removes it by hand (AGENTS.md: a leftover socket is stale
                # only once the owning process is confirmed gone).
                if self.external_pid() is not None:
                    return {"ok": False, "error": "SocketOwnedElsewhere"}
                try:
                    self.socket_path.unlink()
                except OSError:
                    return {"ok": False, "error": "StaleSocketUnremovable"}
            LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._log_path = LOG_DIR / "worker.log"
            log = open(self._log_path, "a", encoding="utf-8")
            # Child inherits this console's environment and responsible-code
            # attribution, so it inherits the console's Local Network grant.
            self._process = subprocess.Popen(
                [
                    str(VENV_PYTHON),
                    str(WORKER_SCRIPT),
                    "--state-dir",
                    str(self.state_dir),
                ],
                cwd=str(PROJECT_ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=False,
            )
            return {"ok": True, "pid": self._process.pid}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=8.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4.0)
                self._process = None
                return {"ok": True}
            self._process = None
            # Adopted worker from a previous console generation: same
            # semantics as adopted jobs — targeted SIGTERM, bounded wait,
            # SIGKILL only if it ignores the term.
            external = self.external_pid()
            if external is None:
                return {"ok": False, "error": "NotRunning"}
            try:
                os.kill(external, signal.SIGTERM)
                for _ in range(50):
                    time.sleep(0.2)
                    os.kill(external, 0)
                os.kill(external, signal.SIGKILL)
            except ProcessLookupError:
                pass  # exited: exactly what stop wants
            except OSError:
                return {"ok": False, "error": "SignalFailed"}
            return {"ok": True}

    def external_pid(self) -> int | None:
        """Pid of a live worker this console did not start, else None.

        Adoption mirrors the job scan: pgrep candidates are verified by
        their full command line (a python interpreter running
        core_worker.py), so an editor or pager holding the file is never
        signaled. Cached briefly — status polls every second."""

        now = time.monotonic()
        cached_at, cached = getattr(self, "_ext_cache", (0.0, None))
        if now - cached_at < 2.0:
            return cached
        found: int | None = None
        try:
            result = subprocess.run(
                ["pgrep", "-f", "core_worker.py"],
                capture_output=True, text=True, timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None:
            own = self.owned_pid()
            for token in result.stdout.split():
                try:
                    pid = int(token)
                except ValueError:
                    continue
                if pid in (own, os.getpid()):
                    continue
                if self._is_worker_process(pid):
                    found = pid
                    break
        self._ext_cache = (now, found)
        return found

    @staticmethod
    def _is_worker_process(pid: int) -> bool:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        command = result.stdout.strip()
        if "core_worker.py" not in command:
            return False
        parts = command.split()
        interpreter = parts[0] if parts else ""
        return "python" in Path(interpreter).name.lower()

    def owned_pid(self) -> int | None:
        process = self._process
        if process is not None and process.poll() is None:
            return process.pid
        return None

    def recent_log(self, lines: int = 12) -> list[str]:
        """The worker log's tail, whoever started the worker.

        Every console generation appends to the same fixed file, so a console
        that merely ADOPTED a running worker still has its log — the previous
        behaviour of returning nothing for an adopted worker meant the Logs
        tab went blank exactly when an operator had restarted the console,
        which is when they are most likely to be looking.

        Reads only the tail: this is called once a second per SSE client, and
        the log grows for as long as the worker lives.
        """

        path = self._log_path or (LOG_DIR / "worker.log")
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 16_384))
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        tail = text.splitlines()
        # The first line of a mid-file read is almost always a fragment.
        if size > 16_384 and tail:
            tail = tail[1:]
        return tail[-lines:]


class FlowLogReader:
    """Incremental flow-log.jsonl reader: session totals plus a recent-event ring.

    Reads only bytes appended since the previous poll, so per-tick cost stays
    proportional to new activity. Session aggregates (uptime, total clicks,
    cycles, mean score) reset on each `start` event; the ring feeds the
    console's scrolling event stream with a monotonically increasing index
    the client uses for append-only dedupe.
    """

    RING_SIZE = 120
    # "job-error" is what streambot.job_events.problem() writes, and it is how
    # every job built on the shared runtime reports trouble; without it the
    # panel's error count stays at zero while the feed fills with warnings.
    ERROR_EVENTS = {
        "poll-error", "frame-skip", "click-skip", "classify-skip", "job-error",
    }

    def __init__(
        self,
        path: Path,
        store: "MetricsStore | None" = None,
        job: str | None = None,
    ) -> None:
        self.path = path
        self.store = store
        self.job = job
        self._offset = 0
        self._remainder = ""
        self._line_no = 0
        self._ring: deque[dict[str, Any]] = deque(maxlen=self.RING_SIZE)
        self._session: dict[str, Any] | None = None

    def _reset(self) -> None:
        self._offset = 0
        self._remainder = ""
        self._line_no = 0
        self._ring.clear()
        self._session = None

    def poll(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            self._reset()
            return
        if size < self._offset:
            self._reset()
        if size == self._offset:
            return
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._offset)
                chunk = handle.read(size - self._offset)
        except OSError:
            return
        self._offset = size
        text = self._remainder + chunk
        lines = text.split("\n")
        self._remainder = lines.pop()  # trailing partial line, if any
        rows: list[tuple[str, str, int, float]] = []
        event_rows: list[tuple[str, int, int, str]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            self._line_no += 1
            event["i"] = self._line_no
            self._ring.append(event)
            if self.store is not None and self.job:
                rows.extend(self.store.rows_from_event(self.job, event))
                t = event.get("t")
                if isinstance(t, (int, float)):
                    # The verbatim line, so the store holds what the job said
                    # rather than a re-serialization of it.
                    event_rows.append((self.job, int(t), self._line_no, line))
            self._apply(event)
        if (rows or event_rows) and self.store is not None:
            # One transaction per poll, not per event: a cold console re-reads
            # a whole flow log on its first poll, and thousands of individual
            # commits would turn that into seconds of fsync.
            self.store.write(rows, event_rows)

    def _apply(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "start":
            self._session = {
                "started_t": event.get("t"),
                "clicks": 0,
                "cycles": 0,
                "score_sum": 0.0,
                "score_n": 0,
            }
            return
        session = self._session
        if session is None:
            # Tail started mid-session: accumulate without a start marker.
            session = self._session = {
                "started_t": event.get("t"),
                "clicks": 0,
                "cycles": 0,
                "score_sum": 0.0,
                "score_n": 0,
            }
        if kind == "click":
            session["clicks"] += 1
            score = event.get("score")
            if isinstance(score, (int, float)):
                session["score_sum"] += float(score)
                session["score_n"] += 1
        elif kind == "cycle":
            completed = event.get("completed")
            session["cycles"] = (
                int(completed)
                if isinstance(completed, int)
                else session["cycles"] + 1
            )

    def metrics(self, window: float = 60.0) -> dict[str, Any] | None:
        """Macro session aggregates plus a recent window, or None if no data."""

        if not self._ring and self._session is None:
            return None
        now = int(time.time())
        ring = list(self._ring)
        recent = [e for e in ring if now - e.get("t", 0) <= window]
        clicks = [e for e in recent if e.get("event") == "click"]
        errors = [e for e in recent if e.get("event") in self.ERROR_EVENTS]
        perceive = [
            e["perceive_ms"]
            for e in recent
            if isinstance(e.get("perceive_ms"), (int, float))
        ]
        act = [e["act_ms"] for e in clicks if isinstance(e.get("act_ms"), (int, float))]
        # How long it took to work out WHERE to click, as opposed to how long
        # looking took (perceive) or clicking took (act). Without it the panel
        # showed the two ends of a step and nothing of the middle.
        resolve = [
            e["resolve_ms"]
            for e in recent
            if isinstance(e.get("resolve_ms"), (int, float))
        ]
        all_clicks = [e for e in ring if e.get("event") == "click"]
        last = all_clicks[-1] if all_clicks else None
        session = self._session or {}
        started = session.get("started_t")
        score_n = session.get("score_n", 0)
        return {
            # Macro (since the session's `start` event).
            "uptime_s": (now - started) if isinstance(started, int) else None,
            "clicks_total": session.get("clicks", 0),
            "cycles": session.get("cycles", 0),
            "mean_score": (
                round(session.get("score_sum", 0.0) / score_n, 3) if score_n else None
            ),
            # Recent window (operational health).
            "clicks_per_min": round(len(clicks) * 60.0 / window, 1),
            "last_score": (
                round(float(last["score"]), 3)
                if last and isinstance(last.get("score"), (int, float))
                else None
            ),
            "last_action": (last.get("element") if last else None),
            "last_action_age_s": (now - last["t"]) if last and "t" in last else None,
            "perceive_ms": round(sum(perceive) / len(perceive)) if perceive else None,
            "resolve_ms": round(sum(resolve) / len(resolve)) if resolve else None,
            "act_ms": round(sum(act) / len(act)) if act else None,
            "errors_recent": len(errors),
        }


    def recent_events(self, limit: int = 60) -> list[dict[str, Any]]:
        # With a store the feed is read back from it — durable across console
        # restarts and log rotation, spanning sessions, ids stable. The ring
        # remains for storeless use and for metrics()' recent window.
        if self.store is not None and self.job:
            return self.store.recent_events(self.job, limit)
        return list(self._ring)[-limit:]


class MetricsStore:
    """Chart history in one SQLite file, kept for thirty days.

    The series used to live in memory, capped at a couple of thousand points
    and cleared on every session start — history vanished with a restart and
    a long run crammed itself into whatever fit. This is the time-series
    treatment instead: everything the flow logs produce, persisted, queryable
    by an arbitrary window, zero-filled where nothing happened (a gap in the
    data IS data — the job was not looking), and bounded by retention rather
    than by a point cap.

    Plain SQLite rather than a dedicated TSDB, deliberately: at our write
    rate (a few points a second across every series) the specialised engines'
    strengths never come into play, and this repo's dependency discipline is
    worth more than their column stores. Measured on this machine: a full
    day's points insert in 0.14s and a chart query answers in ~10ms.

    Three tables. `points` holds raw samples keyed (job, series, t) — REPLACE
    on conflict makes re-ingesting a flow log idempotent, which is what lets
    a cold console rebuild from the logs it finds. `rollup` holds five-minute
    aggregates behind a watermark, rebuilt incrementally from raw whole
    buckets at a time (delete-and-recompute, so it is idempotent too); wide
    windows read the rollup and stay fast no matter how much raw exists.
    `events` holds the flow-log lines themselves, keyed (job, t, seq) with
    INSERT OR IGNORE — so re-ingesting is idempotent *and* keeps each row's
    original rowid, which the feed uses as its stable, monotonic event id.
    A job restarting appends rather than replacing: its history accumulates
    across sessions and leaves by retention, not by being overwritten.
    """

    RETENTION_SECONDS = 30 * 24 * 3600
    ROLLUP_STEP = 300
    TARGET_BUCKETS = 600
    STEPS = (2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400)
    TIMING_SERIES = ("perceive", "resolve", "act", "score")

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        # Must precede table creation to take effect on a fresh file: without
        # it, retention deletes free pages but the file never shrinks, so a
        # month-old console would plateau at its historical maximum forever.
        self._db.execute("pragma auto_vacuum=incremental")
        self._db.execute("pragma journal_mode=WAL")
        self._db.execute("pragma synchronous=NORMAL")
        self._db.executescript(
            """
            create table if not exists points(
              job text not null, series text not null,
              t integer not null, value real not null,
              primary key (job, series, t)
            ) without rowid;
            create table if not exists rollup(
              job text not null, series text not null,
              bucket integer not null, total real not null, n integer not null,
              primary key (job, series, bucket)
            ) without rowid;
            create table if not exists meta(key text primary key, value integer);
            create table if not exists events(
              job text not null, t integer not null,
              seq integer not null, payload text not null,
              primary key (job, t, seq)
            );
            -- For a job's index entries the rowids come out ordered, which is
            -- what makes "the newest N of this job" an index walk, not a scan.
            create index if not exists events_job on events(job);
            """
        )
        self._db.commit()
        self._last_retention = 0.0

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def rows_from_event(
        job: str, event: dict[str, Any]
    ) -> list[tuple[str, str, int, float]]:
        """The chart samples one flow-log event carries."""

        t = event.get("t")
        kind = event.get("event")
        if not isinstance(t, (int, float)) or kind not in ("perceive", "click"):
            return []
        t = int(t)
        rows: list[tuple[str, str, int, float]] = []
        for field, series in (
            ("perceive_ms", "perceive"),
            ("resolve_ms", "resolve"),
            ("act_ms", "act"),
        ):
            value = event.get(field)
            if isinstance(value, (int, float)):
                rows.append((job, series, t, float(value)))
        if kind == "click":
            rows.append((job, "click", t, 1.0))
            score = event.get("score")
            if isinstance(score, (int, float)):
                rows.append((job, "score", t, float(score)))
        return rows

    def write(
        self,
        rows: list[tuple[str, str, int, float]],
        events: list[tuple[str, int, int, str]] = [],
    ) -> None:
        """One transaction: chart samples plus the raw events they came from.

        `events` rows are (job, t, seq, payload) with payload the verbatim
        flow-log line; OR IGNORE keeps the first ingest's rowid forever.
        """

        if not rows and not events:
            return
        with self._lock:
            if events:
                self._db.executemany(
                    "insert or ignore into events values(?,?,?,?)", events
                )
            if not rows:
                self._db.commit()
                self._retention_locked()
                return
            self._db.executemany(
                "insert or replace into points values(?,?,?,?)", rows
            )
            # Data can arrive from behind the watermark: a job's log ingested
            # for the first time after the rollup has already advanced (this
            # is exactly how a stopped job's July history arrives in August).
            # Those buckets were folded before the data existed, so refold
            # them for the jobs just written — whole buckets, so idempotent.
            row = self._db.execute(
                "select value from meta where key='rollup_watermark'"
            ).fetchone()
            watermark = int(row[0]) if row else 0
            oldest = min(r[2] for r in rows)
            if watermark and oldest < watermark:
                for job in {r[0] for r in rows}:
                    self._db.execute(
                        """
                        insert or replace into rollup
                        select job, series, (t / ?) * ? as bucket, sum(value), count(*)
                        from points where job=? and t >= ? and t < ?
                        group by job, series, bucket
                        """,
                        (
                            self.ROLLUP_STEP,
                            self.ROLLUP_STEP,
                            job,
                            (oldest // self.ROLLUP_STEP) * self.ROLLUP_STEP,
                            watermark,
                        ),
                    )
            self._db.commit()
            self._retention_locked()

    def _retention_locked(self) -> None:
        now = time.monotonic()
        if now - self._last_retention < 3600 and self._last_retention:
            return
        self._last_retention = now
        horizon = int(time.time()) - self.RETENTION_SECONDS
        self._db.execute("delete from points where t < ?", (horizon,))
        self._db.execute("delete from rollup where bucket < ?", (horizon,))
        self._db.execute("delete from events where t < ?", (horizon,))
        self._db.commit()
        self._db.execute("pragma incremental_vacuum")

    def _advance_rollup_locked(self) -> None:
        """Fold completed five-minute buckets of raw into the rollup.

        Whole buckets are recomputed from raw (REPLACE), never incremented,
        so re-running over the same data cannot double-count.
        """

        row = self._db.execute(
            "select value from meta where key='rollup_watermark'"
        ).fetchone()
        watermark = int(row[0]) if row else 0
        done = (int(time.time()) // self.ROLLUP_STEP - 1) * self.ROLLUP_STEP
        if done <= watermark:
            return
        self._db.execute(
            """
            insert or replace into rollup
            select job, series, (t / ?) * ? as bucket, sum(value), count(*)
            from points where t >= ? and t < ?
            group by job, series, bucket
            """,
            (self.ROLLUP_STEP, self.ROLLUP_STEP, watermark, done),
        )
        self._db.execute(
            "insert or replace into meta values('rollup_watermark', ?)", (done,)
        )
        self._db.commit()

    def recent_events(self, job: str, limit: int = 60) -> list[dict[str, Any]]:
        """The newest events of one job, oldest first, `i` = stable rowid."""

        with self._lock:
            rows = self._db.execute(
                "select rowid, payload from events where job=?"
                " order by rowid desc limit ?",
                (job, limit),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for rowid, payload in reversed(rows):
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                event["i"] = rowid
                out.append(event)
        return out

    def last_event_t(self, job: str) -> int | None:
        """When this job last said anything — an index seek, safe per tick."""

        with self._lock:
            row = self._db.execute(
                "select max(t) from events where job=?", (job,)
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def pick_step(self, span: int) -> int:
        """The bucket width that keeps a window near the target point count."""

        wanted = max(1, span // self.TARGET_BUCKETS)
        for step in self.STEPS:
            if step >= wanted:
                return step
        return self.STEPS[-1]

    def history(self, start: int, end: int) -> dict[str, Any]:
        """Every job's chart series over [start, end), on one zero-filled grid.

        All jobs together, deliberately: what ran when, side by side, is the
        question a shared timeline answers — the charts draw one line per job
        rather than making the operator pick. Only jobs with at least one
        sample in the window appear; each present job's series are complete
        and zero-filled, so a bucket nobody looked in reads as zero and the
        window can slide without ever meeting a hole.
        """

        span = max(1, end - start)
        step = self.pick_step(span)
        # Both edges on the grid, so the shape is exactly (end-start)/step and
        # a client can compute every timestamp from start + i*step.
        first = (start // step) * step
        end = ((end + step - 1) // step) * step
        buckets = range(first, end, step)

        with self._lock:
            use_rollup = step >= self.ROLLUP_STEP
            if use_rollup:
                self._advance_rollup_locked()
                rows = self._db.execute(
                    """
                    select job, series, (bucket / ?) * ?, sum(total), sum(n)
                    from rollup where bucket >= ? and bucket < ?
                    group by job, series, 3
                    """,
                    (step, step, first, end),
                ).fetchall()
                # The tail past the watermark only exists in raw.
                mark = self._db.execute(
                    "select value from meta where key='rollup_watermark'"
                ).fetchone()
                tail_from = int(mark[0]) if mark else first
                if tail_from < end:
                    rows += self._db.execute(
                        """
                        select job, series, (t / ?) * ?, sum(value), count(*)
                        from points where t >= ? and t < ?
                        group by job, series, 3
                        """,
                        (step, step, max(first, tail_from), end),
                    ).fetchall()
            else:
                rows = self._db.execute(
                    """
                    select job, series, (t / ?) * ?, sum(value), count(*)
                    from points where t >= ? and t < ?
                    group by job, series, 3
                    """,
                    (step, step, first, end),
                ).fetchall()

        timings: dict[str, dict[str, dict[int, tuple[float, int]]]] = {}
        clicks: dict[str, dict[int, float]] = {}
        for job, series, bucket, total, n in rows:
            bucket = int(bucket)
            if series == "click":
                held = clicks.setdefault(job, {})
                held[bucket] = held.get(bucket, 0.0) + float(total)
            elif series in self.TIMING_SERIES:
                held = timings.setdefault(job, {}).setdefault(series, {})
                if bucket in held:
                    prior_total, prior_n = held[bucket]
                    held[bucket] = (prior_total + float(total), prior_n + int(n))
                else:
                    held[bucket] = (float(total), int(n))

        per_minute = 60.0 / step
        jobs_payload: dict[str, dict[str, list[float]]] = {}
        for job in sorted(set(timings) | set(clicks)):
            series_of = timings.get(job, {})
            job_clicks = clicks.get(job, {})

            def averaged(series: str) -> list[float]:
                held = series_of.get(series, {})
                return [
                    round(held[b][0] / held[b][1], 2)
                    if b in held and held[b][1]
                    else 0.0
                    for b in buckets
                ]

            jobs_payload[job] = {
                "perceive": averaged("perceive"),
                "resolve": averaged("resolve"),
                "act": averaged("act"),
                "score": averaged("score"),
                "cpm": [
                    round(job_clicks.get(b, 0.0) * per_minute, 2) for b in buckets
                ],
            }

        return {
            "ok": True,
            "start": first,
            "end": end,
            "step": step,
            "jobs": jobs_payload,
        }


class JobSupervisor:
    """Own at most one runner child per jobs/<name> (job.json declares it)."""

    def __init__(self, metrics: MetricsStore | None = None) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._log_paths: dict[str, Path] = {}
        self._flow_readers: dict[str, FlowLogReader] = {}
        self._scan_cache: tuple[float, dict[str, int]] = (0.0, {})
        self._lock = threading.Lock()
        self.metrics = metrics or MetricsStore(LOG_DIR / "metrics.db")

    def _external_pids(self) -> dict[str, int]:
        """Adopt runner processes this console did not spawn (cached ~2s).

        A console restart must not orphan a running job: the UI still shows
        it as running and the stop button still works (targeted SIGTERM).
        """

        now = time.monotonic()
        cached_at, cached = self._scan_cache
        if now - cached_at < 2.0:
            return cached
        found: dict[str, int] = {}
        own = {p.pid for p in self._processes.values() if p.poll() is None}
        for name, spec in self.registry().items():
            script = spec["runner"][0]
            try:
                result = subprocess.run(
                    ["pgrep", "-f", script],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            pids = [int(p) for p in result.stdout.split() if p.isdigit()]
            for pid in pids:
                if pid in own:
                    continue
                if self._is_runner_process(pid, script):
                    found[name] = pid
                    break
        self._scan_cache = (now, found)
        return found

    @staticmethod
    def _is_runner_process(pid: int, script: str) -> bool:
        """Verify a scanned pid really is our venv running this job script.

        `pgrep -f` matches any command line containing the path (an editor,
        a `tail -f`), and stop() sends SIGTERM to adopted pids — so confirm
        the full command line before ever treating a pid as a runner.
        """

        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        command = result.stdout.strip()
        if script not in command:
            return False
        # The interpreter check must survive macOS resolving the venv
        # symlink chain: ps reports the real binary
        # (…/Python.app/Contents/MacOS/Python), never the .venv/bin/python
        # argv the spawner passed. Accept any interpreter whose basename
        # contains "python" — an editor or pager holding the script path
        # (vim/tail/less) still fails this and is never signaled.
        interpreter = command.split()[0] if command.split() else ""
        return "python" in Path(interpreter).name.lower()

    @staticmethod
    def registry() -> dict[str, dict]:
        jobs: dict[str, dict] = {}
        for manifest in sorted(JOBS_ROOT.glob("*/job.json")):
            try:
                spec = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            name = str(spec.get("name") or manifest.parent.name)
            runner = spec.get("runner")
            if not isinstance(runner, list) or not runner:
                continue
            jobs[name] = {
                "name": name,
                "title": str(spec.get("title", name)),
                "description": str(spec.get("description", "")),
                "runner": [str(part) for part in runner],
                # Settings the job says an operator may change. Carried raw:
                # the schema is validated where it is used, so one job's
                # malformed block cannot hide every other job from the list.
                "config": spec.get("config"),
            }
        return jobs

    @staticmethod
    def config_schema(name: str) -> "ConfigSchema | None":
        spec = JobSupervisor.registry().get(name)
        if spec is None:
            return None
        try:
            return ConfigSchema.from_manifest(spec.get("config"))
        except ConfigurationError:
            return None

    def config(self, name: str) -> dict[str, Any]:
        """The declared settings, their stored values, and where they live."""

        schema = self.config_schema(name)
        if schema is None:
            if name not in self.registry():
                return {"ok": False, "error": "UnknownJob"}
            return {"ok": False, "error": "InvalidConfigSchema"}
        stored = read_values(values_path(name)) or {}
        return {
            "ok": True,
            "name": name,
            "schema": schema.as_dict(),
            "values": schema.resolve(stored),
            "stored": stored,
        }

    def set_config(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        """Store settings for a job, running or not.

        A running job adopts them at its next poll; nothing is pushed and
        nothing is restarted, so a change can never land in the middle of an
        action the job is already taking.
        """

        schema = self.config_schema(name)
        if schema is None:
            if name not in self.registry():
                return {"ok": False, "error": "UnknownJob"}
            return {"ok": False, "error": "InvalidConfigSchema"}
        try:
            validated = schema.validate(values)
        except ConfigurationError as error:
            return {"ok": False, "error": "InvalidValue", "detail": str(error)}
        path = values_path(name)
        stored = read_values(path) or {}
        stored.update(validated)
        try:
            write_values(path, stored)
        except OSError as error:
            return {"ok": False, "error": type(error).__name__}
        return {
            "ok": True,
            "name": name,
            "values": schema.resolve(stored),
            "stored": stored,
        }

    def start(self, name: str) -> dict[str, Any]:
        spec = self.registry().get(name)
        if spec is None:
            return {"ok": False, "error": "UnknownJob"}
        with self._lock:
            process = self._processes.get(name)
            if process is not None and process.poll() is None:
                return {"ok": False, "error": "AlreadyRunning"}
            if name in self._external_pids():
                return {"ok": False, "error": "AlreadyRunning"}
            LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            log_path = LOG_DIR / f"job-{name}.log"
            log = open(log_path, "a", encoding="utf-8")
            # Runner paths resolve against the jobs repository root, and the
            # child inherits the platform/jobs wiring explicitly so external
            # runners find the venv, the package, and the worker socket.
            jobs_repo_root = JOBS_ROOT.parent
            script = jobs_repo_root / spec["runner"][0]
            command = [str(VENV_PYTHON), str(script), *spec["runner"][1:]]
            child_env = dict(os.environ)
            child_env.setdefault("STREAMBOT_HOME", str(PROJECT_ROOT))
            child_env.setdefault("STREAMBOT_JOBS_DIR", str(JOBS_ROOT))
            self._processes[name] = subprocess.Popen(
                command,
                cwd=str(jobs_repo_root),
                env=child_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=False,
            )
            self._log_paths[name] = log_path
            return {"ok": True, "pid": self._processes[name].pid}

    def stop(self, name: str) -> dict[str, Any]:
        with self._lock:
            process = self._processes.get(name)
            if process is None or process.poll() is not None:
                self._processes.pop(name, None)
                external = self._external_pids().get(name)
                if external is None:
                    return {"ok": False, "error": "NotRunning"}
                # Adopted job from a previous console: targeted SIGTERM, then
                # bounded wait; escalate to SIGKILL only if it ignores it.
                try:
                    os.kill(external, signal.SIGTERM)
                    for _ in range(50):
                        time.sleep(0.2)
                        os.kill(external, 0)
                    os.kill(external, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    return {"ok": False, "error": "StopFailed"}
                self._scan_cache = (0.0, {})
                return {"ok": True}
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=4.0)
            self._processes.pop(name, None)
            return {"ok": True}

    def _flow_reader(self, name: str) -> FlowLogReader:
        reader = self._flow_readers.get(name)
        if reader is None:
            reader = FlowLogReader(
                JOBS_ROOT / name / "flow-log.jsonl", store=self.metrics, job=name
            )
            self._flow_readers[name] = reader
        return reader

    def history(
        self, range_seconds: int | None = None, end: int | None = None
    ) -> dict[str, Any]:
        """Every job's chart series over an arbitrary window, zero-filled.

        `end` omitted means "up to now" — the live, sliding view. A concrete
        `end` is a window the operator has panned to, which stays put.
        """

        span = min(max(int(range_seconds or 3600), 60), MetricsStore.RETENTION_SECONDS)
        upto = int(end) if end else int(time.time())
        with self._lock:
            # Ingest anything any log has gained before answering from the db.
            # Incremental after the first read, so this is cheap per tick.
            for name in self.registry():
                self._flow_reader(name).poll()
        return self.metrics.history(upto - span, upto)

    def status(self) -> list[dict[str, Any]]:
        rows = []
        with self._lock:
            external = self._external_pids()
            for name, spec in self.registry().items():
                process = self._processes.get(name)
                owned = process is not None and process.poll() is None
                pid = process.pid if owned else external.get(name)
                running = pid is not None
                log_path = self._log_paths.get(name)
                last_log = ""
                if log_path is not None and log_path.exists():
                    lines = log_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    last_log = lines[-1][-160:] if lines else ""
                metrics = None
                events: list[dict[str, Any]] = []
                if running:
                    reader = self._flow_reader(name)
                    reader.poll()
                    metrics = reader.metrics()
                    events = reader.recent_events()
                configurable = False
                try:
                    configurable = ConfigSchema.from_manifest(spec.get("config")) is not None
                except ConfigurationError:
                    configurable = False
                rows.append(
                    {
                        "name": name,
                        "title": spec["title"],
                        "description": spec["description"],
                        "running": running,
                        "pid": pid,
                        "last_log": last_log,
                        "metrics": metrics,
                        "events": events,
                        # The panel asks for the schema and values only when a
                        # config panel is opened: they change when an operator
                        # edits them, not once a second like everything else.
                        "configurable": configurable,
                    }
                )
            # A job ending does not erase what happened: events persist in the
            # metrics store, thirty days like the charts. When nothing is
            # running the feed follows whichever job spoke last instead of
            # going blank. (Metrics stay None — a stopped job has no live
            # rates worth showing.)
            if not any(row["running"] for row in rows):
                freshest: dict[str, Any] | None = None
                freshest_t = 0
                for row in rows:
                    self._flow_reader(row["name"]).poll()
                    last_t = self.metrics.last_event_t(row["name"])
                    if last_t is not None and last_t > freshest_t:
                        freshest, freshest_t = row, last_t
                if freshest is not None:
                    freshest["events"] = self._flow_reader(
                        freshest["name"]
                    ).recent_events()
        return rows


def host_visible_via_bonjour(timeout: float = 3.0) -> bool | None:
    """Ask Apple's mDNS browser whether a Sunshine host is advertising.

    Uses the system `dns-sd` binary (Apple-signed, already granted local-network
    access) so we can tell "host is on the network" apart from "our worker's
    launch context is permission-blocked" — without needing or storing a host
    address. Returns None if dns-sd is unavailable.
    """

    try:
        proc = subprocess.run(
            ["/usr/bin/dns-sd", "-t", str(int(timeout)), "-B", "_nvstream._tcp"],
            capture_output=True,
            text=True,
            timeout=timeout + 2.0,
        )
        output = proc.stdout
    except subprocess.TimeoutExpired as error:
        output = error.stdout.decode() if isinstance(error.stdout, bytes) else (error.stdout or "")
    except (OSError, ValueError):
        return None
    return any(" Add " in line for line in output.splitlines())


def classify_situation(
    *,
    ipc_present: bool,
    state: str | None,
    last_error_code: str | None,
    bonjour: bool | None,
    owned: bool,
    socket_present: bool,
) -> str:
    """Explain the worker's condition from its typed self-report.

    The worker publishes an allowlisted ``last_error_code`` for classified
    connection failures, so this no longer guesses from reconnect counts and
    exception type names. ``bonjour`` (the Apple-signed system browser) only
    breaks the tie for host-visibility waits: if the system daemon sees the
    host advertising while the worker cannot, the launch context's Local
    Network grant is the prime suspect.
    """

    if ipc_present:
        if state in {"observing", "acting"}:
            return "connected"
        if state == "detached":
            return "detached"
        if state == "waiting":
            if last_error_code == "desktop_session_inactive":
                return "waiting_desktop_session"
            if last_error_code == "host_session_busy":
                return "host_busy"
            if last_error_code in {"no_host_visible", "host_unreachable"}:
                return "permission_blocked" if bonjour else "waiting_host"
            return "waiting_host"
        if state in {"recovering", "starting"}:
            return "connecting"
        if state == "failed":
            return "failed"
        if state == "stopped":
            return "stopped"
        return "unknown"
    if owned and not socket_present:
        return "starting"
    if not owned and not socket_present:
        return "stopped"
    return "unknown"


class SystemEvents:
    """The system's turning points, as an operator would narrate them.

    The worker log is a stream of health snapshots; what an operator needs is
    the moments — the stream dropped, the worker was adopted, a job started,
    IPC went quiet. Those are visible only as DIFFERENCES between snapshots,
    so this compares each one against the last and records what changed, with
    a timestamp the snapshots themselves do not carry.

    In-memory and bounded: the ring covers the recent past, which is what a
    Logs tab is for; forensics belong to the worker log and the operation
    journal on disk.
    """

    MAX_EVENTS = 300

    def __init__(self) -> None:
        self._ring: deque[dict[str, Any]] = deque(maxlen=self.MAX_EVENTS)
        self._lock = threading.Lock()
        self._prev: dict[str, Any] | None = None
        self._prev_running: dict[str, int | None] = {}

    def _emit(self, kind: str, text: str) -> None:
        self._ring.append({"t": int(time.time()), "kind": kind, "text": text})

    def observe(self, status: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
        with self._lock:
            self._observe_locked(status, jobs)

    def _observe_locked(self, status: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
        worker = status.get("worker") or {}
        connection = status.get("connection") or {}
        previous = self._prev
        if previous is not None:
            prev_worker = previous.get("worker") or {}
            prev_connection = previous.get("connection") or {}

            pid, prev_pid = worker.get("pid"), prev_worker.get("pid")
            if pid != prev_pid:
                if pid is None:
                    self._emit("worker", f"worker exited (was pid {prev_pid})")
                elif prev_pid is None:
                    owned = worker.get("owned_by_console")
                    self._emit(
                        "worker",
                        f"worker {'started' if owned else 'adopted'} · pid {pid}",
                    )
                else:
                    self._emit("worker", f"worker replaced · pid {prev_pid} → {pid}")

            socket_now = bool(worker.get("socket_present"))
            if socket_now != bool(prev_worker.get("socket_present")):
                self._emit(
                    "ipc", "IPC socket up" if socket_now else "IPC socket gone"
                )

            ipc_error, prev_ipc = connection.get("ipc_error"), prev_connection.get("ipc_error")
            if ipc_error != prev_ipc:
                if ipc_error:
                    self._emit("ipc", f"IPC not responding ({ipc_error})")
                elif prev_ipc:
                    self._emit("ipc", "IPC recovered")

            state, prev_state = connection.get("state"), prev_connection.get("state")
            if state != prev_state:
                self._emit("stream", f"stream {prev_state or '—'} → {state or '—'}")

            reconnects = connection.get("reconnects")
            prev_reconnects = prev_connection.get("reconnects")
            if (
                isinstance(reconnects, int)
                and isinstance(prev_reconnects, int)
                and reconnects > prev_reconnects
            ):
                self._emit("stream", f"stream reconnect #{reconnects}")

            error = connection.get("last_error_type")
            if error and error != prev_connection.get("last_error_type"):
                code = connection.get("last_error_code")
                self._emit(
                    "stream", f"worker error: {error}{f' ({code})' if code else ''}"
                )

        running = {
            job["name"]: job.get("pid") for job in jobs if job.get("running")
        }
        for name, pid in running.items():
            if name not in self._prev_running:
                self._emit("job", f"job {name} started · pid {pid}")
        for name in self._prev_running:
            if name not in running:
                self._emit("job", f"job {name} stopped")
        self._prev_running = running
        self._prev = {"worker": dict(worker), "connection": dict(connection)}

    def tail(self, limit: int = 120) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._ring)[-limit:]


class ConsoleState:
    def __init__(self, supervisor: WorkerSupervisor, jobs: "JobSupervisor | None" = None) -> None:
        self.jobs = jobs or JobSupervisor()
        self.supervisor = supervisor
        self.events = SystemEvents()
        self._bonjour: bool | None = None
        self._bonjour_checked_at = 0.0
        self._bonjour_probing = False
        self._bonjour_lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_at = 0.0
        self._snapshot_lock = threading.Lock()

    def start_watching(self) -> None:
        """Track turning points once a second, browser open or not.

        A dedicated thread rather than piggybacking on the SSE tick, because
        the moments worth recording do not wait for someone to be watching.
        """

        def watch() -> None:
            while True:
                try:
                    status = self.status()
                    jobs = self.jobs.status()
                    with self._snapshot_lock:
                        self._snapshot = {"status": status, "jobs": jobs}
                        self._snapshot_at = time.monotonic()
                    self.events.observe(status, jobs)
                except Exception:
                    pass  # observation must never take the console down
                time.sleep(1.0)

        threading.Thread(target=watch, name="system-events", daemon=True).start()

    def snapshot(self, max_age: float = 3.0) -> dict[str, Any] | None:
        """The watcher's latest status+jobs, if fresh enough to serve.

        This is what makes a page load instant: the first SSE frame is the
        snapshot the watcher took within the last second, already computed —
        no IPC round trip, no disk, nothing for the new connection to wait on.
        """

        with self._snapshot_lock:
            if self._snapshot is None:
                return None
            if time.monotonic() - self._snapshot_at > max_age:
                return None  # the watcher is wedged; compute inline instead
            return self._snapshot

    def bonjour(self) -> bool | None:
        """The last known answer, refreshed in the background.

        The probe is a three-second `dns-sd` run. It used to happen inline,
        under the lock, on whichever status() call found the cache stale — so
        one page load in a few would sit behind it, which is exactly the
        "sometimes the console takes seconds to appear" complaint. Status now
        always answers with what it has; a stale cache only *starts* a
        refresh, single-flight, off this thread.
        """

        with self._bonjour_lock:
            now = time.monotonic()
            stale = now - self._bonjour_checked_at > 15.0
            if stale and not self._bonjour_probing:
                self._bonjour_probing = True
                threading.Thread(
                    target=self._probe_bonjour, name="bonjour-probe", daemon=True
                ).start()
            return self._bonjour

    def _probe_bonjour(self) -> None:
        result = host_visible_via_bonjour()
        with self._bonjour_lock:
            self._bonjour = result
            self._bonjour_checked_at = time.monotonic()
            self._bonjour_probing = False

    def status(self) -> dict[str, Any]:
        supervisor = self.supervisor
        owned = supervisor.owned_pid()
        socket_present = supervisor.socket_path.exists()
        adopted = None if owned is not None else supervisor.external_pid()
        ipc: dict[str, Any] | None = None
        ipc_error: str | None = None
        if socket_present:
            try:
                ipc = send_control_command(supervisor.socket_path, "status")
            except Exception as error:  # metadata-only surface
                ipc_error = type(error).__name__

        health = (ipc or {}).get("health", {}) if ipc else {}
        page = (ipc or {}).get("page_state", {}) if ipc else {}
        state = health.get("state")
        reconnects = health.get("reconnects")
        last_error = health.get("last_error_type")
        last_error_code = health.get("last_error_code")

        # Classify the situation so the UI can explain it plainly.
        bonjour = self.bonjour()
        situation = classify_situation(
            ipc_present=ipc is not None,
            state=state,
            last_error_code=last_error_code,
            bonjour=bonjour,
            owned=owned is not None,
            socket_present=socket_present,
        )

        controls = page.get("controls", []) or []
        return {
            "ok": True,
            "situation": situation,
            "worker": {
                "owned_by_console": owned is not None,
                # Adopted pid included: the stop button works on any worker
                # this console can verify, not only its own children.
                "pid": owned if owned is not None else adopted,
                "socket_present": socket_present,
            },
            "host_advertising_bonjour": bonjour,
            "connection": {
                "state": state,
                "automation_enabled": (ipc or {}).get("automation_enabled"),
                "frame_number": (ipc or {}).get("frame_number"),
                "frame_age_ms": (ipc or {}).get("frame_age_ms"),
                "reconnects": reconnects,
                "last_error_type": last_error,
                "last_error_code": last_error_code,
                "ipc_error": ipc_error,
            },
            "scene": {
                "primary_layout": page.get("primary_layout"),
                "recommended_control_id": page.get("recommended_control_id"),
                "actionable": page.get("actionable"),
                "controls": [
                    {
                        "control_id": c.get("control_id"),
                        "action_kind": c.get("action_kind"),
                        "confidence": c.get("confidence"),
                        "label": c.get("label"),
                        "x": c.get("x"),
                        "y": c.get("y"),
                    }
                    for c in controls
                ],
            },
            "log_tail": supervisor.recent_log(),
        }


# Hostnames a request may address this console as, and page origins allowed
# to script it. The console binds 127.0.0.1 only, but "local" is not enough:
# any web page the operator visits can fire cross-origin POSTs at
# http://127.0.0.1:8787 (start/stop the worker, dispatch clicks), and DNS
# rebinding lets a remote page read /api/snapshot — live desktop frames.
# A strict Host allowlist defeats rebinding (the rebound request carries the
# attacker's hostname); an Origin check defeats cross-site requests (browsers
# always attach Origin to cross-origin requests). Any localhost port is
# allowed so the UI dev server's proxy keeps working.
_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _is_local_host_header(value: str | None) -> bool:
    if not value:
        return False  # HTTP/1.1 requires Host; absent means not a browser we trust
    host = value.strip()
    if host.startswith("["):  # bracketed IPv6, optionally with a port
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.rsplit(":", 1)[0] if ":" in host else host
    return host.lower() in _LOCAL_HOSTNAMES


def _is_local_origin(value: str | None) -> bool:
    if value is None:
        return True  # same-origin GETs and non-browser clients send no Origin
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme != "http" or parsed.hostname is None:
        return False  # includes "null" and https origins: never ours
    return parsed.hostname.lower() in {"127.0.0.1", "localhost", "::1"}


class Handler(BaseHTTPRequestHandler):
    console: ConsoleState = None  # set on the server instance
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # silence default stderr logging
        return

    def _reject_cross_origin(self) -> bool:
        """Refuse requests not addressed to, and initiated from, this machine."""

        if not _is_local_host_header(self.headers.get("Host")):
            self._send_json({"ok": False, "error": "ForbiddenHost"}, code=403)
            return True
        if not _is_local_origin(self.headers.get("Origin")):
            self._send_json({"ok": False, "error": "ForbiddenOrigin"}, code=403)
            return True
        return False

    def _send_json(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            # A missing or unreadable asset is a 404, not a traceback out of
            # the handler and a dead connection.
            self._send_json({"ok": False, "error": "NotFound"}, code=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # The page and its assets are served from disk on every request, which
        # is what makes editing the console and reloading enough. Hashed
        # bundle assets may be cached; the entry document may not.
        self.send_header(
            "Cache-Control",
            "public, max-age=31536000, immutable"
            if "/assets/" in path.as_posix()
            else "no-store",
        )
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, route: str) -> bool:
        """Serve `route` from STATIC_DIR. False if it is not a static asset.

        The console is a local operator tool, but a path is still untrusted
        input: the resolved file must sit inside STATIC_DIR, so `..` cannot
        walk out into the checkout, and only known asset types are served.
        """

        relative = route.lstrip("/")
        if not relative or relative == "index.html":
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return True
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return False  # escaped the static directory
        content_type = STATIC_TYPES.get(candidate.suffix.lower())
        if content_type is None or not candidate.is_file():
            return False
        self._send_file(candidate, content_type)
        return True

    def _read_json(self) -> dict[str, Any]:
        # A cross-site form can smuggle a JSON-shaped body as text/plain
        # without triggering a CORS preflight; honest clients say what they
        # send. Wrong or missing Content-Type reads as an empty body.
        content_type = (self.headers.get("Content-Type") or "").split(";")[0]
        if content_type.strip().lower() != "application/json":
            return {}
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 65_536:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------------ routes

    def do_GET(self) -> None:
        if self._reject_cross_origin():
            return
        route = self.path.split("?", 1)[0]  # ignore query string when routing
        if route == "/" or not route.startswith("/api/"):
            # The page and every built asset. Anything that is not a real file
            # under static/ falls through to the 404 at the end.
            if self._send_static(route):
                return
        if route == "/api/status":
            self._send_json(self.console.status())
            return
        if route == "/api/syslog":
            self._send_json({"ok": True, "events": self.console.events.tail()})
            return
        if route == "/api/snapshot":
            self._snapshot()
            return
        if route == "/api/jobs":
            self._send_json({"ok": True, "jobs": self.console.jobs.status()})
            return
        if route == "/api/events":
            self._events()
            return
        if route == "/api/jobs/history":
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)

            def integer(key: str) -> int | None:
                raw = (query.get(key) or [""])[0]
                try:
                    return int(raw)
                except ValueError:
                    return None

            self._send_json(
                self.console.jobs.history(integer("range"), integer("end"))
            )
            return
        if route == "/api/jobs/config":
            from urllib.parse import parse_qs, urlparse

            name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
            self._send_json(self.console.jobs.config(name))
            return
        self._send_json({"ok": False, "error": "NotFound"}, code=404)

    def do_POST(self) -> None:
        if self._reject_cross_origin():
            return
        supervisor = self.console.supervisor
        # Routed the same way as GET. These used to match `self.path` whole,
        # so a trailing query string would have fallen through to a 404.
        route = self.path.split("?", 1)[0]
        if route == "/api/worker/start":
            self._send_json(supervisor.start())
            return
        if route == "/api/worker/stop":
            self._send_json(supervisor.stop())
            return
        if route == "/api/worker/disconnect":
            # Detach the stream but keep the worker process and IPC alive.
            # Refused while a job is running: jobs drive input through this
            # worker's connection and would fail mid-flight without it.
            if any(job.get("running") for job in self.console.jobs.status()):
                self._send_json({"ok": False, "error": "JobRunning"}, code=409)
                return
            self._ipc("disconnect", {})
            return
        if route == "/api/worker/connect":
            self._ipc("connect", {})
            return
        if route == "/api/jobs/start":
            name = str(self._read_json().get("name", ""))
            self._send_json(self.console.jobs.start(name))
            return
        if route == "/api/jobs/stop":
            name = str(self._read_json().get("name", ""))
            self._send_json(self.console.jobs.stop(name))
            return
        if route == "/api/jobs/config":
            body = self._read_json()
            name = str(body.get("name", ""))
            values = body.get("values")
            if not isinstance(values, dict):
                self._send_json({"ok": False, "error": "MissingValues"}, code=400)
                return
            self._send_json(self.console.jobs.set_config(name, values))
            return
        if route == "/api/automation":
            enabled = bool(self._read_json().get("enabled"))
            self._ipc("set-automation", {"enabled": enabled})
            return
        if route == "/api/dispatch":
            control_id = str(self._read_json().get("control_id", ""))
            if not control_id:
                self._send_json({"ok": False, "error": "MissingControlId"}, code=400)
                return
            self._ipc("dispatch", {"control_id": control_id})
            return
        self._send_json({"ok": False, "error": "NotFound"}, code=404)

    # ------------------------------------------------------------------ helpers

    def _ipc(self, command: str, arguments: dict[str, Any]) -> None:
        socket_path = self.console.supervisor.socket_path
        if not socket_path.exists():
            self._send_json({"ok": False, "error": "WorkerNotConnected"}, code=409)
            return
        try:
            result = send_control_command(socket_path, command, arguments=arguments)
        except Exception as error:
            self._send_json({"ok": False, "error": type(error).__name__}, code=502)
            return
        self._send_json(result)

    def _events(self) -> None:
        """Stream combined status + jobs as Server-Sent Events (~1s cadence).

        Replaces client polling: the browser opens one EventSource and the
        server pushes a fresh snapshot every second until the client
        disconnects (a failed write). Each connection runs in its own
        ThreadingHTTPServer thread.
        """

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            first = True
            while True:
                # The first frame is the watcher's ready-made snapshot, so a
                # page load paints without waiting on IPC or disk; after that,
                # each tick computes fresh state as before.
                payload = self.console.snapshot() if first else None
                if payload is None:
                    payload = {
                        "status": self.console.status(),
                        "jobs": self.console.jobs.status(),
                    }
                first = False
                message = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                self.wfile.write(message.encode("utf-8"))
                self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            return  # client disconnected; end the stream thread

    def _snapshot(self) -> None:
        socket_path = self.console.supervisor.socket_path
        if not socket_path.exists():
            self._send_json({"ok": False, "error": "WorkerNotConnected"}, code=409)
            return
        # JPEG: ~10x faster encode/decode than PNG (the worker infers the
        # format from the .jpg extension), so the live monitor stays cheap.
        output = LOG_DIR / "snapshot.jpg"
        LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            result = send_control_command(
                socket_path, "snapshot", arguments={"output": str(output)}
            )
        except Exception as error:
            self._send_json({"ok": False, "error": type(error).__name__}, code=502)
            return
        if not result.get("ok") or not output.exists():
            self._send_json(
                {"ok": False, "error": result.get("error", "SnapshotFailed")}, code=502
            )
            return
        data = output.read_bytes()
        output.unlink(missing_ok=True)  # never retain frames on disk
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=PROJECT_ROOT / ".state" / "poc")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--control-socket",
        type=Path,
        default=None,
        help="worker IPC socket; defaults to <state-dir>/core-control.sock",
    )
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=None,
        help="directory holding <name>/job.json entries "
        "(default: $STREAMBOT_JOBS_DIR, else <repo>/jobs)",
    )
    args = parser.parse_args()

    if args.jobs_dir is not None:
        global JOBS_ROOT
        JOBS_ROOT = args.jobs_dir.expanduser().resolve()

    socket_path = args.control_socket or (
        args.state_dir / "core-control.sock"
    )
    supervisor = WorkerSupervisor(args.state_dir, socket_path)
    console = ConsoleState(supervisor)
    console.start_watching()

    handler = type("BoundHandler", (Handler,), {"console": console})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)

    def _shutdown(*_args) -> None:
        # The console is only the operator surface: closing it must never
        # take down the worker or a running job. A restarted console
        # re-adopts both (worker via its IPC socket, jobs via process scan),
        # and stopping them stays an explicit UI/API action.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    url = f"http://127.0.0.1:{args.port}/"
    print(f"streambot control console on {url}")
    print("Launched from a Local-Network-permitted terminal, the worker you")
    print("start here inherits that grant. Open the URL in your browser.")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
