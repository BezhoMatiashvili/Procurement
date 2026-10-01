"""The independent BOQ gold writer (scripts/boq_reference.py) follows the lead's round-4 policies.

GOLD_SPEC 5 now says: a bid description keeping the template's whole with the offered make or
model after it is the position; an item number stored as a number ("1.10" read back as 1.1) is
the position; a row with its own amount product is a position whatever its quantity adds; LS
and lump sum are one unit; a single-sheet schedule with no Summary is ranked on the rows under
its bill. The reference must write the same gold, or the scores would measure a disagreement
between two readers rather than the product. Synthetic data only.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.unit.test_boq_reader_variants import Cells, Fx, _save

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ref = _load("boq_reference")


def _row(row: int, item: str, desc: str, **kw: object) -> object:
    fields: dict[str, object] = {"qty": 2.0, "qty_formula": None, "unit": "PCS", **kw}
    return ref.Row(row=row, item=item, desc=desc, **fields)


def test_the_offered_model_after_the_template_description_keeps_the_position() -> None:
    t = _row(3, "1", "Laser printer A4 mono")
    b = _row(3, "1", "Laser printer A4 mono / LP-2040 (brand Y)")
    matched, ratio = ref.match_rows([t], [b])
    assert matched == {3: b}
    assert ratio[3] < 0.9  # listed as a description change


@pytest.mark.parametrize(
    ("template", "bid", "bid_item"),
    [
        ("Pipe 20", "Pipe 200 mm", "1"),  # the text runs on into another word
        ("UPS 1500 VA", "Line-interactive UPS 1500 VA", "1"),  # not where the template starts
        ("Laser printer A4 mono", "Laser printer A4 mono / LP-2040", "7"),  # another item
    ],
)
def test_a_description_only_starting_like_the_templates_is_not_the_position(
    template: str, bid: str, bid_item: str
) -> None:
    matched, _ = ref.match_rows([_row(3, "1", template)], [_row(3, bid_item, bid)])
    assert matched == {}


def test_an_item_number_stored_as_a_number_keeps_the_position() -> None:
    t = _row(12, "1.10", "Work item number 10 described in full")
    b = _row(12, "1.1", "Work item number 10 described in full", numeric_item=True)
    retyped = _row(12, "1.1", "Work item number 10 described in full")
    other = _row(12, "1.1", "Another work item altogether", numeric_item=True)
    assert ref.match_rows([t], [b])[0] == {12: b}
    assert ref.match_rows([t], [retyped])[0] == {}  # typed as text: a renumbering
    assert ref.match_rows([t], [other])[0] == {}


@pytest.mark.parametrize(("a", "b"), [("LS", "Lump sum"), ("L.S.", "lump-sum"), ("ls", "LUMPSUM")])
def test_the_lump_sums_names_are_one_unit(a: str, b: str) -> None:
    assert ref.unit_key(a) == ref.unit_key(b)


@pytest.mark.parametrize("other", ["item", "l", "set", "pcs", "each"])
def test_the_lump_sum_stays_apart_from_counts_and_litres(other: str) -> None:
    assert ref.unit_key("Lump sum") != ref.unit_key(other)


def test_a_row_with_its_own_amount_is_a_position_whatever_its_quantity_adds() -> None:
    linked = _row(7, "1.6", "PAINTING", qty_formula="=D4+D5", amount_formula="=D7*E7")
    parent = _row(8, "2", "LIGHTING", qty_formula="=SUM(D9:D10)")
    sheet = ref.Sheet(
        name="1. WORKS",
        cols={},
        header_row=1,
        number_row=2,
        subtotal_row=None,
        subtotal_value=None,
        subtotal_formula=None,
        rows=[linked, parent],
    )
    ref.classify_template(sheet)
    assert (linked.kind, parent.kind) == ("position", "parent")


def _schedule(factor: float | None) -> Cells:
    """A single-sheet schedule the reference reads (a column-number row under the header), no
    Summary: two positions, TOTAL, VAT 18%, TOTAL INCL. VAT."""
    head = ("No", "Description", "Unit", "Quantity", "Rate in USD", "Amount")
    cells: Cells = {(1, c): h for c, h in enumerate(head, 1)}
    cells.update({(2, c): c for c in range(1, 7)})
    total = 0.0
    for r, (item, desc, qty, rate) in enumerate(
        (("1", "Laser printer A4 mono", 6.0, 520.0), ("2", "UPS 1500 VA", 8.0, 330.0)), 3
    ):
        amount = qty * rate * (factor or 0.0)
        cells.update({(r, 1): item, (r, 2): desc, (r, 3): "pcs", (r, 4): qty})
        if factor is not None:
            cells[(r, 5)] = rate * factor
        cells[(r, 6)] = Fx(f"=D{r}*E{r}", amount)
        total += amount
    rows = (("TOTAL", "=SUM(F3:F4)", total), ("VAT 18%", "=F5*0.18", total * 0.18))
    rows += (("TOTAL INCL. VAT", "=F5+F6", total * 1.18),)
    for r, (label, formula, value) in enumerate(rows, 5):
        cells[(r, 2)] = label
        cells[(r, 6)] = Fx(formula, value)
    return cells


def test_a_single_sheet_schedule_is_ranked_on_the_rows_under_its_bill(tmp_path: Path) -> None:
    for name, factor in (("template", None), ("Bidder A", 1.0), ("Bidder B", 0.9)):
        _save(tmp_path / f"{name}.xlsx", {"1. GOODS": _schedule(factor)})
    gold = ref.build_gold(tmp_path, tmp_path / "template.xlsx")
    cost = 6.0 * 520.0 + 8.0 * 330.0
    a, b = gold["bids"]["Bidder A"], gold["bids"]["Bidder B"]
    assert a["stated"] == {"total_cost": cost, "excl_vat": None, "incl_vat": round(cost * 1.18, 2)}
    assert a["markup_factor"] == pytest.approx(1.18)
    assert a["evaluated_cost"] == pytest.approx(cost)
    assert b["evaluated_total"] == pytest.approx(round(cost * 0.9 * 1.18, 2))
    assert gold["colours"] == {"red": ["Bidder A"], "green": ["Bidder B"]}
