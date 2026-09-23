"""Excel export of a comparison: one row per difference, plus the whole diff on a second sheet.

Two sheets because two people read this. A buyer wants the short list of values that moved and
forwards it; whoever has to act on it wants the lines themselves. Both come from the same
report, so they cannot disagree.
"""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from procurement_agent.checks.compare import ComparisonReport
from procurement_agent.report.xlsx_safe import cell_safe

_HEADER_FILL = PatternFill("solid", fgColor="DDDDDD")
#: Deliberately not the on-screen tokens in app.css, and deliberately not the BOQ workbook's
#: fills either: these are Excel constants for a different sheet with a different meaning.
_SEVERITY_FILL = {
    "error": PatternFill("solid", fgColor="FBE4E2"),
    "warning": PatternFill("solid", fgColor="FDF3D4"),
    "info": PatternFill("solid", fgColor="EEEEEE"),
}
_TAG_FILL = {
    "insert": PatternFill("solid", fgColor="E3F3E6"),
    "delete": PatternFill("solid", fgColor="FBE4E2"),
    "replace": PatternFill("solid", fgColor="FDF3D4"),
}

_DIFF_HEADERS = ["Where", "Change", "Old text", "New text", "Rules", "Model note"]
_FINDING_HEADERS = ["Severity", "Rule", "What changed", "In A", "In B", "რა შეიცვალა"]


def _header(ws: Worksheet, titles: list[str], widths: list[int]) -> None:
    ws.append(titles)
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = f"A{ws.max_row + 1}"


def to_xlsx(report: ComparisonReport) -> bytes:
    wb = Workbook()
    ws = wb.active
    if not isinstance(ws, Worksheet):  # pragma: no cover - openpyxl always creates one
        raise TypeError("workbook has no active worksheet")
    ws.title = "Differences"

    ws.append([cell_safe(f"A: {report.left_name or report.left_document_id}")])
    ws.append([cell_safe(f"B: {report.right_name or report.right_document_id}")])
    c = report.counts
    ws.append(
        [
            f"{c.critical} critical, {c.substantive} other, {c.cosmetic} formatting, "
            f"{c.moved} moved, {c.unchanged} unchanged",
            f"overlap {report.similarity:.0%}",
            "these are two versions of one document"
            if report.alignment == "versions"
            else "these do not look like two versions of one document",
        ]
    )
    ws.append([])
    _header(ws, _FINDING_HEADERS, [10, 20, 60, 30, 30, 60])
    # Row numbers are counted here rather than read back: ``Worksheet.max_row`` scans every
    # cell, and asking it once per appended row made a large export quadratic.
    at = ws.max_row
    for f in report.findings:
        ws.append(
            [
                f.severity,
                f.rule_id,
                cell_safe(f.message_en),
                cell_safe(f.expected),
                cell_safe(f.actual),
                cell_safe(f.message_ka),
            ]
        )
        at += 1
        ws.cell(at, 1).fill = _SEVERITY_FILL[f.severity]

    diff = wb.create_sheet("Diff")
    _header(diff, _DIFF_HEADERS, [16, 14, 60, 60, 26, 60])
    # one lookup table, not ``report.hunk()`` per row: that is a linear scan, and a workbook
    # pair has tens of thousands of rows
    hunks = {h.id: h for h in report.hunks}
    wrap = Alignment(wrap_text=True, vertical="top")
    at = diff.max_row
    for row in report.rows:
        if row.tag == "equal":
            continue
        hunk = hunks.get(row.hunk_id) if row.hunk_id else None
        anchor = row.right or row.left
        where = anchor.path if anchor is not None else ""
        diff.append(
            [
                # a sheet name is the bidder's text too: "=SUM(1)!A2" would become a formula
                cell_safe(where),
                hunk.kind if hunk else row.tag,
                cell_safe(row.left.text if row.left else ""),
                cell_safe(row.right.text if row.right else ""),
                ", ".join(hunk.rules) if hunk else "",
                cell_safe(hunk.explanation_en if hunk else None),
            ]
        )
        at += 1
        fill = _TAG_FILL.get(row.tag)
        if fill is not None:
            diff.cell(at, 2).fill = fill
        for col in (3, 4, 6):
            diff.cell(at, col).alignment = wrap

    if report.skipped:
        notes = wb.create_sheet("Could not run")
        _header(notes, ["Rule", "Why", "რატომ"], [22, 80, 80])
        for s in report.skipped:
            notes.append([s.rule_id, cell_safe(s.reason_en), cell_safe(s.reason_ka)])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
