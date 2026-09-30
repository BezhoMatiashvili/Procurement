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
