"""Red team round 2: dates, periods and the contract's term, each reproduced with invented text.

AOA-R2-05, AOA-R2-10, EXTRACTION-R2-01, -02, -03 and -15. Synthetic text only: every name, code
and date here is invented.
"""

from __future__ import annotations

from datetime import date

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.extraction.georgian import render_date_like
from procurement_agent.extraction.rules import find_dates
from tests.unit.test_aoa_check import ACT, CONTRACT

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
        ("1-15 ივნისი, 2026 წ.", ["2026-06-01", "2026-06-15"], ["1-", "15 ივნისი, 2026 წ."]),
        ("01-15.06.2026", ["2026-06-01", "2026-06-15"], ["01-", "15.06.2026"]),
        (
            f"1 {chr(0x2013)} 15 ივნისი 2026 წლის",
            ["2026-06-01", "2026-06-15"],
            [f"1 {chr(0x2013)}", "15 ივნისი 2026 წლის"],
        ),
        ("1-15 June 2026", ["2026-06-01", "2026-06-15"], ["1-", "15 June 2026"]),
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
        "01.06-15.06.2026",  # a day that is a month's, not a range's
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
