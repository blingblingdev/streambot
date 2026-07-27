"""Tests for connection routing and typed failure classification."""

from __future__ import annotations

import errno
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

import streambot.connection as conn


class _FakeServer:
    address = "host"
    http_port = 47989
    https_port = 47984


class _FakeClient:
    def __init__(self, config_dir, observation) -> None:
        self._identity = object()
        self._server = None
        self._http = None

    def discover(self, timeout: float = 5.0):
        raise AssertionError("discover must not be called when a host is given")

    def _get_http(self):
        return self._http


class _FakeHTTP:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def get_app_list(self):
        return []


class _SessionHTTP(_FakeHTTP):
    """Fake NvHTTP exposing a Desktop app and a configurable current game."""

    current_game = 0

    def get_app_list(self):
        return [SimpleNamespace(name="Desktop", id=7)]

    def get_server_info(self, use_https: bool = True):
        return "<root/>"

    def parse_server_info(self, raw):
        return SimpleNamespace(current_game=type(self).current_game)


class _FakeProfile:
    observation = object()


class DirectConnectionRoutingTests(unittest.TestCase):
    def test_host_uses_connect_to_server_and_skips_discovery(self) -> None:
        seen: dict[str, object] = {}

        def fake_connect_to_server(host, identity, *args, **kwargs):
            seen["host"] = host
            return _FakeServer()

        with TemporaryDirectory() as directory, mock.patch.object(
            conn, "AutomationMoonlightClient", _FakeClient
        ), mock.patch.object(
            conn, "connect_to_server", fake_connect_to_server
        ), mock.patch.object(conn, "NvHTTP", _FakeHTTP):
            client = conn.connect_paired_worker(
                _FakeProfile(), Path(directory), host="192.0.2.10"
            )

        self.assertEqual(seen["host"], "192.0.2.10")
        self.assertIsInstance(client._server, _FakeServer)

    def test_no_host_uses_discovery(self) -> None:
        class DiscoverClient(_FakeClient):
            def discover(self, timeout: float = 5.0):
                return [_FakeServer()]

        with TemporaryDirectory() as directory, mock.patch.object(
            conn, "AutomationMoonlightClient", DiscoverClient
        ), mock.patch.object(conn, "NvHTTP", _FakeHTTP):
            client = conn.connect_paired_worker(_FakeProfile(), Path(directory))

        self.assertIsInstance(client._server, _FakeServer)

    def test_discovery_requires_exactly_one_host(self) -> None:
        class NoHostClient(_FakeClient):
            def discover(self, timeout: float = 5.0):
                return []

        with TemporaryDirectory() as directory, mock.patch.object(
            conn, "AutomationMoonlightClient", NoHostClient
        ), mock.patch.object(conn, "NvHTTP", _FakeHTTP):
            with self.assertRaises(RuntimeError):
                conn.connect_paired_worker(_FakeProfile(), Path(directory))


class FailureClassificationTests(unittest.TestCase):
    def test_zero_hosts_is_a_typed_environmental_wait(self) -> None:
        class NoHostClient(_FakeClient):
            def discover(self, timeout: float = 5.0):
                return []

        with TemporaryDirectory() as directory, mock.patch.object(
            conn, "AutomationMoonlightClient", NoHostClient
        ), mock.patch.object(conn, "NvHTTP", _FakeHTTP):
            with self.assertRaises(conn.NoHostVisible) as caught:
                conn.connect_paired_worker(_FakeProfile(), Path(directory))
        self.assertEqual(caught.exception.code, "no_host_visible")
        self.assertTrue(caught.exception.environmental)

    def test_multiple_hosts_is_typed_and_not_environmental(self) -> None:
        class TwoHostClient(_FakeClient):
            def discover(self, timeout: float = 5.0):
                return [_FakeServer(), _FakeServer()]

        with TemporaryDirectory() as directory, mock.patch.object(
            conn, "AutomationMoonlightClient", TwoHostClient
        ), mock.patch.object(conn, "NvHTTP", _FakeHTTP):
            with self.assertRaises(conn.MultipleHostsVisible) as caught:
                conn.connect_paired_worker(_FakeProfile(), Path(directory))
        self.assertFalse(caught.exception.environmental)

    def test_unreachable_socket_error_maps_to_host_unreachable(self) -> None:
        class DeniedClient(_FakeClient):
            def discover(self, timeout: float = 5.0):
                raise OSError(errno.EHOSTUNREACH, "no route to host")

        with TemporaryDirectory() as directory, mock.patch.object(
            conn, "AutomationMoonlightClient", DeniedClient
        ), mock.patch.object(conn, "NvHTTP", _FakeHTTP):
            with self.assertRaises(conn.HostUnreachable) as caught:
                conn.connect_paired_worker(_FakeProfile(), Path(directory))
        self.assertEqual(caught.exception.code, "host_unreachable")
        self.assertTrue(caught.exception.environmental)

    def test_other_socket_errors_stay_unclassified(self) -> None:
        class BrokenClient(_FakeClient):
            def discover(self, timeout: float = 5.0):
                raise OSError(errno.ECONNRESET, "reset")

        with TemporaryDirectory() as directory, mock.patch.object(
            conn, "AutomationMoonlightClient", BrokenClient
        ), mock.patch.object(conn, "NvHTTP", _FakeHTTP):
            with self.assertRaises(OSError) as caught:
                conn.connect_paired_worker(_FakeProfile(), Path(directory))
        self.assertNotIsInstance(caught.exception, conn.ConnectFailure)


class DesktopSessionManagementTests(unittest.TestCase):
    def _connect(self, *, current_game: int, manage: bool, http_cls=None):
        class DiscoverClient(_FakeClient):
            def discover(self, timeout: float = 5.0):
                return [_FakeServer()]

        _SessionHTTP.current_game = current_game
        with TemporaryDirectory() as directory, mock.patch.object(
            conn, "AutomationMoonlightClient", DiscoverClient
        ), mock.patch.object(conn, "NvHTTP", http_cls or _SessionHTTP):
            return conn.connect_paired_worker(
                _FakeProfile(),
                Path(directory),
                manage_desktop_session=manage,
            )

    def test_active_desktop_session_is_joined_without_launch(self) -> None:
        client = self._connect(current_game=7, manage=True)
        self.assertIsNotNone(client._http)
        self.assertFalse(client.allow_session_launch)

    def test_idle_host_permits_a_proactive_desktop_launch(self) -> None:
        client = self._connect(current_game=0, manage=True)
        self.assertIsNotNone(client._http)
        self.assertTrue(client.allow_session_launch)

    def test_other_active_session_is_a_typed_environmental_wait(self) -> None:
        with self.assertRaises(conn.HostSessionBusy) as caught:
            self._connect(current_game=99, manage=True)
        self.assertEqual(caught.exception.code, "host_session_busy")
        self.assertTrue(caught.exception.environmental)

    def test_missing_desktop_app_is_a_real_error_not_a_wait(self) -> None:
        class NoDesktopHTTP(_SessionHTTP):
            def get_app_list(self):
                return [SimpleNamespace(name="OtherApp", id=3)]

        with self.assertRaises(conn.DesktopAppMissing) as caught:
            self._connect(current_game=0, manage=True, http_cls=NoDesktopHTTP)
        self.assertFalse(caught.exception.environmental)

    def test_probes_connect_without_session_management_by_default(self) -> None:
        client = self._connect(current_game=0, manage=False)
        self.assertIsNotNone(client._http)
        self.assertFalse(getattr(client, "allow_session_launch", False))


if __name__ == "__main__":
    unittest.main()
