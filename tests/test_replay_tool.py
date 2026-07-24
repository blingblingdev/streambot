"""End-to-end test for the offline manifest replay tool."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPLAY = PROJECT_ROOT / "scripts" / "replay_manifest.py"


def _write_png(path: Path, frame_bgr: np.ndarray) -> None:
    Image.fromarray(frame_bgr[:, :, ::-1]).save(path)


def _manifest(path: Path) -> None:
    manifest = {
        "schema_version": 2,
        "target": "fixture",
        "scenes": {
            "blue-banner": {
                "detect": {
                    "predicates": [
                        {
                            "kind": "color",
                            "region": [0, 0, 100, 100],
                            "bgr": [255, 0, 0],
                            "tolerance": 10,
                            "minimum_fraction": 0.5,
                        }
                    ]
                },
                "controls": [
                    {
                        "id": "ok",
                        "action_kind": "click",
                        "extractor": {"kind": "fixed-point", "point": [640, 360]},
                    }
                ],
                "recommend": {"rule": "by-id", "id": "ok"},
            }
        },
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")


class ReplayToolTests(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPLAY), *arguments],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=120,
        )

    def test_replay_reports_match_and_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            _manifest(manifest_path)
            fixtures = root / "fixtures" / "blue-banner"
            fixtures.mkdir(parents=True)
            matching = np.zeros((720, 1280, 3), dtype=np.uint8)
            matching[0:100, 0:100] = (255, 0, 0)
            _write_png(fixtures / "frame-1.png", matching)

            result = self._run(
                str(manifest_path), str(root / "fixtures"), "--expect", "blue-banner"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["results"][0]["scene_id"], "blue-banner")
            self.assertEqual(payload["results"][0]["control_ids"], ["ok"])
            self.assertEqual(payload["results"][0]["recommended_id"], "ok")

            # A non-matching frame with an expectation is a counted failure.
            _write_png(
                fixtures / "frame-2.png", np.zeros((720, 1280, 3), dtype=np.uint8)
            )
            result = self._run(
                str(manifest_path), str(root / "fixtures"), "--expect", "blue-banner"
            )
            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["mismatches"], 1)

    def test_expect_from_dirname_uses_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            _manifest(manifest_path)
            fixtures = root / "fixtures" / "blue-banner"
            fixtures.mkdir(parents=True)
            matching = np.zeros((720, 1280, 3), dtype=np.uint8)
            matching[0:100, 0:100] = (255, 0, 0)
            _write_png(fixtures / "frame-1.png", matching)
            result = self._run(
                str(manifest_path), str(root / "fixtures"), "--expect-from-dirname"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])

    def test_default_output_contains_no_text_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            _manifest(manifest_path)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            matching = np.zeros((720, 1280, 3), dtype=np.uint8)
            matching[0:100, 0:100] = (255, 0, 0)
            _write_png(fixtures / "frame-1.png", matching)
            result = self._run(str(manifest_path), str(fixtures))
            payload = json.loads(result.stdout)
            self.assertNotIn("control_texts", payload["results"][0])

    def test_bare_manifest_reports_bare_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            _manifest(manifest_path)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            _write_png(
                fixtures / "frame-1.png", np.zeros((720, 1280, 3), dtype=np.uint8)
            )
            result = self._run(str(manifest_path), str(fixtures))
            payload = json.loads(result.stdout)
            self.assertEqual(payload["engine"], "bare")

    def test_extensions_module_is_discovered_and_errors_are_reported(self) -> None:
        # A target directory whose scene_extensions.build_scene_engine returns
        # an engine that raises must be auto-discovered, and the failure must
        # surface as a per-fixture error entry, not a tool crash.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles"
            profiles.mkdir()
            manifest_path = profiles / "scene-manifest.json"
            _manifest(manifest_path)
            (root / "scene_extensions.py").write_text(
                "class _Boom:\n"
                "    def reset(self):\n"
                "        pass\n"
                "    def observe(self, frame, index):\n"
                "        raise RuntimeError('extension engine failure')\n"
                "def build_scene_engine(ocr=None):\n"
                "    return _Boom()\n",
                encoding="utf-8",
            )
            fixtures = root / "fixtures"
            fixtures.mkdir()
            _write_png(
                fixtures / "frame-1.png", np.zeros((720, 1280, 3), dtype=np.uint8)
            )
            result = self._run(str(manifest_path), str(fixtures))
            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["engine"], "extensions")
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["results"][0]["error"], "RuntimeError")

            # --bare must ignore the extensions module entirely.
            result = self._run(str(manifest_path), str(fixtures), "--bare")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["engine"], "bare")


class ReplayRegressionGateTests(unittest.TestCase):
    """Run the fixture regression gate on machines that hold captures."""

    @unittest.skipUnless(
        (PROJECT_ROOT / ".fixtures").is_dir(),
        ".fixtures/ captures are not present on this machine",
    )
    def test_regression_gate_is_green(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "replay_regression.py"),
            ],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=600,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"], payload)


if __name__ == "__main__":
    unittest.main()
