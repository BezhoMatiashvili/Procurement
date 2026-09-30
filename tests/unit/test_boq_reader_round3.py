"""BOQ bills the third red-team round found the reader got wrong.

Each test is one such way: a number typed as text with a stray separator, a unit written as a
plural or a full name, a Summary VAT row worded "Total VAT", a line amount headed "Cost" or
"თანხა", a unit written where the template left none, bidders tied on a half cent, a machinery
cost component, and a bid-form block above the bill. Every workbook is synthetic and built in
memory; every expected number is worked out here from the test's own data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from procurement_agent.checks.boq import (
    BidComparison,
    BoqWorkbook,
    SummaryRow,
    _colour,
    _colour_extremes,
    _label_key,
    _num,
    _plays,
    cents,
    compare_bid,
    grand_total,
    norm_unit,
    read_boq,
    reconcile_summary,
    summary_roles,
    template_findings,
)
from tests.unit.test_boq_reader_variants import Cells, Fx, _rules, _save

_HEAD = ("No", "Description", "Unit", "Quantity", "Rate", "Amount")

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


# --- a unit written where the template left none --------------------------------------------------


def _unit_sheet(units: tuple[str | None, str | None, str | None]) -> Cells:
    """Three positions at 10 x 5.00, with the given units."""
    cells: Cells = {(1, c): h for c, h in enumerate(_HEAD, 1)}
    for i, (item, unit) in enumerate(zip(("2.1", "2.2", "2.3"), units, strict=True)):
        r = i + 2
        cells.update({(r, 1): item, (r, 2): f"Partition wall type {i + 1}", (r, 4): 10.0})
        if unit is not None:
            cells[(r, 3)] = unit
        cells[(r, 5)] = 5.0
        cells[(r, 6)] = Fx(f"=D{r}*E{r}", 50.0)
    return cells


def test_a_unit_written_where_the_template_left_none_is_listed_apart(tmp_path: Path) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _unit_sheet(("m2", None, None))}))
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": _unit_sheet(("m2", "m3", None))}))
    comp = compare_bid(t, b)
    assert [(ln.sheet, ln.item_no, ln.unit) for ln in comp.unit_filled] == [
        ("1. WORKS", "2.2", "m3")
    ]
    [note] = [f for f in comp.findings if f.rule_id == "BOQ.UNIT_FILLED"]
    assert note.severity == "info" and "2.2 (m3)" in note.message_en
    # never a unit change, and the position is still evaluated as the template's
    assert "BOQ.UNIT_CHANGED" not in _rules(comp)
    assert comp.evaluated_cost == pytest.approx(3 * 10 * 5.0)


def test_a_unit_the_template_states_is_never_listed_as_filled(tmp_path: Path) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _unit_sheet(("m2", "m2", None))}))
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": _unit_sheet(("m2", "m3", None))}))
    comp = compare_bid(t, b)
    assert comp.unit_filled == []
    assert _rules(comp).count("BOQ.UNIT_CHANGED") == 1


# --- the line amount headed "Cost", "Sum", "თანხა" ------------------------------------------------


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("Cost", "amount"),
        ("Total cost", "amount"),
        ("Total cost, USD", "amount"),
        ("Sum", "amount"),
        ("თანხა", "amount"),
        ("სულ ღირებულება", "amount"),
        ("ჯამური ღირებულება ლარში", "amount"),
        ("Unit cost", "rate_total"),  # the rate, never the line amount
    ],
)
def test_the_line_amount_is_read_by_its_usual_names(label: str, key: str) -> None:
    assert _label_key(label) == key


@pytest.mark.parametrize(
    "label", ["Cost code", "Costs excl. VAT", "Summary", "Sum insured", "თანხის წყარო"]
)
def test_a_header_only_resembling_the_amount_is_not_the_amount(label: str) -> None:
    assert _label_key(label) is None


def _amount_sheet(amount_label: str, typed: float) -> Cells:
    """1.1 at 210 x 38.50 whose amount is typed as ``typed``, and 1.2 at 10 x 5.00."""
    cells: Cells = {(1, c): h for c, h in enumerate((*_HEAD[:5], amount_label), 1)}
    cells.update({(2, 1): "1.1", (2, 2): "Floor tiling", (2, 3): "m2", (2, 4): 210.0})
    cells.update({(2, 5): 38.5, (2, 6): typed})
    cells.update({(3, 1): "1.2", (3, 2): "Skirting", (3, 3): "m", (3, 4): 10.0, (3, 5): 5.0})
    cells[(3, 6)] = Fx("=D3*E3", 50.0)
    return cells


@pytest.mark.parametrize("label", ["Cost", "თანხა", "Total cost in USD"])
def test_a_line_amount_headed_otherwise_is_still_checked(tmp_path: Path, label: str) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _amount_sheet(label, 8085.0)}))
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": _amount_sheet(label, 6085.0)}))
    assert t.sheets["1. WORKS"].columns["amount"] == 6
    [err] = [f for f in compare_bid(t, b).findings if f.rule_id == "BOQ.ARITHMETIC"]
    assert err.expected == "8,085.00" and err.actual == "6,085.00"
    assert "BOQ.AMOUNTS_NOT_READ" not in [f.rule_id for f in template_findings(t)]


def test_a_sheet_whose_line_amount_is_not_recognised_says_so(tmp_path: Path) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _amount_sheet("Extension", 8085.0)}))
    assert "amount" not in t.sheets["1. WORKS"].columns
    [note] = [f for f in template_findings(t) if f.rule_id == "BOQ.AMOUNTS_NOT_READ"]
    assert note.severity == "info" and "1. WORKS" in note.message_en
