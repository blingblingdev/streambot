"""Lifecycle and safety tests for latest-frame observation."""

from __future__ import annotations

import unittest
from contextlib import AbstractContextManager
from dataclasses import dataclass
from unittest.mock import patch

import numpy as np

from streambot.config import AutomationProfile, ObservationSettings
from streambot.connection import owner_only_umask
from streambot.models import WorkerState
from streambot.observation import (
    LatestFrameObserver,
    OutputRateLimiter,
    create_decoder,
    preserve_host_application_session,
)


class OutputRateLimiterTests(unittest.TestCase):
    def test_gate_admits_at_configured_interval(self) -> None:
        values = iter([0.0, 0.2, 0.5, 0.99, 1.0])
        limiter = OutputRateLimiter(2.0, clock=lambda: next(values))

        self.assertTrue(limiter.admit())
        self.assertFalse(limiter.admit())
        self.assertTrue(limiter.admit())
        self.assertFalse(limiter.admit())
        self.assertTrue(limiter.admit())


class FileCreationSafetyTests(unittest.TestCase):
    def test_owner_only_umask_is_applied_and_restored(self) -> None:
        with patch("streambot.connection.os.umask", return_value=0o022) as umask:
            with owner_only_umask():
                pass

        self.assertEqual(umask.call_args_list[0].args, (0o077,))
        self.assertEqual(umask.call_args_list[1].args, (0o022,))


class DecoderSelectionTests(unittest.TestCase):
    def test_hardware_failure_uses_software_fallback(self) -> None:
        real_context = __import__("av").CodecContext.create

        def context(codec: str, mode: str, hwaccel=None):
            if hwaccel is not None:
                raise RuntimeError("hardware unavailable")
            return real_context(codec, mode)

        with patch("streambot.observation.av.CodecContext.create", side_effect=context):
            selection = create_decoder(
                ObservationSettings(decoder="videotoolbox", software_fallback=True)
            )
        self.assertEqual(selection.backend, "software")
        self.assertTrue(selection.used_fallback)
        selection.decoder.close()

    def test_hardware_failure_is_raised_when_fallback_is_disabled(self) -> None:
        with patch(
            "streambot.observation.av.CodecContext.create",
            side_effect=RuntimeError("hardware unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "hardware unavailable"):
                create_decoder(
                    ObservationSettings(
                        decoder="videotoolbox", software_fallback=False
                    )
                )


class FakeMutableHttp:
    def __init__(self) -> None:
        self.launch_calls = 0
        self.quit_calls = 0

    def launch_app(self) -> None:
        self.launch_calls += 1

    def quit_app(self) -> None:
        self.quit_calls += 1


class HostSessionGuardTests(unittest.TestCase):
    def test_guard_rejects_mutation_and_restores_methods(self) -> None:
        http = FakeMutableHttp()

        with preserve_host_application_session(http):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                http.launch_app()
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                http.quit_app()

        http.launch_app()
        http.quit_app()
        self.assertEqual(http.launch_calls, 1)
        self.assertEqual(http.quit_calls, 1)

    def test_allow_launch_permits_launch_but_never_quit(self) -> None:
        http = FakeMutableHttp()

        with preserve_host_application_session(http, allow_launch=True):
            http.launch_app()
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                http.quit_app()

        self.assertEqual(http.launch_calls, 1)
        self.assertEqual(http.quit_calls, 0)


@dataclass
class FakeApp:
    name: str = "Desktop"
    id: int = 1


@dataclass
class FakeInfo:
    current_game: int


@dataclass
class FakeFrame:
    frame_number: int
    data: np.ndarray


class FakeHttp:
    def __init__(self, current_game: int = 1) -> None:
        self.current_game = current_game

    def get_app_list(self):
        return [FakeApp()]

    def get_server_info(self, use_https: bool):
        return object()

    def parse_server_info(self, value: object):
        return FakeInfo(self.current_game)


class FakeContext(AbstractContextManager):
    def __init__(
        self,
        value: object,
        events: list[str],
        name: str,
        enter_error: BaseException | None = None,
        exit_error: BaseException | None = None,
    ) -> None:
        self.value = value
        self.events = events
        self.name = name
        self.enter_error = enter_error
        self.exit_error = exit_error

    def __enter__(self):
        self.events.append(f"{self.name}:enter")
        if self.enter_error is not None:
            raise self.enter_error
        return self.value

    def __exit__(self, *args: object):
        self.events.append(f"{self.name}:exit")
        if self.exit_error is not None:
            raise self.exit_error
        return False


class FakeBuffer:
    def __init__(self) -> None:
        data = np.zeros((4, 5, 3), dtype=np.uint8)
        self.frames = [FakeFrame(10, data), FakeFrame(10, data), None]

    def get(self, timeout: float):
        return self.frames.pop(0)


class FakeClient:
    def __init__(self, current_game: int = 1) -> None:
        self.http = FakeHttp(current_game)
        self.events: list[str] = []
        self.quit_calls = 0

    def _get_http(self):
        return self.http

    def stream(self, **kwargs: object):
        return FakeContext(object(), self.events, "stream")

    def latest_frame(self):
        return FakeContext(FakeBuffer(), self.events, "buffer")

    def quit_app(self):
        self.quit_calls += 1


class LatestFrameObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = AutomationProfile.from_mapping({"name": "test"})

    def test_lifecycle_returns_only_new_frames_and_never_quits_app(self) -> None:
        client = FakeClient()
        observer = LatestFrameObserver(client, self.profile)

        with observer:
            first = observer.observe()
            duplicate = observer.observe(timeout=0.0)
            self.assertIsNotNone(first)
            self.assertIsNone(duplicate)
            self.assertEqual(observer.health().frames_observed, 1)

        self.assertEqual(observer.health().state, WorkerState.STOPPED)
        self.assertEqual(client.quit_calls, 0)
        self.assertEqual(
            client.events,
            ["stream:enter", "buffer:enter", "buffer:exit", "stream:exit"],
        )

    def test_start_failure_restores_stopped_state(self) -> None:
        client = FakeClient()
        observer = LatestFrameObserver(client, self.profile)
        client.latest_frame = lambda: FakeContext(
            FakeBuffer(),
            client.events,
            "buffer",
            enter_error=RuntimeError("buffer start failed"),
        )

        with self.assertRaisesRegex(RuntimeError, "buffer start failed"):
            observer.start()

        self.assertIn("stream:exit", client.events)
        self.assertEqual(observer.health().state, WorkerState.STOPPED)
        self.assertEqual(client.quit_calls, 0)
        self.assertEqual(
            client.events,
            ["stream:enter", "buffer:enter", "buffer:exit", "stream:exit"],
        )

    def test_start_refuses_to_create_a_desktop_session(self) -> None:
        client = FakeClient(current_game=0)
        observer = LatestFrameObserver(client, self.profile)

        with self.assertRaisesRegex(RuntimeError, "pre-existing"):
            observer.start()

        self.assertEqual(client.events, [])
        self.assertEqual(client.quit_calls, 0)
        self.assertEqual(observer.health().state, WorkerState.STOPPED)

    def test_start_accepts_idle_host_with_sanctioned_launch(self) -> None:
        client = FakeClient(current_game=0)
        client.allow_session_launch = True
        observer = LatestFrameObserver(client, self.profile)

        with observer:
            self.assertIsNotNone(observer.observe())

        self.assertEqual(client.quit_calls, 0)
        self.assertEqual(
            client.events,
            ["stream:enter", "buffer:enter", "buffer:exit", "stream:exit"],
        )

    def test_sanctioned_launch_never_displaces_another_session(self) -> None:
        client = FakeClient(current_game=99)
        client.allow_session_launch = True
        observer = LatestFrameObserver(client, self.profile)

        with self.assertRaisesRegex(RuntimeError, "pre-existing"):
            observer.start()

        self.assertEqual(client.events, [])
        self.assertEqual(observer.health().state, WorkerState.STOPPED)

    def test_stream_cleanup_runs_when_buffer_cleanup_fails(self) -> None:
        client = FakeClient()
        observer = LatestFrameObserver(client, self.profile)
        observer.start()
        observer._buffer_context.exit_error = RuntimeError("buffer cleanup failed")

        with self.assertRaisesRegex(RuntimeError, "buffer cleanup failed"):
            observer.stop()

        self.assertIn("stream:exit", client.events)
        self.assertEqual(client.quit_calls, 0)
        self.assertEqual(observer.health().state, WorkerState.STOPPED)


if __name__ == "__main__":
    unittest.main()
