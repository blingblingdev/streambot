"""Offline tests for the Claude scene resolver adapter (fake client only)."""

from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace

import numpy as np

from streambot.vision_resolver import ClaudeSceneResolver


class FakeMessages:
    def __init__(self, response) -> None:
        self.response = response
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response) -> None:
        self.messages = FakeMessages(response)


def _text_response(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
    )


def _frame() -> np.ndarray:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[0:50, 0:50] = (0, 0, 255)
    return frame


class ClaudeSceneResolverTests(unittest.TestCase):
    def test_resolves_json_fragment(self) -> None:
        fragment = {
            "scene_id": "red-banner",
            "scene": {
                "detect": {
                    "predicates": [
                        {
                            "kind": "color",
                            "region": [0, 0, 50, 50],
                            "bgr": [0, 0, 255],
                            "tolerance": 10,
                            "minimum_fraction": 0.5,
                        }
                    ]
                }
            },
        }
        client = FakeClient(_text_response(json.dumps(fragment)))
        resolver = ClaudeSceneResolver(client=client)
        result = resolver.resolve(_frame())
        self.assertEqual(result["scene_id"], "red-banner")
        self.assertIn("detect", result["scene"])

        request = client.messages.requests[0]
        self.assertEqual(request["model"], "claude-opus-4-8")
        image_block = request["messages"][0]["content"][0]
        self.assertEqual(image_block["type"], "image")
        # The payload is a valid base64 PNG built in memory.
        raw = base64.standard_b64decode(image_block["source"]["data"])
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")

    def test_unclassifiable_returns_none(self) -> None:
        resolver = ClaudeSceneResolver(
            client=FakeClient(_text_response('{"scene_id": null}'))
        )
        self.assertIsNone(resolver.resolve(_frame()))

    def test_non_json_returns_none(self) -> None:
        resolver = ClaudeSceneResolver(
            client=FakeClient(_text_response("I cannot help with that"))
        )
        self.assertIsNone(resolver.resolve(_frame()))

    def test_refusal_returns_none(self) -> None:
        resolver = ClaudeSceneResolver(
            client=FakeClient(_text_response("", stop_reason="refusal"))
        )
        self.assertIsNone(resolver.resolve(_frame()))


if __name__ == "__main__":
    unittest.main()
