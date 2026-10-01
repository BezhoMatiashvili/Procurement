"""Red team round 5: an act's date and service month, a contract's windows, and the dates a
comparison names.

AOA-R5-01, AOA-R5-06, AOA-R5-07, AOA-R5-11, COMPARE-R5-10, COMPARE-R5-11, EXTRACTION-R5-01,
EXTRACTION-R5-02, EXTRACTION-R5-03 and EXTRACTION-R5-13. Synthetic text only: every name, code,
account and date here is invented. Each widened reading has a near miss beside it.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.aoa import _month_near, check_aoa, contract_named_by
from procurement_agent.checks.compare import compare
from procurement_agent.checks.contract_terms import read_term
from procurement_agent.checks.document_facts import document_facts, is_act
from procurement_agent.eval.runner import compare_label
from procurement_agent.extraction.rules import find_dates, run_rules
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion.segmenter import Segment, build
from tests.unit.test_aoa_check import ACT, CONTRACT

_PERIOD = (
    "2. შესრულების თარიღი\nმომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით."
)
_SIGNED = "4. გაფორმების თარიღი\nაქტი გაფორმდა 2026 წლის 30 აპრილი"
_TITLE = "მიღება-ჩაბარების აქტი\n"


def _noisy(report) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    return [(f.rule_id, f.severity) for f in report.findings if f.severity in ("error", "warning")]


def _facts(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in document_facts(text):
        out.setdefault(f.type.value, []).append(f.normalized)
    return out


def _date_noise(report) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    return [n for n in _noisy(report) if n[0].startswith("AOA.ACT_DATE")]


def _act_date_findings(report) -> list[tuple[str, str | None, str | None]]:  # type: ignore[no-untyped-def]
    return [
        (f.severity, f.actual, f.expected) for f in report.findings if f.rule_id == "AOA.ACT_DATE"
    ]


# --- AOA-R5-01: a date that names the contract is never the act's, whoever reads it -------------


def _dated_by(line: str) -> str:
    """The test act with its date line replaced by ``line``."""
    assert _SIGNED in ACT
    return ACT.replace(_SIGNED, line)


def _headed(heading_date: str, line: str) -> str:
    """The test act dated only in its heading, with ``line`` under the heading."""
    act = ACT.replace(_SIGNED + "\n", "")
    return act.replace(
        _TITLE, f"{_TITLE}ქ. თბილისი                      {heading_date}\n{line}\n", 1
    )


_VERB_THEN_CONTRACT = [
    "წინამდებარე აქტი შედგენილია 2026 წლის 1 მარტის ხელშეკრულების საფუძველზე.",
    "წინამდებარე აქტი შედგენილია 2026 წლის 1 მარტის N 9/2026 ხელშეკრულების საფუძველზე.",
    "წინამდებარე აქტი გაფორმდა 01.03.2026-ის ხელშეკრულების შესაბამისად.",
    "წინამდებარე აქტი შედგა 2026 წლის 1 მარტს გაფორმებული ხელშეკრულების საფუძველზე.",
]


@pytest.mark.parametrize("line", _VERB_THEN_CONTRACT)
def test_the_acts_verb_before_the_contracts_date_leaves_the_heading_date(line: str) -> None:
    report = check_aoa(aoa_text=_headed("30.04.2026", line), contract_text=CONTRACT)
    assert report.summary.get("act_date") == "2026-04-30"
    assert _noisy(report) == []
    late = check_aoa(aoa_text=_headed("05.05.2026", line), contract_text=CONTRACT)
    assert _act_date_findings(late) == [("error", "05.05.2026", "30.04.2026")]


def test_the_acts_verb_takes_the_next_date_after_the_contracts() -> None:
    line = "4. აქტი შედგა 2026 წლის 1 მარტის ხელშეკრულების საფუძველზე, 2026 წლის 30 აპრილს."
    report = check_aoa(aoa_text=_dated_by(line), contract_text=CONTRACT)
    assert report.summary.get("act_date") == "2026-04-30"
    assert _noisy(report) == []
    late = check_aoa(
        aoa_text=_dated_by(line.replace("30 აპრილს", "5 მაისს")), contract_text=CONTRACT
    )
    assert _act_date_findings(late) == [("error", "2026 წლის 5 მაისს", "2026 წლის 30 აპრილს")]


def test_the_extraction_reads_the_contracts_date_after_the_acts_verb() -> None:
    facts = _facts(
        _headed("30.04.2026", "წინამდებარე აქტი გაფორმდა 01.03.2026-ის ხელშეკრულების შესაბამისად.")
    )
    assert facts["act_date"] == ["2026-04-30"]
    assert facts["contract_date"] == ["2026-03-01"]


@pytest.mark.parametrize("ending", ["-ის", "-ისა", "-ით"])
def test_a_numeric_dates_case_ending_before_the_contract_names_it(ending: str) -> None:
    text = f"აქტი გაფორმდა 15.05.2026{ending} ხელშეკრულების შესაბამისად."
    [hit] = find_dates(text)
    assert contract_named_by(text, hit) is not None


@pytest.mark.parametrize(
    "text",
    [
        # a period's end, its first day, or a deadline: the contract word after them names none
        "მომსახურება გაიწია 01.03.2026-დან ხელშეკრულების პირობებით.",
        "მომსახურება გაიწია 31.03.2026-მდე ხელშეკრულების პირობებით.",
        "მომსახურება გაიწია 30.03.2026-ის ჩათვლით, ხელშეკრულების პირობების შესაბამისად.",
        "მომსახურება გაიწია 30.03.2026-ში, ხელშეკრულების პირობების შესაბამისად.",
    ],
)
def test_a_numeric_dates_other_endings_name_no_contract(text: str) -> None:
    [hit] = find_dates(text)
    assert contract_named_by(text, hit) is None


# --- AOA-R5-07: a service month after its label, the year after the month's noun -----------------

_MONTH_LINES = [
    "2. საანგარიშო თვე: 2026 წლის აპრილი.",
    "2. საანგარიშო თვე: აპრილი, 2026 წ.",
    "2. „მომსახურება“ შესრულდა აპრილის თვეში, 2026 წ.",
    "2. „მომსახურება“ შესრულდა აპრილის თვის განმავლობაში, 2026 წელი.",
]


@pytest.mark.parametrize("line", _MONTH_LINES)
def test_a_service_month_is_read_in_every_wording(line: str) -> None:
    assert _PERIOD in ACT
    act = ACT.replace(_PERIOD, line)
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary.get("execution_period") == "2026-04-01..2026-04-30"
    assert _noisy(report) == []
    late = ACT.replace(_PERIOD, line).replace("2026 წლის 30 აპრილი", "2026 წლის 31 მაისი")
    report = check_aoa(aoa_text=late, contract_text=CONTRACT)
    assert _act_date_findings(report) == [("error", "2026 წლის 31 მაისი", "2026 წლის 30 აპრილი")]


def test_a_year_that_opens_a_date_is_not_the_months() -> None:
    """ "აპრილის თვეში, 2026 წლის 20 მაისამდე": the year is the date's, and the month alone states
    no period."""
    text = "მომსახურება შესრულდა აპრილის თვეში, 2026 წლის 20 მაისამდე."
    assert _month_near(text, ("შესრულდა",)) == []


def test_a_month_label_of_another_kind_states_no_period() -> None:
    text = "საგარანტიო თვე: 2026 წლის აპრილი."
    assert _month_near(text, ("შესრულდა", "საანგარიშო თვე")) == []


# --- AOA-R5-11: an act not written in Georgian is never a finding for what is Georgian ----------

_EN_PARTIES = (
    'Copper Hill LLC, identification code 205118833 (the "Customer"), and Quiet Harbour LLC, '
    'identification code 405229944 (the "Contractor")'
)
_EN_CONTRACT = (
    "SERVICE AGREEMENT\n"
    "Tbilisi                                   2 March 2026\n"
    f"{_EN_PARTIES}, have concluded this Agreement as follows:\n"
    "1. Subject. The subject of this Agreement is cleaning services for the Customer's office "
    '(the "Services").\n'
    "2. Term. The Contractor shall perform the Services from 1 April 2026 to 31 March 2027 "
    "inclusive.\n"
    "3. Price. The monthly fee for the Services is GEL 900.00 including VAT.\n"
)


def _en_act(signed_line: str) -> str:
    return (
        "ACCEPTANCE ACT\n"
        f"{_EN_PARTIES}, state the following:\n"
        "1. Services. The Contractor has rendered the Services under the Agreement.\n"
        "2. Service period. The Services were rendered from 1 April 2026 to 30 April 2026 "
        "inclusive.\n"
        "3. Fee. The fee for the Services is GEL 900.00 including VAT.\n"
        f"{signed_line}\n"
    )


def test_a_correct_english_act_is_clean_and_its_date_is_read() -> None:
    report = check_aoa(
        aoa_text=_en_act("4. Date. This Act was signed on 30 April 2026."),
        contract_text=_EN_CONTRACT,
    )
    assert report.summary.get("act_date") == "2026-04-30"
    assert _date_noise(report) == []


def test_a_late_english_act_is_corrected_in_english() -> None:
    report = check_aoa(
        aoa_text=_en_act("4. Date. This Act was signed on 5 May 2026."), contract_text=_EN_CONTRACT
    )
    assert _act_date_findings(report) == [("error", "5 May 2026", "30 April 2026")]


def test_an_english_act_whose_date_is_not_read_gets_a_skip_note_never_a_finding() -> None:
    report = check_aoa(aoa_text=_en_act("4. Signatures follow."), contract_text=_EN_CONTRACT)
    assert _date_noise(report) == []
    assert "AOA.ACT_DATE" in {s.rule_id for s in report.skipped}


def test_a_georgian_act_with_no_date_still_gets_the_warning() -> None:
    report = check_aoa(aoa_text=ACT.replace(_SIGNED + "\n", ""), contract_text=CONTRACT)
    assert ("AOA.ACT_DATE", "warning") in _noisy(report)


@pytest.mark.parametrize(
    "line",
    [
        # the contract's signing, not the act's
        "4. The Agreement was signed on 2 March 2026.",
        # what the act consists of
        "4. This Act is made in two copies.",
    ],
)
def test_english_words_that_do_not_date_the_act_give_it_no_date(line: str) -> None:
    report = check_aoa(aoa_text=_en_act(line), contract_text=_EN_CONTRACT)
    assert "act_date" not in report.summary


# --- AOA-R5-06 (policy): a performance window stated as a start and a length is closed ----------

_WINDOW = "2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან - 2026 წლის 1 სექტემბრის ჩათვლით."


def _contract_with(window: str) -> str:
    assert _WINDOW in CONTRACT
    return CONTRACT.replace(_WINDOW, window)


@pytest.mark.parametrize(
    ("window", "end"),
    [
        (
            "2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან 6 (ექვსი) თვის განმავლობაში.",
            "2026-09-01",
        ),
        (
            "2.1. „შემსრულებელი“ ვალდებულია გასწიოს „მომსახურება“ 2026 წლის 1 მარტიდან 3 (სამი) "
            "თვის ვადით.",
            "2026-06-01",
        ),
        (
            "2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან.\n2.2. მომსახურების გაწევის ვადა: 6 "
            "(ექვსი) თვე.",
            "2026-09-01",
        ),
        (
            "2.1. The Contractor shall perform the Services from 1 March 2026 for 6 months.",
            "2026-09-01",
        ),
    ],
)
def test_a_performance_window_of_a_start_and_a_length_is_closed(window: str, end: str) -> None:
    text = _contract_with(window)
    term = read_term(text)
    assert term.performance is not None
    assert (str(term.performance.start), str(term.performance.end)) == ("2026-03-01", end)
    assert _facts(text)["execution_period"] == [f"2026-03-01/{end}"]


def test_an_act_after_a_window_closed_by_its_length_is_out_of_term() -> None:
    contract = _contract_with(
        "2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან 3 (სამი) თვის განმავლობაში."
    )
    act = ACT.replace(
        "2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის", "2026 წლის 1 ივლისიდან 2026 წლის 31 ივლისის"
    ).replace("2026 წლის 30 აპრილი", "2026 წლის 31 ივლისი")
    report = check_aoa(aoa_text=act, contract_text=contract)
    assert "AOA.PERIOD_IN_TERM" in {f.rule_id for f in report.findings if f.severity == "error"}


@pytest.mark.parametrize(
    "window",
    [
        # a report's deadline in the window's sentence is no length of the window
        "2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან და წარმოადგინოს ანგარიში 10 დღის ვადაში.",
        # counted from another event
        "2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან, მიწოდებიდან 12 თვის განმავლობაში.",
        # a length beside an until-phrase: which ends it the clause does not say
        "2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან 6 თვის განმავლობაში ან ვალდებულებების "
        "სრულ შესრულებამდე.",
    ],
)
def test_a_performance_window_with_no_length_of_its_own_stays_open(window: str) -> None:
    term = read_term(_contract_with(window))
    assert term.performance is not None
    assert (str(term.performance.start), term.performance.end) == ("2026-03-01", None)


def test_a_windows_length_is_typed_as_the_window() -> None:
    """The length is the window's own words: typed as the window, so a comparison does not
    report it apart as an unclassified duration."""
    text = (
        "2.1. „შემსრულებელი“ ვალდებულია გასწიოს მომსახურება 2026 წლის 1 მარტიდან 6 თვის "
        "განმავლობაში."
    )
    assert [(h.normalized, h.type) for h in run_rules(text) if h.unit == "month"] == [
        ("P6M", FactType.execution_period)
    ]
    report = (
        "2.1. გასწიოს მომსახურება 2026 წლის 1 მარტიდან და წარმოადგინოს ანგარიში 10 დღის ვადაში."
    )
    assert all(
        h.type is not FactType.execution_period for h in run_rules(report) if h.unit == "day"
    )


def test_a_length_with_no_first_day_does_not_close_the_window() -> None:
    term = read_term(_contract_with("2.1. მომსახურების გაწევის ვადა: 6 (ექვსი) თვე."))
    assert term.performance is None or term.performance.end is None


# --- EXTRACTION-R5-01: "not later than" a date marks its end, in the genitive with its vowel -----

_PARTIES = (
    "შპს „მთის წყარო“, საიდენტიფიკაციო კოდი: 200100211 (შემდგომში „შემკვეთი“), და\n"
    "შპს „ქვის კიბე“, საიდენტიფიკაციო კოდი: 300200322 (შემდგომში „შემსრულებელი“)\n"
)


def _contract(clauses: str, dated: str = "ქ. თბილისი, 2026 წლის 3 მარტი") -> str:
    return f"ხელშეკრულება N 9\n{dated}\n{_PARTIES}{clauses}\n"


@pytest.mark.parametrize(
    "validity",
    [
        "3.1. ხელშეკრულება ძალაში შედის ხელმოწერისთანავე და მოქმედებს ვალდებულებების სრულ "
        "შესრულებამდე, მაგრამ არაუგვიანეს 2026 წლის 31 დეკემბრისა.",
        "3.1. ხელშეკრულება ძალაში შედის ხელმოწერისთანავე და მოქმედებს არაუგვიანეს 2026 წლის 31 "
        "დეკემბრისა.",
        "3.1. ხელშეკრულება ძალაში შედის ხელმოწერისთანავე და მოქმედებს არაუგვიანეს 31.12.2026-ისა.",
        "3.1. The Contract enters into force upon signing and is valid no later than 31 December "
        "2026.",
    ],
)
def test_a_validity_not_later_than_a_date_ends_on_it(validity: str) -> None:
    assert _facts(_contract(validity))["validity_period"] == ["2026-03-03/2026-12-31"]


def test_a_performance_window_not_later_than_a_date_ends_on_it() -> None:
    text = _contract(
        "2.1. „შემსრულებელი“ ვალდებულია შეასრულოს მონტაჟის სამუშაოები 2026 წლის 1 მარტიდან "
        "არაუგვიანეს 2026 წლის 31 ივლისისა."
    )
    assert _facts(text)["execution_period"] == ["2026-03-01/2026-07-31"]


def test_a_date_the_contract_takes_effect_on_stays_its_first_day() -> None:
    text = _contract("3.1. ხელშეკრულება ძალაში შედის 2026 წლის 10 მარტს და მოქმედებს შესრულებამდე.")
    assert _facts(text)["validity_period"] == ["2026-03-10/.."]


# --- EXTRACTION-R5-02: the validity's "until fulfilled" types no performance date ---------------

_CAPPED = (
    "3.1. ხელშეკრულება ძალაში შედის ხელმოწერისთანავე და მოქმედებს ვალდებულებების სრულ "
    "შესრულებამდე, მაგრამ არაუგვიანეს 2026 წლის 31 დეკემბრამდე."
)


def test_a_date_in_the_validity_clause_is_never_a_performance_date() -> None:
    types = {h.type for h in find_dates(_contract(_CAPPED))}
    assert FactType.execution_period not in types
    assert FactType.validity_period in types
    facts = _facts(_contract(_CAPPED))
    assert facts["validity_period"] == ["2026-03-03/2026-12-31"]
    assert "execution_period" not in facts


@pytest.mark.parametrize(
    "clause",
    [
        "2.1. „შემსრულებელი“ ვალდებულია შეასრულოს სამუშაოები 2026 წლის 31 ივლისამდე.",
        # a performance clause that names the validity too: still a performance clause
        "2.1. „შემსრულებელი“ ვალდებულია შეასრულოს სამუშაოები 2026 წლის 31 ივლისამდე, "
        "ხელშეკრულების მოქმედების ვადაში.",
    ],
)
def test_a_performance_clauses_date_stays_a_performance_date(clause: str) -> None:
    [hit] = [h for h in find_dates(_contract(clause)) if h.normalized == "2026-07-31"]
    assert hit.type is FactType.execution_period


# --- EXTRACTION-R5-03: an act of handover or of works done is an act -----------------------------


def _handover(title: str) -> str:
    return (
        f"{title}\n"
        "ქ. ქუთაისი 2026 წლის 5 მარტი\n"
        "შპს „ალფა“ (შემდგომში „მიმწოდებელი“) და ა(ა)იპ „ბეტა“ (შემდგომში „შემსყიდველი“) "
        "ადგენენ:\n"
        "1. 2026 წლის 12 თებერვლის N 45/2026 ხელშეკრულების საფუძველზე „მიმწოდებელმა“ მიაწოდა "
        "საქონელი 1 180 ლარის ღირებულებით.\n"
    )


@pytest.mark.parametrize(
    "title",
    [
        "მიღება-ჩაბარების აქტი N 3",
        "მიღება-გადაცემის აქტი N 3",
        "საქონლის მიღება-გადაცემის აქტი",
        "მიღება - გადაცემის აქტი",
        "შესრულებული სამუშაოების აქტი N 3",
        "შესრულებული მომსახურების აქტი",
    ],
)
def test_an_act_of_handover_or_of_work_done_is_an_act(title: str) -> None:
    text = _handover(title)
    assert is_act(text)
    facts = _facts(text)
    assert facts["act_date"] == ["2026-03-05"]
    assert facts["contract_date"] == ["2026-02-12"]


@pytest.mark.parametrize(
    "clause",
    [
        "1.1. ანგარიშსწორება მოხდება მიღება-გადაცემის აქტის გაფორმებიდან 10 დღეში.",
        "1.1. ანგარიშსწორება მოხდება შესრულებული სამუშაოების აქტის საფუძველზე.",
        "1.1. მიღება-გადაცემის აქტი ფორმდება მიწოდებისას",
        "მიღება-გადაცემის აქტი ფორმდება მიწოდებისას.",
    ],
)
def test_a_contract_that_names_such_an_act_is_no_act(clause: str) -> None:
    text = f"ხელშეკრულება N 9\nქ. თბილისი, 2026 წლის 3 მარტი\n{clause}\n{_PARTIES}"
    assert not is_act(text)
    assert _facts(text)["contract_date"] == ["2026-03-03"]
    # with no title of its own, the clause on the first line is no act's title either
    assert not is_act(f"{clause}\n{_PARTIES}")


# --- EXTRACTION-R5-13: the English validity clause in its long standard wording -----------------


@pytest.mark.parametrize(
    ("validity", "expected"),
    [
        (
            "3.1. This Contract shall enter into force on the date of its signing and shall "
            "remain in force until 31 December 2026.",
            "2026-03-03/2026-12-31",
        ),
        (
            "3.1. This Contract shall enter into force on the date of its signing and shall "
            "remain in force until the full performance of the Parties' obligations.",
            "2026-03-03/..",
        ),
    ],
)
def test_the_english_validity_clause_is_read(validity: str, expected: str) -> None:
    facts = _facts(_contract(validity))
    assert facts["validity_period"] == [expected]
    assert "execution_period" not in facts


def test_an_english_performance_clause_stays_one() -> None:
    text = _contract(
        "2.1. The Contractor shall perform the Services from 1 April 2026 to 30 June 2026.\n"
        "3.1. This Contract shall enter into force on the date of its signing and shall remain "
        "in force until the full performance of the Parties' obligations."
    )
    facts = _facts(text)
    assert facts["execution_period"] == ["2026-04-01/2026-06-30"]
    assert facts["validity_period"] == ["2026-03-03/.."]


def test_a_guarantee_in_force_until_a_date_is_no_validity() -> None:
    text = _contract("3.1. The bank guarantee shall remain in force until 31 December 2026.")
    assert "validity_period" not in _facts(text)


# --- COMPARE-R5-10 and COMPARE-R5-11: the dates a comparison names -------------------------------

_DOC = [
    "ხელშეკრულება № TS-2026/011",
    "ქ. ზუგდიდი, 2026 წლის 14 აპრილი",
    "შპს „ნისლის ხეობა“ (საიდენტიფიკაციო კოდი: 404555777) (შემდგომში „შემსყიდველი“), და",
    "შპს „ქარის წისქვილი“ (საიდენტიფიკაციო კოდი: 404666888) (შემდგომში „მიმწოდებელი“)",
    "1. მიმწოდებელი ვალდებულია მიაწოდოს შემსყიდველს საოფისე სკამები.",
    "2. ხელშეკრულების საერთო ღირებულება შეადგენს 5 000 (ხუთი ათასი) ლარს.",
    "3. საქონლის მიწოდება განხორციელდება ხელშეკრულების გაფორმებიდან 30 (ოცდაათი) კალენდარული "
    "დღის ვადაში.",
]


def _segs(texts: list[str]) -> list[Segment]:
    return build([(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(texts)])


def _rows(
    left: list[str], right: list[str]
) -> list[tuple[str, str | None, str | None, str | None]]:
    report = compare(
        _segs(left),
        _segs(right),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    return [
        (f.rule_id, compare_label(f, report), f.expected, f.actual)
        for f in report.findings
        if f.severity == "error" and f.rule_id in ("CMP.DATE", "CMP.DURATION")
    ]


def _edit(doc: list[str], old: str, new: str) -> list[str]:
    out = list(doc)
    [i] = [i for i, t in enumerate(doc) if old in t]
    out[i] = out[i].replace(old, new)
    return out


def test_a_preamble_date_filled_in_is_the_contract_date() -> None:
    draft = _edit(_DOC, "ქ. ზუგდიდი, 2026 წლის 14 აპრილი", "ქ. ზუგდიდი, „___“ ____________ 2026 წ.")
    rows = [r for r in _rows(draft, _DOC) if r[0] == "CMP.DATE"]
    assert rows == [("CMP.DATE", "Contract date", None, "2026-04-14")]


def test_a_preamble_date_deleted_is_the_contract_date() -> None:
    undated = _edit(_DOC, "ქ. ზუგდიდი, 2026 წლის 14 აპრილი", "ქ. ზუგდიდი")
    rows = [r for r in _rows(_DOC, undated) if r[0] == "CMP.DATE"]
    assert rows == [("CMP.DATE", "Contract date", "2026-04-14", None)]


def test_a_date_moved_out_of_the_preamble_is_no_contract_date_removed() -> None:
    """The same date on both sides, below the parties on one: one reader states it as the
    contract's date and the other does not, and nothing about the date changed."""
    moved = [_DOC[0], _DOC[2], _DOC[3], _DOC[1], *_DOC[4:]]
    assert all(label != "Contract date" for _, label, _, _ in _rows(_DOC, moved))


def test_an_acts_own_date_against_its_contract_is_no_date_added() -> None:
    """A contract and its act: the act's date is what an act has and a contract has not."""
    act = [
        "მიღება-ჩაბარების აქტი",
        "ქ. ზუგდიდი, 2026 წლის 30 მაისი",
        *_DOC[2:4],
        "1. 2026 წლის 14 აპრილის № TS-2026/011 ხელშეკრულების საფუძველზე მიმწოდებელმა მიაწოდა "
        "საოფისე სკამები.",
        "2. ღირებულება შეადგენს 5 000 (ხუთი ათასი) ლარს.",
    ]
    report = compare(
        _segs(_DOC),
        _segs(act),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    assert all(compare_label(f, report) != "Act date" for f in report.findings)


_STAGED = (
    "3. საქონლის მიწოდება განხორციელდება ეტაპობრივად: პირველი პარტია - არაუგვიანეს 2026 წლის 15 "
    "მაისისა, მეორე პარტია - არაუგვიანეს 2026 წლის 15 ივნისისა."
)


def test_each_batch_of_a_staged_delivery_has_a_delivery_deadline() -> None:
    types = [(h.normalized, h.type) for h in find_dates(_STAGED)]
    assert types == [
        ("2026-05-15", FactType.delivery_deadline),
        ("2026-06-15", FactType.delivery_deadline),
    ]


def test_a_staged_deliverys_second_batch_changed_is_its_delivery_deadline() -> None:
    staged = _edit(_DOC, _DOC[-1], _STAGED)
    second = _edit(staged, "2026 წლის 15 ივნისისა", "2026 წლის 30 ივნისისა")
    assert _rows(staged, second) == [
        ("CMP.DURATION", "Delivery deadline", "2026-06-15", "2026-06-30")
    ]
    first = _edit(staged, "2026 წლის 15 მაისისა", "2026 წლის 30 მაისისა")
    assert _rows(staged, first) == [
        ("CMP.DURATION", "Delivery deadline", "2026-05-15", "2026-05-30")
    ]
    both = _edit(first, "2026 წლის 15 ივნისისა", "2026 წლის 30 ივნისისა")
    assert sorted(_rows(staged, both)) == [
        ("CMP.DURATION", "Delivery deadline", "2026-05-15", "2026-05-30"),
        ("CMP.DURATION", "Delivery deadline", "2026-06-15", "2026-06-30"),
    ]


def test_a_date_after_a_batch_of_another_clause_is_no_delivery_deadline() -> None:
    """A date in an item that names no batch, after a staged delivery: not a deadline."""
    text = (
        "3. საქონლის მიწოდება განხორციელდება ეტაპობრივად: პირველი პარტია - არაუგვიანეს 2026 წლის "
        "15 მაისისა, ხოლო ანგარიშ-ფაქტურა წარმოდგენილი უნდა იყოს 2026 წლის 20 ივნისს."
    )
    [_, last] = find_dates(text)
    assert last.type is not FactType.delivery_deadline
