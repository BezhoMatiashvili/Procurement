"""Comparing two workbooks: sheet by sheet, row by row, cell by cell.

Positional alignment is wrong for a spreadsheet. Insert one row at the top of a bid and every
row below it shifts, so a positional diff reports the whole sheet as rewritten — which is the
failure that makes a comparison useless on exactly the files a buyer compares most often.
"""

from __future__ import annotations

import io

from openpyxl import Workbook

from procurement_agent.checks.compare import compare
from procurement_agent.ingestion.excel_reader import read_formulas, read_workbook
from procurement_agent.ingestion.segmenter import segments_from_workbook

HEADERS = ["#", "Description", "Unit", "Qty", "Rate", "Amount"]
LINES = [
    ("1.1", "Concrete works", "m3", 10, 100.0),
    ("1.2", "Steel works", "t", 4, 200.0),
    ("1.3", "Painting", "m2", 25, 8.0),
]


def book(
    lines: list[tuple[str, str, str, float, float]] | None = None,
    *,
    sheets: tuple[str, ...] = ("WORKS",),
    prefix_rows: list[list[object]] | None = None,
    formula_amounts: bool = False,
) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name in sheets:
        ws = wb.create_sheet(name)
        for row in prefix_rows or []:
            ws.append(row)
        ws.append(HEADERS)
        for item, desc, unit, qty, rate in lines or LINES:
            ws.append([item, desc, unit, qty, rate, None])
            r = ws.max_row
            ws.cell(r, 6, f"=D{r}*E{r}" if formula_amounts else qty * rate)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def segs(data: bytes) -> list[object]:
    return segments_from_workbook(read_workbook(data), read_formulas(data))


def run(left: bytes, right: bytes) -> object:
    return compare(
        segs(left),  # type: ignore[arg-type]
        segs(right),  # type: ignore[arg-type]
        left_document_id="A",
        right_document_id="B",
        left_kind="xlsx",
        right_kind="xlsx",
    )


def rules_of(report: object) -> set[str]:
    return {r for h in report.hunks for r in h.rules}  # type: ignore[attr-defined]


def test_a_workbook_compared_with_itself_has_no_differences() -> None:
    data = book()
    report = run(data, data)
    assert report.identical
    assert report.findings == []
    assert report.counts.unchanged > 0


def test_a_row_inserted_at_the_top_does_not_cascade_into_every_row_below() -> None:
    """Rows are aligned by their text cells, the same identity ``BoqLine.key`` uses."""
    report = run(book(), book(prefix_rows=[["Tender T-2026-09"]]))
    assert report.counts.changed == 0
    assert report.counts.added == 1
    assert report.counts.removed == 0


def test_the_same_number_written_differently_is_not_a_difference() -> None:
    """1 000,00 and 1000.00 are the same amount, whatever the locale wrote them as."""
    left = book([("1.1", "Concrete works", "m3", 10, 1000.0)])
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("WORKS")
    ws.append(HEADERS)
    ws.append(["1.1", "Concrete works", "m3", "10", "1 000,00", "10 000,00"])
    buf = io.BytesIO()
    wb.save(buf)

    report = run(left, buf.getvalue())
    assert report.counts.changed == 0
    assert report.errors == []


def test_a_changed_rate_is_found_with_its_cell_reference() -> None:
    dearer = [("1.1", "Concrete works", "m3", 10, 130.0), *LINES[1:]]
    report = run(book(), book(dearer))
    hunk = next(h for h in report.hunks if h.deltas)
    assert hunk.right is not None and "!" in hunk.right.path
    assert hunk.deltas[0].delta is not None


def test_numbers_changing_in_a_spreadsheet_are_not_all_called_critical() -> None:
    """Two bids disagree in hundreds of cells; that is what a bid is. The word has to keep
    meaning something, so a bare figure in a workbook is substantive, not critical."""
    dearer = [(item, desc, unit, qty, rate * 1.2) for item, desc, unit, qty, rate in LINES]
    report = run(book(), book(dearer))
    assert report.counts.changed > 0
    assert report.counts.critical == 0
    assert report.errors == []


def test_an_added_worksheet_is_one_finding_not_one_per_cell() -> None:
    report = run(book(), book(sheets=("WORKS", "MEP")))
    assert "CMP.SHEET_ADDED" in rules_of(report)
    assert len([h for h in report.hunks if "CMP.SHEET_ADDED" in h.rules]) == 1


def test_a_removed_worksheet_says_so() -> None:
    report = run(book(sheets=("WORKS", "MEP")), book())
    assert "CMP.SHEET_REMOVED" in rules_of(report)


def test_a_formula_that_changed_behind_an_unchanged_value_is_reported() -> None:
    """``read_workbook`` opens with ``data_only=True`` and sees cached values only, so a
    rewritten formula with the same cached result is invisible to a value comparison — and a
    rewritten formula is exactly the thing someone would rewrite.

    Built from segments rather than from a file: openpyxl cannot write a cached value beside a
    formula, which is the one combination this rule is about. That real files carry both is
    established elsewhere — a thirteen-sheet bid measures 1102 formula cells, every one of them
    with a value.
    """
    from procurement_agent.ingestion.segmenter import Segment

    def cell(formula: str) -> list[Segment]:
        return [
            Segment(
                index=0,
                text="1000",
                kind="sheet_cell",
                path="WORKS!F2",
                sheet="WORKS",
                row=2,
                col=6,
                formula=formula,
            )
        ]

    report = compare(
        cell("=D2*E2"),
        cell("=E2*D2"),
        left_document_id="A",
        right_document_id="B",
        left_kind="xlsx",
        right_kind="xlsx",
    )
    assert "CMP.FORMULA" in rules_of(report)
    assert [f.rule_id for f in report.findings] == ["CMP.FORMULA"]


def test_a_formula_cell_with_no_cached_value_is_still_compared() -> None:
    """A workbook written by a script has formulas and no cached values at all. Skipping those
    cells would report two visibly different sheets as identical."""
    assert run(book(formula_amounts=True), book(LINES[:2], formula_amounts=True)).hunks


def test_a_workbook_against_a_text_document_says_so_instead_of_diffing_it() -> None:
    from procurement_agent.ingestion.segmenter import build

    text = build([("ხელშეკრულება № SY-2026/004", "text", "¶1", None, None)])
    report = compare(
        segs(book()),  # type: ignore[arg-type]
        text,
        left_document_id="A",
        right_document_id="B",
        left_kind="xlsx",
        right_kind="docx",
    )
    assert any(s.rule_id == "CMP.LINE_DIFF" for s in report.skipped)
