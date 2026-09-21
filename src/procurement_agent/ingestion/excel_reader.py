"""XLSX reading with openpyxl. Formulas are never evaluated (``data_only=True`` returns cached
values); macros are never executed (openpyxl cannot run VBA). Each sheet becomes one or more
chunks rendered as a Markdown table whose first column is the row number, so that quotes the
LLM produces can be matched back to cells.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from procurement_agent.extraction.schemas import Provenance
from procurement_agent.ingestion.chunker import Chunk, estimate_tokens

_MAX_SHEETS = 50
_MAX_ROWS = 20_000
_MAX_COLS = 100


@dataclass(slots=True)
class Cell:
    ref: str
    row: int
    col: int
    value: str


@dataclass(slots=True)
class Sheet:
    name: str
    cells: list[Cell]
    n_rows: int
    n_cols: int


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return f"{v:.10g}"
    return str(v).replace("\n", " ").replace("|", "\\|").strip()


def read_workbook(data: bytes) -> list[Sheet]:
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets: list[Sheet] = []
    try:
        for ws in wb.worksheets[:_MAX_SHEETS]:
            cells: list[Cell] = []
            n_rows = n_cols = 0
            for r_idx, row in enumerate(ws.iter_rows(max_row=_MAX_ROWS, max_col=_MAX_COLS), 1):
                for c_idx, cell in enumerate(row, 1):
                    text = _fmt(cell.value)
                    if text:
                        cells.append(Cell(f"{get_column_letter(c_idx)}{r_idx}", r_idx, c_idx, text))
                        n_rows = max(n_rows, r_idx)
                        n_cols = max(n_cols, c_idx)
            if cells:
                sheets.append(Sheet(ws.title, cells, n_rows, n_cols))
    finally:
        wb.close()
    return sheets


def _rows_to_markdown(sheet: Sheet, rows: list[int], grid: dict[tuple[int, int], str]) -> str:
    cols = sorted({c for (r, c) in grid if r in rows})
    if not cols:
        return ""
    header = "| row | " + " | ".join(get_column_letter(c) for c in cols) + " |"
    sep = "|" + "---|" * (len(cols) + 1)
    lines = [f"Sheet: {sheet.name}", header, sep]
    for r in rows:
        lines.append(f"| {r} | " + " | ".join(grid.get((r, c), "") for c in cols) + " |")
    return "\n".join(lines)


def sheet_chunks(sheets: list[Sheet], *, doc_id: str, max_tokens: int = 1200) -> list[Chunk]:
    chunks: list[Chunk] = []
    for sheet in sheets:
        grid = {(c.row, c.col): c.value for c in sheet.cells}
        rows = sorted({c.row for c in sheet.cells})
        batch: list[int] = []
        for r in rows:
            batch.append(r)
            text = _rows_to_markdown(sheet, batch, grid)
            if estimate_tokens(text) > max_tokens and len(batch) > 1:
                batch.pop()
                _emit(chunks, sheet, batch, grid, doc_id)
                batch = [r]
        if batch:
            _emit(chunks, sheet, batch, grid, doc_id)
    return chunks


def _emit(
    chunks: list[Chunk],
    sheet: Sheet,
    rows: list[int],
    grid: dict[tuple[int, int], str],
    doc_id: str,
) -> None:
    text = _rows_to_markdown(sheet, rows, grid)
    cols = sorted({c for (r, c) in grid if r in rows})
    ref = f"{get_column_letter(cols[0])}{rows[0]}:{get_column_letter(cols[-1])}{rows[-1]}"
    cid = f"{doc_id}:{sheet.name}:{rows[0]}"
    chunks.append(
        Chunk(
            id=cid,
            text=text,
            body=text,
            provenance=Provenance(sheet=sheet.name, cell=ref, chunk_id=cid),
            headings=[sheet.name],
            token_estimate=estimate_tokens(text),
        )
    )
