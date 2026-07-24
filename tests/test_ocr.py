from __future__ import annotations

from io import BytesIO
import json
import struct
import unittest

import numpy as np

from streambot.ocr import OcrError, OcrLine, RapidOcrAdapter
from streambot.rapid_ocr_worker import serve


class FakeWorker:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0
        self.closed = 0

    def recognize(self, _image: np.ndarray, *, detect_text: bool = True):
        self.calls += 1
        return self.result

    def close(self) -> None:
        self.closed += 1


def request(image: np.ndarray, *, detect_text: bool = True) -> bytes:
    header = json.dumps(
        {
            "shape": list(image.shape),
            "nbytes": image.nbytes,
            "detect_text": detect_text,
        }
    ).encode("utf-8")
    return struct.pack("!I", len(header)) + header + image.tobytes()


def responses(payload: bytes) -> list[dict[str, object]]:
    stream = BytesIO(payload)
    values: list[dict[str, object]] = []
    while stream.tell() < len(payload):
        size = struct.unpack("!I", stream.read(4))[0]
        values.append(json.loads(stream.read(size)))
    return values


class RapidOcrAdapterTests(unittest.TestCase):
    def test_worker_is_created_lazily_and_reused(self) -> None:
        line = OcrLine(((1.0, 2.0), (3.0, 4.0)), "choice", 0.9)
        worker = FakeWorker((line,))
        factory_calls = 0

        def factory() -> FakeWorker:
            nonlocal factory_calls
            factory_calls += 1
            return worker

        adapter = RapidOcrAdapter(factory)
        self.assertEqual(factory_calls, 0)
        image = np.zeros((8, 12, 3), dtype=np.uint8)

        self.assertEqual(adapter.recognize_lines(image), (line,))
        self.assertEqual(adapter.recognize(image), "choice")
        self.assertEqual(factory_calls, 1)
        self.assertEqual(worker.calls, 2)
        self.assertEqual(adapter.calls, 2)
        adapter.close()
        self.assertEqual(worker.closed, 1)

    def test_invalid_image_fails_before_worker_creation(self) -> None:
        factory_calls = 0

        def factory() -> FakeWorker:
            nonlocal factory_calls
            factory_calls += 1
            return FakeWorker(())

        adapter = RapidOcrAdapter(factory)

        with self.assertRaises(OcrError):
            adapter.recognize_lines(np.zeros((8, 12), dtype=np.uint8))

        self.assertEqual(factory_calls, 0)
        self.assertEqual(adapter.calls, 0)

    def test_invalid_worker_result_fails_closed(self) -> None:
        adapter = RapidOcrAdapter(lambda: FakeWorker([]))

        with self.assertRaises(OcrError):
            adapter.recognize_lines(np.zeros((8, 12, 3), dtype=np.uint8))


class RapidOcrWorkerProtocolTests(unittest.TestCase):
    def test_engine_is_created_once_for_multiple_in_memory_requests(self) -> None:
        factory_calls = 0
        engine_calls = 0

        def factory(_detect_text: bool):
            nonlocal factory_calls
            factory_calls += 1

            def engine(image: np.ndarray):
                nonlocal engine_calls
                engine_calls += 1
                value = int(image[0, 0, 0])
                return [([[0, 0], [2, 0], [2, 2], [0, 2]], str(value), 0.8)], 0.01

            return engine

        first = np.zeros((3, 4, 3), dtype=np.uint8)
        second = np.ones((3, 4, 3), dtype=np.uint8)
        source = BytesIO(request(first) + request(second) + struct.pack("!I", 0))
        destination = BytesIO()

        serve(source, destination, factory)

        values = responses(destination.getvalue())
        self.assertEqual(factory_calls, 1)
        self.assertEqual(engine_calls, 2)
        self.assertEqual([value["lines"][0]["text"] for value in values], ["0", "1"])

    def test_engine_failure_returns_sanitized_error_and_keeps_protocol_aligned(self) -> None:
        calls = 0

        def factory(_detect_text: bool):
            def engine(_image: np.ndarray):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("sensitive details")
                return [], 0.0

            return engine

        image = np.zeros((3, 4, 3), dtype=np.uint8)
        source = BytesIO(request(image) + request(image) + struct.pack("!I", 0))
        destination = BytesIO()

        serve(source, destination, factory)

        values = responses(destination.getvalue())
        self.assertEqual(values[0], {"ok": False, "error_type": "RuntimeError"})
        self.assertEqual(values[1], {"ok": True, "lines": []})
        self.assertNotIn("sensitive", destination.getvalue().decode("utf-8"))

    def test_detection_and_recognition_engines_are_each_cached(self) -> None:
        modes: list[bool] = []

        def factory(detect_text: bool):
            modes.append(detect_text)

            def engine(_image: np.ndarray):
                return [], 0.0

            return engine

        image = np.zeros((3, 4, 3), dtype=np.uint8)
        source = BytesIO(
            request(image)
            + request(image, detect_text=False)
            + request(image, detect_text=False)
            + struct.pack("!I", 0)
        )
        destination = BytesIO()

        serve(source, destination, factory)

        self.assertEqual(modes, [True, False])
        self.assertEqual(len(responses(destination.getvalue())), 3)


if __name__ == "__main__":
    unittest.main()
