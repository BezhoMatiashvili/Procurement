"""The reading view's block builder. Segments are made by hand: no Docling, no files."""

from __future__ import annotations

import pytest

from procurement_agent.extraction.schemas import Provenance
from procurement_agent.ingestion.segmenter import Segment, SegmentKind
from procurement_agent.ui import reading


def seg(
    text: str, kind: SegmentKind = "text", *, page: int | None = None, **where: object
) -> Segment:
    return Segment(
        index=0,
        text=text,
        kind=kind,
        path="p",
        provenance=Provenance(page=page) if page else None,
        **where,  # type: ignore[arg-type]
    )


def cell(text: str, table: int, row: int, col: int, kind: SegmentKind = "table_cell") -> Segment:
    return seg(text, kind, table=table, table_row=row, table_col=col)


def sheet_cell(text: str, sheet: str, row: int, col: int) -> Segment:
    return seg(text, "sheet_cell", sheet=sheet, row=row, col=col)


def test_paragraphs_keep_their_order_and_kind() -> None:
    out = reading.build([seg("Contract", "heading"), seg("Body"), seg("one", "list_item")])
    assert [(b.kind, b.text) for b in out.blocks] == [
        ("heading", "Contract"),
        ("text", "Body"),
        ("list_item", "one"),
    ]
    assert out.stats == reading.Stats(paragraphs=3, tables=0, sheets=0)


def test_a_tables_cells_become_one_grid() -> None:
    out = reading.build(
        [cell("a", 1, 1, 1), cell("b", 1, 1, 2), cell("c", 1, 2, 1), cell("d", 1, 2, 2)]
    )
    [block] = out.blocks
    assert (block.kind, block.number, block.columns) == ("table", 1, ())
    assert [(r.label, r.cells) for r in block.rows] == [("", ("a", "b")), ("", ("c", "d"))]
    assert out.stats == reading.Stats(paragraphs=0, tables=1, sheets=0)


def test_a_cell_segment_wins_over_the_paragraphs_inside_it() -> None:
    """Word gives a cell both as itself and as the paragraphs it holds; showing both would
    print every line twice."""
    out = reading.build(
        [cell("a\nb", 1, 1, 1), cell("a", 1, 1, 1, "text"), cell("b", 1, 1, 1, "text")]
    )
    assert out.blocks[0].rows[0].cells == ("a\nb",)


def test_paragraphs_of_a_cell_are_joined_when_the_cell_itself_is_absent() -> None:
    out = reading.build([cell("a", 1, 1, 1, "text"), cell("b", 1, 1, 1, "text")])
    assert out.blocks[0].rows[0].cells == ("a\nb",)


def test_a_sheet_is_a_grid_with_letters_and_its_own_row_numbers() -> None:
    out = reading.build(
        [
            sheet_cell("No", "BOQ", 2, 1),
            sheet_cell("Qty", "BOQ", 2, 3),
            sheet_cell("1", "BOQ", 5, 1),
            sheet_cell("24", "BOQ", 5, 3),
        ]
    )
    [block] = out.blocks
    assert (block.kind, block.title) == ("sheet", "BOQ")
    assert block.columns == ("A", "C")  # the empty column B is not drawn
    assert [(r.label, r.cells) for r in block.rows] == [("2", ("No", "Qty")), ("5", ("1", "24"))]
    assert out.stats == reading.Stats(paragraphs=0, tables=0, sheets=1)


def test_a_gap_in_a_row_is_an_empty_cell() -> None:
    out = reading.build(
        [sheet_cell("x", "S", 1, 1), sheet_cell("y", "S", 1, 3), sheet_cell("z", "S", 2, 1)]
    )
    assert [r.cells for r in out.blocks[0].rows] == [("x", "y"), ("z", "")]


def test_two_sheets_and_two_tables_are_not_merged() -> None:
    out = reading.build(
        [
            sheet_cell("a", "One", 1, 1),
            sheet_cell("b", "Two", 1, 1),
            cell("c", 1, 1, 1),
            cell("d", 2, 1, 1),
        ]
    )
    assert [(b.kind, b.title, b.number) for b in out.blocks] == [
        ("sheet", "One", 0),
        ("sheet", "Two", 0),
        ("table", "", 1),
        ("table", "", 2),
    ]
    assert out.stats == reading.Stats(paragraphs=0, tables=2, sheets=2)


def test_a_paragraph_between_two_runs_of_one_table_ends_the_grid() -> None:
    out = reading.build([cell("a", 1, 1, 1), seg("between"), cell("b", 1, 2, 1)])
    assert [b.kind for b in out.blocks] == ["table", "text", "table"]
    assert out.stats.tables == 1  # still one table to count


def test_a_long_grid_carries_on_in_the_next_block() -> None:
    rows = reading.GRID_ROWS * 2 + 15
    out = reading.build([sheet_cell(str(r), "S", r, 1) for r in range(1, rows + 1)], part=1)
    sheet_blocks = [b for b in out.blocks if b.kind == "sheet"]
    assert [len(b.rows) for b in sheet_blocks] == [40, 40, 15]
    assert [b.continued for b in sheet_blocks] == [False, True, True]
    assert sheet_blocks[1].rows[0].label == "41"


def test_columns_past_the_cap_are_counted_not_drawn() -> None:
    wide = reading.MAX_COLUMNS + 5
    out = reading.build([sheet_cell("x", "S", 1, c) for c in range(1, wide + 1)])
    [block] = out.blocks
    assert len(block.columns) == reading.MAX_COLUMNS
    assert len(block.rows[0].cells) == reading.MAX_COLUMNS
    assert block.hidden_columns == 5


def test_a_part_holds_a_bounded_weight_and_the_part_is_clamped() -> None:
    segs = [seg(f"p{i}") for i in range(reading.PART_WEIGHT * 2 + 60)]
    second = reading.build(segs, 2)
    assert (second.part, second.parts, len(second.blocks)) == (2, 3, reading.PART_WEIGHT)
    assert second.blocks[0].text == f"p{reading.PART_WEIGHT}"
    assert reading.build(segs, 99).part == 3
    assert reading.build(segs, 0).part == 1
    assert reading.build(segs, -5).part == 1


def test_the_item_cap_says_how_many_were_left_out() -> None:
    out = reading.build([seg(f"p{i}") for i in range(10)], max_items=4)
    assert (len(out.blocks), out.cut, out.limit) == (4, 6, 4)
    assert reading.build([seg("only")], max_items=4).cut == 0


def test_an_empty_document_is_one_empty_part() -> None:
    out = reading.build([])
    assert (out.blocks, out.part, out.parts) == ((), 1, 1)
    assert out.stats == reading.Stats(paragraphs=0, tables=0, sheets=0)


def test_the_page_of_a_block_comes_from_its_provenance() -> None:
    out = reading.build(
        [
            seg("on three", page=3),
            seg("nowhere"),
            seg("t", "table_cell", page=4, table=1, table_row=1, table_col=1),
        ]
    )
    assert [b.page for b in out.blocks] == [3, None, 4]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3", 3), (" 2 ", 2), ("0", 1), ("-4", 1), ("abc", 1), ("2.5", 1), ("", 1), (None, 1)],
)
def test_the_part_in_a_link_is_read_leniently(raw: str | None, expected: int) -> None:
    assert reading.parse_part(raw) == expected


def test_a_hostile_part_value_cannot_fail_the_page() -> None:
    assert reading.parse_part("9" * 5000) >= 1
