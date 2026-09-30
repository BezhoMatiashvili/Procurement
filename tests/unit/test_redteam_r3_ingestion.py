"""Defects the third red-team round found in how a document's text is assembled.

Each test names its defect (``samples/redteam/<module>/CONFIRMED_R3.md``). Synthetic text only:
every name, code and figure is invented, and each widened reading has a near miss beside it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

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
