"""Deterministic timestamped tests for bounded temporal perception."""

from __future__ import annotations

import unittest

import numpy as np

from streambot.observation import Observation
from streambot.temporal_perception import (
    FlickerPhase,
    SceneContext,
    TemporalFlickerDetector,
    TemporalManifestError,
    load_temporal_scenes,
    TemporalPerceptionAdapter,
)
from streambot.perception_service import (
    ObservationMode,
    PerceptionBroker,
    PerceptionScheduler,
)


def manifest(two: bool = False):
    candidates = [
        {
            "candidate_id": "first",
            "action_kind": "click",
            "region": [20, 20, 40, 40],
            "click_point": [40, 40],
            "detector": "temporal-flicker",
            "high_threshold": 0.16,
            "low_threshold": 0.07,
            "minimum_high_samples": 2,
            "event_ttl_ms": 400,
            "feedback": "candidate-cleared-or-scene-changed",
            "retry_limit": 1,
        }
    ]
    if two:
        candidates.append(
            {
                "candidate_id": "second",
                "action_kind": "click",
                "region": [100, 20, 40, 40],
                "click_point": [120, 40],
                "detector": "temporal-flicker",
            }
        )
    return [
        {
            "scene_id": "synthetic-flash",
            "scene_evidence": "verified-synthetic-layout",
            "observation_mode": "urgent",
            "deadline_ms": 5000,
            "candidates": candidates,
            "control_regions": [[0, 80, 160, 40]],
            "ambiguity_margin": 0.04,
            "camera_cut_threshold": 0.10,
            "baseline_samples": 3,
            "history_size": 6,
        }
    ]


def frame(value: int = 50) -> np.ndarray:
    return np.full((120, 160, 3), value, dtype=np.uint8)


class TemporalManifestTests(unittest.TestCase):
    def test_valid_manifest_loads_and_contains_click(self) -> None:
        scene = load_temporal_scenes(
            manifest(), stream_width=160, stream_height=120
        )[0]
        self.assertEqual(scene.candidates[0].click_point, (40, 40))

    def test_rejects_bounds_overlap_threshold_and_deadline_errors(self) -> None:
        invalid_cases = []
        outside = manifest()
        outside[0]["candidates"][0]["click_point"] = [90, 90]
        invalid_cases.append(outside)
        overlap = manifest(two=True)
        overlap[0]["candidates"][1]["region"] = [30, 30, 40, 40]
        invalid_cases.append(overlap)
        thresholds = manifest()
        thresholds[0]["candidates"][0]["low_threshold"] = 0.2
        invalid_cases.append(thresholds)
        deadline = manifest()
        deadline[0]["deadline_ms"] = 0
        invalid_cases.append(deadline)
        for index, value in enumerate(invalid_cases):
            with self.subTest(index=index), self.assertRaises(TemporalManifestError):
                load_temporal_scenes(value, stream_width=160, stream_height=120)


class TemporalDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_temporal_scenes(
            manifest(), stream_width=160, stream_height=120
        )[0]
        self.context = SceneContext("synthetic-flash", 1, 0.0)
        self.detector = TemporalFlickerDetector()

    def observe(self, number: int, value: np.ndarray):
        return self.detector.observe(
            Observation(number, number / 15.0, value),
            self.context,
            self.settings,
        )

    def warm(self) -> None:
        for number in range(1, 4):
            self.observe(number, frame())

    def test_two_high_samples_produce_one_stable_result_and_event(self) -> None:
        self.warm()
        first = frame()
        first[20:60, 20:60] = 245
        rising = self.observe(4, first)[0]
        high = self.observe(5, first)[0]
        self.assertEqual(rising.phase, FlickerPhase.RISING)
        self.assertFalse(rising.stable)
        self.assertEqual(high.phase, FlickerPhase.HIGH)
        event = self.detector.detection_for(high, self.settings)
        self.assertIsNotNone(event)
        self.assertEqual(event.candidates[0].candidate_id, "first")
        self.assertEqual(event.expiry_seconds, 0.4)

    def test_flash_before_baseline_and_one_frame_spike_are_not_actionable(self) -> None:
        bright = frame()
        bright[20:60, 20:60] = 245
        early = self.observe(1, bright)[0]
        self.assertFalse(early.stable)
        self.observe(2, frame())
        self.observe(3, frame())
        spike = self.observe(4, bright)[0]
        cleared = self.observe(5, frame())[0]
        self.assertFalse(spike.stable)
        self.assertFalse(cleared.stable)

    def test_camera_cut_and_global_pulse_reset_history(self) -> None:
        self.warm()
        results = self.observe(4, frame(130))
        self.assertTrue(all(item.phase is FlickerPhase.REJECTED for item in results))
        bright = frame(130)
        bright[20:60, 20:60] = 245
        self.assertFalse(self.observe(5, bright)[0].stable)

    def test_outside_motion_and_diffuse_candidate_noise_are_rejected(self) -> None:
        self.warm()
        outside = frame()
        outside[80:110, 100:140] = 245
        self.assertFalse(self.observe(4, outside)[0].stable)
        noisy = frame()
        noisy[20:60:4, 20:60:4] = 245
        self.observe(5, noisy)
        self.assertFalse(self.observe(6, noisy)[0].stable)

    def test_equally_strong_candidates_are_ambiguous(self) -> None:
        value = manifest(two=True)
        value[0]["camera_cut_threshold"] = 0.20
        settings = load_temporal_scenes(
            value, stream_width=160, stream_height=120
        )[0]
        detector = TemporalFlickerDetector()
        for number in range(1, 4):
            detector.observe(
                Observation(number, number / 15.0, frame()), self.context, settings
            )
        bright = frame()
        bright[20:60, 20:60] = 245
        bright[20:60, 100:140] = 245
        detector.observe(Observation(4, 4 / 15.0, bright), self.context, settings)
        results = detector.observe(
            Observation(5, 5 / 15.0, bright), self.context, settings
        )
        self.assertTrue(all(not item.stable for item in results))
        self.assertTrue(all(item.phase is FlickerPhase.REJECTED for item in results))

    def test_epoch_change_requires_new_baseline(self) -> None:
        self.warm()
        bright = frame()
        bright[20:60, 20:60] = 245
        self.observe(4, bright)
        changed = SceneContext("synthetic-flash", 2, 4 / 15.0)
        result = self.detector.observe(
            Observation(5, 5 / 15.0, bright), changed, self.settings
        )[0]
        self.assertEqual(result.phase, FlickerPhase.BASELINE)
        self.assertFalse(result.stable)


class TemporalSchedulerTests(unittest.TestCase):
    def test_urgent_adapter_publishes_once_and_exits_urgent_mode(self) -> None:
        settings = load_temporal_scenes(
            manifest(), stream_width=160, stream_height=120
        )
        now = 0.0
        adapter = TemporalPerceptionAdapter(
            settings, lambda _observation: "synthetic-flash", clock=lambda: now
        )
        broker = PerceptionBroker(clock=lambda: now)
        scheduler = PerceptionScheduler(adapter, broker, clock=lambda: now)
        events = []
        for number in range(1, 4):
            now = number / 15.0
            events.extend(
                scheduler.process(Observation(number, now, frame()))
            )
        bright = frame()
        bright[20:60, 20:60] = 245
        for number in (4, 5):
            now = number / 15.0
            events.extend(
                scheduler.process(Observation(number, now, bright))
            )
        actionable = [item for item in events if item.event_type == "action-ready"]
        self.assertEqual(len(actionable), 1)
        self.assertLessEqual(
            actionable[0].emitted_at - 4 / 15.0, 2 / 15.0
        )
        self.assertEqual(scheduler.mode, ObservationMode.INTERACTIVE)

    def test_temporal_deadline_emits_observation_only_timeout(self) -> None:
        settings = load_temporal_scenes(
            manifest(), stream_width=160, stream_height=120
        )
        now = 0.0
        adapter = TemporalPerceptionAdapter(
            settings, lambda _observation: "synthetic-flash", clock=lambda: now
        )
        broker = PerceptionBroker(clock=lambda: now)
        scheduler = PerceptionScheduler(adapter, broker, clock=lambda: now)
        scheduler.process(Observation(1, 0.0, frame()))
        now = 5.1
        events = scheduler.process(Observation(2, now, frame()))
        self.assertEqual([item.event_type for item in events], ["temporal-timeout"])
        self.assertTrue(all(not item.candidates for item in events))


if __name__ == "__main__":
    unittest.main()
