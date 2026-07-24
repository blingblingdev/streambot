"""In-memory calibration helpers for reversible live visual validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


class LiveCalibrationError(RuntimeError):
    """Raised when no reliable visual discriminator can be calibrated."""


@dataclass(frozen=True)
class CalibratedRegion:
    """One discriminative region and its two ephemeral state templates."""

    x: int
    y: int
    width: int
    height: int
    closed_template: np.ndarray
    opened_template: np.ndarray
    closed_threshold: float
    opened_threshold: float
    separation: float


def _validate_frames(groups: Sequence[Sequence[np.ndarray]]) -> tuple[int, int]:
    frames = [frame for group in groups for frame in group]
    if not frames or any(len(group) < 2 for group in groups):
        raise LiveCalibrationError("each calibration state requires two frames")
    shape = frames[0].shape
    if (
        len(shape) != 3
        or shape[2] != 3
        or frames[0].dtype != np.uint8
        or any(frame.shape != shape or frame.dtype != np.uint8 for frame in frames)
    ):
        raise LiveCalibrationError("calibration frames must share one uint8 BGR shape")
    return shape[1], shape[0]


def _median_template(frames: Sequence[np.ndarray]) -> np.ndarray:
    return np.median(np.stack(frames), axis=0).astype(np.uint8)


def _distance(left: np.ndarray, right: np.ndarray) -> float:
    return float(np.abs(left.astype(np.int16) - right.astype(np.int16)).mean())


def calibrate_reversible_region(
    closed_before: Sequence[np.ndarray],
    opened: Sequence[np.ndarray],
    closed_after: Sequence[np.ndarray],
    *,
    tile_size: int = 120,
    minimum_separation: float = 4.0,
) -> CalibratedRegion:
    """Select a tile whose calibrated open and closed states are separable."""

    width, height = _validate_frames((closed_before, opened, closed_after))
    if tile_size < 16:
        raise LiveCalibrationError("tile size is too small")
    closed_frames = tuple(closed_before) + tuple(closed_after)
    best: CalibratedRegion | None = None

    for y in range(0, height, tile_size):
        for x in range(0, width, tile_size):
            right = min(width, x + tile_size)
            bottom = min(height, y + tile_size)
            if right - x < 16 or bottom - y < 16:
                continue
            closed_tiles = [frame[y:bottom, x:right] for frame in closed_frames]
            opened_tiles = [frame[y:bottom, x:right] for frame in opened]
            closed_template = _median_template(closed_tiles)
            opened_template = _median_template(opened_tiles)
            closed_variation = max(
                _distance(tile, closed_template) for tile in closed_tiles
            )
            opened_variation = max(
                _distance(tile, opened_template) for tile in opened_tiles
            )
            cross_distance = _distance(closed_template, opened_template)
            separation = cross_distance - max(closed_variation, opened_variation)
            if best is not None and separation <= best.separation:
                continue
            closed_boundary = (closed_variation + cross_distance) / 2.0
            opened_boundary = (opened_variation + cross_distance) / 2.0
            best = CalibratedRegion(
                x=x,
                y=y,
                width=right - x,
                height=bottom - y,
                closed_template=closed_template,
                opened_template=opened_template,
                closed_threshold=1.0 - closed_boundary / 255.0,
                opened_threshold=1.0 - opened_boundary / 255.0,
                separation=separation,
            )

    if best is None or best.separation < minimum_separation:
        raise LiveCalibrationError("no reliable reversible visual region was found")
    return best
