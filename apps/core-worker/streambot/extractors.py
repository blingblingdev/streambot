"""Declarative frame-based candidate extractors for the control surface.

These let a manifest locate clickable points from the frame itself (data),
rather than the target supplying points through context. `color-blob` finds
connected regions of a target color inside a declared region and returns their
centroids in stream coordinates.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Mapping

import numpy as np


def _region_view(frame: np.ndarray, params: Mapping[str, Any]) -> tuple[int, int, np.ndarray]:
    x, y, w, h = (int(v) for v in params["region"])
    return x, y, frame[y : y + h, x : x + w]


def _color_mask(pixels: np.ndarray, bgr, tolerance: int) -> np.ndarray:
    target = np.asarray(bgr, dtype=np.int16)
    difference = np.abs(pixels.astype(np.int16) - target)
    return np.all(difference <= tolerance, axis=-1)


def _components(mask: np.ndarray) -> list[tuple[int, float, float]]:
    """Return [(area, centroid_y, centroid_x)] for 4-connected mask components."""

    try:
        import cv2
    except Exception:
        cv2 = None
    if cv2 is not None:
        count, _labels, stats, centroids = cv2.connectedComponentsWithStats(
            mask.astype(np.uint8), connectivity=4
        )
        return [
            (int(stats[i, cv2.CC_STAT_AREA]), float(centroids[i][1]), float(centroids[i][0]))
            for i in range(1, count)
        ]
    # Pure-numpy fallback for environments without OpenCV (bounded regions).
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    out: list[tuple[int, float, float]] = []
    for sy in range(height):
        for sx in range(width):
            if mask[sy, sx] and not visited[sy, sx]:
                queue = deque([(sy, sx)])
                visited[sy, sx] = True
                sum_y = 0
                sum_x = 0
                area = 0
                while queue:
                    cy, cx = queue.popleft()
                    sum_y += cy
                    sum_x += cx
                    area += 1
                    for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                        if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not visited[ny, nx]:
                            visited[ny, nx] = True
                            queue.append((ny, nx))
                out.append((area, sum_y / area, sum_x / area))
    return out


def color_blob(
    frame: Any, params: Mapping[str, Any], context: Mapping[str, Any]
) -> tuple[tuple[int, int, int, float], ...]:
    """One indexed control per connected blob of the target color in the region."""

    origin_x, origin_y, region = _region_view(frame, params)
    mask = _color_mask(region, [int(c) for c in params["bgr"]], int(params.get("tolerance", 0)))
    minimum_area = int(params.get("min_area", 1))
    blobs = [blob for blob in _components(mask) if blob[0] >= minimum_area]

    order = params.get("order", "left-right")
    if order == "area-desc":
        blobs.sort(key=lambda b: (-b[0], b[2], b[1]))
    elif order == "top-bottom":
        blobs.sort(key=lambda b: (b[1], b[2]))
    else:  # left-right (default)
        blobs.sort(key=lambda b: (b[2], b[1]))

    maximum = params.get("max_candidates")
    if maximum is not None:
        blobs = blobs[: int(maximum)]

    confidence = float(params.get("confidence", 1.0))
    return tuple(
        (index, int(round(origin_x + cx)), int(round(origin_y + cy)), confidence)
        for index, (_area, cy, cx) in enumerate(blobs)
    )


FRAME_EXTRACTORS = {"color-blob": color_blob}
