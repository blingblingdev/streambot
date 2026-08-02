"""Notice when nothing is happening, and say so.

Every operation a job performs reaches the game through this package, so this
package is the only place that can tell the difference between a job that is
working and a job that has stopped. Nothing else can: a process can be alive,
its log can be recent, and its screen can be frozen for eight minutes while it
retries the same failed alignment. That happened repeatedly, and each time it
was a person noticing the screen was still, long after the fact.

A watchdog turns silence into an event. It is armed with a timeout, touched
by every operation, and when the gap since the last one crosses the timeout it
calls back once. It does not call back again until something happens and stops
happening a second time, because an alarm that repeats every tick is an alarm
people stop reading.

    watchdog = StallWatchdog(timeout_seconds=180.0, on_stall=tell_someone)
    watchdog.start()
    ...
    watchdog.touch("click")     # every operation

The timeout is configurable, as it must be: drawing a bridge clicks several
times a second, while a job waiting for a person to take a turn may be right
to sit still for ten minutes. `STREAMBOT_STALL_SECONDS` sets the default for
a process; the constructor argument overrides it.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable

DEFAULT_TIMEOUT_SECONDS = 180.0

# Off by default. A watchdog that fires on a worker nobody is watching just
# writes into the dark; jobs that want one ask for it.
ENV_TIMEOUT = "STREAMBOT_STALL_SECONDS"


def configured_timeout(default: float = DEFAULT_TIMEOUT_SECONDS) -> float:
    """The stall timeout this process is configured with, in seconds.

    A value of zero or less, or anything unparseable, means "no watchdog" —
    a bad setting must not be able to arm an alarm at a nonsense interval.
    """

    raw = os.environ.get(ENV_TIMEOUT)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class StallWatchdog:
    """Calls back when no operation has been recorded for `timeout_seconds`.

    Thread-safe: `touch` is called from whichever thread is doing the work,
    and the check runs on its own. The callback runs on the watchdog thread
    and must not raise — anything it throws is swallowed, because an alarm
    that takes the job down with it is worse than the stall it was reporting.
    """

    def __init__(
        self,
        timeout_seconds: float | None = None,
        on_stall: Callable[[float, str], None] | None = None,
        *,
        poll_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout_seconds = (
            configured_timeout() if timeout_seconds is None else float(timeout_seconds)
        )
        self._on_stall = on_stall
        self._poll_seconds = max(0.1, float(poll_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._last_touch = clock()
        self._last_what = "start"
        self._fired = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- recording work ---------------------------------------------------

    def touch(self, what: str = "") -> None:
        """Record that something happened. Re-arms a watchdog that has fired."""

        with self._lock:
            self._last_touch = self._clock()
            if what:
                self._last_what = what
            self._fired = False

    def idle_seconds(self) -> float:
        with self._lock:
            return self._clock() - self._last_touch

    @property
    def armed(self) -> bool:
        return self.timeout_seconds > 0

    # --- the check --------------------------------------------------------

    def check(self) -> bool:
        """Fire the callback if the silence has gone on too long.

        Returns True when it fired. Exposed so a caller can drive the check
        itself — the tests do, and so can a job that would rather not have a
        thread. Fires once per stall: silence that has already been reported
        is not news until something happens again.
        """

        if not self.armed:
            return False
        with self._lock:
            idle = self._clock() - self._last_touch
            if idle < self.timeout_seconds or self._fired:
                return False
            self._fired = True
            what = self._last_what
        if self._on_stall is not None:
            try:
                self._on_stall(idle, what)
            except Exception:
                # An alarm must never be the thing that stops the work.
                pass
        return True

    # --- running on its own thread ---------------------------------------

    def start(self) -> None:
        if not self.armed or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="streambot-stall-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            self.check()

    def __enter__(self) -> "StallWatchdog":
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.stop()
