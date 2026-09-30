"""BOQ bills the third red-team round found the reader got wrong.

Each test is one such way: a number typed as text with a stray separator, a unit written as a
plural or a full name, a Summary VAT row worded "Total VAT", a line amount headed "Cost" or
"თანხა", a unit written where the template left none, bidders tied on a half cent, a machinery
cost component, and a bid-form block above the bill. Every workbook is synthetic and built in
memory; every expected number is worked out here from the test's own data.
"""

from __future__ import annotations

import pytest
from openpyxl import Workbook

from procurement_agent.checks.boq import (
    BidComparison,
    BoqWorkbook,
    SummaryRow,
    _colour,
    _colour_extremes,
    _num,
    _plays,
    cents,
    grand_total,
    norm_unit,
    reconcile_summary,
    summary_roles,
)

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


# --- the Summary's VAT row worded "Total VAT" -----------------------------------------------------


@pytest.mark.parametrize(
    ("role", "label"),
    [
        ("vat", "Total VAT"),
        ("vat", "Total VAT 18%"),
        ("vat", "VAT amount"),
        ("vat", "Amount of VAT"),
        ("vat", "სულ დღგ"),
        ("incl", "VAT-inclusive total"),
        ("incl", "Total VAT inclusive"),
        ("excl", "VAT-exclusive total"),
        ("excl", "Total VAT exclusive"),
    ],
)
def test_a_vat_row_or_total_worded_around_the_tax_name_plays_its_part(
    role: str, label: str
) -> None:
    assert _plays(role, label)


@pytest.mark.parametrize(
    ("role", "label"),
    [
        ("vat", "VAT-inclusive total"),
        ("vat", "Total VAT inclusive"),
        ("vat", "VAT-exclusive total"),
        ("vat", "Total excl. VAT"),
        ("vat", "Total incl. VAT"),
        ("vat", "Total cost"),
        ("vat", "Total private works"),
        ("cost", "Total VAT"),
        ("incl", "Total VAT"),
        ("excl", "Total VAT"),
        ("incl", "VAT-exclusive total"),
    ],
)
def test_a_row_worded_near_the_vat_row_does_not_take_its_part(role: str, label: str) -> None:
    assert not _plays(role, label)


@pytest.mark.parametrize(
    ("vat_label", "incl_label"),
    [("Total VAT", "TOTAL PRICE INCLUDING VAT"), ("VAT", "VAT-inclusive total")],
)
def test_a_chain_with_its_vat_row_or_total_worded_so_reconciles_and_ranks(
    vat_label: str, incl_label: str
) -> None:
    book = BoqWorkbook(
        name="B",
        summary_sheet="SUMMARY",
        summary_rows=[
            SummaryRow(9, "1", "WORKS", 2000.0),
            SummaryRow(10, "", "TOTAL COST", 2000.0),
            SummaryRow(11, "", "TOTAL PRICE EXCL. VAT", 2000.0),
            SummaryRow(12, "", vat_label, 360.0, rate=0.18),
            SummaryRow(13, "", incl_label, 2360.0),
        ],
    )
    roles = summary_roles(book)
    assert (roles["vat"].row, roles["incl"].row) == (12, 13)
    assert grand_total(book) == 2360.0
    assert reconcile_summary(book) == []


# --- bidders tied to the cent ---------------------------------------------------------------------

#: the same half-cent total as two different float sums leave it
_TIED = (32574.785, 32574.785000000003)


def test_money_rounds_to_the_cent_half_up_whatever_float_noise_it_carries() -> None:
    assert [cents(x) for x in _TIED] == [32574.79, 32574.79]
    assert cents(0.125) == 0.13 and cents(-2.675) == -2.68
    assert cents(1999.994999) == 1999.99  # not a half cent: rounds down


def test_bidders_tied_to_the_cent_share_their_colour() -> None:
    comps = [BidComparison(bidder=n) for n in ("A", "B", "C")]
    for comp, total in zip(comps, (*_TIED, 34201.64), strict=True):
        comp.common_scope_total, comp.common_scope_positions = total, 5
    _colour(comps)
    assert [c.colour for c in comps] == ["green", "green", "red"]


def test_tied_cells_of_the_merged_workbook_share_their_colour() -> None:
    ws = Workbook().active
    assert ws is not None
    for r, v in enumerate((*_TIED, 34201.64), start=1):
        ws.cell(r, 1, v)
    _colour_extremes(ws, [1, 2, 3], 1)
    fills = [ws.cell(r, 1).fill.fgColor.rgb for r in (1, 2, 3)]
    assert fills[0] == fills[1] != fills[2]
