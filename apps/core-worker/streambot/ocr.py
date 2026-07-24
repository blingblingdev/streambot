"""Persistent isolated OCR over a bounded in-memory protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import select
import struct
import subprocess
import sys
import time
from typing import Callable, Protocol

import numpy as np


MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_BYTES = 1024 * 1024


class OcrError(RuntimeError):
    """Raised when isolated OCR cannot return a safe structured result."""


@dataclass(frozen=True)
class OcrLine:
    """One recognized line with image-local geometry and confidence."""

    box: tuple[tuple[float, float], ...]
    text: str
    confidence: float


class OcrWorker(Protocol):
    """Bounded worker contract used by the lazy OCR adapter."""

    def recognize(
        self, image: np.ndarray, *, detect_text: bool = True
    ) -> tuple[OcrLine, ...]: ...

    def close(self) -> None: ...


def _validate_image(image: np.ndarray) -> np.ndarray:
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise OcrError("OCR input must be a uint8 BGR image")
    contiguous = np.ascontiguousarray(image)
    if contiguous.nbytes > MAX_IMAGE_BYTES:
        raise OcrError("OCR input exceeds the bounded image size")
    return contiguous


class SubprocessOcrWorker:
    """Keep one OCR backend alive outside the Moonlight media process."""

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("OCR timeout must be positive")
        worker_path = Path(__file__).with_name("rapid_ocr_worker.py")
        self._timeout_seconds = timeout_seconds
        self._process = subprocess.Popen(
            [sys.executable, str(worker_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        if self._process.stdin is None or self._process.stdout is None:
            self.close()
            raise OcrError("OCR worker pipes are unavailable")

    def _read_exact(self, size: int) -> bytes:
        if self._process.stdout is None:
            raise OcrError("OCR worker output is unavailable")
        deadline = time.monotonic() + self._timeout_seconds
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                self.close()
                raise OcrError("OCR worker response timed out")
            readable, _writable, _errors = select.select(
                [self._process.stdout], [], [], timeout
            )
            if not readable:
                self.close()
                raise OcrError("OCR worker response timed out")
            chunk = self._process.stdout.read(remaining)
            if not chunk:
                self.close()
                raise OcrError("OCR worker ended unexpectedly")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def recognize(
        self, image: np.ndarray, *, detect_text: bool = True
    ) -> tuple[OcrLine, ...]:
        image = _validate_image(image)
        if self._process.poll() is not None or self._process.stdin is None:
            raise OcrError("OCR worker is not running")
        header = json.dumps(
            {
                "shape": list(image.shape),
                "nbytes": image.nbytes,
                "detect_text": detect_text,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        if len(header) > MAX_MESSAGE_BYTES:
            raise OcrError("OCR request header is too large")
        try:
            self._process.stdin.write(struct.pack("!I", len(header)))
            self._process.stdin.write(header)
            self._process.stdin.write(image.tobytes())
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            self.close()
            raise OcrError("OCR worker request failed") from error

        response_size = struct.unpack("!I", self._read_exact(4))[0]
        if not 0 < response_size <= MAX_MESSAGE_BYTES:
            self.close()
            raise OcrError("OCR worker returned an invalid response size")
        try:
            response = json.loads(self._read_exact(response_size))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            self.close()
            raise OcrError("OCR worker returned invalid metadata") from error
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise OcrError("OCR worker could not recognize the image")
        raw_lines = response.get("lines")
        if not isinstance(raw_lines, list):
            raise OcrError("OCR worker returned invalid lines")
        lines: list[OcrLine] = []
        try:
            for item in raw_lines:
                box = tuple(
                    (float(point[0]), float(point[1])) for point in item["box"]
                )
                text = item["text"]
                confidence = float(item["confidence"])
                if not isinstance(text, str) or len(box) < 2:
                    raise ValueError
                if not 0.0 <= confidence <= 1.0:
                    raise ValueError
                lines.append(OcrLine(box, text, confidence))
        except (KeyError, TypeError, ValueError, IndexError) as error:
            raise OcrError("OCR worker returned an invalid line") from error
        return tuple(lines)

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            return
        self._process = None
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(struct.pack("!I", 0))
                    process.stdin.flush()
                process.wait(timeout=2.0)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()


class RapidOcrAdapter:
    """Lazily reuse one isolated OCR worker for in-memory BGR regions."""

    def __init__(
        self, worker_factory: Callable[[], OcrWorker] | None = None
    ) -> None:
        self._worker_factory = worker_factory or SubprocessOcrWorker
        self._worker: OcrWorker | None = None
        self.calls = 0
        self.total_seconds = 0.0

    def _get_worker(self) -> OcrWorker:
        if self._worker is None:
            self._worker = self._worker_factory()
        return self._worker

    def recognize_lines(
        self, image: np.ndarray, *, detect_text: bool = True
    ) -> tuple[OcrLine, ...]:
        image = _validate_image(image)
        started = time.perf_counter()
        try:
            lines = self._get_worker().recognize(
                image, detect_text=detect_text
            )
        finally:
            self.calls += 1
            self.total_seconds += time.perf_counter() - started
        if not isinstance(lines, tuple) or any(
            not isinstance(line, OcrLine) for line in lines
        ):
            raise OcrError("OCR adapter received an invalid worker result")
        return lines

    def recognize_line(self, image: np.ndarray) -> tuple[OcrLine, ...]:
        """Recognize one already-segmented text line without detection."""

        return self.recognize_lines(image, detect_text=False)

    def recognize(self, image: np.ndarray) -> str:
        """Return newline-joined text for the generic perception protocol."""

        return "\n".join(line.text for line in self.recognize_lines(image))

    def close(self) -> None:
        if self._worker is not None:
            self._worker.close()
            self._worker = None

    def __enter__(self) -> "RapidOcrAdapter":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
