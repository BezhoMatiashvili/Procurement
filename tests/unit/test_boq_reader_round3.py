"""BOQ bills the third red-team round found the reader got wrong.

Each test is one such way: a number typed as text with a stray separator, a unit written as a
plural or a full name, a Summary VAT row worded "Total VAT", a line amount headed "Cost" or
"თანხა", a unit written where the template left none, bidders tied on a half cent, a machinery
cost component, and a bid-form block above the bill. Every workbook is synthetic and built in
memory; every expected number is worked out here from the test's own data.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.boq import _num

# --- numbers typed as text --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1,250.50", 1250.5),
        ("1.250,50", 1250.5),
        ("1 250", 1250.0),
        ("1 250,50", 1250.5),
        ("1 250 000,5", 1250000.5),
        ("1.250.000", 1250000.0),
        ("1.250.000,75", 1250000.75),
        ("12.3450", 12.345),
        ("0.125", 0.125),
        ("0,5", 0.5),
        ("-12.5", -12.5),
        ("12,50", 12.5),
        ("1234.567", 1234.567),  # four digits before: no thousands group, three decimals
        (" 42 ", 42.0),
    ],
)
def test_number_text_that_reads_one_way_is_that_number(text: str, value: float) -> None:
    assert _num(text) == pytest.approx(value)


@pytest.mark.parametrize(
    "text",
    [
        "1450.00.",  # read as 145,000 by stripping every separator: a 100x rate
        "1,450.0.",
        "14.50.0",
        "12.50.",
        "1.25.",
        "1.2.3",
        "7.5,",
        "12,50,",
        "1.23,4",
        "1,250.000.5",  # two grouping characters mixed
        "12,34,567",  # groups that are not threes
        "1 250.000",  # one more group, or three decimals?
        ".5",
        "1250.",
        "10 m3",
    ],
)
def test_number_text_with_a_stray_or_doubled_separator_stays_text(text: str) -> None:
    assert _num(text) is None
