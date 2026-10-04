"""What was read of a document, as numbers and booleans: the ground every "partly read" notice
stands on. Documents here are invented; nothing in the sidecar may ever hold their text."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from docling_core.types.doc.base import BoundingBox, Size
from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.document import (
    DoclingDocument,
    ProvenanceItem,
    TableCell,
    TableData,
)
from docling_core.types.doc.labels import DocItemLabel

from procurement_agent.config import Settings
from procurement_agent.extraction.schemas import Provenance
from procurement_agent.ingestion import read_quality as rq
from procurement_agent.ingestion.chunker import Chunk
from procurement_agent.ingestion.docling_service import save_document

BOX = BoundingBox(l=0, t=0, r=10, b=10)


def _doc(
    pages: int, text_on: dict[int, str], *, footer_on: tuple[int, ...] = ()
) -> DoclingDocument:
    doc = DoclingDocument(name="synthetic")
    for n in range(1, pages + 1):
        doc.add_page(page_no=n, size=Size(width=200, height=200))
    for n, text in text_on.items():
        doc.add_text(
            label=DocItemLabel.TEXT,
            text=text,
            prov=ProvenanceItem(page_no=n, bbox=BOX, charspan=(0, len(text))),
        )
    for n in footer_on:
        doc.add_text(
            label=DocItemLabel.PAGE_FOOTER,
            text="page footer",
            prov=ProvenanceItem(page_no=n, bbox=BOX, charspan=(0, 11)),
            content_layer=ContentLayer.FURNITURE,
        )
    return doc


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        env="dev",
        data_dir=tmp_path / "data",
        dev_api_key="pa_devkey_for_tests_only",
    )


def test_every_page_with_text_is_a_fully_read_document() -> None:
    q = rq.assess_docling(_doc(3, {1: "alpha", 2: "beta", 3: "gamma"}), "pdf")
    assert (q.pages_total, q.pages_with_text) == (3, 3)
    assert q.partial is False
    assert q.has_text
    assert q.pages_without_text == 0


def test_a_page_with_no_text_makes_the_document_partial() -> None:
    q = rq.assess_docling(_doc(2, {1: "page one text"}), "pdf")
    assert (q.pages_total, q.pages_with_text, q.partial) == (2, 1, True)
    assert q.pages_without_text == 1


def test_a_page_with_only_a_footer_has_no_readable_text() -> None:
    """A scan whose page carries a printed footer is still a scan."""
    q = rq.assess_docling(_doc(2, {1: "page one text"}, footer_on=(2,)), "pdf")
    assert q.pages_with_text == 1
    assert q.partial is True


def test_punctuation_alone_is_not_readable_text() -> None:
    q = rq.assess_docling(_doc(1, {1: " - . - "}), "pdf")
    assert q.pages_with_text == 0
    assert q.has_text is False


def test_text_in_a_table_counts_for_the_tables_page() -> None:
    doc = _doc(2, {1: "intro"})
    cell = TableCell(
        text="Total 100",
        start_row_offset_idx=0,
        end_row_offset_idx=1,
        start_col_offset_idx=0,
        end_col_offset_idx=1,
    )
    doc.add_table(
        data=TableData(num_rows=1, num_cols=1, table_cells=[cell]),
        prov=ProvenanceItem(page_no=2, bbox=BOX, charspan=(0, 0)),
    )
    q = rq.assess_docling(doc, "pdf")
    assert (q.pages_with_text, q.partial) == (2, False)


def test_the_page_count_of_the_file_wins_over_a_shortened_page_table() -> None:
    """A timeout can cut Docling's own page table; the file still has all its pages."""
    q = rq.assess_docling(_doc(4, {1: "a", 2: "b", 3: "c", 4: "d"}), "pdf", pdf_pages=80)
    assert q.pages_total == 80
    assert q.pages_with_text == 4
    assert q.partial is True


def test_a_conversion_that_only_partly_succeeded_is_partial_even_with_all_pages_present() -> None:
    q = rq.assess_docling(_doc(1, {1: "text"}), "pdf", conversion_partial=True, errors=3)
    assert q.partial is True
    assert q.errors == 3


def test_a_word_document_has_no_pages_to_count() -> None:
    doc = DoclingDocument(name="synthetic")
    doc.add_text(label=DocItemLabel.TEXT, text="a paragraph")
    q = rq.assess_docling(doc, "docx")
    assert (q.pages_total, q.pages_with_text) == (None, None)
    assert q.partial is False
    assert q.text_chars == 10  # letters and digits, not spaces


def test_a_word_document_that_reported_errors_is_partial() -> None:
    doc = DoclingDocument(name="synthetic")
    doc.add_text(label=DocItemLabel.TEXT, text="a paragraph")
    assert rq.assess_docling(doc, "docx", errors=1).partial is True


def test_a_document_without_any_text_has_none() -> None:
    q = rq.assess_docling(DoclingDocument(name="synthetic"), "docx")
    assert q.text_chars == 0
    assert q.has_text is False


def _chunk(text: str) -> Chunk:
    return Chunk(id="c1", text=text, body=text, provenance=Provenance(chunk_id="c1"))


def test_a_workbook_with_chunks_has_text_and_a_workbook_without_has_none() -> None:
    assert rq.assess_chunks([_chunk("Item | 10")], "xlsx").has_text
    none = rq.assess_chunks([], "xlsx")
    assert none.has_text is False
    assert none.pages_total is None
    assert rq.assess_chunks([_chunk("  \n ")], "xlsx").has_text is False


def test_the_sidecar_holds_numbers_and_booleans_only(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    q = rq.assess_docling(_doc(2, {1: "SECRET-CONTRACT-WORDS"}), "pdf", errors=1, ocr=False)
    path = rq.write_quality(s, "a" * 32, q)
    raw = path.read_text()
    assert "SECRET" not in raw
    data = json.loads(raw)
    assert all(isinstance(v, int | bool | str) or v is None for v in data.values())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ["read_quality.json"]


def test_the_sidecar_reads_back_as_written(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    q = rq.assess_docling(_doc(2, {1: "x1"}), "pdf", errors=2, ocr=True)
    rq.write_quality(s, "b" * 32, q)
    assert rq.read_sidecar(s, "b" * 32) == q


@pytest.mark.parametrize("junk", ["", "not json", "[]", '{"kind": 5}', '{"kind": "pdf"}'])
def test_a_damaged_sidecar_is_treated_as_missing(tmp_path: Path, junk: str) -> None:
    s = _settings(tmp_path)
    path = rq.sidecar_path(s, "c" * 32)
    path.parent.mkdir(parents=True)
    path.write_text(junk)
    assert rq.read_sidecar(s, "c" * 32) is None


class _Doc:
    def __init__(self, doc_id: str, kind: str) -> None:
        self.id, self.kind = doc_id, kind


def test_an_older_document_without_a_sidecar_is_assessed_from_its_stored_parse(
    tmp_path: Path,
) -> None:
    s = _settings(tmp_path)
    save_document(_doc(3, {1: "one", 2: "two"}), s.results_dir / ("d" * 32))
    q = rq.read_quality(s, _Doc("d" * 32, "docx"))
    assert q.text_chars == len("onetwo")  # letters and digits
    assert (q.pages_total, q.pages_with_text, q.partial) == (3, 2, True)


def test_the_sidecar_is_preferred_when_there_is(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    q = rq.ReadQuality(
        kind="pdf", pages_total=9, pages_with_text=2, text_chars=5, partial=True, errors=0
    )
    rq.write_quality(s, "e" * 32, q)
    assert rq.read_quality(s, _Doc("e" * 32, "pdf")) == q


def test_a_missing_stored_parse_is_reported_as_one(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(rq.StoredParseError):
        rq.read_quality(s, _Doc("f" * 32, "pdf"))


def test_a_garbled_stored_parse_is_reported_as_one(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    out = s.results_dir / ("0" * 32)
    out.mkdir(parents=True)
    (out / "docling.json").write_text("{not json")
    with pytest.raises(rq.StoredParseError):
        rq.read_quality(s, _Doc("0" * 32, "docx"))


def test_error_codes_split_into_a_code_and_a_hint() -> None:
    assert rq.split_error("NO_TEXT") == ("NO_TEXT", None)
    assert rq.split_error("NO_TEXT:OCR_UNAVAILABLE") == ("NO_TEXT", "OCR_UNAVAILABLE")
    assert rq.split_error("could not be read: ConversionError") == (None, None)
    assert rq.split_error(None) == (None, None)


def test_no_text_error_carries_its_code_and_hint() -> None:
    e = rq.NoTextError(hint=rq.OCR_UNAVAILABLE)
    assert str(e) == "NO_TEXT:OCR_UNAVAILABLE"
    assert rq.NoTextError().code == "NO_TEXT"
    assert str(rq.NoTextError()) == "NO_TEXT"


def test_settling_a_parse_with_no_text_refuses_it_and_still_records_what_it_saw(
    tmp_path: Path,
) -> None:
    s = _settings(tmp_path)
    with pytest.raises(rq.NoTextError) as raised:
        rq.settle_parse(s, "1" * 32, "pdf", _doc(2, {}), upload=None, ocr=False)
    assert raised.value.hint == rq.OCR_UNAVAILABLE
    saved = rq.read_sidecar(s, "1" * 32)
    assert saved is not None
    assert saved.has_text is False


def test_a_blank_scan_is_image_only_when_ocr_was_on(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(rq.NoTextError) as raised:
        rq.settle_parse(s, "2" * 32, "pdf", _doc(2, {}), upload=None, ocr=True)
    assert raised.value.hint == rq.IMAGE_ONLY


def test_settling_a_word_document_with_no_text_has_no_hint(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(rq.NoTextError) as raised:
        rq.settle_parse(s, "3" * 32, "docx", DoclingDocument(name="x"), upload=None, ocr=True)
    assert raised.value.hint is None


def test_settling_a_readable_parse_returns_and_stores_its_quality(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    q = rq.settle_parse(s, "4" * 32, "pdf", _doc(2, {1: "a", 2: "b"}), upload=None, ocr=True)
    assert q.partial is False
    assert rq.read_sidecar(s, "4" * 32) == q


def test_settling_a_workbook_with_no_cell_text_is_refused(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(rq.NoTextError):
        rq.settle_workbook(s, "5" * 32, [])
    q = rq.settle_workbook(s, "6" * 32, [_chunk("Item | 10")])
    assert q.kind == "xlsx"
    assert rq.read_sidecar(s, "6" * 32) == q


def test_the_partial_note_says_how_many_pages_in_both_languages() -> None:
    q = rq.ReadQuality(
        kind="pdf", pages_total=5, pages_with_text=2, text_chars=40, partial=True, errors=0
    )
    note = rq.partial_note(q, "act")
    assert note is not None
    assert note.rule_id == "DOC.PARTIAL"
    assert "3 of 5 pages had no readable text" in note.reason_en
    assert "findings may be incomplete" in note.reason_en
    assert "3" in note.reason_ka
    assert "5" in note.reason_ka
    assert note.reason_ka != note.reason_en


def test_the_partial_note_names_the_side_it_is_about() -> None:
    q = rq.ReadQuality(
        kind="pdf", pages_total=5, pages_with_text=2, text_chars=40, partial=True, errors=0
    )
    left = rq.partial_note(q, "left")
    right = rq.partial_note(q, "right")
    assert left is not None
    assert right is not None
    assert left.reason_en != right.reason_en


def test_a_fully_read_document_gets_no_note() -> None:
    q = rq.ReadQuality(
        kind="pdf", pages_total=5, pages_with_text=5, text_chars=40, partial=False, errors=0
    )
    assert rq.partial_note(q, "act") is None


def test_a_partial_document_without_pages_says_the_reader_reported_problems() -> None:
    q = rq.ReadQuality(
        kind="docx", pages_total=None, pages_with_text=None, text_chars=40, partial=True, errors=2
    )
    note = rq.partial_note(q, "document")
    assert note is not None
    assert "pages" not in note.reason_en
    assert "findings may be incomplete" in note.reason_en
