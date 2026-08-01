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
import time
from pathlib import Path
from typing import Any

DEFAULT_JOBS_DIR = Path(
    os.environ.get(
        "STREAMBOT_JOBS_DIR",
        str(Path.home() / "Codes" / "Private" / "streambot-jobs" / "jobs"),
    )
)

# The kinds the console understands. `click` feeds its click counter and rate,
# `cycle` its cycle count, `start` resets the session totals. Anything else is
# carried through to the event stream as-is and shown as a line.
CLICK = "click"
CYCLE = "cycle"
START = "start"


class JobEvents:
    """Append-only writer for the stream the console reads.

    Writing never raises. A job that cannot log should carry on working: the
    log exists to explain the work, not to gate it.
    """

    def __init__(self, job_name: str, jobs_dir: Path | None = None) -> None:
        self.job_name = job_name
        root = Path(jobs_dir) if jobs_dir else DEFAULT_JOBS_DIR
        self.path = root / job_name / "flow-log.jsonl"

    def emit(self, kind: str, **fields: Any) -> None:
        """Write one event. Seconds, because that is what the console reads."""

        line = {"event": kind, "t": int(time.time()), **fields}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def start(self, **fields: Any) -> None:
        """Mark a new session; the console resets its totals here."""

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
