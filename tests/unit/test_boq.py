"""BOQ comparison. Workbooks are built in-memory; no client file is ever read here."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from procurement_agent.checks.boq import (
    compare_all,
    compare_bid,
    grand_total,
    rate_outliers,
    read_boq,
    write_merged_workbook,
)

SECTION = "1. WORKS"
# (item, description, unit, qty, materials rate, labor rate)
LINES = [
    ("1.1", "Concrete works", "m3", 10, 100.0, 50.0),
    ("1.2", "Steel works", "t", 4, 200.0, 80.0),
    ("1.3", "Painting", "m2", 25, 8.0, 4.0),
]


def _build(path: Path, lines: list[tuple], *, priced: bool = True, summary_shift: int = 0) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = SECTION
    headers = [
        "No",
        "Item",
        "Description",
        "CODE",
        "Quantity",
        "Unit",
        "Materials",
        "Materials Sum",
        "Labor",
        "Labor Sum",
        "Rate in US $",
        "Amount in US $",
    ]
    for col, title in enumerate(headers, 1):
        ws.cell(1, col, title)
    for col in range(1, len(headers) + 1):
        ws.cell(2, col, col)  # the bare column-number row real templates carry
    for r, (item, desc, unit, qty, mat, lab) in enumerate(lines, start=3):
        ws.cell(r, 2, item)
        ws.cell(r, 3, desc)
        ws.cell(r, 5, qty)
        ws.cell(r, 6, unit)
        if priced:
            ws.cell(r, 7, mat)
            ws.cell(r, 8, mat * qty)
            ws.cell(r, 9, lab)
            ws.cell(r, 10, lab * qty)
            ws.cell(r, 11, mat + lab)
            ws.cell(r, 12, (mat + lab) * qty)

    sm = wb.create_sheet("SUMMARY")
    off = summary_shift
    sm.cell(1, 1 + off, "ITEM")
    sm.cell(1, 2 + off, "DESCRIPTION")
    sm.cell(1, 3 + off, "TOTAL")
    sm.cell(2, 1 + off, "1")
    sm.cell(2, 2 + off, "WORKS")
    sm.cell(2, 3 + off, sum((m + lab) * q for _, _, _, q, m, lab in lines) if priced else 0)
    sm.cell(3, 1 + off, "2")
    sm.cell(3, 2 + off, "TOTAL PRICE INCLUDING VAT")
    sm.cell(3, 3 + off, 1234.0 if priced else 0)
    wb.save(path)
    return path


@pytest.fixture
def template(tmp_path: Path) -> Path:
    """The blank template the client sends out: descriptions and units, no quantities."""
    blank = [(item, desc, unit, 0, 0.0, 0.0) for item, desc, unit, _, _, _ in LINES]
    return _build(tmp_path / "template.xlsx", blank, priced=False)


@pytest.fixture
def good_bid(tmp_path: Path) -> Path:
    return _build(tmp_path / "bid.xlsx", LINES)


def test_the_column_number_row_is_not_read_as_a_line(template: Path) -> None:
    """'1 2 3 4 ...' under the header parses as a perfectly good line whose arithmetic never
    adds up, which makes every sheet look broken."""
    book = read_boq(template)
    assert [line.item_no for line in book.sheets[SECTION].lines] == ["1.1", "1.2", "1.3"]


def test_a_faithful_bid_produces_no_errors(template: Path, good_bid: Path) -> None:
    comp = compare_bid(read_boq(template), read_boq(good_bid))
    assert [f for f in comp.findings if f.severity == "error"] == []
    assert not comp.added and not comp.removed


def test_a_changed_quantity_is_reported(template: Path, tmp_path: Path) -> None:
    priced_template = _build(tmp_path / "t2.xlsx", LINES)
    changed = list(LINES)
    changed[1] = ("1.2", "Steel works", "t", 99, 200.0, 80.0)
    comp = compare_bid(read_boq(priced_template), read_boq(_build(tmp_path / "b2.xlsx", changed)))
    assert [d.bid_qty for d in comp.quantity_changed] == [99]
    assert "BOQ.QUANTITY_CHANGED" in {f.rule_id for f in comp.findings}


def test_filling_a_blank_template_quantity_is_not_an_error(template: Path, good_bid: Path) -> None:
    """A blank quantity in the template means 'bidder fills this in'."""
    comp = compare_bid(read_boq(template), read_boq(good_bid))
    assert len(comp.filled_blank) == len(LINES)
    assert "BOQ.QUANTITY_CHANGED" not in {f.rule_id for f in comp.findings}


def test_an_added_and_a_removed_position_are_distinguished(tmp_path: Path) -> None:
    t = _build(tmp_path / "t3.xlsx", LINES)
    bid_lines = [LINES[0], ("1.9", "Extra scaffolding", "m2", 5, 3.0, 1.0), LINES[2]]
    comp = compare_bid(read_boq(t), read_boq(_build(tmp_path / "b3.xlsx", bid_lines)))
    assert [line.description for line in comp.added] == ["Extra scaffolding"]
    assert [line.description for line in comp.removed] == ["Steel works"]


def test_a_changed_unit_is_not_reported_as_add_plus_remove(tmp_path: Path) -> None:
    t = _build(tmp_path / "t4.xlsx", LINES)
    changed = list(LINES)
    changed[0] = ("1.1", "Concrete works", "m2", 10, 100.0, 50.0)
    comp = compare_bid(read_boq(t), read_boq(_build(tmp_path / "b4.xlsx", changed)))
    assert not comp.added and not comp.removed
    assert "BOQ.UNIT_CHANGED" in {f.rule_id for f in comp.findings}


def test_a_total_that_does_not_add_up_is_caught(tmp_path: Path) -> None:
    """The bidder controls this file, so its totals are a claim to be rechecked."""
    t = _build(tmp_path / "t5.xlsx", LINES)
    b = _build(tmp_path / "b5.xlsx", LINES)
    wb = load_workbook(b)
    wb[SECTION].cell(3, 12, 999999)  # overwrite the line amount
    wb.save(b)
    comp = compare_bid(read_boq(t), read_boq(b))
    arithmetic = [f for f in comp.findings if f.rule_id == "BOQ.ARITHMETIC"]
    assert arithmetic and "999,999.00" in arithmetic[0].actual


def test_an_unpriced_section_is_flagged(template: Path, tmp_path: Path) -> None:
    unpriced = _build(tmp_path / "b6.xlsx", LINES, priced=False)
    comp = compare_bid(read_boq(_build(tmp_path / "t6.xlsx", LINES)), read_boq(unpriced))
    assert "BOQ.SECTION_UNPRICED" in {f.rule_id for f in comp.findings}


def test_summary_is_read_even_when_the_sheet_is_shifted(tmp_path: Path) -> None:
    """One of the client's bidders returned a Summary shifted one column right; a reader that
    assumed columns A and B saw all zeroes - a wrong answer that looks like a right one."""
    normal = read_boq(_build(tmp_path / "s1.xlsx", LINES))
    shifted = read_boq(_build(tmp_path / "s2.xlsx", LINES, summary_shift=1))
    assert grand_total(normal) == grand_total(shifted) == 1234.0


def test_rate_outlier_needs_at_least_three_bidders(tmp_path: Path) -> None:
    t = read_boq(_build(tmp_path / "t7.xlsx", LINES))
    wild = list(LINES)
    wild[0] = ("1.1", "Concrete works", "m3", 10, 10000.0, 50.0)
    bids = [
        read_boq(_build(tmp_path / f"b7{i}.xlsx", lines), name=f"C{i}")
        for i, lines in enumerate([LINES, LINES, wild])
    ]
    findings = rate_outliers(t, compare_all(t, bids))
    assert {f.rule_id for f in findings} == {"BOQ.RATE_OUTLIER"}
    assert "C2" in findings[0].message_en


def test_merged_workbook_stacks_every_bidder_under_each_line(tmp_path: Path) -> None:
    t = read_boq(_build(tmp_path / "t8.xlsx", LINES), name="TEMPLATE")
    bids = [read_boq(_build(tmp_path / f"b8{i}.xlsx", LINES), name=f"C{i}") for i in range(3)]
    out = write_merged_workbook(t, bids, compare_all(t, bids), tmp_path / "merged.xlsx")
    wb = load_workbook(out)
    assert "SUMMARY" in wb.sheetnames and SECTION in wb.sheetnames
    ws = wb[SECTION]
    assert ws.cell(2, 5).value == "— template —"
    assert [ws.cell(r, 5).value for r in (3, 4, 5)] == ["C0", "C1", "C2"]
    # one template row plus one row per bidder, for each line
    assert ws.max_row >= len(LINES) * (1 + len(bids))


def test_merged_summary_colours_the_extremes(tmp_path: Path) -> None:
    t = read_boq(_build(tmp_path / "t9.xlsx", LINES), name="TEMPLATE")
    cheap = [(i, d, u, q, m / 2, lab / 2) for i, d, u, q, m, lab in LINES]
    bids = [
        read_boq(_build(tmp_path / "b90.xlsx", LINES), name="Expensive"),
        read_boq(_build(tmp_path / "b91.xlsx", cheap), name="Cheap"),
    ]
    out = write_merged_workbook(t, bids, compare_all(t, bids), tmp_path / "m2.xlsx")
    ws = load_workbook(out)["SUMMARY"]
    fills = {
        ws.cell(r, 2).value: ws.cell(r, 3).fill.fgColor.rgb
        for r in range(1, ws.max_row + 1)
        if ws.cell(r, 2).value in {"Expensive", "Cheap"}
    }
    assert "F4CCCC" in str(fills.get("Expensive"))  # highest, red
    assert "D9EAD3" in str(fills.get("Cheap"))  # lowest, green
