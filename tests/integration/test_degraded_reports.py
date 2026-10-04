"""
Every report built from a partly read document says what was not read.

Conversions are planned (``tests.degraded_support``). Documents are invented.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from docling.datamodel.base_models import ConversionStatus
from fastapi.testclient import TestClient

from tests.degraded_support import (
    ACT_LINES,
    CONTRACT_LINES,
    SAME_ORIGIN,
    XLSX,
    H,
    Plan,
    install_fake_convert,
    make_client,
    make_legacy,
    multipage_pdf,
    skip_ids,
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


# --- findings 1 and 5: every report from a partly read document says so ------------------------


def test_the_act_report_says_which_pages_of_the_act_were_not_read(
    client: TestClient, register: object
) -> None:
    act = _partial_pdf(client, register, "act")
    contract, _ = upload(client, "contract.xlsx", xlsx_bytes(CONTRACT_LINES), XLSX)
    r = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": act, "contract_document_id": contract},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["read_quality"]["act"]["partial"] is True
    assert body["read_quality"]["contract"]["partial"] is False
    assert skip_ids(body).count("DOC.PARTIAL") == 1
    note = next(s for s in body["skipped"] if s["rule_id"] == "DOC.PARTIAL")
    assert "3 of 5 pages had no readable text" in note["reason_en"]
    assert "findings may be incomplete" in note["reason_en"]
    assert note["reason_ka"] != note["reason_en"]


def test_a_fully_read_act_report_carries_no_partial_note(client: TestClient) -> None:
    act, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    contract, _ = upload(client, "contract.xlsx", xlsx_bytes(CONTRACT_LINES), XLSX)
    body = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": act, "contract_document_id": contract},
    ).json()
    assert "DOC.PARTIAL" not in skip_ids(body)
    assert body["read_quality"]["act"]["partial"] is False


def test_the_act_page_puts_a_warning_at_the_top_when_the_contract_was_only_partly_read(
    client: TestClient, register: object
) -> None:
    contract = _partial_pdf(client, register, "contract")
    act, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    _english(client)
    html = client.get(f"/ui/checks/aoa?act={act}&contract={contract}").text
    assert 'id="read-quality"' in html
    assert "3 of 5 pages had no readable text" in html
    assert html.index('id="read-quality"') < html.index('class="stats"')


def test_a_fully_read_act_page_has_no_read_quality_banner(client: TestClient) -> None:
    act, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    assert 'id="read-quality"' not in client.get(f"/ui/checks/aoa?act={act}").text


def test_two_partly_read_identical_documents_do_not_get_the_all_clear_verdict(
    client: TestClient, register: object
) -> None:
    a = _partial_pdf(client, register, "one")
    # the same planned text, a different file
    data = register(  # type: ignore[operator]
        multipage_pdf(5, "two"),
        Plan(
            pages=5,
            text_on={1: "alpha text", 2: "beta text"},
            status=ConversionStatus.PARTIAL_SUCCESS,
            errors=3,
        ),
    )
    b, _ = upload(client, "two.pdf", data)
    r = client.post(
        "/api/v1/checks/compare", headers=H, json={"left_document_id": a, "right_document_id": b}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["read_quality"]["left"]["partial"] and body["read_quality"]["right"]["partial"]
    assert skip_ids(body).count("DOC.PARTIAL") == 2
    _english(client)
    html = client.get(f"/ui/checks/compare?left={a}&right={b}").text
    assert "No differences. The two documents say exactly the same thing." not in html
    assert "cannot confirm" in html
    assert 'id="read-quality"' in html


def test_two_fully_read_identical_documents_still_get_the_verdict(client: TestClient) -> None:
    a, _ = upload(client, "a.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    b, _ = upload(client, "b.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    _english(client)
    html = client.get(f"/ui/checks/compare?left={a}&right={b}").text
    assert "No differences. The two documents say exactly the same thing." in html
    assert 'id="read-quality"' not in html


def test_two_documents_with_no_readable_text_are_not_reported_identical(
    client: TestClient,
) -> None:
    """The reviewer's critical case: two legacy documents that hold no text. The API and the
    page both refuse; neither says 'no difference'."""
    a, _ = upload(client, "a.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    b, _ = upload(client, "b.xlsx", xlsx_bytes(CONTRACT_LINES), XLSX)
    for doc_id in (a, b):
        make_legacy(client, doc_id, "xlsx")
    r = client.post(
        "/api/v1/checks/compare", headers=H, json={"left_document_id": a, "right_document_id": b}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NO_TEXT"
    _english(client)
    page = client.get(f"/ui/checks/compare?left={a}&right={b}")
    assert page.status_code == 409
    assert "No differences" not in page.text
    assert "NO_TEXT" in page.text


def test_extraction_from_a_partly_read_document_says_so_in_the_result(
    client: TestClient, register: object
) -> None:
    doc_id = _partial_pdf(client, register, "ext")
    job = client.post(f"/api/v1/documents/{doc_id}/extract", headers=H, json={}).json()
    assert wait_job(client, job["id"])["status"] == "done"
    result = client.get(f"/api/v1/jobs/{job['id']}/result", headers=H).json()
    assert result["read_quality"]["document"]["partial"] is True
    assert result["warnings"][0].startswith("DOC.PARTIAL")
    assert "3 of 5 pages had no readable text" in result["warnings"][0]
    _english(client)
    page = client.get(f"/ui/jobs/{job['id']}").text
    assert 'id="read-quality"' in page


def test_the_api_report_of_a_fully_read_document_has_no_extraction_warning_added(
    client: TestClient,
) -> None:
    doc_id, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    job = client.post(f"/api/v1/documents/{doc_id}/extract", headers=H, json={}).json()
    wait_job(client, job["id"])
    result = client.get(f"/api/v1/jobs/{job['id']}/result", headers=H).json()
    assert not any(w.startswith("DOC.PARTIAL") for w in result["warnings"])
    assert result["read_quality"]["document"]["partial"] is False
