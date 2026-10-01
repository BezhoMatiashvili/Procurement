"""Defects the third red-team round found in how a document's text is assembled.

Each test names its defect (``samples/redteam/<module>/CONFIRMED_R3.md``). Synthetic text only:
every name, code and figure is invented, and each widened reading has a near miss beside it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.config import Settings
from procurement_agent.extraction.pipeline import chunks_text
from procurement_agent.ingestion.chunker import chunk_docling_document
from procurement_agent.ingestion.docling_service import convert
from tests.unit.test_aoa_check import ACT, CONTRACT

#: A run of one paragraph: its text and how it differs from the runs around it.
Style = Callable[[Run], None]


def _plain(run: Run) -> None:
    del run


def _bold(run: Run) -> None:
    run.bold = True


def _italic(run: Run) -> None:
    run.italic = True


def _underline(run: Run) -> None:
    run.underline = True


def _read(path: Path) -> str:
    """The document's text as the checks read it: Docling, the chunker, ``chunks_text``."""
    return chunks_text(chunk_docling_document(convert(path, Settings()).document, doc_id="t"))


def _runs(paragraph: Paragraph, runs: list[tuple[str, Style]]) -> None:
    for text, style in runs:
        style(paragraph.add_run(text))


# --- AOA-R3-13: formatting inside a word or a number is no break in its text ----------------------


@pytest.mark.parametrize("style", [_bold, _italic, _underline])
def test_a_formatted_run_inside_a_figure_leaves_the_figure_whole(
    tmp_path: Path, style: Style
) -> None:
    doc = Document()
    _runs(doc.add_paragraph(), [("ღირებულება შეადგენს 7 2", _plain), ("80", style)])
    _runs(doc.add_paragraph(), [(" (შვიდი ათას ორას ოთხმოცი) ლარს.", _plain)])
    _runs(doc.add_paragraph(), [("დირექტორი: ლაშა გოგოლ", _plain), ("აძე", style)])
    doc.save(tmp_path / "a.docx")
    text = _read(tmp_path / "a.docx")
    assert "ღირებულება შეადგენს 7 280" in text
    assert "დირექტორი: ლაშა გოგოლაძე" in text


def test_the_space_at_a_run_boundary_is_kept(tmp_path: Path) -> None:
    """Docling strips each formatted piece: the space a run began or ended with was lost."""
    doc = Document()
    _runs(doc.add_paragraph(), [("თანხა ", _plain), ("500", _bold), (" (ხუთასი) ლარი", _plain)])
    doc.save(tmp_path / "a.docx")
    assert "თანხა 500 (ხუთასი) ლარი" in _read(tmp_path / "a.docx")


def test_paragraphs_stay_apart(tmp_path: Path) -> None:
    """The near miss: two paragraphs are two lines, however each is formatted."""
    doc = Document()
    _runs(doc.add_paragraph(), [("პირველი ", _plain), ("აბზაცი", _bold)])
    _runs(doc.add_paragraph(), [("მეორე", _italic), (" აბზაცი", _plain)])
    doc.save(tmp_path / "a.docx")
    lines = _read(tmp_path / "a.docx").split("\n")
    assert "პირველი აბზაცი" in lines
    assert "მეორე აბზაცი" in lines


def test_a_formatted_run_inside_a_table_cell_leaves_the_cell_whole(tmp_path: Path) -> None:
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "დასახელება"
    table.cell(0, 1).text = "თანხა"
    table.cell(1, 0).text = "მომსახურება"
    _runs(table.cell(1, 1).paragraphs[0], [("1 2", _plain), ("40", _italic), (" ლარი", _plain)])
    doc.save(tmp_path / "a.docx")
    text = _read(tmp_path / "a.docx")
    assert "1 240 ლარი" in text
    assert "1 2\n" not in text


def test_a_formatted_list_item_is_one_line(tmp_path: Path) -> None:
    doc = Document()
    item = doc.add_paragraph(style="List Bullet")
    _runs(item, [("საპროექტო ", _plain), ("დოკუმენტაციის", _bold), (" მომზადება", _plain)])
    doc.save(tmp_path / "a.docx")
    assert "საპროექტო დოკუმენტაციის მომზადება" in _read(tmp_path / "a.docx")


def test_a_hyperlink_inside_a_sentence_stays_in_it(tmp_path: Path) -> None:
    doc = Document()
    paragraph = doc.add_paragraph("ინფორმაცია იხილეთ ")
    rel = paragraph.part.relate_to(
        "https://example.invalid/", RELATIONSHIP_TYPE.HYPERLINK, is_external=True
    )
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel)
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "ვებგვერდზე"
    run.append(text)
    link.append(run)
    paragraph._p.append(link)
    paragraph.add_run(" ყოველდღიურად.")
    doc.save(tmp_path / "a.docx")
    assert "ინფორმაცია იხილეთ ვებგვერდზე ყოველდღიურად." in _read(tmp_path / "a.docx")


def test_a_correct_act_with_formatting_inside_its_fee_and_signatory_is_clean(
    tmp_path: Path,
) -> None:
    doc = Document()
    for line in ACT.strip().split("\n"):
        paragraph = doc.add_paragraph()
        if line.startswith("საფასური შეადგენს 100"):
            _runs(paragraph, [("საფასური შეადგენს 1", _plain), ("00", _italic)])
            _runs(paragraph, [(line.removeprefix("საფასური შეადგენს 100"), _plain)])
        elif line == "დირექტორი: დავით მელაძე":
            _runs(paragraph, [("დირექტორი: დავით მელ", _plain), ("აძე", _underline)])
        else:
            paragraph.add_run(line)
    doc.save(tmp_path / "act.docx")
    report = check_aoa(aoa_text=_read(tmp_path / "act.docx"), contract_text=CONTRACT)
    assert [f.rule_id for f in report.findings if f.severity in ("error", "warning")] == []


# --- EXTRACTION-R3-02 / -03: a two-column table whose first row is data ---------------------------

_SETTINGS = Settings(ollama_base_url="http://localhost:11434")
_PREAMBLE = [
    "ხელშეკრულება N 14/2026",
    "ქ. ქუთაისი, 2026 წლის 9 თებერვალი",
    "შპს „ლურჯი ტბა“, ს/კ: 302118845 (შემდგომში „შემკვეთი“) და შპს „მთის ნაკადი“, ს/კ: "
    "402556671 (შემდგომში „შემსრულებელი“) ვაფორმებთ ხელშეკრულებას:",
]
_ADVANCE = ["ავანსი", "ღირებულების 30% ხელშეკრულების გაფორმებიდან 5 სამუშაო დღეში"]
_BALANCE = ["საბოლოო ანგარიშსწორება", "დარჩენილი 70% აქტის გაფორმებიდან 7 სამუშაო დღეში"]
_PENALTY = ["პირგასამტეხლო", "0.2% ყოველ ვადაგადაცილებულ დღეზე"]
_PRICE = ["ხელშეკრულების ღირებულება", "24 000 ლარი დღგ-ს ჩათვლით"]


def _table_docx(rows: list[list[str]], *, before: list[str], merge_first: bool = False) -> Any:
    doc = Document()
    for line in before:
        doc.add_paragraph(line)
    table = doc.add_table(rows=len(rows), cols=2)
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            table.cell(r, c).text = text
    if merge_first:
        table.cell(0, 0).merge(table.cell(0, 1)).text = rows[0][0]
    return doc


def _extract_docx(tmp_path: Path, doc: Any) -> tuple[dict[str, list[str]], str]:
    from procurement_agent.eval.score import canonical
    from procurement_agent.extraction.pipeline import run_extraction

    path = tmp_path / "c.docx"
    doc.save(path)
    chunks = chunk_docling_document(convert(path, Settings()).document, doc_id="c")
    result = run_extraction(document_id="c", chunks=chunks, settings=_SETTINGS, llm=None)
    out: dict[str, list[str]] = {}
    for f in result.facts:
        out.setdefault(f.type.value, []).append(canonical(f))
    return out, chunks_text(chunks)


@pytest.mark.parametrize(
    "rows",
    [
        [_ADVANCE, _BALANCE, _PENALTY],
        [_PRICE, _ADVANCE, _BALANCE, _PENALTY],
        [["პირობა", "მნიშვნელობა"], _PRICE, _ADVANCE, _BALANCE, _PENALTY],  # the control
    ],
    ids=["advance-first", "price-first", "header-row"],
)
def test_a_headerless_key_terms_table_reads_each_row_on_its_own(
    tmp_path: Path, rows: list[list[str]]
) -> None:
    """EXTRACTION-R3-02: the first row's value was written into every other row, so each row
    was read beside it - a false penalty "30%" and the advance's deadline for the balance."""
    facts, _ = _extract_docx(tmp_path, _table_docx(rows, before=_PREAMBLE))
    assert facts["payment_terms"] == ["P7D working advance 30%"]
    assert facts["penalty"] == ["0.2% per day"]
    if _PRICE in rows:
        assert facts["total_amount"] == ["24000.0"]


def test_each_cell_of_a_headerless_table_is_written_as_often_as_the_table_has_it(
    tmp_path: Path,
) -> None:
    rows = [_ADVANCE, _BALANCE, _PENALTY]
    _, text = _extract_docx(tmp_path, _table_docx(rows, before=_PREAMBLE))
    for row in rows:
        assert text.count(row[1]) == 1
        assert f"{row[0]}: {row[1]}" in text


@pytest.mark.parametrize("second", ["", "სახელმწიფო შესყიდვა"])
def test_a_title_in_a_layout_table_is_read(tmp_path: Path, second: str) -> None:
    """EXTRACTION-R3-03: a title in the first row, its second cell empty or without a digit."""
    rows = [["ხელშეკრულება N 14/2026", second], ["ქ. ქუთაისი", "2026 წლის 9 თებერვალი"]]
    doc = _table_docx(rows, before=[])
    for line in _PREAMBLE[2:]:
        doc.add_paragraph(line)
    facts, text = _extract_docx(tmp_path, doc)
    assert text.count("ხელშეკრულება N 14/2026") == 1
    assert facts["contract_number"] == ["14/2026"]
    assert facts["contract_date"] == ["2026-02-09"]


def test_a_title_merged_across_the_first_row_is_written_once(tmp_path: Path) -> None:
    rows = [["ხელშეკრულება N 14/2026", ""], ["ქ. ქუთაისი", "2026 წლის 9 თებერვალი"]]
    doc = _table_docx(rows, before=[], merge_first=True)
    for line in _PREAMBLE[2:]:
        doc.add_paragraph(line)
    facts, text = _extract_docx(tmp_path, doc)
    assert text.count("ხელშეკრულება N 14/2026") == 1
    assert facts["contract_number"] == ["14/2026"]


@pytest.mark.parametrize(
    "header", [["პირობა", "მნიშვნელობა"], ["", "თანხა"]], ids=["named", "blank-corner"]
)
def test_a_real_header_row_is_still_a_header(tmp_path: Path, header: list[str]) -> None:
    """The near miss: a header names its columns and carries no figure; Docling's triplets,
    which write each row under its column's name, stay as they were."""
    rows = [header, _PRICE, _PENALTY]
    _, text = _extract_docx(tmp_path, _table_docx(rows, before=_PREAMBLE))
    assert f"{_PRICE[0]}, {header[1]} = {_PRICE[1]}" in text
    assert f"{header[0]}:" not in text or not header[0]
