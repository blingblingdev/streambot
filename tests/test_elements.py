"""Synthetic fixture tests for the declarative element locator."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from streambot.config import ConfigurationError
from streambot.elements import (
    ElementResolver,
    load_declaration,
)

FRAME_H, FRAME_W = 200, 320


def cross(tint: tuple[int, int, int]) -> np.ndarray:
    """A 24x24 BGR control: a bright cross on a darker field of one tint."""

    b, g, r = tint
    template = np.zeros((24, 24, 3), dtype=np.uint8)
    template[:, :] = (b // 4, g // 4, r // 4)
    template[10:14, 2:22] = (b, g, r)
    template[2:22, 10:14] = (b, g, r)
    return template


def glyph_mask() -> np.ndarray:
    """The same cross as a 2D binary mask."""

    mask = np.zeros((24, 24), dtype=np.uint8)
    mask[10:14, 2:22] = 255
    mask[2:22, 10:14] = 255
    return mask


def white_cross_on(background: tuple[int, int, int]) -> np.ndarray:
    """A white cross drawn over an arbitrary background colour."""

    patch = np.zeros((24, 24, 3), dtype=np.uint8)
    patch[:, :] = background
    patch[10:14, 2:22] = (255, 255, 255)
    patch[2:22, 10:14] = (255, 255, 255)
    return patch


def frame_with(*placements: tuple[np.ndarray, int, int]) -> np.ndarray:
    """A dark frame with patches stamped at (x, y)."""

    frame = np.full((FRAME_H, FRAME_W, 3), 12, dtype=np.uint8)
    for patch, x, y in placements:
        frame[y : y + patch.shape[0], x : x + patch.shape[1]] = patch
    return frame


GOLD = (40, 180, 220)
BLUE = (220, 180, 40)
# Same cross, desaturated towards grey: measured 0.892 ccoeff against the gold
# template — over the 0.85 threshold, so shape alone accepts it — while its
# colour signature sits 104 away, well outside the 50 tolerance. This is the
# real failure mode (a disabled or alternate-action twin of a control), and
# the only thing that separates the two is the colour gate.
MUTED = (160, 180, 160)


def write_declaration(
    directory: Path,
    *,
    templates: dict[str, np.ndarray],
    declaration: dict,
) -> Path:
    assets = directory / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for name, template in templates.items():
        np.save(assets / f"{name}.npy", template, allow_pickle=False)
    path = directory / "elements.json"
    path.write_text(json.dumps(declaration), encoding="utf-8")
    return path


def simple_declaration() -> dict:
    """One gold control on 'home', one blue control on 'away'."""

    return {
        "schema_version": 1,
        "screens": {
            "home": {"anchors": [{"template": "gold", "y_band": [40, 80]}]},
            "away": {"anchors": [{"template": "blue", "y_band": [120, 160]}]},
        },
        "elements": {
            "start": {"template": "gold", "screen": "home", "y_band": [40, 80]},
            "back": {"template": "blue", "screen": "away", "y_band": [120, 160]},
        },
    }


class DeclarationValidationTests(unittest.TestCase):
    def _load(self, declaration: dict, templates: dict[str, np.ndarray] | None = None):
        templates = templates or {"gold": cross(GOLD), "blue": cross(BLUE)}
        with TemporaryDirectory() as directory:
            path = write_declaration(
                Path(directory), templates=templates, declaration=declaration
            )
            return load_declaration(path)

    def test_valid_declaration_loads_its_templates(self) -> None:
        declaration = self._load(simple_declaration())
        self.assertEqual(sorted(declaration.elements), ["back", "start"])
        self.assertEqual(sorted(declaration.templates), ["blue", "gold"])
        self.assertEqual([region.name for region in declaration.regions], ["frame"])
        self.assertIn("template_bytes", declaration.summary())

    def test_unknown_key_is_rejected(self) -> None:
        spec = simple_declaration()
        spec["elements"]["start"]["colour"] = "gold"
        with self.assertRaises(ConfigurationError):
            self._load(spec)

    def test_element_must_name_a_declared_screen(self) -> None:
        spec = simple_declaration()
        spec["elements"]["start"]["screen"] = "nowhere"
        with self.assertRaises(ConfigurationError):
            self._load(spec)

    def test_missing_template_file_is_rejected(self) -> None:
        spec = simple_declaration()
        with self.assertRaises(ConfigurationError):
            self._load(spec, templates={"gold": cross(GOLD)})

    def test_non_uint8_template_is_rejected(self) -> None:
        spec = simple_declaration()
        with self.assertRaises(ConfigurationError):
            self._load(
                spec,
                templates={
                    "gold": cross(GOLD).astype(np.float32),
                    "blue": cross(BLUE),
                },
            )

    def test_glyph_element_requires_a_mask(self) -> None:
        spec = simple_declaration()
        spec["elements"]["start"]["glyph"] = True
        with self.assertRaises(ConfigurationError):
            self._load(spec)

    def test_bgr_element_rejects_a_mask(self) -> None:
        spec = simple_declaration()
        with self.assertRaises(ConfigurationError):
            self._load(spec, templates={"gold": glyph_mask(), "blue": cross(BLUE)})

    def test_band_must_increase(self) -> None:
        spec = simple_declaration()
        spec["elements"]["start"]["y_band"] = [80, 40]
        with self.assertRaises(ConfigurationError):
            self._load(spec)


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        path = write_declaration(
            Path(self._directory.name),
            templates={"gold": cross(GOLD), "blue": cross(BLUE)},
            declaration=simple_declaration(),
        )
        self.resolver = ElementResolver(load_declaration(path))

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_element_resolves_at_its_centre(self) -> None:
        frame = frame_with((cross(GOLD), 100, 50))
        instances = self.resolver.resolve(frame, "start")
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].center, (112, 62))
        self.assertGreater(instances[0].score, 0.99)
        self.assertEqual(instances[0].region, "frame")

    def test_colour_gate_rejects_a_shape_alike_in_the_wrong_colour(self) -> None:
        frame = frame_with((cross(MUTED), 100, 50))
        raw = self.resolver._match(frame, self.resolver.declaration.templates["gold"])
        self.assertGreater(
            float(raw.max()),
            self.resolver.declaration.settings.threshold,
            "shape alone should clear the threshold — the gate must do the work",
        )
        self.assertEqual(
            self.resolver.resolve(frame, "start", screens={"frame": "home"}), []
        )

    def test_min_rb_skips_a_control_that_has_lost_its_colour(self) -> None:
        # A used control keeps its shape but desaturates; clicking it again
        # does nothing, so an actionable-only lower bound must skip it.
        spec = simple_declaration()
        # Gold sits at R-B 104, the faded twin at 69: the bound goes between.
        spec["elements"]["start"]["min_rb"] = 85.0
        with TemporaryDirectory() as directory:
            path = write_declaration(
                Path(directory),
                templates={"gold": cross(GOLD), "blue": cross(BLUE)},
                declaration=spec,
            )
            resolver = ElementResolver(load_declaration(path))
        faded = cross((90, 180, 210))  # ccoeff 0.984, signature R-B 69
        self.assertEqual(
            resolver.resolve(
                frame_with((faded, 100, 50)), "start", screens={"frame": "home"}
            ),
            [],
        )
        self.assertEqual(
            len(
                resolver.resolve(
                    frame_with((cross(GOLD), 100, 50)),
                    "start",
                    screens={"frame": "home"},
                )
            ),
            1,
        )

    def test_every_element_resolves_zero_on_a_foreign_screen(self) -> None:
        frame = frame_with((cross(BLUE), 100, 130))
        self.assertEqual(self.resolver.classify(frame), {"frame": "away"})
        self.assertEqual(self.resolver.resolve(frame, "start"), [])

    def test_repeated_instances_are_suppressed_to_one_each(self) -> None:
        frame = frame_with((cross(GOLD), 60, 50), (cross(GOLD), 200, 50))
        instances = self.resolver.resolve(frame, "start", screens={"frame": "home"})
        self.assertEqual(len(instances), 2)
        self.assertEqual(
            sorted(instance.center[0] for instance in instances), [72, 212]
        )

    def test_unknown_element_fails_closed(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.resolver.resolve(frame_with(), "nope")

    def test_frame_must_be_bgr_uint8(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.resolver.classify(np.zeros((10, 10), dtype=np.uint8))

    def test_control_outside_its_band_is_still_found(self) -> None:
        # The band is a fast-path guess; a shifted layout must still resolve.
        frame = frame_with((cross(GOLD), 100, 150))
        instances = self.resolver.resolve(frame, "start", screens={"frame": "home"})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].center, (112, 162))


class GlyphTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        declaration = {
            "screens": {
                "home": {
                    "anchors": [
                        {"template": "mark", "y_band": [40, 80], "min_score": 0.7}
                    ]
                }
            },
            "elements": {
                "mark": {
                    "template": "mark",
                    "screen": "home",
                    "y_band": [40, 80],
                    "threshold": 0.7,
                    "glyph": True,
                }
            },
        }
        path = write_declaration(
            Path(self._directory.name),
            templates={"mark": glyph_mask()},
            declaration=declaration,
        )
        self.resolver = ElementResolver(load_declaration(path))

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_glyph_matches_over_any_background(self) -> None:
        # The whole point: the same white icon over different scenery.
        for background in ((0, 0, 0), GOLD, (120, 90, 60)):
            with self.subTest(background=background):
                frame = frame_with((white_cross_on(background), 100, 50))
                instances = self.resolver.resolve(frame, "mark")
                self.assertEqual(len(instances), 1, f"missed on {background}")
                self.assertEqual(instances[0].center, (112, 62))
                self.assertGreater(instances[0].score, 0.95)


class RegionTests(unittest.TestCase):
    """Two independent clients side by side must classify separately."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        declaration = simple_declaration()
        declaration["regions"] = {
            "left": {"x_range": [0, 160]},
            "right": {"x_range": [160, 320]},
        }
        path = write_declaration(
            Path(self._directory.name),
            templates={"gold": cross(GOLD), "blue": cross(BLUE)},
            declaration=declaration,
        )
        self.resolver = ElementResolver(load_declaration(path))

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_regions_classify_independently(self) -> None:
        frame = frame_with((cross(GOLD), 60, 50), (cross(BLUE), 220, 130))
        self.assertEqual(
            self.resolver.classify(frame), {"left": "home", "right": "away"}
        )

    def test_element_resolves_only_in_the_region_showing_its_screen(self) -> None:
        frame = frame_with((cross(GOLD), 60, 50), (cross(GOLD), 220, 50))
        screens = {"left": "home", "right": "away"}
        instances = self.resolver.resolve(frame, "start", screens=screens)
        self.assertEqual([instance.region for instance in instances], ["left"])


class AnalyzeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        path = write_declaration(
            Path(self._directory.name),
            templates={"gold": cross(GOLD), "blue": cross(BLUE)},
            declaration=simple_declaration(),
        )
        self.resolver = ElementResolver(load_declaration(path))

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_analyze_reports_screen_instances_and_its_own_cost(self) -> None:
        frame = frame_with((cross(GOLD), 100, 50))
        analysis = self.resolver.analyze(frame)
        self.assertEqual(analysis.screen, "home")
        self.assertEqual([i.element for i in analysis.instances], ["start"])
        self.assertGreaterEqual(analysis.classify_ms, 0.0)
        self.assertGreaterEqual(analysis.resolve_ms, 0.0)
        payload = analysis.as_dict()
        self.assertEqual(payload["screen"], "home")
        self.assertEqual(payload["instances"][0]["center"], [112, 62])

    def test_analyze_skips_elements_whose_screen_is_not_showing(self) -> None:
        frame = frame_with((cross(GOLD), 100, 50))
        analysis = self.resolver.analyze(frame, elements=["back"])
        self.assertEqual(analysis.instances, [])

    def test_analyze_rejects_an_unknown_element(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.resolver.analyze(frame_with(), elements=["nope"])

    def test_unknown_screen_yields_no_instances(self) -> None:
        analysis = self.resolver.analyze(frame_with())
        self.assertIsNone(analysis.screen)
        self.assertEqual(analysis.instances, [])


if __name__ == "__main__":
    unittest.main()
