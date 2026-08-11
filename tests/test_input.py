"""Tests for bounded, idempotent Moonlight input execution."""

from __future__ import annotations

import random
import unittest

from streambot.config import (
    AutomationProfile,
    ConfigurationError,
    InputActionSettings,
    SafetySettings,
    StreamSettings,
)
from streambot.input import (
    BUTTON_ACTION_PRESS,
    BUTTON_ACTION_RELEASE,
    KEY_ACTION_DOWN,
    KEY_ACTION_UP,
    InputCleanupError,
    InputError,
    InputRateLimitError,
    InputTransportError,
    MoonlightCffiTransport,
    SafeInputDriver,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeTransport:
    def __init__(self) -> None:
        self.is_connected = True
        self.events: list[tuple[object, ...]] = []
        self.failures: dict[tuple[object, ...], int] = {}

    def _record(self, event: tuple[object, ...]) -> int:
        self.events.append(event)
        remaining = self.failures.get(event, 0)
        if remaining:
            self.failures[event] = remaining - 1
            return -1
        return 0

    def mouse_move(self, dx: int, dy: int) -> int:
        return self._record(("mouse_move", dx, dy))

    def mouse_position(self, x: int, y: int, width: int, height: int) -> int:
        return self._record(("mouse_position", x, y, width, height))

    def mouse_button(self, action: int, button: int) -> int:
        return self._record(("mouse_button", action, button))

    def keyboard(self, key_code: int, action: int, modifiers: int) -> int:
        return self._record(("keyboard", key_code, action, modifiers))

    def scroll(self, clicks: int) -> int:
        return self._record(("scroll", clicks))


def action(name: str, kind: str, **values: object) -> InputActionSettings:
    return InputActionSettings(name=name, kind=kind, **values)


def driver(
    actions: tuple[InputActionSettings, ...],
    transport: FakeTransport,
    *,
    dry_run: bool = False,
    limit: int = 30,
    clock: FakeClock | None = None,
    rng: "random.Random | None" = None,
) -> SafeInputDriver:
    return SafeInputDriver(
        actions,
        SafetySettings(dry_run=dry_run, max_actions_per_minute=limit),
        StreamSettings(width=1280, height=720),
        transport,
        clock=clock or FakeClock(),
        sleep=lambda _s: None,
        rng=rng if rng is not None else random.Random(1234),
    )


class InputConfigurationTests(unittest.TestCase):
    def test_all_supported_action_types_load(self) -> None:
        profile = AutomationProfile.from_mapping(
            {
                "name": "input",
                "actions": [
                    {"name": "move", "type": "mouse_move", "dx": -4, "dy": 7},
                    {"name": "point", "type": "mouse_position", "x": 12, "y": 34},
                    {"name": "click", "type": "mouse_button", "button": "left"},
                    {"name": "wheel", "type": "scroll", "clicks": -2},
                    {
                        "name": "shortcut",
                        "type": "key",
                        "key_code": 65,
                        "modifiers": ["ctrl", "shift"],
                    },
                ],
            }
        )

        self.assertEqual(
            [item.kind for item in profile.actions],
            ["mouse_move", "mouse_position", "mouse_button", "scroll", "key"],
        )
        self.assertEqual(profile.actions[2].operation, "click")
        self.assertEqual(profile.actions[4].operation, "tap")

    def test_invalid_action_shapes_fail_closed(self) -> None:
        invalid_actions = [
            {"name": "zero", "type": "mouse_move", "dx": 0, "dy": 0},
            {"name": "zero", "type": "scroll", "clicks": 0},
            {"name": "bad", "type": "mouse_button", "button": "extra"},
            {"name": "bad", "type": "key", "key_code": 0},
            {
                "name": "bad",
                "type": "key",
                "key_code": 65,
                "modifiers": ["ctrl", "ctrl"],
            },
        ]
        for invalid in invalid_actions:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ConfigurationError):
                    AutomationProfile.from_mapping(
                        {"name": "invalid", "actions": [invalid]}
                    )

    def test_duplicate_names_and_out_of_bounds_position_are_rejected(self) -> None:
        duplicate = {"name": "same", "type": "scroll", "clicks": 1}
        with self.assertRaisesRegex(ConfigurationError, "duplicate"):
            AutomationProfile.from_mapping(
                {"name": "invalid", "actions": [duplicate, duplicate]}
            )
        with self.assertRaisesRegex(ConfigurationError, "stream bounds"):
            AutomationProfile.from_mapping(
                {
                    "name": "invalid",
                    "stream": {"width": 320, "height": 240},
                    "actions": [
                        {"name": "point", "type": "mouse_position", "x": 320, "y": 0}
                    ],
                }
            )

    def test_workflow_cannot_reference_unknown_action(self) -> None:
        profile = {
            "name": "invalid",
            "perception": {
                "regions": [{"name": "r", "x": 0, "y": 0, "width": 1, "height": 1}],
                "predicates": [
                    {"name": "p", "type": "pixel", "region": "r", "x": 0, "y": 0, "bgr": [0, 0, 0]}
                ],
                "signals": [{"name": "s", "operator": "all", "predicates": ["p"]}],
            },
            "workflow": {
                "initial_state": "run",
                "states": [
                    {
                        "name": "run",
                        "timeout_seconds": 1,
                        "timeout_state": "failed",
                        "transitions": [
                            {
                                "name": "go",
                                "signal": "s",
                                "target": "done",
                                "actions": ["missing"],
                                "idempotency_key": "go-once",
                                "failure_state": "failed",
                            }
                        ],
                    },
                    {"name": "done", "terminal": "success"},
                    {"name": "failed", "terminal": "failure"},
                ],
            },
        }
        with self.assertRaisesRegex(ConfigurationError, "unknown action"):
            AutomationProfile.from_mapping(profile)


class SafeInputDriverTests(unittest.TestCase):
    def test_dry_run_and_idempotency_do_not_touch_transport(self) -> None:
        transport = FakeTransport()
        subject = driver(
            (action("move", "mouse_move", dx=1, dy=2),),
            transport,
            dry_run=True,
        )

        subject.execute("move", "stable")
        subject.execute("move", "stable")

        self.assertEqual(transport.events, [])
        self.assertEqual(subject.actions_completed, 1)
        self.assertEqual(subject.actions_suppressed, 1)

    def test_execute_scroll_sends_one_event_per_click(self) -> None:
        """A zoom is one action however many clicks it takes."""

        transport = FakeTransport()
        subject = driver((action("move", "mouse_move", dx=1, dy=1),), transport)

        subject.execute_scroll(3, "zoom-in")

        self.assertEqual(
            transport.events, [("scroll", 1), ("scroll", 1), ("scroll", 1)]
        )
        self.assertEqual(subject.actions_completed, 1)

    def test_execute_scroll_goes_down_and_is_idempotent(self) -> None:
        transport = FakeTransport()
        subject = driver((action("move", "mouse_move", dx=1, dy=1),), transport)

        subject.execute_scroll(-2, "zoom-out")
        subject.execute_scroll(-2, "zoom-out")

        self.assertEqual(transport.events, [("scroll", -1), ("scroll", -1)])
        self.assertEqual(subject.actions_suppressed, 1)

    def test_execute_scroll_rejects_absurd_distances(self) -> None:
        """Out of range fails closed rather than spinning the wheel forever."""

        subject = driver((action("move", "mouse_move", dx=1, dy=1),), FakeTransport())

        with self.assertRaises(InputError):
            subject.execute_scroll(500, "far-too-far")
        with self.assertRaises(InputError):
            subject.execute_scroll("3", "not-a-number")

    def test_relative_absolute_scroll_and_click_mapping(self) -> None:
        transport = FakeTransport()
        subject = driver(
            (
                action("move", "mouse_move", dx=-3, dy=9),
                action("point", "mouse_position", x=100, y=200),
                action("wheel", "scroll", clicks=-2),
                action("click", "mouse_button", button="right", operation="click"),
            ),
            transport,
        )

        for index, name in enumerate(("move", "point", "wheel", "click")):
            subject.execute(name, f"key-{index}")

        self.assertEqual(
            transport.events,
            [
                ("mouse_move", -3, 9),
                ("mouse_position", 100, 200, 1280, 720),
                ("scroll", -2),
                ("mouse_button", BUTTON_ACTION_PRESS, 3),
                ("mouse_button", BUTTON_ACTION_RELEASE, 3),
            ],
        )
        self.assertEqual(subject.held_input_count, 0)

    def test_dynamic_position_is_bounded_idempotent_and_rate_limited(self) -> None:
        transport = FakeTransport()
        subject = driver((), transport, limit=1)

        subject.execute_position(100, 200, "position-1")
        subject.execute_position(100, 200, "position-1")

        self.assertEqual(
            transport.events,
            [("mouse_position", 100, 200, 1280, 720)],
        )
        self.assertEqual(subject.actions_suppressed, 1)
        with self.assertRaises(InputRateLimitError):
            subject.execute_position(101, 200, "position-2")
        with self.assertRaisesRegex(InputError, "bounds"):
            subject.execute_position(1280, 200, "outside")

    def test_key_tap_maps_modifiers_and_releases(self) -> None:
        transport = FakeTransport()
        subject = driver(
            (
                action(
                    "shortcut",
                    "key",
                    key_code=65,
                    operation="tap",
                    modifiers=("ctrl", "shift"),
                ),
            ),
            transport,
        )

        subject.execute("shortcut", "shortcut-once")

        self.assertEqual(
            transport.events,
            [
                ("keyboard", 65, KEY_ACTION_DOWN, 3),
                ("keyboard", 65, KEY_ACTION_UP, 3),
            ],
        )
        self.assertEqual(subject.held_input_count, 0)

    def test_explicit_held_inputs_are_released_in_reverse_groups(self) -> None:
        transport = FakeTransport()
        subject = driver(
            (
                action("key-down", "key", key_code=66, operation="down"),
                action("button-down", "mouse_button", button="left", operation="press"),
            ),
            transport,
        )
        subject.execute("key-down", "key")
        subject.execute("button-down", "button")

        self.assertEqual(subject.held_input_count, 2)
        subject.release_all()

        self.assertEqual(transport.events[-2:], [
            ("keyboard", 66, KEY_ACTION_UP, 0),
            ("mouse_button", BUTTON_ACTION_RELEASE, 1),
        ])
        self.assertEqual(subject.held_input_count, 0)

    def test_rate_limit_uses_sliding_minute_window(self) -> None:
        clock = FakeClock()
        transport = FakeTransport()
        subject = driver(
            (action("wheel", "scroll", clicks=1),),
            transport,
            limit=2,
            clock=clock,
        )
        subject.execute("wheel", "one")
        subject.execute("wheel", "two")
        with self.assertRaises(InputRateLimitError):
            subject.execute("wheel", "three")
        clock.now = 60.001
        subject.execute("wheel", "three")

        self.assertEqual(subject.actions_completed, 3)

    def test_disconnected_transport_fails_without_protocol_event(self) -> None:
        transport = FakeTransport()
        transport.is_connected = False
        subject = driver((action("wheel", "scroll", clicks=1),), transport)

        with self.assertRaisesRegex(InputTransportError, "disconnected"):
            subject.execute("wheel", "one")

        self.assertEqual(transport.events, [])

    def test_partial_action_failure_releases_held_input(self) -> None:
        transport = FakeTransport()
        release = ("mouse_button", BUTTON_ACTION_RELEASE, 1)
        transport.failures[release] = 1
        subject = driver(
            (action("click", "mouse_button", button="left", operation="click"),),
            transport,
        )

        with self.assertRaises(InputTransportError):
            subject.execute("click", "one")

        self.assertEqual(transport.events.count(release), 2)
        self.assertEqual(subject.held_input_count, 0)

    def test_typed_key_whose_up_fails_is_released_not_left_held(self) -> None:
        transport = FakeTransport()
        up = ("keyboard", 65, KEY_ACTION_UP, 0)
        transport.failures[up] = 1
        subject = driver((), transport)

        with self.assertRaises(InputTransportError):
            subject.execute_text("a", "type-1")

        # The failed UP is retried by cleanup: the key must not stay down on
        # the host, where it would corrupt every input after this one.
        self.assertEqual(transport.events.count(up), 2)
        self.assertEqual(subject.held_input_count, 0)
        # The failure did not consume the idempotency key: a retry types.
        subject.execute_text("a", "type-1")
        self.assertEqual(
            transport.events.count(("keyboard", 65, KEY_ACTION_DOWN, 0)), 2
        )

    def test_typed_key_whose_down_fails_gets_a_compensating_release(self) -> None:
        transport = FakeTransport()
        down = ("keyboard", 65, KEY_ACTION_DOWN, 0)
        up = ("keyboard", 65, KEY_ACTION_UP, 0)
        transport.failures[down] = 1
        subject = driver((), transport)

        with self.assertRaises(InputTransportError):
            subject.execute_text("a", "type-1")

        self.assertEqual(transport.events, [down, up])
        self.assertEqual(subject.held_input_count, 0)

    def test_rejected_press_still_gets_a_compensating_release(self) -> None:
        transport = FakeTransport()
        press = ("mouse_button", BUTTON_ACTION_PRESS, 1)
        release = ("mouse_button", BUTTON_ACTION_RELEASE, 1)
        transport.failures[press] = 1
        subject = driver(
            (action("press", "mouse_button", button="left", operation="press"),),
            transport,
        )

        with self.assertRaises(InputTransportError):
            subject.execute("press", "one")

        self.assertEqual(transport.events, [press, release])
        self.assertEqual(subject.held_input_count, 0)

    def test_persistent_cleanup_failure_remains_visible_and_retryable(self) -> None:
        transport = FakeTransport()
        release = ("mouse_button", BUTTON_ACTION_RELEASE, 1)
        transport.failures[release] = 10
        subject = driver(
            (action("press", "mouse_button", button="left", operation="press"),),
            transport,
        )
        subject.execute("press", "one")

        with self.assertRaises(InputCleanupError):
            subject.release_all()

        self.assertEqual(subject.held_input_count, 1)
        transport.failures[release] = 0
        subject.release_all()
        self.assertEqual(subject.held_input_count, 0)

    def test_unknown_action_and_empty_idempotency_key_fail_closed(self) -> None:
        transport = FakeTransport()
        subject = driver((action("wheel", "scroll", clicks=1),), transport)
        with self.assertRaisesRegex(Exception, "idempotency"):
            subject.execute("wheel", "")
        with self.assertRaisesRegex(Exception, "unknown"):
            subject.execute("missing", "key")


class NativeTransportTests(unittest.TestCase):
    def test_packaged_library_exposes_input_symbols_without_sending_input(self) -> None:
        transport = MoonlightCffiTransport(lambda: False)

        self.assertFalse(transport.is_connected)
        self.assertTrue(callable(transport.mouse_move))
        self.assertTrue(callable(transport.mouse_position))
        self.assertTrue(callable(transport.mouse_button))
        self.assertTrue(callable(transport.keyboard))
        self.assertTrue(callable(transport.scroll))

    def test_native_char_arguments_are_encoded_as_single_bytes(self) -> None:
        class FakeLibrary:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def LiSendMouseButtonEvent(self, action: bytes, button: int) -> int:
                self.calls.append(("button", action, button))
                return 0

            def LiSendKeyboardEvent(
                self, key_code: int, action: bytes, modifiers: bytes
            ) -> int:
                self.calls.append(("key", key_code, action, modifiers))
                return 0

        transport = MoonlightCffiTransport(lambda: False)
        library = FakeLibrary()
        transport._lib = library

        transport.mouse_button(BUTTON_ACTION_PRESS, 1)
        transport.keyboard(65, KEY_ACTION_DOWN, 3)

        self.assertEqual(
            library.calls,
            [
                ("button", b"\x07", 1),
                ("key", 65, b"\x03", b"\x03"),
            ],
        )


class GlideTests(unittest.TestCase):
    def _positions(self, transport: FakeTransport) -> list[tuple[int, int]]:
        return [
            (e[1], e[2]) for e in transport.events if e[0] == "mouse_position"
        ]

    def test_first_glide_is_instant_without_known_start(self) -> None:
        transport = FakeTransport()
        d = driver((), transport)
        d.execute_glide(600, 400, "g1")
        self.assertEqual(self._positions(transport), [(600, 400)])

    def test_glide_traces_a_multi_point_path_ending_exactly(self) -> None:
        transport = FakeTransport()
        d = driver((), transport)
        d.execute_position(100, 100, "seed")
        transport.events.clear()
        d.execute_glide(700, 500, "g2")
        pts = self._positions(transport)
        self.assertGreater(len(pts), 5, "expected an interpolated path")
        self.assertEqual(pts[-1], (700, 500), "must land exactly on target")
        # Path is not the straight line: at least one point is off the
        # segment (the bow) by more than a pixel.
        def off_line(p):
            # distance from p to the start-end segment
            (x0, y0), (x1, y1) = (100, 100), (700, 500)
            num = abs((y1 - y0) * p[0] - (x1 - x0) * p[1] + x1 * y0 - y1 * x0)
            den = ((y1 - y0) ** 2 + (x1 - x0) ** 2) ** 0.5
            return num / den
        self.assertTrue(any(off_line(p) > 1.0 for p in pts[:-1]))

    def test_glide_counts_as_one_rate_limited_action(self) -> None:
        transport = FakeTransport()
        d = driver((), transport, limit=3)
        d.execute_position(0, 0, "seed")
        # Three glides fit the limit-of-3 (seed already used one slot -> 2 left
        # plus this is a fresh minute window in the fake clock).
        d.execute_glide(500, 500, "a")
        d.execute_glide(10, 10, "b")
        # The many intermediate events did not each consume the budget.
        self.assertEqual(d.actions_completed, 3)  # seed + a + b

    def test_glide_stays_in_bounds(self) -> None:
        transport = FakeTransport()
        d = driver((), transport)
        d.execute_position(5, 5, "seed")
        d.execute_glide(1279, 719, "corner")
        for x, y in self._positions(transport):
            self.assertTrue(0 <= x < 1280 and 0 <= y < 720)

    def test_glide_dry_run_emits_nothing_but_tracks_pointer(self) -> None:
        transport = FakeTransport()
        d = driver((), transport, dry_run=True)
        d.execute_glide(300, 300, "dry")
        self.assertEqual(self._positions(transport), [])
        self.assertEqual(d.actions_completed, 1)

    def test_glide_suppresses_repeated_key(self) -> None:
        transport = FakeTransport()
        d = driver((), transport)
        d.execute_position(0, 0, "seed")
        d.execute_glide(400, 400, "same")
        before = len(transport.events)
        d.execute_glide(400, 400, "same")
        self.assertEqual(len(transport.events), before)
        self.assertEqual(d.actions_suppressed, 1)


if __name__ == "__main__":
    unittest.main()
