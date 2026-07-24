"""Tests for direct-host vs mDNS-discovery connection routing."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
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


class _FakeHTTP:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def get_app_list(self):
        return []


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


if __name__ == "__main__":
    unittest.main()
