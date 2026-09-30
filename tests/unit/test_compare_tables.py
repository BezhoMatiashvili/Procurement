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
