"""
A stored parse that is missing or damaged is a 409 with a code, not a bare 500.

Conversions are planned (``tests.degraded_support``). Documents are invented.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from docling.datamodel.base_models import ConversionStatus
from fastapi.testclient import TestClient

from tests.degraded_support import (
    CONTRACT_LINES,
    SAME_ORIGIN,
    XLSX,
    H,
    Plan,
    damage_parse,
    document,
    install_fake_convert,
    make_client,
    multipage_pdf,
    settings_of,
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


# --- finding 10: the stored parse is missing or damaged ------------------------------------------


@pytest.mark.parametrize("delete", [True, False], ids=["missing", "garbled"])
def test_a_missing_or_damaged_stored_parse_is_409_unreadable_and_the_document_fails(
    client: TestClient, register: object, delete: bool
) -> None:
    data = register(  # type: ignore[operator]
        multipage_pdf(1, f"d{delete}"), Plan(pages=1, text_on={1: "some words here"})
    )
    doc_id, _ = upload(client, "d.pdf", data)
    other, _ = upload(client, "o.xlsx", xlsx_bytes(CONTRACT_LINES), XLSX)
    damage_parse(client, doc_id, delete=delete)
    (settings_of(client).results_dir / doc_id / "read_quality.json").unlink(missing_ok=True)

    r = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": doc_id, "contract_document_id": other},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "DOCUMENT_UNREADABLE"
    assert "upload it again" in detail["message_en"]
    assert document(client, doc_id)["status"] == "failed"
    assert document(client, doc_id)["error_code"] == "DOCUMENT_UNREADABLE"


def test_the_page_of_a_report_on_a_damaged_parse_is_a_409_page_with_the_hint(
    client: TestClient, register: object
) -> None:
    data = register(multipage_pdf(1, "dd"), Plan(pages=1, text_on={1: "some words here"}))  # type: ignore[operator]
    doc_id, _ = upload(client, "d.pdf", data)
    damage_parse(client, doc_id, delete=True)
    (settings_of(client).results_dir / doc_id / "read_quality.json").unlink(missing_ok=True)
    _english(client)
    page = client.get(f"/ui/checks/aoa?act={doc_id}")
    assert page.status_code == 409
    assert "upload it again" in page.text
    assert "Internal Server Error" not in page.text


def test_an_extraction_of_a_damaged_parse_fails_the_job_and_the_document(
    client: TestClient, register: object
) -> None:
    data = register(multipage_pdf(1, "de"), Plan(pages=1, text_on={1: "some words here"}))  # type: ignore[operator]
    doc_id, _ = upload(client, "d.pdf", data)
    damage_parse(client, doc_id, delete=False)
    job = client.post(f"/api/v1/documents/{doc_id}/extract", headers=H, json={}).json()
    assert wait_job(client, job["id"])["status"] == "failed"
    assert document(client, doc_id)["status"] == "failed"


def test_a_template_workbook_that_is_no_workbook_any_more_is_409_not_a_500(
    client: TestClient,
) -> None:
    from tests.boq_factory import LINES, blank, build_boq

    template, _ = upload(
        client, "t.xlsx", build_boq(blank(), priced=False, stated_total=None), XLSX
    )
    bid, _ = upload(client, "b.xlsx", build_boq(LINES, stated_total=1000.0), XLSX)
    (settings_of(client).upload_dir / f"{template}.xlsx").write_bytes(b"not a workbook")
    r = client.post(
        "/api/v1/checks/boq",
        headers=H,
        json={"template_document_id": template, "bid_document_ids": [bid]},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DOCUMENT_UNREADABLE"
    assert document(client, template)["status"] == "failed"
