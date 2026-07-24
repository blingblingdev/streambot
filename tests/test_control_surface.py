"""Tests for the declarative control-surface manifest and scanner."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from streambot.control_surface import (
    Control,
    ManifestControlScanner,
    ManifestError,
    load_control_manifest,
)


def _write(directory: Path, payload: dict) -> Path:
    path = directory / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _minimal_layout() -> dict:
    return {
        "controls": [
            {
                "id": "ok",
                "action_kind": "click",
                "extractor": {"kind": "fixed-point", "x": 1, "y": 2},
            }
        ],
        "recommend": {"rule": "static-index", "index": 0},
    }


class ManifestValidationTests(unittest.TestCase):
    def test_valid_manifest_loads(self) -> None:
        with TemporaryDirectory() as directory:
            path = _write(
                Path(directory),
                {"schema_version": 1, "target": "t", "layouts": {"a": _minimal_layout()}},
            )
            data = load_control_manifest(path)
        self.assertEqual(data["target"], "t")

    def test_rejects_unknown_top_level_key(self) -> None:
        with TemporaryDirectory() as directory:
            path = _write(
                Path(directory),
                {"schema_version": 1, "target": "t", "layouts": {"a": _minimal_layout()}, "x": 1},
            )
            with self.assertRaises(ManifestError):
                load_control_manifest(path)

    def test_rejects_wrong_schema_version(self) -> None:
        with TemporaryDirectory() as directory:
            path = _write(
                Path(directory),
                {"schema_version": 2, "target": "t", "layouts": {"a": _minimal_layout()}},
            )
            with self.assertRaises(ManifestError):
                load_control_manifest(path)

    def test_rejects_layout_missing_recommend(self) -> None:
        layout = _minimal_layout()
        del layout["recommend"]
        with TemporaryDirectory() as directory:
            path = _write(
                Path(directory),
                {"schema_version": 1, "target": "t", "layouts": {"a": layout}},
            )
            with self.assertRaises(ManifestError):
                load_control_manifest(path)

    def test_rejects_duplicate_control_id(self) -> None:
        layout = _minimal_layout()
        layout["controls"].append(dict(layout["controls"][0]))
        with TemporaryDirectory() as directory:
            path = _write(
                Path(directory),
                {"schema_version": 1, "target": "t", "layouts": {"a": layout}},
            )
            with self.assertRaises(ManifestError):
                load_control_manifest(path)

    def test_rejects_empty_layouts(self) -> None:
        with TemporaryDirectory() as directory:
            path = _write(Path(directory), {"schema_version": 1, "target": "t", "layouts": {}})
            with self.assertRaises(ManifestError):
                load_control_manifest(path)

    def test_rejects_each_point_without_source(self) -> None:
        layout = {
            "controls": [
                {"id": "c", "action_kind": "click", "extractor": {"kind": "each-point"}}
            ],
            "recommend": {"rule": "static-index", "index": 0},
        }
        with TemporaryDirectory() as directory:
            path = _write(
                Path(directory),
                {"schema_version": 1, "target": "t", "layouts": {"a": layout}},
            )
            with self.assertRaises(ManifestError):
                load_control_manifest(path)


class ManifestScannerTests(unittest.TestCase):
    def test_unknown_extractor_kind_fails_closed_at_construction(self) -> None:
        manifest = {
            "schema_version": 1,
            "target": "t",
            "layouts": {
                "a": {
                    "controls": [
                        {"id": "c", "action_kind": "click", "extractor": {"kind": "no-such-extractor"}}
                    ],
                    "recommend": {"rule": "none"},
                }
            },
        }
        with self.assertRaises(ManifestError):
            ManifestControlScanner(manifest)

    def test_target_registered_extractor_yields_indexed_controls(self) -> None:
        manifest = {
            "schema_version": 1,
            "target": "t",
            "layouts": {
                "grid": {
                    "controls": [
                        {"id": "node", "action_kind": "click", "extractor": {"kind": "each-point"}}
                    ],
                    "recommend": {"rule": "static-index", "index": 0},
                }
            },
        }

        def each_point(frame, params, context):
            return tuple((i, 10 * i, 20 * i, 0.9) for i in range(3))

        scanner = ManifestControlScanner(manifest, {"each-point": each_point})
        controls = scanner.controls("grid", frame=None)
        self.assertEqual([c.control_id for c in controls], ["node-0", "node-1", "node-2"])
        self.assertEqual(scanner.recommend("grid", controls), "node-0")

    def test_builtin_each_point_uses_context_points(self) -> None:
        manifest = {
            "schema_version": 1,
            "target": "t",
            "layouts": {
                "a": {
                    "controls": [
                        {
                            "id": "opt",
                            "action_kind": "click",
                            "extractor": {"kind": "each-point", "source": "pts", "confidence": 0.95},
                        }
                    ],
                    "recommend": {"rule": "static-index", "index": 0},
                }
            },
        }
        scanner = ManifestControlScanner(manifest)
        controls = scanner.controls("a", frame=None, context={"pts": [(10, 20), (30, 40)]})
        self.assertEqual(
            controls,
            (Control("opt-0", "click", 10, 20, 0.95), Control("opt-1", "click", 30, 40, 0.95)),
        )
        self.assertEqual(scanner.recommend("a", controls), "opt-0")
        # No candidates -> no controls, nothing recommended (fail-safe).
        empty = scanner.controls("a", frame=None, context={"pts": []})
        self.assertEqual(empty, ())
        self.assertIsNone(scanner.recommend("a", empty))

    def test_context_point_by_id_and_start_index(self) -> None:
        manifest = {
            "schema_version": 1,
            "target": "t",
            "layouts": {
                "menu": {
                    "controls": [
                        {"id": "back", "action_kind": "click",
                         "extractor": {"kind": "context-point", "source": "back_pt"}},
                        {"id": "chapter", "action_kind": "click",
                         "extractor": {"kind": "each-point", "source": "chapters", "start_index": 3}},
                    ],
                    "recommend": {"rule": "by-id", "id": "back"},
                }
            },
        }
        scanner = ManifestControlScanner(manifest)
        controls = scanner.controls(
            "menu", frame=None, context={"back_pt": (5, 6), "chapters": [(10, 10), (20, 20)]}
        )
        self.assertEqual(
            [c.control_id for c in controls], ["back", "chapter-3", "chapter-4"]
        )
        self.assertEqual(controls[0], Control("back", "click", 5, 6, 1.0))
        self.assertEqual(scanner.recommend("menu", controls), "back")

    def test_context_recommend_reads_context_key(self) -> None:
        manifest = {
            "schema_version": 1,
            "target": "t",
            "layouts": {
                "a": {
                    "controls": [
                        {"id": "x", "action_kind": "click",
                         "extractor": {"kind": "fixed-point", "x": 1, "y": 1}}
                    ],
                    "recommend": {"rule": "context", "key": "pick"},
                }
            },
        }
        scanner = ManifestControlScanner(manifest)
        controls = scanner.controls("a", frame=None)
        self.assertEqual(scanner.recommend("a", controls, {"pick": "x"}), "x")
        self.assertIsNone(scanner.recommend("a", controls, {}))

    def test_static_index_out_of_range_recommends_nothing(self) -> None:
        manifest = {
            "schema_version": 1,
            "target": "t",
            "layouts": {
                "a": {
                    "controls": [
                        {"id": "c", "action_kind": "click", "extractor": {"kind": "fixed-point", "x": 1, "y": 1}}
                    ],
                    "recommend": {"rule": "static-index", "index": 5},
                }
            },
        }
        scanner = ManifestControlScanner(manifest)
        controls = scanner.controls("a", frame=None)
        self.assertIsNone(scanner.recommend("a", controls))


if __name__ == "__main__":
    unittest.main()
