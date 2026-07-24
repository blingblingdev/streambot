#!/usr/bin/env python3
"""Run owner-confirmed pairing or a bounded headless frame probe."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import resource
import secrets
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
STATE_DIR = PROJECT_DIR / ".state" / "poc"
PAIR_BRIDGE_ADDRESS = ("127.0.0.1", 47888)

if sys.prefix == sys.base_prefix:
    if not VENV_PYTHON.is_file():
        raise SystemExit("Project environment is missing; run ./scripts/bootstrap.sh first")
    os.execv(VENV_PYTHON, [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

import av
import moonlight_python
from av.codec.hwaccel import HWAccel
from moonlight_python import MoonlightClient
from moonlight_python.decoder import Decoder as SoftwareDecoder
from moonlight_python.http_client import NvHTTP


def apple_script(source: str) -> str:
    """Execute AppleScript from stdin so sensitive values avoid process arguments."""

    result = subprocess.run(
        ["/usr/bin/osascript", "-"],
        input=f"activate\n{source}",
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Owner cancelled the confirmation dialog")
    return result.stdout.strip()


def apple_string(value: str) -> str:
    """Escape a value for an AppleScript string literal."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


def confirm(message: str, button: str = "Continue") -> None:
    """Require an explicit local confirmation without terminal disclosure."""

    script = f'''display dialog "{apple_string(message)}" buttons {{"Cancel", "{apple_string(button)}"}} default button "{apple_string(button)}" cancel button "Cancel" with icon caution
'''
    apple_script(script)


def request_pin() -> str:
    """Ask the owner to choose a temporary PIN without logging it."""

    script = '''set resultDialog to display dialog "Choose a temporary 4-digit pairing PIN. Remember it, then enter the same PIN on the Sunshine PIN page." default answer "" buttons {"Cancel", "Start pairing"} default button "Start pairing" cancel button "Cancel" with icon caution
return text returned of resultDialog
'''
    pin = apple_script(script)
    if len(pin) != 4 or not pin.isdigit():
        raise RuntimeError("The pairing PIN must contain exactly four digits")
    return pin


def notify(message: str) -> None:
    """Show a local notification without writing its contents to the terminal."""

    script = f'''display notification "{apple_string(message)}" with title "streambot POC"
'''
    apple_script(script)


def ensure_private_state() -> None:
    """Create the persistent identity directory with private permissions."""

    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_DIR.chmod(0o700)


def enforce_identity_modes() -> None:
    """Restrict all generated identity files to the owner."""

    for name in ("key.pem", "cert.pem", "unique_id"):
        path = STATE_DIR / name
        if path.exists():
            path.chmod(0o600)


def discover_one(client: MoonlightClient):
    """Discover exactly one target while suppressing third-party metadata output."""

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        servers = client.discover(timeout=5.0)
    if len(servers) != 1:
        raise RuntimeError("Expected exactly one visible Sunshine host")
    return servers[0]


def make_client() -> MoonlightClient:
    """Create a worker identity under the protected project state directory."""

    ensure_private_state()
    client = MoonlightClient(config_dir=STATE_DIR)
    enforce_identity_modes()
    return client


def pair_phase() -> dict[str, object]:
    """Pair a new worker identity after local owner confirmation."""

    client = make_client()
    server = discover_one(client)

    target_label = server.hostname.strip() or "Unnamed Sunshine host"
    confirm(
        f"Confirm pairing target: {target_label}. A browser will open the Sunshine PIN page. No stream or remote input will start.",
        "Confirm target",
    )

    web_port = server.https_port + 1
    webbrowser.open(f"https://{server.address}:{web_port}/pin", new=1)
    pin = request_pin()
    notify("Enter the same temporary PIN on the Sunshine PIN page now.")

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        client.pair(server=server, pin=pin)

    enforce_identity_modes()
    modes = {
        name: oct(stat.S_IMODE((STATE_DIR / name).stat().st_mode))
        for name in ("key.pem", "cert.pem", "unique_id")
    }
    return {
        "status": "PASS",
        "phase": "pair",
        "target_count": 1,
        "host_metadata_exposed": False,
        "identity_file_modes": modes,
        "stream_actions": 0,
        "remote_input_actions": 0,
    }


def pair_assisted_phase() -> dict[str, object]:
    """Display a generated PIN while the owner completes Sunshine pairing."""

    client = make_client()
    server = discover_one(client)
    pin = f"{secrets.randbelow(10000):04d}"
    pairing_errors: list[BaseException] = []

    def run_pairing() -> None:
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                client.pair(server=server, pin=pin)
        except BaseException as error:
            pairing_errors.append(error)

    pairing_thread = threading.Thread(target=run_pairing, daemon=True)
    pairing_thread.start()

    web_port = server.https_port + 1
    webbrowser.open(f"https://{server.address}:{web_port}/pin", new=1)
    target_label = server.hostname.strip() or "Unnamed Sunshine host"
    confirm(
        f"Pairing target: {target_label}\n\nTemporary PIN: {pin}\n\nEnter this PIN on the opened Sunshine PIN page. Keep this dialog open while pairing, then click Done after Sunshine reports success.",
        "Done",
    )

    pairing_thread.join(timeout=100.0)
    if pairing_thread.is_alive():
        raise RuntimeError("Pairing did not finish within the bounded timeout")
    if pairing_errors:
        raise RuntimeError("Sunshine pairing failed")

    enforce_identity_modes()
    modes = {
        name: oct(stat.S_IMODE((STATE_DIR / name).stat().st_mode))
        for name in ("key.pem", "cert.pem", "unique_id")
    }
    return {
        "status": "PASS",
        "phase": "pair-assisted",
        "target_count": 1,
        "host_metadata_exposed": False,
        "identity_file_modes": modes,
        "stream_actions": 0,
        "remote_input_actions": 0,
    }


def pair_browser_phase() -> dict[str, object]:
    """Pair through a one-shot local redirect controlled by the browser."""

    client = make_client()
    server = discover_one(client)
    redirect_url = f"https://{server.address}:{server.https_port + 1}/pin"
    pin_ready = threading.Event()
    pin_holder: list[str] = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'''<!doctype html><html><body><main><h1>streambot Pairing</h1><form method="post"><label for="pin">Temporary 4-digit PIN</label><input id="pin" name="pin" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" required autocomplete="off"><button type="submit">Continue to Sunshine</button></form></main></body></html>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            fields = parse_qs(self.rfile.read(content_length).decode("utf-8"))
            pin = fields.get("pin", [""])[0]
            if len(pin) != 4 or not pin.isdigit():
                self.send_error(400)
                return
            pin_holder.append(pin)
            pin_ready.set()
            safe_redirect = redirect_url.replace("&", "&amp;").replace('"', "&quot;")
            body = f'''<!doctype html><html><body><main><h1>Pairing request active</h1><p>Temporary PIN: <strong>{pin}</strong></p><p>Keep this page open while completing pairing.</p><a href="{safe_redirect}" target="_blank" rel="noreferrer">Open Sunshine PIN page</a></main></body></html>'''.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    bridge = ThreadingHTTPServer(PAIR_BRIDGE_ADDRESS, RedirectHandler)
    bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    bridge_thread.start()
    print(json.dumps({"bridge_ready": True}), flush=True)

    try:
        if not pin_ready.wait(timeout=90.0):
            raise RuntimeError("Timed out waiting for local pairing confirmation")
        pin = pin_holder.pop()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            client.pair(server=server, pin=pin)
    finally:
        bridge.shutdown()
        bridge.server_close()
        bridge_thread.join(timeout=2.0)

    enforce_identity_modes()
    modes = {
        name: oct(stat.S_IMODE((STATE_DIR / name).stat().st_mode))
        for name in ("key.pem", "cert.pem", "unique_id")
    }
    return {
        "status": "PASS",
        "phase": "pair-browser",
        "target_count": 1,
        "host_metadata_exposed": False,
        "identity_file_modes": modes,
        "stream_actions": 0,
        "remote_input_actions": 0,
        "pin_transport": "localhost_post",
    }


def prepare_paired_client(client: MoonlightClient, server) -> NvHTTP:
    """Verify the persistent identity without triggering automatic pairing."""

    http = NvHTTP(
        server.address,
        client._identity,
        http_port=server.http_port,
        https_port=server.https_port,
    )
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        http.get_app_list()
    client._server = server
    client._http = http
    return http


def peak_rss_mb() -> float:
    """Return peak resident memory in MiB on macOS."""

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def stream_phase() -> dict[str, object]:
    """Run a ten-second frame-only probe with bounded settings."""

    client = make_client()
    server = discover_one(client)
    http = prepare_paired_client(client, server)
    apps = http.get_app_list()
    desktop = next((app for app in apps if app.name.casefold() == "desktop"), None)
    if desktop is None:
        raise RuntimeError("The target does not expose a Desktop application")

    server_info = http.parse_server_info(http.get_server_info(use_https=True))
    if server_info.current_game not in (0, desktop.id):
        raise RuntimeError("A different application is already active; probe aborted")

    launched_new_session = server_info.current_game == 0
    wall_start = time.monotonic()
    usage_start = resource.getrusage(resource.RUSAGE_SELF)
    unique_frames = 0
    last_frame_number = None
    frame_shape = None
    host_latencies: list[int] = []

    try:
        with client.stream(
            app="Desktop",
            width=1280,
            height=720,
            fps=15,
            bitrate_kbps=4000,
            codec="h264",
            ready_timeout=15.0,
        ):
            probe_deadline = time.monotonic() + 10.0
            with client.latest_frame() as buffer:
                while time.monotonic() < probe_deadline:
                    frame = buffer.get(timeout=1.0)
                    if frame is None or frame.frame_number == last_frame_number:
                        time.sleep(0.02)
                        continue
                    last_frame_number = frame.frame_number
                    unique_frames += 1
                    frame_shape = list(frame.data.shape)
                    if frame.host_processing_latency_us > 0:
                        host_latencies.append(frame.host_processing_latency_us)
                    time.sleep(0.02)
    finally:
        client.stop_stream()
        if launched_new_session:
            with contextlib.suppress(Exception):
                client.quit_app()

    usage_end = resource.getrusage(resource.RUSAGE_SELF)
    wall_seconds = time.monotonic() - wall_start
    if unique_frames == 0 or frame_shape is None:
        raise RuntimeError("No decoded frames were observed")

    average_host_latency_ms = None
    if host_latencies:
        average_host_latency_ms = round(
            sum(host_latencies) / len(host_latencies) / 1000.0, 3
        )

    return {
        "status": "PASS",
        "phase": "stream",
        "settings": {
            "resolution": "1280x720",
            "fps": 15,
            "codec": "h264",
            "bitrate_kbps": 4000,
        },
        "probe_wall_seconds": round(wall_seconds, 3),
        "process_cpu_seconds": round(
            (usage_end.ru_utime + usage_end.ru_stime)
            - (usage_start.ru_utime + usage_start.ru_stime),
            3,
        ),
        "process_peak_rss_mb": round(peak_rss_mb(), 1),
        "unique_decoded_frames": unique_frames,
        "frame_shape": frame_shape,
        "average_host_processing_latency_ms": average_host_latency_ms,
        "image_files_written": 0,
        "host_metadata_exposed": False,
        "startup_mouse_nudges": 1,
        "click_actions": 0,
        "keyboard_actions": 0,
        "ended_new_desktop_session": launched_new_session,
    }


def coexistence_phase() -> dict[str, object]:
    """Probe frame stability while preserving a pre-existing Desktop session."""

    client = make_client()
    server = discover_one(client)
    http = prepare_paired_client(client, server)
    apps = http.get_app_list()
    desktop = next((app for app in apps if app.name.casefold() == "desktop"), None)
    if desktop is None:
        raise RuntimeError("The target does not expose a Desktop application")

    server_info = http.parse_server_info(http.get_server_info(use_https=True))
    if server_info.current_game != desktop.id:
        raise RuntimeError("A pre-existing Desktop session is required")

    wall_start = time.monotonic()
    usage_start = resource.getrusage(resource.RUSAGE_SELF)
    unique_frames = 0
    last_frame_number = None
    last_frame_time = None
    maximum_frame_gap_seconds = 0.0
    frame_shape = None
    inconsistent_shapes = 0
    grayscale_checksums: set[int] = set()

    try:
        with client.stream(
            app="Desktop",
            width=1280,
            height=720,
            fps=15,
            bitrate_kbps=4000,
            codec="h264",
            ready_timeout=15.0,
        ):
            probe_deadline = time.monotonic() + 30.0
            with client.latest_frame() as buffer:
                while time.monotonic() < probe_deadline:
                    frame = buffer.get(timeout=1.0)
                    if frame is None or frame.frame_number == last_frame_number:
                        time.sleep(0.02)
                        continue

                    observed_at = time.monotonic()
                    if last_frame_time is not None:
                        maximum_frame_gap_seconds = max(
                            maximum_frame_gap_seconds, observed_at - last_frame_time
                        )
                    last_frame_time = observed_at
                    last_frame_number = frame.frame_number
                    unique_frames += 1

                    observed_shape = list(frame.data.shape)
                    if frame_shape is None:
                        frame_shape = observed_shape
                    elif observed_shape != frame_shape:
                        inconsistent_shapes += 1

                    sample = frame.data[::32, ::32]
                    grayscale = (
                        sample[:, :, 0].astype("uint32") * 29
                        + sample[:, :, 1].astype("uint32") * 150
                        + sample[:, :, 2].astype("uint32") * 77
                    ) >> 8
                    grayscale_checksums.add(int(grayscale.sum()))
                    time.sleep(0.02)
    finally:
        client.stop_stream()

    usage_end = resource.getrusage(resource.RUSAGE_SELF)
    wall_seconds = time.monotonic() - wall_start
    if unique_frames == 0 or frame_shape is None:
        raise RuntimeError("No decoded frames were observed")

    return {
        "status": "PASS",
        "phase": "coexistence",
        "existing_desktop_required": True,
        "existing_desktop_ended": False,
        "settings": {
            "resolution": "1280x720",
            "fps": 15,
            "codec": "h264",
            "bitrate_kbps": 4000,
        },
        "probe_wall_seconds": round(wall_seconds, 3),
        "process_cpu_seconds": round(
            (usage_end.ru_utime + usage_end.ru_stime)
            - (usage_start.ru_utime + usage_start.ru_stime),
            3,
        ),
        "process_peak_rss_mb": round(peak_rss_mb(), 1),
        "unique_decoded_frames": unique_frames,
        "distinct_grayscale_checksums": len(grayscale_checksums),
        "maximum_observed_frame_gap_seconds": round(maximum_frame_gap_seconds, 3),
        "frame_shape": frame_shape,
        "inconsistent_frame_shapes": inconsistent_shapes,
        "image_files_written": 0,
        "host_metadata_exposed": False,
        "startup_mouse_nudges": 1,
        "click_actions": 0,
        "keyboard_actions": 0,
    }


class VideoToolboxDecoder(SoftwareDecoder):
    """Decode H.264 with the macOS VideoToolbox hardware context."""

    def __init__(self, codec: str = "h264", output_format: str = "bgr24") -> None:
        if codec.lower() != "h264":
            raise RuntimeError("The VideoToolbox probe supports only H.264")
        if output_format not in {"bgr24", "rgb24"}:
            raise RuntimeError("Unsupported decoder output format")
        self._codec_ctx = av.CodecContext.create(
            "h264",
            "r",
            hwaccel=HWAccel("videotoolbox", allow_software_fallback=False),
        )
        self._output_format = output_format
        self._open = True


class ThrottledVideoToolboxDecoder(VideoToolboxDecoder):
    """Decode continuously but convert at most two frames per second to NumPy."""

    def __init__(self, codec: str = "h264", output_format: str = "bgr24") -> None:
        super().__init__(codec=codec, output_format=output_format)
        self._emit_interval = 0.5
        self._next_emit_at = 0.0

    def decode(self, annex_b_data: bytes) -> list[object]:
        if not self._open:
            raise RuntimeError("Decoder is closed")

        try:
            decoded = self._codec_ctx.decode(av.Packet(annex_b_data))
        except av.error.InvalidDataError:
            return []

        now = time.monotonic()
        if not decoded or now < self._next_emit_at:
            return []

        self._next_emit_at = now + self._emit_interval
        return [decoded[-1].to_ndarray(format=self._output_format)]


def efficient_automation_phase(
    *, hardware_decode: bool = False, throttle_decoder_output: bool = False
) -> dict[str, object]:
    """Sample the latest frame at two FPS without ending an existing session."""

    client = make_client()
    server = discover_one(client)
    http = prepare_paired_client(client, server)
    apps = http.get_app_list()
    desktop = next((app for app in apps if app.name.casefold() == "desktop"), None)
    if desktop is None:
        raise RuntimeError("The target does not expose a Desktop application")

    server_info = http.parse_server_info(http.get_server_info(use_https=True))
    if server_info.current_game != desktop.id:
        raise RuntimeError("A pre-existing Desktop session is required")

    sampling_fps = 2
    sampling_interval = 1.0 / sampling_fps
    wall_start = time.monotonic()
    usage_start = resource.getrusage(resource.RUSAGE_SELF)
    processed_samples = 0
    changed_samples = 0
    last_frame_number = None
    frame_shape = None
    processing_seconds = 0.0
    checksums: set[int] = set()
    hardware_decoder_active = False

    original_decoder = moonlight_python.Decoder
    if throttle_decoder_output:
        moonlight_python.Decoder = ThrottledVideoToolboxDecoder
    elif hardware_decode:
        moonlight_python.Decoder = VideoToolboxDecoder

    try:
        with client.stream(
            app="Desktop",
            width=1280,
            height=720,
            fps=15,
            bitrate_kbps=4000,
            codec="h264",
            ready_timeout=15.0,
        ):
            if client._decoder is not None:
                hardware_decoder_active = bool(client._decoder._codec_ctx.is_hwaccel)
            started = time.monotonic()
            probe_deadline = started + 30.0
            next_sample = started
            with client.latest_frame() as buffer:
                while next_sample < probe_deadline:
                    delay = next_sample - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    frame = buffer.get(timeout=1.0)
                    if frame is None:
                        next_sample += sampling_interval
                        continue

                    processing_start = time.monotonic()
                    processed_samples += 1
                    if frame.frame_number != last_frame_number:
                        changed_samples += 1
                    last_frame_number = frame.frame_number
                    frame_shape = list(frame.data.shape)

                    sample = frame.data[::32, ::32]
                    grayscale = (
                        sample[:, :, 0].astype("uint32") * 29
                        + sample[:, :, 1].astype("uint32") * 150
                        + sample[:, :, 2].astype("uint32") * 77
                    ) >> 8
                    checksums.add(int(grayscale.sum()))
                    processing_seconds += time.monotonic() - processing_start
                    next_sample += sampling_interval
    finally:
        client.stop_stream()
        moonlight_python.Decoder = original_decoder

    usage_end = resource.getrusage(resource.RUSAGE_SELF)
    wall_seconds = time.monotonic() - wall_start
    if processed_samples == 0 or frame_shape is None:
        raise RuntimeError("No frames were processed")

    cpu_seconds = (
        (usage_end.ru_utime + usage_end.ru_stime)
        - (usage_start.ru_utime + usage_start.ru_stime)
    )
    return {
        "status": "PASS",
        "phase": (
            "efficient-automation-throttled"
            if throttle_decoder_output
            else (
                "efficient-automation-hardware"
                if hardware_decode
                else "efficient-automation"
            )
        ),
        "decoder": (
            "videotoolbox-throttled"
            if throttle_decoder_output
            else ("videotoolbox" if hardware_decode else "software")
        ),
        "decoder_output_fps_limit": 2 if throttle_decoder_output else None,
        "hardware_decoder_active": hardware_decoder_active,
        "existing_desktop_required": True,
        "existing_desktop_ended": False,
        "sampling_fps": sampling_fps,
        "probe_wall_seconds": round(wall_seconds, 3),
        "process_cpu_seconds": round(cpu_seconds, 3),
        "single_core_cpu_percent": round(cpu_seconds / wall_seconds * 100.0, 1),
        "process_peak_rss_mb": round(peak_rss_mb(), 1),
        "processed_samples": processed_samples,
        "changed_frame_samples": changed_samples,
        "distinct_grayscale_checksums": len(checksums),
        "vision_processing_seconds": round(processing_seconds, 4),
        "average_vision_processing_ms": round(
            processing_seconds / processed_samples * 1000.0, 3
        ),
        "frame_shape": frame_shape,
        "image_files_written": 0,
        "host_metadata_exposed": False,
        "startup_mouse_nudges": 1,
        "click_actions": 0,
        "keyboard_actions": 0,
    }


def parse_args() -> argparse.Namespace:
    """Parse the explicitly selected live phase."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=(
            "pair",
            "pair-assisted",
            "pair-browser",
            "stream",
            "coexistence",
            "efficient-automation",
            "efficient-automation-hardware",
            "efficient-automation-throttled",
        ),
    )
    return parser.parse_args()


def main() -> int:
    """Run one phase and emit only a metadata-free summary."""

    args = parse_args()
    previous_umask = os.umask(0o077)
    try:
        if args.phase == "pair":
            result = pair_phase()
        elif args.phase == "pair-assisted":
            result = pair_assisted_phase()
        elif args.phase == "pair-browser":
            result = pair_browser_phase()
        elif args.phase == "stream":
            result = stream_phase()
        elif args.phase == "coexistence":
            result = coexistence_phase()
        elif args.phase == "efficient-automation":
            result = efficient_automation_phase()
        elif args.phase == "efficient-automation-hardware":
            result = efficient_automation_phase(hardware_decode=True)
        else:
            result = efficient_automation_phase(
                hardware_decode=True, throttle_decoder_output=True
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "phase": args.phase,
                    "error_type": type(error).__name__,
                    "sensitive_details_exposed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
