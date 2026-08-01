"""Local IPC control plane for a persistent automation worker.

Target-agnostic platform component: exposes the latest observation, health, and
page state over a private Unix domain socket, and serializes external action
requests onto the worker's single input-owning frame loop. Target-specific
defaults (such as the socket file name) are supplied by the target adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
import json
from pathlib import Path
from queue import Empty, Queue
import socket
from threading import Event, Lock, Thread
import time
from typing import Any
from uuid import uuid4

import numpy as np
from PIL import Image

from streambot.input import SafeInputDriver
from streambot.observation import Observation


DEFAULT_SOCKET_PATH = Path(".state/poc/control.sock")
MAX_RESPONSE_BYTES = 1_048_576


@dataclass
class PendingCommand:
    """One validated action request waiting for the input-owner thread."""

    request_id: str
    command: str
    arguments: dict[str, Any]
    completed: Event = field(default_factory=Event)
    response: dict[str, Any] | None = None


class PersistentControlPlane:
    """Expose latest state and serialize commands onto the worker thread."""

    ACTION_COMMANDS = {
        "point",
        "move-rel",
        "click",
        "press",
        "hold-click",
        "drag",
        "trace",
        "double-click",
        "type",
        "scroll",
        "escape",
        "backspace",
        "enter",
        "fast-forward",
        "set-automation",
        "dispatch",
    }

    def __init__(
        self,
        socket_path: Path,
        *,
        clock=time.monotonic,
        allow_frame_export: bool = False,
    ) -> None:
        self.socket_path = socket_path
        self.clock = clock
        self.allow_frame_export = allow_frame_export
        self._automation_enabled = False
        self._commands: Queue[PendingCommand] = Queue(maxsize=32)
        self._stop = Event()
        self._lock = Lock()
        self._server: socket.socket | None = None
        self._thread: Thread | None = None
        self._executor: Thread | None = None
        self._latest_frame: np.ndarray | None = None
        self._latest_frame_number: int | None = None
        self._latest_observed_at: float | None = None
        self._health: dict[str, Any] = {"state": "starting"}
        self._page_state: dict[str, Any] = {
            "primary_layout": "unobserved",
            "matches": [],
            "actionable": False,
        }
        self._recent_page_states: deque[dict[str, Any]] = deque(maxlen=32)
        self._last_event: dict[str, Any] | None = None
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=32)
        self._commands_completed = 0
        self._on_detach: Any = None
        self._on_attach: Any = None

    def set_connection_controls(self, detach, attach) -> None:
        """Wire operator stream detach/attach to the owning worker.

        The control plane never owns the connection; it only forwards the
        request to the worker's runtime, which tears down or re-establishes
        the stream through its normal recovery paths.
        """

        self._on_detach = detach
        self._on_attach = attach

    @property
    def automation_enabled(self) -> bool:
        with self._lock:
            return self._automation_enabled

    def pause_automation(self) -> None:
        """Fail-safe internal pause (e.g. a stagnation watchdog fired)."""

        with self._lock:
            self._automation_enabled = False

    def start_executor(self, inputs: SafeInputDriver) -> None:
        """Drain queued commands on a dedicated thread off the frame loop.

        `SafeInputDriver` serializes protocol calls internally, so commands may
        sleep (drag interpolation, hold-click) without freezing perception.
        Semantic interleaving with the automation runtime is prevented by
        rejecting action commands while automation is enabled.
        """

        def _drain() -> None:
            while not self._stop.is_set():
                try:
                    pending = self._commands.get(timeout=0.2)
                except Empty:
                    continue
                self._run_command(inputs, pending)

        self._executor = Thread(
            target=_drain, name="control-executor", daemon=True
        )
        self._executor.start()

    def start(self) -> None:
        """Start the local socket without creating another Moonlight client."""

        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        server.listen(8)
        server.settimeout(0.2)
        self._server = server
        self._thread = Thread(target=self._serve, name="persistent-control", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop IPC and remove only this service's socket."""

        self._stop.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._executor is not None:
            self._executor.join(timeout=2.0)
        if self.socket_path.exists():
            self.socket_path.unlink()

    def publish_observation(self, observation: Observation) -> None:
        """Replace the latest frame; no frame queue is retained."""

        with self._lock:
            self._latest_frame = observation.data
            self._latest_frame_number = observation.frame_number
            self._latest_observed_at = observation.observed_at

    def publish_health(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._health = dict(payload)

    def publish_page_state(
        self, frame_number: int, payload: dict[str, Any]
    ) -> None:
        """Publish metadata-only full-layout matches for the latest frame."""

        sanitized = dict(payload)
        sanitized["frame_number"] = int(frame_number)
        with self._lock:
            previous_identity = (
                self._page_state.get("primary_layout"),
                tuple(self._page_state.get("matches", ())),
                bool(self._page_state.get("actionable", False)),
                bool(self._page_state.get("unknown_interactive", False)),
            )
            current_identity = (
                sanitized.get("primary_layout"),
                tuple(sanitized.get("matches", ())),
                bool(sanitized.get("actionable", False)),
                bool(sanitized.get("unknown_interactive", False)),
            )
            self._page_state = sanitized
            if current_identity != previous_identity:
                self._recent_page_states.append(dict(sanitized))

    def publish_event(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._last_event = dict(payload)
            self._recent_events.append(dict(payload))

    def status(self) -> dict[str, Any]:
        with self._lock:
            age_ms = None
            if self._latest_observed_at is not None:
                age_ms = max(0, round((self.clock() - self._latest_observed_at) * 1000))
            return {
                "ok": True,
                "automation_enabled": self._automation_enabled,
                "commands_completed": self._commands_completed,
                "frame_number": self._latest_frame_number,
                "frame_age_ms": age_ms,
                "health": dict(self._health),
                "page_state": dict(self._page_state),
                "recent_page_states": [
                    dict(page_state) for page_state in self._recent_page_states
                ],
                "last_event": dict(self._last_event) if self._last_event else None,
                "recent_events": [dict(event) for event in self._recent_events],
            }

    def execute_pending(self, inputs: SafeInputDriver) -> None:
        """Drain queued input from the frame loop (legacy serialization path).

        Prefer `start_executor`, which keeps long-running commands off the
        frame loop. Both paths pop from the same queue, so each command is
        executed exactly once even if both are active during a migration.
        """

        for _ in range(4):
            try:
                pending = self._commands.get_nowait()
            except Empty:
                return
            self._run_command(inputs, pending)

    def _run_command(self, inputs: SafeInputDriver, pending: PendingCommand) -> None:
        try:
            pending.response = self._execute(inputs, pending)
            with self._lock:
                self._commands_completed += 1
        except Exception as error:
            pending.response = {"ok": False, "error": type(error).__name__}
        finally:
            pending.completed.set()

    def _execute(
        self, inputs: SafeInputDriver, pending: PendingCommand
    ) -> dict[str, Any]:
        command = pending.command
        args = pending.arguments
        key = f"ipc-{pending.request_id}"
        if command == "set-automation":
            with self._lock:
                self._automation_enabled = bool(args["enabled"])
            return {"ok": True, "command": command}
        if self.automation_enabled:
            # While the automation runtime owns input, external action commands
            # would interleave with its dispatches; fail closed instead.
            return {"ok": False, "error": "AutomationActive", "command": command}
        if command == "move-rel":
            inputs.execute_move(
                int(args["dx"]), int(args["dy"]), f"{key}-move-rel"
            )
            return {"ok": True, "command": command}
        if command == "type":
            inputs.execute_text(str(args["text"]), f"{key}-type")
            return {"ok": True, "command": command}
        if command == "double-click":
            # Two clicks close enough together that the target reads them as
            # one gesture. Sending two separate click commands cannot do it:
            # each glides to the point first, and the gap between them is
            # whatever the socket and the worker thread happen to cost.
            x, y = int(args["x"]), int(args["y"])
            gap = min(0.3, max(0.02, float(args.get("gap_seconds", 0.09))))
            inputs.execute_glide(x, y, f"{key}-point")
            inputs.execute("click", f"{key}-click-1")
            time.sleep(gap)
            inputs.execute("click", f"{key}-click-2")
            return {"ok": True, "command": command}
        if command == "trace":
            # Press once, move through every waypoint, release once. A drag is
            # a straight line by design; some tools want a PATH — Poly Bridge's
            # freehand tool lays a chain of joints along whatever curve the
            # pointer follows, and a curved deck cannot be produced by clicking
            # its joints one at a time. Chaining short drags will not do: each
            # one releases the button and ends the stroke.
            points = [(int(px), int(py)) for px, py in args["points"]]
            if len(points) < 2:
                return {"ok": False, "error": "TooFewPoints", "command": command}
            duration = min(4.0, max(0.1, float(args.get("duration_seconds", 1.0))))
            step = duration / max(1, len(points) - 1)
            inputs.execute_position(points[0][0], points[0][1], f"{key}-point")
            time.sleep(0.1)
            inputs.execute("mouse-down", f"{key}-down")
            try:
                for index, (px, py) in enumerate(points[1:], start=1):
                    if self._stop.is_set():
                        break
                    time.sleep(step)
                    inputs.execute_position(px, py, f"{key}-move-{index}")
            finally:
                inputs.execute("mouse-up", f"{key}-up")
            return {"ok": True, "command": command, "points": len(points)}
        if command in {"point", "click", "hold-click", "drag"}:
            x, y = int(args["x"]), int(args["y"])
            # Natural trajectory to the target before pressing; drag keeps its
            # own linear interpolation (a press-drag is a deliberate straight
            # motion), the rest glide like a hand.
            if command == "drag":
                inputs.execute_position(x, y, f"{key}-point")
            else:
                inputs.execute_glide(x, y, f"{key}-point")
            if command == "click":
                inputs.execute("click", f"{key}-click")
            elif command == "hold-click":
                inputs.execute("mouse-down", f"{key}-down")
                time.sleep(min(0.25, max(0.02, float(args.get("seconds", 0.12)))))
                inputs.execute("mouse-up", f"{key}-up")
            elif command == "drag":
                x2, y2 = int(args["x2"]), int(args["y2"])
                duration = min(
                    1.5, max(0.1, float(args.get("duration_seconds", 0.6)))
                )
                time.sleep(0.1)
                inputs.execute("mouse-down", f"{key}-down")
                try:
                    for index in range(1, 9):
                        if self._stop.is_set():
                            break
                        fraction = index / 8
                        time.sleep(duration / 8)
                        inputs.execute_position(
                            round(x + (x2 - x) * fraction),
                            round(y + (y2 - y) * fraction),
                            f"{key}-move-{index}",
                        )
                finally:
                    inputs.execute("mouse-up", f"{key}-up")
        elif command == "scroll":
            clicks = int(args["clicks"])
            inputs.execute_scroll(clicks, f"{key}-scroll")
            return {"ok": True, "command": "scroll", "clicks": clicks}
        elif command == "press":
            inputs.execute("click", f"{key}-click")
        elif command in {"escape", "backspace", "enter", "fast-forward"}:
            inputs.execute(command, f"{key}-{command}")
        elif command == "dispatch":
            control_id = str(args["control_id"])
            with self._lock:
                controls = list(self._page_state.get("controls", []) or [])
            match = next(
                (c for c in controls if c.get("control_id") == control_id), None
            )
            if match is None or match.get("x") is None:
                return {"ok": False, "error": "UnknownControl", "control_id": control_id}
            inputs.execute_glide(int(match["x"]), int(match["y"]), f"{key}-point")
            inputs.execute("click", f"{key}-click")
            return {"ok": True, "command": "dispatch", "control_id": control_id}
        else:
            raise ValueError("unsupported command")
        return {"ok": True, "command": command}

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept() if self._server else (None, None)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            if connection is None:
                continue
            # One short-lived thread per connection: a status query must not
            # queue behind another client's multi-second action command.
            Thread(
                target=self._handle_connection,
                args=(connection,),
                name="control-request",
                daemon=True,
            ).start()

    def _handle_connection(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(4.0)
            try:
                payload = self._read_request(connection)
                response = self._handle_request(payload)
            except Exception as error:
                response = {"ok": False, "error": type(error).__name__}
            try:
                connection.sendall(
                    (json.dumps(response, sort_keys=True) + "\n").encode()
                )
            except OSError:
                pass

    @staticmethod
    def _read_request(connection: socket.socket) -> dict[str, Any]:
        data = b""
        while b"\n" not in data and len(data) <= 16_384:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data += chunk
        if not data or len(data) > 16_384:
            raise ValueError("invalid request size")
        payload = json.loads(data.split(b"\n", 1)[0])
        if not isinstance(payload, dict):
            raise ValueError("request must be an object")
        return payload

    def controls(self) -> dict[str, Any]:
        """Return the current clickable control surface without coordinates.

        Coordinates are intentionally omitted from the external query; only the
        internal `dispatch` path resolves a named control to its point.
        """

        with self._lock:
            page = self._page_state
            entries = page.get("controls", []) or []
            return {
                "ok": True,
                "frame_number": page.get("frame_number"),
                "recommended_control_id": page.get("recommended_control_id"),
                "controls": [
                    {
                        "control_id": entry.get("control_id"),
                        "action_kind": entry.get("action_kind"),
                        "confidence": entry.get("confidence"),
                    }
                    for entry in entries
                ],
            }

    def report_scene(self, args: dict[str, Any]) -> dict[str, Any]:
        """Publish a running job's own detected controls as the page state.

        This is the hot-pluggable seam: the target-agnostic core worker holds no
        perception, so the running job reports what IT sees each poll and that
        becomes the console overlay. Coordinates stay on this local socket only
        (same surface `dispatch` already resolves); nothing is written to disk.
        """

        controls: list[dict[str, Any]] = []
        for entry in args.get("controls", []) or []:
            control_id = str(entry.get("control_id") or entry.get("id") or "")
            if not control_id:
                continue
            x = entry.get("x")
            y = entry.get("y")
            controls.append(
                {
                    "control_id": control_id,
                    "label": str(entry.get("label", control_id)),
                    "action_kind": str(entry.get("action_kind", "click")),
                    "confidence": entry.get("confidence"),
                    "x": int(x) if x is not None else None,
                    "y": int(y) if y is not None else None,
                }
            )
        recommended = args.get("recommended_control_id")
        payload = {
            "primary_layout": str(args.get("primary_layout", "job")),
            "matches": [control["control_id"] for control in controls],
            "controls": controls,
            "actionable": bool(args.get("actionable", bool(controls))),
            "recommended_control_id": str(recommended) if recommended else None,
            "source": str(args.get("source", "job")),
        }
        with self._lock:
            frame_number = self._latest_frame_number or 0
        self.publish_page_state(frame_number, payload)
        return {"ok": True, "command": "report-scene", "controls": len(controls)}

    def _handle_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = str(payload.get("command", ""))
        if command == "status":
            return self.status()
        if command == "controls":
            return self.controls()
        if command == "report-scene":
            return self.report_scene(dict(payload.get("arguments", {})))
        if command in {"connect", "disconnect"}:
            handler = self._on_attach if command == "connect" else self._on_detach
            if handler is None:
                return {"ok": False, "error": "ConnectionControlUnavailable"}
            handler()
            return {"ok": True, "command": command}
        if command == "snapshot":
            if not self.allow_frame_export:
                return {"ok": False, "error": "FrameExportDisabled"}
            output = Path(str(payload["output"])).expanduser().resolve()
            with self._lock:
                if self._latest_frame is None:
                    return {"ok": False, "error": "NoFrameAvailable"}
                frame = self._latest_frame.copy()
                frame_number = self._latest_frame_number
            output.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(frame[:, :, ::-1]).save(output)
            return {"ok": True, "frame_number": frame_number, "output": str(output)}
        if command not in self.ACTION_COMMANDS:
            return {"ok": False, "error": "UnsupportedCommand"}
        request_id = str(payload.get("id") or uuid4().hex)
        pending = PendingCommand(request_id, command, dict(payload.get("arguments", {})))
        self._commands.put(pending, timeout=0.5)
        if not pending.completed.wait(timeout=3.0):
            return {"ok": False, "error": "CommandTimeout"}
        return pending.response or {"ok": False, "error": "MissingResponse"}


def send_control_command(
    socket_path: Path, command: str, *, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Send one small JSON request to the already-connected worker."""

    payload = {
        "id": uuid4().hex,
        "command": command,
        "arguments": arguments or {},
    }
    if command == "snapshot" and arguments:
        payload["output"] = arguments["output"]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(4.0)
        client.connect(str(socket_path))
        client.sendall((json.dumps(payload, sort_keys=True) + "\n").encode())
        data = b""
        while b"\n" not in data and len(data) <= MAX_RESPONSE_BYTES:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
    if not data:
        raise RuntimeError("persistent control service returned no response")
    if b"\n" not in data or len(data) > MAX_RESPONSE_BYTES:
        raise RuntimeError("persistent control service returned an invalid response")
    return json.loads(data.split(b"\n", 1)[0])
