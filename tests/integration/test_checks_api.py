"""The AOA check endpoint through the full app.

Documents are XLSX so that Docling models are not needed: the checker reads text, and an XLSX
whose cells hold the act's wording exercises the same path. The rules themselves are covered
exhaustively in tests/unit/test_aoa_check.py.
"""

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

H = {"X-API-Key": "pa_devkey_for_tests_only"}

CONTRACT_LINES = [
    "ხელშეკრულება",
    "შპს „ალფა ჯგუფი“. საიდენტიფიკაციო კოდი: 123456789; "
    "იურიდიული მისამართი: ქ. თბილისი, რუსთაველის 12",
    "(შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის ნინო კაპანაძის სახით",
    "„მზე“ პროექტისთვის (ს/კ: 01.02.03.456) მომსახურების გაწევა.",
    "შეასრულოს მომსახურება 2026 წლის 1 მარტიდან - 2026 წლის 1 სექტემბრის ჩათვლით.",
    "ღირებულება შეადგენს 100 (ასი) ლარს.",
]
ACT_LINES = [
    "მიღება-ჩაბარების აქტი",
    "შპს „ალფა ჯგუფი“. საიდენტიფიკაციო კოდი: 123456789; "
    "იურიდიული მისამართი: ქ. თბილისი, რუსთაველის 12",
    "(შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის ნინო კაპანაძის სახით",
    "პროექტ „მზე“ (ს/კ: 01.02.03.456)",
    "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით.",
    "საფასური შეადგენს 100 (ორასი) ლარს.",
    "აქტი გაფორმდა 2026 წლის 30 აპრილი",
]


def _xlsx(lines: list[str]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for line in lines:
        ws.append([line])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_client(tmp_path: Path, **overrides: object) -> Iterator[TestClient]:
    settings = Settings(
        env="dev",
        data_dir=tmp_path / "data",
        dev_api_key="pa_devkey_for_tests_only",
        ollama_base_url="http://127.0.0.1:1",
        retention_days=1,
        **overrides,  # type: ignore[arg-type]
    )
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
    yield from _make_client(tmp_path)


def _upload(client: TestClient, name: str, lines: list[str]) -> str:
    r = client.post(
        "/api/v1/documents",
        headers=H,
        files={
            "file": (
                name,
                _xlsx(lines),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    deadline = time.time() + 20
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{body['parse_job_id']}", headers=H).json()
        if job["status"] in ("done", "failed"):
            assert job["status"] == "done", job
            break
        time.sleep(0.1)
    return str(body["document"]["id"])


def test_authentication_is_required(client: TestClient) -> None:
    r = client.post("/api/v1/checks/aoa", json={"aoa_document_id": "x"})
    assert r.status_code == 401


def test_an_unknown_document_is_not_distinguishable_from_a_foreign_one(client: TestClient) -> None:
    r = client.post("/api/v1/checks/aoa", headers=H, json={"aoa_document_id": "nope"})
    assert r.status_code == 404


def test_the_check_runs_against_the_contract(client: TestClient) -> None:
    contract = _upload(client, "contract.xlsx", CONTRACT_LINES)
    act = _upload(client, "act.xlsx", ACT_LINES)
    r = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": act, "contract_document_id": contract},
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["document_id"] == act
    assert report["reference_document_id"] == contract
    assert "AOA.AMOUNT_WORDS" in {f["rule_id"] for f in report["findings"]}


def test_rules_needing_a_contract_are_reported_as_skipped(client: TestClient) -> None:
    act = _upload(client, "act.xlsx", ACT_LINES)
    report = client.post("/api/v1/checks/aoa", headers=H, json={"aoa_document_id": act}).json()
    assert any(s["rule_id"].startswith("AOA.CONTRACT") for s in report["skipped"])


def test_findings_are_bilingual(client: TestClient) -> None:
    act = _upload(client, "act.xlsx", ACT_LINES)
    report = client.post("/api/v1/checks/aoa", headers=H, json={"aoa_document_id": act}).json()
    finding = next(f for f in report["findings"] if f["rule_id"] == "AOA.AMOUNT_WORDS")
    assert finding["message_ka"] and finding["message_en"]


def test_an_unknown_contractor_type_is_rejected(client: TestClient) -> None:
    act = _upload(client, "act.xlsx", ACT_LINES)
    r = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": act, "contractor_type": "wizard"},
    )
    assert r.status_code == 422


def test_contractor_types_are_discoverable(client: TestClient) -> None:
    r = client.get("/api/v1/checks/contractor-types", headers=H)
    assert r.status_code == 200
    assert "small_business" in r.json()["contractor_types"]


def test_docx_output_is_refused_for_a_spreadsheet(client: TestClient) -> None:
    """Scanned PDFs and spreadsheets cannot carry Word comments; say so instead of failing
    silently or returning an empty document."""
    act = _upload(client, "act.xlsx", ACT_LINES)
    r = client.post("/api/v1/checks/aoa?format=docx", headers=H, json={"aoa_document_id": act})
    assert r.status_code == 415
    assert "Scanned PDFs" in r.json()["detail"]


def test_the_rate_limiter_is_actually_wired(tmp_path: Path) -> None:
    """The Limiter and its 429 handler existed but no middleware applied them, so the
    configured limit never took effect."""
    for client in _make_client(tmp_path, rate_limit="3/minute"):
        codes = [client.get("/api/v1/status", headers=H).status_code for _ in range(6)]
        assert 429 in codes, codes
        break
