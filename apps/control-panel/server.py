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

from streambot.control_plane import send_control_command  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOG_DIR = PROJECT_ROOT / ".state" / "control-panel"
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
            if process is None or process.poll() is not None:
                self._process = None
                return {"ok": False, "error": "NotRunning"}
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=8.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=4.0)
            self._process = None
            return {"ok": True}

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


class JobSupervisor:
    """Own at most one runner child per jobs/<name> (job.json declares it)."""

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._log_paths: dict[str, Path] = {}
        self._lock = threading.Lock()

    @staticmethod
    def registry() -> dict[str, dict]:
        jobs: dict[str, dict] = {}
        for manifest in sorted((PROJECT_ROOT / "jobs").glob("*/job.json")):
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
            }
        return jobs

    def start(self, name: str) -> dict[str, Any]:
        spec = self.registry().get(name)
        if spec is None:
            return {"ok": False, "error": "UnknownJob"}
        with self._lock:
            process = self._processes.get(name)
            if process is not None and process.poll() is None:
                return {"ok": False, "error": "AlreadyRunning"}
            LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            log_path = LOG_DIR / f"job-{name}.log"
            log = open(log_path, "a", encoding="utf-8")
            script = PROJECT_ROOT / spec["runner"][0]
            command = [str(VENV_PYTHON), str(script), *spec["runner"][1:]]
            self._processes[name] = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
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
                return {"ok": False, "error": "NotRunning"}
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=4.0)
            self._processes.pop(name, None)
            return {"ok": True}

    def stop_all(self) -> None:
        for name in list(self._processes):
            self.stop(name)

    @staticmethod
    def _flow_metrics(name: str, window: float = 60.0) -> dict[str, Any] | None:
        """Real operating metrics for a running job, from its flow-log.jsonl.

        Returns clicks-per-minute (efficiency), the recent mean match score
        (recognition confidence), the two latencies that actually matter for a
        sub-second click loop — capture-to-detect (perceive_ms) and
        detect-to-click (act_ms) — the last action, and the recent error count.
        """

        path = PROJECT_ROOT / "jobs" / name / "flow-log.jsonl"
        if not path.is_file():
            return None
        try:
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        except OSError:
            return None
        events = []
        for line in tail:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not events:
            return None
        # Age against the WALL CLOCK, not the newest event's own timestamp —
        # otherwise "last action" is always 0s ago and never advances.
        now = int(time.time())
        recent = [e for e in events if now - e.get("t", 0) <= window]
        clicks = [e for e in recent if e.get("event") == "click"]
        errors = [
            e for e in recent
            if e.get("event") in ("poll-error", "frame-skip", "click-skip", "classify-skip")
        ]
        scores = [e["score"] for e in clicks if isinstance(e.get("score"), (int, float))]
        # capture-to-detect samples come from every poll (click + throttled
        # perceive events); detect-to-click samples come only from real clicks.
        perceive = [
            e["perceive_ms"] for e in recent
            if isinstance(e.get("perceive_ms"), (int, float))
        ]
        act = [e["act_ms"] for e in clicks if isinstance(e.get("act_ms"), (int, float))]
        last = clicks[-1] if clicks else None
        return {
            "clicks_per_min": round(len(clicks) * 60.0 / window, 1),
            "mean_score": round(sum(scores) / len(scores), 3) if scores else None,
            "last_score": round(float(last["score"]), 3) if last and isinstance(last.get("score"), (int, float)) else None,
            "last_action": (last.get("element") if last else None),
            "last_action_age_s": (now - last["t"]) if last and "t" in last else None,
            "perceive_ms": round(sum(perceive) / len(perceive)) if perceive else None,
            "act_ms": round(sum(act) / len(act)) if act else None,
            "errors_recent": len(errors),
        }

    def status(self) -> list[dict[str, Any]]:
        rows = []
        with self._lock:
            for name, spec in self.registry().items():
                process = self._processes.get(name)
                running = process is not None and process.poll() is None
                log_path = self._log_paths.get(name)
                last_log = ""
                if log_path is not None and log_path.exists():
                    lines = log_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    last_log = lines[-1][-160:] if lines else ""
                rows.append(
                    {
                        "name": name,
                        "title": spec["title"],
                        "description": spec["description"],
                        "running": running,
                        "pid": process.pid if running else None,
                        "last_log": last_log,
                        "metrics": self._flow_metrics(name) if running else None,
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

        # Classify the situation so the UI can explain it plainly.
        bonjour = self.bonjour()
        if ipc and state == "observing":
            situation = "connected"
        elif ipc and state in {"recovering", "starting"}:
            situation = (
                "permission_blocked"
                if bonjour and (reconnects or 0) >= 2 and last_error == "RuntimeError"
                else "connecting"
            )
        elif owned and not socket_present:
            situation = "starting"
        elif not owned and not socket_present:
            situation = "stopped"
        else:
            situation = "unknown"

        controls = page.get("controls", []) or []
        return {
            "ok": True,
            "situation": situation,
            "worker": {
                "owned_by_console": owned is not None,
                "pid": owned,
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
        self._send_json({"ok": False, "error": "NotFound"}, code=404)

    def do_POST(self) -> None:
        supervisor = self.console.supervisor
        if self.path == "/api/worker/start":
            self._send_json(supervisor.start())
            return
        if self.path == "/api/worker/stop":
            self._send_json(supervisor.stop())
            return
        if self.path == "/api/jobs/start":
            name = str(self._read_json().get("name", ""))
            self._send_json(self.console.jobs.start(name))
            return
        if self.path == "/api/jobs/stop":
            name = str(self._read_json().get("name", ""))
            self._send_json(self.console.jobs.stop(name))
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
    args = parser.parse_args()

    socket_path = args.control_socket or (
        args.state_dir / "core-control.sock"
    )
    supervisor = WorkerSupervisor(args.state_dir, socket_path)
    console = ConsoleState(supervisor)

    handler = type("BoundHandler", (Handler,), {"console": console})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)

    def _shutdown(*_args) -> None:
        console.jobs.stop_all()
        supervisor.stop()
        server.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    url = f"http://127.0.0.1:{args.port}/"
    print(f"streambot control console on {url}")
    print("Launched from a Local-Network-permitted terminal, the worker you")
    print("start here inherits that grant. Open the URL in your browser.")
    try:
        server.serve_forever()
    finally:
        supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
