"""BOQ sheets laid out the way the client's template is: a header over three rows, positions
counted in "item" straight under it, and a SUB-TOTAL row that closes the sheet.

The first reader treated the unit "item" as a header label (it is the Summary's "Item" column),
so on such a sheet the header "ran on" into the positions and the first three were never read.
Every workbook here is synthetic and built in memory.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from procurement_agent.checks.boq import compare_bid, read_boq, reconcile

SHEET = "3. Doors"

#: (item, description, unit, quantity, rate)
Position = tuple[str, str, str, float, float]

POSITIONS: list[Position] = [
    ("1.1", "Metal door with hinges and lock", "item", 4, 900.0),
    ("1.2", "Door closer with sliding guide rail", "item", 4, 150.0),
    ("1.3", "Door stopper", "item", 6, 12.5),
    ("1.4", "Threshold profile", "lm", 9, 20.0),
]


def _build(
    path: Path,
    positions: list[Position],
    *,
    priced: bool = True,
    subtotal: float | None = None,
    number_row: bool = True,
) -> Path:
    """Title rows, a header over rows 6-7 (+ the "1 2 3" row 8), a section, the positions."""
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.cell(1, 1, "Example project")
    ws.cell(2, 11, "all rates in USD, excluded VAT")
    ws.cell(4, 1, "THE BILLS OF QUANTITIES")
    ws.cell(5, 1, SHEET)
    for col, title in {
        1: "No",
        3: "Description",
        5: "CODE",
        6: "Manufacturer/ BRAND",
        7: "Quantity",
        8: "Unit",
        9: "Unit rates in USD (excl. VAT)",
        15: "Totals in USD (excl. VAT)",
        17: "Notes",
    }.items():
        ws.cell(6, col, title)
    for col, title in {
        9: "Materials",
        10: "Materials Sum",
        11: "Labour",
        12: "Labour Sum",
        13: "Delivery",
        14: "Delivery Sum",
        15: "Rate in USD",
        16: "Amount in USD",
    }.items():
        ws.cell(7, col, title)
    first = 8
    if number_row:
        for col in range(1, 18):
            ws.cell(8, col, col)
        first = 9
    ws.cell(first, 1, "A")
    ws.cell(first, 3, "DOORS")
    ws.cell(first + 2, 1, "A1")
    ws.cell(first + 2, 3, "Building A&B")
    row = first + 3
    total = 0.0
    for item, desc, unit, qty, rate in positions:
        ws.cell(row, 2, item)
        ws.cell(row, 3, desc)
        ws.cell(row, 7, qty)
        ws.cell(row, 8, unit)
        if priced:
            ws.cell(row, 9, rate)
            ws.cell(row, 10, rate * qty)
            ws.cell(row, 15, rate)
            ws.cell(row, 16, rate * qty)
            total += rate * qty
        row += 1
    ws.cell(first + 2, 16, total)
    row += 1
    ws.cell(row, 3, f"SUB-TOTAL {SHEET}")
    ws.cell(row, 16, total if subtotal is None else subtotal)
    wb.save(path)
    return path


def test_positions_counted_in_items_right_under_the_header_are_read(tmp_path: Path) -> None:
    book = read_boq(_build(tmp_path / "t.xlsx", POSITIONS, priced=False))
    sheet = book.sheets[SHEET]
    assert sheet.header_row == 8
    items = [(line.item_no, line.quantity) for line in sheet.leaf_lines]
    assert items == [("1.1", 4.0), ("1.2", 4.0), ("1.3", 6.0), ("1.4", 9.0)]


def test_a_quantity_changed_on_the_first_position_is_reported(tmp_path: Path) -> None:
    template = read_boq(_build(tmp_path / "t.xlsx", POSITIONS, priced=False))
    changed = [("1.1", "Metal door with hinges and lock", "item", 40, 900.0), *POSITIONS[1:]]
    bid = read_boq(_build(tmp_path / "b.xlsx", changed))
    result = compare_bid(template, bid)
    assert [(d.template_qty, d.bid_qty) for d in result.quantity_changed] == [(4.0, 40.0)]
    assert "BOQ.QUANTITY_CHANGED" in {f.rule_id for f in result.findings}
    assert not result.added and not result.removed


def test_without_a_number_row_the_first_position_is_still_a_position(tmp_path: Path) -> None:
    book = read_boq(_build(tmp_path / "t.xlsx", POSITIONS, priced=False, number_row=False))
    assert book.sheets[SHEET].header_row == 7
    assert [line.item_no for line in book.sheets[SHEET].leaf_lines] == ["1.1", "1.2", "1.3", "1.4"]


def test_the_recomputed_total_equals_the_sheets_own_subtotal(tmp_path: Path) -> None:
    sheet = read_boq(_build(tmp_path / "b.xlsx", POSITIONS)).sheets[SHEET]
    assert sheet.subtotal == sum(qty * rate for _, _, _, qty, rate in POSITIONS)
    assert sheet.total == sheet.subtotal
    assert reconcile(sheet) is None


def test_a_subtotal_that_does_not_add_up_is_reported(tmp_path: Path) -> None:
    template = read_boq(_build(tmp_path / "t.xlsx", POSITIONS, priced=False))
    bid = read_boq(_build(tmp_path / "b.xlsx", POSITIONS, subtotal=1000.0))
    gaps = [f for f in compare_bid(template, bid).findings if f.rule_id == "BOQ.RECONCILE"]
    assert len(gaps) == 1
    assert gaps[0].severity == "error"
    assert gaps[0].expected == "1,000.00"
    assert gaps[0].actual == "4,455.00"


def test_a_sheet_headed_in_georgian_with_currencies_in_its_labels_is_read(tmp_path: Path) -> None:
    """ "აღწერა", "ერთეულის ფასი, აშშ დოლარი", "ღირებულება, აშშ დოლარი": the columns are named
    in Georgian and carry their currency; the sheet is read like any other."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "C - Works"
    headers = ["№", "აღწერა", "ერთეული", "რაოდენობა", "მასალა", "ხელფასი", "ტრანსპორტი",
               "ერთეულის ფასი, აშშ დოლარი", "ღირებულება, აშშ დოლარი"]  # fmt: skip
    for col, title in enumerate(headers, start=1):
        ws.cell(7, col, title)
    ws.cell(9, 1, "C1")
    ws.cell(9, 2, "კედლების მოწყობა")
    for row, (item, desc, unit, qty, rate) in enumerate(
        [("1.1", "ტიხრის მოწყობა", "m2", 35, 20.0), ("1.2", "ჰიდროიზოლაცია", "m2", 88, 7.5)],
        start=10,
    ):
        for col, value in enumerate([item, desc, unit, qty, rate, 0, 0, rate, qty * rate], 1):
            ws.cell(row, col, value)
    ws.cell(12, 2, "SUB-TOTAL")
    ws.cell(12, 9, 35 * 20.0 + 88 * 7.5)
    summary = wb.create_sheet("BOQ SUMMARY")
    summary.cell(1, 1, "TOTAL COST")
    wb.create_sheet("Read Me").cell(1, 1, "How to fill in this bill of quantities")
    path = tmp_path / "georgian.xlsx"
    wb.save(path)
    book = read_boq(path)
    sheet = book.sheets["C - Works"]
    assert [(line.item_no, line.quantity, line.amount) for line in sheet.leaf_lines] == [
        ("1.1", 35.0, 700.0),
        ("1.2", 88.0, 660.0),
    ]
    assert set(book.sheets) == {"C - Works"}  # the Summary and the Read Me are no priced sheets
    assert reconcile(sheet) is None


def test_a_summary_with_its_columns_named_otherwise_is_read(tmp_path: Path) -> None:
    """ "No | Section | Amount, USD excl. VAT": the Summary's columns are found by what their
    headers say, not by three exact words."""
    from procurement_agent.checks.boq import grand_total

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "BOQ SUMMARY"
    ws.cell(7, 1, "No")
    ws.cell(7, 2, "Section")
    ws.cell(7, 3, "Amount, USD excl. VAT")
    for row, (item, label, value) in enumerate(
        [
            ("A", "A. WORKS", 1000.0),
            (None, "TOTAL COST", 1000.0),
            (None, "VAT", 180.0),
            (None, "TOTAL PRICE INCLUDING VAT", 1180.0),
        ],  # fmt: skip
        start=9,
    ):
        ws.cell(row, 1, item)
        ws.cell(row, 2, label)
        ws.cell(row, 3, value)
    path = tmp_path / "summary.xlsx"
    wb.save(path)
    assert grand_total(read_boq(path)) == 1180.0
