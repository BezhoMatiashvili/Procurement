"""BOQ bills the third red-team round found the reader got wrong.

Each test is one such way: a number typed as text with a stray separator, a unit written as a
plural or a full name, a Summary VAT row worded "Total VAT", a line amount headed "Cost" or
"თანხა", a unit written where the template left none, bidders tied on a half cent, a machinery
cost component, and a bid-form block above the bill. Every workbook is synthetic and built in
memory; every expected number is worked out here from the test's own data.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.boq import _num, norm_unit

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


# --- units: every name of one measure -----------------------------------------------------------

#: per measure: its abbreviations, its names singular and plural, English and Georgian
_MEASURES = {
    "lm": (
        "m",
        "lm",
        "metre",
        "metres",
        "meter",
        "meters",
        "running metre",
        "linear metres",
        "მ",
        "გრძ.მ",
        "მ.გრძ.",
        "მეტრი",
    ),
    "m2": ("m2", "sq.m", "square metre", "square meters", "მ2", "კვ.მ"),
    "m3": ("m3", "cum", "cubic metre", "cubic meters", "მ3", "კუბ.მ"),
    "km": ("km", "kms", "kilometre", "kilometers", "კმ"),
    "kg": ("kg", "kgs", "kilogram", "kilograms", "კგ", "კილოგრამი"),
    "t": ("t", "tn", "ton", "tonnes", "ტ", "ტონ.", "ტონა"),
    "l": ("l", "lt", "ltr", "ltrs", "litre", "liters", "ლ", "ლიტრი"),
    "pcs": ("pcs", "pc", "nr", "nos", "no.", "number", "pieces", "ც", "ცალ.", "ცალი"),
}


@pytest.mark.parametrize(
    ("measure", "spelling"),
    [(m, s) for m, spellings in _MEASURES.items() for s in spellings],
)
def test_every_name_of_a_measure_folds_to_that_measure(measure: str, spelling: str) -> None:
    assert norm_unit(spelling) == measure


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("ls", "l"),  # a lump sum is not a litre, even with an "s" dropped
        ("lts", "ls"),
        ("item", "pcs"),
        ("each", "pcs"),  # a count by another name stays a question, like "item"
        ("nos", "no-go"),
        ("metres", "m2"),
        ("square metre", "cubic metre"),
        ("kms", "km2"),
        ("kgs", "t"),
        ("ტონ", "ტ2"),
    ],
)
def test_names_of_different_measures_stay_apart(a: str, b: str) -> None:
    assert norm_unit(a) != norm_unit(b)
