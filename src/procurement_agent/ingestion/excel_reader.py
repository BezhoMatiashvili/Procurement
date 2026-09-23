"""XLSX reading with openpyxl. Formulas are never evaluated (``data_only=True`` returns cached
values); macros are never executed (openpyxl cannot run VBA). Each sheet becomes one or more
chunks rendered as a Markdown table whose first column is the row number, so that quotes the
LLM produces can be matched back to cells.

Two properties matter beyond reading the cells.

* **Numbers keep their value.** A cell's text is what a reader sees ("12.345"), but that text
  is ambiguous to ``normalize.parse_number``, which reads a three-digit group after a dot as
  thousands - the convention of the Georgian documents it was written for. A quantity of
  12.345 m3 compared as 12 345 would report a thousandfold change that never happened, so each
  numeric cell also carries the number openpyxl read, and comparisons use that.
* **Truncation is never silent.** The reader caps sheets, rows and columns so a hostile or
  runaway workbook cannot exhaust memory. Data past a cap is dropped, and a comparison of two
  truncated bids would call them identical in exactly the part nobody could see - so every cap
  that bites is flagged on the sheet, logged, and written to the caller's ``notes``.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from procurement_agent.extraction.schemas import Provenance
from procurement_agent.ingestion.chunker import MAX_CHUNK_TOKENS, Chunk, estimate_tokens
from procurement_agent.security.audit import log

_MAX_SHEETS = 50
_MAX_ROWS = 20_000
_MAX_COLS = 100


@dataclass(slots=True)
class Cell:
    ref: str
    row: int
    col: int
    value: str
    #: the value openpyxl read, for numeric cells; ``value`` is only how it is displayed
    number: float | None = None


@dataclass(slots=True)
class Sheet:
    name: str
    cells: list[Cell]
    n_rows: int
    n_cols: int
    #: data exists below ``_MAX_ROWS`` / right of ``_MAX_COLS`` and was not read
    truncated_rows: bool = False
    truncated_cols: bool = False
    notes: list[str] = field(default_factory=list)


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
        text = f"{v:.10g}"
        # "12.345" would be read back by ``parse_number`` as twelve thousand three hundred and
        # forty-five (a dot and three digits is how Georgian documents group thousands). A
        # fourth decimal digit makes it unambiguous for every reader of the chunk text: the
        # rules, the quote check and the model.
        if re.fullmatch(r"-?\d+\.\d{3}", text):
            text += "0"
        return text
    # Newlines fold to spaces; a pipe stays a pipe. Escaping it for the Markdown table is the
    # table's business (``_render``) - the comparison and its quotes show what the cell says.
    return str(v).replace("\n", " ").strip()


def _number(v: Any) -> float | None:
    """The numeric value of a cell, or None. ``bool`` is an ``int`` in Python, not a number."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def _note(notes: list[str] | None, sheet: Sheet | None, text: str) -> None:
    log.warning("workbook_truncated", detail=text)
    if sheet is not None:
        sheet.notes.append(text)
    if notes is not None:
        notes.append(text)


def read_workbook(data: bytes, *, notes: list[str] | None = None) -> list[Sheet]:
    """Every non-empty sheet, up to the caps. Pass ``notes`` to learn which caps were hit."""
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets: list[Sheet] = []
    try:
        worksheets = wb.worksheets
        if len(worksheets) > _MAX_SHEETS:
            _note(
                notes,
                None,
                f"workbook has {len(worksheets)} sheets; only the first {_MAX_SHEETS} were read",
            )
        for ws in worksheets[:_MAX_SHEETS]:
            sheet = _read_sheet(ws)
            if sheet.truncated_rows:
                _note(
                    notes, sheet, f"sheet {sheet.name!r}: only the first {_MAX_ROWS} rows were read"
                )
            if sheet.truncated_cols:
                _note(
                    notes,
                    sheet,
                    f"sheet {sheet.name!r}: only the first {_MAX_COLS} columns were read",
                )
            if sheet.cells:
                sheets.append(sheet)
    finally:
        wb.close()
    return sheets


def _read_sheet(ws: Any) -> Sheet:
    cells: list[Cell] = []
    n_rows = n_cols = 0
    # One row and one column past each cap are read, and data found there means the cap bit.
    # Never an unbounded scan: without max_row/max_col openpyxl pads every row out to the
    # declared width, so a two-cell sheet declaring A1:XFD1048576 cost minutes. Data beyond an
    # *empty* row or column cap+1 is caught by the sheet's declared dimension instead, which is
    # free to read but may be missing (then only the scan speaks) or padded by formatted blank
    # cells (then the note is a false alarm - the cheaper mistake of the two).
    declared_rows, declared_cols = ws.max_row or 0, ws.max_column or 0
    truncated_rows = declared_rows > _MAX_ROWS
    truncated_cols = declared_cols > _MAX_COLS
    rows = ws.iter_rows(max_row=_MAX_ROWS + 1, max_col=_MAX_COLS + 1, values_only=True)
    for r_idx, row in enumerate(rows, 1):
        if r_idx > _MAX_ROWS:
            truncated_rows = truncated_rows or any(_fmt(v) for v in row)
            break
        for c_idx, v in enumerate(row, 1):
            text = _fmt(v)
            if not text:
                continue
            if c_idx > _MAX_COLS:
                truncated_cols = True
                break
            cells.append(Cell(f"{get_column_letter(c_idx)}{r_idx}", r_idx, c_idx, text, _number(v)))
            n_rows = max(n_rows, r_idx)
            n_cols = max(n_cols, c_idx)
    return Sheet(ws.title, cells, n_rows, n_cols, truncated_rows, truncated_cols)


def read_formulas(data: bytes) -> dict[tuple[str, str], str]:
    """``(sheet, cell_ref) -> formula`` for every formula cell, e.g. ``("SUMMARY", "C7")``.

    ``read_workbook`` opens the file with ``data_only=True`` and therefore sees cached values
    only. A bid whose rate formula was rewritten while the cached total stayed the same is a
    difference a value-only comparison cannot see, so the comparison reads formulas separately.
    Still no evaluation and still no VBA: openpyxl returns the formula as text.
    """
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    out: dict[tuple[str, str], str] = {}
    try:
        for ws in wb.worksheets[:_MAX_SHEETS]:
            for row in ws.iter_rows(max_row=_MAX_ROWS, max_col=_MAX_COLS):
                for cell in row:
                    # the cell type, not the text: a string cell reading "=x" is not a formula
                    if getattr(cell, "data_type", None) == "f" and isinstance(cell.value, str):
                        out[(ws.title, f"{get_column_letter(cell.column)}{cell.row}")] = cell.value
    finally:
        wb.close()
    return out


# --- chunking ------------------------------------------------------------------------------------


def _render(
    sheet_name: str, rows: list[int], cells: dict[int, dict[int, str]]
) -> tuple[str, list[int]]:
    """The Markdown table for ``rows``, and the columns it shows."""
    cols = sorted({c for r in rows for c in cells[r]})
    header = "| row | " + " | ".join(get_column_letter(c) for c in cols) + " |"
    sep = "|" + "---|" * (len(cols) + 1)
    lines = [f"Sheet: {sheet_name}", header, sep]
    for r in rows:
        row = cells[r]
        cells_md = (row.get(c, "").replace("|", "\\|") for c in cols)
        lines.append(f"| {r} | " + " | ".join(cells_md) + " |")
    return "\n".join(lines), cols


def _fits(sheet_name: str, rows: list[int], cells: dict[int, dict[int, str]], limit: int) -> bool:
    return estimate_tokens(_render(sheet_name, rows, cells)[0]) <= limit


def _chunk_size(
    sheet_name: str, rows: list[int], start: int, cells: dict[int, dict[int, str]], limit: int
) -> int:
    """How many of ``rows``, from ``start``, fit in one chunk - at least one.

    A chunk's size only grows as rows are added (more lines, and a new column widens every
    line), so the largest prefix that fits is found by galloping and then bisecting: O(log k)
    renders of at most 2k rows, instead of re-rendering and re-measuring the chunk for every
    row added. The result is the same prefix the row-by-row loop found; the token estimate
    itself is used as it is, so a change to it can never make the two disagree.
    """
    left = len(rows) - start
    good, step = 1, 1
    while good < left:
        probe = min(good + step, left)
        if not _fits(sheet_name, rows[start : start + probe], cells, limit):
            bad = probe
            break
        good, step = probe, step * 2
    else:
        return good
    while bad - good > 1:
        mid = (good + bad) // 2
        if _fits(sheet_name, rows[start : start + mid], cells, limit):
            good = mid
        else:
            bad = mid
    return good


def sheet_chunks(
    sheets: list[Sheet], *, doc_id: str, max_tokens: int = MAX_CHUNK_TOKENS
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for sheet in sheets:
        cells: dict[int, dict[int, str]] = {}
        for c in sheet.cells:
            cells.setdefault(c.row, {})[c.col] = c.value
        rows = sorted(cells)
        at = 0
        while at < len(rows):
            size = _chunk_size(sheet.name, rows, at, cells, max_tokens)
            _emit(chunks, sheet, rows[at : at + size], cells, doc_id)
            at += size
    return chunks


def _emit(
    chunks: list[Chunk],
    sheet: Sheet,
    rows: list[int],
    cells: dict[int, dict[int, str]],
    doc_id: str,
) -> None:
    text, cols = _render(sheet.name, rows, cells)
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
