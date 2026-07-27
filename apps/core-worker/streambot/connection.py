"""Protected identity loading and metadata-suppressed Sunshine connection."""

from __future__ import annotations

import contextlib
import errno
import io
import os
from contextlib import contextmanager
from pathlib import Path

from moonlight_python import connect_to_server
from moonlight_python.http_client import NvHTTP

from .config import AutomationProfile
from .observation import AutomationMoonlightClient


IDENTITY_FILES = ("key.pem", "cert.pem", "unique_id")


class ConnectFailure(RuntimeError):
    """Classified, metadata-safe connection failure.

    ``code`` is a short allowlisted identifier safe to publish in health
    output. ``environmental`` marks conditions where the surrounding world is
    not ready (host asleep, session not started): the worker should wait and
    re-check instead of consuming its reconnect failure budget.
    """

    code = "connect_error"
    environmental = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class NoHostVisible(ConnectFailure):
    """Discovery returned zero Sunshine hosts (host offline or LAN denied)."""

    code = "no_host_visible"
    environmental = True


class MultipleHostsVisible(ConnectFailure):
    """Discovery returned more than one host; refusing to guess."""

    code = "multiple_hosts_visible"


class HostUnreachable(ConnectFailure):
    """A LAN connection was denied or unroutable (suspect Local Network TCC)."""

    code = "host_unreachable"
    environmental = True


class DesktopSessionInactive(ConnectFailure):
    """The host has no active Desktop session and launching is not allowed.

    Raised only by callers that opt out of session management; the managed
    worker path launches Desktop itself when the host is idle.
    """

    code = "desktop_session_inactive"
    environmental = True


class HostSessionBusy(ConnectFailure):
    """Another application session is active; the worker must not displace it."""

    code = "host_session_busy"
    environmental = True


class DesktopAppMissing(ConnectFailure):
    """The host exposes no Desktop application; a configuration problem."""

    code = "desktop_app_missing"


_UNREACHABLE_ERRNOS = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN}


@contextmanager
def _classify_reachability():
    """Re-raise routing/permission socket errors as HostUnreachable."""

    try:
        yield
    except OSError as error:
        if error.errno in _UNREACHABLE_ERRNOS:
            raise HostUnreachable() from error
        raise


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
    profile: AutomationProfile,
    state_dir: Path,
    *,
    host: str | None = None,
    manage_desktop_session: bool = False,
) -> AutomationMoonlightClient:
    """Connect one paired worker without emitting host or identity metadata.

    When ``host`` is provided the worker connects to that address directly and
    skips mDNS discovery, which is required on networks where the host does not
    advertise over multicast. The address is never written to output.

    Failures are classified as :class:`ConnectFailure` subclasses so callers
    can distinguish environmental wait states (host asleep, host busy) from
    real errors without string matching.

    ``manage_desktop_session`` applies the worker session policy at connect
    time: an active Desktop session is joined; an idle host (no application
    session at all) permits stream setup to launch Desktop, because nothing
    pre-existing can be displaced; another application's active session is a
    :class:`HostSessionBusy` environmental wait. Quitting host sessions stays
    forbidden unconditionally. Probes that connect first and report session
    state themselves keep the default ``False``.
    """

    with owner_only_umask():
        protect_state_directory(state_dir)
        client = AutomationMoonlightClient(
            config_dir=state_dir, observation=profile.observation
        )
        protect_state_directory(state_dir)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ), _classify_reachability():
        if host:
            server = connect_to_server(host, client._identity)
        else:
            servers = client.discover(timeout=5.0)
            if len(servers) == 0:
                raise NoHostVisible("Expected exactly one visible Sunshine host")
            if len(servers) > 1:
                raise MultipleHostsVisible(
                    "Expected exactly one visible Sunshine host"
                )
            server = servers[0]
    http = NvHTTP(
        server.address,
        client._identity,
        http_port=server.http_port,
        https_port=server.https_port,
    )
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ), _classify_reachability():
        apps = http.get_app_list()
    client._server = server
    client._http = http
    if manage_desktop_session:
        desktop = next(
            (app for app in apps if app.name.casefold() == "desktop"), None
        )
        if desktop is None:
            raise DesktopAppMissing()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ), _classify_reachability():
            info = http.parse_server_info(http.get_server_info(use_https=True))
        current_game = info.current_game or 0
        if current_game == desktop.id:
            client.allow_session_launch = False
        elif current_game == 0:
            # Idle host: stream setup may launch Desktop; nothing pre-existing
            # can be displaced, and quitting remains blocked regardless.
            client.allow_session_launch = True
        else:
            raise HostSessionBusy()
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
