"""Tests for the console's cross-origin guard and stale-socket recovery.

The console binds 127.0.0.1 only, but local is not private: any web page the
operator visits can fire cross-origin requests at it, and DNS rebinding puts
an attacker's hostname on a request that still reaches the loopback port.
"""

from __future__ import annotations

import importlib.util
import json
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
    "control_panel_server_guard",
    PROJECT_ROOT / "apps" / "control-panel" / "server.py",
)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


class RecordingJobs:
    def __init__(self) -> None:
        self.started: list[str] = []

    def start(self, name: str) -> dict:
        self.started.append(name)
        return {"ok": True, "name": name}


class FakeConsole:
    def __init__(self) -> None:
        self.jobs = RecordingJobs()
        self.supervisor = None  # worker routes are not exercised here


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.console = FakeConsole()
        handler = type(
            "BoundHandler", (server.Handler,), {"console": self.console}
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2.0)

    def _request(
        self,
        route: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{route}",
            method=method,
            data=body,
        )
        for name, value in (headers or {}).items():
            # add_header would title-case Host away; set verbatim.
            request.headers[name] = value
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")


class CrossOriginGuardTests(GuardTestCase):
    def test_a_rebound_hostname_is_refused(self) -> None:
        status, payload = self._request(
            "/api/status", headers={"Host": "attacker.example:80"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "ForbiddenHost")

    def test_a_cross_site_post_is_refused_before_touching_the_console(self) -> None:
        status, payload = self._request(
            "/api/jobs/start",
            method="POST",
            headers={
                "Origin": "http://attacker.example",
                "Content-Type": "application/json",
            },
            body=b'{"name": "evil"}',
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "ForbiddenOrigin")
        self.assertEqual(self.console.jobs.started, [])

    def test_a_local_origin_on_any_port_is_allowed(self) -> None:
        # The UI dev server proxies from another localhost port.
        status, payload = self._request(
            "/api/jobs/start",
            method="POST",
            headers={
                "Origin": "http://localhost:3000",
                "Content-Type": "application/json",
            },
            body=b'{"name": "sweep"}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.console.jobs.started, ["sweep"])

    def test_no_origin_header_is_a_plain_local_client(self) -> None:
        status, _payload = self._request(
            "/api/jobs/start",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=b'{"name": "sweep"}',
        )
        self.assertEqual(status, 200)

    def test_a_json_shaped_text_plain_body_reads_as_empty(self) -> None:
        # A cross-site form can smuggle this shape without a CORS preflight.
        status, _payload = self._request(
            "/api/jobs/start",
            method="POST",
            headers={"Content-Type": "text/plain"},
            body=b'{"name": "evil"}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.console.jobs.started, [""])


class StaleSocketTests(unittest.TestCase):
    """A leftover socket is stale only once its owner is confirmed gone."""

    def _supervisor(self, directory: Path) -> object:
        return server.WorkerSupervisor(directory, directory / "core-control.sock")

    def test_start_clears_a_socket_with_no_owning_worker(self) -> None:
        with TemporaryDirectory() as name:
            directory = Path(name)
            socket_path = directory / "core-control.sock"
            socket_path.touch()
            supervisor = self._supervisor(directory)
            fake_process = mock.Mock(pid=4321, poll=mock.Mock(return_value=None))
            with (
                mock.patch.object(supervisor, "external_pid", return_value=None),
                mock.patch.object(server, "LOG_DIR", directory / "logs"),
                mock.patch.object(
                    server.subprocess, "Popen", return_value=fake_process
                ) as popen,
            ):
                result = supervisor.start()
            self.assertTrue(result["ok"])
            self.assertFalse(socket_path.exists())
            popen.assert_called_once()

    def test_start_still_refuses_a_socket_a_live_worker_owns(self) -> None:
        with TemporaryDirectory() as name:
            directory = Path(name)
            socket_path = directory / "core-control.sock"
            socket_path.touch()
            supervisor = self._supervisor(directory)
            with (
                mock.patch.object(supervisor, "external_pid", return_value=999_999),
                mock.patch.object(server.subprocess, "Popen") as popen,
            ):
                result = supervisor.start()
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "SocketOwnedElsewhere")
            self.assertTrue(socket_path.exists())
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
