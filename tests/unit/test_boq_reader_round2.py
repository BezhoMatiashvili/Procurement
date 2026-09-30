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
    BoqWorkbook,
    _label_key,
    _plays,
    compare_bid,
    grand_total,
    norm_unit,
    read_boq,
    reconcile_summary,
    stated_total_cost,
    summary_roles,
    template_findings,
)
from tests.boq_factory import CIVIL, Bid, build_real_boq
from tests.unit.test_boq_reader_variants import (
    Cells,
    Fx,
    _row,
    _rules,
    _save,
    _summary_book,
)

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
    rows = [
        ("1.1", "Trench excavation", "m3", 50.0, 12.0),
        ("1.2", "Sand bedding", "m3", 8.0, 30.0),
    ]
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


# --- Summary rows worded another usual way ----------------------------------------------------


@pytest.mark.parametrize(
    ("role", "label"),
    [
        ("cost", "Total cost, excl. VAT"),
        ("cost", "TOTAL COST (EXCL. VAT)"),
        ("cost", "Total cost without VAT"),
        ("incl", "GRAND TOTAL WITH VAT"),
        ("incl", "Total price incl. 18% VAT"),
        ("incl", "Total including value added tax"),
        ("incl", "საერთო ღირებულება დღგ-ის ჩათვლით"),
        ("incl", "სულ დღგ-ს ჩათვლით"),
        ("incl", "სულ ჯამი დამატებული ღირებულების გადასახადის ჩათვლით"),
        ("excl", "Total price before VAT"),
        ("excl", "სულ დღგ-ის გარეშე"),
        ("vat", "Value added tax @ 18%"),
        ("vat", "Add 18% VAT"),
        ("vat", "18% VAT"),
        ("vat", "დამატებული ღირებულების გადასახადი 18%"),
        ("vat", "დღგ-ის თანხა"),
        ("vat", "VAT (18%)"),
    ],
)
def test_a_summary_row_worded_another_usual_way_plays_its_part(role: str, label: str) -> None:
    assert _plays(role, label)


@pytest.mark.parametrize(
    ("role", "label"),
    [
        ("cost", "Total cost incl. VAT"),
        ("incl", "Total without VAT"),
        ("incl", "TOTAL PRICE EXCL. VAT"),
        ("incl", "VAT 18%"),
        ("incl", "Private works with valuation"),
        ("vat", "Total price incl. 18% VAT"),
        ("vat", "Total price excl. VAT"),
        ("vat", "Vatican road works"),
        ("vat", "Private garden"),
        ("excl", "GRAND TOTAL WITH VAT"),
    ],
)
def test_a_summary_row_does_not_play_a_part_its_words_only_resemble(role: str, label: str) -> None:
    assert not _plays(role, label)


def _chain(*rows: tuple[str, float, float | None]) -> BoqWorkbook:
    """A Summary: one section of 1,000.00 and then ``rows`` as (label, value, rate)."""
    out = [_row(9, "1", "WORKS", 1000.0)]
    out += [
        _row(10 + i, "", label, value, rate=rate) for i, (label, value, rate) in enumerate(rows)
    ]
    return _summary_book(out)


@pytest.mark.parametrize(
    "cost_label", ["Total cost, excl. VAT", "TOTAL COST (EXCL. VAT)", "Total cost without VAT"]
)
def test_a_total_cost_row_that_says_excl_vat_is_the_total_cost(cost_label: str) -> None:
    book = _chain(
        (cost_label, 1000.0, None),
        ("Overheads", 100.0, 0.10),
        ("Profit", 50.0, 0.05),
        ("TOTAL PRICE EXCL. VAT", 1150.0, None),
        ("VAT", 207.0, 0.18),
        ("TOTAL PRICE INCLUDING VAT", 1357.0, None),
    )
    assert {k: r.row for k, r in summary_roles(book).items()} == {
        "cost": 10,
        "overheads": 11,
        "profit": 12,
        "excl": 13,
        "vat": 14,
        "incl": 15,
    }
    assert stated_total_cost(book) == 1000.0 and grand_total(book) == 1357.0
    assert reconcile_summary(book) == []


def test_a_later_total_cost_excl_vat_row_is_the_excl_vat_total() -> None:
    book = _chain(
        ("TOTAL COST", 1000.0, None),
        ("Overheads", 100.0, 0.10),
        ("Total cost excl. VAT", 1100.0, None),
        ("VAT", 198.0, 0.18),
        ("Total incl. VAT", 1298.0, None),
    )
    roles = summary_roles(book)
    assert (roles["cost"].row, roles["excl"].row) == (10, 12)
    assert reconcile_summary(book) == []


def test_vat_is_held_against_a_total_cost_row_that_says_excl_vat_when_nothing_else_does() -> None:
    faithful = _chain(
        ("Total cost, excl. VAT", 1000.0, None),
        ("VAT 18%", 180.0, 0.18),
        ("Total incl. VAT", 1180.0, None),
    )
    assert reconcile_summary(faithful) == []
    wrong = _chain(
        ("Total cost, excl. VAT", 1000.0, None),
        ("VAT 18%", 170.0, 0.18),
        ("Total incl. VAT", 1170.0, None),
    )
    [gap] = reconcile_summary(wrong)
    assert "row 11" in gap.message_en and "row 10" in gap.message_en


@pytest.mark.parametrize(
    ("vat_label", "incl_label"),
    [
        ("Value added tax @ 18%", "TOTAL PRICE INCLUDING VAT"),
        ("Add 18% VAT", "GRAND TOTAL WITH VAT"),
        ("დამატებული ღირებულების გადასახადი 18%", "საერთო ღირებულება დღგ-ის ჩათვლით"),
        ("VAT", "Total price incl. 18% VAT"),
    ],
)
def test_a_chain_worded_another_usual_way_reconciles_and_ranks(
    vat_label: str, incl_label: str
) -> None:
    book = _chain(
        ("TOTAL COST", 1000.0, None),
        ("TOTAL PRICE EXCL. VAT", 1000.0, None),
        (vat_label, 180.0, 0.18),
        (incl_label, 1180.0, None),
    )
    roles = summary_roles(book)
    assert (roles["vat"].row, roles["incl"].row) == (12, 13)
    assert grand_total(book) == 1180.0
    assert reconcile_summary(book) == []


def test_a_bid_whose_summary_states_no_total_incl_vat_says_why_it_is_not_ranked(
    tmp_path: Path,
) -> None:
    t_path, b_path = tmp_path / "t.xlsx", tmp_path / "b.xlsx"
    t_path.write_bytes(build_real_boq())
    b_path.write_bytes(build_real_boq(Bid()))
    t, b = read_boq(t_path, name="T"), read_boq(b_path, name="B")
    incl = summary_roles(b)["incl"]
    b.summary_rows = [r for r in b.summary_rows if r.row != incl.row]
    comp = compare_bid(t, b)
    assert comp.markup_factor is None
    [note] = [f for f in comp.findings if f.rule_id == "BOQ.NOT_RANKED"]
    assert note.severity == "warning" and "incl. VAT" in note.message_en
    # a Summary with its whole chain says nothing of the kind
    assert "BOQ.NOT_RANKED" not in _rules(compare_bid(t, read_boq(b_path, name="B")))


# --- component column groups ------------------------------------------------------------------

_COMPONENT_LABELS = {
    "en": (
        "No",
        "Description",
        "Unit",
        "Quantity",
        "Materials",
        "Labour",
        "Total",
        "Rate",
        "Amount",
    ),
    "ka": (
        "№",
        "დასახელება",
        "განზ.",
        "რაოდენობა",
        "მასალა",
        "ხელფასი",
        "ჯამი",
        "ერთ. ფასი",
        "სულ",
    ),
}


def _component_sheet(labels: tuple[str, ...], split: tuple[float, float] | None) -> Cells:
    """Each component a group over its own rate and amount, and a last column totalling the line:

    No | Description | Unit | Qty | Materials (E:F)  | Labour (G:H)     | Total
                                  | Rate  | Amount   | Rate  | Amount   |
    """
    no, desc, unit, qty, mat, lab, tot, rate, amount = labels
    cells: Cells = {(1, 1): no, (1, 2): desc, (1, 3): unit, (1, 4): qty}
    cells.update({(1, 5): mat, (1, 7): lab, (1, 9): tot})
    cells.update({(2, 5): rate, (2, 6): amount, (2, 7): rate, (2, 8): amount})
    total = 0.0
    for r, (item, text, u, q, price) in enumerate(
        [("1.1", "Wall plaster", "m2", 100.0, 10.0), ("1.2", "Floor screed", "m2", 40.0, 25.0)],
        start=3,
    ):
        cells.update({(r, 1): item, (r, 2): text, (r, 3): u, (r, 4): q})
        m, lb = (price * split[0], price * split[1]) if split else (0.0, 0.0)
        if split:
            cells.update({(r, 5): m, (r, 7): lb})
        cells[(r, 6)] = Fx(f"=D{r}*E{r}", q * m)
        cells[(r, 8)] = Fx(f"=D{r}*G{r}", q * lb)
        cells[(r, 9)] = Fx(f"=F{r}+H{r}", q * (m + lb))
        total += q * (m + lb)
    cells[(5, 2)] = tot
    cells[(5, 9)] = Fx("=SUM(I3:I4)", total)
    return cells


@pytest.mark.parametrize("lang", ["en", "ka"])
def test_a_component_group_binds_its_own_rate_and_amount(tmp_path: Path, lang: str) -> None:
    """ "Materials" over Rate | Amount is the materials rate and sum, not the unit rate: read as
    the unit rate, the materials alone priced the line and ten ARITHMETIC errors followed."""
    labels = _COMPONENT_LABELS[lang]
    t_cells = _component_sheet(labels, None)
    b_cells = _component_sheet(labels, (0.7, 0.3))
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": t_cells}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": b_cells}), name="B")
    assert t.sheets["1. WORKS"].columns == {
        "no": 1,
        "desc": 2,
        "unit": 3,
        "qty": 4,
        "rate_materials": 5,
        "sum_materials": 6,
        "rate_labor": 7,
        "sum_labor": 8,
        "amount": 9,
    }
    comp = compare_bid(t, b)
    assert comp.evaluated_cost == pytest.approx(100 * 10.0 + 40 * 25.0)
    assert comp.findings == []


def test_a_cheaper_materials_rate_does_not_make_a_dearer_bid_cheapest(tmp_path: Path) -> None:
    labels = _COMPONENT_LABELS["en"]
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _component_sheet(labels, None)}))
    a = _save(tmp_path / "a.xlsx", {"1. WORKS": _component_sheet(labels, (0.7, 0.3))})
    c = _save(tmp_path / "c.xlsx", {"1. WORKS": _component_sheet(labels, (0.5, 0.6))})
    cost = {n: compare_bid(t, read_boq(p, name=n)).evaluated_cost for n, p in (("A", a), ("C", c))}
    assert cost["C"] == pytest.approx(cost["A"] * 1.1)


def test_the_client_style_header_block_keeps_its_columns(tmp_path: Path) -> None:
    """A group label over Materials | Materials Sum | ... | Rate | Amount, then a column-number
    row: the layout the reader was written for keeps its column map."""
    path = tmp_path / "t.xlsx"
    path.write_bytes(build_real_boq())
    sheet = read_boq(path, name="T").sheets[CIVIL]
    assert sheet.columns == {
        "no": 1,
        "desc": 3,
        "qty": 7,
        "unit": 8,
        "rate_materials": 9,
        "sum_materials": 10,
        "rate_labor": 11,
        "sum_labor": 12,
        "rate_delivery": 13,
        "sum_delivery": 14,
        "rate_total": 15,
        "amount": 16,
    }
