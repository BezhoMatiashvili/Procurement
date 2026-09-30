"""BOQ bills laid out, worded or typed in ways the second red-team round found the reader missed.

Each test is one such way: a number typed as text with three decimals, the invitation to list
extra items standing in a group heading, a quantity taken from a take-off sheet, component
column groups (Materials: Rate | Amount), a Summary row worded another usual way, sections
closed by their own sub-totals under a grand total, columns headed "Rate in GEL", a unit
written in Georgian. Every workbook is synthetic and built in memory; every expected number is
worked out here from the test's own data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from procurement_agent.checks.boq import (
    _label_key,
    compare_bid,
    norm_unit,
    read_boq,
    template_findings,
)
from tests.unit.test_boq_reader_variants import Cells, Fx, _rules, _save

# --- units ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("t", "ტნ"),
        ("day", "დღე"),
        ("day", "days"),
        ("set", "კომპლ."),
        ("set", "კომპლექტი"),
        ("set", "sets"),
        ("km", "კმ"),
        ("ha", "ჰა"),
        ("l", "ლ"),
        ("l", "litre"),
        ("l", "ლიტრი"),
        ("week", "weeks"),
    ],
)
def test_a_unit_spelled_the_georgian_or_the_plural_way_is_one_unit(a: str, b: str) -> None:
    assert norm_unit(a) == norm_unit(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("ls", "l"),  # a lump sum is not a litre
        ("set", "pcs"),
        ("item", "pcs"),
        ("km", "lm"),
        ("ha", "hr"),
        ("day", "month"),
        ("day", "week"),
        ("l", "lm"),
    ],
)
def test_units_of_different_measures_stay_apart(a: str, b: str) -> None:
    assert norm_unit(a) != norm_unit(b)


# --- column headers naming a currency ---------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("Rate in GEL", "rate_total"),
        ("Amount in GEL", "amount"),
        ("Unit rate in GEL", "rate_total"),
        ("Total amount, GEL", "amount"),
        ("Total price in USD", "amount"),
        ("Rate, ₾", "rate_total"),
        ("Amount EUR", "amount"),
        ("ერთეულის ფასი ლარში", "rate_total"),
        ("ღირებულება ლარში", "amount"),
        ("ერთ. ფასი დოლარში", "rate_total"),
        # the ones read before stay read
        ("Rate in USD", "rate_total"),
        ("Rate in US $", "rate_total"),
        ("Amount (GEL)", "amount"),
        ("ერთეულის ფასი, აშშ დოლარი", "rate_total"),
    ],
)
def test_a_price_column_is_read_whatever_currency_its_header_names(label: str, key: str) -> None:
    assert _label_key(label) == key


@pytest.mark.parametrize(
    "label",
    [
        "Notes in USD",
        "Description in English",
        # a group label over the component columns, and its amounts' twin: never the rate itself
        "Unit rates in USD (excl. VAT)",
        "Totals in USD (excl. VAT)",
        "Rate of exchange GEL",
    ],
)
def test_a_header_that_only_names_a_currency_is_not_a_price_column(label: str) -> None:
    assert _label_key(label) not in ("rate_total", "amount")


def _lari_sheet(rate_head: str, amount_head: str, factor: float | None) -> Cells:
    heads = ["No", "Description", "Unit", "Quantity", rate_head, amount_head]
    cells: Cells = {(1, c): h for c, h in enumerate(heads, 1)}
    rows = [("1.1", "Trench excavation", "m3", 50.0, 12.0), ("1.2", "Sand bedding", "m3", 8.0, 30.0)]
    for r, (item, text, unit, qty, rate) in enumerate(rows, start=2):
        cells.update({(r, 1): item, (r, 2): text, (r, 3): unit, (r, 4): qty})
        if factor is not None:
            cells[(r, 5)] = rate * factor
        cells[(r, 6)] = Fx(f"=E{r}*D{r}", rate * (factor or 0.0) * qty)
    return cells


@pytest.mark.parametrize(
    ("rate_head", "amount_head"),
    [
        ("Rate in GEL", "Amount in GEL"),
        ("Unit rate in GEL", "Total amount, GEL"),
        ("ერთეულის ფასი ლარში", "ღირებულება ლარში"),
    ],
)
def test_a_bill_priced_in_lari_is_evaluated(
    tmp_path: Path, rate_head: str, amount_head: str
) -> None:
    t_cells = _lari_sheet(rate_head, amount_head, None)
    b_cells = _lari_sheet(rate_head, amount_head, 1.0)
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": t_cells}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": b_cells}), name="B")
    assert {"rate_total", "amount"} <= t.sheets["1. WORKS"].columns.keys()
    comp = compare_bid(t, b)
    assert comp.evaluated_cost == pytest.approx(50 * 12.0 + 8 * 30.0)
    assert comp.findings == []


def test_a_sheet_whose_price_columns_are_not_recognised_says_so_not_that_it_is_unpriced(
    tmp_path: Path,
) -> None:
    """A priced sheet whose rate and amount headers the reader does not know is the reader's
    gap: "section left unpriced" blamed the bidders, who priced every line."""
    t_cells = _lari_sheet("Tarif", "Summe", None)
    b_cells = _lari_sheet("Tarif", "Summe", 1.0)
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": t_cells}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": b_cells}), name="B")
    notes = [f for f in template_findings(t) if f.rule_id == "BOQ.PRICES_NOT_READ"]
    assert [(f.severity, f.actual) for f in notes] == [("warning", "1. WORKS")]
    assert "BOQ.SECTION_UNPRICED" not in _rules(compare_bid(t, b))


# --- a quantity taken from elsewhere ----------------------------------------------------------


def _kind(qty_formula: str, *, names: dict[str, str] | None = None, local: bool = False) -> str:
    """The kind of row 7 of a sheet "1. WORKS" whose quantity is ``qty_formula`` and whose amount
    is its own row's product; ``names`` are defined names (sheet-scoped with ``local``)."""
    from openpyxl.workbook.defined_name import DefinedName

    from procurement_agent.checks.boq import _line_kind

    values, formulas = Workbook(), Workbook()
    wf = formulas.active
    assert wf is not None
    wf.title = "1. WORKS"
    for name, target in (names or {}).items():
        scope = wf.defined_names if local else formulas.defined_names
        scope[name] = DefinedName(name, attr_text=target)
    wf.cell(7, 6, qty_formula)
    wf.cell(7, 8, "=G7*F7")
    cols = {"desc": 2, "qty": 6, "amount": 8}
    return _line_kind(values.active, wf, cols, 7, "Waterproofing membrane", True)


@pytest.mark.parametrize(
    "formula",
    [
        "=SUM('Take-off'!D2:D5)",
        "=SUM(Measure!D2:D5)",
        "=SUM(30,30,30,30)",
        "=SUM(12.5, 7.5)",
        "=SUM('Take-off'!D2,'Take-off'!D9)",
    ],
)
def test_a_quantity_added_up_from_a_take_off_sheet_or_constants_is_the_positions_own(
    formula: str,
) -> None:
    """No row of the bill is added up: the row is a position. Read as a parent, it fell out of
    the evaluation, and the sheet no longer reconciled on any faithful bid."""
    assert _kind(formula) == "position"


@pytest.mark.parametrize(
    "formula",
    [
        "=SUM(F8:F12)",
        "=SUM('1. WORKS'!F8:F12)",  # its own sheet, named: still the rows below it
        "=F8+F9",
        "=SUM(Unknown_Block)",  # a name that cannot be followed: the parent it most likely is
    ],
)
def test_a_quantity_adding_up_rows_of_its_own_sheet_is_still_a_parent(formula: str) -> None:
    assert _kind(formula) == "heading"


def test_a_quantity_over_a_defined_name_is_a_parent_only_where_the_name_is_rows_of_this_sheet() -> (
    None
):
    here = {"Block_Q": "'1. WORKS'!$F$8:$F$12"}
    there = {"Survey_Q": "'Take-off'!$D$2:$D$5"}
    assert _kind("=SUM(Block_Q)", names=here) == "heading"
    assert _kind("=SUM(Block_Q)", names=here, local=True) == "heading"
    assert _kind("=SUM(Survey_Q)", names=there) == "position"
    assert _kind("=SUM(Survey_Q)", names=there, local=True) == "position"


def test_a_bill_whose_quantities_come_from_a_take_off_sheet_is_evaluated(tmp_path: Path) -> None:
    def book(rate: float | None) -> dict[str, Cells]:
        takeoff: Cells = {(1, 1): "Wall", (1, 4): "Area", (2, 4): 30.0, (3, 4): 45.0}
        cells = _lari_sheet("Rate", "Amount", None)
        cells[(2, 4)] = Fx("=SUM('Take-off'!D2:D3)", 75.0)
        cells[(3, 4)] = Fx("=SUM(4,4)", 8.0)
        amounts = []
        for r, qty, price in ((2, 75.0, 12.0), (3, 8.0, 30.0)):
            if rate is not None:
                cells[(r, 5)] = price * rate
            amounts.append(price * (rate or 0.0) * qty)
            cells[(r, 6)] = Fx(f"=E{r}*D{r}", amounts[-1])
        cells[(4, 2)] = "SUB-TOTAL"
        cells[(4, 6)] = Fx("=SUM(F2:F3)", sum(amounts))
        return {"1. WORKS": cells, "Take-off": takeoff}

    t = read_boq(_save(tmp_path / "t.xlsx", book(None)), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", book(1.0)), name="B")
    assert [ln.kind for ln in t.sheets["1. WORKS"].lines[:2]] == ["position", "position"]
    comp = compare_bid(t, b)
    assert comp.evaluated_cost == pytest.approx(75 * 12.0 + 8 * 30.0)
    assert comp.findings == []
