"""Bounded event brokering and cadence scheduling for persistent perception."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
import time
from typing import Callable, Iterable, Protocol

from .events import ActionCandidate, PerceptionEvent
from .observation import Observation


class EventMailboxOverflow(RuntimeError):
    """Raised when a distinct actionable event cannot be retained safely."""


class ObservationMode(StrEnum):
    VIDEO = "video"
    INTERACTIVE = "interactive"
    URGENT = "urgent"


@dataclass(frozen=True)
class ModeRequest:
    """One bounded scheduler mode request from a target adapter."""

    mode: ObservationMode
    urgent_seconds: float = 0.0


@dataclass(frozen=True)
class Detection:
    """Target adapter output before broker sequencing and stabilization."""

    event_type: str
    scene_id: str
    layout_signature: str
    confidence: float
    candidates: tuple[ActionCandidate, ...] = ()
    expiry_seconds: float = 1.0
    error_type: str | None = None


@dataclass
class PerceptionMetrics:
    scans_video: int = 0
    scans_interactive: int = 0
    scans_urgent: int = 0
    events_published: int = 0
    events_deduplicated: int = 0
    events_expired: int = 0
    events_dropped: int = 0
    mailbox_overflows: int = 0
    detector_failures: int = 0
    publication_latency_count: int = 0
    publication_latency_seconds_total: float = 0.0
    publication_latency_seconds_max: float = 0.0


class PerceptionAdapter(Protocol):
    def detect(self, observation: Observation) -> Iterable[Detection]: ...
    def reset(self) -> None: ...


class BoundedEventMailbox:
    """Small in-process mailbox with actionable overflow fail-closed behavior."""

    def __init__(self, capacity: int = 32) -> None:
        if capacity < 1:
            raise ValueError("mailbox capacity must be positive")
        self.capacity = capacity
        self._events: deque[PerceptionEvent] = deque()
        self.input_paused = False

    def __len__(self) -> int:
        return len(self._events)

    def clear(self) -> None:
        self._events.clear()
        self.input_paused = False

    def put(self, event: PerceptionEvent) -> bool:
        for index, existing in enumerate(self._events):
            if existing.identity == event.identity:
                self._events[index] = event
                return True
        if len(self._events) < self.capacity:
            self._events.append(event)
            return True
        if event.event_type == "action-ready":
            self.input_paused = True
            raise EventMailboxOverflow("distinct actionable event overflow")
        for index, existing in enumerate(self._events):
            if existing.event_type != "action-ready":
                del self._events[index]
                self._events.append(event)
                return True
        return False

    def expire(self, now: float) -> tuple[PerceptionEvent, ...]:
        expired = tuple(event for event in self._events if event.is_expired(now))
        self._events = deque(event for event in self._events if not event.is_expired(now))
        return expired

    def pop(self, now: float) -> PerceptionEvent | None:
        self.expire(now)
        if not self._events:
            return None
        return self._events.popleft()


class PerceptionBroker:
    """Sequence, deduplicate, expire, and publish perception events."""

    def __init__(
        self,
        *,
        capacity: int = 32,
        clock: Callable[[], float] = time.monotonic,
        callback: Callable[[PerceptionEvent], None] | None = None,
    ) -> None:
        self.clock = clock
        self.mailbox = BoundedEventMailbox(capacity)
        self.callback = callback
        self.metrics = PerceptionMetrics()
        self._sequence = 0
        self._active: set[tuple[str, str, str]] = set()

    def reset(self) -> None:
        self.mailbox.clear()
        self._active.clear()

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def publish(
        self,
        detection: Detection,
        observation: Observation,
        *,
        workflow_epoch: int = 0,
    ) -> PerceptionEvent | None:
        now = self.clock()
        identity = (
            detection.event_type,
            detection.scene_id,
            detection.layout_signature,
        )
        if identity in self._active:
            self.metrics.events_deduplicated += 1
            return None
        event = PerceptionEvent(
            sequence=self._next_sequence(),
            event_type=detection.event_type,
            scene_id=detection.scene_id,
            frame_number=observation.frame_number,
            observed_at=observation.observed_at,
            emitted_at=now,
            expires_at=now + detection.expiry_seconds,
            layout_signature=detection.layout_signature,
            confidence=detection.confidence,
            candidates=detection.candidates,
            workflow_epoch=workflow_epoch,
            error_type=detection.error_type,
        )
        if detection.event_type == "scene-cleared":
            self._active = {
                item for item in self._active if item[1] != detection.scene_id
            }
        try:
            admitted = self.mailbox.put(event)
        except EventMailboxOverflow:
            self.metrics.mailbox_overflows += 1
            raise
        if not admitted:
            self.metrics.events_dropped += 1
            return None
        self._active.add(identity)
        self.metrics.events_published += 1
        latency = max(0.0, now - observation.observed_at)
        self.metrics.publication_latency_count += 1
        self.metrics.publication_latency_seconds_total += latency
        self.metrics.publication_latency_seconds_max = max(
            self.metrics.publication_latency_seconds_max, latency
        )
        if self.callback is not None:
            self.callback(event)
        return event

    def expire(self) -> tuple[PerceptionEvent, ...]:
        expired = self.mailbox.expire(self.clock())
        self.metrics.events_expired += len(expired)
        for event in expired:
            self._active.discard(event.identity)
        return expired


class PerceptionScheduler:
    """Run one target adapter at a mode-dependent monotonic cadence."""

    RATES = {
        ObservationMode.VIDEO: 3.0,
        ObservationMode.INTERACTIVE: 8.0,
        ObservationMode.URGENT: 15.0,
    }

    def __init__(
        self,
        adapter: PerceptionAdapter,
        broker: PerceptionBroker,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.adapter = adapter
        self.broker = broker
        self.clock = clock
        self.mode = ObservationMode.VIDEO
        self._next_scan_at = 0.0
        self._urgent_deadline: float | None = None
        self._last_frame_number = -1

    def reset(self) -> None:
        self.adapter.reset()
        self.broker.reset()
        self.mode = ObservationMode.VIDEO
        self._next_scan_at = 0.0
        self._urgent_deadline = None
        self._last_frame_number = -1

    def set_mode(self, mode: ObservationMode, *, urgent_seconds: float = 0.0) -> None:
        self.mode = mode
        self._next_scan_at = 0.0
        if mode is ObservationMode.URGENT:
            if urgent_seconds <= 0:
                raise ValueError("urgent mode requires a positive deadline")
            self._urgent_deadline = self.clock() + urgent_seconds
        else:
            self._urgent_deadline = None

    def process(self, observation: Observation, *, workflow_epoch: int = 0) -> tuple[PerceptionEvent, ...]:
        now = self.clock()
        preferred_mode = getattr(self.adapter, "preferred_mode", None)
        if preferred_mode is not None:
            requested_value = preferred_mode(observation)
            requested = (
                requested_value
                if isinstance(requested_value, ModeRequest)
                else ModeRequest(requested_value)
            )
            if requested.mode is not self.mode:
                if requested.mode is ObservationMode.URGENT:
                    self.set_mode(
                        ObservationMode.URGENT,
                        urgent_seconds=requested.urgent_seconds,
                    )
                else:
                    self.mode = requested.mode
                    self._urgent_deadline = None
                    self._next_scan_at = 0.0
        if self.mode is ObservationMode.URGENT and self._urgent_deadline is not None:
            if now >= self._urgent_deadline:
                self.mode = ObservationMode.INTERACTIVE
                self._urgent_deadline = None
        if observation.frame_number <= self._last_frame_number or now < self._next_scan_at:
            return ()
        self._last_frame_number = observation.frame_number
        self._next_scan_at = now + 1.0 / self.RATES[self.mode]
        setattr(self.broker.metrics, f"scans_{self.mode.value}", getattr(self.broker.metrics, f"scans_{self.mode.value}") + 1)
        events: list[PerceptionEvent] = []
        try:
            detections = tuple(self.adapter.detect(observation))
        except Exception as error:
            self.broker.metrics.detector_failures += 1
            detections = (
                Detection(
                    event_type="perception-degraded",
                    scene_id="detector",
                    layout_signature=type(error).__name__,
                    confidence=0.0,
                    error_type=type(error).__name__,
                ),
            )
        for detection in detections:
            event = self.broker.publish(
                detection, observation, workflow_epoch=workflow_epoch
            )
            if event is not None:
                events.append(event)
        if any(item.event_type == "action-ready" for item in events):
            self.mode = ObservationMode.INTERACTIVE
            self._urgent_deadline = None
        return tuple(events)
