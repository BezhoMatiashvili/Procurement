"""Dates as Georgian documents write them, and the periods and document facts read from them.

Synthetic text only: every name, code and date here is invented.
"""

from __future__ import annotations

from datetime import date

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.extraction.georgian import (
    MONTH_FORMS,
    month_case,
    month_form,
    month_typo,
    render_date_like,
)
from procurement_agent.extraction.rules import find_dates
from tests.unit.test_aoa_check import ACT, CONTRACT

# --- the day of a date: quoted, ordinal ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "iso", "raw"),
    [
        ("„12“ მარტი, 2027 წ.", "2027-03-12", "„12“ მარტი, 2027 წ."),
        ("ქ. ფოთი, „07“ ოქტომბერი 2027 წელი", "2027-10-07", "„07“ ოქტომბერი 2027 წელი"),
        (",,12'' მარტი, 2027 წ.", "2027-03-12", ",,12'' მარტი, 2027 წ."),
        ("2027 წლის „3“ მაისი", "2027-05-03", "2027 წლის „3“ მაისი"),
    ],
)
def test_a_quoted_day_is_a_date(text: str, iso: str, raw: str) -> None:
    [hit] = find_dates(text)
    assert (hit.normalized, hit.raw_text) == (iso, raw)


def test_a_quoted_name_before_a_date_is_not_part_of_it() -> None:
    [hit] = find_dates("„დამკვეთი“ 14 მარტი 2027")
    assert (hit.normalized, hit.raw_text) == ("2027-03-14", "14 მარტი 2027")


def test_an_unclosed_quote_is_left_out_of_the_date() -> None:
    [hit] = find_dates("„14 მარტი 2027")
    assert hit.raw_text == "14 მარტი 2027"


@pytest.mark.parametrize(
    ("text", "iso"),
    [
        ("2027 წლის 1-ლი აპრილი", "2027-04-01"),
        ("2027 წლის 1-ლი აპრილიდან", "2027-04-01"),
        ("1-ლი აპრილი, 2027 წ.", "2027-04-01"),
        ("2027 წლის 1 - ლი აპრილი", "2027-04-01"),
    ],
)
def test_an_ordinal_day_is_a_date(text: str, iso: str) -> None:
    assert [h.normalized for h in find_dates(text)] == [iso]


def test_both_ends_of_a_range_from_an_ordinal_day_are_read() -> None:
    text = "შეასრულოს სამუშაო 2027 წლის 1-ლი აპრილიდან 2027 წლის 31 ივლისის ჩათვლით."
    assert [h.normalized for h in find_dates(text)] == ["2027-04-01", "2027-07-31"]


def test_a_two_digit_year_is_read_only_where_the_word_for_year_follows_it() -> None:
    assert [h.normalized for h in find_dates("თბილისი, 14.03.27 წ.")] == ["2027-03-14"]
    assert [h.normalized for h in find_dates("14.03.27 წლის ხელშეკრულება")] == ["2027-03-14"]
    assert find_dates("ოთახი 14.03.27, სართული 2") == []
    assert find_dates("კოდი 01.14.03.27 წ.") == []


# --- a month's dative ------------------------------------------------------------------------


def test_the_vowel_stem_month_has_a_dative() -> None:
    assert month_form(8, "dat") == "აგვისტოს"
    assert month_form(8, "gen") == "აგვისტოს"
    assert month_form(8, "nom") == "აგვისტო"
    assert month_form(9, "dat") == "სექტემბერს"
    assert month_form(9, "gen") == "სექტემბრის"
    # the table the typo reader ranks by is unchanged: "აგვისტოს" is still a genitive there
    assert MONTH_FORMS["აგვისტოს"] == (8, "gen")
    assert month_typo("აგვისტოს") is None


@pytest.mark.parametrize(
    ("word", "following", "case"),
    [
        ("აგვისტოს", "", "dat"),
        ("აგვისტოს", ".", "dat"),
        ("აგვისტოს", " გაფორმდა", "dat"),
        ("აგვისტოს", " ჩათვლით", "gen"),
        ("აგვისტოს", ", ჩათვლით", "gen"),
        ("აგვისტოს", " მდგომარეობით", "gen"),
        ("აგვისტოს", " აქტით", "gen"),
        ("აგვისტოსვე", "", "dat"),
        ("სექტემბრის", "", "gen"),
        ("სექტემბერს", " ჩათვლით", "dat"),
        ("აგვისტო", "", "nom"),
    ],
)
def test_the_word_after_a_date_settles_the_case_of_its_month(
    word: str, following: str, case: str
) -> None:
    assert month_case(word, following=following) == case


@pytest.mark.parametrize(
    ("example", "following", "day", "expected"),
    [
        # an act signed on a day is dated in the dative, and so is its correction
        ("2027 წლის 31 აგვისტოს", "", date(2027, 9, 30), "2027 წლის 30 სექტემბერს"),
        ("2027 წლის 5 სექტემბერს", "", date(2027, 8, 31), "2027 წლის 31 აგვისტოს"),
        ("2027 წლის 5 აგვისტოს", ".", date(2027, 7, 31), "2027 წლის 31 ივლისს"),
        # ... and a date a postposition follows is in the genitive
        ("2027 წლის 5 აგვისტოს", " ჩათვლით", date(2027, 7, 31), "2027 წლის 31 ივლისის"),
        ("2027 წლის 5 სექტემბრის", "", date(2027, 8, 31), "2027 წლის 31 აგვისტოს"),
        # a quoted day keeps its quotes, a zero-padded one its zero
        ("„05“ ივლისი, 2027 წ.", "", date(2027, 6, 30), "„30“ ივნისი, 2027 წ."),
        ("„15“ ივლისი, 2027 წ.", "", date(2027, 7, 3), "„3“ ივლისი, 2027 წ."),
        ("„05“ ივლისი, 2027 წ.", "", date(2027, 7, 3), "„03“ ივლისი, 2027 წ."),
        ("05 ივლისი 2027", "", date(2027, 6, 30), "30 ივნისი 2027"),
        ("2027 წლის 1-ლი ივლისი", "", date(2027, 6, 30), "2027 წლის 30 ივნისი"),
    ],
)
def test_a_correction_is_written_in_the_examples_style_and_case(
    example: str, following: str, day: date, expected: str
) -> None:
    assert render_date_like(day, example, following=following) == expected


# --- the act's date, corrected in the act's own form -----------------------------------------

_SIGNED = "აქტი გაფორმდა 2026 წლის 30 აპრილი"


def _act_date_correction(act_date: str) -> str | None:
    act = ACT.replace(_SIGNED, f"აქტი გაფორმდა {act_date}.")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.ACT_DATE"]
    assert finding.severity == "error"
    return finding.expected


def test_an_act_dated_in_the_dative_is_corrected_in_the_dative() -> None:
    assert _act_date_correction("2026 წლის 5 მაისს") == "2026 წლის 30 აპრილს"


def test_an_act_dated_with_a_quoted_day_is_read_and_corrected_in_that_style() -> None:
    act = ACT.replace(_SIGNED, "აქტი გაფორმდა „30“ აპრილი, 2026 წ.")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary["act_date"] == "2026-04-30"
    assert [f for f in report.findings if f.severity in ("error", "warning")] == []
    assert _act_date_correction("„05“ მაისი, 2026 წ.") == "„30“ აპრილი, 2026 წ."
