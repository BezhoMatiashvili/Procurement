"""The egress guard: loopback and the named hosts pass, everything else is refused before a packet.

The whole test suite runs behind a guard (``tests/conftest.py``), so a test or a library that
reaches for the network fails here instead of quietly working on a machine that has one.
"""

from __future__ import annotations

import shutil
import socket
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

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


def test_other_families_than_the_internet_and_unix_are_not_judged() -> None:
    EgressGuard().check_address(socket.AF_NETLINK, (0, 0))


CANONICAL = "/run/parser/parser.sock"

#: Every way to write something the kernel would take for CANONICAL, or for something else, that is
#: not CANONICAL spelled exactly. The kernel resolves a path its own way (symlinks first, then
#: ``..``), so a lexical clean-up in the guard would be a different parser from the kernel's.
OTHER_SPELLINGS = [
    "run/parser/parser.sock",
    "./run/parser/parser.sock",
    "parser.sock",
    "/run/parser/../parser/parser.sock",
    "/run/parser/./parser.sock",
    "/run/parser/parser.sock/.",
    "//run/parser/parser.sock",
    "/run//parser/parser.sock",
    "/run/parser/parser.sock/",
    "/run/parser/parser.sock\0",
    "/run/parser\0/parser.sock",
    "\0/run/parser/parser.sock",
    "\0parser",
    "",
    "/RUN/parser/parser.sock",
    " /run/parser/parser.sock",
    "/run/parser/parser.sock ",
    "/run/parser/parser.sock.bak",
    "/run/parser/other.sock",
    "/run/parser",
    "/run/parser/parser.sock/x",
]


def test_a_unix_socket_is_refused_unless_its_exact_path_was_allowed() -> None:
    """The parser's socket is the one Unix address the application may connect to: a deliberate,
    pinned entry (``main.py`` adds it when the parser mode is ``socket``), not "Unix sockets are
    on this machine, so any of them"."""
    guard = EgressGuard()
    for path in ("/tmp/some.sock", b"/tmp/some.sock", "relative.sock", "\0abstract"):
        with pytest.raises(EgressBlockedError):
            guard.check_address(socket.AF_UNIX, path)
    guard.allow_unix(CANONICAL)
    guard.check_address(socket.AF_UNIX, CANONICAL)
    guard.check_address(socket.AF_UNIX, CANONICAL.encode())
    guard.check_address(socket.AF_UNIX, bytearray(CANONICAL.encode()))
    guard.check_address(socket.AF_UNIX, Path(CANONICAL))


@pytest.mark.parametrize("spelling", OTHER_SPELLINGS)
@pytest.mark.parametrize("as_bytes", [False, True])
def test_every_other_spelling_of_the_address_is_refused_and_reported_without_content(
    spelling: str, as_bytes: bool
) -> None:
    events: list[tuple[str, str]] = []
    guard = EgressGuard(on_block=lambda kind, destination: events.append((kind, destination)))
    guard.allow_unix(CANONICAL)
    address: str | bytes = spelling.encode() if as_bytes else spelling
    with pytest.raises(EgressBlockedError):
        guard.check_address(socket.AF_UNIX, address)
    assert events == [("connect", "a unix socket")], "reported, and nothing of the address in it"
    assert guard.blocked == 1


@pytest.mark.parametrize("address", [None, 5, ("127.0.0.1", 80), ["/run/parser/parser.sock"]])
def test_an_address_that_is_not_a_path_is_refused(address: object) -> None:
    guard = EgressGuard()
    guard.allow_unix(CANONICAL)
    with pytest.raises(EgressBlockedError):
        guard.check_address(socket.AF_UNIX, address)


#: What cannot be allowed at all: relative, not clean, with a NUL, empty, or no file name.
NOT_ALLOWABLE = [
    "run/parser/parser.sock",
    "./run/parser/parser.sock",
    "parser.sock",
    "/run/parser/../parser/parser.sock",
    "/run/parser/./parser.sock",
    "/run/parser/parser.sock/.",
    "//run/parser/parser.sock",
    "/run//parser/parser.sock",
    "/run/parser/parser.sock/",
    "/run/parser/parser.sock\0",
    "/run/parser\0/parser.sock",
    "\0/run/parser/parser.sock",
    "\0parser",
    "",
    "/",
]


@pytest.mark.parametrize("path", NOT_ALLOWABLE)
@pytest.mark.parametrize("as_bytes", [False, True])
def test_only_an_absolute_canonical_path_can_be_allowed(path: str, as_bytes: bool) -> None:
    guard = EgressGuard()
    with pytest.raises(ValueError, match="absolute and canonical"):
        guard.allow_unix(path.encode() if as_bytes else path)
    assert guard.unix_paths == frozenset()


def test_the_message_of_a_refused_path_does_not_repeat_it() -> None:
    with pytest.raises(ValueError) as refused:
        EgressGuard().allow_unix("/run/../secret-name/x.sock")
    assert "secret-name" not in str(refused.value)


def test_a_path_is_allowed_as_text_bytes_or_a_path_object_and_stored_the_same_way() -> None:
    for form in (CANONICAL, CANONICAL.encode(), Path(CANONICAL)):
        guard = EgressGuard()
        guard.allow_unix(form)
        assert guard.unix_paths == frozenset({CANONICAL})


def test_the_kernel_resolves_dot_dot_after_symlinks_so_the_guard_never_does_it_at_all() -> None:
    """``<d>/link/../real/s.sock`` is, to a lexical clean-up, the allowed ``<d>/real/s.sock``; to
    the kernel, with ``link`` pointing at ``<d>/elsewhere/sub``, it is
    ``<d>/elsewhere/real/s.sock``: another socket. Both exist here, and only the one at the exact
    allowed spelling is reachable."""
    base = Path(tempfile.mkdtemp(prefix="pa-eg-"))
    servers: list[socket.socket] = []
    try:
        (base / "real").mkdir()
        (base / "elsewhere" / "sub").mkdir(parents=True)
        (base / "elsewhere" / "real").mkdir()
        (base / "link").symlink_to(base / "elsewhere" / "sub")
        (base / "alias").symlink_to(base / "real")
        for directory in (base / "real", base / "elsewhere" / "real"):
            srv_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv_sock.bind(str(directory / "s.sock"))
            srv_sock.listen(1)
            servers.append(srv_sock)
        allowed = base / "real" / "s.sock"
        guard = egress.installed()
        assert guard is not None
        guard.allow_unix(allowed)
        for spelling in (
            f"{base}/link/../real/s.sock",  # lexically the allowed path, really the other socket
            f"{base}/alias/s.sock",  # the same socket through a symlink, spelled differently
            f"{base}/real/../real/s.sock",
            f"{base}/real//s.sock",
            f"{base}/real/./s.sock",
        ):
            with (
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client,
                pytest.raises(EgressBlockedError),
            ):
                client.connect(spelling)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(allowed))
    finally:
        for srv_sock in servers:
            srv_sock.close()
        shutil.rmtree(base, ignore_errors=True)


def test_the_guard_does_not_depend_on_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = EgressGuard()
    guard.allow_unix(CANONICAL)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(EgressBlockedError):
        guard.check_address(socket.AF_UNIX, "parser.sock")
    monkeypatch.chdir("/")
    with pytest.raises(EgressBlockedError):
        guard.check_address(socket.AF_UNIX, "run/parser/parser.sock")


def test_an_abstract_namespace_name_cannot_be_allowed() -> None:
    with pytest.raises(ValueError, match="absolute and canonical"):
        EgressGuard().allow_unix("\0parser")
    with pytest.raises(ValueError, match="absolute and canonical"):
        EgressGuard().allow_unix("")


def test_internet_addresses_are_judged_as_before_whatever_unix_paths_are_allowed() -> None:
    guard = EgressGuard()
    guard.allow_unix(CANONICAL)
    guard.check_address(socket.AF_INET, ("127.0.0.1", 80))
    with pytest.raises(EgressBlockedError):
        guard.check_address(socket.AF_INET, ("8.8.8.8", 53))
    with pytest.raises(EgressBlockedError):
        guard.check_address(socket.AF_INET6, ("2001:4860:4860::8888", 53))


# --- nothing else relied on every Unix connect being allowed -----------------------------------


def test_the_event_loops_and_the_test_client_never_connect_a_unix_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The guard used to wave every ``AF_UNIX`` address through. ``asyncio`` (and ``uvicorn``'s
    loop, and ``anyio``'s portal) make their wake-up pipe with ``socketpair``, which is never
    connected through ``socket.connect``: this records every address the guard is asked about while
    an event loop, an anyio portal and the whole application start, run a request and stop."""
    import asyncio

    from fastapi.testclient import TestClient

    import procurement_agent.db as dbmod
    from procurement_agent.config import Settings, get_settings
    from procurement_agent.main import create_app

    asked: list[int] = []
    real = EgressGuard.check_address

    def spy(self: EgressGuard, family: int, address: object, kind: str = "connect") -> None:
        asked.append(family)
        real(self, family, address, kind)

    monkeypatch.setattr(EgressGuard, "check_address", spy)

    async def tick() -> int:
        await asyncio.sleep(0)
        return 1

    assert asyncio.run(tick()) == 1
    left, right = socket.socketpair()  # what a loop wakes itself with: not a connect
    left.close()
    right.close()
    settings = Settings(
        env="dev",
        data_dir=tmp_path / "data",
        ollama_base_url="http://127.0.0.1:1",
        rate_limit="10000/minute",
    )
    get_settings.cache_clear()
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()
    assert socket.AF_UNIX not in asked, "something in the application connects a Unix socket"
    # the control: the recording does see a Unix connect when there is one
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c, pytest.raises(EgressBlockedError):
        c.connect(str(tmp_path / "nobody.sock"))
    assert asked[-1] == socket.AF_UNIX


def test_an_allowed_unix_path_is_not_an_allowed_host() -> None:
    guard = EgressGuard()
    guard.allow_unix("/run/parser/parser.sock")
    with pytest.raises(EgressBlockedError):
        guard.check_address(socket.AF_INET, ("8.8.8.8", 53))
    assert guard.unix_paths == frozenset({"/run/parser/parser.sock"})


def test_a_real_unix_connect_goes_through_the_guard() -> None:
    short = Path(tempfile.mkdtemp(prefix="pa-eg-"))
    try:
        path = short / "s.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                with pytest.raises(EgressBlockedError):
                    client.connect(str(path))
                with pytest.raises(EgressBlockedError):
                    client.connect_ex(str(path))
            guard = egress.installed()
            assert guard is not None
            guard.allow_unix(path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(path))
    finally:
        shutil.rmtree(short, ignore_errors=True)


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
