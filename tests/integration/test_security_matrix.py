# ruff: noqa: E501
"""Every route, every kind of caller: who may reach it, whose data it may show, what it refuses.

The routes are found from the application itself (``fastapi.routing.iter_route_contexts``), pinned
in ``ROUTES`` so a new one cannot be added without somebody deciding which class it belongs to, and
then driven as an anonymous caller, as a second signed-in user holding the first user's ids, as a
browser session replayed cross-site, and with hostile path values. Each answer is checked for the
status, for the absence of the first user's marker text and file name, and for the standard headers.
"""

from __future__ import annotations

import io
import re
import time
from collections.abc import Iterator
from typing import Any

import pytest
from docx import Document as WordDocument
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import select

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import session_scope
from procurement_agent.main import create_app
from procurement_agent.models import Document, DocumentStatus
from procurement_agent.security.auth import create_api_key
from procurement_agent.security.headers import COMMON
from tests.boq_factory import LINES, blank, build_boq
from tests.conftest import make_pdf

ALICE_MARK = "ZQX-ALICE-PRIVATE-7731"
ALICE_FILE = "alice-private-contract-7731.docx"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
HTML = {"Accept": "text/html"}
DUMMY = "0" * 32

# --- the inventory ---------------------------------------------------------------------------------------------------

#: Reachable with no credential, and why.
PUBLIC = {
    ("GET", "/health"): "liveness, reveals nothing",
    ("GET", "/ready"): "readiness: only which part is down",
    ("GET", "/openapi.json"): "the schema, development only",
    ("HEAD", "/openapi.json"): "the schema, development only",
    ("GET", "/ui/login"): "the sign-in form",
    ("POST", "/ui/login"): "signing in (same-origin only)",
    ("POST", "/ui/lang"): "a display preference (same-origin only)",
    ("*", "/static"): "the application's own scripts and styles",
}
#: Every other route needs a signed-in caller.
AUTHENTICATED = {
    ("POST", "/api/v1/checks/aoa"),
    ("POST", "/api/v1/checks/aoa/review"),
    ("POST", "/api/v1/checks/boq"),
    ("POST", "/api/v1/checks/compare"),
    ("GET", "/api/v1/checks/contractor-types"),
    ("GET", "/api/v1/documents"),
    ("POST", "/api/v1/documents"),
    ("DELETE", "/api/v1/documents/{doc_id}"),
    ("GET", "/api/v1/documents/{doc_id}"),
    ("POST", "/api/v1/documents/{doc_id}/extract"),
    ("GET", "/api/v1/documents/{doc_id}/pages/{page_no}.png"),
    ("GET", "/api/v1/jobs/{job_id}"),
    ("GET", "/api/v1/jobs/{job_id}/result"),
    ("GET", "/api/v1/status"),
    ("GET", "/ui/"),
    ("GET", "/ui/checks/aoa"),
    ("GET", "/ui/checks/aoa/annotated.docx"),
    ("POST", "/ui/checks/aoa/review"),
    ("GET", "/ui/checks/boq"),
    ("GET", "/ui/checks/boq/merged.xlsx"),
    ("GET", "/ui/checks/compare"),
    ("GET", "/ui/checks/compare/annotated.docx"),
    ("GET", "/ui/checks/compare/differences.xlsx"),
    ("POST", "/ui/checks/compare/explain"),
    ("GET", "/ui/documents"),
    ("POST", "/ui/documents/upload"),
    ("GET", "/ui/documents/{doc_id}"),
    ("POST", "/ui/documents/{doc_id}/delete"),
    ("POST", "/ui/documents/{doc_id}/extract"),
    ("GET", "/ui/jobs/{job_id}"),
    ("POST", "/ui/logout"),
    ("GET", "/ui/start/aoa"),
    ("POST", "/ui/start/aoa"),
    ("GET", "/ui/start/boq"),
    ("GET", "/ui/start/compare"),
    ("POST", "/ui/start/compare"),
}


def _discovered(app: Any) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for ctx in iter_route_contexts(app.routes):
        for method in ctx.methods or {"*"}:
            found.add((method, ctx.path or ""))
    return found


# --- the application, two users, and the first one's resources --------------------------------------------------------


class World:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        with session_scope() as s:
            self.alice_key, _ = create_api_key(s, "alice")
            self.bob_key, _ = create_api_key(s, "bob")
        self.alice = {"X-API-Key": self.alice_key}
        self.bob = {"X-API-Key": self.bob_key}
        self.alice_cookie = self._login(self.alice_key)
        self.bob_cookie = self._login(self.bob_key)
        self.pdf = self._upload("alice-scan.pdf", make_pdf(300, 400), wait=False)
        self.doc = self._upload(
            ALICE_FILE, self._docx(f"SUPPLY CONTRACT No. 17/2024 {ALICE_MARK}", "100")
        )
        self.doc2 = self._upload(
            "alice-v2.docx", self._docx(f"SUPPLY CONTRACT No. 17/2024 {ALICE_MARK}", "200")
        )
        self.template = self._upload(
            "alice-template.xlsx", build_boq(blank(), priced=False, stated_total=None)
        )
        self.bid = self._upload("alice-bid.xlsx", build_boq(LINES))
        self.job = self._extract(self.doc)

    # -- setup helpers
    def _login(self, key: str) -> str:
        r = self.client.post(
            "/ui/login", data={"api_key": key}, headers=SAME_ORIGIN, follow_redirects=False
        )
        token = r.cookies.get("pa_session")
        assert token, r.text[:200]
        self.client.cookies.clear()
        return f"pa_session={token}"

    @staticmethod
    def _docx(first: str, price: str) -> bytes:
        doc = WordDocument()
        doc.add_paragraph(first)
        doc.add_paragraph(f"1. The total price is {price} GEL including VAT.")
        out = io.BytesIO()
        doc.save(out)
        return out.getvalue()

    def _upload(
        self, name: str, data: bytes, *, wait: bool = True, headers: dict[str, str] | None = None
    ) -> str:
        headers = headers or self.alice
        r = self.client.post("/api/v1/documents", headers=headers, files={"file": (name, data)})
        assert r.status_code == 201, r.text[:200]
        if not wait:
            return str(r.json()["document"]["id"])
        job = r.json()["parse_job_id"]
        for _ in range(600):
            status = self.client.get(f"/api/v1/jobs/{job}", headers=headers).json()["status"]
            if status in ("done", "failed"):
                assert status == "done"
                break
            time.sleep(0.05)
        return str(r.json()["document"]["id"])

    def _extract(self, doc: str) -> str:
        r = self.client.post(f"/api/v1/documents/{doc}/extract", headers=self.alice, json={})
        job = str(r.json()["id"])
        for _ in range(600):
            if (
                self.client.get(f"/api/v1/jobs/{job}", headers=self.alice).json()["status"]
                == "done"
            ):
                break
            time.sleep(0.05)
        return job

    # -- requests
    def call(self, method: str, path: str, who: str = "anon", **kw: Any) -> Any:
        headers = dict(kw.pop("headers", {}))
        if who == "alice":
            headers.update(self.alice)
        elif who == "bob":
            headers.update(self.bob)
        elif who == "alice-browser":
            headers.update({"Cookie": self.alice_cookie, **SAME_ORIGIN})
        elif who == "bob-browser":
            headers.update({"Cookie": self.bob_cookie, **SAME_ORIGIN})
        self.client.cookies.clear()
        return self.client.request(method, path, headers=headers, follow_redirects=False, **kw)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    tmp = tmp_path_factory.mktemp("matrix")
    settings = Settings(
        env="dev",
        data_dir=tmp / "data",
        ollama_base_url="http://127.0.0.1:1",
        rate_limit="100000/minute",
        retention_days=1,
    )
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield World(client)
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


# --- the inventory is pinned ---------------------------------------------------------------------------------------------


def test_every_route_is_classified_and_every_classified_route_exists(world: World) -> None:
    discovered = _discovered(world.client.app)
    classified = set(PUBLIC) | AUTHENTICATED
    assert discovered - classified == set(), (
        "a route was added without deciding whether it needs a credential: put it in PUBLIC "
        "(with the reason) or AUTHENTICATED, and cover it in the cross-user table below"
    )
    assert classified - discovered == set(), "a classified route no longer exists"


# --- anonymous callers ------------------------------------------------------------------------------------------------------


def _fill(path: str, **values: str) -> str:
    return re.sub(
        r"\{(\w+)\}",
        lambda m: values.get(m.group(1), DUMMY if "page" not in m.group(1) else "1"),
        path,
    )


@pytest.mark.parametrize(("method", "path"), sorted(AUTHENTICATED))
def test_an_anonymous_caller_is_refused_everywhere_else(
    world: World, method: str, path: str
) -> None:
    r = world.call(method, _fill(path))
    assert r.status_code == 401, (method, path, r.status_code, r.text[:120])
    assert r.json()["detail"] == "missing API key"
    r = world.call(method, _fill(path), headers=HTML)
    assert r.status_code in (303, 401), (
        method,
        path,
        r.status_code,
    )  # a browser is sent to sign in
    if r.status_code == 303:
        assert r.headers["location"].startswith("/ui/login")


@pytest.mark.parametrize(
    "key",
    ["", "pa_", "pa_" + "x" * 43, "Bearer " + "x" * 40, "ჯანარეიკა".encode()],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/v1/documents"), ("GET", "/api/v1/status"), ("POST", "/api/v1/checks/compare")],
)
def test_a_wrong_key_is_refused_like_no_key(world: World, key: str, method: str, path: str) -> None:
    r = world.call(
        method, path, headers={"X-API-Key": key}, **({"json": {}} if method == "POST" else {})
    )
    assert r.status_code == 401


def test_a_revoked_key_and_a_dead_session_stop_working_at_once(world: World) -> None:
    with session_scope() as s:
        key, row = create_api_key(s, "carol")
        key_id = row.id
    headers = {"X-API-Key": key}
    assert world.call("GET", "/api/v1/documents", headers=headers).status_code == 200
    from procurement_agent.security.auth import revoke_api_key

    with session_scope() as s:
        assert revoke_api_key(s, key_id)
    assert world.call("GET", "/api/v1/documents", headers=headers).status_code == 401


# --- one user's ids in another user's hands -----------------------------------------------------------------------------------


def _cross_user_requests(w: World) -> list[tuple[str, str, str, dict[str, Any]]]:
    """``(label, method, path, kwargs)`` - every route that takes an id, with Alice's ids."""
    d, d2, t, b, j, pdf = w.doc, w.doc2, w.template, w.bid, w.job, w.pdf
    return [
        ("api get document", "GET", f"/api/v1/documents/{d}", {}),
        ("api delete document", "DELETE", f"/api/v1/documents/{d}", {}),
        ("api extract", "POST", f"/api/v1/documents/{d}/extract", {"json": {}}),
        ("api page image", "GET", f"/api/v1/documents/{pdf}/pages/1.png", {}),
        ("api job", "GET", f"/api/v1/jobs/{j}", {}),
        ("api result json", "GET", f"/api/v1/jobs/{j}/result", {}),
        ("api result xlsx", "GET", f"/api/v1/jobs/{j}/result", {"params": {"format": "xlsx"}}),
        (
            "api aoa",
            "POST",
            "/api/v1/checks/aoa",
            {"json": {"aoa_document_id": d, "contract_document_id": d2}},
        ),
        (
            "api aoa docx",
            "POST",
            "/api/v1/checks/aoa",
            {"json": {"aoa_document_id": d}, "params": {"format": "docx"}},
        ),
        (
            "api aoa review",
            "POST",
            "/api/v1/checks/aoa/review",
            {"json": {"aoa_document_id": d, "contract_document_id": d2}},
        ),
        (
            "api compare",
            "POST",
            "/api/v1/checks/compare",
            {"json": {"left_document_id": d, "right_document_id": d2}},
        ),
        (
            "api compare xlsx",
            "POST",
            "/api/v1/checks/compare",
            {
                "json": {"left_document_id": d, "right_document_id": d2},
                "params": {"format": "xlsx"},
            },
        ),
        (
            "api boq",
            "POST",
            "/api/v1/checks/boq",
            {"json": {"template_document_id": t, "bid_document_ids": [b]}},
        ),
        (
            "api boq xlsx",
            "POST",
            "/api/v1/checks/boq",
            {
                "json": {"template_document_id": t, "bid_document_ids": [b]},
                "params": {"format": "xlsx"},
            },
        ),
        ("ui document", "GET", f"/ui/documents/{d}", {}),
        ("ui delete", "POST", f"/ui/documents/{d}/delete", {}),
        ("ui extract", "POST", f"/ui/documents/{d}/extract", {"data": {"question": "who?"}}),
        ("ui job", "GET", f"/ui/jobs/{j}", {}),
        ("ui aoa", "GET", "/ui/checks/aoa", {"params": {"act": d, "contract": d2}}),
        (
            "ui aoa docx",
            "GET",
            "/ui/checks/aoa/annotated.docx",
            {"params": {"act": d, "contract": d2}},
        ),
        ("ui aoa review", "POST", "/ui/checks/aoa/review", {"params": {"act": d, "contract": d2}}),
        ("ui boq", "GET", "/ui/checks/boq", {"params": {"template": t, "bids": b}}),
        (
            "ui boq merged",
            "GET",
            "/ui/checks/boq/merged.xlsx",
            {"params": {"template": t, "bids": b}},
        ),
        ("ui compare", "GET", "/ui/checks/compare", {"params": {"left": d, "right": d2}}),
        (
            "ui compare docx",
            "GET",
            "/ui/checks/compare/annotated.docx",
            {"params": {"left": d, "right": d2}},
        ),
        (
            "ui compare xlsx",
            "GET",
            "/ui/checks/compare/differences.xlsx",
            {"params": {"left": d, "right": d2}},
        ),
        (
            "ui compare explain",
            "POST",
            "/ui/checks/compare/explain",
            {"params": {"left": d, "right": d2}},
        ),
    ]


def _leaks(r: Any, w: World, *, ids: bool = False) -> list[str]:
    """What of Alice's the response shows. Her ids count only when the requester did not send them:
    a page that links back to the address Bob typed is not telling him anything."""
    needles = [ALICE_MARK, ALICE_FILE, "alice-private"]
    if ids:
        needles += [w.doc, w.doc2, w.job, w.template, w.bid, w.pdf]
    return [n for n in needles if n.encode() in r.content]


def test_the_cross_user_table_covers_every_route_that_takes_an_id(world: World) -> None:
    """Routes with an id in the path, or ``left``/``right``/``act``/``template``/body ids."""
    covered = {
        (m, re.sub(r"[0-9a-f]{32}", "{id}", p)) for _, m, p, _ in _cross_user_requests(world)
    }
    takes_ids = {
        (m, p)
        for m, p in AUTHENTICATED
        if "{" in p or "/checks/" in p or p.endswith(("/upload", "/start/aoa", "/start/compare"))
    }
    uncovered = {
        (m, p)
        for m, p in takes_ids
        if (m, re.sub(r"\{\w+\}", "{id}", p).replace("{id}.png", "1.png")) not in covered
        and not p.endswith(
            ("/upload", "/start/aoa", "/start/compare", "/contractor-types", "/status")
        )
    }
    assert uncovered == set(), uncovered


def test_the_requests_are_real_alice_succeeds_with_each_of_them(world: World) -> None:
    """Without this, a request that fails for an unrelated reason would 'prove' the refusal."""
    failures = []
    for label, method, path, kw in _cross_user_requests(world):
        if label in ("api delete document", "ui delete"):
            continue  # not repeatable: it would remove the fixture
        who = "alice-browser" if label.startswith("ui") else "alice"
        r = world.call(method, path, who, **kw)
        if r.status_code >= 400:
            failures.append((label, r.status_code, r.text[:80]))
    assert failures == []


@pytest.mark.parametrize("index", range(26))
def test_bob_gets_nothing_of_alices_through_any_route(world: World, index: int) -> None:
    label, method, path, kw = _cross_user_requests(world)[index]
    if label.startswith("ui"):
        r = world.call(method, path, "bob-browser", **kw)
        assert r.status_code < 500, (label, r.status_code)
        assert _leaks(r, world) == [], (label, _leaks(r, world))
    else:
        r = world.call(method, path, "bob", **kw)
        assert r.status_code == 404, (label, r.status_code, r.text[:120])
        assert _leaks(r, world) == [], (label, _leaks(r, world))
    # and nothing of Alice's changed
    with session_scope() as s:
        doc = s.get(Document, world.doc)
        assert doc is not None and doc.status != DocumentStatus.deleted


def test_the_lists_show_only_the_callers_own_documents(world: World) -> None:
    mine = world.call("GET", "/api/v1/documents", "alice").json()
    assert {d["id"] for d in mine} >= {world.doc, world.doc2, world.template, world.bid}
    assert world.call("GET", "/api/v1/documents", "bob").json() == []
    for path in ("/ui/documents", "/ui/"):
        page = world.call("GET", path, "bob-browser")
        assert _leaks(page, world, ids=True) == [], path
        assert ALICE_FILE not in page.text
        alice_page = world.call("GET", path, "alice-browser")
        assert (
            "alice" in alice_page.text.lower()
            or ALICE_FILE in alice_page.text
            or alice_page.status_code == 200
        )


def test_a_foreign_id_in_a_list_of_bids_is_refused_too(world: World) -> None:
    template = world._upload(
        "bob-template.xlsx", build_boq(blank(), priced=False, stated_total=None), headers=world.bob
    )
    mine = world._upload("bob-bid.xlsx", build_boq(LINES), headers=world.bob)
    ok = world.call(
        "POST",
        "/api/v1/checks/boq",
        "bob",
        json={"template_document_id": template, "bid_document_ids": [mine]},
    )
    assert ok.status_code == 200  # the request is fine with only Bob's own documents
    r = world.call(
        "POST",
        "/api/v1/checks/boq",
        "bob",
        json={"template_document_id": template, "bid_document_ids": [mine, world.bid]},
    )
    assert r.status_code == 404  # one of the ids is Alice's: the whole request is refused
    assert _leaks(r, world) == []
    r = world.call(
        "POST",
        "/api/v1/checks/boq",
        "bob",
        json={"template_document_id": world.template, "bid_document_ids": [mine]},
    )
    assert r.status_code == 404  # and so is a foreign template
    assert _leaks(r, world) == []


def test_a_deleted_document_is_gone_for_its_owner_too(world: World) -> None:
    doc = world._upload("to-delete.docx", world._docx("temporary", "1"))
    assert world.call("DELETE", f"/api/v1/documents/{doc}", "alice").status_code == 204
    for method, path in (
        ("GET", f"/api/v1/documents/{doc}"),
        ("POST", f"/api/v1/documents/{doc}/extract"),
    ):
        assert (
            world.call(
                method, path, "alice", **({"json": {}} if method == "POST" else {})
            ).status_code
            == 404
        )


# --- a signed-in browser replayed from another site --------------------------------------------------------------------------


BROWSER_POSTS = [
    ("/ui/documents/{doc}/delete", {}),
    ("/ui/documents/{doc}/extract", {"data": {"question": "who?"}}),
    ("/ui/checks/compare/explain", {"params": {"left": "{doc}", "right": "{doc2}"}}),
    ("/ui/checks/aoa/review", {"params": {"act": "{doc}", "contract": "{doc2}"}}),
    ("/ui/documents/upload", {"files": {"file": ("x.docx", b"x")}}),
    ("/ui/start/aoa", {"files": {"act": ("x.docx", b"x")}}),
    ("/ui/start/compare", {"files": {"left": ("x.docx", b"x"), "right": ("y.docx", b"y")}}),
    ("/ui/logout", {}),
]


def _fill_doc(path: str, kw: dict[str, Any], w: World) -> tuple[str, dict[str, Any]]:
    sub = lambda s: s.replace("{doc}", w.doc).replace("{doc2}", w.doc2)  # noqa: E731
    out = dict(kw)
    if "params" in out:
        out["params"] = {k: sub(v) for k, v in out["params"].items()}
    return sub(path), out


@pytest.mark.parametrize(("path", "kw"), BROWSER_POSTS)
@pytest.mark.parametrize(
    "origin",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},  # a sibling site, not this one
        {"Origin": "http://evil.example"},
        {"Referer": "http://evil.example/page"},
        {},  # no origin information at all: refused, not assumed fine
    ],
    ids=["cross-site", "same-site", "foreign-origin", "foreign-referer", "no-origin-info"],
)
def test_a_browser_session_replayed_from_another_site_is_refused(
    world: World, path: str, kw: dict[str, Any], origin: dict[str, str]
) -> None:
    path, kw = _fill_doc(path, kw, world)
    r = world.call("POST", path, headers={"Cookie": world.alice_cookie, **origin}, **kw)
    assert r.status_code == 403, (path, origin, r.status_code, r.text[:100])
    with session_scope() as s:  # nothing happened
        assert s.scalars(
            select(Document).where(
                Document.id == world.doc, Document.status == DocumentStatus.ready
            )
        ).one()


def test_a_same_origin_post_with_the_session_is_let_through(world: World) -> None:
    r = world.call(
        "POST",
        "/ui/checks/aoa/review",
        headers={"Cookie": world.alice_cookie, **SAME_ORIGIN},
        params={"act": world.doc, "contract": world.doc2},
    )
    assert r.status_code not in (401, 403)


def test_an_api_key_header_is_not_subject_to_the_browser_check(world: World) -> None:
    """Scripts send no Origin and no Sec-Fetch-Site; the key itself is the proof."""
    assert world.call("GET", "/api/v1/documents", "alice").status_code == 200
    r = world.call(
        "POST",
        f"/api/v1/documents/{world.doc}/extract",
        "alice",
        json={},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 202


# --- hostile values in the path --------------------------------------------------------------------------------------------------


HOSTILE = [
    "../../etc/passwd",
    "%00",
    "'",
    "a" * 4000,
    "ჯანარეიკა",
    "0" * 32,
    "%2e%2e%2f",
    "{{7*7}}",
    "<script>1</script>",
    "a%0d%0aSet-Cookie:x=1",
]


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize(
    ("method", "path"), sorted(m for m in AUTHENTICATED if "{doc_id}" in m[1] or "{job_id}" in m[1])
)
def test_a_hostile_path_value_is_a_clean_4xx_never_a_500_or_a_stack_trace(
    world: World, method: str, path: str, value: str
) -> None:
    from urllib.parse import quote

    target = (
        path.replace("{doc_id}", quote(value, safe="%"))
        .replace("{job_id}", quote(value, safe="%"))
        .replace("{page_no}", "1")
    )
    who = "alice-browser" if path.startswith("/ui") else "alice"
    kw: dict[str, Any] = {}
    if method == "POST" and "extract" in path:
        kw = {"json": {}} if who == "alice" else {"data": {}}
    r = world.call(method, target, who, **kw)
    assert r.status_code < 500, (method, path, value[:30], r.status_code)
    assert b"Traceback" not in r.content and b"sqlalchemy" not in r.content.lower()
    assert "set-cookie" not in {k.lower() for k in r.headers} or not value.startswith("a%0d")


# --- every answer carries the standard headers ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/ready",
        "/ui/login",
        "/static/app.js",
        "/nonexistent",
        "/api/v1/documents",
        "/api/v1/status",
        "/ui/documents",
        "/ui/",
    ],
)
def test_every_answer_has_the_standard_headers_and_is_not_cached(world: World, path: str) -> None:
    who = (
        "alice-browser"
        if path.startswith("/ui") and path != "/ui/login"
        else ("alice" if path.startswith("/api") else "anon")
    )
    r = world.call("GET", path, who)
    for name, value in COMMON.items():
        assert r.headers[name] == value, (path, name)
    if path.startswith(("/api/", "/ui/")):
        assert "no-store" in r.headers["cache-control"], path


# --- credentials that nearly work --------------------------------------------------------------------------------------------------


def test_a_key_sharing_only_the_prefix_of_a_real_key_is_refused(world: World) -> None:
    """The lookup is by prefix; the proof is the hash. A forged tail must not pass."""
    key = world.alice_key
    for forged in (
        key[:11] + "x" * (len(key) - 11),
        key[:-1] + ("A" if key[-1] != "A" else "B"),
        key[:11] + key[11:][::-1],
        key + "x",
        key[:-1],
    ):
        assert (
            world.call("GET", "/api/v1/documents", headers={"X-API-Key": forged}).status_code == 401
        ), forged[:14]


def test_a_session_past_its_expiry_is_refused_and_one_inside_it_is_not(world: World) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from procurement_agent.models import UiSession

    cookie = world._login(world.alice_key)
    assert world.call("GET", "/ui/documents", headers={"Cookie": cookie}).status_code == 200
    with session_scope() as s:
        s.execute(update(UiSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    r = world.call("GET", "/ui/documents", headers={"Cookie": cookie})
    assert r.status_code == 401
    # the other sessions of the fixture were aged by the statement above: sign in again for the rest
    world.alice_cookie = world._login(world.alice_key)
    world.bob_cookie = world._login(world.bob_key)
    assert world.call("GET", "/ui/documents", "alice-browser").status_code == 200


def test_the_result_of_a_deleted_document_is_not_served_even_to_its_owner(world: World) -> None:
    doc = world._upload("short-lived.docx", world._docx("temporary contract", "5"))
    job = world._extract(doc)
    assert world.call("GET", f"/api/v1/jobs/{job}/result", "alice").status_code == 200
    assert world.call("DELETE", f"/api/v1/documents/{doc}", "alice").status_code == 204
    assert world.call("GET", f"/api/v1/jobs/{job}/result", "alice").status_code == 404
    assert world.call("GET", f"/api/v1/jobs/{job}", "alice").status_code in (200, 404)
    ui = world.call("GET", f"/ui/jobs/{job}", "alice-browser")
    assert ui.status_code < 500 and b"temporary contract" not in ui.content


# --- hostile request bodies ------------------------------------------------------------------------------------------------------

#: What a caller can put in a body, with the content type it claims. Every one must end in a clean
#: 4xx - never a 500, never a stack trace - on every route that reads a body.
BODIES: dict[str, tuple[bytes, str]] = {
    "empty": (b"", "application/json"),
    "null": (b"null", "application/json"),
    "a list": (b"[]", "application/json"),
    "a bare string": (b'"x"', "application/json"),
    "unterminated": (b'{"question": "x', "application/json"),
    "invalid utf-8": (b'{"question": "\xff\xfe"}', "application/json"),
    "200,000 levels of array": (b"[" * 200_000 + b"]" * 200_000, "application/json"),
    "100,000 levels of object": (b'{"a":' * 100_000 + b"1" + b"}" * 100_000, "application/json"),
    "a 6,000-digit integer": (b'{"fast": ' + b"9" * 6000 + b"}", "application/json"),
    "NaN": (b'{"fast": NaN}', "application/json"),
    "wrong types": (b'{"fast": "yes", "question": [1, 2], "use_llm": {}}', "application/json"),
    "a byte-order mark": (b'\xef\xbb\xbf{"fast": true}', "application/json"),
    "plain text": (b'{"fast": true}', "text/plain"),
    "a form": (b"fast=1&question=%ff%fe", "application/x-www-form-urlencoded"),
    "a form with 5,000 fields": (
        b"&".join(b"f%d=1" % i for i in range(5000)),
        "application/x-www-form-urlencoded",
    ),
    "a bad percent escape": (b"question=%zz%", "application/x-www-form-urlencoded"),
    "multipart without a boundary": (
        b"--x\r\nContent-Disposition: form-data\r\n",
        "multipart/form-data",
    ),
    "multipart with a broken part": (
        b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nx',
        "multipart/form-data; boundary=b",
    ),
    "a content type that is not one": (b"{}", "application/json; charset=nonsense-9000"),
}
#: Not driven here: an upload (its own tests: hostile archives, the pre-auth and size checks) and
#: logout, which reads no body and would end the session every later call in this module relies on.
NO_BODY_TEST = {"/api/v1/documents", "/ui/documents/upload", "/ui/logout"}
BODY_ROUTES = sorted(
    (method, path)
    for method, path in AUTHENTICATED | set(PUBLIC)
    if method == "POST" and path not in NO_BODY_TEST
)


@pytest.mark.parametrize("label", list(BODIES))
@pytest.mark.parametrize(("method", "path"), BODY_ROUTES)
def test_a_hostile_body_is_a_clean_4xx_on_every_route_that_reads_one(
    world: World, method: str, path: str, label: str
) -> None:
    body, content_type = BODIES[label]
    public = (method, path) in PUBLIC
    who = "anon" if public else ("alice-browser" if path.startswith("/ui") else "alice")
    headers = {"Content-Type": content_type, **(SAME_ORIGIN if public else {})}
    r = world.call(method, _fill(path), who, content=body, headers=headers)
    assert r.status_code < 500, (method, path, label, r.status_code, r.text[:200])
    assert b"Traceback" not in r.content and b"sqlalchemy" not in r.content.lower()


def test_the_hostile_bodies_really_reach_the_body_parsers(world: World) -> None:
    """A guard against a vacuous pass: most of them are answered by the parsers (400/413/415/422),
    not turned away earlier for want of a credential."""
    answers = [
        world.call("POST", _fill(path), "alice", content=body, headers={"Content-Type": ctype})
        for path in ("/api/v1/checks/compare", "/api/v1/documents/{doc_id}/extract")
        for body, ctype in BODIES.values()
    ]
    parsed = [r for r in answers if r.status_code in (400, 413, 415, 422)]
    assert len(parsed) >= len(answers) // 2, sorted({r.status_code for r in answers})
    assert not [r for r in answers if r.status_code in (401, 403)]
