"""Bounded, idempotent Moonlight keyboard and mouse input."""

from __future__ import annotations

import math
import random
import time
from collections import deque
from pathlib import Path
from threading import RLock
from typing import Callable, Protocol

import moonlight_python
from cffi import FFI

from .config import InputActionSettings, SafetySettings, StreamSettings


BUTTON_ACTION_PRESS = 0x07
BUTTON_ACTION_RELEASE = 0x08
KEY_ACTION_DOWN = 0x03
KEY_ACTION_UP = 0x04

BUTTONS = {"left": 0x01, "middle": 0x02, "right": 0x03, "x1": 0x04, "x2": 0x05}
MODIFIERS = {"shift": 0x01, "ctrl": 0x02, "alt": 0x04, "meta": 0x08}

# Windows virtual key codes for the characters a URL or a file name needs.
# Typing exists because some work cannot be done by clicking: a guide that
# has to be fetched lives at an address, and an address has to be typed.
_TYPE_KEYS: dict[str, tuple[int, bool]] = {
    **{chr(c): (c - 32, False) for c in range(ord("a"), ord("z") + 1)},
    **{chr(c): (c, True) for c in range(ord("A"), ord("Z") + 1)},
    **{d: (0x30 + int(d), False) for d in "0123456789"},
    " ": (0x20, False), "-": (0xBD, False), "=": (0xBB, False),
    ".": (0xBE, False), "/": (0xBF, False), ";": (0xBA, False),
    "'": (0xDE, False), ",": (0xBC, False), "[": (0xDB, False),
    "]": (0xDD, False), "\\": (0xDC, False), "`": (0xC0, False),
    ":": (0xBA, True), "?": (0xBF, True), "_": (0xBD, True),
    "+": (0xBB, True), "&": (0x37, True), "%": (0x35, True),
    "#": (0x33, True), "@": (0x32, True), "!": (0x31, True),
    "~": (0xC0, True), "(": (0x39, True), ")": (0x30, True),
}


INPUT_CDEF = """
int LiSendMouseMoveEvent(short deltaX, short deltaY);
int LiSendMousePositionEvent(short x, short y, short referenceWidth, short referenceHeight);
int LiSendMouseButtonEvent(char action, int button);
int LiSendKeyboardEvent(short keyCode, char keyAction, char modifiers);
int LiSendScrollEvent(signed char scrollClicks);
"""


class InputError(RuntimeError):
    """Base error for bounded input execution."""


class InputRateLimitError(InputError):
    """Raised before input when the configured action rate is exceeded."""


class InputTransportError(InputError):
    """Raised when the Moonlight input transport rejects an event."""


class InputCleanupError(InputError):
    """Raised when held input could not be released after bounded retries."""


class InputTransport(Protocol):
    """Minimal transport required by the safe input driver."""

    @property
    def is_connected(self) -> bool: ...

    def mouse_move(self, dx: int, dy: int) -> int: ...
    def mouse_position(self, x: int, y: int, width: int, height: int) -> int: ...
    def mouse_button(self, action: int, button: int) -> int: ...
    def keyboard(self, key_code: int, action: int, modifiers: int) -> int: ...
    def scroll(self, clicks: int) -> int: ...


class MoonlightCffiTransport:
    """ABI adapter for input functions exported by moonlight-common-c."""

    def __init__(self, connected: Callable[[], bool]) -> None:
        self._connected = connected
        ffi = FFI()
        ffi.cdef(INPUT_CDEF)
        library_path = (
            Path(moonlight_python.__file__).resolve().parent
            / "libmoonlight-common-c.dylib"
        )
        self._lib = ffi.dlopen(str(library_path))

    @property
    def is_connected(self) -> bool:
        return bool(self._connected())

    def mouse_move(self, dx: int, dy: int) -> int:
        return int(self._lib.LiSendMouseMoveEvent(dx, dy))

    def mouse_position(self, x: int, y: int, width: int, height: int) -> int:
        return int(self._lib.LiSendMousePositionEvent(x, y, width, height))

    def mouse_button(self, action: int, button: int) -> int:
        return int(self._lib.LiSendMouseButtonEvent(bytes((action,)), button))

    def keyboard(self, key_code: int, action: int, modifiers: int) -> int:
        return int(
            self._lib.LiSendKeyboardEvent(
                key_code,
                bytes((action,)),
                bytes((modifiers,)),
            )
        )

    def scroll(self, clicks: int) -> int:
        return int(self._lib.LiSendScrollEvent(clicks))


class SafeInputDriver:
    """Execute named actions with dry-run, limits, idempotency, and cleanup."""

    def __init__(
        self,
        actions: tuple[InputActionSettings, ...],
        safety: SafetySettings,
        stream: StreamSettings,
        transport: InputTransport,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: "random.Random | None" = None,
    ) -> None:
        self._actions = {action.name: action for action in actions}
        self._safety = safety
        self._stream = stream
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()
        self._lock = RLock()
        self._recent_actions: deque[float] = deque()
        self._completed_keys: set[str] = set()
        self._pressed_keys: dict[tuple[int, int], None] = {}
        self._pressed_buttons: dict[int, None] = {}
        # Last commanded absolute pointer position, for natural glides.
        self._pointer: tuple[int, int] | None = None
        self.actions_completed = 0
        self.actions_suppressed = 0
        self.protocol_events_sent = 0

    def _check_rate(self) -> None:
        now = self._clock()
        cutoff = now - 60.0
        while self._recent_actions and self._recent_actions[0] <= cutoff:
            self._recent_actions.popleft()
        if len(self._recent_actions) >= self._safety.max_actions_per_minute:
            raise InputRateLimitError("input action rate limit exceeded")
        self._recent_actions.append(now)

    def _send(self, method: Callable[..., int], *args: int) -> None:
        result = method(*args)
        if result != 0:
            raise InputTransportError("Moonlight input event was rejected")
        self.protocol_events_sent += 1

    @staticmethod
    def _modifier_mask(action: InputActionSettings) -> int:
        return sum(MODIFIERS[modifier] for modifier in action.modifiers)

    def _dispatch(self, action: InputActionSettings) -> None:
        if action.kind == "mouse_move":
            if action.dx is None or action.dy is None:
                raise InputError("mouse move action is incomplete")
            self._send(self._transport.mouse_move, action.dx, action.dy)
        elif action.kind == "mouse_position":
            if action.x is None or action.y is None:
                raise InputError("mouse position action is incomplete")
            self._send(
                self._transport.mouse_position,
                action.x,
                action.y,
                self._stream.width,
                self._stream.height,
            )
        elif action.kind == "mouse_button":
            if action.button is None or action.operation is None:
                raise InputError("mouse button action is incomplete")
            button = BUTTONS[action.button]
            if action.operation in {"click", "press"}:
                self._pressed_buttons[button] = None
                self._send(self._transport.mouse_button, BUTTON_ACTION_PRESS, button)
            if action.operation in {"click", "release"}:
                self._send(self._transport.mouse_button, BUTTON_ACTION_RELEASE, button)
                self._pressed_buttons.pop(button, None)
        elif action.kind == "scroll":
            if action.clicks is None:
                raise InputError("scroll action is incomplete")
            self._send(self._transport.scroll, action.clicks)
        elif action.kind == "key":
            if action.key_code is None or action.operation is None:
                raise InputError("key action is incomplete")
            modifiers = self._modifier_mask(action)
            key = (action.key_code, modifiers)
            if action.operation in {"tap", "down"}:
                self._pressed_keys[key] = None
                self._send(
                    self._transport.keyboard,
                    action.key_code,
                    KEY_ACTION_DOWN,
                    modifiers,
                )
            if action.operation in {"tap", "up"}:
                self._send(
                    self._transport.keyboard,
                    action.key_code,
                    KEY_ACTION_UP,
                    modifiers,
                )
                self._pressed_keys.pop(key, None)
        else:
            raise InputError("unsupported input action")

    def execute(self, action: str, idempotency_key: str) -> None:
        """Execute one configured action or suppress its completed stable key."""

        with self._lock:
            if not idempotency_key:
                raise InputError("idempotency key is required")
            if idempotency_key in self._completed_keys:
                self.actions_suppressed += 1
                return
            configured = self._actions.get(action)
            if configured is None:
                raise InputError("input action is unknown")
            self._check_rate()
            if self._safety.dry_run:
                self._completed_keys.add(idempotency_key)
                self.actions_completed += 1
                return
            if not self._transport.is_connected:
                raise InputTransportError("Moonlight input transport is disconnected")
            try:
                self._dispatch(configured)
            except BaseException as error:
                try:
                    self._release_all_locked()
                except InputCleanupError as cleanup_error:
                    error.add_note(str(cleanup_error))
                raise
            self._completed_keys.add(idempotency_key)
            self.actions_completed += 1


    def execute_text(self, text: str, idempotency_key: str) -> None:
        """Type a short string, one key at a time, under the same rails.

        Every character is a key down and up through the ordinary transport,
        so the per-minute limit, the dry-run switch and the held-key tracking
        all apply exactly as they do to a click. Characters with no mapping
        are skipped rather than guessed at — a wrong key in an address is
        worse than a missing one, because it goes somewhere.
        """

        with self._lock:
            if not idempotency_key:
                raise InputError("idempotency key is required")
            if idempotency_key in self._completed_keys:
                return
            if len(text) > 512:
                raise InputError("text is too long to type")
            # One rate check for the whole string: typing an address is one
            # action from the operator's point of view, and charging it per
            # character would trip the limit on a single URL.
            self._check_rate()
            try:
                for character in text:
                    mapping = _TYPE_KEYS.get(character)
                    if mapping is None:
                        continue
                    key_code, shifted = mapping
                    modifiers = MODIFIERS["shift"] if shifted else 0
                    if self._safety.dry_run:
                        continue
                    # Tracked like any tap: if the UP send fails, the key is
                    # in _pressed_keys and the cleanup below releases it —
                    # an untracked stuck key would corrupt every input after.
                    key = (key_code, modifiers)
                    self._pressed_keys[key] = None
                    self._send(
                        self._transport.keyboard, key_code, KEY_ACTION_DOWN, modifiers
                    )
                    self._send(
                        self._transport.keyboard, key_code, KEY_ACTION_UP, modifiers
                    )
                    self._pressed_keys.pop(key, None)
            except BaseException as error:
                try:
                    self._release_all_locked()
                except InputCleanupError as cleanup_error:
                    error.add_note(str(cleanup_error))
                raise
            self._completed_keys.add(idempotency_key)

    def execute_position(self, x: int, y: int, idempotency_key: str) -> None:
        """Execute one bounded dynamic absolute pointer action safely."""

        with self._lock:
            if not idempotency_key:
                raise InputError("idempotency key is required")
            if not isinstance(x, int) or not isinstance(y, int):
                raise InputError("pointer coordinates must be integers")
            if not (0 <= x < self._stream.width and 0 <= y < self._stream.height):
                raise InputError("pointer coordinates are outside stream bounds")
            if idempotency_key in self._completed_keys:
                self.actions_suppressed += 1
                return
            self._check_rate()
            if self._safety.dry_run:
                self._completed_keys.add(idempotency_key)
                self.actions_completed += 1
                return
            if not self._transport.is_connected:
                raise InputTransportError("Moonlight input transport is disconnected")
            self._send(
                self._transport.mouse_position,
                x,
                y,
                self._stream.width,
                self._stream.height,
            )
            self._pointer = (x, y)
            self._completed_keys.add(idempotency_key)
            self.actions_completed += 1

    def execute_scroll(self, clicks: int, idempotency_key: str) -> None:
        """Scroll the wheel, under the same rails as every other action.

        The transport has carried scroll since the beginning — the driver
        already knows the action kind — but nothing above it could ask for
        one, so a job could click and type and drag and not turn a wheel.
        Poly Bridge zooms on the wheel, and a level drawn at the zoom it
        happens to open at puts members below the game's minimum length.

        One rate check for the whole gesture: a zoom is one action from the
        operator's point of view, however many clicks it takes.
        """

        with self._lock:
            if not idempotency_key:
                raise InputError("idempotency key is required")
            if not isinstance(clicks, int):
                raise InputError("scroll clicks must be an integer")
            if not -32 <= clicks <= 32:
                raise InputError("scroll clicks are out of range")
            if idempotency_key in self._completed_keys:
                self.actions_suppressed += 1
                return
            self._check_rate()
            if self._safety.dry_run:
                self._completed_keys.add(idempotency_key)
                self.actions_completed += 1
                return
            if not self._transport.is_connected:
                raise InputTransportError("Moonlight input transport is disconnected")
            step = 1 if clicks > 0 else -1
            for _ in range(abs(clicks)):
                self._send(self._transport.scroll, step)
            self._completed_keys.add(idempotency_key)
            self.actions_completed += 1

    def _glide_path(
        self, start: tuple[int, int], end: tuple[int, int]
    ) -> list[tuple[int, int]]:
        """Eased cubic-Bezier path with a slight bow and per-point jitter.

        The bow (a control-point offset perpendicular to the travel line) and
        the ease-in-out timing make the motion read as a hand, not a jump.
        The last point is exactly `end` so the click lands precisely.
        """

        sx, sy = start
        ex, ey = end
        distance = math.hypot(ex - sx, ey - sy)
        if distance < 2:
            return [end]
        steps = int(min(28, max(6, distance / 34)))
        # Perpendicular unit vector for the bow.
        px, py = -(ey - sy) / distance, (ex - sx) / distance
        bow = self._rng.uniform(-0.12, 0.12) * distance
        cx = (sx + ex) / 2 + px * bow
        cy = (sy + ey) / 2 + py * bow
        path: list[tuple[int, int]] = []
        for index in range(1, steps + 1):
            t = index / steps
            eased = t * t * (3 - 2 * t)  # smoothstep ease-in-out
            omt = 1 - eased
            bx = omt * omt * sx + 2 * omt * eased * cx + eased * eased * ex
            by = omt * omt * sy + 2 * omt * eased * cy + eased * eased * ey
            if index < steps:
                jitter = min(2.0, distance * 0.01)
                bx += self._rng.uniform(-jitter, jitter)
                by += self._rng.uniform(-jitter, jitter)
            x = min(self._stream.width - 1, max(0, int(round(bx))))
            y = min(self._stream.height - 1, max(0, int(round(by))))
            path.append((x, y))
        path[-1] = end
        return path

    def execute_glide(
        self,
        x: int,
        y: int,
        idempotency_key: str,
        *,
        step_seconds: float = 0.016,
    ) -> None:
        """Move the pointer to (x, y) along a natural trajectory.

        Counts as ONE rate-limited action regardless of how many transport
        position events the path emits, so humanized motion does not exhaust
        the per-minute budget. Falls back to an instant set when the start
        position is unknown (first move of a session).
        """

        with self._lock:
            if not idempotency_key:
                raise InputError("idempotency key is required")
            if not isinstance(x, int) or not isinstance(y, int):
                raise InputError("pointer coordinates must be integers")
            if not (0 <= x < self._stream.width and 0 <= y < self._stream.height):
                raise InputError("pointer coordinates are outside stream bounds")
            if idempotency_key in self._completed_keys:
                self.actions_suppressed += 1
                return
            self._check_rate()
            if self._safety.dry_run:
                self._pointer = (x, y)
                self._completed_keys.add(idempotency_key)
                self.actions_completed += 1
                return
            if not self._transport.is_connected:
                raise InputTransportError("Moonlight input transport is disconnected")
            start = self._pointer
            path = [(x, y)] if start is None else self._glide_path(start, (x, y))
            for index, (px, py) in enumerate(path):
                self._send(
                    self._transport.mouse_position,
                    px,
                    py,
                    self._stream.width,
                    self._stream.height,
                )
                self._pointer = (px, py)
                if index < len(path) - 1:
                    self._sleep(
                        max(0.0, step_seconds * self._rng.uniform(0.6, 1.4))
                    )
            self._completed_keys.add(idempotency_key)
            self.actions_completed += 1

    def execute_move(self, dx: int, dy: int, idempotency_key: str) -> None:
        """Execute one bounded dynamic RELATIVE pointer motion safely.

        Some scenes capture the mouse (panorama look-around) and ignore
        absolute position events entirely; the only way to steer their
        internal cursor is relative deltas (LiSendMouseMoveEvent).
        """

        with self._lock:
            if not idempotency_key:
                raise InputError("idempotency key is required")
            if not isinstance(dx, int) or not isinstance(dy, int):
                raise InputError("pointer deltas must be integers")
            if abs(dx) > self._stream.width or abs(dy) > self._stream.height:
                raise InputError("pointer deltas exceed stream bounds")
            if idempotency_key in self._completed_keys:
                self.actions_suppressed += 1
                return
            self._check_rate()
            if self._safety.dry_run:
                self._completed_keys.add(idempotency_key)
                self.actions_completed += 1
                return
            if not self._transport.is_connected:
                raise InputTransportError("Moonlight input transport is disconnected")
            self._send(self._transport.mouse_move, dx, dy)
            self._completed_keys.add(idempotency_key)
            self.actions_completed += 1

    def _release_with_retries(self, method: Callable[..., int], *args: int) -> bool:
        for _attempt in range(3):
            try:
                self._send(method, *args)
                return True
            except Exception:
                continue
        return False

    def _release_all_locked(self) -> None:
        if self._safety.dry_run:
            self._pressed_keys.clear()
            self._pressed_buttons.clear()
            return
        failures = 0
        for key_code, modifiers in tuple(reversed(self._pressed_keys)):
            if self._release_with_retries(
                self._transport.keyboard, key_code, KEY_ACTION_UP, modifiers
            ):
                self._pressed_keys.pop((key_code, modifiers), None)
            else:
                failures += 1
        for button in tuple(reversed(self._pressed_buttons)):
            if self._release_with_retries(
                self._transport.mouse_button, BUTTON_ACTION_RELEASE, button
            ):
                self._pressed_buttons.pop(button, None)
            else:
                failures += 1
        if failures:
            raise InputCleanupError("held input release failed after bounded retries")

    def release_all(self) -> None:
        """Release every locally tracked key and button with bounded retries."""

        with self._lock:
            self._release_all_locked()

    def close(self) -> None:
        self.release_all()

    def __enter__(self) -> "SafeInputDriver":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def held_input_count(self) -> int:
        with self._lock:
            return len(self._pressed_keys) + len(self._pressed_buttons)
