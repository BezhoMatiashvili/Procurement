"""The Host allow-list: which names this server answers to, judged on the header as a whole."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from procurement_agent.security.hosts import LOCAL_HOSTS, AllowedHostsMiddleware, host_of


def _app(allowed: tuple[str, ...] = (), always: tuple[str, ...] = LOCAL_HOSTS) -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"pong": "yes"}

    @app.websocket("/ws")
    async def ws(socket) -> None:  # type: ignore[no-untyped-def]
        await socket.accept()
        await socket.close()

    app.add_middleware(AllowedHostsMiddleware, allowed_hosts=allowed, always=always)
    return app


def _get(app: FastAPI, host: str | None) -> int:
    with TestClient(app, base_url="http://placeholder") as c:
        headers = {"host": host} if host is not None else {}
        return c.get("/ping", headers=headers).status_code


# --- parsing -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "host"),
    [
        ("example.com", "example.com"),
        ("EXAMPLE.com:8443", "example.com"),
        ("example.com.", "example.com"),
        ("127.0.0.1:8000", "127.0.0.1"),
        ("[::1]:8000", "::1"),
        ("[::1]", "::1"),
        ("[fe80::1%eth0]:80", "fe80::1%eth0"),
        ("  localhost  ", "localhost"),
    ],
)
def test_host_header_parsing(header: str, host: str) -> None:
    assert host_of(header) == host


@pytest.mark.parametrize(
    "header",
    [
        "",
        "   ",
        "user@example.com",
        "example.com:80@evil.example",
        "example.com:notaport",
        "example.com:80:81",
        "example.com/path",
        "example.com\\path",
        "example.com#frag",
        "example.com?q",
        "exa mple.com",
        "example.com\r\nX-Injected: 1",
        "[::1",
        "[::1]x",
        "[::1]:abc",
        "[nonsense]:80",
        ":80",
    ],
)
def test_a_header_that_is_not_a_host_is_none(header: str) -> None:
    assert host_of(header) is None


# --- the rule ------------------------------------------------------------------------------------


def test_the_loopback_names_are_always_served() -> None:
    app = _app()
    for host in ("localhost:8000", "127.0.0.1:8000", "127.9.9.9", "[::1]:8000", "LOCALHOST"):
        assert _get(app, host) == 200, host


def test_a_name_that_is_not_listed_is_refused() -> None:
    app = _app(("tasks.company.example",))
    assert _get(app, "tasks.company.example") == 200
    assert _get(app, "TASKS.company.example:8443") == 200
    for host in (
        "attacker.example",
        "tasks.company.example.attacker.example",
        "xtasks.company.example",
        "tasks.company.example@attacker.example",
        "10.0.0.5",  # an address that is not ours is not "local" because it is private
        "192.168.1.10:8000",
        "0.0.0.0",
    ):
        assert _get(app, host) == 400, host


def test_the_refusal_is_plain_and_says_nothing_about_the_configuration() -> None:
    app = _app(("tasks.company.example",))
    with TestClient(app) as c:
        r = c.get("/ping", headers={"host": "attacker.example"})
    assert r.status_code == 400
    assert r.text == "Invalid host header"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "company" not in r.text


def test_wildcards_match_subdomains_only() -> None:
    app = _app(("*.company.example",))
    assert _get(app, "tasks.company.example") == 200
    assert _get(app, "a.b.company.example") == 200
    assert _get(app, "company.example") == 400  # the bare domain is its own entry
    assert _get(app, "badcompany.example") == 400
    assert _get(app, ".company.example") == 400


def test_a_request_without_a_host_is_refused() -> None:
    app = _app(("tasks.company.example",))
    assert _get(app, "") == 400


def test_two_host_headers_are_refused() -> None:
    """A proxy and this server could otherwise read different ones."""
    app = _app(("tasks.company.example",))
    with TestClient(app) as c:
        # httpx collapses duplicate keys in a dict; send the pairs
        r = c.get("/ping", headers=[("host", "tasks.company.example"), ("host", "localhost")])
    assert r.status_code == 400


def test_testserver_is_only_there_when_asked_for() -> None:
    assert _get(_app(always=(*LOCAL_HOSTS, "testserver")), "testserver") == 200
    assert _get(_app(), "testserver") == 400


def test_websockets_are_refused_with_a_policy_close() -> None:
    app = _app(("tasks.company.example",))
    with (
        TestClient(app) as c,
        pytest.raises(WebSocketDisconnect) as gone,
        c.websocket_connect("/ws", headers={"host": "attacker.example"}),
    ):
        pass
    assert gone.value.code == 1008


def test_the_lifespan_scope_passes_through() -> None:
    """The middleware sees the startup scope too, which has no headers."""
    app = _app(("tasks.company.example",))
    with TestClient(app):  # entering the context runs the lifespan
        pass
