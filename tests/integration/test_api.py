"""API tests through the full app (worker thread, SQLite, file storage) — LLM disabled,
XLSX only so Docling models are not needed."""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.main import create_app


def _boq_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "BOQ"
    ws.append(["#", "Description", "Qty", "Unit", "Unit price (GEL)", "Total (GEL)"])
    ws.append([1, "კაბელი 3x2.5", 120, "მ", 4.5, 540])
    ws.append([2, "ავტომატი 16A", 10, "ცალი", 12, 120])
    ws.append(["", "", "", "", "სულ ჯამი:", "660 ლარი"])
    ws.append(["", "მიწოდების ვადა: 30 კალენდარული დღე", "", "", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        env="dev",
        data_dir=tmp_path / "data",
        dev_api_key="pa_devkey_for_tests_only",
        ollama_base_url="http://127.0.0.1:1",  # nothing listens here; LLM is disabled per request
        retention_days=1,
    )
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


H = {"X-API-Key": "pa_devkey_for_tests_only"}


def _wait(client: TestClient, job_id: str, timeout: float = 20.0) -> dict[str, object]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = client.get(f"/api/v1/jobs/{job_id}", headers=H).json()
        if j["status"] in ("done", "failed", "cancelled"):
            return dict(j)
        time.sleep(0.1)
    msg = "job did not finish"
    raise AssertionError(msg)


def test_health_is_public_and_minimal(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_auth_required(client: TestClient) -> None:
    assert client.get("/api/v1/documents").status_code == 401
    assert client.get("/api/v1/documents", headers={"X-API-Key": "pa_wrong"}).status_code == 401
    assert client.get("/api/v1/status", headers=H).status_code == 200


def test_upload_rejects_bad_file(client: TestClient) -> None:
    r = client.post("/api/v1/documents", headers=H, files={"file": ("x.xlsx", b"not a zip")})
    assert r.status_code == 400
    assert "not a valid" in r.json()["detail"]
    r = client.post("/api/v1/documents", headers=H, files={"file": ("x.exe", b"MZ")})
    assert r.status_code == 400


def test_xlsx_roundtrip_rules_only(client: TestClient, tmp_path: Path) -> None:
    r = client.post("/api/v1/documents", headers=H, files={"file": ("boq.xlsx", _boq_xlsx())})
    assert r.status_code == 201, r.text
    doc_id = r.json()["document"]["id"]
    parse = _wait(client, r.json()["parse_job_id"])
    assert parse["status"] == "done", parse
    assert client.get(f"/api/v1/documents/{doc_id}", headers=H).json()["status"] == "ready"

    r = client.post(f"/api/v1/documents/{doc_id}/extract", headers=H, json={"use_llm": False})
    assert r.status_code == 202
    job = _wait(client, r.json()["id"])
    assert job["status"] == "done", job

    res = client.get(f"/api/v1/jobs/{job['id']}/result", headers=H).json()
    types = {f["type"] for f in res["facts"]}
    assert "total_amount" in types
    assert "delivery_deadline" in types
    total = next(f for f in res["facts"] if f["type"] == "total_amount")
    assert total["provenance"]["sheet"] == "BOQ"
    assert total["provenance"]["cell"]

    x = client.get(f"/api/v1/jobs/{job['id']}/result?format=xlsx", headers=H)
    assert x.status_code == 200
    assert x.headers["content-type"].startswith("application/vnd.openxmlformats")

    # uploaded file is stored under our uuid with restrictive permissions, never the user's name
    stored = list((tmp_path / "data" / "uploads").iterdir())
    assert len(stored) == 1
    assert stored[0].name == f"{doc_id}.xlsx"
    assert stored[0].stat().st_mode & 0o777 == 0o600

    # delete removes files and hides the document
    assert client.delete(f"/api/v1/documents/{doc_id}", headers=H).status_code == 204
    assert client.get(f"/api/v1/documents/{doc_id}", headers=H).status_code == 404
    assert not stored[0].exists()
    assert client.get(f"/api/v1/jobs/{job['id']}/result", headers=H).status_code == 404


def test_documents_are_owner_scoped(client: TestClient) -> None:
    """A second user (real key) must not see the dev user's document."""
    from procurement_agent.db import session_scope
    from procurement_agent.security.auth import create_api_key

    with session_scope() as s:
        other_key, _ = create_api_key(s, "bob")
    r = client.post("/api/v1/documents", headers=H, files={"file": ("boq.xlsx", _boq_xlsx())})
    doc_id = r.json()["document"]["id"]
    bob = {"X-API-Key": other_key}
    assert client.get(f"/api/v1/documents/{doc_id}", headers=bob).status_code == 404
    assert client.get("/api/v1/documents", headers=bob).json() == []
    assert client.delete(f"/api/v1/documents/{doc_id}", headers=bob).status_code == 404


def test_extract_requires_ready_document(client: TestClient) -> None:
    r = client.post("/api/v1/documents", headers=H, files={"file": ("boq.xlsx", _boq_xlsx())})
    doc_id = r.json()["document"]["id"]
    _wait(client, r.json()["parse_job_id"])
    r = client.post(f"/api/v1/documents/{doc_id}/extract", headers=H, json={"question": "x"})
    assert r.status_code == 422  # question too short


def test_ui_login_cookie_and_csrf(client: TestClient) -> None:
    # an API-style request (no Accept: text/html) still gets a plain 401
    assert client.get("/ui/documents", headers={"Accept": "application/json"}).status_code == 401
    r = client.post(
        "/ui/login", data={"api_key": "pa_devkey_for_tests_only"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "pa_session" in r.cookies
    assert "HttpOnly" in r.headers["set-cookie"]
    assert client.get("/ui/documents").status_code == 200
    # cookie-authenticated POST from another site is refused
    r = client.post("/ui/logout", headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/ui/logout", headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert r.status_code == 303


HTML = {"Accept": "text/html,application/xhtml+xml"}


def test_ui_unauthenticated_browser_is_sent_to_login(client: TestClient) -> None:
    """A browser opening a deep link without a session lands on the form, not on raw JSON."""
    r = client.get("/ui/documents", headers=HTML, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login?next=%2Fui%2Fdocuments"

    r = client.get("/ui/documents", headers=HTML)
    assert r.status_code == 200
    assert "Sign in" in r.text

    # and signing in returns you to where you were headed
    r = client.post(
        "/ui/login",
        data={"api_key": "pa_devkey_for_tests_only", "next": "/ui/documents"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/documents"


@pytest.mark.parametrize(
    "hostile",
    ["//evil.example", "https://evil.example", "/api/v1/documents", "/ui/login", "\\evil"],
)
def test_ui_login_next_cannot_be_an_open_redirect(client: TestClient, hostile: str) -> None:
    r = client.post(
        "/ui/login",
        data={"api_key": "pa_devkey_for_tests_only", "next": hostile},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/documents"
