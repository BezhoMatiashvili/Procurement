"""Requests are judged before their bodies are read.

The tests that matter drive the application as a raw ASGI callable with a ``receive`` that counts
the bytes it hands out: whatever a layer does with a body, the count says whether it was read at
all. Starlette's TestClient cannot show that - it hands the app the whole body in one message.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, File, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.types import Message

import procurement_agent.db as dbmod
from procurement_agent.config import SMALL_BODY_BYTES, Settings, get_settings
from procurement_agent.db import session_scope
from procurement_agent.main import create_app
from procurement_agent.models import AuditLog
from procurement_agent.security.body_limit import BodySizeLimitMiddleware
from procurement_agent.security.preauth import upload_routes

KEY = "pa_devkey_for_tests_only"
MIB = 1024 * 1024
UPLOADS = {
    ("POST", "/api/v1/documents"),
    ("POST", "/ui/documents/upload"),
    ("POST", "/ui/start/aoa"),
    ("POST", "/ui/start/compare"),
}
WRONG_KEY = "pa_" + "x" * 43


@pytest.fixture
def app(tmp_path: Path) -> Iterator[FastAPI]:
    settings = Settings(
        env="dev",
        data_dir=tmp_path / "data",
        dev_api_key=KEY,
        ollama_base_url="http://127.0.0.1:1",
        max_file_mb=5,
        rate_limit="10000/minute",
    )
    get_settings.cache_clear()
    application = create_app(settings)
    application.dependency_overrides[get_settings] = lambda: settings
    with TestClient(application):  # runs the lifespan: database, worker
        yield application
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


class Call:
    """One request sent straight to the ASGI app, with a counted body."""

    def __init__(
        self,
        app: FastAPI,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body_bytes: int = 0,
        chunk: int = 64 * 1024,
        content_length: bool = False,
        content_type: str = "multipart/form-data; boundary=x",
        well_formed: bool = False,
    ) -> None:
        self.app, self.chunk, self.total = app, chunk, body_bytes
        #: Start the body as a real multipart file part, so a parser keeps reading it.
        self.opening = (
            b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n'
            b"Content-Type: application/pdf\r\n\r\n"
            if well_formed
            else b""
        )
        self.consumed = 0
        self.receive_calls = 0
        self.messages: list[Message] = []
        raw = {"host": "testserver", "content-type": content_type, **(headers or {})}
        if content_length:
            raw["content-length"] = str(body_bytes)
        self.scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in raw.items()],
            "client": ("203.0.113.9", 5555),
            "server": ("testserver", 80),
        }

    async def _receive(self) -> Message:
        self.receive_calls += 1
        remaining = self.total - self.consumed
        if remaining <= 0:
            return {"type": "http.request", "body": b"", "more_body": False}
        size = min(self.chunk, remaining)
        body = (self.opening + b"x" * size)[:size]
        self.opening = self.opening[size:]
        self.consumed += size
        return {"type": "http.request", "body": body, "more_body": self.consumed < self.total}

    async def _send(self, message: Message) -> None:
        self.messages.append(message)

    def run(self) -> Call:
        asyncio.run(self.app(self.scope, self._receive, self._send))
        return self

    @property
    def status(self) -> int:
        return next(int(m["status"]) for m in self.messages if m["type"] == "http.response.start")

    @property
    def text(self) -> str:
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        ).decode()

    @property
    def detail(self) -> str:
        return str(json.loads(self.text).get("detail"))


def _audit_rows(action: str) -> list[tuple[str, str, str | None]]:
    with session_scope() as s:
        rows = s.scalars(select(AuditLog).where(AuditLog.action == action)).all()
        return [(r.actor, r.outcome, r.detail) for r in rows]


# --- which routes take a file -----------------------------------------------------------------


def test_the_upload_routes_are_found_from_the_routes_themselves(app: FastAPI) -> None:
    assert upload_routes(app.routes) == UPLOADS


def test_discovery_finds_a_new_upload_route_and_ignores_the_rest() -> None:
    router = APIRouter(prefix="/v2")

    @router.post("/scan")
    async def scan(scan_file: UploadFile) -> dict[str, int]:
        return {"n": 1}

    @router.post("/many")
    async def many(files: list[UploadFile] = File(...)) -> dict[str, int]:
        return {"n": len(files)}

    @router.post("/json")
    def plain(body: dict[str, int]) -> dict[str, int]:
        return body

    @router.get("/scan")
    def listing() -> list[str]:
        return []

    probe = FastAPI()
    probe.include_router(router)
    assert upload_routes(probe.routes) == {("POST", "/v2/scan"), ("POST", "/v2/many")}


def test_an_upload_route_the_discovery_misses_fails_loudly_not_open(app: FastAPI) -> None:
    """If discovery ever found nothing, every route would be held to the small cap: an upload
    would be refused with 413 - never admitted with a large body."""
    middleware = BodySizeLimitMiddleware(
        app,
        default_max_bytes=SMALL_BODY_BYTES,
        upload_max_bytes=10 * MIB,
        upload_routes=frozenset(),
    )
    assert middleware.limit_for("POST", "/api/v1/documents") == SMALL_BODY_BYTES


def test_the_cap_follows_the_route(app: FastAPI) -> None:
    middleware = BodySizeLimitMiddleware(
        app, default_max_bytes=100, upload_max_bytes=5000, upload_routes=frozenset(UPLOADS)
    )
    assert middleware.limit_for("POST", "/api/v1/documents") == 5000
    assert middleware.limit_for("POST", "/ui/start/aoa") == 5000
    assert middleware.limit_for("GET", "/api/v1/documents") == 100  # same path, another method
    assert middleware.limit_for("POST", "/api/v1/checks/compare") == 100
    assert middleware.limit_for("POST", "/ui/login") == 100


# --- the body of an unauthenticated upload is never read --------------------------------------


@pytest.mark.parametrize(("method", "path"), sorted(UPLOADS))
@pytest.mark.parametrize(
    ("headers", "detail"),
    [
        ({}, "missing API key"),
        ({"x-api-key": WRONG_KEY}, "invalid API key"),
        ({"x-api-key": "pa_short"}, "invalid API key"),
        ({"x-api-key": "not-even-a-key"}, "invalid API key"),
        (
            {"cookie": "pa_session=ps_" + "y" * 43, "sec-fetch-site": "same-origin"},
            "session expired or invalid",
        ),
    ],
    ids=["no-credential", "wrong-key", "short-key", "garbage-key", "dead-session"],
)
def test_no_byte_of_the_body_is_read_for_a_caller_who_may_not_upload(
    app: FastAPI, method: str, path: str, headers: dict[str, str], detail: str
) -> None:
    call = Call(app, method, path, headers=headers, body_bytes=60 * MIB, chunk=MIB).run()
    assert call.status == 401
    assert call.detail == detail
    assert call.consumed == 0, f"{call.consumed} bytes of the body were read before the 401"


@pytest.mark.parametrize(("method", "path"), sorted(UPLOADS))
def test_a_cross_site_post_with_a_cookie_is_refused_before_its_body(
    app: FastAPI, method: str, path: str
) -> None:
    headers = {"cookie": "pa_session=ps_" + "y" * 43, "sec-fetch-site": "cross-site"}
    call = Call(app, method, path, headers=headers, body_bytes=30 * MIB, chunk=MIB).run()
    assert call.status == 403
    assert call.detail == "cross-site request refused"
    assert call.consumed == 0


def test_a_declared_length_changes_nothing_about_that(app: FastAPI) -> None:
    call = Call(
        app, "POST", "/api/v1/documents", headers={"x-api-key": WRONG_KEY},
        body_bytes=4 * MIB, content_length=True,
    ).run()  # fmt: skip
    assert (call.status, call.consumed) == (401, 0)


def test_the_refusal_closes_the_connection_rather_than_waiting_for_the_body(app: FastAPI) -> None:
    call = Call(
        app, "POST", "/api/v1/documents", headers={"x-api-key": WRONG_KEY}, body_bytes=MIB
    ).run()
    start = next(m for m in call.messages if m["type"] == "http.response.start")
    assert (b"connection", b"close") in start["headers"]
    assert (b"cache-control", b"no-store") in start["headers"]


def test_a_valid_credential_does_get_its_body_read(app: FastAPI) -> None:
    call = Call(
        app, "POST", "/api/v1/documents", headers={"x-api-key": KEY}, body_bytes=300 * 1024
    ).run()
    assert call.consumed > 0  # the handler read it (and stopped at the first malformed chunk)
    assert call.status in (400, 422)  # not a multipart body: the handler refuses it, not the gate


def test_other_methods_on_an_upload_path_are_not_held_to_the_upload_rules(app: FastAPI) -> None:
    call = Call(app, "GET", "/api/v1/documents", content_type="application/json").run()
    assert call.status == 401  # the route's own authenticate, as before
    assert call.detail == "missing API key"


# --- audit: the same rows authenticate writes, once -------------------------------------------


def test_a_refused_credential_is_audited_exactly_as_the_route_would(app: FastAPI) -> None:
    Call(app, "POST", "/api/v1/documents", headers={"x-api-key": WRONG_KEY}, body_bytes=MIB).run()
    Call(app, "POST", "/api/v1/documents", body_bytes=MIB).run()  # no credential: not audited
    cross_site = {"cookie": "pa_session=ps_" + "y" * 43, "sec-fetch-site": "cross-site"}
    Call(app, "POST", "/ui/start/compare", headers=cross_site, body_bytes=MIB).run()
    assert _audit_rows("auth.denied") == [
        ("anonymous", "denied", "POST /api/v1/documents: invalid API key")
    ]
    assert _audit_rows("auth.csrf") == [
        ("anonymous", "denied", "POST /ui/start/compare: cross-site request refused")
    ]


def test_the_key_itself_is_never_in_the_audit_row(app: FastAPI) -> None:
    Call(app, "POST", "/api/v1/documents", headers={"x-api-key": WRONG_KEY}, body_bytes=10).run()
    with session_scope() as s:
        every = " ".join(f"{r.detail} {r.actor}" for r in s.scalars(select(AuditLog)))
    assert WRONG_KEY not in every
    assert "xxxxxxxx" not in every


# --- order: the rate limit comes first --------------------------------------------------------


def test_the_rate_limit_answers_before_any_credential_lookup(tmp_path: Path) -> None:
    settings = Settings(
        env="dev",
        data_dir=tmp_path / "data",
        dev_api_key=KEY,
        ollama_base_url="http://127.0.0.1:1",
        rate_limit="2/minute",
    )
    get_settings.cache_clear()
    application = create_app(settings)
    application.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(application):
            statuses = [
                Call(
                    application,
                    "POST",
                    "/api/v1/documents",
                    headers={"x-api-key": WRONG_KEY},
                    body_bytes=10,
                )
                .run()
                .status
                for _ in range(5)
            ]
            assert statuses == [401, 401, 429, 429, 429]
            assert (
                len(_audit_rows("auth.denied")) == 2
            )  # the throttled three never reached a lookup
    finally:
        dbmod._engine = None
        dbmod._SessionLocal = None
        get_settings.cache_clear()


# --- routes that take no file are held to a megabyte ------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "ctype"),
    [
        ("POST", "/api/v1/checks/compare", "application/json"),
        ("POST", "/api/v1/checks/boq", "application/json"),
        ("POST", "/api/v1/checks/aoa/review", "application/json"),
        ("POST", "/api/v1/documents/abc/extract", "application/json"),
        ("POST", "/ui/login", "application/x-www-form-urlencoded"),
        ("POST", "/ui/lang", "application/x-www-form-urlencoded"),
        ("POST", "/ui/documents/abc/extract", "application/x-www-form-urlencoded"),
    ],
)
def test_a_body_beyond_a_megabyte_is_cut_off_on_a_route_that_takes_no_file(
    app: FastAPI, method: str, path: str, ctype: str
) -> None:
    call = Call(
        app, method, path, headers={"x-api-key": KEY}, body_bytes=60 * MIB, chunk=64 * 1024,
        content_type=ctype,
    ).run()  # fmt: skip
    assert call.status == 413
    assert call.detail == "request body too large"
    assert call.consumed <= SMALL_BODY_BYTES + 64 * 1024, (
        call.consumed
    )  # one chunk past the cap, no more


def test_a_declared_oversize_body_on_such_a_route_is_refused_without_reading_any(
    app: FastAPI,
) -> None:
    call = Call(
        app, "POST", "/api/v1/checks/compare", headers={"x-api-key": KEY},
        body_bytes=90 * MIB, content_length=True, content_type="application/json",
    ).run()  # fmt: skip
    assert (call.status, call.consumed) == (413, 0)


def test_an_anonymous_json_flood_is_cut_off_at_a_megabyte_not_buffered_then_refused(
    app: FastAPI,
) -> None:
    """The old behaviour: ~100 MB read and parsed, then a 401."""
    call = Call(
        app,
        "POST",
        "/api/v1/checks/compare",
        body_bytes=90 * MIB,
        chunk=MIB,
        content_type="application/json",
    ).run()
    assert call.status == 413
    assert call.consumed <= SMALL_BODY_BYTES + MIB


def test_ordinary_small_requests_are_unaffected(app: FastAPI) -> None:
    body = json.dumps({"left_document_id": "a", "right_document_id": "b"}).encode()
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/checks/compare",
            headers={"X-API-Key": KEY, "Content-Type": "application/json"},
            content=body,
        )
    assert r.status_code == 404  # no such document - the route ran


def test_the_upload_cap_is_still_the_big_one(app: FastAPI) -> None:
    """A valid caller may send two files and framing; one byte more than that is refused."""
    settings: Settings = app.state.settings
    over = settings.max_request_bytes + 1
    call = Call(
        app,
        "POST",
        "/api/v1/documents",
        headers={"x-api-key": KEY},
        body_bytes=over,
        chunk=MIB,
        well_formed=True,
    ).run()
    assert call.status == 413
    assert settings.max_request_bytes < call.consumed <= over


def test_a_helper_for_the_tests_themselves_counts_what_it_hands_out() -> None:
    """The proof above is only as good as the counter."""
    seen: list[Awaitable[Any] | Callable[..., Any]] = []
    probe = Call(FastAPI(), "POST", "/x", body_bytes=2500, chunk=1000)

    async def drain() -> None:
        while True:
            message = await probe._receive()
            seen.append(lambda: None)
            if not message.get("more_body"):
                return

    asyncio.run(drain())
    assert probe.consumed == 2500
    assert probe.receive_calls == 3
