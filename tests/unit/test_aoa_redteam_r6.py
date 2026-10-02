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

import pytest
from docx import Document
from lxml import etree

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.aoa_scope import _table_items, scope_part
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.registry.extract import parse_extract
from procurement_agent.registry.fields import compare_addresses
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


def _accepted(
    tmp_path: Path, lines: list[str], findings: list[Finding], *, checked: bool = False
) -> list[str]:
    """The act's paragraphs once annotated and every tracked change accepted; ``checked``: the
    annotator is given the checked text, as the API gives it."""
    src = tmp_path / "act.docx"
    src.write_bytes(_word(lines))
    text = "\n".join(lines) + "\n" if checked else None
    out, _ = annotate_docx(src, findings, tmp_path / "o.docx", text=text)
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


# --- R6-03: a value copied from the other party, corrected only where it is wrong ---------------

_CUSTOMER_ADDRESS = "ქ. თბილისი, წერეთლის 41"
_CONTRACTOR_ADDRESS = "ქ. ქუთაისი, ნინოშვილის 8"
_CUSTOMER_ACCOUNT = "GE64ZZ0000000987654321"
_CONTRACTOR_ACCOUNT = "GE15ZZ0000000123456789"


def _customer_account(text: str) -> str:
    """The customer's own account, in its block of the preamble."""
    return text.replace(
        "წერეთლის 41 (შემდგომში", f"წერეთლის 41, ანგარიში: {_CUSTOMER_ACCOUNT} (შემდგომში"
    )


def _each_partys_account(text: str) -> str:
    return _customer_account(text).replace(
        "ნინოშვილის 8 (შემდგომში", f"ნინოშვილის 8, ანგარიში: {_CONTRACTOR_ACCOUNT} (შემდგომში"
    )


def _act(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


class TestCopiedFromTheOtherParty:
    """The customer's value written for the contractor is right where the customer's block
    states it: a tracked change there would make a right act wrong."""

    def test_an_address_is_corrected_in_the_contractors_block_only(self, tmp_path: Path) -> None:
        lines = [line.replace(_CONTRACTOR_ADDRESS, _CUSTOMER_ADDRESS) for line in ACT_LINES]
        [finding] = [f for f in _of(_check(_act(lines)), "AOA.PARTY_ADDRESS") if f.replacement]
        accepted = _accepted(tmp_path, lines, [finding], checked=True)
        assert accepted[1] == ACT_LINES[1]
        assert accepted[2] == ACT_LINES[2]

    def test_the_contractors_own_copies_are_all_corrected(self, tmp_path: Path) -> None:
        """Near miss: the same wrong address restated in the contractor's signature block is the
        contractor's too."""
        lines = [line.replace(_CONTRACTOR_ADDRESS, _CUSTOMER_ADDRESS) for line in ACT_LINES]
        lines.append(f"მისამართი: {_CUSTOMER_ADDRESS}")
        [finding] = [f for f in _of(_check(_act(lines)), "AOA.PARTY_ADDRESS") if f.replacement]
        accepted = _accepted(tmp_path, lines, [finding], checked=True)
        assert accepted[1] == ACT_LINES[1]
        assert accepted[2] == ACT_LINES[2]
        assert accepted[-1] == f"მისამართი: {_CONTRACTOR_ADDRESS}"

    def test_an_address_against_the_registry_likewise(self, tmp_path: Path) -> None:
        lines = [line.replace(_CONTRACTOR_ADDRESS, _CUSTOMER_ADDRESS) for line in ACT_LINES]
        entry = RegistryEntry(
            id_code="405987612",
            name="შპს სიგმა სერვისი",
            address=_CONTRACTOR_ADDRESS,
            source="extract",
        )
        report = _check(_act(lines), None, registry=RegistrySource.of(None, [entry]))
        [finding] = [f for f in _of(report, "AOA.REGISTRY_ADDRESS") if f.replacement]
        accepted = _accepted(tmp_path, lines, [finding], checked=True)
        assert accepted[1] == ACT_LINES[1]
        assert accepted[2] == ACT_LINES[2]

    def test_the_customers_account_is_corrected_in_the_payment_line_only(
        self, tmp_path: Path
    ) -> None:
        lines = [_customer_account(line) for line in ACT_LINES]
        lines[8] = lines[8].replace(_CONTRACTOR_ACCOUNT, _CUSTOMER_ACCOUNT)
        report = _check(_act(lines), _each_partys_account(CONTRACT))
        [finding] = [f for f in _of(report, "AOA.BANK") if f.replacement]
        assert finding.replacement == _CONTRACTOR_ACCOUNT
        accepted = _accepted(tmp_path, lines, [finding], checked=True)
        assert accepted[1] == lines[1]  # the customer's own, where its block states it
        assert accepted[8] == ACT_LINES[8]

    def test_the_customers_account_in_its_own_block_alone_is_no_change(
        self, tmp_path: Path
    ) -> None:
        """Near miss: an act that states only the customer's account, in the customer's block,
        is still reported, but nothing written there is wrong to rewrite."""
        lines = [_customer_account(line) for line in ACT_LINES]
        lines[8] = "ბანკის კოდი: ZZBKGE22"
        report = _check(_act(lines), _each_partys_account(CONTRACT))
        [finding] = [f for f in _of(report, "AOA.BANK") if f.actual == _CUSTOMER_ACCOUNT]
        assert finding.replacement is None
        assert finding.expected == _CONTRACTOR_ACCOUNT

    def test_a_name_is_corrected_where_it_names_the_contractor(self, tmp_path: Path) -> None:
        lines = list(ACT_LINES)
        lines[2] = lines[2].replace("შპს „სიგმა სერვისი“", "შპს „ომეგა ტრეიდი“")
        lines[15] = "შპს „ომეგა ტრეიდი“"
        report = _check(_act(lines))
        findings = [f for f in _of(report, "AOA.PARTY_NAME") if f.replacement]
        assert findings
        accepted = _accepted(tmp_path, lines, findings, checked=True)
        assert accepted[1] == ACT_LINES[1]
        assert accepted[12] == ACT_LINES[12]
        assert accepted[2] == ACT_LINES[2]
        assert accepted[15] == ACT_LINES[15]
        # the contractor's director, under its role and the name copied for it, is its own
        assert _of(report, "AOA.DIRECTOR") == []


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

    SIGNATURES = ("„შემკვეთი“", "20.03.2026", "„შემსრულებელი“", "20.03.2026")

    def test_lines_alike_are_told_apart_by_the_lines_around_them(self, tmp_path: Path) -> None:
        """R6-03: a value alone on its line under each party's role: the role above it says
        which place is which, in the file as in the checked text."""
        checked = "\n".join(self.SIGNATURES) + "\n"
        finding = _finding("20.03.2026", "25.03.2026", places=((1,), 2))
        src = tmp_path / "act.docx"
        src.write_bytes(_word(list(self.SIGNATURES)))
        out, _ = annotate_docx(src, [finding], tmp_path / "o.docx", text=checked)
        accepted = [paragraph_text(p, accepted=True) for p in Document(str(out)).paragraphs]
        assert accepted == ["„შემკვეთი“", "20.03.2026", "„შემსრულებელი“", "25.03.2026"]

    def test_lines_alike_around_other_lines_get_comments_alone(self, tmp_path: Path) -> None:
        """Near miss: a file that writes the blocks in another order than the checked text has
        them: the lines around no place are the ones the check read, and nothing is rewritten."""
        checked = "\n".join(self.SIGNATURES) + "\n"
        finding = _finding("20.03.2026", "25.03.2026", places=((1,), 2))
        src = tmp_path / "act.docx"
        src.write_bytes(_word([*self.SIGNATURES[2:], *self.SIGNATURES[:2]]))
        out, _ = annotate_docx(src, [finding], tmp_path / "o.docx", text=checked)
        assert _revisions(out) == 0

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


# --- R6-06: the act's date against the work done, whatever dates follow ------------------------

_PREAMBLE = (
    "მიღება-ჩაბარების აქტი\nქ. თბილისი\nშპს „ომეგა ტრეიდი“ (საიდენტიფიკაციო კოდი: 404123457) და "
    "შპს „სიგმა სერვისი“ (საიდენტიფიკაციო კოდი: 405987612) შორის დადებული ხელშეკრულების №7 "
    "საფუძველზე.\n"
)


def _act_date_findings(body: str) -> list[Finding]:
    return _of(_check(_PREAMBLE + body, None), "AOA.ACT_DATE")


class TestTheWorkDoneIsNotTheDatesAfterIt:
    """Rule 4: an act dated the day the work was done is right; a date written after the
    sentence that says when - a payment's deadline, a signature's day, a date on a line of its
    own after the act's own date - says nothing of the work."""

    def test_a_payment_deadline_is_not_the_end_of_the_work(self) -> None:
        body = (
            "მომსახურება შესრულდა 25.03.2026.\n"
            "ანგარიშსწორება განხორციელდება 05.04.2026-მდე.\n"
            "აქტი გაფორმდა 25.03.2026."
        )
        assert _act_date_findings(body) == []

    def test_a_signature_date_is_not_the_end_of_the_work(self) -> None:
        body = (
            "მომსახურება შესრულდა 25.03.2026.\n"
            "საფასური შეადგენს 500 (ხუთასი) ლარს.\n"
            "აქტი გაფორმდა 25.03.2026.\n"
            "ხელმოწერა: ________ 26.03.2026"
        )
        assert _act_date_findings(body) == []

    def test_a_date_after_the_acts_own_does_not_hide_a_wrong_act_date(self) -> None:
        body = (
            "მომსახურება შესრულდა 25.03.2026.\n"
            "საფასური შეადგენს 500 (ხუთასი) ლარს.\n"
            "აქტი გაფორმდა 20.03.2026.\n"
            "20.03.2026"
        )
        [finding] = _act_date_findings(body)
        assert (finding.quote, finding.replacement) == ("20.03.2026", "25.03.2026")

    def test_a_signature_on_the_next_line_is_not_the_end_of_the_work(self) -> None:
        body = (
            "მომსახურება შესრულდა 25.03.2026.\n"
            "ხელმოწერა: ________ 26.03.2026\n"
            "აქტი გაფორმდა 25.03.2026."
        )
        assert _act_date_findings(body) == []

    def test_a_period_under_its_heading_still_ends_where_it_says(self) -> None:
        """Near miss: a heading's dates on the line after it, a payment's after them: the
        period is the heading's range, and an act dated on its last day is right."""
        body = (
            "2. შესრულების პერიოდი:\n"
            "01.03.2026 - 31.03.2026\n"
            "3. ანგარიშსწორება განხორციელდება 10.04.2026-მდე.\n"
            "4. აქტი გაფორმდა 31.03.2026."
        )
        assert _act_date_findings(body) == []

    def test_a_verb_ending_its_line_reads_the_dates_on_the_next(self) -> None:
        """Near miss: "შესრულდა:" with the range on the line below is the period still, and an
        act dated before its end is wrong."""
        body = "მომსახურება შესრულდა:\n01.03.2026 - 31.03.2026\nაქტი გაფორმდა 20.03.2026."
        [finding] = _act_date_findings(body)
        assert finding.quote == "20.03.2026"

    def test_a_completion_sentence_with_its_payment_beside_it(self) -> None:
        """Near miss: the payment's deadline in the very sentence that dates the work."""
        body = (
            "მომსახურება შესრულდა 25.03.2026, ანგარიშსწორება 05.04.2026-მდე.\n"
            "აქტი გაფორმდა 25.03.2026."
        )
        assert _act_date_findings(body) == []


# --- R6-04: an extract's page footer ends no section --------------------------------------------

_EXTRACT = (
    "*B90000071 *\nამონაწერი მეწარმეთა და არასამეწარმეო\n(არაკომერციული) იურიდიული პირების\n"
    "რეესტრიდან\nგანაცხადის ნომერი: 765432\nგანაცხადის რეგისტრაციის ნომერი: B90000071\n"
    "ამონაწერის მომზადების თარიღი: 25/04/2026 11:20:00\nსუბიექტი\n"
    "საფირმო სახელწოდება: შპს სიგმა სერვისი\n"
    "იურიდიული მისამართი: საქართველო, ქ. ქუთაისი, ნინოშვილის ქ. №8\n"
    "საიდენტიფიკაციო კოდი: 405987612\n"
    "სამართლებრივი ფორმა: შეზღუდული პასუხისმგებლობის საზოგადოება\n"
    "ხელმძღვანელობაზე/წარმომადგენლობაზე უფლებამოსილი პირები\n"
    "- 01000000077, ნიკა სამხარაძე,\nდირექტორი, ,\n"
    "საჯარო რეესტრის ეროვნული სააგენტო. http://public.reestri.gov.ge გვერდი: 1(2)\n"
    "- 01000000088, თამარ გოგოლაძე,\nდირექტორი, ,\n"
    "დამფუძნებლები\n- 01000000099, ლევან ქავთარაძე,\n"
    "საჯარო რეესტრის ეროვნული სააგენტო. http://public.reestri.gov.ge გვერდი: 2(2)\n"
)


class TestAnExtractOverAPageBreak:
    def test_the_persons_after_a_page_footer_are_read(self) -> None:
        entry, people = parse_extract(_EXTRACT)
        assert entry.directors == ("ნიკა სამხარაძე", "თამარ გოგოლაძე")
        assert [p.position for p in people] == ["დირექტორი", "დირექტორი"]

    def test_the_2025_footer_and_a_page_number_alone_on_its_line(self) -> None:
        text = _EXTRACT.replace(
            "საჯარო რეესტრის ეროვნული სააგენტო. http://public.reestri.gov.ge გვერდი: 1(2)\n",
            "http://public.reestri.gov.ge\n1(2)\n",
        )
        entry, _ = parse_extract(text)
        assert entry.directors == ("ნიკა სამხარაძე", "თამარ გოგოლაძე")

    def test_the_founders_are_still_no_directors(self) -> None:
        """Near miss: the section after the persons still ends theirs."""
        entry, _ = parse_extract(_EXTRACT)
        assert "ლევან ქავთარაძე" not in entry.directors


# --- R6-05: the contractor's tax status, as the act says it of the contractor -------------------

_TYPED = (
    "მიღება-ჩაბარების აქტი\n"
    "შპს „ომეგა ტრეიდი“, საიდენტიფიკაციო კოდი: 404123457{customer} (შემდგომში „შემკვეთი“) და\n"
    "ი/მ „გიორგი ნიკოლაძე“, პირადი ნომერი: 01001012345{contractor} (შემდგომში „შემსრულებელი“)\n"
    "„შემკვეთის“ პროექტ „ნათელი“ (ს/კ: 03.04.05.678) მომსახურების გაწევა.\n"
    "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით.\n"
    "საფასური შეადგენს 700 (შვიდასი) ლარს, დღგ-ს გარეშე.\nაქტი გაფორმდა 2026 წლის 30 აპრილი\n"
)


def _type_of(customer: str = "", contractor: str = "") -> object:
    report = _check(_TYPED.format(customer=customer, contractor=contractor), None)
    return report.summary.get("contractor_type")


class TestTheContractorsStatus:
    def test_a_status_denied_is_no_status(self) -> None:
        assert _type_of(contractor=", დღგ-ს გადამხდელი არ არის") is None
        assert _type_of(contractor=", მცირე მეწარმის სტატუსის გარეშე") is None

    def test_the_customers_status_is_not_the_contractors(self) -> None:
        assert _type_of(customer=", დღგ-ს გადამხდელი") is None

    def test_the_contractors_own_status_is_read(self) -> None:
        """Near miss: stated of the contractor, and not denied."""
        assert _type_of(contractor=", დღგ-ს გადამხდელი") == "individual_entrepreneur_vat"
        assert _type_of(contractor=", მცირე მეწარმე") == "small_business"
        assert (
            _type_of(contractor=", მცირე მეწარმის სტატუსით და არ არის დღგ-ს გადამხდელი")
            == "small_business"
        )


# --- R6-09: a services table's money column, and its cells kept apart ---------------------------


class TestAServicesTablesColumns:
    @pytest.mark.parametrize("column", ["ანაზღაურება", "Fee", "საფასური"])
    def test_a_money_column_is_no_part_of_a_service(self, column: str) -> None:
        line = (
            f"1, მომსახურება = ფასადის ესკიზი. 1, რაოდენობა = 2. 1, {column} = 500. "
            f"2, მომსახურება = ფანჯრის ნახაზი. 2, რაოდენობა = 4. 2, {column} = 800"
        )
        items = [scope_part(t) for t in _table_items(line) or []]
        assert [i.numbers for i in items] == [("2",), ("4",)]

    def test_two_figures_in_two_cells_are_two_numbers(self) -> None:
        """Near miss: an unnamed second figure column is no money column, and its figure is
        a number of its own, not thousands of the quantity's."""
        line = "1, მომსახურება = ფასადის ესკიზი. 1, რაოდენობა = 2. 1, ფურცლები = 500"
        [item] = [scope_part(t) for t in _table_items(line) or []]
        assert set(item.numbers) == {"2", "500"}


# --- R6-10: what the plot code label holds --------------------------------------------------------


def _cadastral_notes(act: str) -> list[Finding]:
    return _of(_check(act, None), "AOA.CADASTRAL_PRESENT")


class TestThePlotCode:
    NO_PLOT = ACT.replace(
        "„შემკვეთის“ პროექტ „ნათელი“ (ს/კ: 03.04.05.678) მომსახურების გაწევა.",
        "„შემკვეთის“ პროექტ „ნათელი“ - მომსახურების გაწევა.",
    )

    def test_an_identification_code_in_groups_is_no_plot_code(self) -> None:
        act = self.NO_PLOT.replace("საიდენტიფიკაციო კოდი: 405987612", "ს/კ 405.987.612")
        [note] = _cadastral_notes(act)
        assert note.message_en == "the act does not state the cadastral code"

    def test_a_year_after_a_dashed_plot_code_is_no_part_of_it(self) -> None:
        act = ACT.replace(
            "(ს/კ: 03.04.05.678) მომსახურების გაწევა.",
            "საკადასტრო კოდი 03-04-05-678 2026 წლის აპრილში მომსახურების გაწევა.",
        )
        [note] = _cadastral_notes(act)
        assert (note.quote, note.replacement) == ("03-04-05-678", "03.04.05.678")

    def test_a_plot_code_in_other_groups_is_still_reported(self) -> None:
        """Near miss: a code under the plot label that is no plot code's shape, and no party's
        code either, is still named as written wrong."""
        act = ACT.replace("(ს/კ: 03.04.05.678)", "(ს/კ: 03.4.05.678)")
        [note] = _cadastral_notes(act)
        assert note.quote == "03.4.05.678"


# --- R6-11: an address written with an initial, or with "#" ---------------------------------------


class TestAnAddressWrittenShort:
    REGISTRY = "საქართველო, ქ. ქუთაისი, აკაკი წერეთლის ქუჩა, N 8"

    @pytest.mark.parametrize(
        "act",
        [
            "ქ. ქუთაისი, ა. წერეთლის ქ. 8",
            "ქ. ქუთაისი, აკაკი წერეთლის ქ. #8",
            "ქ. ქუთაისი, ა. წერეთლის ქ. # 8",
        ],
    )
    def test_the_same_address(self, act: str) -> None:
        assert compare_addresses(act, self.REGISTRY) == "same"

    def test_another_initial_is_another_name(self) -> None:
        """Near miss: an initial of another first name is not this street's."""
        assert compare_addresses("ქ. ქუთაისი, გ. წერეთლის ქ. 8", self.REGISTRY) == "different"
