"""Excel export of an ExtractionResult."""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from procurement_agent.extraction.schemas import ExtractionResult

_HEADERS = [
    "Type", "Label", "Value", "Normalized", "Unit", "Currency", "Confidence", "Conflict",
    "Source", "Page", "Sheet", "Cell", "Quote",
]  # fmt: skip


def _cell_safe(v: object) -> object:
    """Neutralise spreadsheet formula injection: values starting with = + - @ are prefixed."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def to_xlsx(result: ExtractionResult, document_name: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    if not isinstance(ws, Worksheet):  # pragma: no cover - openpyxl always creates one
        raise TypeError("workbook has no active worksheet")
    ws.title = "Facts"
    ws.append([_cell_safe(f"Document: {document_name}")])
    ws.append([f"Model: {result.model or 'rules only'}", f"LLM calls: {result.llm_calls}"])
    ws.append([])
    ws.append(_HEADERS)
    for c in ws[4]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDDDDD")
    for f in result.facts:
        ws.append(
            [
                f.type.value,
                f.label,
                _cell_safe(f.value),
                _cell_safe(f.normalized),
                f.unit,
                f.currency,
                round(f.confidence, 2),
                "YES" if f.conflict else "",
                f.source,
                f.provenance.page,
                f.provenance.sheet,
                f.provenance.cell,
                _cell_safe(f.raw_text),
            ]
        )
    for i, width in enumerate([18, 22, 30, 16, 8, 9, 11, 9, 8, 6, 12, 10, 60], 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    if result.warnings:
        ws2 = wb.create_sheet("Warnings")
        for w in result.warnings:
            ws2.append([_cell_safe(w)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
