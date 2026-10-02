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


def _tracked(path: Path) -> tuple[list[str], list[str]]:
    """The text each tracked deletion and each tracked insertion holds, in document order."""
    from lxml import etree

    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    deleted = ["".join(t.text or "" for t in d.iter(f"{w}delText")) for d in root.iter(f"{w}del")]
    inserted = ["".join(t.text or "" for t in i.iter(f"{w}t")) for i in root.iter(f"{w}ins")]
    return deleted, inserted


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


def test_a_month_typo_is_marked_on_the_word_with_its_correction(tmp_path: Path) -> None:
    """The comment marks the misspelled word, not the whole date, and says how to write it."""
    from procurement_agent.checks.aoa import check_aoa
    from tests.unit.test_aoa_check import ACT, CONTRACT

    act = ACT.replace("1 აპრილიდან", "1 აპრლიდან")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.SPELLING"]
    path = tmp_path / "act.docx"
    doc = Document()
    for line in act.splitlines():
        doc.add_paragraph(line)
    doc.save(path)

    out, unanchored = annotate_docx(path, [finding], tmp_path / "o.docx")
    assert unanchored == []
    assert "should be: აპრილიდან" in _comments_xml(out)
    # the word alone is struck through and its correction written beside it, as a tracked change
    assert _tracked(out) == (["აპრლიდან"], ["აპრილიდან"])


def test_the_invoice_note_is_marked_on_the_acts_total(tmp_path: Path) -> None:
    """Rule 9's note says how to invoice the act; it goes on the amount, and says nothing is
    wrong there - no "should be"."""
    from procurement_agent.checks.aoa import check_aoa
    from tests.unit.test_fx_invoice import USD_ACT, USD_CONTRACT

    report = check_aoa(aoa_text=USD_ACT, contract_text=USD_CONTRACT)
    [note] = [f for f in report.findings if f.rule_id == "AOA.FX_INVOICE"]
    path = tmp_path / "act.docx"
    doc = Document()
    for line in USD_ACT.splitlines():
        doc.add_paragraph(line)
    doc.save(path)

    out, unanchored = annotate_docx(path, [note], tmp_path / "o.docx")
    assert unanchored == []
    xml = _comments_xml(out)
    assert "AOA.FX_INVOICE" in xml
    assert "should be" not in xml
    runs = [r.text for p in Document(str(out)).paragraphs for r in p.runs]
    assert "11 000 (თერთმეტი ათასი) აშშ დოლარს" in runs


def test_a_note_for_review_says_so_before_its_message() -> None:
    """A note the rules could not decide asks for a reading, not a correction."""
    note = _finding("AOA.SERVICES", severity="info", needs_review=True)
    lines = comment_text(note).split("\n")
    assert lines[0] == "[ინფორმაცია / Info] AOA.SERVICES"
    assert lines[1] == "გადასამოწმებელი / Needs review: ციფრი და სიტყვიერი ჩანაწერი არ ემთხვევა"
    assert "გადასამოწმებელი" not in comment_text(_finding())


def test_a_service_for_review_is_marked_on_the_acts_own_line(tmp_path: Path) -> None:
    """The act lists a service the contract never did: the note goes on that line of the annex,
    with no "should be" - there is nothing to correct it to."""
    from procurement_agent.checks.aoa import check_aoa
    from tests.unit.test_aoa_scope import ACT, CONTRACT

    extra = "- 10 სერვისის გვერდის შექმნა და ლოგოს დიზაინი"
    act = ACT.replace("- საკონტაქტო ფორმის აწყობა\n", f"- საკონტაქტო ფორმის აწყობა\n{extra}\n")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    [note] = [f for f in report.findings if f.rule_id == "AOA.SERVICES"]
    path = tmp_path / "act.docx"
    doc = Document()
    for line in act.splitlines():
        doc.add_paragraph(line)
    doc.save(path)

    out, unanchored = annotate_docx(path, [note], tmp_path / "o.docx")
    assert unanchored == []
    xml = _comments_xml(out)
    assert "გადასამოწმებელი / Needs review:" in xml
    assert "should be" not in xml
    runs = [r.text for p in Document(str(out)).paragraphs for r in p.runs]
    assert extra[2:] in runs  # the comment covers the item itself


# --- AOA-10: a word is marked where it stands as a word, not inside a longer one -----------------


def _anchored(path: Path) -> list[str]:
    from procurement_agent.eval.score import comment_anchors

    return list(comment_anchors(path).values())


def _paragraphs_doc(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "act.docx"
    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    doc.save(path)
    return path


def test_a_typo_is_not_marked_inside_the_correct_words_it_begins(tmp_path: Path) -> None:
    """'ივნის' (a letter dropped) is marked on itself, not inside 'ივნისიდან' or 'ივნისის'."""
    path = _paragraphs_doc(
        tmp_path,
        "შესრულდა 2026 წლის 1 ივნისიდან 2026 წლის 30 ივნისის ჩათვლით.",
        "აქტი გაფორმდა 2026 წლის 30 ივნის.",
    )
    typo = _finding("AOA.SPELLING", severity="warning", quote="ივნის", expected="ივნისს")
    out, unanchored = annotate_docx(path, [typo], tmp_path / "o.docx")
    assert unanchored == []
    assert _anchored(out) == ["ივნის"]
    marked = Document(str(out)).paragraphs[1]
    assert "ივნის" in [r.text for r in marked.runs]


def test_the_nominative_is_not_marked_inside_the_genitive(tmp_path: Path) -> None:
    """Two findings, the genitive in the preamble and the nominative in the signature: one
    comment each, every one in the case written at its place."""
    path = _paragraphs_doc(
        tmp_path,
        "წარმოდგენილი დირექტორის, თემურ კაპანაშვილის სახით, მეორე მხრივ,",
        "დირექტორი: თემურ კაპანაშვილი",
    )
    genitive = _finding("AOA.DIRECTOR", quote="თემურ კაპანაშვილის", expected="თემურ კობიაშვილის")
    nominative = _finding("AOA.DIRECTOR", quote="თემურ კაპანაშვილი", expected="თემურ კობიაშვილი")
    out, _ = annotate_docx(path, [genitive, nominative], tmp_path / "o.docx")
    assert sorted(_anchored(out)) == ["თემურ კაპანაშვილი", "თემურ კაპანაშვილის"]
    xml = _comments_xml(out)
    assert xml.count("should be:") == 2


def test_a_stem_found_only_inside_a_longer_word_is_still_marked(tmp_path: Path) -> None:
    """A finding whose words stand nowhere as words of their own - a project named in the
    nominative, the act writing it with a postposition - is still marked where it stands."""
    path = _paragraphs_doc(tmp_path, "პროექტ „ტყის ბილიკისთვის“ მომსახურება")
    finding = _finding("AOA.PROJECT_NAME", quote="ტყის ბილიკ", expected="ტყის სახლი")
    out, unanchored = annotate_docx(path, [finding], tmp_path / "o.docx")
    assert unanchored == []
    assert _anchored(out) == ["ტყის ბილიკ"]


# --- AOA-09: a finding that names some of its places marks those only ---------------------------


def _name_in_paragraph_and_table(tmp_path: Path, *, merged: bool = False) -> Path:
    """The name in a paragraph, then in a signature table, then in a paragraph after it."""
    path = tmp_path / "act.docx"
    doc = Document()
    doc.add_paragraph("წარმოდგენილი დირექტორის, ლია ბერიძის სახით")
    table = doc.add_table(rows=1, cols=2)
    if merged:
        table.rows[0].cells[0].merge(table.rows[0].cells[1])
    table.rows[0].cells[0].paragraphs[0].add_run("დირექტორი: ლია ბერიძე")
    doc.add_paragraph("დანართი: ლია ბერიძე")
    doc.save(path)
    return path


def test_the_places_named_are_counted_in_reading_order(tmp_path: Path) -> None:
    """The table stands between the two paragraphs, and its place is the one named."""
    finding = _finding("AOA.DIRECTOR", quote="ლია ბერიძე", places=((0,), 2))
    out, _ = annotate_docx(_name_in_paragraph_and_table(tmp_path), [finding], tmp_path / "o.docx")
    result = Document(str(out))
    assert _anchored(out) == ["ლია ბერიძე"]
    assert "ლია ბერიძე" in [r.text for r in result.tables[0].rows[0].cells[0].paragraphs[0].runs]


def test_a_merged_cell_is_one_place(tmp_path: Path) -> None:
    finding = _finding("AOA.DIRECTOR", quote="ლია ბერიძე", places=((1,), 2))
    path = _name_in_paragraph_and_table(tmp_path, merged=True)
    out, _ = annotate_docx(path, [finding], tmp_path / "o.docx")
    assert _anchored(out) == ["ლია ბერიძე"]
    assert "ლია ბერიძე" in [r.text for r in Document(str(out)).paragraphs[1].runs]


def test_places_the_document_does_not_show_mark_every_place(tmp_path: Path) -> None:
    """The checked text counted three places, the file shows two: the ordinals cannot be
    trusted, and every place is marked rather than a wrong one."""
    finding = _finding("AOA.DIRECTOR", quote="ლია ბერიძე", places=((2,), 3))
    out, _ = annotate_docx(_name_in_paragraph_and_table(tmp_path), [finding], tmp_path / "o.docx")
    assert _anchored(out) == ["ლია ბერიძე", "ლია ბერიძე"]
