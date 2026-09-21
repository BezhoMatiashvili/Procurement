"""Real Docling conversion of the synthetic fixtures (CPU).

Needs prefetched models: ./scripts/prefetch_models.sh
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from procurement_agent.config import Settings
from procurement_agent.extraction.pipeline import run_extraction
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion import docling_service
from procurement_agent.ingestion.chunker import chunk_docling_document

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "models" / "docling"
FIXTURES = ROOT / "tests" / "fixtures"

pytestmark = [
    pytest.mark.docling,
    pytest.mark.skipif(not MODELS.exists(), reason="docling models not downloaded"),
]


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(
        ollama_base_url="http://127.0.0.1:1", docling_artifacts_path=MODELS, docling_num_threads=8
    )


def test_pdf_offer_conversion_and_rules(settings: Settings) -> None:
    result = docling_service.convert(FIXTURES / "offer_ka_en.pdf", settings)
    doc = result.document
    md = doc.export_to_markdown()
    assert "48 750,00" in md
    assert "საგარანტიო" in md  # Georgian text layer survived
    assert len(doc.tables) >= 1  # TableFormer found the price table

    chunks = chunk_docling_document(doc, doc_id="offer")
    assert chunks
    # the layout model labels "1. ფასი და ღირებულება ..." as a footnote; we must not lose it
    assert any("48 750,00 ლარს" in c.text for c in chunks)
    assert all(c.provenance.page == 1 for c in chunks)
    assert any(c.provenance.bbox is not None for c in chunks)

    res = run_extraction(document_id="offer", chunks=chunks, settings=settings, llm=None)
    by_type: dict[FactType, list[str]] = {}
    for f in res.facts:
        by_type.setdefault(f.type, []).append(str(f.normalized or f.value))
    assert "48750.0" in by_type[FactType.total_amount]
    assert "P24M" in by_type[FactType.warranty_period]
    assert "P30D" in by_type[FactType.delivery_deadline]
    assert "405123456" in by_type[FactType.party_id_code]
    assert "2026-09-12" in by_type.get(FactType.contract_date, []) + by_type.get(FactType.date, [])
    assert "2026-10-31" in by_type[FactType.validity_period]


def test_docx_contract_conversion_and_rules(settings: Settings) -> None:
    result = docling_service.convert(FIXTURES / "contract_ka.docx", settings)
    chunks = chunk_docling_document(result.document, doc_id="contract")
    res = run_extraction(document_id="contract", chunks=chunks, settings=settings, llm=None)
    values = {(f.type, str(f.normalized or f.value)) for f in res.facts}
    assert (FactType.contract_number, "PR-2026/017") in values
    assert (FactType.contract_date, "2026-03-15") in values
    assert (FactType.party_id_code, "204567890") in values
    assert (FactType.party_id_code, "405123456") in values
    assert (FactType.total_amount, "48750.0") in values


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_scanned_pdf_ocr(settings: Settings) -> None:
    result = docling_service.convert(FIXTURES / "offer_ka_en_scanned.pdf", settings)
    md = result.document.export_to_markdown()
    assert "48" in md and "750" in md
    assert "საგარანტიო" in md or "Warranty" in md


def test_save_and_reload_roundtrip(settings: Settings, tmp_path: Path) -> None:
    result = docling_service.convert(FIXTURES / "contract_ka.docx", settings)
    path = docling_service.save_document(result.document, tmp_path / "r")
    assert path.stat().st_mode & 0o777 == 0o600
    doc2 = docling_service.load_document(path)
    assert doc2.export_to_markdown() == result.document.export_to_markdown()
