"""Response headers, the session cookie and its lifetime, in development and in production.

Production is built the way the container builds it: ``PA_ENV=prod``, a listed host name, no dev
key, an API key row in the database, and requests made to ``https://`` so ``Secure`` cookies are
sent back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from starlette.requests import Request

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import session_scope
from procurement_agent.main import create_app
from procurement_agent.models import UiSession
from procurement_agent.security.auth import create_api_key
from procurement_agent.security.headers import COMMON, ERROR_CSP, HSTS, PAGE_CSP
from procurement_agent.security.ratelimit import client_key

DEV_KEY = "pa_devkey_for_tests_only"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
#: A browser navigation: an unauthenticated one is sent to the sign-in page, anything else gets 401.
HTML = {"Accept": "text/html"}
HOST = "tasks.company.test"


def _app(tmp_path: Path, **overrides: Any) -> Iterator[tuple[TestClient, Settings]]:
    base: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "ollama_base_url": "http://127.0.0.1:1",
        "rate_limit": "10000/minute",
    }
    base.update(overrides)
    settings = Settings(**base)
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    url = f"https://{HOST}" if settings.env == "prod" else "http://testserver"
    with TestClient(app, base_url=url) as client:
        yield client, settings
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


@pytest.fixture
def dev(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    yield from _app(tmp_path, env="dev", dev_api_key=DEV_KEY)


@pytest.fixture
def prod(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    yield from _app(
        tmp_path,
        env="prod",
        docling_artifacts_path=tmp_path / "models",
        allowed_hosts=(HOST,),
        models_manifest_required=False,  # no models in a test; see test_models_manifest.py
    )


def _prod_key() -> str:
    with session_scope() as s:
        key, _ = create_api_key(s, "alice")
    return key


def _login(client: TestClient, key: str) -> Any:
    return client.post(
        "/ui/login", data={"api_key": key}, headers=SAME_ORIGIN, follow_redirects=False
    )


# --- the headers -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/ui/login", "/health", "/static/app.js", "/nonexistent", "/openapi.json"]
)
def test_every_answer_carries_the_common_headers(
    dev: tuple[TestClient, Settings], path: str
) -> None:
    client, _ = dev
    r = client.get(path)
    for name, value in COMMON.items():
        assert r.headers[name] == value, (path, name)
    assert r.headers["content-security-policy"] == PAGE_CSP


def test_the_policies_say_what_they_are_meant_to() -> None:
    assert COMMON["Cross-Origin-Opener-Policy"] == "same-origin"
    assert COMMON["Cross-Origin-Resource-Policy"] == "same-origin"
    assert COMMON["X-Frame-Options"] == "DENY"
    for feature in ("camera", "microphone", "geolocation", "payment", "usb", "interest-cohort"):
        assert f"{feature}=()" in COMMON["Permissions-Policy"]
    assert "frame-ancestors 'none'" in PAGE_CSP and "frame-ancestors 'none'" in ERROR_CSP
    assert "default-src 'none'" in ERROR_CSP
    assert "unsafe-inline" not in PAGE_CSP and "unsafe-eval" not in PAGE_CSP


def test_hsts_is_sent_in_production_and_never_in_development(
    dev: tuple[TestClient, Settings], prod: tuple[TestClient, Settings]
) -> None:
    assert "strict-transport-security" not in dev[0].get("/health").headers
    assert prod[0].get("/health").headers["strict-transport-security"] == HSTS
    assert "includeSubDomains" in HSTS and "max-age=31536000" in HSTS


def test_the_refusals_written_before_the_application_carry_the_headers_too(
    prod: tuple[TestClient, Settings],
) -> None:
    """A host that is not listed and a body over its limit are answered by the two outermost
    layers, which the page-header middleware never sees; they send the set themselves."""
    client, _ = prod
    refused_host = client.get("/health", headers={"host": "attacker.example"})
    too_big = client.post(
        "/api/v1/checks/compare",
        headers={"X-API-Key": "pa_" + "x" * 43, "content-length": str(50 * 1024 * 1024)},
        content=b"{}",
    )
    assert (refused_host.status_code, too_big.status_code) == (400, 413)
    for r in (refused_host, too_big):
        for name, value in COMMON.items():
            assert r.headers[name] == value, (r.status_code, name)
        assert r.headers["content-security-policy"] == ERROR_CSP
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["connection"] == "close"


def test_a_credential_refused_before_the_upload_body_is_read_has_them_too(
    prod: tuple[TestClient, Settings],
) -> None:
    client, _ = prod
    r = client.post(
        "/api/v1/documents",
        headers={"X-API-Key": "pa_" + "x" * 43},
        files={"file": ("a.docx", b"x")},
    )
    assert r.status_code == 401
    for name, value in COMMON.items():
        assert r.headers[name] == value, name
    assert r.headers["strict-transport-security"] == HSTS
    assert r.headers["connection"] == "close"


def test_the_server_banner_is_switched_off_where_the_server_is_started() -> None:
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text(encoding="utf-8")
    assert "--no-server-header" in dockerfile.split("CMD [", 1)[1]


# --- the session cookie ------------------------------------------------------------------------


def test_the_development_cookie_keeps_its_name_and_is_not_secure(
    dev: tuple[TestClient, Settings],
) -> None:
    client, _ = dev
    r = _login(client, DEV_KEY)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("pa_session=ps_")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie  # plain HTTP on localhost would not send it back


def test_the_production_cookie_is_host_prefixed_secure_and_scoped_to_this_host(
    prod: tuple[TestClient, Settings],
) -> None:
    client, _ = prod
    r = _login(client, _prod_key())
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-pa_session=ps_")
    attributes = {a.strip().split("=")[0].lower() for a in cookie.split(";")[1:]}
    assert {"secure", "httponly", "samesite", "path", "max-age"} <= attributes
    assert "domain" not in attributes  # __Host- forbids it, and it would widen the cookie's scope
    assert "Path=/" in cookie and "SameSite=strict" in cookie
    assert "pa_session" not in client.cookies  # the unprefixed name is never used


def test_the_production_session_signs_in_and_out_by_that_name(
    prod: tuple[TestClient, Settings],
) -> None:
    client, _ = prod
    assert (
        client.get("/ui/documents", headers=HTML, follow_redirects=False).status_code == 303
    )  # to the login
    _login(client, _prod_key())
    assert client.get("/ui/documents").status_code == 200
    out = client.post("/ui/logout", headers=SAME_ORIGIN, follow_redirects=False)
    cleared = out.headers["set-cookie"]
    assert cleared.startswith("__Host-pa_session=") and "Max-Age=0" in cleared
    assert "Secure" in cleared  # a __Host- cookie can only be cleared by a Secure one
    assert client.get("/ui/documents", headers=HTML, follow_redirects=False).status_code == 303


def test_a_cookie_under_the_other_environments_name_is_not_a_credential(
    prod: tuple[TestClient, Settings],
) -> None:
    client, _ = prod
    _login(client, _prod_key())
    token = client.cookies.get("__Host-pa_session")
    assert token
    client.cookies.clear()
    client.cookies.set("pa_session", token, domain=HOST)  # the development name, a valid token
    assert client.get("/ui/documents", headers=HTML, follow_redirects=False).status_code == 303


def test_the_language_cookie_is_secure_in_production_only(
    dev: tuple[TestClient, Settings], prod: tuple[TestClient, Settings]
) -> None:
    for (client, _), secure in ((dev, False), (prod, True)):
        r = client.post(
            "/ui/lang", data={"lang": "ka"}, headers=SAME_ORIGIN, follow_redirects=False
        )
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("pa_lang=ka")
        assert ("Secure" in cookie) is secure


# --- idle timeout ------------------------------------------------------------------------------


def _age_session(*, last_seen: timedelta | None, created: timedelta) -> None:
    with session_scope() as s:
        now = datetime.now(UTC)
        s.execute(
            update(UiSession).values(
                created_at=now - created,
                last_seen_at=None if last_seen is None else now - last_seen,
            )
        )


def test_a_session_unused_for_longer_than_the_idle_limit_is_over(
    dev: tuple[TestClient, Settings],
) -> None:
    client, settings = dev
    _login(client, DEV_KEY)
    assert client.get("/ui/documents").status_code == 200
    idle = timedelta(minutes=settings.ui_session_idle_minutes)
    _age_session(last_seen=idle + timedelta(minutes=1), created=idle + timedelta(minutes=30))
    r = client.get("/ui/documents", headers=HTML, follow_redirects=False)
    assert r.status_code == 303  # to the sign-in page: the session is no longer good
    with session_scope() as s:
        assert (
            s.scalars(select(UiSession)).one().revoked_at is None
        )  # expired by idleness, not revoked


def test_a_session_in_use_stays_alive_past_the_idle_limit_since_sign_in(
    dev: tuple[TestClient, Settings],
) -> None:
    client, settings = dev
    _login(client, DEV_KEY)
    idle = timedelta(minutes=settings.ui_session_idle_minutes)
    _age_session(last_seen=timedelta(minutes=1), created=idle * 3)  # long ago in, used a minute ago
    assert client.get("/ui/documents").status_code == 200


def test_a_session_never_used_is_measured_from_sign_in(dev: tuple[TestClient, Settings]) -> None:
    client, settings = dev
    _login(client, DEV_KEY)
    idle = timedelta(minutes=settings.ui_session_idle_minutes)
    _age_session(last_seen=None, created=idle + timedelta(minutes=5))
    assert client.get("/ui/documents", headers=HTML, follow_redirects=False).status_code == 303
    _age_session(last_seen=None, created=idle - timedelta(minutes=5))
    # a fresh sign-in is needed once the first was refused; test the inside-the-limit case alone
    client.cookies.clear()
    _login(client, DEV_KEY)
    _age_session(last_seen=None, created=idle - timedelta(minutes=5))
    assert client.get("/ui/documents").status_code == 200


def test_the_idle_limit_is_configurable_and_bounded() -> None:
    assert Settings().ui_session_idle_minutes == 60
    assert Settings(ui_session_idle_minutes=5).ui_session_idle_minutes == 5
    for bad in (0, 4, 1441):
        with pytest.raises(ValueError, match="ui_session_idle_minutes"):
            Settings(ui_session_idle_minutes=bad)


# --- the rate limit's view of an IPv6 caller ---------------------------------------------------


def _request(host: str) -> Request:
    return Request({"type": "http", "headers": [], "client": (host, 1234), "path": "/"})


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("203.0.113.9", "ip:203.0.113.9"),
        ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "ip:2001:db8:1:2::/64"),
        ("2001:db8:1:2:1111:2222:3333:4444", "ip:2001:db8:1:2::/64"),  # same /64: same bucket
        ("2001:db8:1:3::1", "ip:2001:db8:1:3::/64"),  # another /64: another bucket
        ("::ffff:203.0.113.9", "ip:203.0.113.9"),  # an IPv4 caller on a dual-stack socket
        ("fe80::1%eth0", "ip:fe80::/64"),
        ("not-an-address", "ip:not-an-address"),
        ("unknown", "ip:unknown"),
    ],
)
def test_the_rate_limit_key_for_an_address(host: str, key: str) -> None:
    assert client_key(_request(host)) == key


def test_a_spray_of_addresses_from_one_prefix_shares_one_bucket() -> None:
    keys = {client_key(_request(f"2001:db8:aa:bb:{i:x}::{i:x}")) for i in range(1000)}
    assert keys == {"ip:2001:db8:aa:bb::/64"}


def test_a_request_without_a_client_gets_a_key() -> None:
    assert (
        client_key(Request({"type": "http", "headers": [], "client": None, "path": "/"}))
        == "ip:unknown"
    )
