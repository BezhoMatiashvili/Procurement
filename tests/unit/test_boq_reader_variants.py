"""BOQ layouts and Summaries worded otherwise than the client's template.

Each test is one way a real bill differs from the template the reader was first written for: a
group header over rate and amount sub-columns, a quantity split per building, a closing row not
called SUB-TOTAL, a Summary lettered A, B, C or written the Georgian way, a unit spelled in
Cyrillic, a column inserted into the item-number block. Every workbook is synthetic and built
in memory; every expected number is worked out here from the test's own data.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from openpyxl import Workbook

import tests.boq_factory as factory
from procurement_agent.checks import boq
from procurement_agent.checks.boq import (
    BoqLine,
    BoqSheet,
    BoqWorkbook,
    SummaryRow,
    compare_all,
    compare_bid,
    grand_total,
    norm_unit,
    read_bid,
    read_boq,
    reconcile,
    reconcile_summary,
    stated_total_cost,
    summary_roles,
    template_findings,
    write_merged_workbook,
)
from tests.boq_factory import CIVIL, Bid, build_real_boq

#: overheads 10% + profit 5% on the cost, then 18% VAT - the factory's defaults
MARKUP = 1.15 * 1.18


@dataclass(frozen=True)
class Fx:
    """A formula and the value a spreadsheet would have calculated for it."""

    text: str
    value: float | str


Cells = dict[tuple[int, int], object]


def _save(path: Path, sheets: dict[str, Cells], *, chart: str | None = None) -> Path:
    """Write the sheets, every ``Fx`` with its cached value, as Excel saves a workbook."""
    wb = Workbook()
    default = wb.active
    assert default is not None
    wb.remove(default)
    cache: dict[tuple[int, str], object] = {}
    for index, (name, cells) in enumerate(sheets.items()):
        ws = wb.create_sheet(name)
        for (r, c), v in cells.items():
            if isinstance(v, Fx):
                cell = ws.cell(r, c, v.text)
                cache[(index, cell.coordinate)] = v.value
            else:
                ws.cell(r, c, v)
    if chart is not None:
        # a chart sheet as Excel saves it: a sheet holding one chart and no cells
        from openpyxl.chart import BarChart, Reference

        plotted = BarChart()
        plotted.add_data(Reference(wb.worksheets[0], min_col=3, min_row=2, max_row=4))
        wb.create_chartsheet(chart).add_chart(plotted)
    buf = io.BytesIO()
    wb.save(buf)
    path.write_bytes(factory._with_cached_values(buf.getvalue(), cache))
    return path


def _rules(comp: object) -> list[str]:
    return [f.rule_id for f in comp.findings]  # type: ignore[attr-defined]


def _row(row: int, item: str, label: str, value: float | None, **kw: object) -> SummaryRow:
    return SummaryRow(row=row, item=item, label=label, value=value, **kw)  # type: ignore[arg-type]


def _summary_book(rows: list[SummaryRow]) -> BoqWorkbook:
    book = BoqWorkbook(name="B", summary_rows=rows, summary_sheet="SUMMARY")
    return book


# --- the Summary chain ------------------------------------------------------------------------


def test_each_summary_part_is_the_first_row_labelled_for_it_not_the_last() -> None:
    """A "TOTAL COST INCL. OVERHEADS & PROFIT" after the profit, and an advance payment "of the
    price incl. VAT" under the grand total, are neither the cost nor the grand total: taken from
    the last row labelled so, they turned the markup into 1.18 and 0.27 and flipped the colours.
    """
    book = _summary_book(
        [
            _row(9, "2", "CIVIL WORKS", 1000.0),
            _row(10, "", "TOTAL COST", 1000.0),
            _row(11, "", "Overheads", 100.0, rate=0.10),
            _row(12, "", "Profit", 50.0, rate=0.05),
            _row(13, "", "TOTAL COST INCL. OVERHEADS & PROFIT", 1150.0),
            _row(14, "", "TOTAL PRICE EXCL. VAT", 1150.0),
            _row(15, "", "VAT", 207.0, rate=0.18),
            _row(16, "", "TOTAL PRICE INCLUDING VAT", 1357.0),
            _row(18, "", "Advance payment 20% of the price incl. VAT", 271.4),
        ]
    )
    roles = summary_roles(book)
    assert {k: r.row for k, r in roles.items()} == {
        "cost": 10,
        "overheads": 11,
        "profit": 12,
        "excl": 14,
        "vat": 15,
        "incl": 16,
    }
    assert stated_total_cost(book) == 1000.0
    assert grand_total(book) == 1357.0
    # the restated running sum (row 13) is not added to the excl.-VAT total a second time
    assert reconcile_summary(book) == []


def test_a_section_named_like_a_chain_part_does_not_take_its_place() -> None:
    """Rows above TOTAL COST are sections: "Overhead line relocation" is work, not overheads."""
    book = _summary_book(
        [
            _row(9, "1", "Overhead line relocation", 400.0),
            _row(10, "2", "CIVIL WORKS", 600.0),
            _row(11, "", "TOTAL COST", 1000.0),
            _row(12, "", "Overheads", 100.0, rate=0.10),
            _row(13, "", "TOTAL PRICE EXCL. VAT", 1100.0),
            _row(14, "", "VAT", 198.0, rate=0.18),
            _row(15, "", "TOTAL PRICE INCLUDING VAT", 1298.0),
        ]
    )
    assert summary_roles(book)["overheads"].row == 12
    assert reconcile_summary(book) == []


def test_the_cost_is_the_last_total_cost_before_the_chain_goes_on() -> None:
    """A TOTAL COST per building, then the overall one: the chain runs from the overall one."""
    book = _summary_book(
        [
            _row(9, "1", "Building A", 600.0),
            _row(10, "", "TOTAL COST Building A", 600.0),
            _row(11, "2", "Building B", 400.0),
            _row(12, "", "TOTAL COST Building B", 400.0),
            _row(13, "", "TOTAL COST", 1000.0),
            _row(14, "", "Overheads", 100.0, rate=0.10),
            _row(15, "", "TOTAL PRICE EXCL. VAT", 1100.0),
            _row(16, "", "VAT", 198.0, rate=0.18),
            _row(17, "", "TOTAL PRICE INCLUDING VAT", 1298.0),
        ]
    )
    assert stated_total_cost(book) == 1000.0


def test_a_georgian_summary_chain_reconciles() -> None:
    """ზედნადები ხარჯები and გეგმიური დაგროვება (a Georgian estimate's overheads and profit) are
    part of the chain; the excl.-VAT total adds every row between it and TOTAL COST."""
    book = _summary_book(
        [
            _row(9, "1", "სამშენებლო სამუშაოები", 20000.0),
            _row(10, "", "ჯამური ღირებულება", 20000.0),
            _row(11, "", "ზედნადები ხარჯები", 2000.0, rate=0.10),
            _row(12, "", "გეგმიური დაგროვება", 1000.0, rate=0.05),
            _row(13, "", "სულ დღგ-ს გარეშე", 23000.0),
            _row(14, "", "დღგ", 4140.0, rate=0.18),
            _row(15, "", "სულ ჯამი დღგ-ს ჩათვლით", 27140.0),
        ]
    )
    assert summary_roles(book)["profit"].row == 12
    assert reconcile_summary(book) == []
    # and a profit that does not follow from its rate is still caught
    book.summary_rows[3] = _row(12, "", "გეგმიური დაგროვება", 1500.0, rate=0.05)
    book.summary_rows[4] = _row(13, "", "სულ დღგ-ს გარეშე", 23500.0)
    gaps = [f.message_en for f in reconcile_summary(book)]
    assert len(gaps) >= 1 and "row 12" in gaps[0]


@pytest.mark.parametrize("extra", ["Contingency 5%", "Indirect costs", "Margin"])
def test_any_row_between_total_cost_and_excl_vat_is_part_of_the_excl_vat_total(
    extra: str,
) -> None:
    book = _summary_book(
        [
            _row(9, "1", "WORKS", 1000.0),
            _row(10, "", "TOTAL COST", 1000.0),
            _row(11, "", extra, 50.0),
            _row(12, "", "TOTAL PRICE EXCL. VAT", 1050.0),
            _row(13, "", "VAT", 189.0, rate=0.18),
            _row(14, "", "TOTAL PRICE INCLUDING VAT", 1239.0),
        ]
    )
    assert reconcile_summary(book) == []


@pytest.mark.parametrize("items", [("A", "B"), ("A.", "B."), ("2", "3")])
def test_total_cost_is_held_against_the_top_level_sections_however_numbered(
    items: tuple[str, str],
) -> None:
    book = _summary_book(
        [
            _row(9, items[0], "CIVIL WORKS", 4000.0),
            _row(10, items[1], "DOORS", 6000.0),
            _row(11, "", "TOTAL COST", 7000.0),
        ]
    )
    [gap] = reconcile_summary(book)
    assert "row 11" in gap.message_en and "rows 9, 10" in gap.message_en
    assert gap.expected == "10,000.00" and gap.actual == "7,000.00"


def test_numbered_sub_sections_of_lettered_sections_are_not_added_twice() -> None:
    book = _summary_book(
        [
            _row(9, "A", "BUILDING WORKS", 1000.0),
            _row(10, "1", "Walls", 600.0),
            _row(11, "2", "Floors", 400.0),
            _row(12, "B", "EXTERNAL WORKS", 500.0),
            _row(13, "", "TOTAL COST", 1500.0),
        ]
    )
    assert reconcile_summary(book) == []


# --- sheets and their headers -----------------------------------------------------------------

POSITIONS = [
    # item, description, unit, qty, (materials, labour, delivery)
    ("1.1", "Excavation of trenches", "m3", 100.0, (0.0, 8.0, 2.0)),
    ("1.2", "Concrete in foundations", "m3", 40.0, (90.0, 20.0, 5.0)),
]


def _grouped_sheet(labels: tuple[str, ...], priced: bool) -> Cells:
    """A two-row header whose first row groups the rates and the amounts:

    No | Description | Unit | Quantity | Unit Rate (E:H)                     | Amount (I:L)
                                        | Materials | Labour | Delivery | Total | (the same)
    """
    no, desc, unit, qty, rate, amount, *subs = labels
    cells: Cells = {(1, 1): no, (1, 2): desc, (1, 3): unit, (1, 4): qty, (1, 5): rate}
    cells[(1, 9)] = amount
    for k, sub in enumerate(subs):
        cells[(2, 5 + k)] = sub
        cells[(2, 9 + k)] = sub
    total = 0.0
    for r, (item, text, u, q, rates) in enumerate(POSITIONS, start=3):
        cells.update({(r, 1): item, (r, 2): text, (r, 3): u, (r, 4): q})
        parts = rates if priced else (0.0, 0.0, 0.0)
        if priced:
            for k, x in enumerate(parts):
                cells[(r, 5 + k)] = x
        cells[(r, 8)] = Fx(f"=E{r}+F{r}+G{r}", sum(parts))
        for k, col in enumerate("EFGH"):
            value = (parts[k] if k < 3 else sum(parts)) * q
            cells[(r, 9 + k)] = Fx(f"={col}{r}*D{r}", value)
        total += sum(parts) * q
    cells[(6, 2)] = "SUB-TOTAL 1. WORKS"
    cells[(6, 12)] = Fx("=SUM(L3:L4)", total)
    return cells


_GROUP_LABELS = {
    "en": (
        *("No", "Description", "Unit", "Quantity", "Unit Rate, USD", "Amount, USD"),
        *("Materials", "Labour", "Delivery", "Total"),
    ),
    "ka": (
        *("№", "სამუშაოს დასახელება", "ერთეული", "რაოდენობა", "ერთეულის ფასი", "ღირებულება"),
        *("მასალა", "ხელფასი", "ტრანსპორტი", "სულ"),
    ),
}


@pytest.mark.parametrize("lang", ["en", "ka"])
def test_a_group_header_binds_rate_and_amount_to_its_total_sub_columns(
    tmp_path: Path, lang: str
) -> None:
    """ "Unit Rate" over Materials | Labour | Delivery | Total is not the materials rate."""
    labels = _GROUP_LABELS[lang]
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _grouped_sheet(labels, False)}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": _grouped_sheet(labels, True)}), name="B")
    assert t.sheets["1. WORKS"].columns == {
        "no": 1,
        "desc": 2,
        "unit": 3,
        "qty": 4,
        "rate_materials": 5,
        "rate_labor": 6,
        "rate_delivery": 7,
        "rate_total": 8,
        "sum_materials": 9,
        "sum_labor": 10,
        "sum_delivery": 11,
        "amount": 12,
    }
    comp = compare_bid(t, b)
    assert comp.evaluated_cost == pytest.approx(100 * 10.0 + 40 * 115.0)
    assert comp.findings == []


def test_a_quantity_grouped_per_building_is_read_from_its_total(tmp_path: Path) -> None:
    """ "Quantity" over Block A | Block B | Total: the quantity is the Total, and a quantity that
    adds the row's own cells (=D3+E3) is the position's own, not a parent's."""

    def sheet(rate: float | None) -> Cells:
        cells: Cells = {(1, 1): "No", (1, 2): "Description", (1, 3): "Unit", (1, 4): "Quantity"}
        cells.update({(1, 7): "Rate in USD", (1, 8): "Amount in USD"})
        cells.update({(2, 4): "Block A", (2, 5): "Block B", (2, 6): "Total"})
        total = 0.0
        for r, (item, text, qa, qb) in enumerate(
            [("1.1", "Floor tiles", 120.0, 80.0), ("1.2", "Screed 50 mm", 60.0, 40.0)], start=3
        ):
            cells.update({(r, 1): item, (r, 2): text, (r, 3): "m2", (r, 4): qa, (r, 5): qb})
            cells[(r, 6)] = Fx(f"=D{r}+E{r}", qa + qb)
            if rate is not None:
                cells[(r, 7)] = rate
            cells[(r, 8)] = Fx(f"=G{r}*F{r}", (rate or 0.0) * (qa + qb))
            total += (rate or 0.0) * (qa + qb)
        cells[(6, 2)] = "SUB-TOTAL"
        cells[(6, 8)] = Fx("=SUM(H3:H4)", total)
        return cells

    t = read_boq(_save(tmp_path / "t.xlsx", {"4. FLOORS": sheet(None)}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"4. FLOORS": sheet(20.0)}), name="B")
    assert t.sheets["4. FLOORS"].columns["qty"] == 6
    assert [ln.kind for ln in t.sheets["4. FLOORS"].leaf_lines] == ["position", "position"]
    comp = compare_bid(t, b)
    assert comp.evaluated_cost == pytest.approx((200 + 100) * 20.0)
    assert comp.findings == []


def test_a_quantity_that_adds_its_own_row_is_a_position_one_adding_other_rows_a_parent() -> None:
    from procurement_agent.checks.boq import _line_kind

    values, formulas = Workbook(), Workbook()
    wf = formulas.active
    assert wf is not None
    wf.cell(7, 6, "=D7+E7")
    wf.cell(7, 8, "=G7*F7")
    wf.cell(12, 6, "=F13+F14")
    cols = {"desc": 2, "qty": 6, "amount": 8}
    assert _line_kind(values.active, wf, cols, 7, "Floor tiles", True) == "position"
    assert _line_kind(values.active, wf, cols, 12, "Tiling, both blocks", True) == "heading"


def test_a_group_row_summing_a_defined_name_is_a_subtotal() -> None:
    """``=SUM(Block1_Amounts)`` names no cell of its own row: counted as a position, the block
    was evaluated twice."""
    from procurement_agent.checks.boq import _line_kind

    values, formulas = Workbook(), Workbook()
    wf = formulas.active
    assert wf is not None
    wf.cell(11, 16, "=SUM(Block1_Amounts)")
    wf.cell(12, 16, "=O12*G12")
    wf.cell(13, 16, "=150*4")
    cols = {"desc": 3, "qty": 7, "amount": 16}
    assert _line_kind(values.active, wf, cols, 11, "Block 1", False) == "subtotal"
    assert _line_kind(values.active, wf, cols, 12, "Door stopper", True) == "position"
    # a number typed as a formula reads like a typed number
    assert _line_kind(values.active, wf, cols, 13, "Door stopper", True) == "position"


def _one_row_bill(first_heading: str, *, rate_column: bool, rate: float | None) -> Cells:
    """One header row, no number row, positions under plain headings."""
    heads = ["No", "Description", "Unit", "Qty"] + (["Rate in USD"] if rate_column else [])
    heads.append("Amount in USD")
    cells: Cells = {(1, c): h for c, h in enumerate(heads, 1)}
    amount_col = len(heads)
    cells[(2, 2)], cells[(4, 2)] = first_heading, "Installation"
    positions = {3: ("1.1", "Gypsum board 12.5 mm", 6.0), 5: ("2.1", "Partition installation", 9.0)}
    for r, (item, text, price) in positions.items():
        cells.update({(r, 1): item, (r, 2): text, (r, 3): "m2", (r, 4): 300.0})
        amount = price * rate * 300.0 if rate is not None else 0.0
        if rate_column:
            if rate is not None:
                cells[(r, 5)] = price * rate
            cells[(r, amount_col)] = Fx(f"=E{r}*D{r}", amount)
        elif rate is not None:
            cells[(r, amount_col)] = amount
    return cells


@pytest.mark.parametrize("rate_column", [True, False])
def test_a_first_heading_that_reads_like_a_column_label_is_a_heading(
    tmp_path: Path, rate_column: bool
) -> None:
    """ "Materials" alone in the Description column under a one-row header is the bill's first
    heading. Read as a header label, it bound the materials rate to the descriptions."""
    t_cells = _one_row_bill("Materials", rate_column=rate_column, rate=None)
    b_cells = _one_row_bill("Materials", rate_column=rate_column, rate=1.0)
    t = read_boq(_save(tmp_path / "t.xlsx", {"4. PARTITIONS": t_cells}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"4. PARTITIONS": b_cells}), name="B")
    sheet = t.sheets["4. PARTITIONS"]
    assert sheet.header_row == 1 and "rate_materials" not in sheet.columns
    comp = compare_bid(t, b)
    assert comp.evaluated_cost == pytest.approx(300 * 6.0 + 300 * 9.0)
    assert "BOQ.NON_NUMERIC" not in _rules(comp)


def _plain_sheet(rows: Sequence[tuple[str, str, str, float, float | None]]) -> Cells:
    """No | Description | Unit | Quantity | Rate in USD | Amount in USD, positions from row 2;
    each row is (item, description, unit, qty, rate) and the amount is =E*D."""
    heads = ["No", "Description", "Unit", "Quantity", "Rate in USD", "Amount in USD"]
    cells: Cells = {(1, c): h for c, h in enumerate(heads, 1)}
    for r, (item, text, unit, qty, rate) in enumerate(rows, start=2):
        cells.update({(r, 1): item, (r, 2): text, (r, 3): unit, (r, 4): qty})
        if rate:
            cells[(r, 5)] = rate
        cells[(r, 6)] = Fx(f"=E{r}*D{r}", (rate or 0.0) * qty)
    return cells


def test_positions_described_by_a_symbol_or_a_size_are_read(tmp_path: Path) -> None:
    """ "Ø12" (a rebar diameter) has a letter in any script but not in [A-Za-zა-ჰ]; "600×300" (a
    tile size) has none. Both carry an item number and a quantity: they are positions."""
    rows = [
        ("1.1", "Reinforcement bars", "t", 2.0, 900.0),
        ("1.2", "Ø12", "t", 1.5, 950.0),
        ("1.3", "600×300", "m2", 40.0, 25.0),
    ]
    blank = [(i, d, u, q, None) for i, d, u, q, _ in rows]
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _plain_sheet(blank)}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": _plain_sheet(rows)}), name="B")
    assert [ln.item_no for ln in t.sheets["1. WORKS"].leaf_lines] == ["1.1", "1.2", "1.3"]
    comp = compare_bid(t, b)
    assert comp.evaluated_cost == pytest.approx(2 * 900 + 1.5 * 950 + 40 * 25)


def test_a_repeated_header_block_and_number_row_are_not_lines(tmp_path: Path) -> None:
    """A print layout repeats the header (and its "1 2 3" row) on every page."""
    rows = [("1.1", "Plaster", "m2", 10.0, 5.0), ("1.2", "Paint", "m2", 10.0, 2.0)]
    cells = _plain_sheet(rows)
    repeat = ["No", "Description", "Unit", "Quantity", "Rate in USD", "Amount in USD"]
    cells.update({(4, c): h for c, h in enumerate(repeat, 1)})
    cells.update({(5, c): c for c in range(1, 7)})
    cells.update({(6, 1): "1.3", (6, 2): "Skirting", (6, 3): "lm", (6, 4): 8.0, (6, 5): 3.0})
    cells[(6, 6)] = Fx("=E6*D6", 24.0)
    book = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": cells}), name="B")
    assert [ln.item_no for ln in book.sheets["1. WORKS"].lines] == ["1.1", "1.2", "1.3"]
    assert template_findings(book) == []


@pytest.mark.parametrize(
    "label", ["TOTAL CARRIED TO SUMMARY", "Carried to Summary", "სულ ჯამი", "ჯამი"]
)
def test_a_sheet_closed_by_another_label_is_reconciled(tmp_path: Path, label: str) -> None:
    """The closing row's SUM stops one row short: 70.00 of positions against 50.00 stated."""
    rows = [("1.1", "Plaster", "m2", 10.0, 5.0), ("1.2", "Paint", "m2", 10.0, 2.0)]
    cells = _plain_sheet(rows)
    cells[(5, 2)] = label
    cells[(5, 6)] = Fx("=SUM(F2:F2)", 50.0)
    book = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": cells}), name="B")
    sheet = book.sheets["1. WORKS"]
    assert (sheet.subtotal_row, sheet.subtotal) == (5, 50.0)
    gap = reconcile(sheet)
    assert gap is not None and gap.expected == "50.00" and gap.actual == "70.00"


def test_a_vat_row_under_the_positions_is_not_the_closing_total(tmp_path: Path) -> None:
    rows = [("1.1", "Plaster", "m2", 10.0, 5.0)]
    cells = _plain_sheet(rows)
    cells[(4, 2)] = "Total incl. VAT"
    cells[(4, 6)] = Fx("=F2*1.18", 59.0)
    book = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": cells}), name="B")
    assert book.sheets["1. WORKS"].subtotal_row is None


def test_money_is_compared_to_the_cent_however_large_the_sheet(tmp_path: Path) -> None:
    """2.50 off on a three-million sheet used to pass: the tolerance grew with the amount."""
    rows = [("1.1", "Earthworks", "m3", 100000.0, 30.0), ("1.2", "Fill", "m3", 1000.0, 7.379)]
    cells = _plain_sheet(rows)
    cells[(5, 2)] = "SUB-TOTAL"
    cells[(5, 6)] = Fx("=SUM(F2:F3)-2.5", 3007376.5)
    sheet = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": cells}), name="B").sheets["1. WORKS"]
    gap = reconcile(sheet)
    assert gap is not None and "2.50" in gap.message_en
    line = BoqLine(
        sheet="S", row=9, item_no="1.1", description="x", unit="m3", quantity=100000.0,
        rate_total=30.0, amount=3000002.5,
    )  # fmt: skip
    assert [f.rule_id for f in boq._arithmetic_findings(line, ())] == ["BOQ.ARITHMETIC"]
    # float noise is not a difference, and neither is a cent of rounding
    assert boq._same_money(0.1 + 0.2, 0.3) and boq._same_money(3007379.0, 3007379.01)


@pytest.mark.parametrize("label", ["Unit of measurement", "განზ. ერთ.", "ზ/ე", "U.o.M."])
def test_a_unit_header_written_the_long_or_the_short_way_is_read(
    tmp_path: Path, label: str
) -> None:
    rows = [("1.1", "Excavation", "m3", 10.0, 5.0), ("1.2", "Backfill", "m3", 10.0, 3.0)]
    t_cells, b_cells = _plain_sheet(rows), _plain_sheet(rows)
    t_cells[(1, 3)] = b_cells[(1, 3)] = label
    b_cells[(3, 3)] = "m2"
    t = read_boq(_save(tmp_path / "t.xlsx", {"2. EARTH": t_cells}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"2. EARTH": b_cells}), name="B")
    assert t.sheets["2. EARTH"].columns["unit"] == 3
    [changed] = [f for f in compare_bid(t, b).findings if f.rule_id == "BOQ.UNIT_CHANGED"]
    assert (changed.expected, changed.actual, changed.severity) == ("m3", "m2", "error")


def test_the_rate_is_not_read_as_a_unit() -> None:
    from procurement_agent.checks.boq import _label_key

    assert _label_key("ერთ. ფასი") == "rate_total"
    assert _label_key("Unit rate") == "rate_total"
    assert _label_key("ერთ.") == "unit"
    assert _label_key("Measured quantity") != "unit"


def test_a_sheet_whose_unit_column_is_not_recognised_says_units_were_not_compared(
    tmp_path: Path,
) -> None:
    rows = [("1.1", "Excavation", "m3", 10.0, 5.0)]
    cells = _plain_sheet(rows)
    cells[(1, 3)] = "Einheit"
    t = read_boq(_save(tmp_path / "t.xlsx", {"2. EARTH": cells}), name="T")
    notes = [f for f in template_findings(t) if f.rule_id == "BOQ.UNITS_NOT_COMPARED"]
    assert [(f.severity, f.actual) for f in notes] == [("info", "2. EARTH")]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("m2", "м2"),  # the Cyrillic "м" prints as m
        ("m3", "м3"),
        ("lm", "m"),
        ("t", "ტ"),
        ("t", "ტონა"),
        ("t", "tn"),
        ("month", "თვე"),
        ("kg", "კგ"),
        ("pcs", "ც"),
        ("pcs", "nr"),
        ("hr", "სთ"),
    ],
)
def test_the_same_unit_spelled_another_way_is_one_unit(a: str, b: str) -> None:
    assert norm_unit(a) == norm_unit(b)


@pytest.mark.parametrize(("a", "b"), [("item", "pcs"), ("kg", "t"), ("m2", "m3"), ("lm", "m2")])
def test_different_units_stay_different(a: str, b: str) -> None:
    assert norm_unit(a) != norm_unit(b)


# --- placeholders -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Any other item not included above, to be specified by the Bidder",
        "Other works not covered by this BOQ (Bidder to specify)",
        "Others (please specify)",
        "Other items (to be specified)",
        "სხვა სამუშაოები, რომლებიც არ არის გათვალისწინებული (მიუთითეთ)",
        "Any other items to be listed here by the tenderer",
        "Other works not included in this BOQ",
        "Others, not included elsewhere",
        "სხვა ხარჯები (ჩამოწერეთ)",
    ],
)
def test_a_placeholder_is_matched_by_what_it_asks_of_the_bidder(text: str) -> None:
    assert boq._PLACEHOLDER.search(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "Supply of other fixings as specified in the drawings",
        "Protection of other trades' work by the contractor",
        "სხვა მასალების ტრანსპორტირების ხარჯი",
        "სხვადასხვა ხარჯები",
        # "other" inside a described position, with an instruction that is not to the bidder
        "Paint to walls and other surfaces, colour to be specified by the Engineer",
        "Sealing of joints and other openings not included in the facade package",
        "Removal of debris and others, quantity to be specified on site",
        "ფასადის შეღებვა და სხვა ზედაპირები, ფერი მიუთითეთ ნახაზის მიხედვით",
        "კედლის შეღებვა, სხვა მასალა არ შედის",
    ],
)
def test_a_position_that_mentions_other_things_is_not_a_placeholder(text: str) -> None:
    assert boq._PLACEHOLDER.search(text) is None


def test_a_priced_placeholder_worded_otherwise_is_the_bidders_own_addition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheets = {
        name: [
            (*row[:1], "Any other item not included above, to be specified by the Bidder", *row[2:])
            if len(row) == 5 and "listed here" in row[1]
            else row
            for row in rows
        ]
        for name, rows in factory.REAL_SHEETS.items()
    }
    monkeypatch.setattr(factory, "REAL_SHEETS", sheets)
    t_path, b_path = tmp_path / "t.xlsx", tmp_path / "b.xlsx"
    t_path.write_bytes(build_real_boq())
    b_path.write_bytes(build_real_boq(Bid(qty={(CIVIL, "2.1"): 1}, rates={(CIVIL, "2.1"): 300.0})))
    comp = compare_bid(read_boq(t_path, name="T"), read_boq(b_path, name="B"))
    status = {p.item_no: p.status for p in comp.positions if p.sheet == CIVIL}
    assert status["2.1"] == "optional_priced"
    assert "BOQ.OPTIONAL_PRICED" in _rules(comp) and "BOQ.FILLED_BLANK" not in _rules(comp)
    # the placeholder's amount is shown once in the breakdown, under its own name
    assert comp.difference == {"optional_items": pytest.approx(300.0 * MARKUP, abs=0.01)}
    assert "optional items (placeholders)" in boq._explain(comp)


# --- the item number, typed totals, component rates -------------------------------------------


def _numbered_block(ref: str | None) -> Cells:
    """Section codes in A, item numbers in B - or, with ``ref``, a column the bidder inserted
    at B (blank, or holding their own references) and the item numbers in C."""
    shift = 0 if ref is None else 1
    heads = {1: "No", 3 + shift: "Description", 4 + shift: "Unit", 5 + shift: "Quantity"}
    heads.update({6 + shift: "Rate in USD", 7 + shift: "Amount in USD"})
    cells: Cells = {(1, c): h for c, h in heads.items()}
    cells[(2, 1)], cells[(2, 3 + shift)] = "A1", "Block 1"
    rows = [("1.1", "Plaster", "m2", 10.0, 5.0), ("1.2", "Paint", "m2", 20.0, 2.0)]
    for r, (item, text, unit, qty, rate) in enumerate(rows, start=3):
        if ref:
            cells[(r, 2)] = f"{ref}-{r:02d}"
        cells.update({(r, 2 + shift): item, (r, 3 + shift): text, (r, 4 + shift): unit})
        cells.update({(r, 5 + shift): qty, (r, 6 + shift): rate})
        e, f = "EF"[shift], "FG"[shift]
        cells[(r, 7 + shift)] = Fx(f"={f}{r}*{e}{r}", rate * qty)
    return cells


@pytest.mark.parametrize("ref", ["", "R"])
def test_a_column_inserted_in_the_item_number_block_keeps_every_position(
    tmp_path: Path, ref: str
) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _numbered_block(None)}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": _numbered_block(ref)}), name="B")
    assert [ln.item_no for ln in b.sheets["1. WORKS"].lines] == ["A1", "1.1", "1.2"]
    comp = compare_bid(t, b)
    assert (comp.added, comp.removed) == ([], [])
    assert comp.evaluated_cost == pytest.approx(10 * 5.0 + 20 * 2.0)


def _components_sheet(amounts: Sequence[object] | None) -> Cells:
    """Qty | Materials | Labour | Delivery | Amount, and no rate column at all."""
    heads = ["No", "Description", "Unit", "Qty", "Materials", "Labour", "Delivery", "Amount"]
    cells: Cells = {(1, c): h for c, h in enumerate(heads, 1)}
    rows = [("1.1", "Gypsum board", "m2", 100.0, (8.0, 4.0, 0.9))]
    rows.append(("1.2", "Metal studs", "lm", 40.0, (2.0, 1.0, 0.5)))
    for k, (r, (item, text, unit, qty, parts)) in enumerate(enumerate(rows, start=2)):
        cells.update({(r, 1): item, (r, 2): text, (r, 3): unit, (r, 4): qty})
        if amounts is not None:
            cells.update({(r, 5): parts[0], (r, 6): parts[1], (r, 7): parts[2]})
            cells[(r, 8)] = amounts[k]
    return cells


def test_an_amount_that_leaves_a_component_out_is_reported_without_a_rate_column(
    tmp_path: Path,
) -> None:
    left_out = [Fx("=(E2+F2)*D2", 1200.0), Fx("=(E3+F3)*D3", 120.0)]
    t = read_boq(_save(tmp_path / "t.xlsx", {"5. LINING": _components_sheet(None)}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"5. LINING": _components_sheet(left_out)}), name="B")
    comp = compare_bid(t, b)
    arithmetic = [f for f in comp.findings if f.rule_id == "BOQ.ARITHMETIC"]
    assert [(f.actual, f.expected) for f in arithmetic] == [
        ("1,200.00", "1,290.00"),
        ("120.00", "140.00"),
    ]
    # the evaluation itself was always right: template quantity x the sum of the components
    assert comp.evaluated_cost == pytest.approx(100 * 12.9 + 40 * 3.5)


def test_a_typed_amount_is_flagged_only_where_the_template_computes_it(tmp_path: Path) -> None:
    """A number typed where the template has a formula replaced that formula; one typed where
    the template itself has nothing replaced nothing."""
    typed = [1290.0, 140.0]
    t = read_boq(_save(tmp_path / "t.xlsx", {"5. LINING": _components_sheet(None)}), name="T")
    b = read_boq(_save(tmp_path / "b.xlsx", {"5. LINING": _components_sheet(typed)}), name="B")
    assert compare_bid(t, b).findings == []

    formulas = _components_sheet(None)
    formulas[(2, 8)] = Fx("=(E2+F2+G2)*D2", 0.0)
    formulas[(3, 8)] = Fx("=(E3+F3+G3)*D3", 0.0)
    t = read_boq(_save(tmp_path / "t2.xlsx", {"5. LINING": formulas}), name="T")
    comp = compare_bid(t, b)
    flagged = [f.message_en for f in comp.findings if f.rule_id == "BOQ.HARDCODED_TOTAL"]
    assert len(flagged) == 2 and "row 2" in flagged[0]


# --- the Summary sheet, chart sheets, unreadable bids ------------------------------------------


def _with_summary(cost: float, *, name: str = "BOQ SUMMARY") -> dict[str, Cells]:
    rows = [("1.1", "Plaster", "m2", 10.0, cost / 10.0)]
    excl = cost * 1.15
    summary: Cells = {(1, 1): "ITEM", (1, 2): "DESCRIPTION", (1, 3): "TOTAL", (1, 4): "RATE"}
    chain = [
        ("1", "WORKS", Fx("='1. WORKS'!F2", cost), None),
        ("", "TOTAL COST", Fx("=C2", cost), None),
        ("", "Overheads", Fx("=C3*D4", cost * 0.10), 0.10),
        ("", "Profit", Fx("=C3*D5", cost * 0.05), 0.05),
        ("", "TOTAL PRICE EXCL. VAT", Fx("=SUM(C3:C5)", excl), None),
        ("", "VAT", Fx("=C6*D7", excl * 0.18), 0.18),
        ("", "TOTAL PRICE INCLUDING VAT", Fx("=C6+C7", excl * 1.18), None),
    ]
    for r, (item, label, value, rate) in enumerate(chain, start=2):
        summary.update({(r, 1): item, (r, 2): label, (r, 3): value})
        if rate is not None:
            summary[(r, 4)] = rate
    return {name: summary, "1. WORKS": _plain_sheet(rows)}


def test_the_summary_is_found_among_several_sheets_named_summary(tmp_path: Path) -> None:
    """A working tab "Summary of unit rates" beside the Summary: the Summary is the one that
    states the totals, and the bid is ranked on it."""
    sheets = _with_summary(1000.0)
    sheets["Summary of unit rates"] = {(1, 1): "Item", (1, 2): "Rate", (2, 1): "1.1", (2, 2): 100}
    book = read_boq(_save(tmp_path / "b.xlsx", sheets), name="B")
    assert book.summary_sheet == "BOQ SUMMARY"
    assert grand_total(book) == pytest.approx(1000.0 * MARKUP)


def test_a_missing_summary_is_named_as_the_sheet_holding_the_totals(tmp_path: Path) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", _with_summary(1000.0)), name="T")
    sheets = _with_summary(900.0)
    del sheets["BOQ SUMMARY"]
    comp = compare_bid(t, read_boq(_save(tmp_path / "b.xlsx", sheets), name="B"))
    [removed] = [f for f in comp.findings if f.rule_id == "BOQ.SHEET_REMOVED"]
    assert removed.severity == "error" and "Summary" in removed.message_en
    assert "helper sheet" not in removed.message_en


def test_a_chart_sheet_is_listed_not_read(tmp_path: Path) -> None:
    """Excel's "Move Chart > New sheet" saves a chart sheet, which has no cells at all."""
    t = read_boq(_save(tmp_path / "t.xlsx", _with_summary(1000.0)), name="T")
    b_path = _save(tmp_path / "b.xlsx", _with_summary(900.0), chart="Cost chart")
    b = read_boq(b_path, name="B")
    comp = compare_bid(t, b)
    assert _rules(comp) == ["BOQ.SHEET_ADDED"]
    assert comp.evaluated_cost == pytest.approx(900.0)
    # the merged workbook is written for such a bid too
    assert write_merged_workbook(t, [b], [comp], tmp_path / "merged.xlsx").exists()


def test_an_unreadable_bid_is_reported_on_that_bidder_alone(tmp_path: Path) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", _with_summary(1000.0)), name="T")
    good = read_bid(_save(tmp_path / "good.xlsx", _with_summary(900.0)), name="Good")
    broken_path = tmp_path / "broken.xlsx"
    broken_path.write_bytes(b"PK\x03\x04 not really a workbook")
    broken = read_bid(broken_path, name="Broken")
    assert broken.read_error is not None
    comps = compare_all(t, [good, broken])
    assert comps[0].evaluable and comps[0].findings == []
    assert not comps[1].evaluable and _rules(comps[1]) == ["BOQ.UNREADABLE"]
    assert boq._explain(comps[1]).startswith("not evaluated")
    out = write_merged_workbook(t, [good, broken], comps, tmp_path / "merged.xlsx")
    assert out.exists()


def test_reconcile_ignores_a_sheet_without_a_closing_row() -> None:
    sheet = BoqSheet(name="S", columns={})
    assert reconcile(sheet) is None
