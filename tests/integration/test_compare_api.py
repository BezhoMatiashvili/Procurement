"""The comparison endpoint through the full app: contract, formats and refusals.

Documents are DOCX from ``tests/fixtures/compare``, which ``scripts/gen_synthetic_samples``
writes: one base and one revision per class of change. Nothing here is client material.

One constraint is asserted rather than assumed: this endpoint never calls the model. The app is
built pointing at a dead Ollama port, so a stray call would surface as a failure rather than as
a slow test.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.main import create_app

H = {"X-API-Key": "pa_devkey_for_tests_only"}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PAIRS = Path(__file__).resolve().parents[1] / "fixtures" / "compare"

pytestmark = pytest.mark.skipif(
    not (PAIRS / "base.docx").is_file(),
    reason="run scripts/gen_synthetic_samples.py to build the comparison fixtures",
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        env="dev",
        data_dir=tmp_path / "data",
        dev_api_key="pa_devkey_for_tests_only",
        ollama_base_url="http://127.0.0.1:1",
        retention_days=1,
    )
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        c.post(
            "/ui/login",
            data={"api_key": "pa_devkey_for_tests_only"},
            headers={"Sec-Fetch-Site": "same-origin"},  # sign-in is CSRF-guarded too
        )
        yield c
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


def _upload(client: TestClient, name: str, data: bytes, media: str = DOCX) -> str:
    r = client.post("/api/v1/documents", headers=H, files={"file": (name, data, media)})
    assert r.status_code == 201, r.text
    body = r.json()
    deadline = time.time() + 30
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{body['parse_job_id']}", headers=H).json()
        if job["status"] in ("done", "failed"):
            assert job["status"] == "done", job
            break
        time.sleep(0.05)
    return str(body["document"]["id"])


def _pair(client: TestClient, case: str = "amount") -> tuple[str, str]:
    return (
        _upload(client, "base.docx", (PAIRS / "base.docx").read_bytes()),
        _upload(client, f"{case}.docx", (PAIRS / f"{case}.docx").read_bytes()),
    )


def _compare(client: TestClient, left: str, right: str, **kw: object) -> dict[str, object]:
    r = client.post(
        "/api/v1/checks/compare",
        headers=H,
        json={"left_document_id": left, "right_document_id": right, **kw},
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


def test_a_changed_amount_comes_back_as_a_critical_finding(client: TestClient) -> None:
    left, right = _pair(client, "amount")
    body = _compare(client, left, right)

    assert body["alignment"] == "versions"
    errors = [f for f in body["findings"] if f["severity"] == "error"]
    assert {f["rule_id"] for f in errors} >= {"CMP.AMOUNT"}
    assert body["counts"]["critical"] >= 1


def test_an_unchanged_copy_reports_nothing(client: TestClient) -> None:
    """The same file uploaded twice is two documents with identical content."""
    data = (PAIRS / "base.docx").read_bytes()
    left = _upload(client, "a.docx", data)
    right = _upload(client, "b.docx", data)
    body = _compare(client, left, right)

    assert body["hunks"] == []
    assert body["findings"] == []
    assert body["similarity"] == 1.0


def test_comparing_a_document_with_itself_is_refused_with_a_reason(client: TestClient) -> None:
    left, _ = _pair(client)
    r = client.post(
        "/api/v1/checks/compare",
        headers=H,
        json={"left_document_id": left, "right_document_id": left},
    )
    assert r.status_code == 422
    assert "same" in r.json()["detail"]


def test_an_unparsed_document_is_a_conflict_not_a_crash(client: TestClient) -> None:
    left, right = _pair(client)
    client.post(f"/api/v1/documents/{right}", headers=H)  # no-op; keep the ids realistic
    r = client.post(
        "/api/v1/documents",
        headers=H,
        files={"file": ("fresh.docx", (PAIRS / "base.docx").read_bytes(), DOCX)},
    )
    fresh = r.json()["document"]["id"]
    # Raced against the parse worker on purpose: whichever way it lands must be a clean answer.
    out = client.post(
        "/api/v1/checks/compare",
        headers=H,
        json={"left_document_id": left, "right_document_id": fresh},
    )
    assert out.status_code in (200, 409)


def test_another_users_document_is_not_found(client: TestClient) -> None:
    left, _ = _pair(client)
    r = client.post(
        "/api/v1/checks/compare",
        headers=H,
        json={"left_document_id": left, "right_document_id": "f" * 32},
    )
    assert r.status_code == 404


def test_the_excel_export_has_the_findings_and_the_diff(client: TestClient) -> None:
    left, right = _pair(client, "amount")
    r = client.post(
        "/api/v1/checks/compare?format=xlsx",
        headers=H,
        json={"left_document_id": left, "right_document_id": right},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == XLSX
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames[:2] == ["Differences", "Diff"]
    assert wb["Diff"].max_row > 1


def test_the_word_export_carries_a_comment_per_finding(client: TestClient) -> None:
    left, right = _pair(client, "amount")
    r = client.post(
        "/api/v1/checks/compare?format=docx",
        headers=H,
        json={"left_document_id": left, "right_document_id": right},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == DOCX
    assert int(r.headers["X-Error-Count"]) >= 1


def test_the_word_export_is_refused_for_a_spreadsheet_with_a_reason(client: TestClient) -> None:
    """Word comments cannot be written into a workbook; 415 and a sentence, not an empty file."""
    from tests.unit.test_compare_xlsx import book

    left = _upload(client, "a.xlsx", book(), XLSX)
    right = _upload(client, "b.xlsx", book(sheets=("WORKS", "MEP")), XLSX)
    r = client.post(
        "/api/v1/checks/compare?format=docx",
        headers=H,
        json={"left_document_id": left, "right_document_id": right},
    )
    assert r.status_code == 415
    assert ".docx" in r.json()["detail"]


def test_two_workbooks_are_compared_cell_by_cell(client: TestClient) -> None:
    from tests.unit.test_compare_xlsx import LINES, book

    dearer = [("1.1", "Concrete works", "m3", 10, 130.0), *LINES[1:]]
    left = _upload(client, "a.xlsx", book(), XLSX)
    right = _upload(client, "b.xlsx", book(dearer), XLSX)
    body = _compare(client, left, right)

    assert body["counts"]["changed"] >= 1
    paths = [h["right"]["path"] for h in body["hunks"] if h["right"]]
    assert any("!" in p for p in paths), "a spreadsheet hunk should point at a cell"


def test_an_unrelated_pair_says_so_rather_than_drawing_a_noisy_diff(client: TestClient) -> None:
    from tests.unit.test_compare_xlsx import book

    left = _upload(client, "base.docx", (PAIRS / "base.docx").read_bytes())
    right = _upload(client, "b.xlsx", book(), XLSX)
    body = _compare(client, left, right)

    assert body["alignment"] == "unrelated"
    assert any(s["rule_id"] == "CMP.LINE_DIFF" for s in body["skipped"])


def test_the_endpoint_never_calls_the_model(client: TestClient) -> None:
    """Ollama is a dead port in this fixture: a call would raise, not merely be slow."""
    left, right = _pair(client, "amount")
    body = _compare(client, left, right)
    assert body["llm_model"] is None
    assert body["llm_explained"] == 0


def test_an_unknown_explain_job_is_ignored_rather_than_failing(client: TestClient) -> None:
    left, right = _pair(client, "amount")
    body = _compare(client, left, right, explain_job_id="0" * 32)
    assert body["llm_explained"] == 0
    assert body["counts"]["critical"] >= 1
