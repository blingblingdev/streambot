#!/usr/bin/env python3
"""Serve one RapidOCR model over a bounded binary standard-I/O protocol."""

from __future__ import annotations

import json
import struct
import sys
from typing import BinaryIO, Callable

import numpy as np


MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_BYTES = 1024 * 1024


def read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_response(stream: BinaryIO, value: dict[str, object]) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > MAX_MESSAGE_BYTES:
        payload = b'{"ok":false,"error_type":"ResponseTooLarge"}'
    stream.write(struct.pack("!I", len(payload)))
    stream.write(payload)
    stream.flush()


def default_engine_factory(detect_text: bool):
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR(use_text_det=detect_text, use_angle_cls=False)


def serve(
    source: BinaryIO,
    destination: BinaryIO,
    engine_factory: Callable[[bool], object] = default_engine_factory,
) -> None:
    engines: dict[bool, object] = {}
    while True:
        try:
            header_size = struct.unpack("!I", read_exact(source, 4))[0]
        except EOFError:
            return
        if header_size == 0:
            return
        if header_size > MAX_MESSAGE_BYTES:
            write_response(destination, {"ok": False, "error_type": "HeaderTooLarge"})
            return
        try:
            header = json.loads(read_exact(source, header_size))
            shape = tuple(int(value) for value in header["shape"])
            nbytes = int(header["nbytes"])
            detect_text = header.get("detect_text", True)
            if not isinstance(detect_text, bool):
                raise ValueError
            if (
                len(shape) != 3
                or shape[2] != 3
                or any(value <= 0 for value in shape)
                or not 0 < nbytes <= MAX_IMAGE_BYTES
            ):
                raise ValueError
            if int(np.prod(shape)) != nbytes:
                raise ValueError
            image = np.frombuffer(read_exact(source, nbytes), dtype=np.uint8).reshape(
                shape
            )
            if detect_text not in engines:
                engines[detect_text] = engine_factory(detect_text)
            result, _elapsed = engines[detect_text](image)
            lines = [
                {
                    "box": [[float(value) for value in point] for point in box],
                    "text": text,
                    "confidence": float(confidence),
                }
                for box, text, confidence in result or []
            ]
            write_response(destination, {"ok": True, "lines": lines})
        except Exception as error:
            write_response(
                destination,
                {"ok": False, "error_type": type(error).__name__},
            )


def main() -> int:
    serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
