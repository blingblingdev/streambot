"""Offline tests for the shared job runtime: no socket, no worker, no game."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from streambot.job_config import ConfigSchema, values_path, write_values
from streambot.jobkit import JobLoop, PollContext

IDLE_FIELD = {
    "key": "idle_seconds",
    "type": "integer",
    "min": 1,
    "max": 3600,
    "default": 210,
}


def analysis(screen: str | None, *instances: tuple[str, int, int]) -> dict:
    return {
        "ok": True,
        "screen": screen,
        "screens": {"frame": screen},
        "classify_ms": 4.0,
        "resolve_ms": 2.0,
        "instances": [
            {"element": element, "center": [x, y], "score": 0.99, "region": "frame"}
            for element, x, y in instances
        ],
    }


class FakeClient:
    """Stands in for the worker: records what the job asked it to do."""

    def __init__(self, timeline: list[dict]) -> None:
        self.timeline = timeline
        self.clicks: list[tuple[int, int]] = []
        self.analyses = 0
        self.registered = False

    def register(self, declaration, assets_dir=None) -> dict:
        self.registered = True
        return {"ok": True, "elements": ["replay"]}

    def analyze(self, elements=None) -> dict:
        index = min(self.analyses, len(self.timeline) - 1)
        self.analyses += 1
        return self.timeline[index]

    def click(self, x: int, y: int) -> bool:
        self.clicks.append((x, y))
        return True

    def report_scene(self, layout, controls) -> None:
        pass


class LoopFixture:
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.schema = ConfigSchema.from_manifest({"fields": [dict(IDLE_FIELD)]})

    def tearDown(self) -> None:
        self._directory.cleanup()

    def build(self, timeline: list[dict], **values) -> JobLoop:
        path = values_path("demo", self.root / "config")
        write_values(path, values)
        loop = JobLoop(
            "demo",
            config_schema=self.schema,
            values_dir=self.root / "config",
            jobs_dir=self.root / "jobs",
        )
        loop.client = FakeClient(timeline)
        loop.events.path = self.root / "flow-log.jsonl"
        return loop

    def events(self, loop: JobLoop) -> list[dict]:
        path = loop.events.path
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def drive(self, loop: JobLoop, policy, sleeps: int) -> str:
        """Run the loop for a bounded number of sleeps, then stop it."""

        import streambot.jobkit as jobkit

        calls = {"n": 0}
        real_sleep = jobkit.time.sleep

        def fake_sleep(seconds: float) -> None:
            calls["n"] += 1
            loop.slept = getattr(loop, "slept", [])
            loop.slept.append(seconds)
            if calls["n"] >= sleeps:
                loop.request_stop()

        jobkit.time.sleep = fake_sleep
        try:
            return loop.run(policy)
        finally:
            jobkit.time.sleep = real_sleep


class LoopTests(LoopFixture, unittest.TestCase):
    def test_a_policy_clicks_what_the_worker_located(self) -> None:
        loop = self.build([analysis("settlement", ("replay", 790, 632))])

        def policy(ctx: PollContext) -> None:
            if ctx.screen == "settlement":
                ctx.click("replay")
                ctx.cycle()

        self.drive(loop, policy, sleeps=1)
        self.assertEqual(loop.client.clicks, [(790, 632)])
        self.assertEqual(loop.cycles, 1)
        kinds = [event["event"] for event in self.events(loop)]
        self.assertEqual(kinds[0], "start")
        self.assertIn("click", kinds)
        self.assertIn("cycle", kinds)

    def test_a_click_event_carries_the_analysis_cost(self) -> None:
        loop = self.build([analysis("settlement", ("replay", 790, 632))])
        self.drive(loop, lambda ctx: ctx.click("replay"), sleeps=1)
        click = [e for e in self.events(loop) if e["event"] == "click"][0]
        self.assertEqual(click["resolve_ms"], 2.0)
        self.assertEqual(click["classify_ms"], 4.0)
        self.assertEqual(click["element"], "replay")

    def test_registration_happens_once_before_any_analysis(self) -> None:
        loop = self.build([analysis(None)])
        loop.declaration = self.root / "elements.json"
        self.drive(loop, lambda ctx: None, sleeps=2)
        self.assertTrue(loop.client.registered)

    def test_an_unknown_screen_is_waited_out_not_failed(self) -> None:
        loop = self.build([analysis(None)])
        reason = self.drive(loop, lambda ctx: None, sleeps=3)
        self.assertEqual(reason, "stop-requested")
        self.assertEqual(loop.client.clicks, [])

    def test_a_policy_that_raises_does_not_end_the_run(self) -> None:
        loop = self.build([analysis("settlement", ("replay", 1, 2))])

        def explode(ctx: PollContext) -> None:
            raise RuntimeError("boom")

        reason = self.drive(loop, explode, sleeps=3)
        self.assertEqual(reason, "stop-requested")
        problems = [e for e in self.events(loop) if e["event"] == "job-error"]
        self.assertEqual(problems[0]["what"], "poll-error")
        self.assertEqual(problems[0]["error"], "RuntimeError")

    def test_a_failed_analysis_is_reported_and_retried(self) -> None:
        loop = self.build([{"ok": False, "error": "NoFrameAvailable"}])
        self.drive(loop, lambda ctx: None, sleeps=2)
        problems = [e for e in self.events(loop) if e["event"] == "job-error"]
        self.assertEqual(problems[0]["what"], "analyze-failed")

    def test_zero_cycles_means_unlimited(self) -> None:
        loop = self.build([analysis("settlement", ("replay", 1, 2))], max_cycles=0)
        reason = self.drive(loop, lambda ctx: ctx.cycle(), sleeps=4)
        self.assertEqual(reason, "stop-requested")
        self.assertGreaterEqual(loop.cycles, 3)

    def test_a_cycle_cap_stops_the_run(self) -> None:
        loop = self.build([analysis("settlement", ("replay", 1, 2))], max_cycles=2)
        reason = self.drive(loop, lambda ctx: ctx.cycle(), sleeps=6)
        self.assertEqual(reason, "max-cycles")
        self.assertEqual(loop.cycles, 2)

    def test_the_poll_interval_is_configuration(self) -> None:
        loop = self.build([analysis(None)], poll_seconds=7.0)
        self.drive(loop, lambda ctx: None, sleeps=1)
        self.assertEqual(loop.slept, [7.0])

    def test_a_policy_may_ask_to_wait_longer(self) -> None:
        loop = self.build([analysis(None)])
        self.drive(loop, lambda ctx: ctx.idle(30.0), sleeps=1)
        self.assertEqual(loop.slept, [30.0])


class HotConfigTests(LoopFixture, unittest.TestCase):
    def test_a_change_mid_run_is_adopted_and_recorded(self) -> None:
        loop = self.build([analysis(None)], idle_seconds=210)
        seen: list[int] = []

        def policy(ctx: PollContext) -> None:
            seen.append(ctx.get("idle_seconds"))
            if len(seen) == 1:
                # The operator edits the file while the job is running.
                write_values(
                    values_path("demo", self.root / "config"), {"idle_seconds": 930}
                )

        self.drive(loop, policy, sleeps=3)
        self.assertEqual(seen[0], 210)
        self.assertIn(930, seen[1:])
        changes = [e for e in self.events(loop) if e["event"] == "config-changed"]
        self.assertEqual(changes[0]["changed"], {"idle_seconds": 930})

    def test_a_cap_set_mid_run_stops_the_job(self) -> None:
        loop = self.build([analysis("settlement", ("replay", 1, 2))])

        def policy(ctx: PollContext) -> None:
            ctx.cycle()
            write_values(values_path("demo", self.root / "config"), {"max_cycles": 1})

        reason = self.drive(loop, policy, sleeps=5)
        self.assertEqual(reason, "max-cycles")


if __name__ == "__main__":
    unittest.main()
