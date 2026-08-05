"""Tests for how the console serves its page and built assets."""

from __future__ import annotations

import importlib.util
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "core-worker"))

_spec = importlib.util.spec_from_file_location(
    "control_panel_server_static",
    PROJECT_ROOT / "apps" / "control-panel" / "server.py",
)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


class StaticRouteTests(unittest.TestCase):
    """The console serves a built bundle, so it needs a real static route.

    A hashed asset name cannot be enumerated in advance, which is why this is
    a directory handler rather than the two hardcoded files it replaced — and
    why the confinement test below matters.
    """

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.static = Path(self._directory.name) / "static"
        (self.static / "assets").mkdir(parents=True)
        (self.static / "index.html").write_text("<!doctype html>ok", encoding="utf-8")
        (self.static / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
        (self.static / "assets" / "app-abc123.css").write_text(".a{}", encoding="utf-8")
        (self.static / "secret.txt").write_text("not an asset type", encoding="utf-8")
        # A file the console must never reach through a crafted path.
        Path(self._directory.name, "outside.js").write_text("stolen", encoding="utf-8")

        self._patch = mock.patch.object(server, "STATIC_DIR", self.static)
        self._patch.start()

        handler = type("BoundHandler", (server.Handler,), {"console": None})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2.0)
        self._patch.stop()
        self._directory.cleanup()

    def get(self, path: str):
        request = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read(), dict(error.headers)

    def test_root_serves_the_page(self) -> None:
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))

    def test_index_html_serves_the_page(self) -> None:
        self.assertEqual(self.get("/index.html")[0], 200)

    def test_hashed_assets_are_served_with_their_types(self) -> None:
        for path, expected in (
            ("/assets/app-abc123.js", "application/javascript"),
            ("/assets/app-abc123.css", "text/css"),
        ):
            with self.subTest(path=path):
                status, _body, headers = self.get(path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(expected))

    def test_hashed_assets_may_be_cached_but_the_page_may_not(self) -> None:
        # The names carry a content hash, so they are safe to cache forever;
        # the entry document must never be, or a rebuild would not show up.
        self.assertIn("immutable", self.get("/assets/app-abc123.js")[2]["Cache-Control"])
        self.assertEqual(self.get("/")[2]["Cache-Control"], "no-store")

    def test_a_path_that_escapes_the_static_directory_is_refused(self) -> None:
        for path in (
            "/../outside.js",
            "/assets/../../outside.js",
            "/%2e%2e/outside.js",
        ):
            with self.subTest(path=path):
                status, body, _headers = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"stolen", body)

    def test_unknown_asset_types_are_not_served(self) -> None:
        self.assertEqual(self.get("/secret.txt")[0], 404)

    def test_a_missing_asset_is_a_404_not_a_traceback(self) -> None:
        status, body, _headers = self.get("/assets/app-does-not-exist.js")
        self.assertEqual(status, 404)
        self.assertIn(b"NotFound", body)

    def test_unknown_api_paths_still_answer_json(self) -> None:
        status, body, headers = self.get("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn(b"NotFound", body)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))


class PostRoutingTests(unittest.TestCase):
    def test_post_routes_ignore_the_query_string_like_get_does(self) -> None:
        # do_POST used to compare the whole path, so `/api/jobs/start?x=1`
        # fell through to a 404 while the same GET route worked.
        source = (
            PROJECT_ROOT / "apps" / "control-panel" / "server.py"
        ).read_text(encoding="utf-8")
        do_post = source.split("def do_POST")[1].split("def ")[0]
        self.assertIn('route = self.path.split("?", 1)[0]', do_post)
        self.assertNotIn("self.path ==", do_post)


if __name__ == "__main__":
    unittest.main()
