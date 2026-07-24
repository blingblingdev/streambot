"""Claude-backed scene resolver for the novel-scene fallback.

This adapter is the only component that may send a frame outside the process,
and only when the operator explicitly wires it into `NovelSceneFallback` with
`enabled=True`. It asks a vision-capable Claude model to classify one unknown
frame and propose a declarative scene-manifest fragment; the fragment is then
validated by the same executed JSON Schema as authored scenes before it is
cached or used. Credentials resolve from the standard Anthropic SDK
environment; nothing is read from or written to the repository.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any, Mapping

import numpy as np

DEFAULT_MODEL = "claude-opus-4-8"

_SYSTEM_PROMPT = """You classify one frame from a streamed application and propose a declarative
scene entry for an automation manifest. Respond with a single JSON object and
nothing else:

{
  "scene_id": "<lowercase-kebab-case id describing the screen>",
  "scene": {
    "detect": {"operator": "all", "predicates": [ ... ]},
    "controls": [ {"id": ..., "action_kind": "click", "extractor": {...}} ],
    "recommend": {"rule": "none"}
  }
}

Predicate kinds you may use (choose robust, simple evidence):
- {"kind": "color", "region": [x, y, w, h], "bgr": [b, g, r], "tolerance": t,
   "minimum_fraction": f} — a region dominated by a color.
- {"kind": "pixel", "region": [x, y, w, h], "x": px, "y": py,
   "bgr": [b, g, r], "tolerance": t} — one distinctive pixel inside a region.
Extractor kinds you may use:
- {"kind": "fixed-point", "point": [x, y]} — one clickable point.
- {"kind": "color-blob", "region": [x, y, w, h], "bgr": [b, g, r],
   "tolerance": t, "min_area": a} — one control per colored blob.
All coordinates are absolute pixels in the frame's resolution. Regions
must stay inside the image. Prefer two or three predicates that together
uniquely identify this screen. If the frame shows no interactive
controls, return an empty controls array. If you cannot classify the screen
at all, respond with exactly {"scene_id": null}.
"""


class ClaudeSceneResolver:
    """Resolve unknown frames through the Claude API (explicit opt-in only)."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        client: Any | None = None,
        max_tokens: int = 4096,
        timeout_seconds: float = 60.0,
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(timeout=timeout_seconds)
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    @staticmethod
    def _encode_png(frame: np.ndarray) -> str:
        from PIL import Image

        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("resolver requires a uint8 BGR frame")
        buffer = io.BytesIO()
        Image.fromarray(frame[:, :, ::-1]).save(buffer, format="PNG")
        return base64.standard_b64encode(buffer.getvalue()).decode("utf-8")

    def resolve(self, frame: np.ndarray) -> Mapping[str, Any] | None:
        height, width = frame.shape[0], frame.shape[1]
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            thinking={"type": "adaptive"},
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": self._encode_png(frame),
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                f"The frame is {width}x{height} pixels. "
                                "Classify it and answer with the JSON object only."
                            ),
                        },
                    ],
                }
            ],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            return None
        text = next(
            (
                block.text
                for block in response.content
                if getattr(block, "type", None) == "text"
            ),
            "",
        )
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            payload = json.loads(text[start:end])
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not payload.get("scene_id"):
            return None
        if not isinstance(payload.get("scene"), dict):
            return None
        # The fallback validates the fragment against the executed manifest
        # schema before caching; this adapter only shapes the envelope.
        return {"scene_id": str(payload["scene_id"]), "scene": payload["scene"]}


__all__ = ["ClaudeSceneResolver", "DEFAULT_MODEL"]
