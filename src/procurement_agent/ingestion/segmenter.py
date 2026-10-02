"""Segments: the unit a document diff aligns on — one paragraph, list item or table cell.

Chunks are the wrong unit for a line-by-line diff. The chunker packs up to 1200 tokens into one
string and serialises a whole table onto a single line, so on the client's acceptance acts the
longest chunk "line" is 2542 characters and a one-character change reads as a wholly rewritten
block. Segments go the other way: the smallest unit a reader would point at and call a line.
Measured on those acts, segments are 1-306 characters, median 23.

Docling's DOCX backend emits the paragraphs inside a table **both** as text items and inside the
``TableItem`` — 23 of 27 cells on those acts — so emitting both duplicates every table hunk.
Those text items are children of the table itself. Text items are therefore authoritative, and
a table cell is dropped only when a text item *under that table* carries its text. Matching
against every paragraph in the document instead - as this once did - dropped PDF cells, which
exist only inside the table: a cell reading "100" vanished because a sentence elsewhere was
also "100", and a changed cell then showed up as a bare addition.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pypdfium2
from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.text import TextItem
from docling_core.types.doc.labels import DocItemLabel
from openpyxl.utils import coordinate_to_tuple

from procurement_agent.extraction.normalize import normalize_spaces
from procurement_agent.extraction.schemas import Provenance
from procurement_agent.ingestion.chunker import provenance_of
from procurement_agent.ingestion.excel_reader import Sheet
from procurement_agent.ingestion.pdfium_lock import PDFIUM_LOCK

__all__ = [
    "Segment",
    "SegmentKind",
    "TableCell",
    "build",
    "document_text",
    "pdf_has_text_layer",
    "segments_from_docling",
    "segments_from_workbook",
]

SegmentKind = Literal["text", "heading", "list_item", "table_cell", "sheet_cell"]

_LAYERS: set[ContentLayer] = {ContentLayer.BODY, ContentLayer.FURNITURE}
_HEADING_LABELS = {DocItemLabel.TITLE, DocItemLabel.SECTION_HEADER}


@dataclass(frozen=True, slots=True)
class Segment:
    """One alignable unit of a document, with where it came from."""

    index: int
    text: str
    kind: SegmentKind
    #: human-readable location: "¶14", "T2/r3/c1", "SUMMARY!C7"
    path: str
    heading: str | None = None
    provenance: Provenance | None = None
    #: offsets into ``document_text(segments)``, the coordinate space Finding.span uses
    start: int = 0
    end: int = 0
    # --- spreadsheet coordinates, so the diff can align by sheet and row rather than position
    sheet: str | None = None
    row: int | None = None
    col: int | None = None
    #: the cell's formula, when it has one (spreadsheets only)
    formula: str | None = None
    #: the cell's numeric value (spreadsheets only). ``text`` is how the number is displayed,
    #: and "12.345" is ambiguous to ``parse_number``; compare numbers through this instead.
    number: float | None = None
    # --- a Word or PDF table's cell, whether the parser gave it as the cell or as a paragraph
    # inside it: the table (numbered from 1), the row and column (from 1), and the header of the
    # column - its first row's text - for the rows below it. A figure's currency is often only
    # there ("ღირებულება (ლარი)"), and a row is aligned as a row by these.
    table: int | None = None
    table_row: int | None = None
    table_col: int | None = None
    column: str | None = None


@dataclass(frozen=True, slots=True)
class TableCell:
    """Where in a Word or PDF table a segment sits (see ``Segment.table``)."""

    table: int
    row: int
    col: int
    column: str | None = None


#: The alignment key. Whitespace only — deliberately *not* ``normalize_confusables``, because
#: folding homoglyphs here would make a swapped Cyrillic character compare equal and the
#: character-substitution rule could never fire on the one thing it exists to catch.
def key_of(text: str) -> str:
    return normalize_spaces(text)


_Record = (
    tuple[str, SegmentKind, str, str | None, Provenance | None]
    | tuple[str, SegmentKind, str, str | None, Provenance | None, TableCell | None]
)


def build(records: Iterable[_Record]) -> list[Segment]:
    """Assign indices and offsets. Empty text is dropped rather than aligned against."""
    out: list[Segment] = []
    pos = 0
    for record in records:
        text, kind, path, heading, prov = record[:5]
        cell = record[5] if len(record) > 5 else None
        stripped = text.strip()
        if not stripped:
            continue
        out.append(
            Segment(
                index=len(out),
                text=stripped,
                kind=kind,
                path=path,
                heading=heading,
                provenance=prov,
                start=pos,
                end=pos + len(stripped),
                table=cell.table if cell else None,
                table_row=cell.row if cell else None,
                table_col=cell.col if cell else None,
                column=cell.column if cell else None,
            )
        )
        pos += len(stripped) + 1  # the "\n" that document_text joins with
    return out


def document_text(segments: list[Segment]) -> str:
    """The string ``Segment.start``/``end`` index into."""
    return "\n".join(s.text for s in segments)


def _items(doc: DoclingDocument) -> Iterator[Any]:
    for item, _level in doc.iterate_items(included_content_layers=_LAYERS, with_groups=False):
        yield item


def _text_under_tables(doc: DoclingDocument) -> dict[str, set[str]]:
    """Table ref -> the keys of every text item nested under that table."""
    out: dict[str, set[str]] = {}
    for item in _items(doc):
        if not isinstance(item, TextItem) or not item.text.strip():
            continue
        parent = item.parent
        while parent is not None:
            node = parent.resolve(doc)
            if getattr(getattr(node, "data", None), "table_cells", None) is not None:
                out.setdefault(parent.cref, set()).add(key_of(item.text))
            parent = getattr(node, "parent", None)
    return out


def _table_node(item: Any, doc: DoclingDocument) -> tuple[str, Any] | None:
    """The table a text item is nested under, as (its ref, the table), if any."""
    parent = item.parent
    while parent is not None:
        node = parent.resolve(doc)
        if getattr(getattr(node, "data", None), "table_cells", None) is not None:
            return parent.cref, node
        parent = getattr(node, "parent", None)
    return None


def _grid(table: Any) -> list[tuple[int, int, str]]:
    """A table's cells as (row, column, text), in reading order, each spanned cell once."""
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, str]] = []
    for cell in table.data.table_cells:
        at = (cell.start_row_offset_idx, cell.start_col_offset_idx)
        if at in seen:
            continue
        seen.add(at)
        out.append((at[0], at[1], (cell.text or "").strip()))
    return sorted(out)


#: A first row's cell longer than this, or of several lines, is content, not a column's name.
_HEADER_MAX = 60


def _headers(grid: list[tuple[int, int, str]]) -> dict[int, str]:
    """Column -> its header: the first row's text, when it is short enough to be a name."""
    return {
        c: text
        for r, c, text in grid
        if r == 0 and text and len(text) <= _HEADER_MAX and "\n" not in text
    }


def _cells_of_text_items(doc: DoclingDocument, numbers: dict[str, int]) -> dict[str, TableCell]:
    """Text item ref -> the table cell it is a paragraph of.

    Docling's DOCX backend gives a table's paragraphs as text items under the table, and those
    are what reach the diff. Each is the cell whose text - or one of whose lines - it is, the
    first not yet taken in reading order, so the same word in two rows ("ცალი") lands in each.
    Each table is indexed once, line by line: a priced annex of a few hundred rows is a normal
    document.
    """
    out: dict[str, TableCell] = {}
    free: dict[str, dict[str, deque[tuple[int, int]]]] = {}
    headers: dict[str, dict[int, str]] = {}
    for item in _items(doc):
        if not isinstance(item, TextItem) or not item.text.strip():
            continue
        found = _table_node(item, doc)
        if found is None or found[0] not in numbers:
            continue
        ref, table = found
        if ref not in free:
            grid = _grid(table)
            spots: dict[str, deque[tuple[int, int]]] = {}
            for row, col, text in grid:
                for line in text.splitlines():
                    spots.setdefault(key_of(line), deque()).append((row, col))
            free[ref], headers[ref] = spots, _headers(grid)
        waiting = free[ref].get(key_of(item.text))
        if not waiting:
            continue
        row, col = waiting.popleft()
        column = headers[ref].get(col) if row > 0 else None
        out[item.self_ref] = TableCell(numbers[ref], row + 1, col + 1, column)
    return out


def segments_from_docling(doc: DoclingDocument) -> list[Segment]:
    under_table = _text_under_tables(doc)
    numbers = {
        item.self_ref: n
        for n, item in enumerate(
            (
                i
                for i in _items(doc)
                if getattr(getattr(i, "data", None), "table_cells", None) is not None
            ),
            start=1,
        )
    }
    in_cell = _cells_of_text_items(doc, numbers)

    records: list[_Record] = []
    heading: str | None = None
    para = 0
    table = 0
    for item in _items(doc):
        if isinstance(item, TextItem):
            text = item.text.strip()
            if not text:
                continue
            para += 1
            label = getattr(item.label, "value", str(item.label))
            if item.label in _HEADING_LABELS:
                kind: SegmentKind = "heading"
                heading = text
            elif label == DocItemLabel.LIST_ITEM.value:
                kind = "list_item"
            else:
                kind = "text"
            records.append(
                (
                    text,
                    kind,
                    f"¶{para}",
                    heading,
                    provenance_of(doc, [item]),
                    in_cell.get(item.self_ref),
                )
            )
            continue

        data = getattr(item, "data", None)
        cells = getattr(data, "table_cells", None)
        if cells is None:
            continue
        table += 1
        covered = under_table.get(item.self_ref, set())
        grid = _grid(item)  # a spanned cell repeats across the rows and columns it covers
        header = _headers(grid)
        for row, col, text in grid:
            if not text:
                continue
            if key_of(text) in covered:
                continue  # already emitted as a text item; see the module docstring
            path = f"T{table}/r{row + 1}/c{col + 1}"
            where = TableCell(table, row + 1, col + 1, header.get(col) if row > 0 else None)
            records.append((text, "table_cell", path, heading, provenance_of(doc, [item]), where))
    return build(records)


def segments_from_workbook(
    sheets: list[Sheet], formulas: dict[tuple[str, str], str] | None = None
) -> list[Segment]:
    """One segment per non-empty cell, carrying its sheet, row and column.

    The coordinates are what lets the comparison align sheet by sheet and row by row instead of
    by position, so inserting a row at the top of a bid does not report every row below it as
    changed.
    """
    formulas = formulas or {}
    out: list[Segment] = []
    pos = 0

    def emit(
        name: str,
        ref: str,
        row: int,
        col: int,
        text: str,
        formula: str | None,
        number: float | None = None,
    ) -> None:
        nonlocal pos
        out.append(
            Segment(
                index=len(out),
                text=text,
                kind="sheet_cell",
                path=f"{name}!{ref}",
                heading=name,
                provenance=Provenance(sheet=name, cell=ref),
                start=pos,
                end=pos + len(text),
                sheet=name,
                row=row,
                col=col,
                formula=formula,
                number=number,
            )
        )
        pos += len(text) + 1

    for sheet in sheets:
        seen: set[str] = set()
        for cell in sheet.cells:
            text = cell.value.strip()
            if not text:
                continue
            seen.add(cell.ref)
            emit(
                sheet.name,
                cell.ref,
                cell.row,
                cell.col,
                text,
                formulas.get((sheet.name, cell.ref)),
                cell.number,
            )

        # A formula cell that has never been calculated carries no cached value, so
        # ``read_workbook`` - which opens the file with ``data_only=True`` - does not see it at
        # all. It is still a cell with content, and a comparison that skipped it would report
        # two visibly different sheets as identical.
        for (name, ref), formula in formulas.items():
            if name != sheet.name or ref in seen:
                continue
            row, col = coordinate_to_tuple(ref)
            emit(name, ref, row, col, formula, formula)

    # A sheet of nothing but never-calculated formulas has no cached value anywhere, so
    # ``read_workbook`` returned no Sheet for it at all and the loop above never saw it.
    # Appended after the others, in workbook order: both sides of a comparison get the same
    # treatment, and sheets are aligned by name.
    known = {sheet.name for sheet in sheets}
    for (name, ref), formula in formulas.items():
        if name in known:
            continue
        row, col = coordinate_to_tuple(ref)
        emit(name, ref, row, col, formula, formula)
    return out


def pdf_has_text_layer(path: Path) -> bool:
    """True when a PDF carries its own text, False for a scan (or for anything unreadable).

    The comparison softens character substitutions on OCR'd input, because Tesseract emits
    Cyrillic lookalikes inside Latin codes. Treating every PDF that way hid a deliberate swap
    in a born-digital one; this is what tells the two apart. Reading the text layer is not
    parsing the document again - pdfium only lists the characters the file already contains.
    """
    with PDFIUM_LOCK:  # pdfium is not thread-safe, not even across documents
        try:
            pdf = pypdfium2.PdfDocument(str(path))
        except (pypdfium2.PdfiumError, OSError):
            return False
        try:
            for i in range(len(pdf)):
                page = pdf[i]
                try:
                    textpage = page.get_textpage()
                    try:
                        if textpage.get_text_range().strip():
                            return True
                    finally:
                        textpage.close()
                finally:
                    page.close()
            return False
        finally:
            pdf.close()
