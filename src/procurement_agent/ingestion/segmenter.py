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

__all__ = [
    "Segment",
    "SegmentKind",
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


#: The alignment key. Whitespace only — deliberately *not* ``normalize_confusables``, because
#: folding homoglyphs here would make a swapped Cyrillic character compare equal and the
#: character-substitution rule could never fire on the one thing it exists to catch.
def key_of(text: str) -> str:
    return normalize_spaces(text)


_Record = tuple[str, SegmentKind, str, str | None, Provenance | None]


def build(records: Iterable[_Record]) -> list[Segment]:
    """Assign indices and offsets. Empty text is dropped rather than aligned against."""
    out: list[Segment] = []
    pos = 0
    for text, kind, path, heading, prov in records:
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


def segments_from_docling(doc: DoclingDocument) -> list[Segment]:
    under_table = _text_under_tables(doc)

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
            records.append((text, kind, f"¶{para}", heading, provenance_of(doc, [item])))
            continue

        data = getattr(item, "data", None)
        cells = getattr(data, "table_cells", None)
        if cells is None:
            continue
        table += 1
        covered = under_table.get(item.self_ref, set())
        seen: set[tuple[int, int]] = set()
        for cell in cells:
            at = (cell.start_row_offset_idx, cell.start_col_offset_idx)
            text = (cell.text or "").strip()
            if not text or at in seen:
                continue  # a spanned cell repeats across the rows and columns it covers
            seen.add(at)
            if key_of(text) in covered:
                continue  # already emitted as a text item; see the module docstring
            path = f"T{table}/r{at[0] + 1}/c{at[1] + 1}"
            records.append((text, "table_cell", path, heading, provenance_of(doc, [item])))
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
