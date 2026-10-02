"""The egress guard: loopback and the named hosts pass, everything else is refused before a packet.

The whole test suite runs behind a guard (``tests/conftest.py``), so a test or a library that
reaches for the network fails here instead of quietly working on a machine that has one.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

import httpx
import pytest
import requests

from procurement_agent.security import egress
from procurement_agent.security.egress import EgressBlockedError, EgressGuard


def test_every_test_runs_behind_a_guard() -> None:
    assert egress.installed() is not None


# --- real sockets, through the guard installed for the session ----------------------------------


def test_a_loopback_connection_is_allowed() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(("127.0.0.1", server.getsockname()[1]), timeout=2):
            pass


def test_localhost_by_name_is_allowed() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(("localhost", server.getsockname()[1]), timeout=2):
            pass


@pytest.mark.parametrize(
    "host",
    [
        "93.184.216.34",
        "203.0.113.7",
        "169.254.169.254",
        "8.8.8.8",
        "2001:4860:4860::8888",
        "0.0.0.0",
    ],
)
def test_a_public_address_is_refused_before_anything_is_sent(host: str) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        with pytest.raises(EgressBlockedError):
            s.connect((host, 80))
        with pytest.raises(EgressBlockedError):
            s.connect_ex((host, 80))


def test_udp_to_a_public_address_is_refused() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(EgressBlockedError):
            s.sendto(b"x", ("8.8.8.8", 53))
        with pytest.raises(EgressBlockedError):
            s.sendto(b"x", 0, ("8.8.8.8", 53))


def test_resolving_a_public_name_is_refused() -> None:
    with pytest.raises(EgressBlockedError):
        socket.getaddrinfo("example.com", 80)
    with pytest.raises(EgressBlockedError):
        socket.gethostbyname("huggingface.co")
    with pytest.raises(EgressBlockedError):
        socket.gethostbyname_ex("example.com")


def test_a_name_handed_straight_to_connect_is_refused() -> None:
    with socket.socket() as s, pytest.raises(EgressBlockedError):
        s.connect(("example.com", 80))


def test_the_libraries_that_would_phone_home_are_covered() -> None:
    with pytest.raises(urllib.error.URLError) as raised:
        urllib.request.urlopen("http://example.com/", timeout=2)
    assert isinstance(raised.value.reason, EgressBlockedError)
    with pytest.raises(httpx.ConnectError):
        httpx.get("http://example.com/", timeout=2)
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get("http://example.com/", timeout=2)


# --- the rules, without sockets ------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.5.5.5", "::1", "::ffff:127.0.0.1", "localhost"])
def test_loopback_in_any_form_is_allowed(host: str) -> None:
    guard = EgressGuard()
    guard.check_address(socket.AF_INET6 if ":" in host else socket.AF_INET, (host, 80))
    guard.check_name(host)


def test_named_hosts_and_the_addresses_they_resolve_to_are_allowed() -> None:
    guard = EgressGuard(["ollama"])
    guard.check_name("ollama")
    guard.check_name("OLLAMA.")
    with pytest.raises(EgressBlockedError):
        guard.check_address(socket.AF_INET, ("10.1.2.3", 11434))  # not yet resolved from the name
    guard._remember([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 11434))])
    guard.check_address(socket.AF_INET, ("10.1.2.3", 11434))
    with pytest.raises(EgressBlockedError):
        guard.check_address(socket.AF_INET, ("10.1.2.4", 11434))  # a neighbour is no friend
    with pytest.raises(EgressBlockedError):
        guard.check_name("ollama.example.com")


def test_unix_sockets_and_other_families_are_not_judged() -> None:
    EgressGuard().check_address(socket.AF_UNIX, "/tmp/some.sock")


def test_a_refusal_is_reported_without_content() -> None:
    events: list[tuple[str, str]] = []
    guard = EgressGuard(on_block=lambda kind, destination: events.append((kind, destination)))
    with pytest.raises(EgressBlockedError, match="nothing leaves this machine"):
        guard.check_name("telemetry.example.com")
    with pytest.raises(EgressBlockedError):
        guard.check_address(socket.AF_INET, ("8.8.8.8", 53), "send")
    assert events == [("lookup", "telemetry.example.com"), ("send", "8.8.8.8")]
    assert guard.blocked == 2


def test_a_reporting_failure_does_not_replace_the_refusal() -> None:
    def broken(kind: str, destination: str) -> None:
        raise RuntimeError("the audit table is gone")

    with pytest.raises(EgressBlockedError):
        EgressGuard(on_block=broken).check_name("example.com")


def test_install_is_idempotent_and_reversible() -> None:
    before = socket.socket.connect
    guard = EgressGuard()
    guard.install()
    guard.install()
    assert socket.socket.connect is not before
    guard.uninstall()
    assert socket.socket.connect is before


def test_ensure_installed_joins_the_guard_that_is_already_there() -> None:
    first = egress.ensure_installed(())
    second = egress.ensure_installed(["joined-host.test"])
    assert first is second
    second.check_name("joined-host.test")


def test_a_model_server_given_by_address_is_allowed_by_that_address_only() -> None:
    guard = EgressGuard(["10.1.2.3", "[fd00::5]"])
    guard.check_name("10.1.2.3")  # what httpcore asks the resolver for
    guard.check_address(socket.AF_INET, ("10.1.2.3", 11434))
    guard.check_address(socket.AF_INET6, ("fd00::5", 11434, 0, 0))
    with pytest.raises(EgressBlockedError):
        guard.check_address(socket.AF_INET, ("10.1.2.4", 11434))


def test_binding_a_server_socket_is_not_egress() -> None:
    """uvicorn binds 0.0.0.0 in the container, via ``getaddrinfo`` with AI_PASSIVE: no network."""
    guard = EgressGuard()
    guard.install()
    try:
        for host in (None, "0.0.0.0", "::", "127.0.0.1"):
            socket.getaddrinfo(host, 0, 0, socket.SOCK_STREAM, 0, socket.AI_PASSIVE)
            socket.getaddrinfo(host, 0, flags=socket.AI_PASSIVE)
        # ...but the flag makes neither an outside name local nor an outside address a destination
        with pytest.raises(EgressBlockedError):
            socket.getaddrinfo("example.com", 0, flags=socket.AI_PASSIVE)
        with pytest.raises(EgressBlockedError):
            socket.getaddrinfo("8.8.8.8", 53)
        with socket.socket() as s:
            s.bind(("0.0.0.0", 0))
            s.listen(1)
    finally:
        guard.uninstall()
