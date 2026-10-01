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

from tests.unit.test_boq_reader_round5 import _save_hidden
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


def _sheet_of(rows: list[tuple[int, str, str]]) -> object:
    """A reference sheet of (row, item, kind) rows."""
    return ref.Sheet(
        name="1. WORKS",
        cols={},
        header_row=1,
        number_row=2,
        subtotal_row=None,
        subtotal_value=None,
        subtotal_formula=None,
        rows=[
            ref.Row(row=r, item=item, desc="text", qty=1.0, qty_formula=None, unit="", kind=kind)
            for r, item, kind in rows
        ],
    )


@pytest.mark.parametrize("heading_item", ["", "2", "B"])
def test_a_heading_numbered_otherwise_ends_the_section_group(heading_item: str) -> None:
    sheet = _sheet_of(
        [
            (3, "A1", "position"),
            (4, "A2", "position"),
            (5, heading_item, "heading"),
            (6, "2.1", "position"),
            (7, "2.2", "position"),
        ]
    )
    groups = ref.section_groups(sheet)
    assert (groups[4], groups[6], groups[7]) == ("A2", "r6", "r7")


def test_a_heading_numbered_under_the_section_keeps_the_group() -> None:
    sheet = _sheet_of(
        [
            (3, "A9", "position"),
            (4, "9.1", "heading"),
            (5, "9.1.1", "position"),
            (6, "9.2", "position"),
        ]
    )
    groups = ref.section_groups(sheet)
    assert [groups[r] for r in (3, 5, 6)] == ["A9", "A9", "A9"]


def _match(t_item: str, b_item: str) -> bool:
    t = ref.Row(row=3, item=t_item, desc="Trench excavation", qty=2.0, qty_formula=None, unit="m3")
    b = ref.Row(row=3, item=b_item, desc="Trench excavation", qty=2.0, qty_formula=None, unit="m3")
    return ref.match_rows([t], [b])[0] == {3: b}


@pytest.mark.parametrize(
    ("t_item", "b_item", "same"),
    [("1", "1.", True), ("1.1", "1.1.", True), ("1.1", "1.10", False), ("1", "11", False)],
)
def test_only_a_closing_dot_is_folded_from_an_item_number(
    t_item: str, b_item: str, same: bool
) -> None:
    assert _match(t_item, b_item) is same


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("კაც/სთ", "კაც.სთ"),
        ("კაც/სთ", "კაც-სთ"),
        ("შტ", "ც"),
        ("შტ.", "ცალი"),
        ("kW", "კვტ"),
        ("month", "მთვ"),
        ("m", "გრ.მ"),
    ],
)
def test_the_usual_georgian_unit_spellings_are_one_unit(a: str, b: str) -> None:
    assert ref.unit_key(a) == ref.unit_key(b)


@pytest.mark.parametrize(
    ("a", "b"), [("კაც/სთ", "სთ"), ("კვტ", "კვტ/სთ"), ("შტ", "LS"), ("მთვ", "დღე")]
)
def test_units_the_georgian_spellings_do_not_merge(a: str, b: str) -> None:
    assert ref.unit_key(a) != ref.unit_key(b)


@pytest.mark.parametrize("hidden", [True, False])
def test_a_hidden_draft_sheet_no_summary_links_is_no_section(tmp_path: Path, hidden: bool) -> None:
    dig = ("1.1", "Trench excavation", "m3")

    def book(name: str, live: float | None, draft: float | None) -> None:
        def lines(rate: float | None) -> tuple[_Line, ...]:
            return ((*dig, 10.0, rate, None if rate is None else 10.0 * rate),)

        sheets = {
            "1. WORKS": _ref_bill(lines(live), template=live is None),
            "1. WORKS (old)": _ref_bill(lines(draft), template=draft is None),
        }
        _save_hidden(tmp_path / f"{name}.xlsx", sheets, {"1. WORKS (old)"} if hidden else set())

    book("template", None, None)
    book("A", 110.0, 95.0)
    book("B", 100.0, None)
    gold = ref.build_gold(tmp_path, tmp_path / "template.xlsx")
    a, b = gold["bids"]["A"], gold["bids"]["B"]
    if hidden:
        assert list(gold["positions"]) == ["1. WORKS"]
        assert b["unpriced_sheets"] == []
        assert [(h["sheet"], h["item"]) for h in a["heading_priced"]] == [("1. WORKS (old)", "1.1")]
        assert a["evaluated_cost"] == 1100.0
    else:  # near miss: a visible second bill is a section
        assert b["unpriced_sheets"] == ["1. WORKS (old)"]
