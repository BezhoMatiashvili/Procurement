"""Writing findings back into a Word document as comments. Fixtures are synthetic."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest
from docx import Document

from procurement_agent.checks.findings import Finding
from procurement_agent.report.docx_annotate import (
    UnsupportedDocumentError,
    annotate_docx,
    comment_text,
)


def _finding(rule_id: str = "AOA.AMOUNT_WORDS", **kw: object) -> Finding:
    base = {
        "rule_id": rule_id,
        "severity": "error",
        "message_ka": "ციფრი და სიტყვიერი ჩანაწერი არ ემთხვევა",
        "message_en": "digits and words disagree",
    }
    return Finding(**{**base, **kw})  # type: ignore[arg-type]


@pytest.fixture
def act(tmp_path: Path) -> Path:
    path = tmp_path / "act.docx"
    doc = Document()
    para = doc.add_paragraph()
    para.add_run("საფასური შეადგენს ")
    para.add_run("100").bold = True  # Word splits a paragraph at every formatting change
    para.add_run(" (ორასი) ლარს.")
    doc.add_paragraph("დირექტორი: დავთ მელაძე")
    doc.save(path)
    return path


def _comments_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        if "word/comments.xml" not in z.namelist():
            return ""
        return z.read("word/comments.xml").decode()


def test_a_comment_is_written_for_each_finding(act: Path, tmp_path: Path) -> None:
    out, unanchored = annotate_docx(
        act,
        [
            _finding(actual="ორასი (200)", expected="100"),
            _finding("AOA.DIRECTOR", actual="დავთ მელაძე", expected="დავით მელაძე"),
        ],
        tmp_path / "out.docx",
    )
    xml = _comments_xml(out)
    assert unanchored == []
    assert len(re.findall(r'w:author="Procurement Agent"', xml)) == 2
    assert "AOA.DIRECTOR" in xml


def test_the_comment_carries_the_correct_value(act: Path, tmp_path: Path) -> None:
    """The client asked for the correct variant to be written in the comment."""
    out, _ = annotate_docx(
        act, [_finding(actual="ორასი (200)", expected="100")], tmp_path / "o.docx"
    )
    assert "should be: 100" in _comments_xml(out)


def test_the_comment_anchors_on_the_offending_word_not_the_paragraph(
    act: Path, tmp_path: Path
) -> None:
    """A run is split so the comment marks 'ორასი', not the whole sentence."""
    out, _ = annotate_docx(
        act, [_finding(actual="ორასი (200)", expected="100")], tmp_path / "o.docx"
    )
    doc = Document(str(out))
    assert "ორასი" in [r.text for r in doc.paragraphs[0].runs]


def test_splitting_leaves_no_stray_empty_runs(act: Path, tmp_path: Path) -> None:
    """Every empty run left in the paragraph must be a comment reference, which Word requires;
    splitting artefacts would give a comment an invisible anchor."""
    out, _ = annotate_docx(act, [_finding(actual="ორასი (200)")], tmp_path / "o.docx")
    doc = Document(str(out))
    for run in doc.paragraphs[0].runs:
        if not run.text:
            tags = {c.tag.split("}")[-1] for c in run._r}
            assert "commentReference" in tags


def test_two_findings_in_one_paragraph_keep_both_comments(act: Path, tmp_path: Path) -> None:
    """Cleaning up empty runs must not detach a comment already anchored in that paragraph."""
    out, unanchored = annotate_docx(
        act,
        [
            _finding("AOA.AMOUNT_WORDS", actual="ორასი (200)", expected="100"),
            _finding("AOA.FEE", actual="საფასური შეადგენს", expected="100"),
        ],
        tmp_path / "o.docx",
    )
    assert unanchored == []
    doc = Document(str(out))
    refs = [
        c for run in doc.paragraphs[0].runs for c in run._r if c.tag.endswith("commentReference")
    ]
    assert len(refs) == 2


def test_findings_with_no_anchor_are_returned_not_dropped(act: Path, tmp_path: Path) -> None:
    """An annotated document that quietly omits findings is worse than no annotation."""
    orphan = _finding(
        "AOA.REGISTRY", severity="warning", message_ka="რეესტრი", message_en="registry"
    )
    out, unanchored = annotate_docx(act, [orphan], tmp_path / "o.docx")
    assert [f.rule_id for f in unanchored] == ["AOA.REGISTRY"]
    assert "AOA.REGISTRY" in _comments_xml(out)  # still present, as a document-level note


def test_the_source_document_is_not_modified(act: Path, tmp_path: Path) -> None:
    before = act.read_bytes()
    annotate_docx(act, [_finding(actual="ორასი (200)")], tmp_path / "o.docx")
    assert act.read_bytes() == before


def test_a_pdf_is_rejected_explicitly(tmp_path: Path) -> None:
    """Scanned PDFs take the report-and-highlight path; failing loudly keeps them from being
    silently served worse than DOCX inputs."""
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(UnsupportedDocumentError, match="Scanned PDFs"):
        annotate_docx(pdf, [_finding()], tmp_path / "o.docx")


def test_comment_text_is_bilingual() -> None:
    text = comment_text(_finding(actual="ორასი", expected="100"))
    assert "ციფრი და სიტყვიერი" in text and "digits and words disagree" in text


def test_findings_inside_tables_are_marked_in_place(tmp_path: Path) -> None:
    """Acceptance acts lay their numbered sections out in tables, and Document.paragraphs does
    not reach inside one - so the amount, the dates and the signatories, the fields most in
    need of marking, all fell back to a document-level note."""
    path = tmp_path / "table_act.docx"
    doc = Document()
    table = doc.add_table(rows=1, cols=1)
    para = table.rows[0].cells[0].paragraphs[0]
    para.add_run("საფასური შეადგენს 100 (ორასი) ლარს.")
    doc.save(path)

    out, unanchored = annotate_docx(
        path, [_finding(actual="ორასი (200)", expected="100")], tmp_path / "o.docx"
    )
    assert unanchored == []
    result = Document(str(out))
    runs = [r.text for r in result.tables[0].rows[0].cells[0].paragraphs[0].runs]
    assert "ორასი" in runs


def test_quoting_differences_do_not_defeat_the_anchor(tmp_path: Path) -> None:
    """A finding states the cleaned value 'შპს ნიმუში'; the document writes 'შპს „ნიმუში“'."""
    path = tmp_path / "quoted.docx"
    doc = Document()
    doc.add_paragraph("მხარე: შპს „ნიმუში“ საიდენტიფიკაციო კოდი: 405123456")
    doc.save(path)

    out, unanchored = annotate_docx(
        path,
        [_finding("AOA.PARTY_NAME", actual="შპს ნიმუში", expected="შპს სხვა ნიმუში")],
        tmp_path / "o.docx",
    )
    assert unanchored == []
    assert "AOA.PARTY_NAME" in _comments_xml(out)
