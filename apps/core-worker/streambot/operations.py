"""The record of what was actually done to the machine.

Every operation that reaches the worker — a look at the screen, an analysis of
what is on it, an input sent to it — is appended here as one line, attributed
to the job that asked for it. This is the audit trail, and it is written by the
platform rather than by each job, for two reasons:

- A job's own log says what the job believed. This says what the worker did.
  When those disagree, only one of them is evidence.
- A record a job writes is a record a job can forget to write. Anything that
  goes through the worker is recorded whether the caller cooperates or not.

Metadata only, and structurally so: `record` accepts scalars and plain
containers of scalars, and silently drops anything else. A frame, an array or
a buffer therefore cannot reach this file even by mistake.

Writing never raises. If the journal cannot be written the work carries on —
losing the record of an operation is bad, failing the operation because of it
is worse.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

# A long-lived worker performs millions of operations. Keep one rollover so a
# stalled investigation still has recent history without the file growing
# without bound.
MAX_JOURNAL_BYTES = 8 * 1024 * 1024

OBSERVE = "observe"
ANALYZE = "analyze"
ACT = "act"
REGISTER = "register"

_SCALARS = (str, int, float, bool)


def _clean(value: Any, depth: int = 0) -> Any:
    """Reduce a value to recordable metadata, or None if it is not."""

    if value is None or isinstance(value, _SCALARS):
        return value
    if depth >= 3:
        return None
    if isinstance(value, (list, tuple)):
        cleaned = [_clean(item, depth + 1) for item in value]
        return [item for item in cleaned if item is not None]
    if isinstance(value, dict):
        return {
            str(key): _clean(item, depth + 1)
            for key, item in value.items()
            if _clean(item, depth + 1) is not None
        }
    return None  # arrays, buffers, objects: never


class OperationJournal:
    """Append-only, metadata-only record of worker operations."""

    def __init__(self, path: Path | str, *, max_bytes: int = MAX_JOURNAL_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = int(max_bytes)

    def record(
        self,
        operation: str,
        *,
        job: str | None = None,
        ok: bool = True,
        ms: float | None = None,
        **fields: Any,
    ) -> None:
        line: dict[str, Any] = {
            "t": int(time.time()),
            "op": str(operation),
            "job": str(job) if job else None,
            "ok": bool(ok),
        }
        if ms is not None:
            line["ms"] = round(float(ms), 1)
        for key, value in fields.items():
            cleaned = _clean(value)
            if cleaned is not None:
                line[str(key)] = cleaned
        try:
            self._rotate_if_large()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass  # the record is not allowed to be what stops the work

    def _rotate_if_large(self) -> None:
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        try:
            self.path.replace(self.path.with_suffix(".prev.jsonl"))
        except OSError:
            pass


__all__ = ["ACT", "ANALYZE", "OBSERVE", "REGISTER", "OperationJournal"]
