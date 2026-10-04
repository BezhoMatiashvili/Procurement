"""
A parse that read nothing is not 'ready', and one that read part of a file says so.

Conversions are planned (``tests.degraded_support``); the real parser is exercised in
``test_degraded_docling.py``. Documents are invented.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from docling.datamodel.base_models import ConversionStatus
from fastapi.testclient import TestClient

from procurement_agent.db import session_scope
from procurement_agent.jobs.worker import Worker
from procurement_agent.models import Document, DocumentStatus
from tests.degraded_support import (
    ACT_LINES,
    SAME_ORIGIN,
    XLSX,
    H,
    Plan,
    document,
    empty_xlsx,
    install_fake_convert,
    make_client,
    multipage_pdf,
    settings_of,
    sidecar,
    upload,
    wait_job,
    xlsx_bytes,
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from make_client(tmp_path)


@pytest.fixture
def register(monkeypatch: pytest.MonkeyPatch) -> object:
    return install_fake_convert(monkeypatch, ocr=False)


def _english(client: TestClient) -> None:
    client.post("/ui/lang", data={"lang": "en", "next": "/ui/"}, headers=SAME_ORIGIN)


def _partial_pdf(client: TestClient, register: object, tag: str = "p") -> str:
    """A 5-page PDF of which 2 pages were read: the reader said so, and ready it is."""
    data = register(  # type: ignore[operator]
        multipage_pdf(5, tag),
        Plan(
            pages=5,
            text_on={1: "alpha text", 2: "beta text"},
            status=ConversionStatus.PARTIAL_SUCCESS,
            errors=3,
        ),
    )
    doc_id, _ = upload(client, f"{tag}.pdf", data)
    return doc_id


# --- finding 3: a file that yielded no text is not "ready" ------------------------------------


def test_an_image_only_pdf_without_ocr_fails_with_a_code_not_ready(
    client: TestClient, register: object
) -> None:
    data = register(multipage_pdf(2, "scan"), Plan(pages=2))  # type: ignore[operator]
    doc_id, job_id = upload(client, "scan.pdf", data)

    doc = document(client, doc_id)
    assert doc["status"] == "failed"
    assert doc["error_code"] == "NO_TEXT"
    assert doc["error_hint"] == "OCR_UNAVAILABLE"
    job = wait_job(client, job_id)
    assert job["status"] == "failed"
    assert "NO_TEXT" in str(job["message"])
    assert "scan" not in str(doc["error"]).lower()  # content-free: a code, not a sentence about it


def test_a_workbook_without_any_cell_text_fails_with_a_code(client: TestClient) -> None:
    doc_id, _ = upload(client, "blank.xlsx", empty_xlsx(), XLSX)
    doc = document(client, doc_id)
    assert doc["status"] == "failed"
    assert doc["error_code"] == "NO_TEXT"
    assert doc["error_hint"] is None


def test_a_failed_for_no_text_document_is_refused_by_every_check_with_409(
    client: TestClient, register: object
) -> None:
    scan = register(multipage_pdf(2, "scan"), Plan(pages=2))  # type: ignore[operator]
    bad, _ = upload(client, "scan.pdf", scan)
    good, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)

    aoa = client.post("/api/v1/checks/aoa", headers=H, json={"aoa_document_id": bad})
    cmp_ = client.post(
        "/api/v1/checks/compare",
        headers=H,
        json={"left_document_id": good, "right_document_id": bad},
    )
    ext = client.post(f"/api/v1/documents/{bad}/extract", headers=H, json={})
    assert (aoa.status_code, cmp_.status_code, ext.status_code) == (409, 409, 409)


def test_the_page_of_a_document_that_has_no_text_says_so_in_both_languages(
    client: TestClient, register: object
) -> None:
    scan = register(multipage_pdf(2, "scan"), Plan(pages=2))  # type: ignore[operator]
    doc_id, _ = upload(client, "scan.pdf", scan)
    ka = client.get(f"/ui/documents/{doc_id}").text
    _english(client)
    en = client.get(f"/ui/documents/{doc_id}").text
    assert "NO_TEXT" in ka
    assert "NO_TEXT" in en
    assert "no text" in en.lower()
    assert "ტექსტ" in ka
    assert "OCR" in en


# --- finding 2 and 5: a partly read document is ready, and says so ------------------------------


def test_a_partial_conversion_is_ready_and_records_what_was_not_read(
    client: TestClient, register: object
) -> None:
    doc_id = _partial_pdf(client, register)
    doc = document(client, doc_id)
    assert doc["status"] == "ready"
    q = doc["read_quality"]
    assert q["partial"] is True  # type: ignore[index]
    assert (q["pages_total"], q["pages_with_text"]) == (5, 2)  # type: ignore[index]
    assert q["errors"] == 3  # type: ignore[index]
    assert doc["page_count"] == 5
    # only numbers and flags are stored beside the document
    assert set(sidecar(client, doc_id)) == {
        "kind",
        "pages_total",
        "pages_with_text",
        "text_chars",
        "partial",
        "errors",
        "ocr",
    }


def test_a_pdf_with_one_image_page_among_text_pages_is_partial_even_when_converted_cleanly(
    client: TestClient, register: object
) -> None:
    """The reviewer's case: page 2 is a picture of 'Total amount 999999'. Docling reports
    success; one page of two has text."""
    data = register(multipage_pdf(2, "mixed"), Plan(pages=2, text_on={1: "Page one text"}))  # type: ignore[operator]
    doc_id, _ = upload(client, "mixed.pdf", data)
    doc = document(client, doc_id)
    assert doc["status"] == "ready"
    assert doc["read_quality"]["partial"] is True
    assert doc["read_quality"]["pages_with_text"] == 1


def test_a_page_table_cut_short_by_a_timeout_does_not_hide_the_missing_pages(
    client: TestClient, register: object
) -> None:
    """80 pages in the file, a short timeout: Docling's own page table holds 4."""
    data = register(  # type: ignore[operator]
        multipage_pdf(80, "long"),
        Plan(
            pages=4,
            text_on={1: "a", 2: "b", 3: "c", 4: "d"},
            status=ConversionStatus.PARTIAL_SUCCESS,
            errors=3,
        ),
    )
    doc_id, _ = upload(client, "long.pdf", data)
    doc = document(client, doc_id)
    assert doc["status"] == "ready"
    assert (doc["read_quality"]["pages_total"], doc["read_quality"]["pages_with_text"]) == (80, 4)
    assert doc["page_count"] == 80


def test_a_fully_read_pdf_is_not_partial(client: TestClient, register: object) -> None:
    data = register(  # type: ignore[operator]
        multipage_pdf(2, "full"), Plan(pages=2, text_on={1: "one", 2: "two"})
    )
    doc_id, _ = upload(client, "full.pdf", data)
    assert document(client, doc_id)["read_quality"]["partial"] is False


# --- finding 7: the database fails while a parse is settled ---------------------------------------


def test_a_database_failure_settling_a_parse_job_fails_the_document_too(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import OperationalError

    real = Worker._settle
    calls = {"n": 0}

    def flaky(self: Worker, job_id: str, status: object, msg: object, **kw: object) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:  # the settle that would have marked the document ready
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        return real(self, job_id, status, msg, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Worker, "_settle", flaky)
    doc_id, job_id = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)

    assert wait_job(client, job_id)["status"] == "failed"
    doc = document(client, doc_id)
    assert doc["status"] == "failed", "a document must not stay 'parsing' for ever"
    assert "parsing" not in str(doc["status"])


def test_the_sweep_fails_a_document_stuck_in_parsing_with_no_job_running(
    client: TestClient,
) -> None:
    from datetime import UTC, datetime, timedelta

    doc_id, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    s = settings_of(client)
    old = datetime.now(UTC) - timedelta(seconds=s.docling_timeout_s + 3600)
    with session_scope() as db:
        doc = db.get(Document, doc_id)
        assert doc is not None
        doc.status = DocumentStatus.parsing  # as left by a settle that never happened
        job = doc.jobs[0]
        job.finished_at = old
    worker: Worker = client.app.state.worker  # type: ignore[attr-defined]
    worker.sweep_expired()
    assert document(client, doc_id)["status"] == "failed"


def test_the_sweep_leaves_a_document_that_is_being_parsed_now(client: TestClient) -> None:
    from datetime import UTC, datetime

    doc_id, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    with session_scope() as db:
        doc = db.get(Document, doc_id)
        assert doc is not None
        doc.status = DocumentStatus.parsing
        doc.jobs[0].finished_at = datetime.now(UTC)  # just now, within docling_timeout_s
    client.app.state.worker.sweep_expired()  # type: ignore[attr-defined]
    assert document(client, doc_id)["status"] == "parsing"
