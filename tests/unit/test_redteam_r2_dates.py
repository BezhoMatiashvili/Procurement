"""Red team round 2: dates, periods and the contract's term, each reproduced with invented text.

AOA-R2-05, AOA-R2-10, EXTRACTION-R2-01, -02, -03 and -15. Synthetic text only: every name, code
and date here is invented.
"""

from __future__ import annotations

from datetime import date

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.contract_terms import sentence_spans
from procurement_agent.checks.document_facts import document_facts
from procurement_agent.extraction.anchors import preamble_end
from procurement_agent.extraction.georgian import render_date_like
from procurement_agent.extraction.rules import find_dates
from tests.unit.test_aoa_check import ACT, CONTRACT
from tests.unit.test_dates_and_periods import _contract_dates

# --- AOA-R2-10: a date's year word is a whole word, and a correction keeps every style --------


@pytest.mark.parametrize(
    ("text", "iso", "raw"),
    [
        ("აქტი გაფორმდა 5 მაისს, 2026 წელს.", "2026-05-05", "5 მაისს, 2026 წელს"),
        ("აქტი გაფორმდა 5 მაისი, 2026 წელი.", "2026-05-05", "5 მაისი, 2026 წელი"),
        ("5 მაისი, 2026 წლისა", "2026-05-05", "5 მაისი, 2026 წლისა"),
        ("5 მაისი, 2026 წ, თბილისი", "2026-05-05", "5 მაისი, 2026 წ"),
        # a year word the reader does not take whole is left out entirely, never cut
        ("5 მაისი 2026 წინასწარ", "2026-05-05", "5 მაისი 2026"),
        ("5 მაისი 2026 და", "2026-05-05", "5 მაისი 2026"),
    ],
)
def test_a_year_word_after_a_date_is_taken_whole_or_not_at_all(
    text: str, iso: str, raw: str
) -> None:
    [hit] = find_dates(text)
    assert (hit.normalized, hit.raw_text) == (iso, raw)


def test_a_two_digit_years_date_includes_its_year_word() -> None:
    [hit] = find_dates("აქტი გაფორმდა 05.07.26 წ.")
    assert (hit.normalized, hit.raw_text) == ("2026-07-05", "05.07.26 წ.")
    [hit] = find_dates("14.03.27 წლის ხელშეკრულება")
    assert hit.raw_text == "14.03.27 წლის"
    # still no date without the word for year
    assert find_dates("ოთახი 05.07.26, სართული 2") == []
    assert find_dates("ოთახი 05.07.26 წინ") == []


# Every form the date reader reads, written back in its own style: the round trip is exact.
_FORMS = [
    "2026 წლის 5 ივლისი",
    "2026 წლის 5 ივლისს",
    "2026 წ. 5 ივლისი",
    "5 ივლისი, 2026 წ.",
    "5 ივლისს, 2026 წელს",
    "5 ივლისი 2026 წელი",
    "„05“ ივლისი, 2026 წ.",
    "2026 წლის 1-ლი ივლისი",
    "05.07.2026",
    "5/7/2026",
    "05.07.26 წ.",
    "05.07.26 წლის",
    "2026-07-05",
    "5 July 2026",
    "5th July 2026",
    "05 Jul 2026",
    "July 5, 2026",
    "Jul 5 2026",
    "5 JULY 2026",
]


@pytest.mark.parametrize("form", _FORMS)
def test_every_date_form_round_trips_through_its_own_style(form: str) -> None:
    [hit] = find_dates(form)
    assert hit.raw_text == form
    assert render_date_like(date.fromisoformat(str(hit.normalized)), hit.raw_text) == form


@pytest.mark.parametrize(
    ("example", "day", "expected"),
    [
        ("5 ივლისს, 2026 წელს", date(2026, 6, 30), "30 ივნისს, 2026 წელს"),
        ("05.07.26 წ.", date(2026, 6, 30), "30.06.26 წ."),
        ("5.7.26 წ.", date(2026, 6, 3), "3.6.26 წ."),
        ("5 July 2026", date(2026, 6, 30), "30 June 2026"),
        ("5th July 2026", date(2026, 6, 1), "1st June 2026"),
        ("5th July 2026", date(2026, 6, 22), "22nd June 2026"),
        ("5th July 2026", date(2026, 6, 13), "13th June 2026"),
        ("July 5, 2026", date(2026, 6, 30), "June 30, 2026"),
        ("5 Jul 2026", date(2026, 9, 30), "30 Sep 2026"),
        ("5 july 2026", date(2026, 6, 30), "30 june 2026"),
        ("2026 წლის 1-ლი ივლისი", date(2026, 6, 30), "2026 წლის 30 ივნისი"),
    ],
)
def test_a_correction_keeps_the_examples_style(example: str, day: date, expected: str) -> None:
    assert render_date_like(day, example) == expected


def test_a_word_that_only_looks_like_a_month_keeps_the_iso_fallback() -> None:
    assert render_date_like(date(2026, 6, 30), "5 Julia 2026") == "2026-06-30"


_SIGNED = "აქტი გაფორმდა 2026 წლის 30 აპრილი"


def _act_date_finding(act_date: str) -> tuple[str | None, str | None]:
    act = ACT.replace(_SIGNED, f"აქტი გაფორმდა {act_date}")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.ACT_DATE"]
    assert finding.severity == "error"
    return finding.actual, finding.expected


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("5 მაისს, 2026 წელს.", "30 აპრილს, 2026 წელს"),
        ("05.05.26 წ.", "30.04.26 წ."),
        ("5 May 2026", "30 April 2026"),
    ],
)
def test_the_act_date_correction_keeps_the_acts_style(written: str, expected: str) -> None:
    actual, correction = _act_date_finding(written)
    assert actual == written.rstrip(".") or actual == written
    assert correction == expected


# --- AOA-R2-05: a day range with the month and year after it -----------------------------------


@pytest.mark.parametrize(
    ("text", "dates", "raws"),
    [
        ("1-15 ივნისი, 2026 წ.", ["2026-06-01", "2026-06-15"], ["1", "15 ივნისი, 2026 წ."]),
        ("01-15.06.2026", ["2026-06-01", "2026-06-15"], ["01", "15.06.2026"]),
        (
            f"1 {chr(0x2013)} 15 ივნისი 2026 წლის",
            ["2026-06-01", "2026-06-15"],
            ["1", "15 ივნისი 2026 წლის"],
        ),
        ("1-15 June 2026", ["2026-06-01", "2026-06-15"], ["1", "15 June 2026"]),
    ],
)
def test_a_day_range_before_a_date_is_the_ranges_first_day(
    text: str, dates: list[str], raws: list[str]
) -> None:
    hits = find_dates(text)
    assert [h.normalized for h in hits] == dates
    assert [h.raw_text for h in hits] == raws


@pytest.mark.parametrize(
    "text",
    [
        "15-06-2026",  # a date written with dashes
        "20-15 ივნისი, 2026 წ.",  # a first day after the last is no range
        "15-15 ივნისი, 2026 წ.",
        # "01.06-15.06.2026" is a range (AOA-R3-02, test_redteam_r3_dates); a day.month
        # after a stop is part of another number
        "5.01.06-15.06.2026",
        "2026-06-15",
        "N 3-15 ივნისი, 2026 წ.",  # a number, not a day
        "ოთახი12-15 ივნისი, 2026 წ.",
        "1.5-15 ივნისი, 2026 წ.",
    ],
)
def test_what_only_looks_like_a_day_range_stays_one_date(text: str) -> None:
    assert len(find_dates(text)) == 1


_PERIOD = "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით."


@pytest.mark.parametrize("period", ["1-15 აპრილი, 2026 წ.", "01-15.04.2026"])
def test_a_day_range_period_is_read_whole_and_a_month_end_act_is_clean(period: str) -> None:
    act = ACT.replace(_PERIOD, f"მომსახურება შესრულდა {period}.")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary["execution_period"] == "2026-04-01..2026-04-15"
    assert [f.rule_id for f in report.findings if f.severity in ("error", "warning")] == []


def test_a_year_slip_in_a_day_range_period_quotes_and_corrects_the_whole_period() -> None:
    act = ACT.replace(_PERIOD, "მომსახურება შესრულდა 1-30 აპრილი, 2025 წ.")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.PERIOD_IN_TERM"]
    assert (finding.actual, finding.expected) == ("1-30 აპრილი, 2025 წ.", "1-30 აპრილი, 2026 წ.")


# --- EXTRACTION-R2-01: a numeric date's genitive before "ჩათვლით" is a range's end -------------

_A = "შპს „ნისლის ველი“"
_B = "შპს „კლდის წვერი“"
_PARTIES_R2 = (
    f"{_A}, საიდენტიფიკაციო კოდი: 200100200 (შემდგომში „შემკვეთი“), და\n"
    f"{_B}, საიდენტიფიკაციო კოდი: 300200300 (შემდგომში „შემსრულებელი“)\n"
)
_PLACE = "ქ. თბილისი, 2027 წლის 3 მარტი\n"


def _contract(*clauses: str, title: str = "მომსახურების ხელშეკრულება N 7") -> str:
    return f"{title}\n{_PLACE}{_PARTIES_R2}" + "\n".join(clauses) + "\n"


def _facts(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in document_facts(text):
        out.setdefault(f.type.value, []).append(f.normalized)
    return out


_IN_FORCE = "3.1. ხელშეკრულება ძალაში შედის ხელმოწერის მომენტიდან და მოქმედებს {}."


@pytest.mark.parametrize(
    "end",
    [
        "31.12.2027-ის ჩათვლით",
        "31.12.2027 - ის ჩათვლით",
        "31.12.2027 წლის ჩათვლით",
        "31.12.2027-მდე",
    ],
)
def test_a_numeric_dates_genitive_before_inclusive_ends_the_validity(end: str) -> None:
    facts = _facts(_contract(_IN_FORCE.format(end)))
    assert facts["validity_period"] == ["2027-03-03/2027-12-31"]


@pytest.mark.parametrize(
    "window",
    [
        "15.03.2027-დან 30.09.2027-ის ჩათვლით",
        "15.03.2027 წლიდან 30.09.2027 წლის ჩათვლით",
        "15.03.2027-დან 30.09.2027-მდე",
    ],
)
def test_a_numeric_dates_genitive_before_inclusive_ends_the_performance_window(
    window: str,
) -> None:
    clause = f"2.1. „შემსრულებელი“ ვალდებულია შეასრულოს მომსახურება {window}."
    assert _facts(_contract(clause))["execution_period"] == ["2027-03-15/2027-09-30"]


def test_a_genitive_before_another_postposition_is_no_end() -> None:
    clause = "3.1. ხელშეკრულება ძალაში შედის 01.04.2027-ის მდგომარეობით."
    assert _facts(_contract(clause))["validity_period"] == ["2027-04-01/.."]


# --- EXTRACTION-R2-03: a performance window stated with the future passive ---------------------


@pytest.mark.parametrize(
    "clause",
    [
        "2.1. მომსახურება შესრულდება 2027 წლის 15 მარტიდან 2027 წლის 30 სექტემბრის ჩათვლით.",
        "2.1. სამუშაოები განხორციელდება 2027 წლის 15 მარტიდან 2027 წლის 30 სექტემბრამდე.",
        "2.1. სამუშაოები ჩატარდება 2027 წლის 15 მარტიდან 2027 წლის 30 სექტემბრამდე.",
        "2.1. სამუშაოები დაიწყება 2027 წლის 15 მარტს და დასრულდება 2027 წლის 30 სექტემბერს.",
        "2.1. The services will be performed from 15 March 2027 until 30 September 2027.",
    ],
)
def test_a_performance_window_in_the_future_passive_is_the_execution_period(clause: str) -> None:
    assert _facts(_contract(clause))["execution_period"] == ["2027-03-15/2027-09-30"]


def test_a_validity_until_the_obligations_are_fulfilled_is_no_performance_window() -> None:
    clause = (
        "3.1. ხელშეკრულება ძალაში შედის ხელმოწერის მომენტიდან და მოქმედებს მანამ, სანამ "
        "მომსახურება სრულად შესრულდება."
    )
    facts = _facts(_contract(clause))
    assert facts["validity_period"] == ["2027-03-03/.."]
    assert "execution_period" not in facts


@pytest.mark.parametrize(
    "clause",
    [
        "5.1. საქონლის მიწოდება განხორციელდება 2027 წლის 30 აპრილამდე.",
        "5.1. გადარიცხვა განხორციელდება 2027 წლის 1 აპრილიდან 2027 წლის 30 აპრილამდე.",
        "5.1. შეხვედრა ჩატარდება 2027 წლის 1 აპრილიდან 2027 წლის 3 აპრილამდე.",
    ],
)
def test_a_future_passive_about_something_other_than_the_work_is_no_window(clause: str) -> None:
    assert "execution_period" not in _facts(_contract(clause))


# --- EXTRACTION-R2-02: parties named in the title do not end the preamble ---------------------

_TITLES = [
    f"მომსახურების ხელშეკრულება N 7 {_A}-სა და {_B}-ს შორის",
    f"ხელშეკრულება N 15/2027 {_A}-სა და {_B}-ს შორის",
    f"{_A}-სა და {_B}-ს შორის გაფორმებული ხელშეკრულება N 7",
    f"{_A}-სა და {_B}-ს შორის დადებული მომსახურების ხელშეკრულება N 7",
]


@pytest.mark.parametrize("title", _TITLES)
def test_parties_named_in_the_title_leave_the_signing_date_in_the_preamble(title: str) -> None:
    text = _contract("3.1. ხელშეკრულება ძალაში შედის ხელმოწერის მომენტიდან.", title=title)
    assert preamble_end(text) == text.index(_A, text.index("\n"))
    facts = _facts(text)
    assert facts["contract_date"] == ["2027-03-03"]
    assert facts["validity_period"] == ["2027-03-03/.."]
    assert _contract_dates(text) == ["2027-03-03"]


def test_a_title_with_parties_flattened_onto_one_line_still_dates_the_contract() -> None:
    text = _contract(
        "3.1. ხელშეკრულება ძალაში შედის ხელმოწერის მომენტიდან.", title=_TITLES[0]
    ).replace("\n", " ")
    assert _facts(text)["contract_date"] == ["2027-03-03"]


def test_a_party_block_between_the_parties_still_ends_the_preamble() -> None:
    block = (
        f"წინამდებარე ხელშეკრულება დაიდო {_A}-სა (შემდგომში „შემკვეთი“) და {_B}-ს "
        "(შემდგომში „შემსრულებელი“) შორის 2027 წლის 9 მაისს.\n"
    )
    text = "ხელშეკრულება N 7\n" + block + "1.1. მომსახურება.\n"
    assert preamble_end(text) == text.index(_A)
    assert "contract_date" not in _facts(text)


def test_a_flattened_contract_without_a_title_ends_its_preamble_at_the_first_party() -> None:
    text = _contract("1.1. მომსახურება 2027 წლის 9 მაისს.").replace("\n", " ")
    assert preamble_end(text) == text.index(_A)


def test_a_party_named_after_the_contract_word_without_between_ends_the_preamble() -> None:
    text = f"ხელშეკრულება N 7 {_A} (შემდგომში „შემკვეთი“) და {_B}\n2027 წლის 9 მაისი\n"
    assert preamble_end(text) == text.index(_A)


# --- EXTRACTION-R2-15: a sentence does not end inside a bracket or at a chained abbreviation ---


def _sentences(text: str) -> list[str]:
    return [text[a:b] for a, b in sentence_spans(text)]


def test_a_stop_inside_a_short_bracket_ends_no_sentence() -> None:
    text = "სარემონტო სამუშაოები (იხ. დანართი N1) სრულად. შემდეგი წინადადება."
    assert _sentences(text) == [
        "სარემონტო სამუშაოები (იხ. დანართი N1) სრულად.",
        "შემდეგი წინადადება.",
    ]


def test_a_chained_abbreviation_ends_no_sentence() -> None:
    text = "სამუშაოები, მ.შ. სახურავის შეკეთება. შემდეგი, ე.წ. მეორე ეტაპი."
    assert _sentences(text) == [
        "სამუშაოები, მ.შ. სახურავის შეკეთება.",
        "შემდეგი, ე.წ. მეორე ეტაპი.",
    ]


def test_and_so_on_still_ends_a_sentence() -> None:
    text = "ფანჯრები, კარები და ა.შ. შემდეგი წინადადება."
    assert _sentences(text) == ["ფანჯრები, კარები და ა.შ.", "შემდეგი წინადადება."]


def test_a_bracket_left_open_or_running_on_does_not_hold_a_sentence_together() -> None:
    open_bracket = "პირველი (იხილეთ დანართი. მეორე წინადადება."
    assert _sentences(open_bracket) == ["პირველი (იხილეთ დანართი.", "მეორე წინადადება."]
    broken = "პირველი (იხილეთ.\nმეორე) წინადადება."
    assert _sentences(broken) == ["პირველი (იხილეთ.", "მეორე) წინადადება."]


def test_a_subject_clause_is_read_past_an_abbreviation() -> None:
    clause = (
        "1.1. ხელშეკრულების საგანია სარემონტო სამუშაოები (იხ. დანართი N1), მ.შ. სახურავის შეკეთება."
    )
    assert _facts(_contract(clause))["subject"] == [
        "სარემონტო სამუშაოები (იხ. დანართი N1), მ.შ. სახურავის შეკეთება"
    ]
