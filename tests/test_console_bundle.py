"""The console's built frontend must be present and current.

The bundle under `apps/control-panel/static/` is committed on purpose: the
console is launched as a plain Python process from a terminal that holds the
macOS Local Network grant the worker inherits, and nothing on that path may
come to require a JavaScript toolchain.

Committing a build artifact buys exactly one new failure mode — editing the
source and forgetting to rebuild — so the suite checks for it rather than
leaving it to be discovered as a browser showing yesterday's console.
"""

from __future__ import annotations

import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONSOLE = PROJECT_ROOT / "apps" / "control-panel"
STATIC = CONSOLE / "static"
UI = CONSOLE / "ui"

# Everything the build reads. `bun.lock` is included because a dependency
# change is a rebuild too.
SOURCE_GLOBS = ("src/**/*.ts", "src/**/*.tsx", "src/**/*.css", "index.html")
SOURCE_FILES = ("build.ts", "package.json", "bun.lock")


def newest_source() -> tuple[Path, float]:
    candidates: list[Path] = []
    for pattern in SOURCE_GLOBS:
        candidates.extend(UI.glob(pattern))
    candidates.extend(UI / name for name in SOURCE_FILES)
    # Tests do not change what is shipped.
    live = [path for path in candidates if path.is_file() and ".test." not in path.name]
    newest = max(live, key=lambda path: path.stat().st_mtime)
    return newest, newest.stat().st_mtime


class BundleTests(unittest.TestCase):
    def test_the_page_and_its_assets_are_committed(self) -> None:
        index = STATIC / "index.html"
        self.assertTrue(index.is_file(), "static/index.html is missing — run bun run build")
        html = index.read_text(encoding="utf-8")
        assets = sorted((STATIC / "assets").glob("*"))
        self.assertTrue(assets, "static/assets is empty — run bun run build")
        for asset in assets:
            self.assertIn(
                f"/assets/{asset.name}",
                html,
                f"{asset.name} is not referenced by the page — stale build output",
            )

    def test_every_asset_the_page_asks_for_exists(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for prefix, suffix in (('src="/assets/', '"'), ('href="/assets/', '"')):
            start = 0
            while (found := html.find(prefix, start)) != -1:
                end = html.index(suffix, found + len(prefix))
                name = html[found + len(prefix) : end]
                self.assertTrue(
                    (STATIC / "assets" / name).is_file(),
                    f"page references {name}, which is not in the build output",
                )
                start = end

    @unittest.skipUnless(UI.is_dir(), "console UI sources not on this machine")
    def test_the_build_is_not_older_than_its_sources(self) -> None:
        newest, source_time = newest_source()
        built = max(
            (path.stat().st_mtime for path in STATIC.rglob("*") if path.is_file()),
            default=0.0,
        )
        self.assertGreaterEqual(
            built,
            source_time,
            f"{newest.relative_to(UI)} is newer than the build output — "
            f"run `bun run build` in apps/control-panel/ui",
        )


if __name__ == "__main__":
    unittest.main()
