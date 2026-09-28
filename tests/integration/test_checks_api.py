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
from sqlalchemy import select

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import session_scope
from procurement_agent.main import create_app
from procurement_agent.models import Job
from tests.unit.test_aoa_llm import ReadingLLM

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


#: the act in dollars: 11,000 USD, the work done and the act signed on 17 September
USD_ACT_LINES = [
    *ACT_LINES[:4],
    "მომსახურება შესრულდა 2026 წლის 17 სექტემბერს.",
    "საფასური შეადგენს 11 000 (თერთმეტი ათასი) აშშ დოლარს.",
    "აქტი გაფორმდა 2026 წლის 17 სექტემბერი",
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


def test_a_contract_rule_runs_end_to_end(client: TestClient) -> None:
    """The service period against the contract's performance window (rule 10.5): the act a
    year early, its work and its date both in 2025."""
    contract = _upload(client, "contract.xlsx", CONTRACT_LINES)
    act = _upload(
        client, "act.xlsx", [line.replace("2026 წლის", "2025 წლის") for line in ACT_LINES]
    )
    report = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": act, "contract_document_id": contract},
    ).json()
    [finding] = [f for f in report["findings"] if f["rule_id"] == "AOA.PERIOD_IN_TERM"]
    # a year earlier lands the whole period inside the window: the year is what was mistyped,
    # and the correction is the period as the act writes it, with the right year
    assert (finding["expected"], finding["actual"]) == (
        "2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის",
        "2025 წლის 1 აპრილიდან 2025 წლის 30 აპრილის",
    )


def test_a_note_for_review_is_flagged_in_the_report(client: TestClient) -> None:
    """A subject the act words its own way is a note for a person to read: the report says so
    with ``needs_review``, which every other finding carries as false. A spreadsheet has no
    list lines, so the services are named as not compared."""
    contract = _upload(
        client,
        "contract.xlsx",
        [
            *CONTRACT_LINES,
            "ხელშეკრულების საგანი: ვებგვერდის დიზაინის მომსახურების გაწევა (შემდგომში "
            "„მომსახურება“).",
        ],
    )
    act = _upload(
        client,
        "act.xlsx",
        [
            *ACT_LINES,
            "რომლის საგანსაც წარმოადგენს საიტის ვიზუალის შექმნა (შემდგომში „მომსახურება“).",
        ],
    )
    report = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": act, "contract_document_id": contract},
    ).json()
    [note] = [f for f in report["findings"] if f["rule_id"] == "AOA.SUBJECT"]
    assert (note["severity"], note["needs_review"]) == ("info", True)
    assert note["quote"] == "საიტის ვიზუალის შექმნა"
    others = [f for f in report["findings"] if f["rule_id"] != "AOA.SUBJECT"]
    assert others and all(f["needs_review"] is False for f in others)
    assert "AOA.SERVICES" in {s["rule_id"] for s in report["skipped"]}


#: the pair above whose subject the act words its own way: one note the model can be asked about
SUBJECT_CONTRACT = [
    *CONTRACT_LINES,
    "ხელშეკრულების საგანი: ვებგვერდის დიზაინის მომსახურების გაწევა (შემდგომში „მომსახურება“).",
]
SUBJECT_ACT = [
    *ACT_LINES,
    "რომლის საგანსაც წარმოადგენს საიტის ვიზუალის შექმნა (შემდგომში „მომსახურება“).",
]


def _with_model(client: TestClient, llm: object) -> None:
    client.app.state.llm = llm  # type: ignore[attr-defined]
    client.app.state.worker._llm = llm  # type: ignore[attr-defined]


def _finished(client: TestClient, job_id: str) -> str:
    deadline = time.time() + 20
    while time.time() < deadline:
        status = str(client.get(f"/api/v1/jobs/{job_id}", headers=H).json()["status"])
        if status not in ("queued", "running"):
            return status
        time.sleep(0.05)
    raise AssertionError("the review did not finish")


def _review_job_count(act: str) -> int:
    with session_scope() as s:
        return len(
            list(s.scalars(select(Job).where(Job.document_id == act, Job.kind == "aoa_review")))
        )


def test_the_model_review_is_queued_once_and_applied_on_request(client: TestClient) -> None:
    llm = ReadingLLM()
    _with_model(client, llm)
    ids = {
        "aoa_document_id": _upload(client, "act.xlsx", SUBJECT_ACT),
        "contract_document_id": _upload(client, "contract.xlsx", SUBJECT_CONTRACT),
    }
    first = client.post("/api/v1/checks/aoa/review", headers=H, json=ids)
    assert first.status_code == 202, first.text
    job = first.json()["job"]
    assert (first.json()["items"], job["kind"]) == (1, "aoa_review")
    assert _finished(client, job["id"]) == "done"

    again = client.post("/api/v1/checks/aoa/review", headers=H, json=ids)
    assert again.status_code == 200
    assert again.json()["job"]["id"] == job["id"] and again.json()["job"]["status"] == "done"
    assert len(llm.calls) == 1

    reviewed = client.post(
        "/api/v1/checks/aoa", headers=H, json={**ids, "review_job_id": job["id"]}
    ).json()
    [note] = [f for f in reviewed["findings"] if f["rule_id"] == "AOA.SUBJECT"]
    assert (note["severity"], note["needs_review"], note["reviewed_by"]) == ("warning", True, "llm")
    assert reviewed["summary"]["review"] == "current"

    # asked without the job, the report is the rules' alone
    plain = client.post("/api/v1/checks/aoa", headers=H, json=ids).json()
    [note] = [f for f in plain["findings"] if f["rule_id"] == "AOA.SUBJECT"]
    assert (note["severity"], note["reviewed_by"]) == ("info", None)
    assert "review" not in plain["summary"] and plain["summary"]["review_items"] == "1"


def test_nothing_open_queues_no_review(client: TestClient) -> None:
    _with_model(client, ReadingLLM())
    ids = {
        "aoa_document_id": _upload(client, "act.xlsx", ACT_LINES),
        "contract_document_id": _upload(client, "contract.xlsx", CONTRACT_LINES),
    }
    r = client.post("/api/v1/checks/aoa/review", headers=H, json=ids)
    assert (r.status_code, r.json()) == (200, {"job": None, "items": 0})
    assert _review_job_count(ids["aoa_document_id"]) == 0


def test_no_review_job_is_created_without_a_model(client: TestClient) -> None:
    _with_model(client, None)
    ids = {
        "aoa_document_id": _upload(client, "act.xlsx", SUBJECT_ACT),
        "contract_document_id": _upload(client, "contract.xlsx", SUBJECT_CONTRACT),
    }
    r = client.post("/api/v1/checks/aoa/review", headers=H, json=ids)
    assert r.status_code == 503
    assert _review_job_count(ids["aoa_document_id"]) == 0


def test_the_review_is_refused_when_the_operator_turns_it_off(tmp_path: Path) -> None:
    for c in _make_client(tmp_path, aoa_llm_enabled=False):
        ids = {
            "aoa_document_id": _upload(c, "act.xlsx", SUBJECT_ACT),
            "contract_document_id": _upload(c, "contract.xlsx", SUBJECT_CONTRACT),
        }
        assert c.post("/api/v1/checks/aoa/review", headers=H, json=ids).status_code == 409
        assert _review_job_count(ids["aoa_document_id"]) == 0


def test_a_failed_review_is_reused_not_queued_again(client: TestClient) -> None:
    llm = ReadingLLM(error=RuntimeError("the model process died"))
    _with_model(client, llm)
    ids = {
        "aoa_document_id": _upload(client, "act.xlsx", SUBJECT_ACT),
        "contract_document_id": _upload(client, "contract.xlsx", SUBJECT_CONTRACT),
    }
    job = client.post("/api/v1/checks/aoa/review", headers=H, json=ids).json()["job"]
    assert _finished(client, job["id"]) == "failed"
    again = client.post("/api/v1/checks/aoa/review", headers=H, json=ids)
    assert (again.status_code, again.json()["job"]["id"]) == (200, job["id"])
    assert len(llm.calls) == 1 and _review_job_count(ids["aoa_document_id"]) == 1
    # a failed review has nothing to apply: the report is the rules'
    report = client.post(
        "/api/v1/checks/aoa", headers=H, json={**ids, "review_job_id": job["id"]}
    ).json()
    assert all(f["reviewed_by"] is None for f in report["findings"])


def test_a_review_job_id_that_is_not_the_callers_is_ignored(client: TestClient) -> None:
    ids = {
        "aoa_document_id": _upload(client, "act.xlsx", SUBJECT_ACT),
        "contract_document_id": _upload(client, "contract.xlsx", SUBJECT_CONTRACT),
    }
    for bad in ("../../etc/passwd", "a" * 64, "nosuchjob"):
        r = client.post("/api/v1/checks/aoa", headers=H, json={**ids, "review_job_id": bad})
        assert r.status_code == 200
        assert "review" not in r.json()["summary"]
    too_long = client.post("/api/v1/checks/aoa", headers=H, json={**ids, "review_job_id": "a" * 65})
    assert too_long.status_code == 422


def test_an_entered_advance_reaches_the_invoice_note(client: TestClient) -> None:
    act = _upload(client, "act.xlsx", USD_ACT_LINES)
    r = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": act, "advance_amount": "1000", "advance_date": "2026-09-10"},
    )
    assert r.status_code == 200, r.text
    report = r.json()
    [note] = [f for f in report["findings"] if f["rule_id"] == "AOA.FX_INVOICE"]
    assert note["severity"] == "info"
    assert (
        "1,000.00 USD × NBG rate of 2026-09-10 + 10,000.00 USD × NBG rate of 2026-09-17"
        in note["message_en"]
    )
    assert (report["summary"]["advance_amount"], report["summary"]["advance_source"]) == (
        "1000.00",
        "input",
    )
    # no rate source is connected, and the report says what it could not do
    assert "AOA.FX_RATE" in {s["rule_id"] for s in report["skipped"]}


@pytest.mark.parametrize(
    "bad",
    [
        {"advance_amount": "abc"},
        {"advance_amount": -1},
        {"advance_amount": "1000.555"},
        {"advance_amount": "1" * 16},
        {"advance_date": "10.09.2026"},
        {"advance_date": "1800-01-01"},
    ],
)
def test_a_malformed_advance_is_refused(client: TestClient, bad: dict[str, object]) -> None:
    """The API refuses what the report page's link quietly leaves out: a caller here can fix
    it."""
    r = client.post("/api/v1/checks/aoa", headers=H, json={"aoa_document_id": "x", **bad})
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
