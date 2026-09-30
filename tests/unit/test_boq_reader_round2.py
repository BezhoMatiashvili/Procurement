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
    _PLACEHOLDER,
    BoqLine,
    BoqWorkbook,
    _label_key,
    _num,
    _plays,
    _two_readings,
    compare_bid,
    grand_total,
    norm_unit,
    read_boq,
    reconcile,
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


# --- numbers typed as text with three decimals ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "readings"),
    [("1.250", (1.25, 1250.0)), ("5,000", (5.0, 5000.0)), ("-2.500", (-2.5, -2500.0))],
)
def test_text_with_three_digits_after_one_separator_reads_two_ways(
    text: str, readings: tuple[float, float]
) -> None:
    assert _two_readings(text) == readings
    # and is never guessed on its own: read as thousands, 1.25 t became 1,250 t
    assert _num(text) is None


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1,250.50", 1250.5),
        ("1.250,50", 1250.5),
        ("12.5", 12.5),
        ("0.125", 0.125),
        ("1.250.000", 1250000.0),
        ("12.3450", 12.345),
        ("1 250", 1250.0),
    ],
)
def test_text_that_reads_one_way_keeps_its_reading(text: str, value: float) -> None:
    assert _two_readings(text) is None
    assert _num(text) == pytest.approx(value)


_HEAD = ("No", "Description", "Unit", "Quantity", "Rate", "Amount")


def _text_qty_sheet(qty: object, rate: float | None, *, amount: float | None = None) -> Cells:
    """Position 1.1 with the given quantity and rate, its amount a formula cached as ``amount``
    (0 when not given), and position 1.2 at 10 x 100 (priced when ``rate`` is)."""
    cells: Cells = {(1, c): h for c, h in enumerate(_HEAD, 1)}
    cells.update({(2, 1): "1.1", (2, 2): "Reinforcement bars", (2, 3): "t", (2, 4): qty})
    if rate is not None:
        cells[(2, 5)] = rate
    cells[(2, 6)] = Fx("=D2*E2", amount if amount is not None else 0.0)
    cells.update({(3, 1): "1.2", (3, 2): "Concrete", (3, 3): "m3", (3, 4): 10.0})
    if rate is not None:
        cells[(3, 5)] = 100.0
    cells[(3, 6)] = Fx("=D3*E3", 1000.0 if rate is not None else 0.0)
    return cells


def _one_line(tmp_path: Path, name: str, cells: Cells) -> BoqLine:
    book = read_boq(_save(tmp_path / f"{name}.xlsx", {"1. WORKS": cells}), name=name)
    return book.sheets["1. WORKS"].lines[0]


def test_a_text_quantity_is_read_the_way_its_row_computes_it(tmp_path: Path) -> None:
    line = _one_line(tmp_path, "a", _text_qty_sheet("1.250", 1000.0, amount=1250.0))
    assert line.quantity == 1.25 and line.non_numeric == ()
    # the same text beside an amount computed with 1,250 is a thousand and a quarter
    line = _one_line(tmp_path, "b", _text_qty_sheet("1.250", 2.0, amount=2500.0))
    assert line.quantity == 1250.0 and line.non_numeric == ()


def test_a_text_rate_is_read_the_way_its_row_computes_it(tmp_path: Path) -> None:
    cells = _text_qty_sheet(2.0, None, amount=2900.0)
    cells[(2, 5)] = "1.450"
    line = _one_line(tmp_path, "r", cells)
    assert line.rate_total == 1450.0 and line.non_numeric == ()


@pytest.mark.parametrize(
    ("rate", "amount"),
    [
        (None, None),  # an unpriced row: 0 = 1.25 x nothing says nothing
        (1000.0, 777.0),  # an amount neither reading computes
    ],
)
def test_a_text_quantity_its_row_cannot_settle_stays_text(
    tmp_path: Path, rate: float | None, amount: float | None
) -> None:
    line = _one_line(tmp_path, "t", _text_qty_sheet("1.250", rate, amount=amount))
    assert line.quantity is None
    assert line.non_numeric == (("qty", "1.250"),)


def test_a_bid_quantity_typed_as_text_is_no_quantity_change(tmp_path: Path) -> None:
    """The template says 5; the bid's cell says "5.000" and its amount is 5 x 140."""
    t_cells = _text_qty_sheet(5.0, None)
    b_cells = _text_qty_sheet("5.000", 140.0, amount=700.0)
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": t_cells}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": b_cells}), name="B")
    comp = compare_bid(t, b)
    assert comp.quantity_changed == [] and comp.findings == []
    assert comp.evaluated_cost == pytest.approx(5 * 140.0 + 1000.0)


def test_a_template_quantity_its_row_cannot_settle_is_evaluated_for_no_bidder(
    tmp_path: Path,
) -> None:
    """An unpriced template's "1.250": never 1,250 t, and never the bidder's own amount as if
    the template had left the quantity blank."""
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _text_qty_sheet("1.250", None)}))
    b_cells = _text_qty_sheet("1.250", 1000.0, amount=1250.0)
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": b_cells}), name="B")
    notes = [f for f in template_findings(t) if f.rule_id == "BOQ.NON_NUMERIC"]
    assert len(notes) == 1 and "1.25 or 1,250" in notes[0].message_en
    comp = compare_bid(t, b)
    pos = next(p for p in comp.positions if p.item_no == "1.1")
    assert (pos.status, pos.cost) == ("non_numeric", 0.0)
    assert comp.filled_blank == [] and comp.unpriced_positions == []
    assert comp.evaluated_cost == pytest.approx(1000.0)
    assert "BOQ.ARITHMETIC" not in _rules(comp)


# --- placeholders: where the invitation stands and what it asks ---------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Undescribed items (Bidder to list and price)",
        "Any other undescribed items",
        "Items to be listed here by the tenderer",
        "The tenderer is to list below any additional work",
        "List here any additional items",
        "აღწერაში არ შეტანილი სამუშაოები",
        "სხვა ხარჯები",
    ],
)
def test_an_invitation_addressed_to_the_bidder_or_opening_the_text_is_a_placeholder(
    text: str,
) -> None:
    assert _PLACEHOLDER.search(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        # the listing is someone else's, or the word qualifies described work
        "Door and room signage, texts to be listed by the Engineer",
        "Removal of undescribed obstructions met in excavation, as instructed",
        "მობილიზაცია, დროებითი შენობები და სხვა ხარჯები",
        "Fixings listed here are to be galvanised",
        "Floor tiles 600x600, colour: bidder to specify",
        "Contractor to list defects at handover and make good",
    ],
)
def test_the_same_words_further_into_a_described_position_are_no_invitation(text: str) -> None:
    assert _PLACEHOLDER.search(text) is None


def _heading_sheet(heading: str, prices: dict[str, tuple[float, float]]) -> Cells:
    """A group row over two blank-quantity lines, then a closing Sub-total and one more
    position, then a described position; ``prices`` gives (qty, rate) for the lines priced.

    A1 | <heading>                       | amount =SUM(F3:F4)
    1.1 | Item 1   | item | (blank)      | amount =D3*E3
    1.2 | Item 2   | item | (blank)      | amount =D4*E4
        | Sub-total                      | =SUM(F3:F4)
    1.3 | Excavation | m3 | 10 | 50      | =D6*E6
    """
    cells: Cells = {(1, c): h for c, h in enumerate(_HEAD, 1)}
    cells.update({(2, 1): "A1", (2, 2): heading})
    subtotal = 0.0
    for r, item in ((3, "1.1"), (4, "1.2")):
        cells.update({(r, 1): item, (r, 2): f"Item {r - 2}", (r, 3): "item"})
        qty, rate = prices.get(item, (None, None))
        if qty is not None and rate is not None:
            cells.update({(r, 4): qty, (r, 5): rate})
            subtotal += qty * rate
        cells[(r, 6)] = Fx(f"=D{r}*E{r}", (qty or 0.0) * (rate or 0.0))
    cells[(2, 6)] = Fx("=SUM(F3:F4)", subtotal)
    cells.update({(5, 2): "Sub-total", (5, 6): Fx("=SUM(F3:F4)", subtotal)})
    cells.update({(6, 1): "1.3", (6, 2): "Excavation", (6, 3): "m3", (6, 4): 10.0})
    qty, rate = prices.get("1.3", (10.0, 0.0))
    cells.update({(6, 5): rate, (6, 6): Fx("=D6*E6", 10.0 * rate)})
    return cells


@pytest.mark.parametrize(
    "heading", ["Undescribed items (Bidder to list and price)", "აღწერაში არ შეტანილი სამუშაოები"]
)
def test_a_line_under_an_inviting_heading_is_a_placeholder(tmp_path: Path, heading: str) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _heading_sheet(heading, {})}))
    bid = _heading_sheet(heading, {"1.1": (2.0, 600.0), "1.3": (10.0, 50.0)})
    comp = compare_bid(t, read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": bid}), name="B"))
    status = {p.item_no: (p.status, p.cost) for p in comp.positions}
    assert status["1.1"] == ("optional_priced", 0.0)
    assert status["1.2"][0] == "optional"
    assert status["1.3"] == ("priced", 500.0)
    assert comp.filled_blank == [] and comp.evaluated_cost == pytest.approx(500.0)
    assert "BOQ.OPTIONAL_PRICED" in _rules(comp) and "BOQ.FILLED_BLANK" not in _rules(comp)


@pytest.mark.parametrize("heading", ["Sundry items / სხვადასხვა ხარჯები", "Earthworks"])
def test_a_line_under_a_plain_heading_is_the_bidders_own_filled_in_quantity(
    tmp_path: Path, heading: str
) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _heading_sheet(heading, {})}))
    bid = _heading_sheet(heading, {"1.1": (2.0, 600.0), "1.3": (10.0, 50.0)})
    comp = compare_bid(t, read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": bid}), name="B"))
    status = {p.item_no: (p.status, p.cost) for p in comp.positions}
    assert status["1.1"] == ("filled_blank", 1200.0)
    assert comp.priced_optional == []


def test_an_inviting_headings_reach_ends_at_the_sub_total_that_closes_it(tmp_path: Path) -> None:
    """1.3 stands after the group's Sub-total: a described position, priced and evaluated."""
    heading = "Any other undescribed items"
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _heading_sheet(heading, {})}))
    bid = _heading_sheet(heading, {"1.3": (10.0, 50.0)})
    comp = compare_bid(t, read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": bid}), name="B"))
    status = {p.item_no: (p.status, p.cost) for p in comp.positions}
    assert status["1.3"] == ("priced", 500.0)
    assert status["1.1"][0] == status["1.2"][0] == "optional"
    assert comp.unfilled_positions == []


@pytest.mark.parametrize(
    "text",
    [
        "Door and room signage, texts to be listed by the Engineer",
        "Removal of undescribed obstructions met in excavation, as instructed",
    ],
)
def test_a_described_position_mentioning_listing_is_evaluated(tmp_path: Path, text: str) -> None:
    cells = _heading_sheet("Earthworks", {"1.3": (10.0, 0.0)})
    cells[(6, 2)] = text
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": cells}))
    bid = _heading_sheet("Earthworks", {"1.3": (10.0, 90.0)})
    bid[(6, 2)] = text
    comp = compare_bid(t, read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": bid}), name="B"))
    status = {p.item_no: (p.status, p.cost) for p in comp.positions}
    assert status["1.3"] == ("priced", 900.0)
    assert comp.priced_optional == []


# --- the row that closes a sheet --------------------------------------------------------------


def _closed_sheet(rows: list[tuple[str, object, float]], *, linked: int | None) -> dict[str, Cells]:
    """Positions 1.1 (10 x 100), 1.2 (5 x 20), 2.1 (5 x 40) in rows 2, 3 and 5 of "1. WORKS",
    then ``rows`` as (label, formula, cached value) from row 4 on, skipping row 5; with
    ``linked``, a Summary whose WORKS row links to that row."""
    cells: Cells = {(1, c): h for c, h in enumerate(_HEAD, 1)}
    for r, item, text, unit, qty, rate in (
        (2, "1.1", "Plaster", "m2", 10.0, 100.0),
        (3, "1.2", "Paint", "m2", 5.0, 20.0),
        (5, "2.1", "Skirting", "lm", 5.0, 40.0),
    ):
        cells.update({(r, 1): item, (r, 2): text, (r, 3): unit, (r, 4): qty, (r, 5): rate})
        cells[(r, 6)] = Fx(f"=D{r}*E{r}", qty * rate)
    for r, (label, formula, value) in zip((4, 6, 7, 8), rows, strict=False):
        cells.update({(r, 2): label, (r, 6): Fx(str(formula), value)})
    sheets: dict[str, Cells] = {"1. WORKS": cells}
    if linked is not None:
        total = cells[(linked, 6)]
        assert isinstance(total, Fx)
        sheets["SUMMARY"] = {
            (1, 1): "ITEM",
            (1, 2): "DESCRIPTION",
            (1, 3): "TOTAL",
            (2, 1): "1",
            (2, 2): "WORKS",
            (2, 3): Fx(f"='1. WORKS'!F{linked}", total.value),
            (3, 2): "TOTAL COST",
            (3, 3): Fx("=C2", total.value),
        }
    return sheets


@pytest.mark.parametrize(
    ("section_1", "section_2", "grand"),
    [
        ("Sub-total Section 1", "Sub-total Section 2", "TOTAL CARRIED TO SUMMARY"),
        ("ქვეჯამი I თავი", "ქვეჯამი II თავი", "სულ ჯამი"),
    ],
)
@pytest.mark.parametrize("linked", [7, None])
def test_sections_closed_by_sub_totals_under_a_grand_total_reconcile_against_the_grand_total(
    tmp_path: Path, section_1: str, section_2: str, grand: str, linked: int | None
) -> None:
    rows = [
        (section_1, "=SUM(F2:F3)", 1100.0),
        (section_2, "=SUM(F5:F5)", 200.0),
        (grand, "=F4+F6", 1300.0),
    ]
    book = read_boq(_save(tmp_path / "b.xlsx", _closed_sheet(rows, linked=linked)), name="B")
    sheet = book.sheets["1. WORKS"]
    assert (sheet.subtotal_row, sheet.subtotal) == (7, 1300.0)
    assert reconcile(sheet) is None
    assert reconcile_summary(book) == []


def test_one_sub_total_stays_the_closing_row_under_a_total_that_adds_more_to_it(
    tmp_path: Path,
) -> None:
    """SUB-TOTAL, a contingency on it, and the TOTAL the Summary links to: the positions add up
    to the SUB-TOTAL, not to the TOTAL with the contingency."""
    sheets = _closed_sheet([], linked=None)
    works = sheets["1. WORKS"]
    works.update({(6, 2): "SUB-TOTAL", (6, 6): Fx("=SUM(F2:F5)", 1300.0)})
    works.update({(7, 2): "Contingency 5%", (7, 6): Fx("=F6*0.05", 65.0)})
    works.update({(8, 2): "TOTAL", (8, 6): Fx("=F6+F7", 1365.0)})
    sheets["SUMMARY"] = {
        (1, 1): "ITEM",
        (1, 2): "DESCRIPTION",
        (1, 3): "TOTAL",
        (2, 1): "1",
        (2, 2): "WORKS",
        (2, 3): Fx("='1. WORKS'!F8", 1365.0),
    }
    sheet = read_boq(_save(tmp_path / "b.xlsx", sheets), name="B").sheets["1. WORKS"]
    assert (sheet.subtotal_row, sheet.subtotal) == (6, 1300.0)
    assert reconcile(sheet) is None
