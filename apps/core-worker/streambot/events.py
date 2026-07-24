"""Immutable metadata-only contracts for persistent perception."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


EVENT_TYPES = frozenset(
    {
        "scene-entered",
        "scene-updated",
        "scene-cleared",
        "action-ready",
        "action-expired",
        "unknown-layout",
        "unmapped-choice",
        "ocr-unresolved",
        "ambiguous-choice",
        "perception-degraded",
        "ambiguous-flicker",
        "temporal-timeout",
    }
)


@dataclass(frozen=True)
class ActionCandidate:
    """One semantic action candidate with optional stream-space coordinates."""

    candidate_id: str
    action_kind: str
    x: int | None = None
    y: int | None = None

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.action_kind:
            raise ValueError("candidate identity and action kind are required")
        if (self.x is None) != (self.y is None):
            raise ValueError("candidate coordinates must be both present or absent")
        if self.x is not None:
            if isinstance(self.x, bool) or isinstance(self.y, bool):
                raise ValueError("candidate coordinates must be integers")
            if not isinstance(self.x, int) or not isinstance(self.y, int):
                raise ValueError("candidate coordinates must be integers")
            if not 0 <= self.x < 7680 or not 0 <= self.y < 4320:
                raise ValueError("candidate coordinates are outside supported bounds")


@dataclass(frozen=True)
class PerceptionEvent:
    """One sanitized perception fact bound to a source observation."""

    sequence: int
    event_type: str
    scene_id: str
    frame_number: int
    observed_at: float
    emitted_at: float
    expires_at: float
    layout_signature: str
    confidence: float
    candidates: tuple[ActionCandidate, ...] = ()
    workflow_epoch: int = 0
    error_type: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise ValueError("event sequence must be positive")
        if self.event_type not in EVENT_TYPES:
            raise ValueError("event type is unsupported")
        if not self.scene_id or not self.layout_signature:
            raise ValueError("scene identity and layout signature are required")
        if isinstance(self.frame_number, bool) or self.frame_number < 0:
            raise ValueError("frame number must be non-negative")
        times = (self.observed_at, self.emitted_at, self.expires_at)
        if not all(math.isfinite(value) for value in times):
            raise ValueError("event times must be finite")
        if self.emitted_at < self.observed_at or self.expires_at <= self.emitted_at:
            raise ValueError("event timing order is invalid")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("event confidence must be finite and bounded")
        if self.event_type == "action-ready" and not self.candidates:
            raise ValueError("action-ready requires at least one candidate")
        if self.event_type != "action-ready" and self.candidates:
            raise ValueError("only action-ready may carry candidates")
        if self.workflow_epoch < 0:
            raise ValueError("workflow epoch must be non-negative")
        if self.event_type == "perception-degraded" and not self.error_type:
            raise ValueError("degraded events require an error type")

    @property
    def identity(self) -> tuple[str, str, str]:
        """Return the stable deduplication identity."""

        return self.event_type, self.scene_id, self.layout_signature

    def is_expired(self, now: float) -> bool:
        return self.expires_at <= now

    def diagnostic_payload(self, now: float) -> dict[str, Any]:
        """Return a coordinate-free, text-free diagnostic representation."""

        payload: dict[str, Any] = {
            "schema_version": 1,
            "type": self.event_type,
            "sequence": self.sequence,
            "scene_id": self.scene_id,
            "frame_number": self.frame_number,
            "confidence": self.confidence,
            "candidate_ids": [item.candidate_id for item in self.candidates],
            "expires_in_ms": max(0, round((self.expires_at - now) * 1000)),
        }
        if self.error_type is not None:
            payload["error_type"] = self.error_type
        return payload
