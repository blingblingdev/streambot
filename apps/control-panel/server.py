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
import subprocess
import sys
import threading
import time
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
                # A worker from another launch context already owns the socket.
                return {"ok": False, "error": "SocketOwnedElsewhere"}
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
        if self._log_path is None or not self._log_path.exists():
            return []
        text = self._log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return text[-lines:]


class FlowLogReader:
    """Incremental flow-log.jsonl reader: session totals plus a recent-event ring.

    Reads only bytes appended since the previous poll, so per-tick cost stays
    proportional to new activity. Session aggregates (uptime, total clicks,
    cycles, mean score) reset on each `start` event; the ring feeds the
    console's scrolling event stream with a monotonically increasing index
    the client uses for append-only dedupe.
    """

    RING_SIZE = 120
    # Whole-session series for the trend charts, capped so hours of dense
    # events stay bounded in memory and on the wire; the page re-fetches
    # them on load, which is what makes chart history survive a refresh.
    HISTORY_MAX_POINTS = 2000
    # "job-error" is what streambot.job_events.problem() writes, and it is how
    # every job built on the shared runtime reports trouble; without it the
    # panel's error count stays at zero while the feed fills with warnings.
    ERROR_EVENTS = {
        "poll-error", "frame-skip", "click-skip", "classify-skip", "job-error",
    }

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._remainder = ""
        self._line_no = 0
        self._ring: deque[dict[str, Any]] = deque(maxlen=self.RING_SIZE)
        self._session: dict[str, Any] | None = None
        self._perceive: deque[tuple[int, float]] = deque(maxlen=self.HISTORY_MAX_POINTS)
        self._resolve: deque[tuple[int, float]] = deque(maxlen=self.HISTORY_MAX_POINTS)
        self._score: deque[tuple[int, float]] = deque(maxlen=self.HISTORY_MAX_POINTS)
        self._click_ts: deque[int] = deque(maxlen=3 * self.HISTORY_MAX_POINTS)

    def _reset(self) -> None:
        self._offset = 0
        self._remainder = ""
        self._line_no = 0
        self._ring.clear()
        self._session = None
        self._perceive.clear()
        self._resolve.clear()
        self._score.clear()
        self._click_ts.clear()

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
            self._apply(event)

    def _apply(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        t = event.get("t")
        if isinstance(t, (int, float)):
            perceive = event.get("perceive_ms")
            if kind in ("perceive", "click") and isinstance(perceive, (int, float)):
                self._perceive.append((int(t), float(perceive)))
            resolve = event.get("resolve_ms")
            if kind in ("perceive", "click") and isinstance(resolve, (int, float)):
                self._resolve.append((int(t), float(resolve)))
            if kind == "click":
                self._click_ts.append(int(t))
                score = event.get("score")
                if isinstance(score, (int, float)):
                    self._score.append((int(t), float(score)))
        if kind == "start":
            self._session = {
                "started_t": event.get("t"),
                "clicks": 0,
                "cycles": 0,
                "score_sum": 0.0,
                "score_n": 0,
            }
            self._perceive.clear()
            self._resolve.clear()
            self._score.clear()
            self._click_ts.clear()
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


    def history(self) -> dict[str, Any]:
        """Whole-session chart series: perceive/score points, clicks-per-minute.

        Clicks/min is bucketed per minute with gaps zero-filled so the line
        drops to zero during long matches instead of interpolating across.
        """

        cpm: list[list[float]] = []
        if self._click_ts:
            counts: dict[int, int] = {}
            for ts in self._click_ts:
                counts[ts // 60] = counts.get(ts // 60, 0) + 1
            first, last = min(counts), max(counts)
            last = max(last, int(time.time()) // 60)
            first = max(first, last - self.HISTORY_MAX_POINTS + 1)
            cpm = [[m * 60 + 30, counts.get(m, 0)] for m in range(first, last + 1)]
        return {
            "perceive": [[t, v] for t, v in self._perceive],
            "resolve": [[t, v] for t, v in self._resolve],
            "score": [[t, v] for t, v in self._score],
            "cpm": cpm,
        }

    def recent_events(self, limit: int = 60) -> list[dict[str, Any]]:
        return list(self._ring)[-limit:]


class JobSupervisor:
    """Own at most one runner child per jobs/<name> (job.json declares it)."""

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._log_paths: dict[str, Path] = {}
        self._flow_readers: dict[str, FlowLogReader] = {}
        self._scan_cache: tuple[float, dict[str, int]] = (0.0, {})
        self._lock = threading.Lock()

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
            reader = FlowLogReader(JOBS_ROOT / name / "flow-log.jsonl")
            self._flow_readers[name] = reader
        return reader

    def history(self, name: str) -> dict[str, Any]:
        """Session chart series for one job (perceive, score, clicks/min)."""

        if name not in self.registry():
            return {"ok": False, "error": "UnknownJob"}
        with self._lock:
            reader = self._flow_reader(name)
            reader.poll()
            return {"ok": True, **reader.history()}

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


class ConsoleState:
    def __init__(self, supervisor: WorkerSupervisor, jobs: "JobSupervisor | None" = None) -> None:
        self.jobs = jobs or JobSupervisor()
        self.supervisor = supervisor
        self._bonjour: bool | None = None
        self._bonjour_checked_at = 0.0
        self._bonjour_lock = threading.Lock()

    def bonjour(self) -> bool | None:
        with self._bonjour_lock:
            now = time.monotonic()
            if now - self._bonjour_checked_at > 15.0:
                self._bonjour = host_visible_via_bonjour()
                self._bonjour_checked_at = now
            return self._bonjour

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


class Handler(BaseHTTPRequestHandler):
    console: ConsoleState = None  # set on the server instance
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # silence default stderr logging
        return

    def _send_json(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
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
        route = self.path.split("?", 1)[0]  # ignore query string when routing
        if route in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if route == "/api/status":
            self._send_json(self.console.status())
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

            name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
            self._send_json(self.console.jobs.history(name))
            return
        if route == "/api/jobs/config":
            from urllib.parse import parse_qs, urlparse

            name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
            self._send_json(self.console.jobs.config(name))
            return
        if route == "/vendor/echarts.min.js":
            self._send_file(
                STATIC_DIR / "vendor" / "echarts.min.js",
                "application/javascript; charset=utf-8",
            )
            return
        self._send_json({"ok": False, "error": "NotFound"}, code=404)

    def do_POST(self) -> None:
        supervisor = self.console.supervisor
        if self.path == "/api/worker/start":
            self._send_json(supervisor.start())
            return
        if self.path == "/api/worker/stop":
            self._send_json(supervisor.stop())
            return
        if self.path == "/api/worker/disconnect":
            # Detach the stream but keep the worker process and IPC alive.
            # Refused while a job is running: jobs drive input through this
            # worker's connection and would fail mid-flight without it.
            if any(job.get("running") for job in self.console.jobs.status()):
                self._send_json({"ok": False, "error": "JobRunning"}, code=409)
                return
            self._ipc("disconnect", {})
            return
        if self.path == "/api/worker/connect":
            self._ipc("connect", {})
            return
        if self.path == "/api/jobs/start":
            name = str(self._read_json().get("name", ""))
            self._send_json(self.console.jobs.start(name))
            return
        if self.path == "/api/jobs/stop":
            name = str(self._read_json().get("name", ""))
            self._send_json(self.console.jobs.stop(name))
            return
        if self.path == "/api/jobs/config":
            body = self._read_json()
            name = str(body.get("name", ""))
            values = body.get("values")
            if not isinstance(values, dict):
                self._send_json({"ok": False, "error": "MissingValues"}, code=400)
                return
            self._send_json(self.console.jobs.set_config(name, values))
            return
        if self.path == "/api/automation":
            enabled = bool(self._read_json().get("enabled"))
            self._ipc("set-automation", {"enabled": enabled})
            return
        if self.path == "/api/dispatch":
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
            while True:
                payload = {
                    "status": self.console.status(),
                    "jobs": self.console.jobs.status(),
                }
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
