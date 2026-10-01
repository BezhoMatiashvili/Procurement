"""The independent BOQ gold writer (scripts/boq_reference.py) follows round 5's rules.

GOLD_SPEC 5 now says, among others: the Georgian "with VAT" is the total incl. VAT; a filled-in
blank line is priced only by its amount; a template quantity typed as text is evaluated for no
bidder; a heading ends a section group; "1." and "1" are one item number; a single-sheet price
list closed by its total incl. VAT is ranked on it; a hidden sheet the Summary does not link is
no section; the usual Georgian unit spellings are synonyms. The reference must write the same
gold, or the scores would measure a disagreement between two readers rather than the product.
Synthetic data only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.test_boq_reader_variants import Cells, Fx, _save
from tests.unit.test_boq_reference_round4 import ref


@pytest.mark.parametrize(
    "label",
    ["ჯამი დღგ-ით", "სულ დღგ-თ", "სულ ღირებულება დღგ-ს გათვალისწინებით", "სულ დღგ-ს ჩათვლით"],
)
def test_the_georgian_with_vat_is_the_total_incl_vat(label: str) -> None:
    assert ref.INCL_VAT.search(ref.fold(label))
    assert not ref.EXCL_VAT.search(ref.fold(label))


@pytest.mark.parametrize("label", ["სულ დღგ-ს გამოკლებით", "ჯამი დღგ-ს გარეშე"])
def test_the_georgian_vat_taken_off_is_the_total_excl_vat(label: str) -> None:
    assert ref.EXCL_VAT.search(ref.fold(label))
    assert not ref.INCL_VAT.search(ref.fold(label))


@pytest.mark.parametrize("label", ["დღგ 18%", "დღგ-ს თანხა", "დღგ-თან ერთად"])
def test_a_vat_label_that_is_no_total_is_not_read_as_one(label: str) -> None:
    assert not ref.INCL_VAT.search(ref.fold(label))


#: (item, description, unit, quantity, rate, amount): None leaves the cell empty; a number amount
#: is written as the row's own product with that cached value, text as typed
_Line = tuple[str, str, str, object, object, object]
_REF_HEAD = ("No", "Description", "Unit", "Quantity", "Rate in USD", "Amount")


def _ref_bill(lines: tuple[_Line, ...], *, template: bool = False) -> Cells:
    """A bill the reference reads: its header, a column-number row, the lines, a SUB-TOTAL."""
    cells: Cells = {(1, c): h for c, h in enumerate(_REF_HEAD, 1)}
    cells.update({(2, c): c for c in range(1, 7)})
    total = 0.0
    for r, (item, desc, unit, qty, rate, amount) in enumerate(lines, 3):
        cells.update({(r, 1): item, (r, 2): desc, (r, 3): unit})
        for c, value in ((4, qty), (5, rate)):
            if value is not None:
                cells[(r, c)] = value
        if template or isinstance(amount, float):
            value = 0.0 if template else amount
            cells[(r, 6)] = Fx(f"=D{r}*E{r}", value)
            total += value  # type: ignore[operator]
        elif amount is not None:
            cells[(r, 6)] = amount
    last = len(lines) + 2
    cells[(last + 1, 2)] = "SUB-TOTAL"
    cells[(last + 1, 6)] = Fx(f"=SUM(F3:F{last})", total)
    return cells


def _gold(tmp_path: Path, template: tuple[_Line, ...], bids: dict[str, tuple[_Line, ...]]) -> dict:
    _save(tmp_path / "template.xlsx", {"1. WORKS": _ref_bill(template, template=True)})
    for name, lines in bids.items():
        _save(tmp_path / f"{name}.xlsx", {"1. WORKS": _ref_bill(lines)})
    return ref.build_gold(tmp_path, tmp_path / "template.xlsx")


_DIG = ("1.1", "Trench excavation", "m3")


def test_a_blank_line_filled_in_without_a_price_is_listed_unpriced(tmp_path: Path) -> None:
    sup = ("1.2", "Site supervision, months", "month")
    gold = _gold(
        tmp_path,
        ((*_DIG, 10.0, None, None), (*sup, None, None, None)),
        {
            "A": ((*_DIG, 10.0, 110.0, 1100.0), (*sup, 6.0, None, None)),
            "B": ((*_DIG, 10.0, 100.0, 1000.0), (*sup, 6.0, 3000.0, 18000.0)),
            "C": ((*_DIG, 10.0, 105.0, 1050.0), (*sup, 6.0, 2900.0, None)),  # qty x rate
        },
    )
    a, b, c = (gold["bids"][n] for n in "ABC")
    assert a["filled_blank"] == [
        {"sheet": "1. WORKS", "item": "1.2", "bid_qty": 6.0, "amount": 0.0}
    ]
    assert [u["item"] for u in a["unpriced_positions"]] == ["1.2"]
    assert a["position_costs"]["1. WORKS|4"] == 0.0
    assert b["position_costs"]["1. WORKS|4"] == 18000.0
    assert c["position_costs"]["1. WORKS|4"] == pytest.approx(17400.0)
    assert not b["unpriced_positions"] and not c["unpriced_positions"]
    assert [x["item"] for x in gold["common_scope"]] == ["1.1"]


def test_text_in_a_filled_in_blank_line_leaves_it_unpriced_at_no_cost(tmp_path: Path) -> None:
    plaster = ("1.2", "Cement plaster to walls", "m2")
    gold = _gold(
        tmp_path,
        ((*_DIG, 10.0, None, None), (*plaster, None, None, None)),
        {
            "A": ((*_DIG, 10.0, 100.0, 1000.0), (*plaster, 420.0, "incl.", 4095.0)),
            "B": ((*_DIG, 10.0, 100.0, 1000.0), (*plaster, 420.0, 9.75, "n/a")),
        },
    )
    a, b = gold["bids"]["A"], gold["bids"]["B"]
    assert [x["amount"] for x in a["filled_blank"]] == [4095.0]
    assert [x["amount"] for x in b["filled_blank"]] == [0.0]
    for g in (a, b):
        assert g["position_costs"]["1. WORKS|4"] == 0.0
        assert g["evaluated_cost"] == 1000.0
        assert [u["item"] for u in g["unpriced_positions"]] == ["1.2"]


def test_a_template_quantity_typed_as_text_is_evaluated_for_no_bidder(tmp_path: Path) -> None:
    rebar = ("1.2", "Reinforcement B500", "t")
    gold = _gold(
        tmp_path,
        ((*_DIG, 10.0, None, None), (*rebar, "2.4 t", None, None)),
        {
            "A": ((*_DIG, 10.0, 110.0, 1100.0), (*rebar, "2.4 t", None, 3480.0)),
            "B": ((*_DIG, 10.0, 100.0, 1000.0), (*rebar, 3.0, 1450.0, 4350.0)),
        },
    )
    for name in "AB":
        g = gold["bids"][name]
        assert g["position_costs"]["1. WORKS|4"] == 0.0
        assert not g["filled_blank"] and not g["unpriced_positions"]
    assert [x["item"] for x in gold["common_scope"]] == ["1.1"]
