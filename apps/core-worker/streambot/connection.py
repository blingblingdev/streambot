"""Protected identity loading and metadata-suppressed Sunshine connection."""

from __future__ import annotations

import contextlib
import io
import os
from contextlib import contextmanager
from pathlib import Path

from moonlight_python import connect_to_server
from moonlight_python.http_client import NvHTTP

from .config import AutomationProfile
from .observation import AutomationMoonlightClient


IDENTITY_FILES = ("key.pem", "cert.pem", "unique_id")


@contextmanager
def owner_only_umask():
    """Apply owner-only file creation permissions and restore caller state."""

    previous_umask = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous_umask)


def protect_state_directory(state_dir: Path) -> None:
    """Create the identity directory and enforce owner-only access."""

    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    for name in IDENTITY_FILES:
        path = state_dir / name
        if path.exists():
            path.chmod(0o600)


def connect_paired_worker(
    profile: AutomationProfile, state_dir: Path, *, host: str | None = None
) -> AutomationMoonlightClient:
    """Connect one paired worker without emitting host or identity metadata.

    When ``host`` is provided the worker connects to that address directly and
    skips mDNS discovery, which is required on networks where the host does not
    advertise over multicast. The address is never written to output.
    """

    with owner_only_umask():
        protect_state_directory(state_dir)
        client = AutomationMoonlightClient(
            config_dir=state_dir, observation=profile.observation
        )
        protect_state_directory(state_dir)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        if host:
            server = connect_to_server(host, client._identity)
        else:
            servers = client.discover(timeout=5.0)
            if len(servers) != 1:
                raise RuntimeError("Expected exactly one visible Sunshine host")
            server = servers[0]
    http = NvHTTP(
        server.address,
        client._identity,
        http_port=server.http_port,
        https_port=server.https_port,
    )
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        http.get_app_list()
    client._server = server
    client._http = http
    return client


def desktop_session_is_active(client: AutomationMoonlightClient) -> bool:
    """Return only whether the configured host currently exposes active Desktop."""

    http = client._get_http()
    apps = http.get_app_list()
    desktop = next((app for app in apps if app.name.casefold() == "desktop"), None)
    if desktop is None:
        return False
    info = http.parse_server_info(http.get_server_info(use_https=True))
    return info.current_game == desktop.id
