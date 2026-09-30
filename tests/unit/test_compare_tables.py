"""Word tables in the comparison: a cell's column header says what its figure counts, and a
row is aligned as a row (red-team round 1, COMPARE-08, 10, 11).

Every document here is invented.
"""

from __future__ import annotations

from procurement_agent.checks.compare import compare
from procurement_agent.ingestion.segmenter import Segment, TableCell, build

HEADER = ["№", "დასახელება", "ერთეული", "რაოდენობა", "ღირებულება (ლარი)"]
ROWS = [
    ["1", "კედლების შეღებვა", "კვ.მ", "300", "450 (ოთხას ორმოცდაათი)"],
    ["2", "იატაკის დაგება", "კვ.მ", "120", "960 (ცხრაას სამოცი)"],
    ["3", "კარების მონტაჟი", "ცალი", "6", "720 (შვიდას ოცი)"],
    ["4", "ფანჯრების შეცვლა", "ცალი", "9", "1350"],
]
BEFORE = ["დანართი №3. სამუშაოების ხარჯთაღრიცხვა"]
AFTER = ["ჯამური ღირებულება მოიცავს ყველა გადასახადს."]


def document(rows: list[list[str]]) -> list[Segment]:
    records: list[tuple] = [(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(BEFORE)]  # type: ignore[type-arg]
    for r, row in enumerate([HEADER, *rows], start=1):
        for c, text in enumerate(row, start=1):
            header = HEADER[c - 1] if r > 1 else None
            records.append(
                (text, "table_cell", f"T1/r{r}/c{c}", None, None, TableCell(1, r, c, header))
            )
    records.extend((t, "text", f"¶{9 + i}", None, None) for i, t in enumerate(AFTER))
    return build(records)


def run(left: list[Segment], right: list[Segment]):  # type: ignore[no-untyped-def]
    return compare(
        left,
        right,
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )


def found(report) -> list[tuple[str, str | None, str | None, str]]:  # type: ignore[no-untyped-def]
    return [
        (f.rule_id, f.expected, f.actual, f.severity)
        for f in report.findings
        if f.severity != "info" and f.rule_id != "CMP.FORMATTING"
    ]


def with_cell(row: int, col: int, text: str) -> list[list[str]]:
    rows = [list(r) for r in ROWS]
    rows[row][col] = text
    return rows


# --- COMPARE-10: an amount's words in a cell whose currency is in the column header ----------


def test_an_amounts_words_changed_in_a_priced_column_are_amount_words() -> None:
    right = with_cell(0, 4, "450 (ხუთასი)")
    [(rule, before, after, severity)] = found(run(document(ROWS), document(right)))
    assert (rule, severity) == ("CMP.AMOUNT_WORDS", "error")
    assert (before, after) == ("ოთხას ორმოცდაათი (450)", "ხუთასი (500)")


def test_an_amounts_words_misspelled_in_a_priced_column_are_amount_words() -> None:
    right = with_cell(2, 4, "720 (შვიდას ოცოი)")
    assert found(run(document(ROWS), document(right))) == [
        ("CMP.AMOUNT_WORDS", "შვიდას ოცი", "შვიდას ოცოი", "error")
    ]


def test_an_amounts_words_before_the_currency_paragraph_are_amount_words() -> None:
    left = build(
        [
            (t, "text", f"¶{i}", None, None)
            for i, t in enumerate(["სულ ასანაზღაურებელია:", "640 (ექვსას ორმოცი)", "ლარი."])
        ]
    )
    right = build(
        [
            (t, "text", f"¶{i}", None, None)
            for i, t in enumerate(["სულ ასანაზღაურებელია:", "640 (შვიდას ორმოცი)", "ლარი."])
        ]
    )
    [(rule, _before, _after, severity)] = found(run(left, right))
    assert (rule, severity) == ("CMP.AMOUNT_WORDS", "error")


def test_a_count_in_words_in_a_quantity_column_stays_a_count() -> None:
    rows = with_cell(2, 3, "6 (ექვსი)")
    right = with_cell(2, 3, "6 (შვიდი)")
    [(rule, _before, _after, severity)] = found(run(document(rows), document(right)))
    assert (rule, severity) == ("CMP.AMOUNT_WORDS", "warning")


# --- COMPARE-11: a price cell written with its words is a figure ------------------------------


def test_a_price_changed_in_a_cell_written_with_its_words_is_an_amount_change() -> None:
    right = with_cell(1, 4, "990 (ცხრაას სამოცი)")
    rules = found(run(document(ROWS), document(right)))
    assert ("CMP.AMOUNT", "960", "990", "error") in rules
    assert "CMP.TEXT_CHANGED" not in [r[0] for r in rules]


def test_a_docx_paragraph_inside_a_table_knows_its_cell_and_column() -> None:
    from docling_core.types.doc.document import DoclingDocument, TableCell, TableData
    from docling_core.types.doc.labels import DocItemLabel

    from procurement_agent.ingestion.segmenter import segments_from_docling

    grid = [["დასახელება", "ფასი (ლარი)"], ["ცალი", "ცალი"], ["მომსახურება", "55"]]
    doc = DoclingDocument(name="t")
    data = TableData(
        num_rows=len(grid),
        num_cols=2,
        table_cells=[
            TableCell(
                text=text,
                start_row_offset_idx=r,
                end_row_offset_idx=r + 1,
                start_col_offset_idx=c,
                end_col_offset_idx=c + 1,
            )
            for r, row in enumerate(grid)
            for c, text in enumerate(row)
        ],
    )
    table = doc.add_table(data=data)
    for row in grid:
        for text in row:
            doc.add_text(label=DocItemLabel.PARAGRAPH, text=text, parent=table)
    segments = segments_from_docling(doc)
    assert [(s.text, s.table_row, s.table_col, s.column) for s in segments] == [
        ("დასახელება", 1, 1, None),
        ("ფასი (ლარი)", 1, 2, None),
        ("ცალი", 2, 1, "დასახელება"),
        ("ცალი", 2, 2, "ფასი (ლარი)"),
        ("მომსახურება", 3, 1, "დასახელება"),
        ("55", 3, 2, "ფასი (ლარი)"),
    ]
    assert all(s.kind == "text" and s.table == 1 for s in segments)


# --- COMPARE-08: a row inserted or removed is one change; the rows after it renumbered are not


def renumbered(rows: list[list[str]]) -> list[list[str]]:
    return [[str(n), *row[1:]] for n, row in enumerate(rows, start=1)]


def test_a_row_inserted_into_a_numbered_table_is_one_change() -> None:
    new = ["0", "ჭერის შეთეთრება", "კვ.მ", "80", "240"]
    right = renumbered([*ROWS[:2], new, *ROWS[2:]])
    assert found(run(document(ROWS), document(right))) == [
        ("CMP.TEXT_ADDED", None, "3 | ჭერის შეთეთრება | კვ.მ | 80 | 240", "warning")
    ]


def test_a_row_removed_from_a_numbered_table_is_one_change() -> None:
    right = renumbered([ROWS[0], *ROWS[2:]])
    assert found(run(document(ROWS), document(right))) == [
        (
            "CMP.TEXT_REMOVED",
            "2 | იატაკის დაგება | კვ.მ | 120 | 960 (ცხრაას სამოცი)",
            None,
            "warning",
        )
    ]


def test_a_changed_figure_in_a_row_is_still_that_rows_change() -> None:
    # the figure is in the quantity column, beside its unit: the row's quantity (COMPARE-R2-09)
    right = with_cell(3, 3, "11")
    assert found(run(document(ROWS), document(right))) == [
        ("CMP.QUANTITY", "9 item", "11 item", "error")
    ]


def test_a_changed_row_number_that_is_no_renumbering_is_still_reported() -> None:
    # a table whose first column is a code, not a count of its rows: a changed code is a change
    rows = [["7", *ROWS[0][1:]], ["12", *ROWS[1][1:]]]
    right = [["7", *ROWS[0][1:]], ["13", *ROWS[1][1:]]]
    rules = [r[0] for r in found(run(document(rows), document(right)))]
    assert rules == ["CMP.AMOUNT"]


def signature_table(director: str) -> list[Segment]:
    """A signature table as Docling gives a Word one: each cell whole, and again as the
    paragraphs inside it, all in the one row."""
    cells = [
        ["„შემკვეთი“", "შპს „ზღვის ქარი“", f"დირექტორი {director}"],
        ["„შემსრულებელი“", "შპს „მთის წყარო“", "დირექტორი: ლევან ქავთარაძე"],
    ]
    records: list[tuple] = [("ხელმოწერები", "text", "¶1", None, None)]  # type: ignore[type-arg]
    for col, lines in enumerate(cells, start=1):
        where = TableCell(1, 1, col, None)
        records.append(("\n".join(lines), "table_cell", f"T1/r1/c{col}", None, None, where))
    for col, lines in enumerate(cells, start=1):
        where = TableCell(1, 1, col, None)
        records.extend(
            (line, "text", f"¶{col}{k}", None, None, where) for k, line in enumerate(lines)
        )
    return build(records)


def test_a_director_renamed_in_a_signature_table_is_one_change() -> None:
    left, right = signature_table("ნანა გელაშვილი"), signature_table("ნანა გელაძე")
    report = run(left, right)
    assert found(report) == [("CMP.PARTY", "ნანა გელაშვილი", "ნანა გელაძე", "error")]
    # every line of both documents is in the diff, the cell whole and its paragraphs alike
    assert sorted(r.left.index for r in report.rows if r.left) == [s.index for s in left]
    assert sorted(r.right.index for r in report.rows if r.right) == [s.index for s in right]


def test_rows_numbered_with_a_full_stop_are_renumbered_not_changed_in_amount() -> None:
    dotted = [[f"{row[0]}.", *row[1:]] for row in ROWS]
    right = [[f"{n}.", *row[1:]] for n, row in enumerate([ROWS[0], *ROWS[2:]], start=1)]
    assert found(run(document(dotted), document(right))) == [
        (
            "CMP.TEXT_REMOVED",
            "2. | იატაკის დაგება | კვ.მ | 120 | 960 (ცხრაას სამოცი)",
            None,
            "warning",
        )
    ]


def test_a_word_table_of_four_hundred_rows_is_read_and_aligned_quickly() -> None:
    """Each table is indexed once: matched paragraph by paragraph against every cell, a priced
    annex of a few hundred rows cost millions of comparisons on every compare."""
    import time

    from docling_core.types.doc.document import DoclingDocument, TableData
    from docling_core.types.doc.document import TableCell as DoclingCell
    from docling_core.types.doc.labels import DocItemLabel

    from procurement_agent.ingestion.segmenter import segments_from_docling

    def annex(bump: int) -> list[Segment]:
        grid = [HEADER] + [
            [str(r), f"სამუშაო ნომერი {r}", "ცალი", str(r + (1 if r == bump else 0)), f"{r * 3}"]
            for r in range(1, 401)
        ]
        doc = DoclingDocument(name="t")
        cells = [
            DoclingCell(
                text=text,
                start_row_offset_idx=r,
                end_row_offset_idx=r + 1,
                start_col_offset_idx=c,
                end_col_offset_idx=c + 1,
            )
            for r, row in enumerate(grid)
            for c, text in enumerate(row)
        ]
        table = doc.add_table(data=TableData(num_rows=len(grid), num_cols=5, table_cells=cells))
        for row in grid:
            for text in row:
                doc.add_text(label=DocItemLabel.PARAGRAPH, text=text, parent=table)
        return segments_from_docling(doc)

    started = time.perf_counter()
    left, right = annex(0), annex(200)
    report = run(left, right)
    assert time.perf_counter() - started < 20
    assert found(report) == [("CMP.QUANTITY", "200 item", "201 item", "error")]


# --- COMPARE-R2-13 / R2-08: a row that moved is a move, and what changed in it is still read ----


def moved(report) -> list[tuple[str | None, str | None]]:  # type: ignore[no-untyped-def]
    return [(f.expected, f.actual) for f in report.findings if f.rule_id == "CMP.MOVED"]


def every_line_once(report, left: list[Segment], right: list[Segment]) -> None:  # type: ignore[no-untyped-def]
    assert sorted(r.left.index for r in report.rows if r.left) == [s.index for s in left]
    assert sorted(r.right.index for r in report.rows if r.right) == [s.index for s in right]


def test_two_rows_swapped_are_a_move_not_a_row_removed_and_added() -> None:
    left, right = document(ROWS), document([ROWS[0], ROWS[2], ROWS[1], ROWS[3]])
    report = run(left, right)
    assert found(report) == []
    assert len(moved(report)) == 1
    every_line_once(report, left, right)


def test_a_row_moved_to_the_end_of_its_table_is_a_move() -> None:
    left, right = document(ROWS), document([ROWS[0], ROWS[2], ROWS[3], ROWS[1]])
    report = run(left, right)
    assert found(report) == []
    assert len(moved(report)) == 1
    every_line_once(report, left, right)


def test_rows_swapped_and_renumbered_are_a_move_not_a_changed_number() -> None:
    swapped = [["3", *ROWS[3][1:]], ["4", *ROWS[2][1:]]]
    report = run(document(ROWS), document([*ROWS[:2], *swapped]))
    assert found(report) == []
    assert len(moved(report)) == 1


def test_a_price_changed_in_a_row_that_also_moved_is_the_price_s_change() -> None:
    changed = ["4", "ფანჯრების შეცვლა", "ცალი", "9", "1400"]
    left, right = document(ROWS), document([ROWS[0], ROWS[1], changed, ROWS[2]])
    report = run(left, right)
    assert found(report) == [("CMP.AMOUNT", "1350", "1400", "error")]
    assert len(moved(report)) == 1
    every_line_once(report, left, right)


def test_rows_of_nothing_but_numbers_swapped_are_never_paired_as_a_move() -> None:
    numeric = [["1", "10", "20", "30", "40"], ["2", "50", "60", "70", "80"], ROWS[2]]
    swapped = [["1", "50", "60", "70", "80"], ["2", "10", "20", "30", "40"], ROWS[2]]
    assert moved(run(document(numeric), document(swapped))) == []


def test_a_row_whose_words_another_row_shares_is_not_taken_for_a_move() -> None:
    # the row the other document writes twice may have moved to either place: no move
    twice = [
        ROWS[0],
        ["2", *ROWS[2][1:]],
        ["3", *ROWS[3][1:]],
        ["4", *ROWS[1][1:]],
        ["5", *ROWS[1][1:]],
    ]
    report = run(document(ROWS), document(twice))
    assert moved(report) == []
    assert sorted(r for r, *_ in found(report)) == [
        "CMP.TEXT_ADDED",
        "CMP.TEXT_ADDED",
        "CMP.TEXT_REMOVED",
    ]


def test_a_row_really_removed_is_still_one_row_removed() -> None:
    report = run(document(ROWS), document([ROWS[0], ROWS[1], ROWS[3]]))
    assert [r for r, *_ in found(report)] == ["CMP.TEXT_REMOVED"]
    assert moved(report) == []


# --- COMPARE-R2-09: a quantity column's figure, with its row's unit, is a quantity -------------


def test_a_quantity_cell_changed_is_the_row_s_quantity() -> None:
    right = with_cell(2, 3, "8")
    assert found(run(document(ROWS), document(right))) == [
        ("CMP.QUANTITY", "6 item", "8 item", "error")
    ]


def test_a_unit_cell_changed_is_the_row_s_quantity() -> None:
    right = with_cell(2, 2, "კომპლექტი")
    assert found(run(document(ROWS), document(right))) == [
        ("CMP.QUANTITY", "6 item", "6 set", "error")
    ]


def test_a_quantity_and_its_unit_changed_together_are_one_change() -> None:
    right = with_cell(2, 2, "კომპლექტი")
    right[2][3] = "4"
    assert found(run(document(ROWS), document(right))) == [
        ("CMP.QUANTITY", "6 item", "4 set", "error")
    ]


def test_a_square_metre_quantity_changed_is_the_row_s_quantity() -> None:
    right = with_cell(0, 3, "320")
    assert found(run(document(ROWS), document(right))) == [
        ("CMP.QUANTITY", "300 m2", "320 m2", "error")
    ]


def test_a_price_cell_beside_a_unit_stays_an_amount() -> None:
    right = with_cell(3, 4, "1400")
    assert found(run(document(ROWS), document(right))) == [("CMP.AMOUNT", "1350", "1400", "error")]


def test_a_quantity_beside_a_unit_no_list_names_stays_a_number() -> None:
    rows = with_cell(3, 2, "რეისი")
    right = [list(r) for r in rows]
    right[3][3] = "11"
    assert found(run(document(rows), document(right))) == [("CMP.AMOUNT", "9", "11", "error")]


def test_a_unit_price_header_is_no_unit_column() -> None:
    header = ["№", "დასახელება", "ერთეულის ფასი", "რაოდენობა"]
    left = [["1", "კარების მონტაჟი", "ცალი", "6"]]
    right = [["1", "კარების მონტაჟი", "კომპლექტი", "6"]]

    def table(rows: list[list[str]]) -> list[Segment]:
        records: list[tuple] = []  # type: ignore[type-arg]
        for r, row in enumerate([header, *rows], start=1):
            for c, text in enumerate(row, start=1):
                where = TableCell(1, r, c, header[c - 1] if r > 1 else None)
                records.append((text, "table_cell", f"T1/r{r}/c{c}", None, None, where))
        return build(records)

    assert [r for r, *_ in found(run(table(left), table(right)))] == ["CMP.TEXT_CHANGED"]


def test_a_quantity_changed_with_its_words_is_one_change() -> None:
    left, right = with_cell(2, 3, "6 (ექვსი)"), with_cell(2, 3, "7 (შვიდი)")
    assert found(run(document(left), document(right))) == [
        ("CMP.QUANTITY", "6 item", "7 item", "error")
    ]
