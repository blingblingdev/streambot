"""Branch-graph coverage ledger: map-wins reconciliation and DFS frontier."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from streambot.branch_ledger import BranchLedger, BranchLedgerError


class BranchLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="branch-ledger-")
        self.path = Path(self._dir.name) / "branch-ledger.json"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_observation_reconciles_and_persists(self) -> None:
        ledger = BranchLedger(self.path)
        ledger.observe_map(
            "chapter-6",
            nodes=["n0", "n1"],
            ends_reached=["end-a"],
            ends_unreached=["end-b"],
        )
        reloaded = BranchLedger(self.path).snapshot("chapter-6")
        self.assertEqual(reloaded["nodes"], ["n0", "n1"])
        self.assertEqual(reloaded["ends_reached"], ["end-a"])
        self.assertEqual(reloaded["ends_unreached"], ["end-b"])

    def test_reached_end_wins_over_stale_unreached(self) -> None:
        ledger = BranchLedger(self.path)
        ledger.observe_map("c", nodes=[], ends_unreached=["end-a"])
        ledger.observe_map("c", nodes=[], ends_reached=["end-a"])
        state = ledger.snapshot("c")
        self.assertEqual(state["ends_reached"], ["end-a"])
        self.assertEqual(state["ends_unreached"], [])
        # A later partial observation omitting the end does not regress it.
        ledger.observe_map("c", nodes=["n9"])
        self.assertEqual(ledger.snapshot("c")["ends_reached"], ["end-a"])

    def test_unvisited_edges_exclude_traversed_and_locked(self) -> None:
        ledger = BranchLedger(self.path)
        ledger.record_traversal("c", "e1")
        ledger.record_lock("c", "e2", "affinity below threshold")
        self.assertEqual(
            ledger.unvisited_edges("c", ["e1", "e2", "e3"]), ("e3",)
        )

    def test_frontier_exhaustion_requires_reasons_and_no_unreached(self) -> None:
        ledger = BranchLedger(self.path)
        ledger.observe_map("c", nodes=[], ends_unreached=["end-a"])
        self.assertFalse(ledger.frontier_exhausted("c"))
        ledger.observe_map("c", nodes=[], ends_reached=["end-a"])
        ledger.record_lock("c", "e2")
        self.assertFalse(ledger.frontier_exhausted("c"))  # lock without reason
        ledger.record_lock("c", "e2", "requires chapter 7")
        self.assertTrue(ledger.frontier_exhausted("c"))

    def test_lock_keeps_first_reason(self) -> None:
        ledger = BranchLedger(self.path)
        ledger.record_lock("c", "e1", "first reason")
        ledger.record_lock("c", "e1", "second reason")
        self.assertEqual(ledger.snapshot("c")["locks"]["e1"], "first reason")

    def test_rejects_unknown_schema(self) -> None:
        self.path.write_text(json.dumps({"schema_version": 99, "maps": {}}))
        with self.assertRaises(BranchLedgerError):
            BranchLedger(self.path)


if __name__ == "__main__":
    unittest.main()
