"""Pressing a job, or an uploaded file, shows what came of it.

Every row of the Jobs table opens a result: the reading job the content it read, an extraction
its fields, a model job the report it belongs to. The documents here are synthetic - the Word
fixture, ``boq_factory`` workbooks and files built in memory.
"""

from __future__ import annotations

import io
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from docx import Document as WordDocument
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select

from procurement_agent.config import Settings
from procurement_agent.db import session_scope
from procurement_agent.models import AuditLog, Document, DocumentStatus, Job, JobStatus
from procurement_agent.security.auth import create_api_key
from procurement_agent.ui.i18n import STRINGS
from tests.boq_factory import LINES, build_boq
from tests.integration.test_ui_flows import (
    DOCX,
    SAME_ORIGIN,
    XLSX,
    H,
    _act,
    _make_ui_client,
    _upload,
)

#: The contract number in tests/fixtures/contract_ka.docx.
CONTRACT_NO = "PR-2026/017"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from _make_ui_client(tmp_path)


def ka(key: str) -> str:
    return STRINGS[key][1]


def _settings(client: TestClient) -> Settings:
    s: Settings = client.app.state.settings  # type: ignore[attr-defined]
    return s


def _bid(client: TestClient, name: str = "Company-1.xlsx") -> str:
    return _upload(client, name, build_boq(LINES, stated_total=1000.0), XLSX)


def _word(client: TestClient, *lines: str) -> str:
    doc = WordDocument()
    for line in lines:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return _upload(client, "note.docx", buf.getvalue(), DOCX)


def _jobs(doc_id: str, kind: str | None = None) -> list[Job]:
    with session_scope() as s:
        query = select(Job).where(Job.document_id == doc_id).order_by(Job.created_at)
        if kind:
            query = query.where(Job.kind == kind)
        return list(s.scalars(query))


def _parse_job(doc_id: str) -> str:
    return _jobs(doc_id, "parse")[0].id


def _set_job(job_id: str, **fields: object) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        for name, value in fields.items():
            setattr(job, name, value)


def _model_job(doc_id: str, kind: str, params: dict[str, object]) -> str:
    """A job row nobody has told the worker about: queued, and it stays that way."""
    with session_scope() as s:
        job = Job(document_id=doc_id, owner="dev", kind=kind, params=params)
        s.add(job)
        s.flush()
        return job.id


def _extract(client: TestClient, doc_id: str) -> str:
    r = client.post(f"/api/v1/documents/{doc_id}/extract", headers=H, json={"use_llm": False})
    assert r.status_code == 202, r.text
    job_id = str(r.json()["id"])
    deadline = time.time() + 30
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}", headers=H).json()
        if job["status"] in ("done", "failed"):
            assert job["status"] == "done", job
            return job_id
        time.sleep(0.05)
    raise AssertionError("the extraction did not finish")


def _audits(action: str) -> list[AuditLog]:
    with session_scope() as s:
        return list(s.scalars(select(AuditLog).where(AuditLog.action == action)))


def _long_xlsx(rows: int) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Long"
    for r in range(1, rows + 1):
        ws.cell(r, 1, f"row-{r}")
        ws.cell(r, 2, r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- the rows are pressable ----------------------------------------------------------------


def test_every_job_row_is_a_link_whatever_the_job_was(client: TestClient) -> None:
    doc_id = _bid(client)
    extraction = _extract(client, doc_id)
    parse = _parse_job(doc_id)
    page = client.get(f"/ui/documents/{doc_id}").text
    for job_id in (parse, extraction):
        assert f'<tr data-href="/ui/jobs/{job_id}">' in page
        assert f'<a href="/ui/jobs/{job_id}"><code>{job_id[:8]}</code></a>' in page
    assert page.count('class="go"') == 2  # and a "View" cell, so the way in is spelled out
    assert ka("job.view") in page and ka("job.result") in page and ka("job.lead") in page


def test_the_row_script_follows_a_row_but_never_leaves_the_interface() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "src/procurement_agent/ui/static/app.js").read_text()
    assert 'querySelectorAll("tr[data-href]")' in script
    # Ctrl/Cmd/Shift/middle-click are left to the anchor, so a new tab still opens
    assert "e.button !== 0" in script and "e.ctrlKey" in script and "e.metaKey" in script
    assert 'indexOf("/ui/") !== 0' in script


def test_an_uploaded_files_row_in_the_documents_list_is_pressable(client: TestClient) -> None:
    doc_id = _bid(client)
    page = client.get("/ui/documents").text
    assert f'<tr data-href="/ui/documents/{doc_id}">' in page


def test_the_page_that_lists_the_jobs_has_no_inline_script_or_handler(client: TestClient) -> None:
    page = client.get(f"/ui/documents/{_bid(client)}").text
    assert not re.search(r"<script(?![^>]*\ssrc=)", page)
    assert not re.search(r"\son[a-z]+=", page) and 'style="' not in page


# --- the reading job ------------------------------------------------------------------------


def test_the_reading_job_shows_what_was_read_from_a_word_file(client: TestClient) -> None:
    doc_id = _act(client)
    parse = _parse_job(doc_id)
    r = client.get(f"/ui/jobs/{parse}")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert ka("reading.title") in r.text
    assert CONTRACT_NO in r.text  # the file's own text, not a summary of it
    assert f'href="/ui/documents/{doc_id}#extract"' in r.text  # the way on to the fields
    # reading a document's content leaves a trace, as rendering one of its pages does
    [row] = _audits("document.content_view")
    assert (row.actor, row.document_id, row.job_id, row.outcome) == ("dev", doc_id, parse, "ok")
    assert row.detail == "part 1 of 1"


def test_the_reading_job_shows_a_workbook_as_sheets_with_column_letters(
    client: TestClient,
) -> None:
    page = client.get(f"/ui/jobs/{_parse_job(_bid(client))}").text
    assert "1. WORKS" in page and "SUMMARY" in page  # both sheets, by name
    assert "Concrete works" in page
    assert '<th scope="col">A</th>' in page  # a cell can be named as Excel would name it
    assert '<th scope="row" class="rowno">3</th>' in page  # row 3 is where the first line sits
    assert ka("reading.stat.sheets") in page


def test_text_in_a_document_cannot_become_markup_on_the_page(client: TestClient) -> None:
    doc_id = _word(client, "<b>bold</b> <script>alert(1)</script>")
    page = client.get(f"/ui/jobs/{_parse_job(doc_id)}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)" not in page and "<b>bold</b>" not in page


def test_a_long_reading_is_paged_and_a_link_edited_into_nonsense_still_opens(
    client: TestClient,
) -> None:
    doc_id = _upload(client, "Long.xlsx", _long_xlsx(300), XLSX)
    url = f"/ui/jobs/{_parse_job(doc_id)}"
    first = client.get(url).text
    assert "<td>row-1</td>" in first and "<td>row-121</td>" not in first
    assert f"{url}?part=2" in first and ka("reading.prev") not in first

    second = client.get(f"{url}?part=2").text
    assert "<td>row-121</td>" in second and "<td>row-1</td>" not in second

    last = client.get(f"{url}?part=99999").text  # past the end: the last part, not an error
    assert "<td>row-300</td>" in last
    assert ka("reading.prev") in last and ka("reading.next") not in last

    for nonsense in ("abc", "0", "-3", "", "2.5"):
        r = client.get(f"{url}?part={nonsense}")
        assert r.status_code == 200 and "<td>row-1</td>" in r.text, nonsense
    assert client.get(f"{url}?part={'9' * 5000}").status_code == 200


def test_a_reading_that_is_still_running_polls_its_job(client: TestClient) -> None:
    parse = _parse_job(_bid(client))
    _set_job(parse, status=JobStatus.running, progress=40, message="converting")
    page = client.get(f"/ui/jobs/{parse}").text
    assert f'data-poll-job="{parse}"' in page and "40%" in page
    assert "Concrete works" not in page


def test_a_reading_that_failed_says_why_and_shows_no_content(client: TestClient) -> None:
    doc_id = _bid(client)
    parse = _parse_job(doc_id)
    _set_job(parse, status=JobStatus.failed, message="conversion failed: boom")
    with session_scope() as s:
        doc = s.get(Document, doc_id)
        assert doc is not None
        doc.status, doc.error = DocumentStatus.failed, "conversion failed: boom"
    page = client.get(f"/ui/jobs/{parse}").text
    assert ka("reading.failed") in page and "conversion failed: boom" in page
    assert "Concrete works" not in page
    assert not _audits("document.content_view")  # nothing was read


def test_a_reading_whose_stored_parse_is_gone_is_a_notice_not_a_500(client: TestClient) -> None:
    doc_id = _act(client)
    (_settings(client).results_dir / doc_id / "docling.json").unlink()
    r = client.get(f"/ui/jobs/{_parse_job(doc_id)}")
    assert r.status_code == 200
    assert ka("reading.unavailable") in r.text and CONTRACT_NO not in r.text
    assert not _audits("document.content_view")


def test_the_reading_page_speaks_english_when_asked(client: TestClient) -> None:
    parse = _parse_job(_bid(client))
    client.post("/ui/lang", data={"lang": "en", "next": "/ui/"}, headers=SAME_ORIGIN)
    page = client.get(f"/ui/jobs/{parse}").text
    assert STRINGS["reading.title"][0] in page and ka("reading.title") not in page


# --- extraction and the model jobs ----------------------------------------------------------


def test_an_extraction_job_still_opens_its_result(client: TestClient) -> None:
    job_id = _extract(client, _bid(client))
    page = client.get(f"/ui/jobs/{job_id}").text
    assert f"/api/v1/jobs/{job_id}/result?format=xlsx" in page  # the download of its result
    assert ka("result.gone") not in page


def test_an_extraction_whose_result_file_is_gone_says_so(client: TestClient) -> None:
    doc_id = _bid(client)
    job_id = _extract(client, doc_id)
    (_settings(client).results_dir / doc_id / "jobs" / f"{job_id}.json").unlink()
    r = client.get(f"/ui/jobs/{job_id}")
    assert r.status_code == 200 and ka("result.gone") in r.text
    assert "format=xlsx" not in r.text


def test_an_extraction_whose_result_file_is_damaged_says_so(client: TestClient) -> None:
    doc_id = _bid(client)
    job_id = _extract(client, doc_id)
    (_settings(client).results_dir / doc_id / "jobs" / f"{job_id}.json").write_text("{not json")
    r = client.get(f"/ui/jobs/{job_id}")
    assert r.status_code == 200 and ka("result.gone") in r.text


def test_a_model_job_opens_the_report_it_belongs_to(client: TestClient) -> None:
    left, right = _bid(client, "Old.xlsx"), _bid(client, "New.xlsx")
    explain = _model_job(right, "compare_explain", {"left": left, "right": right})
    r = client.get(f"/ui/jobs/{explain}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/ui/checks/compare?left={left}&right={right}&job={explain}"
    assert client.get(f"/ui/jobs/{explain}").status_code == 200  # and the report does open

    review = _model_job(right, "aoa_review", {"contract": left, "digest": "d"})
    r = client.get(f"/ui/jobs/{review}", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (
        303,
        f"/ui/checks/aoa?act={right}&contract={left}&review={review}",
    )


def test_a_model_job_that_lost_its_parameters_opens_something_that_exists(
    client: TestClient,
) -> None:
    doc_id = _bid(client)
    explain = _model_job(doc_id, "compare_explain", {})
    r = client.get(f"/ui/jobs/{explain}", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, f"/ui/documents/{doc_id}")
    review = _model_job(doc_id, "aoa_review", {})
    r = client.get(f"/ui/jobs/{review}", follow_redirects=False)
    assert r.headers["location"] == f"/ui/checks/aoa?act={doc_id}&review={review}"


# --- who may open it ------------------------------------------------------------------------


def test_a_deleted_documents_job_opens_the_expired_page(client: TestClient) -> None:
    doc_id = _act(client)
    parse = _parse_job(doc_id)
    assert client.delete(f"/api/v1/documents/{doc_id}", headers=H).status_code == 204
    r = client.get(f"/ui/jobs/{parse}")
    assert r.status_code == 200 and ka("expired.title") in r.text
    assert CONTRACT_NO not in r.text


def test_another_users_job_is_not_opened(client: TestClient) -> None:
    doc_id = _act(client)
    parse = _parse_job(doc_id)
    explain = _model_job(doc_id, "compare_explain", {"left": doc_id, "right": doc_id})
    with session_scope() as s:
        bob_key, _ = create_api_key(s, "bob")
    client.cookies.clear()
    login = client.post(
        "/ui/login", data={"api_key": bob_key}, headers=SAME_ORIGIN, follow_redirects=False
    )
    assert login.status_code == 303

    page = client.get(f"/ui/jobs/{parse}")
    assert page.status_code == 200 and ka("expired.title") in page.text
    assert CONTRACT_NO not in page.text
    # a redirect would tell Bob the job exists; he gets the same page as for a job that does not
    r = client.get(f"/ui/jobs/{explain}", follow_redirects=False)
    assert r.status_code == 200 and ka("expired.title") in r.text
    assert not _audits("document.content_view")
