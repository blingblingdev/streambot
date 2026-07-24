"""Tests for persistent metadata-only perception services."""

from __future__ import annotations

import json
import unittest

import numpy as np

from streambot.events import ActionCandidate, PerceptionEvent
from streambot.observation import Observation
from streambot.perception_service import (
    BoundedEventMailbox,
    Detection,
    EventMailboxOverflow,
    PerceptionBroker,
    PerceptionScheduler,
    ObservationMode,
)


def observation(number: int = 1, observed_at: float = 0.0) -> Observation:
    return Observation(number, observed_at, np.zeros((2, 2, 3), dtype=np.uint8))


class EventContractTests(unittest.TestCase):
    def test_rejects_invalid_contract_values(self) -> None:
        candidate = ActionCandidate("left", "click", 10, 20)
        base = dict(
            sequence=1,
            event_type="action-ready",
            scene_id="choice",
            frame_number=1,
            observed_at=0.0,
            emitted_at=1.0,
            expires_at=2.0,
            layout_signature="abc",
            confidence=0.9,
            candidates=(candidate,),
        )
        for field, value in (
            ("sequence", 0),
            ("frame_number", -1),
            ("confidence", float("nan")),
            ("expires_at", 1.0),
        ):
            changed = dict(base)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                PerceptionEvent(**changed)
        with self.assertRaises(ValueError):
            ActionCandidate("bad", "click", 1, None)
        with self.assertRaises(ValueError):
            ActionCandidate("bad", "click", 8000, 1)

    def test_diagnostic_serialization_contains_no_pixels_text_or_coordinates(self) -> None:
        event = PerceptionEvent(
            1,
            "action-ready",
            "known-choice",
            7,
            1.0,
            1.1,
            2.1,
            "digest",
            1.0,
            (ActionCandidate("route-choice", "click", 321, 456),),
        )
        encoded = json.dumps(event.diagnostic_payload(1.2), sort_keys=True)
        self.assertNotIn("321", encoded)
        self.assertNotIn("456", encoded)
        self.assertNotIn("frame_data", encoded)
        self.assertNotIn("ocr", encoded.casefold())


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1.0
        self.broker = PerceptionBroker(clock=lambda: self.now)
        self.action = Detection(
            "action-ready",
            "choice",
            "layout-a",
            0.9,
            (ActionCandidate("best", "click", 100, 200),),
        )

    def test_repeated_identity_is_suppressed_and_sequence_is_monotonic(self) -> None:
        first = self.broker.publish(self.action, observation(observed_at=0.5))
        duplicate = self.broker.publish(self.action, observation(2, 0.6))
        changed = self.broker.publish(
            Detection("scene-updated", "choice", "layout-b", 1.0),
            observation(3, 0.7),
        )
        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(changed.sequence, first.sequence + 1)
        self.assertEqual(self.broker.metrics.events_deduplicated, 1)

    def test_scene_clear_permits_later_reentry(self) -> None:
        self.broker.publish(self.action, observation())
        self.broker.publish(
            Detection("scene-cleared", "choice", "cleared", 1.0), observation(2)
        )
        reentered = self.broker.publish(self.action, observation(3))
        self.assertIsNotNone(reentered)

    def test_expired_event_cannot_become_actionable(self) -> None:
        self.broker.publish(self.action, observation())
        self.now = 2.1
        self.assertIsNone(self.broker.mailbox.pop(self.now))

    def test_distinct_action_overflow_fails_closed(self) -> None:
        mailbox = BoundedEventMailbox(capacity=1)
        first = self.broker.publish(self.action, observation())
        mailbox.put(first)
        other = PerceptionEvent(
            2,
            "action-ready",
            "other",
            2,
            0.0,
            1.0,
            2.0,
            "layout-b",
            1.0,
            (ActionCandidate("other", "click", 1, 1),),
        )
        with self.assertRaises(EventMailboxOverflow):
            mailbox.put(other)
        self.assertTrue(mailbox.input_paused)


class SchedulerCadenceTests(unittest.TestCase):
    def test_video_mode_honors_cadence_and_skips_detector_calls(self) -> None:
        class Adapter:
            calls = 0

            def detect(self, _observation):
                self.calls += 1
                return ()

            def reset(self):
                pass

        now = 0.0
        adapter = Adapter()
        broker = PerceptionBroker(clock=lambda: now)
        scheduler = PerceptionScheduler(adapter, broker, clock=lambda: now)
        scheduler.process(observation(1))
        now = 0.1
        scheduler.process(observation(2))
        now = 0.34
        scheduler.process(observation(3))
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(broker.metrics.scans_video, 2)

    def test_urgent_mode_falls_back_after_deadline(self) -> None:
        class Adapter:
            def detect(self, _observation):
                return ()

            def reset(self):
                pass

        now = 1.0
        scheduler = PerceptionScheduler(
            Adapter(), PerceptionBroker(clock=lambda: now), clock=lambda: now
        )
        scheduler.set_mode(ObservationMode.URGENT, urgent_seconds=0.2)
        now = 1.21
        scheduler.process(observation())
        self.assertEqual(scheduler.mode, ObservationMode.INTERACTIVE)


if __name__ == "__main__":
    unittest.main()
