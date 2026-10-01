"""EXTRACTION-R4-07: a key-terms table keyed by an item-number column kept none of its terms.

Docling writes each row as "1, პირობა = <label>. 1, მნიშვნელობა = <value>", a full stop between a
term's label and its value, so every value was read without its label. Synthetic text only:
every name, code and figure is invented, and each widened reading has a near miss beside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from docx import Document

from procurement_agent.config import Settings
from procurement_agent.eval.score import canonical
from procurement_agent.extraction.pipeline import chunks_text, run_extraction
from procurement_agent.ingestion.chunker import chunk_docling_document
from procurement_agent.ingestion.docling_service import convert

_SETTINGS = Settings(ollama_base_url="http://localhost:11434")
_PREAMBLE = [
    "ხელშეკრულება N 31/2026",
    "ქ. ქუთაისი, 2026 წლის 9 თებერვალი",
    "შპს „ლურჯი ტბა“, ს/კ: 302118845 (შემდგომში „შემკვეთი“) და შპს „მთის ნაკადი“, ს/კ: "
    "402556671 (შემდგომში „შემსრულებელი“) ვაფორმებთ ხელშეკრულებას:",
    "4. ხელშეკრულების ძირითადი პირობები:",
]
_TERMS = [
    ["ხელშეკრულების ღირებულება", "24 000 ლარი (დღგ-ს ჩათვლით)"],
    ["ანგარიშსწორების პირობები", "მიღება-ჩაბარების აქტის გაფორმებიდან 7 სამუშაო დღის ვადაში"],
    ["საგარანტიო ვადა", "12 თვე"],
    ["პირგასამტეხლო", "ღირებულების 0.2% ყოველ ვადაგადაცილებულ დღეზე"],
]


def _docx(rows: list[list[str]]) -> Any:
    doc = Document()
    for line in _PREAMBLE:
        doc.add_paragraph(line)
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            table.cell(r, c).text = text
    return doc


def _extract(tmp_path: Path, rows: list[list[str]]) -> tuple[dict[str, list[str]], str]:
    path = tmp_path / "c.docx"
    _docx(rows).save(path)
    chunks = chunk_docling_document(convert(path, Settings()).document, doc_id="c")
    result = run_extraction(document_id="c", chunks=chunks, settings=_SETTINGS, llm=None)
    out: dict[str, list[str]] = {}
    for f in result.facts:
        out.setdefault(f.type.value, []).append(canonical(f))
    return out, chunks_text(chunks)


def _numbered(header: list[str], key: str = "{}") -> list[list[str]]:
    return [header] + [[key.format(i), *row] for i, row in enumerate(_TERMS, start=1)]


def test_a_numbered_key_terms_table_keeps_its_terms(tmp_path: Path) -> None:
    facts, text = _extract(tmp_path, _numbered(["N", "პირობა", "მნიშვნელობა"]))
    assert facts["total_amount"] == ["24000.0"]
    assert facts["payment_terms"] == ["P7D working"]
    assert facts["warranty_period"] == ["P12M"]
    assert facts["penalty"] == ["0.2% per day"]
    # one row, one sentence: the label beside its value, the item number kept as the row key
    assert "3, პირობა = საგარანტიო ვადა, მნიშვნელობა = 12 თვე;" in text


def test_item_numbers_written_with_a_stop_or_as_letters_key_the_rows(tmp_path: Path) -> None:
    facts, _ = _extract(tmp_path, _numbered(["№", "პირობა", "მნიშვნელობა"], key="{}."))
    assert facts["warranty_period"] == ["P12M"]
    letters = [["#", "პირობა", "მნიშვნელობა"]] + [
        [letter, *row] for letter, row in zip("აბგდ", _TERMS, strict=True)
    ]
    facts, _ = _extract(tmp_path, letters)
    assert facts["penalty"] == ["0.2% per day"]


def test_the_same_table_without_the_number_column_is_unchanged(tmp_path: Path) -> None:
    """The control the defect was measured against."""
    facts, _ = _extract(tmp_path, [["პირობა", "მნიშვნელობა"], *_TERMS])
    assert facts["total_amount"] == ["24000.0"]
    assert facts["warranty_period"] == ["P12M"]


def test_a_numbered_price_table_keeps_its_cells_as_docling_writes_them(tmp_path: Path) -> None:
    """A near miss: the line amounts of a lump-sum price table are read cell by cell (a total
    column in a row keyed by an item number is a line, never the contract's total)."""
    rows = [
        ["N", "დასახელება", "ჯამი (ლარი)"],
        ["1", "ოფისის დასუფთავება", "500 ლარი"],
        ["2", "ეზოს მოვლა", "700 ლარი"],
    ]
    facts, text = _extract(tmp_path, rows)
    assert "total_amount" not in facts
    assert "1, ჯამი (ლარი) = 500 ლარი" in text


def test_a_table_whose_first_column_is_no_item_number_keeps_its_triplets(tmp_path: Path) -> None:
    rows = [["პირობა", "მნიშვნელობა", "შენიშვნა"]] + [[*row, "-"] for row in _TERMS]
    _, text = _extract(tmp_path, rows)
    assert "12 თვე, შენიშვნა =" not in text
    assert "საგარანტიო ვადა, შენიშვნა = -" in text
