"""Red team round 3: dates, periods and whose date is whose, each reproduced with invented text.

AOA-R3-02, AOA-R3-10, COMPARE-R3-02, COMPARE-R3-03 and EXTRACTION-R3-05. Synthetic text only:
every name, code and date here is invented.
"""

from __future__ import annotations

from datetime import date

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.extraction.georgian import render_date_like
from procurement_agent.extraction.rules import find_dates
from tests.unit.test_aoa_check import ACT, CONTRACT

_SIGNED = "აქტი გაფორმდა 2026 წლის 30 აპრილი"


def _act_date_finding(act_date: str) -> tuple[str | None, str | None]:
    act = ACT.replace(_SIGNED, f"აქტი გაფორმდა {act_date}")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.ACT_DATE"]
    assert finding.severity == "error"
    return finding.actual, finding.expected


# --- AOA-R3-10: a four-digit numeric date keeps its year word -----------------------------------


@pytest.mark.parametrize(
    "form",
    ["05.07.2026 წ.", "05.07.2026წ.", "05.07.2026 წელს", "05.07.2026 წლის", "5/7/2026 წელი"],
)
def test_a_numeric_dates_year_word_is_part_of_it(form: str) -> None:
    [hit] = find_dates(f"აქტი გაფორმდა {form}, თბილისში")
    assert (hit.normalized, hit.raw_text) == ("2026-07-05", form)
    assert render_date_like(date(2026, 7, 5), hit.raw_text) == form


@pytest.mark.parametrize(
    ("text", "raw"),
    [
        ("05.07.2026 წინ", "05.07.2026"),  # a word that only starts like the year word
        ("05.07.2026 წლიდან", "05.07.2026"),  # "from the year": a postposition, read after it
        ("05.07.2026 წყალი", "05.07.2026"),
    ],
)
def test_a_word_that_is_no_year_word_stays_out_of_a_numeric_date(text: str, raw: str) -> None:
    [hit] = find_dates(text)
    assert hit.raw_text == raw


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("05.05.2026 წ.", "30.04.2026 წ."),
        ("05.05.2026წ.", "30.04.2026წ."),
        ("05.05.2026 წელს", "30.04.2026 წელს"),
    ],
)
def test_the_act_date_quote_and_correction_keep_the_year_word(written: str, expected: str) -> None:
    assert _act_date_finding(written) == (written, expected)


# --- AOA-R3-02: a period whose two ends name their months and share one year ---------------------

_DASH = chr(0x2013)


@pytest.mark.parametrize(
    ("text", "dates", "raws"),
    [
        ("01.06-30.06.2026", ["2026-06-01", "2026-06-30"], ["01.06", "30.06.2026"]),
        ("25.05-30.06.2026", ["2026-05-25", "2026-06-30"], ["25.05", "30.06.2026"]),
        (f"01.06 {_DASH} 30.06.2026 წ.", ["2026-06-01", "2026-06-30"], ["01.06", "30.06.2026 წ."]),
        (
            "1 ივნისი - 30 ივნისი, 2026 წ.",
            ["2026-06-01", "2026-06-30"],
            ["1 ივნისი", "30 ივნისი, 2026 წ."],
        ),
        (
            "25 მაისი - 30 ივნისი 2026 წელი",
            ["2026-05-25", "2026-06-30"],
            ["25 მაისი", "30 ივნისი 2026 წელი"],
        ),
    ],
)
def test_a_day_and_month_before_a_full_date_is_the_ranges_first_day(
    text: str, dates: list[str], raws: list[str]
) -> None:
    hits = find_dates(text)
    assert [h.normalized for h in hits] == dates
    assert [h.raw_text for h in hits] == raws


@pytest.mark.parametrize(
    "text",
    [
        "1.2-3.04.2026",  # a single-digit month reads as a version or a clause number
        "01.2-30.06.2026",
        "5.01.06-30.06.2026",  # a day.month after a stop is part of another number
        "N 01.06-30.06.2026",  # a number marker makes it a number
        "ა01.06-30.06.2026",
        "7 01.06-30.06.2026",
        "30.06-01.06.2026",  # a first day after the last is no range
        "01.13-30.06.2026",  # no such month
        "3 ოთახი - 30 ივნისი, 2026 წ.",  # a word that is no month
        "1 ივნისი, 30 ივნისი, 2026 წ.",  # no dash between them
        "ოთახი12 ივნისი - 30 ივნისი, 2026 წ.",
    ],
)
def test_what_only_looks_like_a_month_range_stays_one_date(text: str) -> None:
    assert len(find_dates(text)) == 1


_PERIOD = "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით."


def _with_period(period: str):  # type: ignore[no-untyped-def]
    act = ACT.replace(_PERIOD, f"მომსახურება შესრულდა {period}.")
    return check_aoa(aoa_text=act, contract_text=CONTRACT)


@pytest.mark.parametrize("period", ["01.04-30.04.2026", "1 აპრილი - 30 აპრილი, 2026 წ."])
def test_a_month_range_sharing_one_year_is_read_whole_and_a_correct_act_is_clean(
    period: str,
) -> None:
    report = _with_period(period)
    assert report.summary["execution_period"] == "2026-04-01..2026-04-30"
    assert [f.rule_id for f in report.findings if f.severity in ("error", "warning")] == []


@pytest.mark.parametrize(
    ("period", "correction"),
    [
        ("01.04-30.04.2025", "01.04-30.04.2026"),
        ("1 აპრილი - 30 აპრილი, 2025 წ.", "1 აპრილი - 30 აპრილი, 2026 წ."),
    ],
)
def test_a_year_slip_in_a_month_range_quotes_and_corrects_the_whole_period(
    period: str, correction: str
) -> None:
    [finding] = [f for f in _with_period(period).findings if f.rule_id == "AOA.PERIOD_IN_TERM"]
    assert (finding.actual, finding.expected) == (period, correction)


def test_a_month_range_starting_before_the_term_is_outside_it() -> None:
    # the contract's term runs from 1 March 2026
    report = _with_period("25.02-30.04.2026")
    assert report.summary["execution_period"] == "2026-02-25..2026-04-30"
    assert "AOA.PERIOD_IN_TERM" in [f.rule_id for f in report.errors]


# --- COMPARE-R3-03: a day range before one month and year is one period in a comparison ---------

from procurement_agent.checks.compare import compare, hits_of  # noqa: E402
from procurement_agent.ingestion.segmenter import TableCell, build  # noqa: E402
from tests.unit.test_compare_redteam2 import CONTRACT as SUPPLY  # noqa: E402
from tests.unit.test_compare_redteam2 import edit, found, run  # noqa: E402

_WINDOW = "01.06.2026 - 30.06.2026"
_SUPPLY = edit(
    SUPPLY,
    "1.1. მიმწოდებელი ვალდებულია მიაწოდოს შემსყიდველს სასკოლო მერხები.",
    f"1.1. მიმწოდებელი ვალდებულია მიაწოდოს შემსყიდველს სასკოლო მერხები {_WINDOW} პერიოდში.",
)


@pytest.mark.parametrize(
    "form", ["01-30.06.2026", "1-30 ივნისი, 2026 წ.", "2026 წლის 1-30 ივნისი", "01.06-30.06.2026"]
)
def test_a_day_range_is_one_period_on_its_line(form: str) -> None:
    periods = [h.normalized for h in hits_of(f"მიწოდება {form} პერიოდში.")]
    assert periods == ["2026-06-01/2026-06-30"]


@pytest.mark.parametrize(
    "form", ["01-30.06.2026", "1-30 ივნისი, 2026 წ.", "2026 წლის 1-30 ივნისი", "01.06-30.06.2026"]
)
def test_a_window_restyled_as_a_day_range_is_no_change(form: str) -> None:
    assert found(run(_SUPPLY, edit(_SUPPLY, _WINDOW, form))) == []


def test_a_day_range_whose_end_moved_is_one_period_change() -> None:
    assert found(run(_SUPPLY, edit(_SUPPLY, _WINDOW, "01-25.06.2026"))) == [
        ("CMP.DURATION", "2026-06-01/2026-06-30", "2026-06-01/2026-06-25", "error")
    ]


def test_an_acts_period_restyled_as_a_day_range_is_no_change() -> None:
    from tests.unit.test_compare_redteam2 import ACT as SMALL_ACT
    from tests.unit.test_compare_redteam2 import PERIOD as SMALL_PERIOD

    right = edit(SMALL_ACT, SMALL_PERIOD, "01-31.07.2026")
    assert found(run(SMALL_ACT, right)) == []


def _schedule(cell: str):  # type: ignore[no-untyped-def]
    header = ["ეტაპი", "ვადა", "გადახდის წილი"]
    rows = [header, ["I ეტაპი", cell, "30%"], ["II ეტაპი", "01.07.2026 - 31.07.2026", "70%"]]
    records: list[tuple] = [("დანართი №2. მიწოდების გრაფიკი", "text", "¶1", None, None)]  # type: ignore[type-arg]
    for r, row in enumerate(rows, start=1):
        for c, text in enumerate(row, start=1):
            head = header[c - 1] if r > 1 else None
            records.append(
                (text, "table_cell", f"T1/r{r}/c{c}", None, None, TableCell(1, r, c, head))
            )
    return build(records)


def test_a_table_cells_period_restyled_as_a_day_range_is_no_change() -> None:
    report = compare(
        _schedule(_WINDOW),
        _schedule("2026 წლის 1-30 ივნისი"),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    assert found(report) == []
