"""Corrections written into the act as tracked changes (the client's "Track Change"), beside the
comments: the words at fault struck through, the right ones inserted, for a reviewer to accept
or reject in Word. Invented text only.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding, SkipNote
from procurement_agent.eval.score import comment_anchors
from procurement_agent.report.docx_annotate import (
    annotate_docx,
    paragraph_text,
    summary_comment,
    tracked_edits,
)
from tests.unit.test_aoa_check import ACT, CONTRACT

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_WHEN = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _finding(quote: str, replacement: str | None, rule: str = "AOA.PARTY_NAME") -> Finding:
    return Finding(
        rule_id=rule,
        severity="error",
        message_ka="შეცდომა",
        message_en="error",
        expected=replacement,
        actual=quote,
        quote=quote,
        replacement=replacement,
    )


def _act(tmp_path: Path, *lines: str, bold: str | None = None) -> Path:
    doc = Document()
    for line in lines:
        p = doc.add_paragraph()
        if bold and bold in line:
            head, tail = line.split(bold, 1)
            p.add_run(head)
            p.add_run(bold).bold = True
            p.add_run(tail)
        else:
            p.add_run(line)
    path = tmp_path / "act.docx"
    doc.save(str(path))
    return path


def _texts(path: Path, *, accepted: bool) -> list[str]:
    return [paragraph_text(p, accepted=accepted) for p in Document(str(path)).paragraphs]


def _revisions(path: Path) -> list[etree._Element]:
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    return [el for el in root.iter() if el.tag in (f"{_W}del", f"{_W}ins")]


class TestTrackedEdits:
    def test_only_the_word_that_differs_is_replaced(self) -> None:
        written = "შპს „დელტა ტრთიდი“"
        [(a, b, new)] = tracked_edits(written, "შპს დელტა ტრეიდი") or []
        assert (written[a:b], new) == ("ტრთიდი", "ტრეიდი")

    def test_each_differing_word_of_an_equal_length_phrase(self) -> None:
        written = "15 (თხუთმეტი) სამუშაო დღის"
        edits = tracked_edits(written, "5 (ხუთი) სამუშაო დღის") or []
        assert [(written[a:b], new) for a, b, new in edits] == [("15", "5"), ("თხუთმეტი", "ხუთი")]

    def test_other_separators_replace_the_whole(self) -> None:
        assert tracked_edits("01-02-03-456", "01.02.03.456") == [(0, 12, "01.02.03.456")]

    def test_one_value_for_another_replaces_the_whole(self) -> None:
        written = "GE29 NB00 0000 0101 9049 17"
        assert tracked_edits(written, "GE29NB0000000101904917") == [
            (0, len(written), "GE29NB0000000101904917")
        ]

    def test_a_run_of_words_between_shared_words(self) -> None:
        written = "გადაიხდება 15 (თხუთმეტი) დღეში"
        [(a, b, new)] = tracked_edits(written, "გადაიხდება 5 დღეში") or []
        assert (written[a:b], new) == ("15 (თხუთმეტი", "5")

    def test_words_that_cannot_be_lined_up_get_no_change(self) -> None:
        """Near misses: a rewrite with no word in common at either end, and a value left out
        (an addition), are a comment's to explain."""
        assert tracked_edits("ფასადის შეღებვა", "სახურავის მთლიანი შეკეთება") is None
        assert tracked_edits("შპს დელტა", "შპს დელტა ტრეიდი") is None

    def test_words_that_already_read_right_need_no_change(self) -> None:
        assert tracked_edits("შპს „დელტა“", "შპს დელტა") == []


class TestAnnotateWithTrackedChanges:
    LINES = ("შპს „დელტა ტრთიდი“, საიდენტიფიკაციო კოდი: 987654312", "მეორე აბზაცი.")

    def test_accepting_reads_the_correction_and_rejecting_the_original(
        self, tmp_path: Path
    ) -> None:
        src = _act(tmp_path, *self.LINES)
        findings = [
            _finding("შპს „დელტა ტრთიდი“", "შპს დელტა ტრეიდი"),
            _finding("987654312", "987654321", rule="AOA.ID_CODES"),
        ]
        out, unanchored = annotate_docx(src, findings, tmp_path / "o.docx", when=_WHEN)
        assert unanchored == []
        assert _texts(out, accepted=False) == list(self.LINES)
        assert _texts(out, accepted=True) == [
            "შპს „დელტა ტრეიდი“, საიდენტიფიკაციო კოდი: 987654321",
            "მეორე აბზაცი.",
        ]
        # the comments still mark the words the act wrote
        # (folded: quotes dropped)
        assert sorted(comment_anchors(out).values()) == ["987654312", "შპს დელტა ტრთიდი"]

    def test_a_revision_is_dated_signed_and_numbered_apart(self, tmp_path: Path) -> None:
        src = _act(tmp_path, *self.LINES)
        out, _ = annotate_docx(
            src, [_finding("987654312", "987654321")], tmp_path / "o.docx", when=_WHEN
        )
        revisions = _revisions(out)
        assert [r.tag.split("}")[1] for r in revisions] == ["del", "ins"]
        assert {r.get(f"{_W}author") for r in revisions} == {"Procurement Agent"}
        assert {r.get(f"{_W}date") for r in revisions} == {"2026-10-02T12:00:00Z"}
        assert len({r.get(f"{_W}id") for r in revisions}) == 2

    def test_the_inserted_word_keeps_the_words_formatting(self, tmp_path: Path) -> None:
        src = _act(tmp_path, *self.LINES, bold="ტრთიდი")
        out, _ = annotate_docx(
            src, [_finding("შპს „დელტა ტრთიდი“", "შპს დელტა ტრეიდი")], tmp_path / "o.docx"
        )
        [inserted] = [r for r in _revisions(out) if r.tag == f"{_W}ins"]
        assert inserted.find(f"{_W}r/{_W}rPr/{_W}b") is not None

    def test_a_finding_without_a_replacement_gets_a_comment_alone(self, tmp_path: Path) -> None:
        src = _act(tmp_path, *self.LINES)
        out, _ = annotate_docx(src, [_finding("987654312", None)], tmp_path / "o.docx")
        assert _revisions(out) == []
        assert list(comment_anchors(out).values()) == ["987654312"]

    def test_the_first_finding_to_claim_the_words_writes_them(self, tmp_path: Path) -> None:
        """Two findings with different corrections for the same words: one tracked change, the
        first's, and both comments."""
        src = _act(tmp_path, *self.LINES)
        findings = [
            _finding("987654312", "987654321", rule="AOA.ID_CODES"),
            _finding("987654312", "111111111", rule="AOA.REGISTRY"),
        ]
        out, _ = annotate_docx(src, findings, tmp_path / "o.docx")
        assert _texts(out, accepted=True)[0].endswith("987654321")
        assert len([r for r in _revisions(out) if r.tag == f"{_W}ins"]) == 1
        with zipfile.ZipFile(out) as z:
            comments = z.read("word/comments.xml").decode()
        assert "AOA.ID_CODES" in comments and "AOA.REGISTRY" in comments

    def test_a_run_that_is_more_than_text_is_not_changed(self, tmp_path: Path) -> None:
        """A field inside the words: a tracked change would rewrite what Word computes, so the
        place keeps its comment alone."""
        doc = Document()
        p = doc.add_paragraph()
        p.add_run("კოდი: 987")
        run = p.add_run("654312")
        run._r.append(OxmlElement("w:fldChar"))
        src = tmp_path / "field.docx"
        doc.save(str(src))
        out, unanchored = annotate_docx(
            src, [_finding("987654312", "987654321")], tmp_path / "o.docx"
        )
        assert unanchored == []
        assert _revisions(out) == []

    def test_revision_ids_start_above_every_id_the_document_uses(self, tmp_path: Path) -> None:
        doc = Document()
        p = doc.add_paragraph("კოდი: 987654312")
        mark = OxmlElement("w:bookmarkStart")
        mark.set(qn("w:id"), "40")
        mark.set(qn("w:name"), "x")
        p._p.insert(0, mark)
        src = tmp_path / "ids.docx"
        doc.save(str(src))
        out, _ = annotate_docx(src, [_finding("987654312", "987654321")], tmp_path / "o.docx")
        assert all(int(r.get(f"{_W}id")) > 40 for r in _revisions(out))

    def test_characters_xml_cannot_carry_are_dropped(self, tmp_path: Path) -> None:
        src = _act(tmp_path, *self.LINES)
        out, _ = annotate_docx(src, [_finding("987654312", "9876\x0b54321")], tmp_path / "o.docx")
        assert _texts(out, accepted=True)[0].endswith("987654321")


class TestSummaryComment:
    def test_the_summary_and_the_unanchored_open_the_file_on_its_first_word(
        self, tmp_path: Path
    ) -> None:
        src = _act(tmp_path, "მიღება-ჩაბარების აქტი", "მეორე აბზაცი.")
        report = CheckReport(
            document_id="x",
            skipped=[
                SkipNote(rule_id="AOA.FX_RATE", reason_ka="კურსი არ არის", reason_en="no rate")
            ],
            summary={"not_applicable": "AOA.TAX_WORDING"},
        )
        summary = summary_comment(report)
        assert summary is not None and "AOA.FX_RATE" in summary and "AOA.TAX_WORDING" in summary
        orphan = _finding("სიტყვა, რომელიც აქტში არ წერია", None, rule="AOA.REGISTRY")
        out, unanchored = annotate_docx(src, [orphan], tmp_path / "o.docx", summary=summary)
        assert [f.rule_id for f in unanchored] == ["AOA.REGISTRY"]
        assert list(comment_anchors(out).values()) == ["მიღება"]  # the first word
        with zipfile.ZipFile(out) as z:
            comments = z.read("word/comments.xml").decode()
        assert "AOA.FX_RATE" in comments and "AOA.REGISTRY" in comments
        # the body is the act's: no paragraph added for the notes
        assert _texts(out, accepted=False) == ["მიღება-ჩაბარების აქტი", "მეორე აბზაცი."]

    def test_a_report_with_nothing_skipped_has_no_summary(self) -> None:
        assert summary_comment(CheckReport(document_id="x")) is None


class TestReplacementsTheCheckSets:
    def test_a_code_slip_is_offered_as_a_tracked_change(self) -> None:
        report = check_aoa(aoa_text=ACT.replace("987654321", "987654312"), contract_text=CONTRACT)
        [finding] = [f for f in report.findings if f.rule_id == "AOA.ID_CODES"]
        assert (finding.quote, finding.replacement) == ("987654312", "987654321")

    def test_a_value_left_out_is_no_replacement(self) -> None:
        act = ACT.replace("საიდენტიფიკაციო კოდი: 987654321, ", "")
        [finding] = [
            f
            for f in check_aoa(aoa_text=act, contract_text=CONTRACT).findings
            if f.rule_id == "AOA.ID_CODES"
        ]
        assert finding.expected == "987654321" and finding.replacement is None

    def test_a_note_to_check_is_no_replacement(self) -> None:
        report = check_aoa(aoa_text=ACT, contract_text=CONTRACT)
        assert all(f.replacement is None for f in report.findings if f.needs_review)


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice is not installed")
def test_libreoffice_reads_the_changes_and_the_comments(tmp_path: Path) -> None:
    """The annotated file opens in another word processor with its revisions and comments."""
    src = _act(tmp_path, *TestAnnotateWithTrackedChanges.LINES)
    findings = [
        _finding("შპს „დელტა ტრთიდი“", "შპს დელტა ტრეიდი"),
        _finding("987654312", "987654321", rule="AOA.ID_CODES"),
    ]
    out, _ = annotate_docx(src, findings, tmp_path / "o.docx")
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "odt", "--outdir", str(tmp_path), str(out)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    with zipfile.ZipFile(tmp_path / "o.odt") as z:
        content = z.read("content.xml").decode()
    assert content.count("<text:deletion>") == 2
    assert content.count("<text:insertion>") == 2
    assert content.count("<office:annotation ") == 2
