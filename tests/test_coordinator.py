"""Tests for serialized persistent interaction coordination."""

from __future__ import annotations

import unittest

from streambot.coordinator import (
    CoordinatorState,
    InteractionCoordinator,
    InteractionPolicy,
)
from streambot.events import ActionCandidate, PerceptionEvent


class FakeDispatcher:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.releases = 0

    def dispatch(self, candidate, idempotency_key):
        self.actions.append(idempotency_key)

    def release_all(self):
        self.releases += 1


def event(
    *,
    sequence: int = 1,
    expires_at: float = 2.0,
    epoch: int = 0,
    action_kind: str = "click",
) -> PerceptionEvent:
    coordinates = (100, 200) if action_kind == "click" else (None, None)
    return PerceptionEvent(
        sequence,
        "action-ready",
        "choice",
        sequence,
        0.0,
        0.1,
        expires_at,
        "layout",
        0.9,
        (ActionCandidate("best", action_kind, *coordinates),),
        epoch,
    )


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.2
        self.dispatcher = FakeDispatcher()
        self.policy = InteractionPolicy(
            "choice",
            ("click", "wait-for-timeout"),
            retry_allowed=True,
            retry_after_seconds=0.5,
            feedback_timeout_seconds=1.5,
        )
        self.coordinator = InteractionCoordinator(
            self.dispatcher,
            (self.policy,),
            stream_width=1280,
            stream_height=720,
            clock=lambda: self.now,
        )

    def test_one_event_produces_at_most_one_input_attempt(self) -> None:
        current = event()
        self.assertTrue(self.coordinator.accept(current))
        self.assertFalse(self.coordinator.accept(current))
        self.assertEqual(len(self.dispatcher.actions), 1)

    def test_expired_duplicate_and_wrong_epoch_produce_no_input(self) -> None:
        self.now = 3.0
        self.assertFalse(self.coordinator.accept(event()))
        self.assertFalse(self.coordinator.accept(event(epoch=1, expires_at=4.0)))
        self.assertEqual(self.dispatcher.actions, [])

    def test_two_clear_samples_confirm_success_and_unblock(self) -> None:
        self.coordinator.accept(event())
        self.assertFalse(self.coordinator.observe_feedback(2, False))
        self.assertTrue(self.coordinator.observe_feedback(3, False))
        self.assertEqual(self.coordinator.state, CoordinatorState.OBSERVING)
        self.assertEqual(self.coordinator.workflow_epoch, 1)

    def test_retry_occurs_once_only_while_original_layout_remains(self) -> None:
        self.coordinator.accept(event(expires_at=3.0))
        self.now = 0.8
        self.coordinator.observe_feedback(2, True)
        self.now = 0.9
        self.coordinator.observe_feedback(3, True)
        self.assertEqual(len(self.dispatcher.actions), 2)
        self.assertEqual(self.coordinator.metrics.actions_retried, 1)

    def test_accepted_event_expiry_does_not_cancel_feedback_retry(self) -> None:
        self.coordinator.accept(event(expires_at=0.4))
        self.now = 0.8

        self.coordinator.observe_feedback(2, True)

        self.assertEqual(len(self.dispatcher.actions), 2)
        self.assertEqual(self.coordinator.state, CoordinatorState.VERIFYING)

    def test_uncertain_feedback_fails_closed_and_releases_input(self) -> None:
        self.coordinator.accept(event(expires_at=4.0))
        self.now = 2.0
        self.coordinator.observe_feedback(2, True)
        self.assertEqual(self.coordinator.state, CoordinatorState.FAILED)
        self.assertEqual(self.dispatcher.releases, 1)

    def test_timeout_action_never_dispatches_input(self) -> None:
        self.assertTrue(
            self.coordinator.accept(event(action_kind="wait-for-timeout"))
        )
        self.assertEqual(self.dispatcher.actions, [])

    def test_reset_releases_input_and_clears_live_lease(self) -> None:
        self.coordinator.accept(event())
        self.coordinator.reset()
        self.assertTrue(self.coordinator.is_idle)
        self.assertEqual(self.dispatcher.releases, 1)


if __name__ == "__main__":
    unittest.main()


class ResetWatermarkTests(unittest.TestCase):
    def test_reset_clears_committed_frame_watermark(self) -> None:
        dispatcher = FakeDispatcher()
        policy = InteractionPolicy("choice", ("click",), clear_samples=1)
        coordinator = InteractionCoordinator(
            dispatcher,
            (policy,),
            stream_width=1280,
            stream_height=720,
            clock=lambda: 0.2,
        )
        self.assertTrue(coordinator.accept(event(sequence=500, expires_at=2.0)))
        self.assertTrue(coordinator.observe_feedback(501, False))
        self.assertEqual(coordinator.last_committed_action_frame, 500)
        coordinator.reset()
        # Frame numbers restart low after a reconnect; the stale watermark
        # must not reject the fresh event.
        fresh = event(sequence=3, epoch=coordinator.workflow_epoch, expires_at=2.0)
        self.assertTrue(coordinator.accept(fresh))
