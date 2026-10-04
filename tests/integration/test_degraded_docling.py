"""The real parser on the inputs that used to give a clean-looking result (CPU, local only).

* an 80-page PDF converted with a ten-second budget: Docling returns ``partial_success`` with the
  first few pages; it used to be stored as a complete ``ready`` document;
* a PDF of page images on a host without Tesseract: no text, and it used to be ``ready``, empty;
* a PDF whose second page is a picture of the text that matters.

Needs the Docling models (``./scripts/prefetch_models.sh``). The PDFs are generated here from
invented text.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.degraded_support import H, document, make_client, skip_ids, upload

reportlab_canvas = pytest.importorskip("reportlab.pdfgen.canvas")
reportlab_pagesizes = pytest.importorskip("reportlab.lib.pagesizes")
reportlab_utils = pytest.importorskip("reportlab.lib.utils")
Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "models" / "docling"

pytestmark = [
    pytest.mark.docling,
    pytest.mark.skipif(not MODELS.exists(), reason="docling models not downloaded"),
]


def _picture_of_text(text: str) -> object:
    img = Image.new("RGB", (1200, 300), "white")
    ImageDraw.Draw(img).text((40, 120), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return reportlab_utils.ImageReader(buf)


def _pdf(path: Path, pages: list[str | None]) -> bytes:
    """A PDF with, for each entry, a page of text lines (str) or a page that is a picture of
    words (None)."""
    c = reportlab_canvas.Canvas(str(path), pagesize=reportlab_pagesizes.A4)
    for n, body in enumerate(pages):
        if body is None:
            c.drawImage(_picture_of_text(f"Total amount {999999 + n} payable at once"), 40, 500)
        else:
            y = 800
            for i in range(55):
                c.drawString(
                    40, y, f"Page {n + 1} line {i + 1}: {body} item {n}-{i} in {i + 5} days"
                )
                y -= 14
        c.showPage()
    c.save()
    return path.read_bytes()


@pytest.fixture
def short_budget(tmp_path: Path) -> Iterator[TestClient]:
    yield from make_client(tmp_path, docling_artifacts_path=MODELS, docling_timeout_s=10)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from make_client(tmp_path, docling_artifacts_path=MODELS)


def test_eighty_pages_in_ten_seconds_is_a_partial_document_not_a_complete_one(
    short_budget: TestClient, tmp_path: Path
) -> None:
    data = _pdf(tmp_path / "long.pdf", ["the contractor shall deliver"] * 80)
    doc_id, _ = upload(short_budget, "long.pdf", data)
    doc = document(short_budget, doc_id)
    quality = doc["read_quality"]
    if doc["status"] == "ready" and not quality["partial"]:
        pytest.skip("this machine converted 80 pages inside the budget; nothing was cut")
    assert doc["status"] == "ready"
    assert quality["partial"] is True
    assert quality["pages_total"] == 80
    assert quality["pages_with_text"] < 80
    assert doc["page_count"] == 80

    act = short_budget.post("/api/v1/checks/aoa", headers=H, json={"aoa_document_id": doc_id})
    assert act.status_code == 200
    assert "DOC.PARTIAL" in skip_ids(act.json())
    note = next(s for s in act.json()["skipped"] if s["rule_id"] == "DOC.PARTIAL")
    assert "pages had no readable text" in note["reason_en"]


@pytest.mark.skipif(shutil.which("tesseract") is not None, reason="this host has Tesseract")
def test_a_pdf_of_page_images_without_tesseract_fails_with_no_text(
    client: TestClient, tmp_path: Path
) -> None:
    data = _pdf(tmp_path / "scan.pdf", [None, None])
    doc_id, job_id = upload(client, "scan.pdf", data)
    doc = document(client, doc_id)
    assert doc["status"] == "failed"
    assert doc["error_code"] == "NO_TEXT"
    assert doc["error_hint"] == "OCR_UNAVAILABLE"
    refused = client.post("/api/v1/checks/aoa", headers=H, json={"aoa_document_id": doc_id})
    assert refused.status_code == 409
    assert job_id


@pytest.mark.skipif(shutil.which("tesseract") is not None, reason="this host has Tesseract")
def test_a_picture_page_among_text_pages_makes_the_document_partial(
    client: TestClient, tmp_path: Path
) -> None:
    """The reviewer's case: page 2 is a picture of 'Total amount 999999'. The conversion
    succeeded; the document used to be reported as read."""
    data = _pdf(tmp_path / "mixed.pdf", ["the acceptance act states", None])
    doc_id, _ = upload(client, "mixed.pdf", data)
    doc = document(client, doc_id)
    assert doc["status"] == "ready"
    quality = doc["read_quality"]
    assert (quality["pages_total"], quality["pages_with_text"]) == (2, 1)
    assert quality["partial"] is True
    assert quality["ocr"] is False

    other, _ = upload(client, "act.xlsx", _xlsx(), "application/vnd.ms-excel")
    report = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": other, "contract_document_id": doc_id},
    ).json()
    assert "DOC.PARTIAL" in skip_ids(report)
    assert report["read_quality"]["contract"]["pages_with_text"] == 1


def _xlsx() -> bytes:
    from tests.degraded_support import ACT_LINES, xlsx_bytes

    return xlsx_bytes(ACT_LINES)
