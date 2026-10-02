"""Defects the sixth red-team round found in the acceptance-act work: the corrections a reviewer
accepts as tracked changes, the registry extract reader, the contractor's type, the act's date,
the services in tables, the plot code, addresses and the annotated PDF.

Each test states the defect's id (R6-NN) and what the spec asks for; every widened reading has a
near miss beside it that must still not match. Invented text only: the pair is the red team's.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from docx import Document
from lxml import etree

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.registry.snapshot import RegistryEntry
from procurement_agent.registry.source import RegistrySource
from procurement_agent.report.docx_annotate import annotate_docx, paragraph_text, tracked_edits

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

CONTRACT = """ხელშეკრულება №7
ქ. თბილისი                    2 მარტი, 2026 წელი
შპს „ომეგა ტრეიდი“, საიდენტიფიკაციო კოდი: 404123457; იურიდიული მისამართი: ქ. თბილისი,
წერეთლის 41 (შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის ლევან ბერიძის სახით და
შპს „სიგმა სერვისი“, საიდენტიფიკაციო კოდი: 405987612, იურიდიული მისამართი: ქ. ქუთაისი,
ნინოშვილის 8 (შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის თამარ გოგოლაძის სახით
1.1. „შემსრულებელი“ იღებს ვალდებულებას „ნათელი“ პროექტისთვის (ს/კ: 03.04.05.678)
მომსახურების გაწევა.
2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან - 2026 წლის 1 სექტემბრის ჩათვლით.
4.1. ღირებულება შეადგენს 700 (შვიდასი) ლარს, რომელიც მოიცავს გადასახადებს მათ შორის დღგ-ს.
4.3. ანგარიშსწორება: სს „ზღაპრული ბანკი“, ბანკის კოდი: ZZBKGE22,
ანგარიშის ნომერი: GE15ZZ0000000123456789.
"""
ACT_LINES = [
    "მიღება-ჩაბარების აქტი",
    "შპს „ომეგა ტრეიდი“. საიდენტიფიკაციო კოდი: 404123457; იურიდიული მისამართი: ქ. თბილისი, "
    "წერეთლის 41 (შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის ლევან ბერიძის სახით და",
    "შპს „სიგმა სერვისი“, საიდენტიფიკაციო კოდი: 405987612, იურიდიული მისამართი: ქ. ქუთაისი, "
    "ნინოშვილის 8 (შემდგომში „შემსრულებელი“)",
    "„შემკვეთის“ პროექტ „ნათელი“ (ს/კ: 03.04.05.678) მომსახურების გაწევა.",
    "2. შესრულების თარიღი",
    "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით.",
    "3. საფასური",
    "საფასური შეადგენს 700 (შვიდასი) ლარს გადასახადების ჩათვლით. ბანკი: სს „ზღაპრული ბანკი“;",
    "ბანკის კოდი: ZZBKGE22; ა/ა: GE15ZZ0000000123456789",
    "4. გაფორმების თარიღი",
    "აქტი გაფორმდა 2026 წლის 30 აპრილი",
    "„შემკვეთი“",
    "შპს „ომეგა ტრეიდი“",
    "დირექტორი ლევან ბერიძე",
    "„შემსრულებელი“",
    "შპს „სიგმა სერვისი“",
    "დირექტორი: თამარ გოგოლაძე",
]
ACT = "\n".join(ACT_LINES) + "\n"


def _check(act: str = ACT, contract: str | None = CONTRACT, **kw: object) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract, **kw)  # type: ignore[arg-type]


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _word(lines: list[str]) -> bytes:
    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _accepted(tmp_path: Path, lines: list[str], findings: list[Finding]) -> list[str]:
    """The act's paragraphs once annotated and every tracked change accepted."""
    src = tmp_path / "act.docx"
    src.write_bytes(_word(lines))
    out, _ = annotate_docx(src, findings, tmp_path / "o.docx")
    return [paragraph_text(p, accepted=True) for p in Document(str(out)).paragraphs]


def _revisions(path: Path) -> int:
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    return sum(1 for el in root.iter(f"{_W}ins"))


# --- R6-01: the registry's legal form, corrected in the party's own name ------------------------


class TestRegistryLegalForm:
    EXTRACT = RegistryEntry(
        id_code="405987612",
        name="სს სიგმა სერვისი",
        legal_form="სააქციო საზოგადოება",
        source="extract",
    )

    def test_only_the_partys_own_form_is_changed(self, tmp_path: Path) -> None:
        report = _check(registry=RegistrySource.of(None, [self.EXTRACT]))
        [finding] = [f for f in _of(report, "AOA.REGISTRY_NAME") if f.replacement]
        accepted = _accepted(tmp_path, ACT_LINES, [finding])
        assert accepted[2].startswith("სს „სიგმა სერვისი“, საიდენტიფიკაციო კოდი: 405987612")
        assert accepted[15] == "სს „სიგმა სერვისი“"
        # the customer's name, whose form is right, is left as it was
        assert accepted[1].startswith("შპს „ომეგა ტრეიდი“")
        assert accepted[12] == "შპს „ომეგა ტრეიდი“"


class TestAlignmentOfWords:
    def test_a_word_is_never_replaced_by_a_phrase(self) -> None:
        """Near miss of the values that may be replaced whole: a code, an account, grouped or
        not."""
        assert tracked_edits("შპს", "სს სიგმა სერვისი") is None
        assert tracked_edits("სერვისი", "სიგმა სერვისი ჯგუფი") is None
        written = "GE15 ZZ00 0000 0123 4567 89"
        assert tracked_edits(written, "GE15ZZ0000000123456789") == [
            (0, len(written), "GE15ZZ0000000123456789")
        ]
        assert tracked_edits("405-987-621", "405987612") == [(0, 11, "405987612")]


# --- R6-02, R6-07, R6-12, R6-15, R6-16: where a tracked change may go --------------------------


def _finding(quote: str, replacement: str, **kw: object) -> Finding:
    return Finding(
        rule_id="AOA.ACT_DATE",
        severity="error",
        message_ka="შეცდომა",
        message_en="error",
        expected=replacement,
        actual=quote,
        quote=quote,
        replacement=replacement,
        **kw,  # type: ignore[arg-type]
    )


def _xml(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode()


def _raw(tmp_path: Path, body: str) -> Path:
    """A Word file whose first paragraph is ``body`` (raw WordprocessingML inside ``w:p``)."""
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    doc = Document()
    doc.add_paragraph("მიღება-ჩაბარების აქტი")
    doc.element.body.insert(1, parse_xml(f"<w:p {nsdecls('w')}>{body}</w:p>"))
    path = tmp_path / "raw.docx"
    doc.save(str(path))
    return path


class TestWhereATrackedChangeGoes:
    LINES = ("ხელშეკრულება დაიდო 20.03.2026 წელს.", "აქტი გაფორმდა 20.03.2026 წელს.")

    def test_a_place_the_file_counts_differently_gets_a_comment_alone(self, tmp_path: Path) -> None:
        """R6-02: the check counted three places (a header it read too), the file shows two:
        which one is the act's own date cannot be told, and nothing is rewritten."""
        finding = _finding("20.03.2026", "25.03.2026", places=((1,), 3))
        src = tmp_path / "act.docx"
        src.write_bytes(_word(list(self.LINES)))
        out, _ = annotate_docx(src, [finding], tmp_path / "o.docx")
        assert _revisions(out) == 0

    def test_the_words_around_a_place_say_which_it_is(self, tmp_path: Path) -> None:
        """R6-02: the check read the act's date first (a text box before its anchor, say); the
        file writes the contract's first. The words around each place pick the act's own."""
        checked = "აქტი გაფორმდა 20.03.2026 წელს.\nხელშეკრულება დაიდო 20.03.2026 წელს."
        finding = _finding("20.03.2026", "25.03.2026", places=((0,), 2))
        src = tmp_path / "act.docx"
        src.write_bytes(_word(list(self.LINES)))
        out, _ = annotate_docx(src, [finding], tmp_path / "o.docx", text=checked)
        accepted = [paragraph_text(p, accepted=True) for p in Document(str(out)).paragraphs]
        assert accepted == [
            "ხელშეკრულება დაიდო 20.03.2026 წელს.",
            "აქტი გაფორმდა 25.03.2026 წელს.",
        ]

    def test_a_value_in_an_inline_content_control_is_found(self, tmp_path: Path) -> None:
        """R6-02: a date picker inside the paragraph: Docling reads it, and so must the marks."""
        src = _raw(
            tmp_path,
            "<w:r><w:t xml:space='preserve'>აქტი გაფორმდა </w:t></w:r>"
            "<w:sdt><w:sdtPr/><w:sdtContent><w:r><w:t>20.03.2026</w:t></w:r></w:sdtContent></w:sdt>",
        )
        out, unanchored = annotate_docx(
            src, [_finding("20.03.2026", "25.03.2026")], tmp_path / "o.docx"
        )
        assert unanchored == []
        assert "<w:commentRangeStart" in _xml(out)

    def test_another_reviewers_change_between_the_words_blocks_the_change(
        self, tmp_path: Path
    ) -> None:
        """R6-07: moving the runs past an earlier deletion would reorder the act's words."""
        src = _raw(
            tmp_path,
            "<w:r><w:t>40598</w:t></w:r>"
            "<w:del w:id='7' w:author='x' w:date='2026-01-01T00:00:00Z'>"
            "<w:r><w:delText>33</w:delText></w:r></w:del>"
            "<w:r><w:t>7621</w:t></w:r>",
        )
        finding = Finding(
            rule_id="AOA.ID_CODES",
            severity="error",
            message_ka="შეცდომა",
            message_en="error",
            expected="405987612",
            quote="405987621",
            actual="405987621",
            replacement="405987612",
        )
        out, _ = annotate_docx(src, [finding], tmp_path / "o.docx")
        assert _revisions(out) == 0
        rejected = [paragraph_text(p) for p in Document(str(out)).paragraphs]
        assert rejected[1] == "40598337621"  # the earlier deletion still between its words

    def test_a_fields_result_is_never_rewritten_and_its_comment_marks_the_field(
        self, tmp_path: Path
    ) -> None:
        """R6-12: a code shown by a DOCPROPERTY field: Word computes it, so no tracked change,
        and the comment goes round the whole field rather than inside its result."""
        src = _raw(
            tmp_path,
            "<w:r><w:t xml:space='preserve'>კოდი: </w:t></w:r>"
            "<w:r><w:fldChar w:fldCharType='begin'/></w:r>"
            "<w:r><w:instrText xml:space='preserve'> DOCPROPERTY code </w:instrText></w:r>"
            "<w:r><w:fldChar w:fldCharType='separate'/></w:r>"
            "<w:r><w:t>405987621</w:t></w:r>"
            "<w:r><w:fldChar w:fldCharType='end'/></w:r>",
        )
        finding = Finding(
            rule_id="AOA.ID_CODES",
            severity="error",
            message_ka="შეცდომა",
            message_en="error",
            expected="405987612",
            quote="405987621",
            actual="405987621",
            replacement="405987612",
        )
        out, _ = annotate_docx(src, [finding], tmp_path / "o.docx")
        assert _revisions(out) == 0
        root = etree.fromstring(_xml(out).encode())
        [p] = [p for p in root.iter(f"{_W}p") if "კოდი" in "".join(root.itertext())][1:2] or [
            list(root.iter(f"{_W}p"))[1]
        ]
        children = [c.tag.split("}")[1] for c in p]
        start = children.index("commentRangeStart")
        begin = next(i for i, c in enumerate(p) if c.find(f"{_W}fldChar") is not None)
        assert start < begin  # the comment opens before the field begins

    def test_an_id_that_is_a_digit_only_to_unicode_does_not_break_the_file(
        self, tmp_path: Path
    ) -> None:
        """R6-15: a bookmark id "²": str.isdigit takes it, int does not."""
        src = _raw(
            tmp_path,
            "<w:bookmarkStart w:id='²' w:name='x'/><w:r><w:t>405987621</w:t></w:r>",
        )
        finding = Finding(
            rule_id="AOA.ID_CODES",
            severity="error",
            message_ka="შეცდომა",
            message_en="error",
            expected="405987612",
            quote="405987621",
            actual="405987621",
            replacement="405987612",
        )
        out, _ = annotate_docx(src, [finding], tmp_path / "o.docx")
        assert _revisions(out) == 1

    def test_a_page_break_in_a_run_is_kept(self, tmp_path: Path) -> None:
        """R6-16: a run holding a page break is never rewritten by splitting."""
        src = _raw(
            tmp_path,
            "<w:r><w:t xml:space='preserve'>კოდი 405987621 </w:t>"
            "<w:br w:type='page'/><w:t>ბოლო</w:t></w:r>",
        )
        finding = Finding(
            rule_id="AOA.ID_CODES",
            severity="error",
            message_ka="შეცდომა",
            message_en="error",
            expected="405987612",
            quote="405987621",
            actual="405987621",
            replacement="405987612",
        )
        out, _ = annotate_docx(src, [finding], tmp_path / "o.docx")
        assert 'w:type="page"' in _xml(out)
