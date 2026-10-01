"""Defects the third red-team round found in reading the parties, their codes and signatories.

Each test names its defect (``samples/redteam/aoa/CONFIRMED_R3.md``): AOA-R3-01 (a code of the
wrong length, or grouped by hyphens), AOA-R3-04 (an individual entrepreneur written out),
AOA-R3-05 (a functional director co-signing), AOA-R3-06 (a patronymic in a signatory's name).
Synthetic text only: the pair is ``test_aoa_check``'s, every name and code invented, and each
widened reading has a near miss beside it.
"""

from __future__ import annotations

import re

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.extraction.anchors import (
    _LEGAL_FORM,
    _legal_form,
    find_anchors,
    find_parties,
    split_patronymic,
)
from procurement_agent.extraction.normalize import (
    ID_CODE_RE,
    ID_CODE_WRITTEN_RE,
    id_code_digits,
    in_grouping,
)
from procurement_agent.extraction.schemas import FactType
from tests.unit.test_aoa_check import ACT, CONTRACT
from tests.unit.test_extraction_redteam_r2 import _extract


def _check(act: str = ACT, contract: str | None = CONTRACT) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract)


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _substantive(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.severity in ("error", "warning")]


_CONTRACTOR_CODE = "საიდენტიფიკაციო კოდი: 987654321"


# --- AOA-R3-01: a code of the wrong length or grouped by hyphens is a code to compare -------------


@pytest.mark.parametrize(
    ("written", "digits"),
    [
        ("9876543211", "9876543211"),  # a digit doubled
        ("98765432", "98765432"),  # a digit dropped
        ("987-654-312", "987654312"),  # grouped by hyphens, a transposition
        ("987.654.321", "987654321"),  # grouped by dots
        ("987 654 312", "987654312"),  # grouped by spaces, as before
    ],
)
def test_a_labelled_code_of_any_plausible_length_or_grouping_is_read(
    written: str, digits: str
) -> None:
    [m] = ID_CODE_WRITTEN_RE.finditer(f"შპს „ბეტა სერვისი“, ს/კ: {written}, მისამართი")
    assert m.group(1) == written
    assert id_code_digits(m.group(1)) == digits


@pytest.mark.parametrize(
    "text",
    [
        "ს/კ: 12345, მისამართი",  # five digits: no code
        "ს/კ: 123456, მისამართი",  # six
        "ს/კ: 12345678901234, მისამართი",  # fourteen: an account's digits, no code
        "ს/კ: 12-34-56, მისამართი",  # a date-like grouping
    ],
)
def test_a_number_no_code_could_be_is_not_read(text: str) -> None:
    assert list(ID_CODE_WRITTEN_RE.finditer(text)) == []
    assert list(ID_CODE_RE.finditer(text)) == []


@pytest.mark.parametrize("pattern", [ID_CODE_RE, ID_CODE_WRITTEN_RE])
def test_a_code_with_a_case_ending_reads_its_digits(pattern: re.Pattern[str]) -> None:
    [m] = pattern.finditer("ს/კ 987654321-ის მფლობელი")
    assert m.group(1) == "987654321"


@pytest.mark.parametrize("pattern", [ID_CODE_RE, ID_CODE_WRITTEN_RE])
def test_grouped_space_code_before_a_year_is_still_the_nine_digits(
    pattern: re.Pattern[str],
) -> None:
    [m] = pattern.finditer("ს/კ: 987 654 321 2026 წლის")
    assert m.group(1) == "987 654 321"


@pytest.mark.parametrize(
    ("written", "digits"),
    [("987-654-321", "987654321"), ("987.654.321", "987654321"), ("12345678901", "12345678901")],
)
def test_a_code_fact_is_read_in_any_grouping(written: str, digits: str) -> None:
    [m] = ID_CODE_RE.finditer(f"შპს „ბეტა სერვისი“, ს/კ: {written}, მისამართი")
    assert id_code_digits(m.group(1)) == digits
    facts = _extract(f"შპს „ბეტა სერვისი“, ს/კ: {written}, მისამართი: ქ. ქუთაისი")
    assert facts.get(FactType.party_id_code) == [digits]


@pytest.mark.parametrize("written", ["9876543211", "98765432", "987-654-3211"])
def test_a_code_of_the_wrong_length_is_no_code_fact(written: str) -> None:
    """The near miss for extraction: a code fact is 9 or 11 digits (GOLD_SPEC 2); only the
    act check reads a wrong length, to say what is wrong with it."""
    assert list(ID_CODE_RE.finditer(f"ს/კ: {written}, მისამართი")) == []
    facts = _extract(f"შპს „ბეტა სერვისი“, ს/კ: {written}, მისამართი: ქ. ქუთაისი")
    assert FactType.party_id_code not in facts


@pytest.mark.parametrize("wrong", ["9876543211", "98765432"])
def test_a_code_of_the_wrong_length_is_an_error_corrected_to_the_nearest_contract_code(
    wrong: str,
) -> None:
    act = ACT.replace(_CONTRACTOR_CODE, f"საიდენტიფიკაციო კოდი: {wrong}")
    [finding] = _of(_check(act), "AOA.ID_CODES")
    assert finding.severity == "error"
    assert (finding.quote, finding.actual, finding.expected) == (wrong, wrong, "987654321")
    # the message says what is wrong with the digits
    assert f"{len(wrong)}" in finding.message_en


def test_a_hyphen_grouped_slip_is_quoted_and_corrected_in_its_grouping() -> None:
    act = ACT.replace(_CONTRACTOR_CODE, "საიდენტიფიკაციო კოდი: 987-654-312")
    [finding] = _of(_check(act), "AOA.ID_CODES")
    assert (finding.quote, finding.actual, finding.expected) == (
        "987-654-312",
        "987654312",
        "987-654-321",
    )


def test_a_correct_hyphen_grouped_code_is_the_contract_code() -> None:
    act = ACT.replace(_CONTRACTOR_CODE, "საიდენტიფიკაციო კოდი: 987-654-321")
    assert _substantive(_check(act)) == []


def test_a_wrong_length_code_near_no_contract_code_has_no_correction() -> None:
    act = ACT.replace(_CONTRACTOR_CODE, "საიდენტიფიკაციო კოდი: 5550001112")
    [finding] = _of(_check(act), "AOA.ID_CODES")
    assert finding.severity == "error"
    assert finding.expected is None


@pytest.mark.parametrize(
    ("code", "written", "grouped"),
    [
        ("987654321", "987-654-312", "987-654-321"),
        ("987654321", "987 654 312", "987 654 321"),
        ("987654321", "987654312", "987654321"),
        ("987654321", "9876-54321", "9876-54321"),
        ("987654321", "987-654-3211", "987-654-321"),  # a length the code cannot keep: threes
    ],
)
def test_a_correction_is_written_in_the_grouping_of_the_code_it_corrects(
    code: str, written: str, grouped: str
) -> None:
    assert in_grouping(code, written) == grouped


# --- AOA-R3-04: the individual entrepreneur written out is ი/მ ------------------------------------


@pytest.mark.parametrize(
    ("written", "short"),
    [
        ("შეზღუდული პასუხისმგებლობის საზოგადოება", "შპს"),
        ("შეზღუდული პასუხისმგებლობის საზოგადოების", "შპს"),
        ("სააქციო საზოგადოება", "სს"),
        ("არასამეწარმეო (არაკომერციული) იურიდიული პირი", "ააიპ"),
        ("საჯარო სამართლის იურიდიული პირი", "სსიპ"),
        ("ინდივიდუალური მეწარმე", "ი/მ"),
        ("ინდივიდუალური მეწარმის", "ი/მ"),
        ("ინდ. მეწარმე", "ი/მ"),
        ("ინდ.მეწარმე", "ი/მ"),
        ("ი / მ", "ი/მ"),
        ("ი/მ", "ი/მ"),
        ("შპს", "შპს"),
    ],
)
def test_every_legal_form_read_is_written_short(written: str, short: str) -> None:
    assert re.fullmatch(_LEGAL_FORM, written)
    assert _legal_form(written) == short


def _entrepreneur(text: str, form: str, name: str = "ანა ბერიძე") -> str:
    """``text`` with the contractor an individual entrepreneur, named with ``form``."""
    return (
        text.replace(
            "შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321",
            f"{form} {name}, პირადი ნომერი: 01001012345",
        )
        .replace("შპს „ბეტა სერვისი“", f"{form} {name}")
        .replace(" წარმოდგენილი დირექტორის დავით მელაძის სახით", "")
        .replace("დირექტორი: დავით მელაძე", name)
    )


@pytest.mark.parametrize(
    ("in_contract", "in_act"),
    [
        ("ი/მ", "ინდივიდუალური მეწარმე"),
        ("ინდივიდუალური მეწარმე", "ი/მ"),
        ("ი/მ", "ინდ. მეწარმე"),
    ],
)
def test_the_individual_entrepreneur_written_out_is_the_same_party(
    in_contract: str, in_act: str
) -> None:
    report = _check(_entrepreneur(ACT, in_act), _entrepreneur(CONTRACT, in_contract))
    assert _of(report, "AOA.PARTY_NAME") == []
    [party] = [p for p in find_parties(_entrepreneur(ACT, in_act)) if "ბერიძე" in (p.name or "")]
    assert party.name == "ი/მ ანა ბერიძე"


def test_another_entrepreneur_written_out_is_still_another_party() -> None:
    """The near miss: the form written out, the person not the contract's."""
    report = _check(
        _entrepreneur(ACT, "ინდივიდუალური მეწარმე", "ნანა ბერიძე"), _entrepreneur(CONTRACT, "ი/მ")
    )
    assert [f.severity for f in _of(report, "AOA.PARTY_NAME")] == ["error"]


# --- AOA-R3-05: a financial, commercial or technical director is not the party's head -------------

_SIGNATURE = "დირექტორი: დავით მელაძე"


@pytest.mark.parametrize(
    "title",
    [
        "ფინანსური დირექტორი",
        "კომერციული დირექტორი",
        "ტექნიკური დირექტორი",
        "საოპერაციო დირექტორი",
        "ფინანსური დირექტორის",
    ],
)
def test_a_functional_director_co_signing_is_not_the_head(title: str) -> None:
    act = ACT.replace(_SIGNATURE, f"{_SIGNATURE}\n{title}: ლევან წულაია")
    assert _of(_check(act), "AOA.DIRECTOR") == []
    [contractor] = [p for p in find_parties(act) if p.name == "შპს ბეტა სერვისი"]
    assert contractor.directors == ["დავით მელაძე"]


@pytest.mark.parametrize("title", ["გენერალური დირექტორი", "აღმასრულებელი დირექტორი"])
def test_a_head_title_with_its_own_qualifier_is_still_the_head(title: str) -> None:
    """The near miss: a second head under a head's title is compared, and is wrong here."""
    act = ACT.replace(_SIGNATURE, f"{_SIGNATURE}\n{title}: ლევან წულაია")
    [finding] = _of(_check(act), "AOA.DIRECTOR")
    assert finding.actual == "ლევან წულაია"


@pytest.mark.parametrize("surname", ["ჩიტაური", "სანტორო"])
def test_a_signatory_whose_surname_ends_as_an_adjective_is_still_read(surname: str) -> None:
    """The near miss for a flattened act: a surname in "-ური" or "სა-...-ო" right before the
    next head's title is a name, not a qualifier of that title."""
    act = (
        ACT.replace("დირექტორი ნინო კაპანაძე", f"დირექტორი ნინო {surname}")
        .replace("დირექტორის ნინო კაპანაძის", f"დირექტორის ნინო {surname}ს")
        .replace("„შემსრულებელი“\nშპს „ბეტა სერვისი“\nდირექტორი: დავით მელაძე", "")
        .replace(
            f"დირექტორი ნინო {surname}\n", f"დირექტორი ნინო {surname}\nდირექტორი: დავით მელაძე\n"
        )
    )
    names = [d for p in find_parties(act) for d in p.directors]
    assert "დავით მელაძე" in names


# --- AOA-R3-06: a patronymic in a signatory's name ------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("დირექტორი: დავით მელაძე", "დირექტორი: დავით გიორგის ძე მელაძე"),
        ("დირექტორი ნინო კაპანაძე", "დირექტორი ნინო ზურაბის ასული კაპანაძე"),
        ("დირექტორის დავით მელაძის სახით", "დირექტორის დავით გიორგის ძე მელაძის სახით"),
    ],
)
def test_a_patronymic_is_no_part_of_the_name_compared(old: str, new: str) -> None:
    act = ACT.replace(old, new) if old in ACT else ACT
    contract = CONTRACT.replace(old, new) if old in CONTRACT else CONTRACT
    assert _of(_check(act, contract), "AOA.DIRECTOR") == []


def test_the_director_with_a_patronymic_is_read_as_first_name_and_surname() -> None:
    act = ACT.replace("დირექტორი: დავით მელაძე", "დირექტორი: დავით გიორგის ძე მელაძე")
    directors = [h for h in find_anchors(act) if h.type is FactType.party_director]
    [hit] = [h for h in directors if "მელაძე" in h.value]
    assert hit.value == "დავით მელაძე"
    # the fact points at the name as the act writes it, not at the start of the text
    assert hit.raw_text == "დავით გიორგის ძე მელაძე"


def test_a_slip_in_a_surname_after_a_patronymic_is_still_an_error() -> None:
    act = ACT.replace("დირექტორი: დავით მელაძე", "დირექტორი: დავით გიორგის ძე მელაძა")
    [finding] = _of(_check(act), "AOA.DIRECTOR")
    assert finding.severity == "error"
    # the whole name as written is quoted, and its correction keeps the patronymic
    assert (finding.quote, finding.actual, finding.expected) == (
        "დავით გიორგის ძე მელაძა",
        "დავით გიორგის ძე მელაძა",
        "დავით გიორგის ძე მელაძე",
    )


def test_a_slip_in_a_genitive_after_a_patronymic_is_corrected_in_the_genitive() -> None:
    act = ACT.replace(
        "დირექტორის ნინო კაპანაძის სახით", "დირექტორის ნინო ზურაბის ასული კაპანაძას სახით"
    )
    [finding] = _of(_check(act), "AOA.DIRECTOR")
    assert (finding.quote, finding.expected) == (
        "ნინო ზურაბის ასული კაპანაძას",
        "ნინო ზურაბის ასული კაპანაძის",
    )


@pytest.mark.parametrize(
    ("name", "split"),
    [
        ("დავით გიორგის ძე მელაძე", ("დავით მელაძე", "გიორგის ძე")),
        ("ნინო ზურაბის ასული კაპანაძე", ("ნინო კაპანაძე", "ზურაბის ასული")),
        ("დავით მელაძე", ("დავით მელაძე", None)),
        # near misses: no ძე/ასული, or not between the two names
        ("დავით მელაძის ხელმოწერა თარიღი", ("დავით მელაძის ხელმოწერა თარიღი", None)),
        ("დავით გიორგი ძე მელაძე", ("დავით გიორგი ძე მელაძე", None)),
    ],
)
def test_a_patronymic_is_split_off_a_name(name: str, split: tuple[str, str | None]) -> None:
    assert split_patronymic(name) == split


def test_a_genitive_before_a_word_that_is_no_patronymic_is_read_as_before() -> None:
    """The near miss: "<name> <genitive> <noun>" with no ძე/ასული reads two words, as it did."""
    act = ACT.replace("დირექტორი: დავით მელაძე", "დირექტორი: დავით მელაძის ხელმოწერა")
    [contractor] = [p for p in find_parties(act) if p.name == "შპს ბეტა სერვისი"]
    assert contractor.directors == ["დავით მელაძის"]
