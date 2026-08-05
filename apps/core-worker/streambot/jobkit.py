"""What every job runner used to write for itself.

Four runners in the jobs repository carried their own copy of the same code:
re-exec into the venv, sideload a resolver, wrap the control socket, pull a
frame through a temporary JPEG, click and time it, append a JSONL line, install
signal handlers, and loop with a try/except so one bad poll cannot kill the
run. The copies had already drifted — one of them looked for the checkout in a
different place than the others — which is what a copied bootstrap always does
eventually.

A job written against this module supplies only what is genuinely its own:
what its screens and controls look like (a declaration), what to do when it
sees them (a policy), and what an operator may tune (a config schema).

    from streambot.jobkit import JobLoop, bootstrap

    bootstrap()                       # venv + import path, before anything else

    def poll(ctx):
        if ctx.screen == "settlement":
            ctx.click("replay")
            ctx.cycle()
        return ctx.idle()

    JobLoop("my-job", declaration=Path("recordings/elements.json")).run(poll)

Observation, analysis and input all go through the worker, so the platform's
operation record is complete without the job doing anything to keep it so.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_STREAMBOT_HOME = Path.home() / "Codes" / "Private" / "lolita" / "streambot"


def streambot_home() -> Path:
    """The checkout that owns the venv, the package and the worker socket."""

    home = os.environ.get("STREAMBOT_HOME")
    return Path(home).expanduser() if home else DEFAULT_STREAMBOT_HOME


def bootstrap() -> None:
    """Re-exec into the project venv and make the package importable.

    Call this at the top of a runner, before importing anything from
    `streambot`. It replaces the block every runner used to copy — and, being
    one implementation, it cannot drift from itself.
    """

    home = streambot_home()
    venv_python = home / ".venv" / "bin" / "python"
    if sys.prefix == sys.base_prefix:
        if not venv_python.is_file():
            raise SystemExit(
                "streambot venv missing; run scripts/bootstrap.sh in $STREAMBOT_HOME"
            )
        os.execv(
            str(venv_python),
            [str(venv_python), str(Path(sys.argv[0]).resolve()), *sys.argv[1:]],
        )
    package_root = str(home / "apps" / "core-worker")
    if package_root not in sys.path:
        sys.path.insert(0, package_root)


def default_socket() -> Path:
    return streambot_home() / ".state" / "poc" / "core-control.sock"


@dataclass
class Found:
    """One located control the policy can act on."""

    element: str
    center: tuple[int, int]
    score: float
    region: str


class JobClient:
    """Everything a job does to the machine, done through the worker.

    Every call is attributed to the job, so the worker's operation record can
    say who asked. Failures are returned, never raised: a job's loop must
    survive a busy worker, and the next poll can simply try again.
    """

    def __init__(self, job_name: str, socket_path: Path | None = None) -> None:
        from streambot.control_plane import send_control_command

        self._send = send_control_command
        self.job_name = job_name
        self.socket_path = Path(socket_path) if socket_path else default_socket()
        self.last_error: str | None = None
        self.last_act_ms: float | None = None

    def _call(self, command: str, arguments: dict[str, Any] | None = None) -> dict:
        try:
            response = self._send(
                self.socket_path, command, arguments=arguments or {}, job=self.job_name
            )
        except Exception as error:  # IPC timeout, socket hiccup, worker busy
            self.last_error = type(error).__name__
            return {"ok": False, "error": self.last_error}
        if not response.get("ok", False):
            self.last_error = str(response.get("error"))
        return response

    def register(self, declaration: Path, assets_dir: Path | None = None) -> dict:
        """Hand the worker this job's element declaration."""

        arguments: dict[str, Any] = {"declaration_path": str(Path(declaration).resolve())}
        if assets_dir is not None:
            arguments["assets_dir"] = str(Path(assets_dir).resolve())
        return self._call("register-elements", arguments)

    def analyze(self, elements: Iterable[str] | None = None) -> dict:
        """Ask the worker what is on screen and where.

        The response carries the worker's own breakdown (classify_ms,
        resolve_ms); `perceive_ms` is added here because only this side can
        see the round trip, and the round trip is what the operator waits for.
        """

        arguments: dict[str, Any] = {}
        if elements is not None:
            arguments["elements"] = list(elements)
        started = time.perf_counter()
        response = self._call("analyze", arguments)
        response["perceive_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return response

    def click(self, x: int, y: int) -> bool:
        started = time.perf_counter()
        ok = bool(self._call("click", {"x": int(x), "y": int(y)}).get("ok"))
        self.last_act_ms = round((time.perf_counter() - started) * 1000, 1)
        return ok

    def press(self, key: str) -> bool:
        return bool(self._call(key, {}).get("ok"))

    def report_scene(self, layout: str, controls: list[dict]) -> None:
        self._call(
            "report-scene",
            {
                "primary_layout": layout,
                "controls": controls,
                "actionable": bool(controls),
                "source": self.job_name,
            },
        )

    def status(self) -> dict:
        return self._call("status")


class PollContext:
    """What a policy is given, and what it may do, during one poll."""

    def __init__(self, loop: "JobLoop", analysis: dict) -> None:
        self._loop = loop
        self._analysis = analysis
        self.screen: str | None = analysis.get("screen")
        self.screens: dict[str, str | None] = analysis.get("screens", {})
        self.found: list[Found] = [
            Found(
                element=instance["element"],
                center=(instance["center"][0], instance["center"][1]),
                score=instance["score"],
                region=instance.get("region", "frame"),
            )
            for instance in analysis.get("instances", [])
        ]
        self.acted = False
        self.sleep_seconds: float | None = None

    @property
    def config(self):
        return self._loop.config

    def get(self, key: str) -> Any:
        return self._loop.config.get(key)

    def first(self, element: str) -> Found | None:
        for candidate in self.found:
            if candidate.element == element:
                return candidate
        return None

    def click(self, element: str) -> bool:
        """Click a located element. Records the outcome; never raises."""

        target = self.first(element)
        if target is None:
            return False
        if not self._loop.client.click(*target.center):
            self._loop.events.problem("click-failed", element=element)
            return False
        self.acted = True
        self._loop.clicks += 1
        # The whole path that led here, so one line can be read end to end:
        # the look that found the control, its two worker-side phases, and the
        # click itself.
        self._loop.events.clicked(
            element=element,
            screen=self.screen,
            center=list(target.center),
            score=target.score,
            perceive_ms=self._analysis.get("perceive_ms"),
            classify_ms=self._analysis.get("classify_ms"),
            resolve_ms=self._analysis.get("resolve_ms"),
            act_ms=self._loop.client.last_act_ms,
        )
        return True

    def cycle(self, **fields: Any) -> None:
        self._loop.cycles += 1
        self._loop.events.cycle(completed=self._loop.cycles, **fields)

    def doing(self, what: str, **fields: Any) -> None:
        self._loop.events.doing(what, **fields)

    def idle(self, seconds: float | None = None) -> float | None:
        """Ask for a longer wait than the configured poll interval."""

        self.sleep_seconds = seconds
        return seconds


Policy = Callable[[PollContext], Any]


class JobLoop:
    """The crash-proof loop, the settings, and the record — once, for everyone.

    There is deliberately no built-in stall timeout. The gaps between steps in
    a game are long and unpredictable, and a loop that gives up on silence
    gives up on the normal case. It ends on an explicit stop, or on the
    operator's own `max_cycles`/`max_seconds` (0 meaning unlimited).
    """

    def __init__(
        self,
        job_name: str,
        *,
        declaration: Path | None = None,
        assets_dir: Path | None = None,
        config_schema=None,
        socket_path: Path | None = None,
        values_dir: Path | None = None,
        jobs_dir: Path | None = None,
    ) -> None:
        from streambot.job_config import ConfigSchema, JobConfig, values_path
        from streambot.job_events import JobEvents

        self.job_name = job_name
        self.declaration = Path(declaration) if declaration else None
        self.assets_dir = Path(assets_dir) if assets_dir else None
        self.client = JobClient(job_name, socket_path)
        self.events = JobEvents(job_name, jobs_dir)
        schema = config_schema or ConfigSchema.from_manifest(
            _manifest_config(job_name, jobs_dir)
        )
        self.config = JobConfig(schema, values_path(job_name, values_dir))
        self.cycles = 0
        self.clicks = 0
        self._stop = False
        self._registered = False
        self._last_look_at = 0.0

    def request_stop(self, *_args) -> None:
        self._stop = True

    def _record_look(self, analysis: dict) -> None:
        """Report what looking cost, whether or not it led to a click.

        Most of a run is spent looking and doing nothing — waiting out a level,
        a load, an idle period. If only clicks were reported the panel would
        show nothing at all for minutes at a time, which is exactly when an
        operator most wants to know the job is still alive and still fast.
        Throttled, because the record is for humans, not for every poll.
        """

        now = time.time()
        if now - self._last_look_at < 2.0:
            return
        self._last_look_at = now
        self.events.emit(
            "perceive",
            perceive_ms=analysis.get("perceive_ms"),
            classify_ms=analysis.get("classify_ms"),
            resolve_ms=analysis.get("resolve_ms"),
            screen=analysis.get("screen"),
        )

    def _ensure_registered(self) -> bool:
        if self._registered or self.declaration is None:
            return True
        response = self.client.register(self.declaration, self.assets_dir)
        if response.get("ok"):
            self._registered = True
            self.events.doing(
                "registered elements", elements=response.get("elements")
            )
        else:
            self.events.problem("register-failed", error=response.get("error"))
        return self._registered

    def run(self, policy: Policy, elements: Iterable[str] | None = None) -> str:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self.events.start(config=self.config.values)
        started = time.time()

        while not self._stop:
            changed = self.config.poll()
            if changed:
                # An operator changed something while this was running. Say so
                # in the record: a run that behaves differently from here on
                # should carry the reason next to the evidence.
                self.events.emit("config-changed", changed=changed)

            max_seconds = self.config.get("max_seconds")
            if max_seconds and time.time() - started >= max_seconds:
                return "max-seconds"
            max_cycles = self.config.get("max_cycles")
            if max_cycles and self.cycles >= max_cycles:
                return "max-cycles"

            sleep_seconds = float(self.config.get("poll_seconds"))
            if not self._ensure_registered():
                time.sleep(sleep_seconds)
                continue
            try:
                analysis = self.client.analyze(elements)
                if not analysis.get("ok"):
                    self.events.problem("analyze-failed", error=analysis.get("error"))
                else:
                    self._record_look(analysis)
                    context = PollContext(self, analysis)
                    policy(context)
                    if context.sleep_seconds is not None:
                        sleep_seconds = float(context.sleep_seconds)
                    elif context.acted:
                        sleep_seconds = min(sleep_seconds, 1.2)
            except Exception as error:
                # One bad poll must never end the run.
                self.events.problem("poll-error", error=type(error).__name__)
            time.sleep(sleep_seconds)
        return "stop-requested"

    def finish(self, reason: str) -> int:
        self.events.emit(
            "done", reason=reason, cycles=self.cycles, clicks=self.clicks
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "stopped": reason,
                    "cycles": self.cycles,
                    "clicks": self.clicks,
                },
                ensure_ascii=False,
            )
        )
        return 0


def _manifest_config(job_name: str, jobs_dir: Path | None) -> Any:
    """The `config` block from this job's manifest, if it declares one."""

    root = Path(jobs_dir) if jobs_dir else Path(
        os.environ.get(
            "STREAMBOT_JOBS_DIR",
            str(Path.home() / "Codes" / "Private" / "lolita" / "streambot-jobs" / "jobs"),
        )
    )
    try:
        manifest = json.loads((root / job_name / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest.get("config") if isinstance(manifest, dict) else None


def runner_arguments(description: str) -> argparse.Namespace:
    """The arguments every runner accepts.

    Settings belong in the config file, where they can be changed while the
    job runs; these are only what must be fixed before it starts.
    """

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--socket", type=Path, default=None)
    parser.add_argument("--jobs-dir", type=Path, default=None)
    parser.add_argument("--values-dir", type=Path, default=None)
    return parser.parse_args()


__all__ = [
    "Found",
    "JobClient",
    "JobLoop",
    "PollContext",
    "bootstrap",
    "default_socket",
    "runner_arguments",
    "streambot_home",
]
