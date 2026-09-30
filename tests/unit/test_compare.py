"""The comparison engine: what it finds, what it refuses to call a difference, and what it
never reports at all.

Documents here are invented. The client's own files are git-ignored, blocked by a pre-commit
hook and confidential, and nothing in this file is derived from them.

The first two tests are the ones that matter most. A checker that fires on a correct document
is worse than no checker (docs/ACCURACY.md), and the comparison's version of that constraint is
that a document compared with an identical copy must produce nothing at all.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.compare import (
    DEFAULT_MIN_SIMILARITY,
    DocumentTooLargeError,
    compare,
    cosmetic_kind,
    inline_ops,
)
from procurement_agent.ingestion.segmenter import Segment, SegmentKind, build

# A short synthetic contract. Invented company, code, account and people throughout.
BASE = [
    "ხელშეკრულება № SY-2026/004 მიწოდების შესახებ",
    "ქ. ბათუმი, 2026 წლის 4 თებერვალი",
    'შპს "ველოსი ტექნიკა" (ს/კ 401000111), დირექტორი ლევან ხარაზი.',
    "2. ღირებულება. ხელშეკრულების ღირებულება შეადგენს 12 400,00 ლარს დღგ-ს ჩათვლით.",
    "3. მიწოდების ვადა. მიწოდება 20 კალენდარული დღის განმავლობაში.",
    "საბანკო რეკვიზიტები: ანგარიში GE11VL0000000123456789, ბანკის კოდი VELOGE22.",
]


#: Characters this file is about are built from codepoints, never pasted in. A test for
#: invisible characters whose source contains invisible characters is a test nobody can
#: review - and bandit flags exactly that in src/ for the same reason.
ZWSP = chr(0x200B)  # zero-width space
CYRILLIC_O = chr(0x041E)  # renders identically to Latin O
CYRILLIC_A = chr(0x0410)  # renders identically to Latin A


def segs(texts: list[str], kind: SegmentKind = "text") -> list[Segment]:
    return build([(t, kind, f"¶{i + 1}", None, None) for i, t in enumerate(texts)])


def run(left: list[str], right: list[str], **kw: object) -> object:
    return compare(
        segs(left),
        segs(right),
        left_document_id="L",
        right_document_id="R",
        left_kind=kw.pop("left_kind", "docx"),  # type: ignore[arg-type]
        right_kind=kw.pop("right_kind", "docx"),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def rules_of(report: object) -> set[str]:
    return {r for h in report.hunks for r in h.rules}  # type: ignore[attr-defined]


# --- the constraint that matters most ---------------------------------------------------------


def test_a_document_compared_with_itself_has_no_differences() -> None:
    report = run(BASE, BASE)
    assert report.hunks == []
    assert report.findings == []
    assert report.field_deltas == []
    assert report.identical
    assert report.counts.unchanged == len(BASE)


def test_an_identical_copy_is_not_a_difference_either() -> None:
    """Separate lists, same words: nothing about identity may depend on object identity."""
    report = run(BASE, list(BASE))
    assert report.identical
    assert report.similarity == 1.0
    assert report.alignment == "versions"


# --- the values the client named --------------------------------------------------------------


def test_a_changed_amount_is_critical_and_carries_the_difference() -> None:
    right = [p.replace("12 400,00", "15 900,00") for p in BASE]
    report = run(BASE, right)
    delta = next(d for h in report.hunks for d in h.deltas if d.rule_id == "CMP.AMOUNT")
    assert delta.severity == "error"
    assert delta.delta == pytest.approx(3500.0)
    assert delta.delta_pct == pytest.approx(28.23, abs=0.01)
    assert report.counts.critical == 1


def test_a_changed_date_reports_how_many_days_it_moved() -> None:
    right = [p.replace("4 თებერვალი", "19 თებერვალი") for p in BASE]
    delta = next(d for h in run(BASE, right).hunks for d in h.deltas if d.rule_id == "CMP.DATE")
    assert delta.days == 15


def test_a_changed_bank_code_is_critical_and_says_the_new_one_is_malformed() -> None:
    """The seven-character code is the shape of the error the client planted in their own set."""
    right = [p.replace("VELOGE22", "VELGE22") for p in BASE]
    delta = next(d for h in run(BASE, right).hunks for d in h.deltas if d.rule_id == "CMP.BANK")
    assert delta.severity == "error"
    assert delta.note_en is not None and "BIC" in delta.note_en


def test_a_changed_account_number_is_reported_even_though_neither_validates() -> None:
    """Anonymised accounts fail their checksum on both sides; that must not hide the change."""
    right = [p.replace("GE11VL0000000123456789", "GE11VL0000000999999999") for p in BASE]
    assert "CMP.BANK" in rules_of(run(BASE, right))


def test_an_amount_written_out_in_georgian_words_is_compared_too() -> None:
    """A revision that edits only the words leaves every digit untouched, so the amount rules
    see nothing. On the client's own set that is an entire planted error."""
    left = ["ღირებულება 100 (ასი) ლარი."]
    right = ["ღირებულება 100 (ორასი) ლარი."]
    report = run(left, right)
    delta = next(d for h in report.hunks for d in h.deltas if d.rule_id == "CMP.AMOUNT_WORDS")
    assert delta.severity == "error"
    assert delta.delta == pytest.approx(100.0)


def test_a_bare_number_in_a_table_cell_is_a_value_change_not_a_text_change() -> None:
    """A table keeps the currency in a neighbouring cell, so the amount rules cannot see it."""
    report = compare(
        segs(["48 750,00"], kind="table_cell"),
        segs(["52 100,00"], kind="table_cell"),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    delta = next(d for h in report.hunks for d in h.deltas)
    assert delta.rule_id == "CMP.AMOUNT"
    assert delta.severity == "error"
    assert delta.delta == pytest.approx(3350.0)


def test_a_name_that_differs_only_by_inflection_is_not_a_difference() -> None:
    """Georgian is heavily inflected; ``normalize.names_match`` is the project's answer."""
    left = ["დირექტორი ლევან ხარაზი ხელს აწერს."]
    right = ["დირექტორი ლევან ხარაზის სახელით."]
    report = run(left, right)
    assert not [d for h in report.hunks for d in h.deltas if d.rule_id == "CMP.PARTY"]


# --- added, removed, moved ----------------------------------------------------------------------


def test_an_added_paragraph_is_reported_as_added() -> None:
    report = run(BASE, [*BASE[:3], "7. კონფიდენციალურობა.", *BASE[3:]])
    assert report.counts.added == 1
    assert report.counts.removed == 0
    assert "CMP.TEXT_ADDED" in rules_of(report)


def test_a_removed_paragraph_is_reported_as_removed() -> None:
    report = run(BASE, [p for p in BASE if not p.startswith("3.")])
    assert report.counts.removed == 1
    assert report.counts.added == 0
    assert "CMP.TEXT_REMOVED" in rules_of(report)


def test_a_relocated_paragraph_is_a_move_not_a_deletion_plus_an_insertion() -> None:
    moved = BASE[4]
    report = run(BASE, [BASE[0], moved, *BASE[1:4], BASE[5]])
    assert report.counts.moved == 1
    assert report.counts.added == 0
    assert report.counts.removed == 0
    hunk = next(h for h in report.hunks if h.kind == "moved")
    assert hunk.moved_from and hunk.moved_to and hunk.moved_from != hunk.moved_to


def test_a_short_or_numeric_cell_is_never_called_moved() -> None:
    """A workbook is full of cells reading 0.00; pairing them up would invent hundreds of moves.

    The displaced cell is still reported - as a removal and an addition, which is what it looks
    like without a claim nobody can support.
    """
    stable = [
        "alpha stable line number one",
        "beta stable line number two",
        "gamma stable line number three",
    ]
    report = run([stable[0], "0.00", *stable[1:]], [*stable, "0.00"])
    assert report.counts.moved == 0
    assert report.counts.added == 1
    assert report.counts.removed == 1


# --- differences that are not differences -------------------------------------------------------


def test_quotes_dashes_and_spacing_alone_are_formatting() -> None:
    right = [p.replace('"', "„").replace("-", "–").replace(". ", ".  ") for p in BASE]
    report = run(BASE, right)
    assert report.counts.critical == 0
    assert report.errors == []
    assert rules_of(report) <= {"CMP.FORMATTING"}


def test_the_same_number_written_differently_is_formatting() -> None:
    report = run(["სულ 1 000,00 ლარი"], ["სულ 1000.00 ლარი"])
    assert report.counts.cosmetic == 1
    assert report.errors == []


def test_typography_can_never_manufacture_a_field_difference() -> None:
    """The document-wide pass folds its text exactly as the hunks do.

    A revision replacing every hyphen with an en dash once reported the contract number
    changing from "SY-2026/004" to "SY", because the number pattern stopped matching at the
    dash while the line itself was correctly called formatting-only.
    """
    right = [p.replace("-", "–") for p in BASE]
    assert run(BASE, right).field_deltas == []


# --- characters that do not look like differences ------------------------------------------------


def test_a_cyrillic_lookalike_inside_a_bank_code_is_reported() -> None:
    right = [p.replace("VELOGE22", f"VEL{CYRILLIC_O}GE22") for p in BASE]
    report = run(BASE, right)
    assert "CMP.HOMOGLYPH" in rules_of(report)
    finding = next(f for f in report.findings if f.rule_id == "CMP.HOMOGLYPH")
    assert finding.severity == "warning"


def test_a_lookalike_on_scanned_input_is_information_not_a_warning() -> None:
    """normalize.py documents Tesseract as a source of these. On a scan it is OCR, not tampering."""
    right = [p.replace("VELOGE22", f"VEL{CYRILLIC_O}GE22") for p in BASE]
    report = run(BASE, right, left_kind="pdf", right_kind="pdf")
    finding = next(f for f in report.findings if f.rule_id == "CMP.HOMOGLYPH")
    assert finding.severity == "info"


def test_a_lookalike_is_still_reported_when_it_hides_behind_an_ordinary_edit() -> None:
    """The case a character substitution would actually be used in.

    ``cosmetic_kind`` only speaks when a change is *nothing but* invisible, and ``hits_of``
    folds confusables away before comparing values — so a Cyrillic letter swapped into a bank
    code in the same paragraph as a plausible wording change was once reported as neither a
    banking difference nor a substitution, only as "text changed".
    """
    left = ["ბანკის კოდი VELOGE22, ანგარიში გახსნილია თბილისში."]
    right = [f"ბანკის კოდი VEL{CYRILLIC_O}GE22, ანგარიში გახსნილია ბათუმში."]
    report = run(left, right)

    assert "CMP.HOMOGLYPH" in rules_of(report)
    assert any(f.rule_id == "CMP.HOMOGLYPH" for f in report.findings)
    # and the ordinary edit is still reported alongside it, not swallowed by it
    assert "CMP.TEXT_CHANGED" in rules_of(report)


def test_a_zero_width_character_is_still_reported_beside_an_ordinary_edit() -> None:
    left = ["ანგარიში GE11VL0000000123456789 გახსნილია თბილისში."]
    right = [f"ანგარიში GE11VL00000{ZWSP}00123456789 გახსნილია ბათუმში."]
    assert "CMP.INVISIBLE_CHARS" in rules_of(run(left, right))


def test_zero_width_characters_are_reported_on_their_own() -> None:
    report = run(["ანგარიში GE11VL0000000123456789"], [f"ანგარიში GE11VL00000{ZWSP}00123456789"])
    assert "CMP.INVISIBLE_CHARS" in rules_of(report)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("a b", "a b", "identical"),
        ("a  b", "a b", "formatting"),
        ("„a“", '"a"', "formatting"),
        ("ABC", f"{CYRILLIC_A}BC", "homoglyph"),
        ("ab", f"a{ZWSP}b", "invisible"),
        ("a b", "a c", None),
    ],
)
def test_cosmetic_kind_names_the_shape_of_a_difference(
    left: str, right: str, expected: str | None
) -> None:
    assert cosmetic_kind(left, right) == expected


# --- alignment ------------------------------------------------------------------------------------


def test_two_unrelated_documents_are_not_treated_as_two_versions() -> None:
    """Measured on the client's set: revisions align at 0.81-0.98, an act against its own
    contract draft at 0.08. The line diff is noise below the threshold and says so."""
    other = [
        "მინდობილობა",
        "ქ. ქუთაისი, 2026 წლის 9 ივნისი",
        "წინამდებარე მინდობილობით უფლებამოსილება ენიჭება წარმომადგენელს.",
    ]
    report = run(BASE, other)
    assert report.alignment == "unrelated"
    assert report.similarity < DEFAULT_MIN_SIMILARITY
    assert any(s.rule_id == "CMP.LINE_DIFF" for s in report.skipped)


def test_a_rewritten_block_pairs_its_lines_rather_than_reporting_one_blob() -> None:
    left = ["alpha one two three", "beta four five six", "gamma seven eight"]
    right = ["alpha one two THREE", "gamma seven eight nine"]
    report = run(left, right)
    assert [h.kind for h in report.hunks].count("changed") >= 1
    assert any(h.kind == "removed" for h in report.hunks)


def test_a_value_that_only_moved_is_still_found_by_the_field_comparison() -> None:
    """The positional diff sees a deletion and an insertion; the field pass sees one account."""
    bank = "საბანკო რეკვიზიტები: ანგარიში GE11VL0000000123456789."
    other = "სხვა პუნქტი აქ არის."
    left = [bank, other]
    right = [other, bank]
    report = run(left, right)
    assert not [d for d in report.field_deltas if d.rule_id == "CMP.BANK"]


# --- word-level highlighting ----------------------------------------------------------------------


def test_the_inline_diff_marks_words_not_whole_lines() -> None:
    ops = inline_ops("მიწოდება 20 კალენდარული დღე", "მიწოდება 45 კალენდარული დღე")
    changed = [o for o in ops if o.tag != "equal"]
    assert len(changed) == 1
    assert changed[0].left == "20"
    assert changed[0].right == "45"


def test_the_inline_diff_handles_georgian_words_as_words() -> None:
    ops = inline_ops("საგარანტიო ვადა", "საგარანტიო პერიოდი")
    assert any(o.left == "ვადა" and o.right == "პერიოდი" for o in ops if o.tag != "equal")


# --- limits and honesty -------------------------------------------------------------------------


def test_a_document_past_the_segment_limit_is_refused_rather_than_truncated() -> None:
    with pytest.raises(DocumentTooLargeError):
        compare(
            segs([f"line {i}" for i in range(50)]),
            segs(["line 0"]),
            left_document_id="L",
            right_document_id="R",
            max_segments=10,
        )


def test_critical_findings_are_never_truncated_however_many_minor_ones_there_are() -> None:
    """The cap exists so a thousand changed lines cannot bury the one that matters."""
    left = ["ღირებულება 12 400,00 ლარი."]
    right = ["ღირებულება 15 900,00 ლარი."]
    for i in range(200):
        left += [f"უცვლელი ღუზა {i}", f"სტრიქონი {i} ტექსტით"]
        right += [f"უცვლელი ღუზა {i}", f"სტრიქონი {i} სხვა ტექსტით"]
    report = run(left, right)

    assert {f.rule_id for f in report.errors} == {"CMP.AMOUNT"}
    assert any(s.rule_id == "CMP.MINOR" for s in report.skipped)
    assert len(report.findings) < 100


def test_a_wholesale_replacement_is_not_dressed_up_as_line_edits() -> None:
    """Past the pairing budget a block whose lines have nothing in common is reported as a
    removal and an addition.

    Scoring every left line against every right line is quadratic, so such a block is paired
    in order, and only lines alike enough pair (red team R3-05: a word inserted into each of
    many clauses is still a set of line edits). Lines that pair with nothing are a replacement.
    The document-wide field pass is what still reports the amount as *changed*, which is
    exactly why that pass does not depend on alignment.
    """
    left = [f"სტრიქონი {i} ძველი ტექსტით და ღირებულება 12 400,00 ლარი" for i in range(60)]
    right = [
        f"ყოველი შემდეგი აბზაცი #{i * 7}: მხარეთა წერილობითი შეთანხმებით, დამატებითი "
        "პირობების გათვალისწინებით, საბოლოო ღირებულება 15 900,00 ლარი"
        for i in range(60)
    ]
    report = run(left, right)

    assert report.counts.changed == 0
    assert report.counts.added == 60
    assert report.counts.removed == 60
    changed = [d for d in report.field_deltas if d.kind == "changed"]
    assert [d.rule_id for d in changed] == ["CMP.AMOUNT"]


def test_the_rows_cover_every_segment_so_nothing_is_silently_dropped() -> None:
    report = run(BASE, [*BASE[:3], "ახალი პუნქტი", *BASE[4:]])
    left_seen = {r.left.index for r in report.rows if r.left}
    right_seen = {r.right.index for r in report.rows if r.right}
    assert len(left_seen) == report.left_segments
    assert len(right_seen) == report.right_segments
