"""What a stored document says, cut into blocks a page can show.

The reading view answers "what did the system take from my file?". It is built from the same
``Segment`` list the comparison reads (``extraction.pipeline.load_segments``), so a figure on the
page is the figure the checks saw. Pure - no database, no Docling - so a test needs no document.

Paragraphs, headings and list items stay as they are. A table's cells arrive one segment each,
tagged with their table, row and column, and a sheet's one segment per cell with its sheet, row
and column: here they are put back into grids. A grid is shown in row chunks and only with the
columns that hold something, so a sheet that uses column A and column AX is not forty empty
columns between them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from openpyxl.utils import get_column_letter

if TYPE_CHECKING:
    from procurement_agent.ingestion.segmenter import Segment

__all__ = [
    "GRID_ROWS",
    "MAX_COLUMNS",
    "PART_WEIGHT",
    "Block",
    "GridRow",
    "Reading",
    "Stats",
    "build",
    "parse_part",
]

#: Rows in one shown grid. A longer table or sheet carries on in the next block.
GRID_ROWS = 40
#: Columns shown per grid. Any more are counted, not drawn.
MAX_COLUMNS = 40
#: What one part of the page holds: a paragraph weighs 1, a grid its number of rows.
PART_WEIGHT = 120

BlockKind = Literal["heading", "text", "list_item", "table", "sheet"]
GridKind = Literal["table", "sheet"]

_PARAGRAPHS: dict[str, BlockKind] = {"heading": "heading", "list_item": "list_item"}


@dataclass(frozen=True, slots=True)
class GridRow:
    #: the sheet's own row number as text; empty for a Word or PDF table, which has none
    label: str
    cells: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Block:
    kind: BlockKind
    #: the paragraph, heading or list item
    text: str = ""
    #: the PDF page it starts on, when the parser knew one
    page: int | None = None
    #: a sheet's name
    title: str = ""
    #: a table's number, from 1
    number: int = 0
    #: a sheet's column letters, one per shown column; empty for a table
    columns: tuple[str, ...] = ()
    rows: tuple[GridRow, ...] = ()
    #: this grid carries on from the block before it
    continued: bool = False
    #: columns that hold something but are not drawn
    hidden_columns: int = 0

    @property
    def weight(self) -> int:
        return len(self.rows) or 1


@dataclass(frozen=True, slots=True)
class Stats:
    paragraphs: int
    tables: int
    sheets: int


@dataclass(frozen=True, slots=True)
class Reading:
    #: the blocks of the part being shown
    blocks: tuple[Block, ...]
    part: int
    parts: int
    stats: Stats
    #: items past ``limit`` that were not turned into blocks (0 when everything is here)
    cut: int
    limit: int


@dataclass(slots=True)
class _Grid:
    """A table or a sheet being collected, cell by cell, until the next segment is not its own."""

    kind: GridKind
    #: a table's number or a sheet's name
    key: int | str
    page: int | None
    #: the cell's whole text, from a segment that is the cell itself
    whole: dict[tuple[int, int], str] = field(default_factory=dict)
    #: paragraphs found inside a cell, which is how Word's tables arrive
    lines: dict[tuple[int, int], list[str]] = field(default_factory=dict)

    def add(self, seg: Segment, row: int, col: int) -> None:
        if seg.kind in ("table_cell", "sheet_cell"):
            self.whole[(row, col)] = seg.text
        else:
            self.lines.setdefault((row, col), []).append(seg.text)

    def blocks(self) -> list[Block]:
        # A segment that is the cell wins over paragraphs inside it: it holds all of them.
        cells = {slot: "\n".join(lines) for slot, lines in self.lines.items()}
        cells.update(self.whole)
        if not cells:
            return []
        used = sorted({col for _, col in cells})
        shown = used[:MAX_COLUMNS]
        by_row: dict[int, dict[int, str]] = {}
        for (row, col), text in cells.items():
            by_row.setdefault(row, {})[col] = text
        is_sheet = self.kind == "sheet"
        rows = [
            GridRow(
                label=str(row) if is_sheet else "",
                cells=tuple(by_row[row].get(col, "") for col in shown),
            )
            for row in sorted(by_row)
        ]
        letters = tuple(get_column_letter(col) for col in shown) if is_sheet else ()
        return [
            Block(
                kind=self.kind,
                page=self.page,
                title=str(self.key) if is_sheet else "",
                number=0 if is_sheet else int(self.key),
                columns=letters,
                rows=tuple(rows[start : start + GRID_ROWS]),
                continued=start > 0,
                hidden_columns=len(used) - len(shown),
            )
            for start in range(0, len(rows), GRID_ROWS)
        ]


def _page(seg: Segment) -> int | None:
    return seg.provenance.page if seg.provenance is not None else None


def _cell_of(seg: Segment) -> tuple[GridKind, int | str, int, int] | None:
    """The grid, row and column a segment is a cell of; None for a paragraph of the body."""
    if seg.sheet is not None and seg.row is not None and seg.col is not None:
        return "sheet", seg.sheet, seg.row, seg.col
    if seg.table is not None and seg.table_row is not None and seg.table_col is not None:
        return "table", seg.table, seg.table_row, seg.table_col
    return None


def _blocks(segments: Sequence[Segment]) -> tuple[list[Block], Stats]:
    blocks: list[Block] = []
    paragraphs = 0
    tables: set[int | str] = set()
    sheets: set[int | str] = set()
    grid: _Grid | None = None

    for seg in segments:
        cell = _cell_of(seg)
        if cell is None:
            if grid is not None:
                blocks.extend(grid.blocks())
                grid = None
            paragraphs += 1
            blocks.append(
                Block(kind=_PARAGRAPHS.get(seg.kind, "text"), text=seg.text, page=_page(seg))
            )
            continue
        kind, key, row, col = cell
        if grid is None or (grid.kind, grid.key) != (kind, key):
            if grid is not None:
                blocks.extend(grid.blocks())
            grid = _Grid(kind=kind, key=key, page=_page(seg))
        grid.add(seg, row, col)
        (sheets if kind == "sheet" else tables).add(key)
    if grid is not None:
        blocks.extend(grid.blocks())
    return blocks, Stats(paragraphs=paragraphs, tables=len(tables), sheets=len(sheets))


def _parts(blocks: Sequence[Block]) -> list[list[Block]]:
    parts: list[list[Block]] = []
    current: list[Block] = []
    weight = 0
    for block in blocks:
        if current and weight + block.weight > PART_WEIGHT:
            parts.append(current)
            current, weight = [], 0
        current.append(block)
        weight += block.weight
    if current:
        parts.append(current)
    return parts or [[]]


def build(segments: Sequence[Segment], part: int = 1, *, max_items: int = 20_000) -> Reading:
    """The blocks of one part of the document, with what the whole of it holds.

    ``part`` is clamped into range, so a link edited by hand shows the first or last part rather
    than failing. ``max_items`` bounds the work for a document the comparison would refuse too.
    """
    shown = segments[: max(max_items, 0)]
    blocks, stats = _blocks(shown)
    parts = _parts(blocks)
    part = min(max(part, 1), len(parts))
    return Reading(
        blocks=tuple(parts[part - 1]),
        part=part,
        parts=len(parts),
        stats=stats,
        cut=len(segments) - len(shown),
        limit=max_items,
    )


def parse_part(raw: str | None) -> int:
    """The ``part`` query value as a number; anything that is not one is the first part."""
    try:
        return max(int((raw or "1").strip()), 1)
    except ValueError:
        return 1
