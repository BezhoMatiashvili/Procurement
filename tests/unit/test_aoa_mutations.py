"""Mistakes the AOA mutation harness (scripts/mutate_aoa.py) planted and the check missed or
reported wrongly. Each test plants the same kind of mistake in the synthetic act. Synthetic text
only."""

from __future__ import annotations

from pathlib import Path

from docx import Document

from procurement_agent.checks.amounts import digits_and_words
from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import Finding
from procurement_agent.eval.score import comment_anchors
from procurement_agent.extraction.georgian import same_word
from procurement_agent.report.docx_annotate import annotate_docx
from tests.unit.test_aoa_check import ACT, CONTRACT

#: Cyrillic letters that look like Latin ones, written by code point so the source stays plain
CYRILLIC_ES = chr(0x0441)
CYRILLIC_O = chr(0x043E)


def _of(rule: str, act: str, contract: str = CONTRACT) -> list[Finding]:
    report = check_aoa(aoa_text=act, contract_text=contract)
    return [f for f in report.findings if f.rule_id == rule]


def _docx(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "act.docx"
    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    doc.save(path)
    return path


def _anchored_texts(path: Path) -> list[str]:
    """The text each comment's range covers, in document order."""
    return list(comment_anchors(path).values())


# --- a director misspelled in every form the act writes ------------------------------------------


def test_a_misspelled_director_is_reported_in_each_form_the_act_writes() -> None:
    """The preamble has the genitive, the signature block the nominative: both are the mistake,
    and each needs its own correction, in its own case."""
    act = ACT.replace("ნინო კაპანაძის", "ნინო კაპანიძის").replace("ნინო კაპანაძე", "ნინო კაპანიძე")
    found = {(f.actual, f.expected) for f in _of("AOA.DIRECTOR", act)}
    assert found == {
        ("ნინო კაპანიძის", "ნინო კაპანაძის"),
        ("ნინო კაპანიძე", "ნინო კაპანაძე"),
    }


def test_every_place_of_every_form_gets_a_comment(tmp_path: Path) -> None:
    act = ACT.replace("ნინო კაპანაძის", "ნინო კაპანიძის").replace("ნინო კაპანაძე", "ნინო კაპანიძე")
    act += "ხელმომწერი: ნინო კაპანიძე\n"
    findings = _of("AOA.DIRECTOR", act)
    out, unanchored = annotate_docx(_docx(tmp_path, act), findings, tmp_path / "o.docx")
    assert unanchored == []
    anchored = _anchored_texts(out)
    assert sum("კაპანიძე" in a for a in anchored) == 2
    assert sum("კაპანიძის" in a for a in anchored) == 1


def test_a_director_misspelled_in_one_place_only_is_still_reported() -> None:
    act = ACT.replace("დირექტორი ნინო კაპანაძე", "დირექტორი ნინო კაპანიძე")
    found = {(f.actual, f.expected) for f in _of("AOA.DIRECTOR", act)}
    assert found == {("ნინო კაპანიძე", "ნინო კაპანაძე")}


# --- a Cyrillic letter inside a Georgian word ----------------------------------------------------


def test_a_cyrillic_letter_is_quoted_as_the_act_writes_it() -> None:
    """The check reads a text with look-alike letters folded to Latin; the finding must still
    carry the word as written, or neither the reader nor the Word comment can find it."""
    word = "მომ" + CYRILLIC_ES + "ახურების"
    act = ACT.replace("01.02.03.456) მომსახურების", f"01.02.03.456) {word}")
    [finding] = _of("AOA.SPELLING", act)
    assert finding.actual == word
    assert finding.quote == word
    assert finding.expected == "მომსახურების"
    assert "Cyrillic" in finding.message_en


def test_a_word_with_a_cyrillic_letter_gets_its_comment(tmp_path: Path) -> None:
    word = "ხელშეკ" + CYRILLIC_O + "რულება"
    act = ACT + f"{word} დასრულდა.\n"
    [finding] = _of("AOA.SPELLING", act)
    out, unanchored = annotate_docx(_docx(tmp_path, act), [finding], tmp_path / "o.docx")
    assert unanchored == []
    assert _anchored_texts(out) == [word]


def test_a_latin_look_alike_in_the_finding_still_finds_the_cyrillic_word(tmp_path: Path) -> None:
    """Any finding computed over the folded text anchors on the letters the document has."""
    word = "ხელშეკ" + CYRILLIC_O + "რულება"
    finding = Finding(
        rule_id="AOA.SPELLING",
        severity="warning",
        message_ka="შეცდომა",
        message_en="misspelled",
        actual="ხელშეკoრულება",
    )
    out, unanchored = annotate_docx(
        _docx(tmp_path, f"{word} დასრულდა."), [finding], tmp_path / "o.docx"
    )
    assert unanchored == []
    assert _anchored_texts(out) == [word]


# --- the amount in words -------------------------------------------------------------------------


def test_a_letter_dropped_from_a_short_number_word_is_a_misspelling() -> None:
    [finding] = _of("AOA.AMOUNT_WORDS", ACT.replace("100 (ასი)", "100 (აი)"))
    assert (finding.actual, finding.expected, finding.severity) == ("აი", "ასი", "warning")


def test_a_two_letter_word_in_brackets_is_not_taken_for_a_number() -> None:
    """ "და" is a word, not a slip of a number: the bracket is no amount in words."""
    [pair] = digits_and_words("მუხლი 5 (და სხვა)")
    assert not pair.numeral


def test_tetri_and_the_currency_inside_the_brackets() -> None:
    act = ACT.replace("100 (ასი) ლარს", "100.00 (ასი ლარი და 00 თეთრი)")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert [f.rule_id for f in report.findings if f.severity in ("error", "warning")] == []


def test_tetri_in_words_are_read() -> None:
    [pair] = digits_and_words("შეადგენს 2 400.50 (ორი ათას ოთხასი ლარი და ორმოცდაათი თეთრი)")
    assert pair.words == "ორი ათას ოთხასი"
    assert pair.spelled == 2400.5


def test_tetri_after_the_currency_without_a_conjunction() -> None:
    [pair] = digits_and_words("შეადგენს 100.05 (ასი ლარი ხუთი თეთრი)")
    assert pair.spelled == 100.05


def test_a_wrong_tetri_amount_is_not_read_as_right() -> None:
    [pair] = digits_and_words("შეადგენს 100.50 (ასი ლარი და ოცი თეთრი)")
    assert pair.spelled == 100.2


# --- the project name ----------------------------------------------------------------------------

_PROJECT_CONTRACT = CONTRACT.replace("„მზე“ პროექტისთვის", "„ზღვის რეზიდენსი“ პროექტისთვის")


def _project_act(name: str) -> str:
    return ACT.replace("პროექტ „მზე“", f"პროექტ „{name}“")


def test_the_project_in_another_case_is_the_project() -> None:
    assert _of("AOA.PROJECT_NAME", _project_act("ზღვის რეზიდენსისთვის"), _PROJECT_CONTRACT) == []


def test_a_letter_dropped_from_the_project_name_is_reported() -> None:
    [finding] = _of("AOA.PROJECT_NAME", _project_act("ზღვის რეზდენსისთვის"), _PROJECT_CONTRACT)
    assert finding.actual == "ზღვის რეზდენსისთვის"
    assert finding.expected == "ზღვის რეზიდენსისთვის"


def test_same_word_allows_case_endings_but_not_a_missing_letter() -> None:
    assert same_word("რეზიდენსისთვის", "რეზიდენსი")
    assert same_word("ტოგონიძის", "ტოგონიძე")
    assert same_word("ლომიას", "ლომია")
    assert same_word("ლაზურმა", "ლაზური")
    assert same_word("რეზიდენსისათვის", "რეზიდენსი")
    assert not same_word("ლაზუი", "ლაზური")
    assert not same_word("რეზდენსი", "რეზიდენსი")


# --- the fee in words ----------------------------------------------------------------------------


def test_the_fee_in_words_is_checked_against_the_contract_too() -> None:
    """The figure is the contract's price, the words state another: the act states two fees and
    the one in words is wrong. Both rules say so, on the words, with the words to write."""
    act = ACT.replace("100 (ასი)", "100 (ორასი)")
    fee = [(f.actual, f.expected) for f in _of("AOA.FEE", act)]
    words = [(f.actual, f.expected) for f in _of("AOA.AMOUNT_WORDS", act)]
    assert fee == [("ორასი", "ასი")]
    assert words == [("ორასი", "ასი")]


def test_a_misspelled_fee_in_words_is_not_a_second_fee() -> None:
    assert _of("AOA.FEE", ACT.replace("100 (ასი)", "100 (ასოი)")) == []


# --- a stray letter is one finding, not two ------------------------------------------------------


def test_a_stray_letter_in_a_service_line_does_not_make_it_another_service() -> None:
    from procurement_agent.checks.aoa_scope import scope_part, share

    meant = scope_part("სერვისის გვერდის აღწერა")
    assert share(scope_part("სერვისის გkვერდის აღწერა"), meant) == 1.0
    assert share(scope_part("სერვისის გ" + CYRILLIC_O + "ვერდის აღწერა"), meant) == 1.0
