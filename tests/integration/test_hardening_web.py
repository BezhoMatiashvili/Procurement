"""Hardening of the web layer, the worker and the session model, through the real app.

Worker races are driven synchronously: the background thread is stopped and the test calls
``Worker._process`` itself, with one stage replaced by a function that deletes the document
mid-job. Racing the real thread would make these tests flaky and prove nothing.
"""

from __future__ import annotations

import io
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from PIL import Image
from sqlalchemy import select

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import session_scope
from procurement_agent.jobs import worker as worker_mod
from procurement_agent.jobs.worker import Worker
from procurement_agent.main import create_app
from procurement_agent.models import AuditLog, Document, DocumentStatus, Job, JobStatus
from tests.conftest import make_pdf

KEY = "pa_devkey_for_tests_only"
H = {"X-API-Key": KEY}
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(total: int = 660) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "BOQ"
    ws.append(["#", "Description", "Qty", "Unit", "Unit price (GEL)", "Total (GEL)"])
    ws.append([1, "cable 3x2.5", 120, "m", 4.5, 540])
    ws.append([2, "breaker 16A", 10, "pcs", 12, 120])
    ws.append(["", "", "", "", "Total:", f"{total} GEL"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "dev",
        "data_dir": tmp_path / "data",
        "dev_api_key": KEY,
        "ollama_base_url": "http://127.0.0.1:1",
        "retention_days": 1,
        "rate_limit": "10000/minute",
    }
    base.update(overrides)
    return Settings(**base)


def _client(tmp_path: Path, **overrides: Any) -> Iterator[TestClient]:
    settings = _settings(tmp_path, **overrides)
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(tmp_path)


def _worker(client: TestClient) -> Worker:
    w: Worker = client.app.state.worker  # type: ignore[attr-defined]
    return w


def _settings_of(client: TestClient) -> Settings:
    s: Settings = client.app.state.settings  # type: ignore[attr-defined]
    return s


def _wait_job(client: TestClient, job_id: str, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = client.get(f"/api/v1/jobs/{job_id}", headers=H).json()
        if j["status"] in ("done", "failed", "cancelled"):
            return dict(j)
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _ready_doc(client: TestClient, data: bytes | None = None) -> str:
    r = client.post("/api/v1/documents", headers=H, files={"file": ("boq.xlsx", data or _xlsx())})
    assert r.status_code == 201, r.text
    assert _wait_job(client, r.json()["parse_job_id"])["status"] == "done"
    return str(r.json()["document"]["id"])


def _job_row(doc_id: str, kind: str, params: dict[str, object] | None = None) -> str:
    """A queued job the background thread has not been told about."""
    with session_scope() as s:
        job = Job(document_id=doc_id, owner="dev", kind=kind, params=params or {})
        s.add(job)
        s.flush()
        return job.id


def _job_status(job_id: str) -> JobStatus:
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        return job.status


def _doc_status(doc_id: str) -> DocumentStatus:
    with session_scope() as s:
        doc = s.get(Document, doc_id)
        assert doc is not None
        return doc.status


def _deleting(client: TestClient, doc_id: str, original: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a pipeline stage so the user deletes the document while it runs."""

    def stage(*args: Any, **kwargs: Any) -> Any:
        out = original(*args, **kwargs)
        assert client.delete(f"/api/v1/documents/{doc_id}", headers=H).status_code == 204
        return out

    return stage


# --- 1. a deleted document stays deleted --------------------------------------------------


def test_deleting_during_an_extraction_leaves_no_result_behind(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = _ready_doc(client)
    job_id = _job_row(doc_id, "extract", {"use_llm": False})
    monkeypatch.setattr(
        worker_mod, "run_extraction", _deleting(client, doc_id, worker_mod.run_extraction)
    )
    _worker(client)._process(job_id)

    assert _job_status(job_id) == JobStatus.cancelled
    assert _doc_status(doc_id) == DocumentStatus.deleted
    assert not (_settings_of(client).results_dir / doc_id).exists()
    assert client.get(f"/api/v1/jobs/{job_id}/result", headers=H).status_code == 404


def test_deleting_during_a_parse_does_not_bring_the_document_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _worker(client).stop()  # the upload's parse job must wait for this test to run it
    r = client.post("/api/v1/documents", headers=H, files={"file": ("boq.xlsx", _xlsx())})
    doc_id, job_id = r.json()["document"]["id"], r.json()["parse_job_id"]
    monkeypatch.setattr(
        worker_mod, "load_chunks", _deleting(client, doc_id, worker_mod.load_chunks)
    )
    _worker(client)._process(job_id)

    assert _doc_status(doc_id) == DocumentStatus.deleted
    assert _job_status(job_id) == JobStatus.cancelled
    assert client.get(f"/api/v1/documents/{doc_id}", headers=H).status_code == 404
    assert not (_settings_of(client).results_dir / doc_id).exists()
    assert not list(_settings_of(client).upload_dir.iterdir())


def test_a_parse_that_fails_after_deletion_does_not_mark_the_document_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _worker(client).stop()
    r = client.post("/api/v1/documents", headers=H, files={"file": ("boq.xlsx", _xlsx())})
    doc_id, job_id = r.json()["document"]["id"], r.json()["parse_job_id"]

    def delete_then_fail(*_: Any, **__: Any) -> Any:
        client.delete(f"/api/v1/documents/{doc_id}", headers=H)
        raise ValueError("unreadable")

    monkeypatch.setattr(worker_mod, "load_chunks", delete_then_fail)
    _worker(client)._process(job_id)
    assert _doc_status(doc_id) == DocumentStatus.deleted
    assert _job_status(job_id) == JobStatus.cancelled


def test_deleting_the_left_document_during_an_explain_leaves_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from procurement_agent.checks.compare_llm import ExplanationSet

    left, right = _ready_doc(client), _ready_doc(client, _xlsx(total=700))
    job_id = _job_row(right, "compare_explain", {"left": left, "right": right})

    def explain(*_: Any, **__: Any) -> ExplanationSet:
        assert client.delete(f"/api/v1/documents/{left}", headers=H).status_code == 204
        return ExplanationSet(model="stub")

    monkeypatch.setattr(worker_mod, "request_explanations", explain)
    _worker(client)._process(job_id)
    assert _job_status(job_id) == JobStatus.cancelled
    assert not (_settings_of(client).results_dir / right / "jobs" / f"{job_id}.json").exists()


def test_deleting_the_contract_during_a_review_leaves_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review hangs off the act, so deleting the contract does not cancel it on its own;
    settling re-checks both documents, and the stored answers go."""
    from procurement_agent.checks.aoa_llm import ScopeReviewSet

    act, contract = _ready_doc(client), _ready_doc(client, _xlsx(total=700))
    job_id = _job_row(act, "aoa_review", {"contract": contract, "digest": "d"})

    def review(*_: Any, **kwargs: Any) -> ScopeReviewSet:
        assert client.delete(f"/api/v1/documents/{contract}", headers=H).status_code == 204
        return ScopeReviewSet(
            model="stub",
            aoa_document_id=kwargs["aoa_document_id"],
            contract_document_id=kwargs["contract_document_id"],
            digest="d",
        )

    monkeypatch.setattr(worker_mod, "request_review", review)
    _worker(client)._process(job_id)
    assert _job_status(job_id) == JobStatus.cancelled
    assert not (_settings_of(client).results_dir / act / "jobs" / f"{job_id}.json").exists()


def test_a_review_whose_contract_is_gone_is_cancelled_before_the_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    act, contract = _ready_doc(client), _ready_doc(client, _xlsx(total=700))
    job_id = _job_row(act, "aoa_review", {"contract": contract, "digest": "d"})
    assert client.delete(f"/api/v1/documents/{contract}", headers=H).status_code == 204

    def never(*_: Any, **__: Any) -> Any:  # pragma: no cover - the point is it is not called
        raise AssertionError("the model was asked about a deleted contract")

    monkeypatch.setattr(worker_mod, "request_review", never)
    _worker(client)._process(job_id)
    assert _job_status(job_id) == JobStatus.cancelled


def test_a_review_job_without_its_contract_fails_plainly(client: TestClient) -> None:
    job_id = _job_row(_ready_doc(client), "aoa_review", {})
    _worker(client)._process(job_id)
    assert _job_status(job_id) == JobStatus.failed


def test_finishing_never_overwrites_a_cancelled_job(client: TestClient) -> None:
    doc_id = _ready_doc(client)
    job_id = _job_row(doc_id, "extract")
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        job.status = JobStatus.cancelled
    _worker(client)._finish(job_id, JobStatus.done, "late")
    assert _job_status(job_id) == JobStatus.cancelled


def test_a_deleted_documents_result_is_not_served(client: TestClient) -> None:
    """Even if a result file survived on disk (the old race), the endpoints refuse it."""
    doc_id = _ready_doc(client)
    r = client.post(f"/api/v1/documents/{doc_id}/extract", headers=H, json={"use_llm": False})
    job_id = r.json()["id"]
    assert _wait_job(client, job_id)["status"] == "done"
    with session_scope() as s:
        doc = s.get(Document, doc_id)
        assert doc is not None
        doc.status = DocumentStatus.deleted  # files deliberately left in place
    assert client.get(f"/api/v1/jobs/{job_id}/result", headers=H).status_code == 404
    client.post("/ui/login", data={"api_key": KEY}, headers=SAME_ORIGIN)
    page = client.get(f"/ui/jobs/{job_id}")
    # "660" is the document's total; as a standalone number, not as a run inside a random hex id
    assert "BOQ" not in page.text
    assert re.search(r"(?<![0-9a-f])660(?![0-9a-f])", page.text) is None


def test_delete_commits_before_it_removes_files(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files first and status second left a window in which the worker could see a live
    document, write its output into a directory that had just been removed, and win."""
    from procurement_agent.api import documents as docs_api

    doc_id = _ready_doc(client)
    seen: list[DocumentStatus] = []
    original = docs_api.delete_document_files

    def spy(settings: Settings, did: str, kind: str) -> None:
        seen.append(_doc_status(did))  # read through a separate connection: committed state
        original(settings, did, kind)

    monkeypatch.setattr(docs_api, "delete_document_files", spy)
    assert client.delete(f"/api/v1/documents/{doc_id}", headers=H).status_code == 204
    assert seen == [DocumentStatus.deleted]


def test_the_retention_sweep_commits_before_it_removes_files(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    doc_id = _ready_doc(client)
    with session_scope() as s:
        doc = s.get(Document, doc_id)
        assert doc is not None
        doc.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    seen: list[DocumentStatus] = []
    original = worker_mod.delete_document_files

    def spy(settings: Settings, did: str, kind: str) -> None:
        seen.append(_doc_status(did))
        original(settings, did, kind)

    monkeypatch.setattr(worker_mod, "delete_document_files", spy)
    assert _worker(client).sweep_expired() == 1
    assert seen == [DocumentStatus.deleted]


# --- 9. worker robustness ------------------------------------------------------------------


def test_a_restart_fails_documents_left_parsing(client: TestClient) -> None:
    doc_id = _ready_doc(client)
    with session_scope() as s:
        doc = s.get(Document, doc_id)
        assert doc is not None
        doc.status = DocumentStatus.parsing
    w = Worker(_settings_of(client), None)
    w._recover()
    with session_scope() as s:
        doc = s.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.failed
        assert doc.error == "interrupted by restart"


def test_the_worker_loop_survives_finish_raising(client: TestClient) -> None:
    _worker(client).stop()
    w = Worker(_settings_of(client), None)
    calls: list[str] = []

    def boom_process(job_id: str) -> None:
        calls.append(job_id)
        raise RuntimeError("stage")

    def boom_finish(*_: Any) -> None:
        raise RuntimeError("database is locked")

    w._process = boom_process  # type: ignore[method-assign]
    w._finish = boom_finish  # type: ignore[method-assign]
    w.start()
    try:
        w.submit("a")
        w.submit("b")
        deadline = time.time() + 5
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert calls == ["a", "b"]
        assert w.is_alive
    finally:
        w.stop()
    assert not w.is_alive


def test_stop_does_not_wait_behind_queued_jobs(client: TestClient) -> None:
    _worker(client).stop()
    w = Worker(_settings_of(client), None)
    ran: list[str] = []

    def slow(job_id: str) -> None:
        ran.append(job_id)
        time.sleep(0.3)

    w._process = slow  # type: ignore[method-assign]
    w.start()
    for i in range(10):
        w.submit(f"j{i}")
    time.sleep(0.05)
    started = time.monotonic()
    w.stop()
    assert time.monotonic() - started < 2.0
    assert len(ran) <= 2  # the one running finishes; the queue stays queued in the database


def test_the_heartbeat_moves(client: TestClient) -> None:
    w = _worker(client)
    assert w.is_alive
    assert w.heartbeat_age < 60


def test_a_user_cannot_queue_unbounded_jobs(tmp_path: Path) -> None:
    for c in _client(tmp_path, max_active_jobs_per_user=2):
        doc_id = _ready_doc(c)
        _worker(c).stop()  # nothing drains the queue from here on
        codes = [
            c.post(f"/api/v1/documents/{doc_id}/extract", headers=H, json={"use_llm": False})
            for _ in range(3)
        ]
        assert [r.status_code for r in codes] == [202, 202, 429]
        assert "Retry-After" in codes[-1].headers
        r = c.post("/api/v1/documents", headers=H, files={"file": ("b.xlsx", _xlsx())})
        assert r.status_code == 429
        c.post("/ui/login", data={"api_key": KEY}, headers=SAME_ORIGIN)
        page = c.post(
            f"/ui/documents/{doc_id}/extract", data={"use_llm": "true"}, headers=SAME_ORIGIN
        )
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]


# --- 7. server-side sessions ---------------------------------------------------------------


def _real_key(user: str = "alice") -> tuple[str, str]:
    from procurement_agent.security.auth import create_api_key

    with session_scope() as s:
        key, row = create_api_key(s, user)
        return key, row.id


def _login(client: TestClient, key: str) -> str:
    r = client.post("/ui/login", data={"api_key": key}, headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303, r.text
    token = r.cookies.get("pa_session")
    assert token
    return str(token)


JSON = {"Accept": "application/json"}


def _as_cookie(client: TestClient, token: str) -> int:
    """Status of a UI page fetched with exactly this cookie and nothing else."""
    client.cookies.clear()
    client.cookies.set("pa_session", token)
    return client.get("/ui/documents", headers=JSON).status_code


def test_the_session_cookie_is_not_the_api_key(client: TestClient) -> None:
    from procurement_agent.models import UiSession

    key, _ = _real_key()
    token = _login(client, key)
    assert token != key and key not in token
    assert token.startswith("ps_")
    assert client.get("/ui/documents").status_code == 200
    with session_scope() as s:
        rows = s.scalars(select(UiSession)).all()
        assert [r.user for r in rows] == ["alice"]
        assert all(token not in (r.token_hash, r.id) for r in rows)  # only the hash is stored


def test_a_cookie_holding_an_api_key_is_refused(client: TestClient) -> None:
    """Old cookies were the raw key; there must be no fallback that still honours them."""
    key, _ = _real_key()
    assert _as_cookie(client, key) == 401
    assert _as_cookie(client, KEY) == 401  # the dev key, likewise


def test_logout_revokes_the_session_on_the_server(client: TestClient) -> None:
    key, _ = _real_key()
    token = _login(client, key)
    r = client.post("/ui/logout", headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert _as_cookie(client, token) == 401  # a copy of the cookie is dead too


def test_an_expired_session_is_refused(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from procurement_agent.models import UiSession

    token = _login(client, _real_key()[0])
    with session_scope() as s:
        for row in s.scalars(select(UiSession)):
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert _as_cookie(client, token) == 401


def test_the_session_lifetime_is_the_setting(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from procurement_agent.models import UiSession

    before = datetime.now(UTC)
    _login(client, _real_key()[0])
    with session_scope() as s:
        row = s.scalars(select(UiSession)).one()
        expires = row.expires_at.replace(tzinfo=UTC)
    assert timedelta(hours=7, minutes=59) < expires - before <= timedelta(hours=8, minutes=1)


def test_revoking_the_key_ends_its_sessions(client: TestClient) -> None:
    from procurement_agent.security.auth import revoke_api_key

    key, key_id = _real_key()
    token = _login(client, key)
    with session_scope() as s:
        assert revoke_api_key(s, key_id)
    assert _as_cookie(client, token) == 401


def test_a_dev_session_dies_with_the_dev_key(client: TestClient) -> None:
    token = _login(client, KEY)
    assert _as_cookie(client, token) == 200
    _settings_of(client).dev_api_key = None
    assert _as_cookie(client, token) == 401


def test_signing_in_again_retires_the_previous_session(client: TestClient) -> None:
    key, _ = _real_key()
    first = _login(client, key)
    _login(client, key)
    assert _as_cookie(client, first) == 401


def test_sign_in_is_refused_from_another_site(client: TestClient) -> None:
    """Login CSRF: a hostile page could sign the victim into the attacker's account."""
    r = client.post(
        "/ui/login",
        data={"api_key": KEY},
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "pa_session" not in r.cookies


@pytest.mark.parametrize("key", ["pa_ქართული_გასაღები_12345678901", "ключ", "pa_é" + "x" * 30])
def test_a_non_ascii_key_is_refused_not_a_500(client: TestClient, key: str) -> None:
    r = client.post("/ui/login", data={"api_key": key}, headers=SAME_ORIGIN)
    assert r.status_code == 200
    assert 'id="key-error"' in r.text
    raw = client.get("/api/v1/documents", headers={"X-API-Key": key.encode("utf-8")})
    assert raw.status_code == 401


def test_the_invalid_key_message_is_translated(client: TestClient) -> None:
    r = client.post("/ui/login", data={"api_key": "pa_wrong_key_000000000000"}, headers=SAME_ORIGIN)
    assert "API გასაღები არასწორია" in r.text
    client.cookies.set("pa_lang", "en")
    r = client.post("/ui/login", data={"api_key": "pa_wrong_key_000000000000"}, headers=SAME_ORIGIN)
    assert "Invalid API key" in r.text


# --- 5. the audit trail --------------------------------------------------------------------


def _audits(action: str) -> list[AuditLog]:
    with session_scope() as s:
        return list(s.scalars(select(AuditLog).where(AuditLog.action == action)))


def test_a_refused_upload_is_audited_even_though_the_request_fails(client: TestClient) -> None:
    """The denial used to be added to the request session, which the 400 then rolled back.
    A real key (not the dev key) also exercises the last-used write, which must not hold the
    write lock the separate audit transaction needs."""
    key, _ = _real_key()
    started = time.monotonic()
    r = client.post(
        "/api/v1/documents", headers={"X-API-Key": key}, files={"file": ("x.xlsx", b"nope")}
    )
    assert r.status_code == 400
    assert time.monotonic() - started < 5
    rows = _audits("document.upload")
    assert [(a.actor, a.outcome) for a in rows] == [("alice", "denied")]
    assert rows[0].ip == "testclient"


def test_a_wrong_key_and_a_cross_site_post_are_audited_without_the_key(
    client: TestClient,
) -> None:
    wrong = "pa_wrongwrongwrongwrongwrong"
    assert client.get("/api/v1/documents", headers={"X-API-Key": wrong}).status_code == 401
    denied = _audits("auth.denied")
    assert [(a.actor, a.outcome, a.ip) for a in denied] == [("anonymous", "denied", "testclient")]
    assert wrong not in (denied[0].detail or "")

    _login(client, KEY)
    r = client.post("/ui/logout", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert len(_audits("auth.csrf")) == 1


def test_a_request_without_any_credential_is_not_audited(client: TestClient) -> None:
    """Every browser's first visit is one; auditing them would bury the real refusals."""
    assert client.get("/api/v1/documents").status_code == 401
    assert _audits("auth.denied") == []


def test_sign_in_audits_carry_the_address(client: TestClient) -> None:
    client.post("/ui/login", data={"api_key": "pa_wrong_key_000000000000"}, headers=SAME_ORIGIN)
    _login(client, KEY)
    assert [(a.outcome, a.ip) for a in _audits("ui.login")] == [
        ("denied", "testclient"),
        ("ok", "testclient"),
    ]


def test_viewing_a_page_image_is_audited_and_not_cached(client: TestClient, min_pdf: bytes) -> None:
    _worker(client).stop()  # no Docling needed: the page renders from the upload itself
    r = client.post("/api/v1/documents", headers=H, files={"file": ("a.pdf", min_pdf)})
    doc_id = r.json()["document"]["id"]
    page = client.get(f"/api/v1/documents/{doc_id}/pages/1.png", headers=H)
    assert page.status_code == 200
    assert "no-store" in page.headers["cache-control"]
    views = _audits("document.page_view")
    assert [(a.document_id, a.detail) for a in views] == [(doc_id, "page 1")]


def _uploaded_pdf(client: TestClient, pdf: bytes) -> str:
    _worker(client).stop()  # no Docling needed: the page renders from the upload itself
    r = client.post("/api/v1/documents", headers=H, files={"file": ("a.pdf", pdf)})
    assert r.status_code == 201, r.text
    return str(r.json()["document"]["id"])


@pytest.mark.parametrize("box", ["nan,0,10,10", "0,inf,10,10", "0,0,-inf,10", "1e999,0,10,10"])
def test_a_page_image_refuses_a_bbox_that_is_no_place_on_a_page(
    client: TestClient, min_pdf: bytes, box: str
) -> None:
    doc_id = _uploaded_pdf(client, min_pdf)
    page = client.get(f"/api/v1/documents/{doc_id}/pages/1.png", params={"bbox": box}, headers=H)
    assert page.status_code == 400


def test_a_page_image_with_extreme_boxes_is_quick(client: TestClient, min_pdf: bytes) -> None:
    """One box with an edge at 7e8 held the GIL for five seconds, and twenty are accepted."""
    doc_id = _uploaded_pdf(client, min_pdf)
    started = time.perf_counter()
    page = client.get(
        f"/api/v1/documents/{doc_id}/pages/1.png",
        params=[("bbox", "50,-7e8,60,7e8")] * 20,
        headers=H,
    )
    assert page.status_code == 200
    assert time.perf_counter() - started < 3.0


def test_a_page_the_pdf_makes_enormous_is_rendered_inside_the_pixel_cap(client: TestClient) -> None:
    from procurement_agent.ingestion.render import MAX_PIXELS

    doc_id = _uploaded_pdf(client, make_pdf(14400, 14400))
    page = client.get(f"/api/v1/documents/{doc_id}/pages/1.png", headers=H)
    assert page.status_code == 200
    width, height = Image.open(io.BytesIO(page.content)).size
    assert width * height <= MAX_PIXELS * 1.01


# --- 13. last-used bookkeeping -------------------------------------------------------------


def test_last_used_is_written_at_most_once_a_minute(client: TestClient) -> None:
    from procurement_agent.models import ApiKey

    key, key_id = _real_key()

    def last_used() -> object:
        with session_scope() as s:
            row = s.get(ApiKey, key_id)
            assert row is not None
            return row.last_used_at

    client.get("/api/v1/documents", headers={"X-API-Key": key})
    first = last_used()
    assert first is not None
    for _ in range(3):
        client.get("/api/v1/documents", headers={"X-API-Key": key})
    assert last_used() == first


# --- 2. the body cap -----------------------------------------------------------------------


def _multipart(files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "hardeningboundary"
    parts = []
    for field, name, data in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
            f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
            + data
            + b"\r\n"
        )
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _chunks(data: bytes, size: int = 64 * 1024) -> Iterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


@pytest.fixture
def small(tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(tmp_path, max_file_mb=1)


def test_a_chunked_upload_over_the_cap_is_refused(small: TestClient) -> None:
    """No Content-Length, so the old header check never fired."""
    body, ctype = _multipart([("file", "big.xlsx", b"x" * (3 * 1024 * 1024))])
    r = small.post(
        "/api/v1/documents",
        headers={**H, "Content-Type": ctype},
        content=_chunks(body),
    )
    assert r.status_code == 413
    assert r.json() == {"detail": "request body too large"}
    assert not list(_settings_of(small).upload_dir.iterdir())


def test_a_declared_oversize_body_is_refused_up_front(small: TestClient) -> None:
    body, ctype = _multipart([("file", "big.xlsx", b"x" * (3 * 1024 * 1024))])
    r = small.post("/api/v1/documents", headers={**H, "Content-Type": ctype}, content=body)
    assert r.status_code == 413


def test_the_two_file_forms_fit_under_the_cap(small: TestClient) -> None:
    """The act and comparison forms post two files in one request. Two files at the per-file
    limit were over the old cap of one file plus a megabyte, so the second file was refused."""
    _login(small, KEY)
    full = b"x" * (1024 * 1024)
    for path, fields in (
        ("/ui/start/aoa", [("act", "a.docx", full), ("contract", "c.docx", full)]),
        ("/ui/start/compare", [("left", "l.docx", full), ("right", "r.docx", full)]),
    ):
        body, ctype = _multipart(fields)
        r = small.post(path, headers={**SAME_ORIGIN, "Content-Type": ctype}, content=body)
        assert r.status_code == 200, (path, r.status_code)  # the upload is refused, as a page
        assert "text/html" in r.headers["content-type"]


def test_an_anonymous_multipart_body_is_refused_before_it_is_read(
    small: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette import formparsers

    parsed: list[bool] = []
    original = formparsers.MultiPartParser.parse

    async def spy(self: formparsers.MultiPartParser) -> Any:
        parsed.append(True)
        return await original(self)

    monkeypatch.setattr(formparsers.MultiPartParser, "parse", spy)
    body, ctype = _multipart([("file", "a.xlsx", b"x" * 1000)])
    r = small.post("/api/v1/documents", headers={"Content-Type": ctype}, content=_chunks(body))
    assert r.status_code == 401
    assert parsed == []  # nothing was spooled for a caller with no credential at all


# --- 4. uploads off the event loop ---------------------------------------------------------


def test_upload_validation_runs_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from procurement_agent.api import documents as docs_api

    on_loop: list[bool] = []
    original = docs_api.validate_upload

    def spy(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return original(*args, **kwargs)

    monkeypatch.setattr(docs_api, "validate_upload", spy)
    assert (
        client.post("/api/v1/documents", headers=H, files={"file": ("a.xlsx", _xlsx())}).status_code
        == 201
    )
    _login(client, KEY)
    client.post("/ui/documents/upload", headers=SAME_ORIGIN, files={"file": ("b.xlsx", _xlsx())})
    assert on_loop == [False, False]


def test_a_failed_commit_takes_the_uploaded_file_with_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.orm import Session

    def failing_commit(self: Session) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(Session, "commit", failing_commit)
    with pytest.raises(RuntimeError, match="disk full"):
        client.post("/api/v1/documents", headers=H, files={"file": ("a.xlsx", _xlsx())})
    monkeypatch.undo()
    assert not list(_settings_of(client).upload_dir.glob("*"))


# --- 8. UI refusals render pages -----------------------------------------------------------


@pytest.fixture
def ui(client: TestClient) -> TestClient:
    _login(client, KEY)
    return client


def _extract_params(doc_id: str) -> list[dict[str, object]]:
    with session_scope() as s:
        jobs = s.scalars(select(Job).where(Job.document_id == doc_id, Job.kind == "extract"))
        return [dict(j.params) for j in jobs]


def test_unticking_use_llm_is_respected(ui: TestClient) -> None:
    doc_id = _ready_doc(ui)
    _worker(ui).stop()
    ui.post(
        f"/ui/documents/{doc_id}/extract", data={"targets": "total_amount"}, headers=SAME_ORIGIN
    )
    ui.post(f"/ui/documents/{doc_id}/extract", data={"use_llm": "true"}, headers=SAME_ORIGIN)
    assert [p["use_llm"] for p in _extract_params(doc_id)] == [False, True]
    page = ui.get(f"/ui/documents/{doc_id}").text
    # opt-in: the model measured as a precision regression on the client's documents
    assert 'name="use_llm" value="true">' in page
    assert 'name="use_llm" value="true" checked' not in page


def test_the_model_is_off_unless_asked_for_and_a_question_turns_it_on(client: TestClient) -> None:
    doc_id = _ready_doc(client)
    _worker(client).stop()
    client.post(f"/api/v1/documents/{doc_id}/extract", headers=H, json={})
    client.post(
        f"/api/v1/documents/{doc_id}/extract", headers=H, json={"question": "what is the fee?"}
    )
    assert [p["use_llm"] for p in _extract_params(doc_id)] == [False, True]


def _is_page(r: Any) -> bool:
    return r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_a_short_question_renders_a_page_not_a_500(ui: TestClient) -> None:
    doc_id = _ready_doc(ui)
    r = ui.post(f"/ui/documents/{doc_id}/extract", data={"question": "ab"}, headers=SAME_ORIGIN)
    assert _is_page(r)
    assert "3" in r.text and "500" in r.text


def test_extracting_from_a_document_not_ready_renders_a_page(ui: TestClient) -> None:
    doc_id = _ready_doc(ui)
    with session_scope() as s:
        doc = s.get(Document, doc_id)
        assert doc is not None
        doc.status = DocumentStatus.parsing
    r = ui.post(f"/ui/documents/{doc_id}/extract", data={}, headers=SAME_ORIGIN)
    assert _is_page(r)
    assert "not ready" in r.text


@pytest.mark.parametrize(
    "query", ["template={t}&bids=", "template={t}&bids=,,,", "template=" + "a" * 65 + "&bids={t}"]
)
def test_malformed_boq_links_render_a_page_not_a_500(ui: TestClient, query: str) -> None:
    template = _ready_doc(ui)
    r = ui.get("/ui/checks/boq?" + query.format(t=template))
    assert _is_page(r)


def test_a_boq_refusal_says_why_instead_of_expired(ui: TestClient) -> None:
    template, bid = _ready_doc(ui), _ready_doc(ui)
    with session_scope() as s:
        doc = s.get(Document, bid)
        assert doc is not None
        doc.status = DocumentStatus.parsing
    for path in ("/ui/checks/boq", "/ui/checks/boq/merged.xlsx"):
        r = ui.get(f"{path}?template={template}&bids={bid}")
        assert _is_page(r)
        assert "not ready" in r.text


def test_an_act_not_ready_renders_a_page_not_json(ui: TestClient) -> None:
    act = _ready_doc(ui)
    with session_scope() as s:
        doc = s.get(Document, act)
        assert doc is not None
        doc.status = DocumentStatus.parsing
    for path in ("/ui/checks/aoa", "/ui/checks/aoa/annotated.docx"):
        r = ui.get(f"{path}?act={act}")
        assert _is_page(r)
        assert "not ready" in r.text


def test_every_ui_response_is_uncacheable(ui: TestClient) -> None:
    doc_id = _ready_doc(ui)
    for path in ("/ui/", "/ui/documents", f"/ui/documents/{doc_id}", "/ui/start/aoa"):
        assert ui.get(path).headers["cache-control"] == "no-store", path
    assert ui.get("/static/app.css").headers.get("cache-control") != "no-store"


def test_the_language_toggle_works_before_sign_in(client: TestClient) -> None:
    r = client.post(
        "/ui/lang",
        data={"lang": "en", "next": "/ui/login"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.cookies.get("pa_lang") == "en"
    cross = client.post("/ui/lang", data={"lang": "en"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403


def _pair_with_job(owner: str, *, kind: str = "compare_explain") -> tuple[str, str, str]:
    """Two live documents and an explain job over them, all written straight to the DB."""
    with session_scope() as s:
        docs = [
            Document(owner="dev", display_name=n, kind="xlsx", size_bytes=1, sha256="0" * 64)
            for n in ("l.xlsx", "r.xlsx")
        ]
        s.add_all(docs)
        s.flush()
        job = Job(
            document_id=docs[1].id,
            owner=owner,
            kind=kind,
            params={"left": docs[0].id, "right": docs[1].id},
        )
        s.add(job)
        s.flush()
        return docs[0].id, docs[1].id, job.id


@pytest.mark.parametrize(
    ("owner", "kind", "job_id", "expected"),
    [
        ("dev", "compare_explain", None, True),
        ("mallory", "compare_explain", None, False),  # someone else's run
        ("dev", "extract", None, False),  # a job of another kind
        ("dev", "compare_explain", "../../x/jobs/y", False),  # a path, not an id
        ("dev", "compare_explain", "a" * 65, False),
    ],
)
def test_the_compare_job_parameter_must_be_the_callers_explain_run(
    client: TestClient, owner: str, kind: str, job_id: str | None, expected: bool
) -> None:
    from procurement_agent.api.checks import owned_explain_job
    from procurement_agent.security.auth import Principal

    left, right, real = _pair_with_job(owner, kind=kind)
    with session_scope() as s:
        found = owned_explain_job(
            s, Principal("dev", None), job_id or real, s.get(Document, left), s.get(Document, right)
        )
        assert (found is not None) == expected


def _act_with_review(owner: str, *, kind: str = "aoa_review") -> tuple[str, str, str]:
    """An act, its contract and a review job over the pair, all written straight to the DB."""
    with session_scope() as s:
        docs = [
            Document(owner="dev", display_name=n, kind="docx", size_bytes=1, sha256="0" * 64)
            for n in ("act.docx", "contract.docx")
        ]
        s.add_all(docs)
        s.flush()
        job = Job(document_id=docs[0].id, owner=owner, kind=kind, params={"contract": docs[1].id})
        s.add(job)
        s.flush()
        return docs[0].id, docs[1].id, job.id


@pytest.mark.parametrize(
    ("owner", "kind", "job_id", "expected"),
    [
        ("dev", "aoa_review", None, True),
        ("mallory", "aoa_review", None, False),  # someone else's review
        ("dev", "compare_explain", None, False),  # a job of another kind
        ("dev", "aoa_review", "../../x/jobs/y", False),  # a path, not an id
        ("dev", "aoa_review", "a" * 65, False),
        ("dev", "aoa_review", "", False),
    ],
)
def test_the_act_review_parameter_must_be_the_callers_review(
    client: TestClient, owner: str, kind: str, job_id: str | None, expected: bool
) -> None:
    from procurement_agent.api.checks import owned_review_job
    from procurement_agent.security.auth import Principal

    act, contract, real = _act_with_review(owner, kind=kind)
    with session_scope() as s:
        found = owned_review_job(
            s,
            Principal("dev", None),
            real if job_id is None else job_id,
            s.get(Document, act),
            s.get(Document, contract),
        )
        assert (found is not None) == expected


def test_a_review_of_another_pair_or_of_no_contract_is_ignored(client: TestClient) -> None:
    from procurement_agent.api.checks import owned_review_job
    from procurement_agent.security.auth import Principal

    act, _, job = _act_with_review("dev")
    _, other_contract, _ = _act_with_review("dev")
    with session_scope() as s:
        principal, doc = Principal("dev", None), s.get(Document, act)
        assert doc is not None
        assert owned_review_job(s, principal, job, doc, s.get(Document, other_contract)) is None
        assert owned_review_job(s, principal, job, doc, None) is None


def test_an_explain_run_for_another_pair_is_ignored(client: TestClient) -> None:
    from procurement_agent.api.checks import owned_explain_job
    from procurement_agent.security.auth import Principal

    _, right, job = _pair_with_job("dev")
    other_left, _, _ = _pair_with_job("dev")
    with session_scope() as s:
        found = owned_explain_job(
            s, Principal("dev", None), job, s.get(Document, other_left), s.get(Document, right)
        )
        assert found is None


# --- 10. readiness -------------------------------------------------------------------------


def test_ready_when_the_database_and_worker_are_up(client: TestClient) -> None:
    r = client.get("/ready")
    assert (r.status_code, r.json()) == (200, {"status": "ready"})


def test_not_ready_when_the_worker_is_down(client: TestClient) -> None:
    _worker(client).stop()
    r = client.get("/ready")
    assert (r.status_code, r.json()) == (503, {"status": "unavailable", "reason": "worker_down"})


def test_not_ready_when_an_idle_worker_stopped_beating(client: TestClient) -> None:
    _worker(client)._heartbeat = time.monotonic() - 1_000
    r = client.get("/ready")
    assert (r.status_code, r.json()["reason"]) == (503, "worker_stalled")


def test_a_worker_busy_with_a_long_job_is_still_ready(client: TestClient) -> None:
    """A single Docling or LLM call may run its whole timeout without a progress beat."""
    w = _worker(client)
    w._busy_since = time.monotonic() - 500
    w._heartbeat = time.monotonic() - 500
    assert client.get("/ready").status_code == 200


def test_ready_is_not_rate_limited(tmp_path: Path) -> None:
    for c in _client(tmp_path, rate_limit="2/minute"):
        assert {c.get("/ready").status_code for _ in range(6)} == {200}
