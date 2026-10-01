"""COMPARE-R3-04: square and cubic metres written with the Georgian letter and a plain digit
("მ2", "მ3"), and "ც." for ცალი, are units of the closed list - in prose and in a table's unit
column - with a near miss beside each.

Every document here is invented.
"""

from __future__ import annotations

from procurement_agent.checks.compare import compare, hits_of
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion.segmenter import Segment, TableCell, build

PROSE = [
    "ნასყიდობის ხელშეკრულება № QK-2026/318",
    "ქ. ზუგდიდი, 2026 წლის 9 აპრილი",
    "1.1. მიმწოდებელი აწვდის კერამიკულ ფილას.",
    "1.2. ფილის რაოდენობა: 240 მ2.",
    "1.3. ხელშეკრულება შედგენილია 2 ეგზემპლარად.",
]

HEADER = ["№", "დასახელება", "ერთეული", "რაოდენობა", "ერთეულის ფასი (ლარი)"]
ROWS = [
    ["1", "ფილის დაგება", "მ2", "240", "35"],
    ["2", "ბეტონის ჩასხმა", "მ3", "18", "210"],
    ["3", "სამაგრი", "ც.", "650", "3"],
]


def segs(texts: list[str]) -> list[Segment]:
    return build([(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(texts)])


def table(rows: list[list[str]]) -> list[Segment]:
    records: list[tuple] = [("დანართი №2. ხარჯთაღრიცხვა", "text", "¶1", None, None)]  # type: ignore[type-arg]
    for r, row in enumerate([HEADER, *rows], start=1):
        for c, text in enumerate(row, start=1):
            header = HEADER[c - 1] if r > 1 else None
            records.append(
                (text, "table_cell", f"T1/r{r}/c{c}", None, None, TableCell(1, r, c, header))
            )
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


def quantities(text: str) -> list[str]:
    return [str(h.normalized) for h in hits_of(text) if h.type is FactType.quantity]


def with_cell(row: int, col: int, text: str) -> list[list[str]]:
    rows = [list(r) for r in ROWS]
    rows[row][col] = text
    return rows


def edit(doc: list[str], old: str, new: str) -> list[str]:
    return [t.replace(old, new) for t in doc]


def test_the_georgian_metre_with_a_digit_is_a_unit() -> None:
    assert quantities("რაოდენობა: 240 მ2") == ["240 m2"]
    assert quantities("რაოდენობა: 18 მ3") == ["18 m3"]
    assert quantities("რაოდენობა: 650 ც.") == ["650 item"]


def test_a_near_miss_of_the_metre_or_the_piece_is_no_unit() -> None:
    # a digit after "მ2" makes another token; a word that starts with "ც" is a word
    assert quantities("კოდი 240 მ25") == []
    assert quantities("240 ცხრილი") == []
    assert quantities("მუხლი 12 ცალკე") == []


def test_a_quantity_in_georgian_metres_changed_in_prose_is_a_quantity() -> None:
    report = run(segs(PROSE), segs(edit(PROSE, "240 მ2", "260 მ2")))
    assert found(report) == [("CMP.QUANTITY", "240 m2", "260 m2", "error")]


def test_the_same_quantity_restyled_is_no_change() -> None:
    report = run(segs(PROSE), segs(edit(PROSE, "240 მ2", "240 კვ.მ")))
    assert found(report) == []


def test_a_georgian_metre_row_s_quantity_changed_is_a_quantity() -> None:
    report = run(table(ROWS), table(with_cell(0, 3, "260")))
    assert found(report) == [("CMP.QUANTITY", "240 m2", "260 m2", "error")]


def test_a_unit_cell_changed_between_georgian_metres_is_a_quantity() -> None:
    report = run(table(ROWS), table(with_cell(1, 2, "მ2")))
    assert found(report) == [("CMP.QUANTITY", "18 m3", "18 m2", "error")]


def test_an_abbreviated_piece_row_s_quantity_changed_is_a_quantity() -> None:
    report = run(table(ROWS), table(with_cell(2, 3, "700")))
    assert found(report) == [("CMP.QUANTITY", "650 item", "700 item", "error")]
