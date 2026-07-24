"""Persistent branch-graph coverage ledger for map-driven DFS traversal.

The route map shown by the target app is the ground truth for coverage: it
displays traversed branches, unreached endpoint markers, and locked edges
natively. This ledger only caches what perception extracted from the map plus
the two things the map cannot display — why a lock exists and which traversals
this worker already performed — and it is corrected to match the map on every
observation (map wins).

The ledger is target-agnostic: nodes, edges, ends, and locks are opaque string
identifiers scoped by a map id (typically one section). Perception extractors
own the mapping from pixels to identifiers.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any, Iterable


SCHEMA_VERSION = 1


class BranchLedgerError(RuntimeError):
    """Raised when the ledger file cannot be used safely."""


class BranchLedger:
    """Track visited edges, unreached ends, and locks per map, atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if int(data.get("schema_version", 0)) != SCHEMA_VERSION:
                raise BranchLedgerError(
                    f"unsupported branch ledger schema in {path}"
                )
            self._data: dict[str, Any] = data
        else:
            self._data = {"schema_version": SCHEMA_VERSION, "maps": {}}

    # ------------------------------------------------------------- accessors

    def _map(self, map_id: str) -> dict[str, Any]:
        return self._data["maps"].setdefault(
            map_id,
            {
                "nodes": [],
                "ends_reached": [],
                "ends_unreached": [],
                "locks": {},
                "visited_edges": [],
            },
        )

    def snapshot(self, map_id: str) -> dict[str, Any]:
        """Return a deep copy of one map's state for read-only callers."""

        with self._lock:
            return json.loads(json.dumps(self._map(map_id)))

    # ----------------------------------------------------------- observation

    def observe_map(
        self,
        map_id: str,
        *,
        nodes: Iterable[str],
        ends_reached: Iterable[str] = (),
        ends_unreached: Iterable[str] = (),
    ) -> None:
        """Reconcile one map observation; the map always wins.

        An end that moved from unreached to reached stays reached even if a
        later partial observation omits it (scrolled out of view): omission is
        not evidence of regression, only contradiction is.
        """

        with self._lock:
            entry = self._map(map_id)
            entry["nodes"] = sorted(set(entry["nodes"]) | set(nodes))
            reached = set(entry["ends_reached"]) | set(ends_reached)
            unreached = (set(entry["ends_unreached"]) | set(ends_unreached)) - reached
            entry["ends_reached"] = sorted(reached)
            entry["ends_unreached"] = sorted(unreached)
            self._save()

    def record_lock(self, map_id: str, edge: str, reason: str | None = None) -> None:
        """Record a locked edge, keeping the first non-empty reason seen."""

        with self._lock:
            locks = self._map(map_id)["locks"]
            if edge not in locks or (reason and not locks[edge]):
                locks[edge] = reason or ""
            self._save()

    def record_traversal(self, map_id: str, edge: str) -> None:
        """Mark one branch edge as visited by this worker."""

        with self._lock:
            entry = self._map(map_id)
            if edge not in entry["visited_edges"]:
                entry["visited_edges"].append(edge)
            self._save()

    # -------------------------------------------------------------- frontier

    def unvisited_edges(self, map_id: str, candidates: Iterable[str]) -> tuple[str, ...]:
        """Return candidates not yet traversed and not known locked."""

        with self._lock:
            entry = self._map(map_id)
            visited = set(entry["visited_edges"])
            locked = set(entry["locks"])
            return tuple(c for c in candidates if c not in visited and c not in locked)

    def frontier_exhausted(self, map_id: str) -> bool:
        """True when the map shows no unreached end and every lock has a reason."""

        with self._lock:
            entry = self._map(map_id)
            if entry["ends_unreached"]:
                return False
            return all(reason for reason in entry["locks"].values())

    # ----------------------------------------------------------------- write

    def _save(self) -> None:
        payload = json.dumps(
            self._data, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)
