#!/usr/bin/env python3
"""Replay a scene manifest against captured frame fixtures, offline.

For every image under the fixture directory (recursively), the manifest is
executed by `SceneEngine` and the classification, control ids, and
recommendation are reported. Ambiguity is surfaced explicitly: when
`--expect <scene-id>` is given (or the fixture's parent directory name matches
a scene id), mismatches are counted as failures so a detector change is proven
against past captures before any live session.

Output is metadata-only by default (scene ids, control ids, scores); recognized
text is never printed unless `--show-text` is passed for local calibration.

When the manifest belongs to a target whose directory holds a
`scene_extensions.py` exposing `build_scene_engine`, the engine is built
through it so `x-` extension kinds resolve exactly as they do live; pass
`--bare` to force the plain declarative engine instead.

Usage:
  scripts/replay_manifest.py jobs/<t>/profiles/scene-manifest.json .fixtures/phone-reply \
      [--expect phone-reply] [--ocr] [--show-text] [--bare]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

if sys.prefix == sys.base_prefix:
    if not VENV_PYTHON.is_file():
        raise SystemExit("Project environment is missing; run ./scripts/bootstrap.sh first")
    os.execv(
        VENV_PYTHON,
        [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
    )

sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

import numpy as np  # noqa: E402

from streambot.scene import SceneEngine, load_scene_manifest  # noqa: E402

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}


def _load_image(path: Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    return rgb[:, :, ::-1].copy()  # BGR for the engine


def _load_templates(manifest_path: Path, manifest: dict) -> dict[str, np.ndarray]:
    names: set[str] = set()
    for scene in manifest["scenes"].values():
        for predicate in scene["detect"]["predicates"]:
            if predicate["kind"] == "template":
                names.add(predicate["template"])
        for control in scene.get("controls", []):
            if control["extractor"]["kind"] == "template-grid":
                names.add(control["extractor"]["template"])
    templates: dict[str, np.ndarray] = {}
    for name in names:
        template_path = (manifest_path.parent / name).resolve()
        templates[name] = np.load(template_path, allow_pickle=False)
    return templates


def _build_engine(manifest_path: Path, ocr, bare: bool):
    """Build the engine through the target's extension registry when present.

    `build_scene_engine` wires the target's `x-` detector/extractor kinds; a
    bare `SceneEngine` rejects manifests referencing them, which previously
    made offline replay impossible for the real target manifest.
    """

    target_dir = manifest_path.resolve().parent.parent
    extensions_path = target_dir / "scene_extensions.py"
    if bare or not extensions_path.is_file():
        manifest = load_scene_manifest(manifest_path)
        templates = _load_templates(manifest_path, manifest)
        return SceneEngine(manifest, templates=templates, ocr=ocr), "bare"
    if str(target_dir) not in sys.path:
        sys.path.insert(0, str(target_dir))
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"{target_dir.name.replace('-', '_')}_scene_extensions", extensions_path
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load extensions module: {extensions_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_scene_engine(ocr=ocr), "extensions"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("fixtures", type=Path)
    parser.add_argument("--expect", default=None)
    parser.add_argument(
        "--expect-from-dirname",
        action="store_true",
        help="Treat each fixture's parent directory name as its expected scene id.",
    )
    parser.add_argument("--ocr", action="store_true", help="Inject the RapidOCR adapter.")
    parser.add_argument(
        "--ocr-timeout",
        type=float,
        default=60.0,
        help="Per-request OCR worker timeout in seconds; the first request"
        " pays the model cold start, so replay defaults far above the"
        " runtime's 10s.",
    )
    parser.add_argument("--show-text", action="store_true")
    parser.add_argument(
        "--bare",
        action="store_true",
        help="Ignore any target scene_extensions.py and build a plain engine.",
    )
    args = parser.parse_args()

    ocr = None
    if args.ocr:
        from streambot.ocr import RapidOcrAdapter, SubprocessOcrWorker

        ocr = RapidOcrAdapter(
            worker_factory=lambda: SubprocessOcrWorker(
                timeout_seconds=args.ocr_timeout
            )
        )
    engine, engine_kind = _build_engine(args.manifest, ocr, args.bare)

    images = sorted(
        path
        for path in args.fixtures.rglob("*")
        if path.suffix.lower() in IMAGE_SUFFIXES
    )
    if not images:
        print(json.dumps({"ok": False, "error": "NoFixturesFound"}))
        return 1

    results = []
    mismatches = 0
    try:
        for index, path in enumerate(images):
            engine.reset()
            frame = _load_image(path)
            expected = args.expect
            if expected is None and args.expect_from_dirname:
                expected = path.parent.name
            try:
                facts = engine.observe(frame, index)
            except Exception as exc:  # one bad frame must not kill the run
                mismatches += 1
                results.append(
                    {
                        "fixture": str(path.relative_to(args.fixtures)),
                        "error": type(exc).__name__,
                        "expected": expected,
                        "matched": False,
                    }
                )
                continue
            matched = expected is None or facts.scene_id == expected
            if not matched:
                mismatches += 1
            entry = {
                "fixture": str(path.relative_to(args.fixtures)),
                "scene_id": facts.scene_id,
                "control_ids": [control.control_id for control in facts.controls],
                "recommended_id": facts.recommended_id,
                "scores": {k: round(v, 4) for k, v in facts.scores.items()},
                "expected": expected,
                "matched": matched,
            }
            if args.show_text:
                entry["control_texts"] = [
                    control.text for control in facts.controls if control.text
                ]
            results.append(entry)
    finally:
        if ocr is not None:
            ocr.close()

    print(
        json.dumps(
            {
                "ok": mismatches == 0,
                "engine": engine_kind,
                "fixtures": len(images),
                "mismatches": mismatches,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
