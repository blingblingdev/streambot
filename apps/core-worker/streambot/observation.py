"""Efficient latest-frame observation for a pre-existing Desktop session."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Callable

import av
import numpy as np
from av.codec.hwaccel import HWAccel
from moonlight_python import MoonlightClient
from moonlight_python.decoder import CODEC_NAMES, Decoder

from .config import AutomationProfile, ObservationSettings
from .models import WorkerHealth, WorkerState


@dataclass(frozen=True)
class DecoderSelection:
    """Decoder instance and the backend that was actually selected."""

    decoder: Decoder
    backend: str
    used_fallback: bool


class OutputRateLimiter:
    """Monotonic fixed-interval gate used to limit NumPy conversion."""

    def __init__(self, fps: float, clock: Callable[[], float] = time.monotonic) -> None:
        if fps <= 0:
            raise ValueError("fps must be positive")
        self._interval = 1.0 / fps
        self._clock = clock
        self._next_output = 0.0

    def admit(self) -> bool:
        """Return true when one output is due."""

        now = self._clock()
        if now < self._next_output:
            return False
        self._next_output = now + self._interval
        return True


class ThrottledDecoder(Decoder):
    """Decode every packet but convert only automation-relevant frames."""

    def __init__(
        self,
        codec: str,
        output_format: str,
        sample_fps: float,
        *,
        hardware: bool,
    ) -> None:
        codec_name = CODEC_NAMES.get(codec.lower())
        if codec_name is None:
            raise ValueError("unsupported codec")
        if output_format not in {"bgr24", "rgb24"}:
            raise ValueError("unsupported output format")

        hwaccel = None
        if hardware:
            if codec_name != "h264":
                raise ValueError("VideoToolbox mode requires H.264")
            hwaccel = HWAccel("videotoolbox", allow_software_fallback=False)

        self._codec_ctx = av.CodecContext.create(codec_name, "r", hwaccel=hwaccel)
        if not hardware:
            self._codec_ctx.thread_type = "AUTO"
            self._codec_ctx.thread_count = 0
        self._output_format = output_format
        self._open = True
        self._gate = OutputRateLimiter(sample_fps)
        self.packets_received = 0
        self.frames_decoded = 0
        self.frames_emitted = 0

    @property
    def hardware_active(self) -> bool:
        """Report whether the PyAV codec context uses hardware acceleration."""

        return bool(self._codec_ctx.is_hwaccel)

    def decode(self, annex_b_data: bytes) -> list[np.ndarray]:
        """Decode the packet and emit at most one rate-limited NumPy frame."""

        if not self._open:
            raise RuntimeError("decoder is closed")
        self.packets_received += 1
        try:
            decoded = self._codec_ctx.decode(av.Packet(annex_b_data))
        except av.error.InvalidDataError:
            return []
        self.frames_decoded += len(decoded)
        if not decoded or not self._gate.admit():
            return []
        self.frames_emitted += 1
        return [decoded[-1].to_ndarray(format=self._output_format)]


def create_decoder(settings: ObservationSettings) -> DecoderSelection:
    """Create the requested decoder with an explicit software fallback."""

    if settings.decoder == "software":
        return DecoderSelection(
            decoder=ThrottledDecoder(
                "h264", "bgr24", settings.sample_fps, hardware=False
            ),
            backend="software",
            used_fallback=False,
        )
    try:
        decoder = ThrottledDecoder(
            "h264", "bgr24", settings.sample_fps, hardware=True
        )
    except Exception:
        if not settings.software_fallback:
            raise
        return DecoderSelection(
            decoder=ThrottledDecoder(
                "h264", "bgr24", settings.sample_fps, hardware=False
            ),
            backend="software",
            used_fallback=True,
        )
    return DecoderSelection(
        decoder=decoder,
        backend="videotoolbox",
        used_fallback=False,
    )


@contextmanager
def preserve_host_application_session(http, *, allow_launch: bool = False):
    """Guard upstream stream setup against host application mutation.

    Quitting is always rejected: the worker must never end a host session.
    Launching is rejected by default and may be allowed only when the caller
    verified at connect time that the host has NO active application session,
    so starting Desktop cannot displace anything pre-existing.
    """

    original_launch = http.launch_app
    original_quit = http.quit_app

    def reject_mutation(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Host application mutation is disabled")

    if not allow_launch:
        http.launch_app = reject_mutation
    http.quit_app = reject_mutation
    try:
        yield
    finally:
        http.launch_app = original_launch
        http.quit_app = original_quit


class AutomationMoonlightClient(MoonlightClient):
    """Moonlight client that installs the configured throttled decoder."""

    def __init__(self, *args: object, observation: ObservationSettings, **kwargs: object):
        super().__init__(*args, **kwargs)
        self._observation_settings = observation
        self.decoder_backend: str | None = None
        self.decoder_used_fallback = False
        # Set by connect_paired_worker only after verifying the host has no
        # active application session, so a launch cannot displace anything.
        self.allow_session_launch = False

    def _setup_stream(
        self,
        app: str,
        width: int,
        height: int,
        fps: int,
        bitrate_kbps: int,
        codec: str,
        output_format: str = "bgr24",
    ):
        selection = create_decoder(self._observation_settings)
        try:
            with preserve_host_application_session(
                self._get_http(), allow_launch=self.allow_session_launch
            ):
                session, default_decoder = super()._setup_stream(
                    app, width, height, fps, bitrate_kbps, codec, output_format
                )
        except BaseException:
            selection.decoder.close()
            raise
        default_decoder.close()
        self._decoder = selection.decoder
        self.decoder_backend = selection.backend
        self.decoder_used_fallback = selection.used_fallback
        return session, selection.decoder


@dataclass(frozen=True)
class Observation:
    """One latest-frame sample and safe timing metadata."""

    frame_number: int
    observed_at: float
    data: np.ndarray


class LatestFrameObserver:
    """Own one stream connection while preserving the host application session."""

    def __init__(self, client: MoonlightClient, profile: AutomationProfile) -> None:
        self._client = client
        self._profile = profile
        self._stream_context = None
        self._buffer_context = None
        self._buffer = None
        self._lock = Lock()
        self._state = WorkerState.STOPPED
        self._frames_observed = 0
        self._last_frame_number: int | None = None

    def _require_launchable_desktop(self) -> None:
        """Allow start only into an active Desktop session or an idle host.

        A sanctioned launch (``allow_session_launch``, granted at connect time
        when the host reported no active session) accepts an idle host; the
        launch then happens inside stream setup. A different application's
        active session must never be displaced.
        """

        http = self._client._get_http()
        apps = http.get_app_list()
        desktop = next((app for app in apps if app.name.casefold() == "desktop"), None)
        if desktop is None:
            raise RuntimeError("Desktop application is unavailable")
        info = http.parse_server_info(http.get_server_info(use_https=True))
        current_game = info.current_game or 0
        if current_game == desktop.id:
            return
        if current_game == 0 and getattr(
            self._client, "allow_session_launch", False
        ):
            return
        raise RuntimeError("A pre-existing Desktop session is required")

    def start(self) -> None:
        """Start observation only after proving the Desktop policy allows it."""

        with self._lock:
            if self._state is not WorkerState.STOPPED:
                raise RuntimeError("observer is already started")
            self._state = WorkerState.STARTING
            try:
                self._require_launchable_desktop()
                stream = self._profile.stream
                self._stream_context = self._client.stream(
                    app="Desktop",
                    width=stream.width,
                    height=stream.height,
                    fps=stream.fps,
                    bitrate_kbps=stream.bitrate_kbps,
                    codec=stream.codec,
                    ready_timeout=15.0,
                )
                self._stream_context.__enter__()
                self._buffer_context = self._client.latest_frame()
                self._buffer = self._buffer_context.__enter__()
                self._state = WorkerState.OBSERVING
            except BaseException:
                try:
                    self._cleanup()
                finally:
                    self._state = WorkerState.STOPPED
                raise

    def observe(self, timeout: float = 1.0) -> Observation | None:
        """Return a new latest frame, suppressing duplicate frame numbers."""

        with self._lock:
            if self._state is not WorkerState.OBSERVING or self._buffer is None:
                raise RuntimeError("observer is not running")
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                remaining = max(0.0, deadline - time.monotonic())
                frame = self._buffer.get(timeout=remaining)
                if frame is not None and frame.frame_number != self._last_frame_number:
                    self._last_frame_number = frame.frame_number
                    self._frames_observed += 1
                    return Observation(
                        frame_number=frame.frame_number,
                        observed_at=time.monotonic(),
                        data=frame.data,
                    )
                if remaining <= 0:
                    return None
                time.sleep(min(0.02, remaining))

    def _cleanup(self) -> None:
        buffer_context = self._buffer_context
        stream_context = self._stream_context
        self._buffer_context = None
        self._buffer = None
        self._stream_context = None
        try:
            if buffer_context is not None:
                buffer_context.__exit__(None, None, None)
        finally:
            if stream_context is not None:
                stream_context.__exit__(None, None, None)

    def stop(self) -> None:
        """Disconnect this observer without calling the application quit endpoint."""

        with self._lock:
            try:
                self._cleanup()
            finally:
                self._state = WorkerState.STOPPED

    def health(self) -> WorkerHealth:
        """Return a metadata-only lifecycle snapshot."""

        with self._lock:
            return WorkerHealth(state=self._state, frames_observed=self._frames_observed)

    def __enter__(self) -> "LatestFrameObserver":
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()
