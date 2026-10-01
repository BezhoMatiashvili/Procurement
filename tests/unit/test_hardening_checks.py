"""Hardening of the checks, the reports they write and the spreadsheet reader.

Each test pins one defect found in review: it failed on the code as it was and passes on the
fix. Every workbook, document and value here is invented; nothing is derived from client files.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from procurement_agent.checks.boq import (
    MERGED_COLUMNS,
    compare_all,
    read_boq,
    write_merged_workbook,
)

# --- helpers ------------------------------------------------------------------------------------

_BOQ_HEADERS = [
    "No",
    "Item",
    "Description",
    "CODE",
    "Quantity",
    "Unit",
    "Materials",
    "Materials Sum",
    "Labor",
    "Labor Sum",
    "Rate in US $",
    "Amount in US $",
]


def _text(ws: object, row: int, col: int, value: str) -> None:
    """Write ``value`` as a *string* cell even when it starts with '=' - what a bidder's file
    can legitimately contain, and what openpyxl would otherwise turn into a formula."""
    cell = ws.cell(row, col, value)  # type: ignore[attr-defined]
    cell.data_type = "s"


def boq_file(
    path: Path,
    rows: list[dict[str, object]],
    *,
    headers: list[str] | None = None,
    sheet: str = "1. WORKS",
    summary: list[tuple[str, str, float]] | None = None,
    extra_sheets: dict[str, list[dict[str, object]]] | None = None,
    summary_title: str = "SUMMARY",
) -> Path:
    """A BOQ workbook whose cells are given by header name, so a test can leave any out."""
    headers = headers or _BOQ_HEADERS
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = sheet

    def fill(target: object, lines: list[dict[str, object]]) -> None:
        for col, title in enumerate(headers, 1):
            target.cell(1, col, title)  # type: ignore[attr-defined]
        for r, line in enumerate(lines, start=2):
            for name, value in line.items():
                col = headers.index(name) + 1
                if isinstance(value, str):
                    _text(target, r, col, value)
                else:
                    target.cell(r, col, value)  # type: ignore[attr-defined]

    fill(ws, rows)
    for name, lines in (extra_sheets or {}).items():
        fill(wb.create_sheet(name), lines)
    if summary is not None:
        sm = wb.create_sheet(summary_title)
        sm.cell(1, 1, "ITEM")
        sm.cell(1, 2, "DESCRIPTION")
        sm.cell(1, 3, "TOTAL")
        for r, (item, label, total) in enumerate(summary, start=2):
            _text(sm, r, 1, item)
            _text(sm, r, 2, label)
            sm.cell(r, 3, total)
    wb.save(path)
    return path


def _line(item: str, desc: str, qty: float, rate: float, unit: str = "m2") -> dict[str, object]:
    return {
        "Item": item,
        "Description": desc,
        "Quantity": qty,
        "Unit": unit,
        "Rate in US $": rate,
        "Amount in US $": qty * rate,
    }


def _formula_cells(path: Path) -> list[str]:
    wb = load_workbook(path, data_only=False)
    try:
        return [
            f"{ws.title}!{cell.coordinate}={cell.value!r}"
            for ws in wb.worksheets
            for row in ws.iter_rows()
            for cell in row
            if cell.data_type == "f"
        ]
    finally:
        wb.close()


# --- 1. formula injection in the merged BOQ workbook ---------------------------------------------

_PAYLOADS = ('=HYPERLINK("http://example.invalid","x")', "+1+1", "-2+3", "@SUM(1)")


def test_merged_boq_workbook_never_turns_bidder_text_into_a_formula(tmp_path: Path) -> None:
    lines = [_line("1.1", "Concrete works", 10, 5.0), _line("1.2", "Steel works", 4, 7.0)]
    template = read_boq(boq_file(tmp_path / "t.xlsx", lines), name="TEMPLATE")
    evil = [
        {**lines[0], "Description": "Concrete works", "Unit": _PAYLOADS[0]},
        {**_line(_PAYLOADS[1], _PAYLOADS[3] + " added work", 1, 1.0)},
        lines[1],
    ]
    bid = read_boq(
        boq_file(
            tmp_path / "b.xlsx",
            evil,
            summary=[("1", _PAYLOADS[0] + " WORKS", 1.0), ("2", "TOTAL PRICE", 2.0)],
            extra_sheets={_PAYLOADS[2]: [_line("9", "Extra", 1, 1.0)]},
        ),
        name=_PAYLOADS[0][:31],
    )
    out = write_merged_workbook(template, [bid], compare_all(template, [bid]), tmp_path / "m.xlsx")
    assert _formula_cells(out) == []


def test_merged_boq_workbook_survives_control_characters(tmp_path: Path) -> None:
    """openpyxl raises IllegalCharacterError on a C0 control; one odd byte in one bid must not
    fail the whole export. Built from objects: openpyxl refuses to write such a file at all,
    but a workbook from another producer can carry one."""
    from procurement_agent.checks.boq import BoqLine, BoqSheet, BoqWorkbook

    def book(name: str, desc: str) -> BoqWorkbook:
        line = BoqLine(
            sheet="S",
            row=2,
            item_no="1\x0b",
            description=desc,
            unit="m\x1f",
            quantity=1.0,
            rate_total=1.0,
            amount=1.0,
        )
        return BoqWorkbook(
            name=name,
            sheets={"S": BoqSheet(name="S", columns={}, lines=[line])},
            summary={"Odd\x0b label": 1.0},
            sheet_order=["S"],
        )

    template, bid = book("T", "Concrete\x0bworks"), book("B\x01", "Concrete\x0bworks")
    out = write_merged_workbook(template, [bid], compare_all(template, [bid]), tmp_path / "m.xlsx")
    assert out.exists()


def test_cell_safe_escapes_formula_leads_and_keeps_numbers() -> None:
    from procurement_agent.report.xlsx_safe import cell_safe

    assert cell_safe("=1+1") == "'=1+1"
    assert cell_safe("\x01=1+1") == "'=1+1"
    assert cell_safe("plain") == "plain"
    assert cell_safe(-5) == -5
    assert cell_safe(1.5) == 1.5
    assert cell_safe(None) is None


def test_compare_xlsx_where_column_is_escaped() -> None:
    from procurement_agent.checks.compare import compare
    from procurement_agent.ingestion.excel_reader import read_formulas, read_workbook
    from procurement_agent.ingestion.segmenter import segments_from_workbook
    from procurement_agent.report.compare_xlsx import to_xlsx

    def book(value: str) -> bytes:
        wb = Workbook()
        ws = wb.create_sheet("=SUM(1)")
        wb.remove(wb.worksheets[0])
        ws.cell(1, 1, value)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    left, right = book("alpha value"), book("bravo value")
    report = compare(
        segments_from_workbook(read_workbook(left), read_formulas(left)),
        segments_from_workbook(read_workbook(right), read_formulas(right)),
        left_document_id="A",
        right_document_id="B",
        left_kind="xlsx",
        right_kind="xlsx",
    )
    data = to_xlsx(report)
    wb = load_workbook(io.BytesIO(data), data_only=False)
    formulas = [
        c.coordinate
        for ws in wb.worksheets
        for r in ws.iter_rows()
        for c in r
        if c.data_type == "f"
    ]
    assert formulas == []


# --- 2. arithmetic on a sheet with no rate column -------------------------------------------------


def test_no_rate_column_does_not_make_every_priced_line_an_arithmetic_error(
    tmp_path: Path,
) -> None:
    headers = ["No", "Item", "Description", "Quantity", "Unit", "Amount in US $"]
    rows = [
        {
            "Item": "1.1",
            "Description": "Concrete works",
            "Quantity": 10,
            "Unit": "m3",
            "Amount in US $": 500.0,
        },
    ]
    template = read_boq(boq_file(tmp_path / "t.xlsx", rows, headers=headers), name="T")
    bid = read_boq(boq_file(tmp_path / "b.xlsx", rows, headers=headers), name="B")
    [comp] = compare_all(template, [bid])
    assert [f for f in comp.findings if f.rule_id == "BOQ.ARITHMETIC"] == []


def test_empty_rate_cell_is_not_an_arithmetic_error_but_a_wrong_one_is(tmp_path: Path) -> None:
    rows = [
        {**_line("1.1", "Concrete works", 10, 5.0), "Rate in US $": None},
        {**_line("1.2", "Steel works", 4, 7.0), "Amount in US $": 99.0},
    ]
    template = read_boq(boq_file(tmp_path / "t.xlsx", rows), name="T")
    bid = read_boq(boq_file(tmp_path / "b.xlsx", rows), name="B")
    [comp] = compare_all(template, [bid])
    arithmetic = [f for f in comp.findings if f.rule_id == "BOQ.ARITHMETIC"]
    assert len(arithmetic) == 1
    assert "row 3" in arithmetic[0].message_en


# --- 8. BOQ comparison details -----------------------------------------------------------------


def test_added_position_is_cross_footed(tmp_path: Path) -> None:
    lines = [_line("1.1", "Concrete works", 10, 5.0)]
    template = read_boq(boq_file(tmp_path / "t.xlsx", lines), name="T")
    wrong = {**_line("1.2", "Extra works", 2, 5.0), "Amount in US $": 999.0}
    bid = read_boq(boq_file(tmp_path / "b.xlsx", [*lines, wrong]), name="B")
    [comp] = compare_all(template, [bid])
    assert [line.description for line in comp.added] == ["Extra works"]
    assert any(f.rule_id == "BOQ.ARITHMETIC" and "row 3" in f.message_en for f in comp.findings)


def test_lines_on_an_added_sheet_are_cross_footed(tmp_path: Path) -> None:
    lines = [_line("1.1", "Concrete works", 10, 5.0)]
    template = read_boq(boq_file(tmp_path / "t.xlsx", lines), name="T")
    wrong = {**_line("9.1", "Extra works", 2, 5.0), "Amount in US $": 999.0}
    bid = read_boq(
        boq_file(tmp_path / "b.xlsx", lines, extra_sheets={"9. EXTRA": [wrong]}), name="B"
    )
    [comp] = compare_all(template, [bid])
    assert comp.added_sheets == ["9. EXTRA"]
    assert any(f.rule_id == "BOQ.ARITHMETIC" and "9. EXTRA" in f.message_en for f in comp.findings)


def test_float_noise_in_a_quantity_is_not_a_quantity_change() -> None:
    from procurement_agent.checks.boq import BoqLine, BoqSheet, BoqWorkbook, compare_bid

    def book(qty: float) -> BoqWorkbook:
        line = BoqLine(sheet="S", row=2, item_no="1", description="Works", unit="m", quantity=qty)
        return BoqWorkbook(
            name="B", sheets={"S": BoqSheet("S", {}, lines=[line])}, sheet_order=["S"]
        )

    assert 0.1 * 3 != 0.3
    assert compare_bid(book(0.3), book(0.1 * 3)).quantity_changed == []
    assert len(compare_bid(book(0.3), book(0.31)).quantity_changed) == 1


def test_repeated_position_keeps_each_bidder_price_in_the_merged_workbook(tmp_path: Path) -> None:
    """Two template positions with the same number and description (boilerplate repeated under
    two sections) collapsed onto one key, so the first showed the second's price."""
    lines = [_line("1", "Excavation", 10, 5.0), _line("1", "Excavation", 20, 5.0)]
    template = read_boq(boq_file(tmp_path / "t.xlsx", lines), name="T")
    priced = [_line("1", "Excavation", 10, 7.0), _line("1", "Excavation", 20, 9.0)]
    bid = read_boq(boq_file(tmp_path / "b.xlsx", priced), name="B")
    out = write_merged_workbook(template, [bid], compare_all(template, [bid]), tmp_path / "m.xlsx")
    ws = load_workbook(out, data_only=True)["1. WORKS"]
    rate = MERGED_COLUMNS["rate_total"]
    rates = [ws.cell(r, rate).value for r in range(2, ws.max_row + 1) if ws.cell(r, 5).value == "B"]
    assert rates == [7.0, 9.0]


def test_rate_outliers_do_not_pool_repeated_positions(tmp_path: Path) -> None:
    from procurement_agent.checks.boq import rate_outliers

    lines = [_line("1", "Excavation", 10, 5.0), _line("1", "Excavation", 10, 5.0)]
    template = read_boq(boq_file(tmp_path / "t.xlsx", lines), name="T")
    # position one is priced ~1, position two ~10 by everyone: no bidder is an outlier on
    # either position, but pooled together the median sits in one group and flags the other
    bids = [
        read_boq(
            boq_file(
                tmp_path / f"b{i}.xlsx",
                [_line("1", "Excavation", 10, lo), _line("1", "Excavation", 10, hi)],
            ),
            name=f"B{i}",
        )
        for i, (lo, hi) in enumerate([(1.0, 10.0), (1.1, 11.0), (0.9, 9.0), (1.0, 10.5)])
    ]
    assert rate_outliers(template, compare_all(template, bids)) == []


def test_summary_sheet_is_found_whatever_its_case(tmp_path: Path) -> None:
    lines = [_line("1.1", "Concrete works", 10, 5.0)]
    path = boq_file(
        tmp_path / "b.xlsx", lines, summary=[("1", "TOTAL PRICE", 50.0)], summary_title="Summary"
    )
    assert read_boq(path).summary == {"1 TOTAL PRICE": 50.0}


def test_item_number_is_not_read_from_the_description_column(tmp_path: Path) -> None:
    headers = ["No", "Description", "Quantity", "Unit", "Rate in US $", "Amount in US $"]
    rows = [
        {
            "No": "1.1",
            "Description": "Concrete works",
            "Quantity": 1,
            "Unit": "m3",
            "Rate in US $": 2.0,
            "Amount in US $": 2.0,
        }
    ]
    book = read_boq(boq_file(tmp_path / "t.xlsx", rows, headers=headers))
    [line] = book.sheets["1. WORKS"].lines
    assert line.item_no == "1.1"


def test_cli_does_not_charge_rate_outliers_to_the_first_bidder(tmp_path: Path) -> None:
    from procurement_agent.checks import boq

    lines = [_line("1.1", "Concrete works", 10, 5.0)]
    t = boq_file(tmp_path / "t.xlsx", lines)
    bids = [
        boq_file(tmp_path / f"b{i}.xlsx", [_line("1.1", "Concrete works", 10, rate)])
        for i, rate in enumerate([5.0, 5.5, 4.5, 500.0])
    ]
    out = tmp_path / "m.xlsx"
    assert boq.main(["--template", str(t), "--bids", *map(str, bids), "--out", str(out)]) == 0
    ws = load_workbook(out)["Findings"]
    rows = [(ws.cell(r, 1).value, ws.cell(r, 3).value) for r in range(2, ws.max_row + 1)]
    assert ("b0", "BOQ.RATE_OUTLIER") not in rows
    assert ("(all bidders)", "BOQ.RATE_OUTLIER") in rows


# --- 3. spreadsheet numbers with three decimals --------------------------------------------------


def _xlsx(rows: list[list[object]], *, title: str = "WORKS") -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = title
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _compare_books(left: bytes, right: bytes) -> object:
    from procurement_agent.checks.compare import compare
    from procurement_agent.ingestion.excel_reader import read_formulas, read_workbook
    from procurement_agent.ingestion.segmenter import segments_from_workbook

    return compare(
        segments_from_workbook(read_workbook(left), read_formulas(left)),
        segments_from_workbook(read_workbook(right), read_formulas(right)),
        left_document_id="A",
        right_document_id="B",
        left_kind="xlsx",
        right_kind="xlsx",
    )


def test_three_decimal_quantity_change_is_a_small_delta_not_a_thousandfold_one() -> None:
    """'12.345' reads as twelve thousand three hundred and forty-five to parse_number, which
    takes a three-digit group after a dot for thousands. The reader carries the cell's value."""
    left = _xlsx([["Item", "Qty"], ["Concrete works", 12.345]])
    right = _xlsx([["Item", "Qty"], ["Concrete works", 12.35]])
    report = _compare_books(left, right)
    deltas = [d for h in report.hunks for d in h.deltas]  # type: ignore[attr-defined]
    assert len(deltas) == 1
    assert deltas[0].delta == pytest.approx(0.005)


def test_equal_three_decimal_values_written_differently_are_equal() -> None:
    left = _xlsx([["Item", "Qty"], ["Concrete works", 0.125]])
    right = _xlsx([["Item", "Qty"], ["Concrete works", 0.1250000000001]])
    assert _compare_books(left, right).hunks == []  # type: ignore[attr-defined]


def test_cells_carry_their_numeric_value() -> None:
    from procurement_agent.ingestion.excel_reader import read_workbook
    from procurement_agent.ingestion.segmenter import segments_from_workbook

    sheets = read_workbook(_xlsx([["Qty", 12.345, 7, True, "12.5"]]))
    by_ref = {c.ref: c for c in sheets[0].cells}
    assert by_ref["B1"].number == pytest.approx(12.345)
    assert by_ref["C1"].number == 7.0
    assert by_ref["D1"].number is None  # a boolean is not a quantity
    assert by_ref["E1"].number is None  # text stays text
    segs = {s.path: s for s in segments_from_workbook(sheets)}
    assert segs["WORKS!B1"].number == pytest.approx(12.345)


# --- 5. workbook caps and chunking cost ----------------------------------------------------------


def _reference_sheet_chunks(
    sheets: list[object], *, doc_id: str, max_tokens: int = 1200
) -> list[str]:
    """The pre-hardening chunker, kept verbatim as the oracle for the incremental one."""
    from openpyxl.utils import get_column_letter

    from procurement_agent.ingestion.chunker import estimate_tokens

    def md(sheet: object, rows: list[int], grid: dict[tuple[int, int], str]) -> str:
        cols = sorted({c for (r, c) in grid if r in rows})
        if not cols:
            return ""
        header = "| row | " + " | ".join(get_column_letter(c) for c in cols) + " |"
        sep = "|" + "---|" * (len(cols) + 1)
        lines = [f"Sheet: {sheet.name}", header, sep]  # type: ignore[attr-defined]
        for r in rows:
            lines.append(f"| {r} | " + " | ".join(grid.get((r, c), "") for c in cols) + " |")
        return "\n".join(lines)

    out: list[str] = []
    for sheet in sheets:
        grid = {(c.row, c.col): c.value for c in sheet.cells}  # type: ignore[attr-defined]
        rows = sorted({c.row for c in sheet.cells})  # type: ignore[attr-defined]
        batch: list[int] = []
        for r in rows:
            batch.append(r)
            if estimate_tokens(md(sheet, batch, grid)) > max_tokens and len(batch) > 1:
                batch.pop()
                out.append(md(sheet, batch, grid))
                batch = [r]
        if batch:
            out.append(md(sheet, batch, grid))
    return out


def test_incremental_chunking_matches_the_reference_exactly() -> None:
    """Rows whose column set grows mid-batch are where incremental accounting goes wrong."""
    from procurement_agent.ingestion.excel_reader import read_workbook, sheet_chunks

    rows: list[list[object]] = []
    for i in range(400):
        width = 1 + (i * 7) % 9  # the column set widens and narrows as rows go by
        rows.append([f"პოზიცია {i}" if i % 3 else f"item {i}"] + [i * 1.5] * (width - 1))
        if i % 50 == 0:
            rows.append([])  # empty rows are skipped, not rendered
    sheets = read_workbook(_xlsx(rows))
    for max_tokens in (60, 300, 1200):
        got = [c.text for c in sheet_chunks(sheets, doc_id="d", max_tokens=max_tokens)]
        assert got == _reference_sheet_chunks(sheets, doc_id="d", max_tokens=max_tokens)


def test_chunking_a_large_sheet_is_linear() -> None:
    import time

    from procurement_agent.ingestion.excel_reader import Cell, Sheet, sheet_chunks

    cells = [Cell(f"A{r}", r, 1, f"line {r} of the bill") for r in range(1, 20_001)]
    started = time.perf_counter()
    chunks = sheet_chunks([Sheet("BIG", cells, 20_000, 1)], doc_id="d")
    assert time.perf_counter() - started < 5.0
    assert len(chunks) > 10


def test_row_cap_is_reported_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    from procurement_agent.ingestion import excel_reader

    monkeypatch.setattr(excel_reader, "_MAX_ROWS", 5)
    notes: list[str] = []
    sheets = excel_reader.read_workbook(_xlsx([[f"r{i}"] for i in range(10)]), notes=notes)
    assert sheets[0].truncated_rows
    assert len(sheets[0].cells) == 5
    assert any("WORKS" in n and "5" in n for n in notes)


def test_column_and_sheet_caps_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from procurement_agent.ingestion import excel_reader

    monkeypatch.setattr(excel_reader, "_MAX_COLS", 3)
    monkeypatch.setattr(excel_reader, "_MAX_SHEETS", 1)
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["a", "b", "c", "d", "e"])
    wb.create_sheet("SECOND").append(["x"])
    buf = io.BytesIO()
    wb.save(buf)
    notes: list[str] = []
    sheets = excel_reader.read_workbook(buf.getvalue(), notes=notes)
    assert [s.name for s in sheets] == [ws.title]
    assert sheets[0].truncated_cols
    assert len(notes) == 2


def test_an_untruncated_workbook_has_no_notes() -> None:
    from procurement_agent.ingestion.excel_reader import read_workbook

    notes: list[str] = []
    read_workbook(_xlsx([["a", 1], ["b", 2]]), notes=notes)
    assert notes == []


# --- 4 and 13. AOA -------------------------------------------------------------------------------

from tests.unit.test_aoa_check import ACT, CONTRACT  # noqa: E402 - the known-good synthetic pair

_CUSTOMER_BANK = "ბანკის კოდი: AAAAGE22,\nანგარიშის ნომერი: GE29NB0000000101904917."
_CONTRACTOR_BANK = "ბანკის კოდი: BBBBGE22,\nანგარიშის ნომერი: GE11VL0000000123456789."
_ACT_BANK = "ბანკის კოდი: AAAAGE22; ა/ა: GE29NB0000000101904917"


def _two_bank_contract() -> str:
    """A contract listing both parties' bank details, the customer's first."""
    head = CONTRACT.split("4.3.")[0]
    return (
        head
        + f"4.3. შემკვეთის რეკვიზიტები: სს „ალფა ბანკი“, {_CUSTOMER_BANK}\n"
        + f"4.4. შემსრულებლის რეკვიზიტები: სს „ბეტა ბანკი“, {_CONTRACTOR_BANK}\n"
    )


def _aoa_errors(act: str, contract: str | None = None, **kw: object) -> list[str]:
    from procurement_agent.checks.aoa import check_aoa

    report = check_aoa(aoa_text=act, contract_text=contract or CONTRACT, **kw)  # type: ignore[arg-type]
    return [f.rule_id for f in report.errors]


def test_act_listing_only_the_contractors_bank_details_is_not_an_error() -> None:
    act = ACT.replace(_ACT_BANK, "ბანკის კოდი: BBBBGE22; ა/ა: GE11VL0000000123456789")
    assert "AOA.BANK" not in _aoa_errors(act, _two_bank_contract())


def test_act_bank_code_of_a_different_bank_is_still_an_error() -> None:
    """The client's planted mistake: a bank code naming a bank the contract never mentions."""
    act = ACT.replace("AAAAGE22", "CCCCGE22")
    assert "AOA.BANK" in _aoa_errors(act, _two_bank_contract())


def test_act_pairing_one_partys_account_with_the_other_partys_bank_is_an_error() -> None:
    """Both values appear in the contract, so a plain subset test passes - but the bank code
    belongs to the other party's account, and a payment would go astray."""
    act = ACT.replace(_ACT_BANK, "ბანკის კოდი: AAAAGE22; ა/ა: GE11VL0000000123456789")
    assert "AOA.BANK" in _aoa_errors(act, _two_bank_contract())


def test_act_cadastral_code_subset_of_the_contracts_is_not_an_error() -> None:
    contract = CONTRACT.replace(
        "(ს/კ: 01.02.03.456)", "(ს/კ: 01.02.03.456) და მიმდებარე ნაკვეთისთვის (ს/კ: 01.02.03.457)"
    )
    assert "AOA.CADASTRAL" not in _aoa_errors(ACT, contract)
    assert "AOA.CADASTRAL" in _aoa_errors(ACT.replace("01.02.03.456", "01.02.03.999"), contract)


def test_tax_wording_net_is_a_word_not_part_of_internet() -> None:
    act = ACT.replace("გადასახადების ჩათვლით", "ინტერნეტ მომსახურებისთვის")
    assert "AOA.TAX_WORDING" in _aoa_errors(act, contractor_type="natural_person")
    act = ACT.replace("გადასახადების ჩათვლით", "internet service")
    assert "AOA.TAX_WORDING" in _aoa_errors(act, contractor_type="natural_person")
    for net in ("NET ღირებულებით", "ნეტო ღირებულებით", "NET-ით"):
        act = ACT.replace("გადასახადების ჩათვლით", net)
        assert "AOA.TAX_WORDING" not in _aoa_errors(act, contractor_type="natural_person")


def test_project_name_is_compared_beyond_its_first_word() -> None:
    contract = CONTRACT.replace("„მზე“ პროექტისთვის", "„ბათუმის სკოლა“ პროექტისთვის")
    act = ACT.replace("პროექტ „მზე“", "პროექტ „ბათუმის სასტუმრო“")
    assert "AOA.PROJECT_NAME" in _aoa_errors(act, contract)


def test_project_name_tolerates_case_endings_on_every_word() -> None:
    contract = CONTRACT.replace("„მზე“ პროექტისთვის", "„ბათუმის სკოლა“ პროექტისთვის")
    act = ACT.replace("პროექტ „მზე“", "პროექტ „ბათუმის სკოლის“")
    assert "AOA.PROJECT_NAME" not in _aoa_errors(act, contract)
    assert _aoa_errors(act, contract) == []


def test_bank_finding_location_is_deterministic() -> None:
    from procurement_agent.checks.aoa import check_aoa

    act = ACT.replace(
        _ACT_BANK, "ბანკის კოდი: ZZZZGE22; ბანკის კოდი: YYYYGE22; ა/ა: GE29NB0000000101904917"
    )
    spans = {
        next(
            f.span
            for f in check_aoa(aoa_text=act, contract_text=CONTRACT).findings
            if f.rule_id == "AOA.BANK"
        )
        for _ in range(5)
    }
    assert len(spans) == 1
    assert act[slice(*next(iter(spans)))] == "YYYYGE22"  # the first in sorted order


def test_director_skip_note_is_added_once_per_party() -> None:
    from procurement_agent.checks.aoa import AoaChecker, DocumentView
    from procurement_agent.extraction.anchors import Party

    party = Party(name="შპს ბეტა სერვისი", id_code="987654321")
    act = DocumentView(
        "a",
        "",
        [Party(name=party.name, id_code=party.id_code, directors=["დავით მელაძე", "ლევან ხარაზი"])],
    )
    checker = AoaChecker(aoa=act, contract=DocumentView("c", "", [party]))
    checker.rule_parties()
    assert [s.rule_id for s in checker.skipped] == ["AOA.DIRECTOR"]


def test_running_the_checker_twice_does_not_duplicate_findings() -> None:
    from procurement_agent.checks.aoa import AoaChecker, DocumentView

    checker = AoaChecker(
        aoa=DocumentView.build("a", ACT.replace("100 (ასი)", "100 (ორასი)")),
        contract=DocumentView.build("c", CONTRACT),
    )
    first = checker.run()
    second = checker.run()
    assert first.findings == second.findings
    assert first.skipped == second.skipped


# --- 14. one amount helper for both checks (characterisation: outputs frozen from before) -------

_NUMBER_CASES = [
    ("48 750,00 GEL", 48750.0),
    ("1,250.50", 1250.5),
    ("1.250,50", 1250.5),
    ("abc", None),
    ("USD 25,000.00", 25000.0),
    ("12,5", 12.5),
    ("0", 0.0),
    ("total 3 350 ლარი და 20", 3350.0),
]
_AMOUNT_TEXTS = [
    (
        "ღირებულება შეადგენს 100 (ასი) ლარს და 48 750,00 (ორმოცდარვა ათას შვიდას ორმოცდაათი) ლარს.",
        [100.0, 48750.0],
        [(100, "ასი"), (48750, "ორმოცდარვა ათას შვიდას ორმოცდაათი")],
    ),
    ("Price USD 1,250.00, penalty 0.5% per day, 50 ლარი", [50.0, 1250.0], []),
    ("თანხა (ორასი) და (not a number) და (ოცდახუთი)", [], [(200, "ორასი"), (25, "ოცდახუთი")]),
    ("3 (სამი) ცალი, 200 (ორასი) ლარი, 5 (ხუთთთ) დღე", [200.0], [(3, "სამი"), (200, "ორასი")]),
]


@pytest.mark.parametrize(("value", "expected"), _NUMBER_CASES)
def test_number_of_is_the_old_as_number(value: str, expected: float | None) -> None:
    from procurement_agent.checks import amounts
    from procurement_agent.checks.compare import as_number

    assert amounts.number_of(value) == expected
    assert as_number(value) == expected
    assert amounts.number_of("x", 5) == 5.0


@pytest.mark.parametrize(("text", "money", "words"), _AMOUNT_TEXTS)
def test_amount_helpers_keep_their_outputs(
    text: str, money: list[float], words: list[tuple[int, str]]
) -> None:
    from procurement_agent.checks import amounts

    assert sorted(amounts.money_amounts(text)) == money
    assert amounts.word_amounts(text) == words


@pytest.mark.parametrize(("text", "money", "words"), _AMOUNT_TEXTS)
def test_money_hits_are_the_money_amounts_with_where_each_is_written(
    text: str, money: list[float], words: list[tuple[int, str]]
) -> None:
    from procurement_agent.checks import amounts

    hits = amounts.money_hits(text)
    assert sorted(h.value for h in hits) == money
    assert all(text[h.span[0] : h.span[1]] == h.raw and h.currency for h in hits)


def test_money_hits_keep_the_currency_and_the_document_order_and_drop_penalties() -> None:
    from procurement_agent.checks import amounts

    text = "Price USD 20.00; ღირებულება 1 000 ლარი; პირგასამტეხლო 50 ლარი."
    assert [(h.value, h.currency) for h in amounts.money_hits(text)] == [
        (20.0, "USD"),
        (1000.0, "GEL"),
    ]


def test_the_checks_read_every_chunks_verbatim_body_in_order() -> None:
    from procurement_agent.extraction.pipeline import chunks_text
    from procurement_agent.extraction.schemas import Provenance
    from procurement_agent.ingestion.chunker import Chunk

    chunks = [
        Chunk(id="d:0", text="სათაური\nპირველი", body="პირველი", provenance=Provenance()),
        Chunk(id="d:1", text="მხოლოდ ტექსტი", body="", provenance=Provenance()),
    ]
    assert chunks_text(chunks) == "პირველი\nმხოლოდ ტექსტი"


def test_aoa_amount_words_rule_keeps_its_outputs() -> None:
    from procurement_agent.checks.aoa import check_aoa

    report = check_aoa(aoa_text=_AMOUNT_TEXTS[3][0])
    got = [
        (f.severity, f.expected, f.actual, f.span)
        for f in report.findings
        if f.rule_id == "AOA.AMOUNT_WORDS"
    ]
    # the words themselves are marked, not the figure beside them
    assert got == [("warning", None, "ხუთთთ", (36, 41))]
    # with no contract to say which half is right, the figure stands and the words are corrected
    report = check_aoa(aoa_text="100 (ორასი) ლარი")
    assert [
        (f.severity, f.expected, f.actual)
        for f in report.findings
        if f.rule_id == "AOA.AMOUNT_WORDS"
    ] == [("error", "ასი", "ორასი")]


# --- 6 and 7. the model pass over a comparison ---------------------------------------------------

from tests.unit.test_compare_llm import (  # noqa: E402 - reuse the fixtures, not copy them
    NEW,
    OLD,
    FakeLLM,
    good_item,
    report_with_one_rewrite,
    segs,
)


def test_a_stored_run_records_what_it_was_asked_about() -> None:
    from procurement_agent.checks.compare_llm import hunks_digest, request_explanations

    report = report_with_one_rewrite()
    stored = request_explanations(report, FakeLLM([good_item(report.unexplained[0].id)]))
    assert stored.left_document_id == "L"
    assert stored.right_document_id == "R"
    assert stored.hunks_digest == hunks_digest(report)


def test_a_stored_run_for_another_left_document_is_not_applied() -> None:
    from procurement_agent.checks.compare import compare
    from procurement_agent.checks.compare_llm import apply_explanations, request_explanations

    report = report_with_one_rewrite()
    stored = request_explanations(report, FakeLLM([good_item(report.unexplained[0].id)]))
    other = compare(
        segs(["ხელშეკრულება № SY-2026/004", OLD]),  # type: ignore[arg-type]
        segs(["ხელშეკრულება № SY-2026/004", NEW]),  # type: ignore[arg-type]
        left_document_id="SOMETHING-ELSE",
        right_document_id="R",
    )
    out = apply_explanations(other, stored)
    assert out.llm_explained == 0
    assert any("different" in s.reason_en for s in out.skipped)


def test_a_stored_run_is_not_applied_when_the_hunks_have_shifted() -> None:
    """Hunk ids are positions (h1, h2, ...). A comparison that gained a hunk in front renumbers
    every one after it, and an answer about h1 would land on someone else's change."""
    from procurement_agent.checks.compare import compare
    from procurement_agent.checks.compare_llm import apply_explanations, request_explanations

    report = report_with_one_rewrite()
    stored = request_explanations(report, FakeLLM([good_item(report.unexplained[0].id)]))
    shifted = compare(
        segs(["ხელშეკრულება № SY-2026/005", OLD]),  # type: ignore[arg-type]
        segs(["ხელშეკრულება № SY-2026/004", NEW]),  # type: ignore[arg-type]
        left_document_id="L",
        right_document_id="R",
    )
    out = apply_explanations(shifted, stored)
    assert out.llm_explained == 0
    assert any("changed since" in s.reason_en for s in out.skipped)


def test_a_two_character_quote_proves_nothing() -> None:
    from procurement_agent.checks.compare_llm import explain

    report = report_with_one_rewrite()
    item = good_item(report.unexplained[0].id).model_copy(
        update={"quote_left": "აქ", "quote_right": "რე"}  # substrings of both texts
    )
    out = explain(report, FakeLLM([item]))
    assert out.llm_explained == 0
    assert any("too short" in s.reason_en for s in out.skipped)


def test_a_short_quote_that_is_the_whole_changed_word_is_accepted() -> None:
    from procurement_agent.checks.compare import compare
    from procurement_agent.checks.compare_llm import explain

    report = compare(
        segs(["პირობა: მიწოდება ხდება დროულად საწყობში."]),  # type: ignore[arg-type]
        segs(["პირობა: მიწოდება ხდება ხშირად საწყობში."]),  # type: ignore[arg-type]
        left_document_id="L",
        right_document_id="R",
    )
    [hunk] = report.unexplained
    item = good_item(hunk.id).model_copy(update={"quote_left": "დროულად", "quote_right": "ხშირად"})
    assert explain(report, FakeLLM([item])).llm_explained == 1


def test_rejection_notes_give_the_real_reason() -> None:
    from procurement_agent.checks.compare_llm import explain

    report = report_with_one_rewrite()
    hid = report.unexplained[0].id
    out = explain(report, FakeLLM([good_item("h999")]))
    assert any("not one of the changes" in s.reason_en for s in out.skipped)
    assert not any("quote" in s.reason_en for s in out.skipped)
    out = explain(report, FakeLLM([good_item(hid).model_copy(update={"summary_en": " "})]))
    assert any("empty" in s.reason_en for s in out.skipped)
    assert not any("quote" in s.reason_en for s in out.skipped)


def test_the_model_cannot_demote_a_negation() -> None:
    from procurement_agent.checks.compare import compare
    from procurement_agent.checks.compare_llm import explain

    report = compare(
        segs(["მიმწოდებელი პასუხისმგებელია დაზიანებაზე ტრანსპორტირებისას."]),  # type: ignore[arg-type]
        segs(["მიმწოდებელი არ არის პასუხისმგებელი დაზიანებაზე ტრანსპორტირებისას."]),  # type: ignore[arg-type]
        left_document_id="L",
        right_document_id="R",
    )
    [hunk] = report.unexplained
    item = good_item(hunk.id, material=False).model_copy(
        update={"quote_left": "პასუხისმგებელია", "quote_right": "არ არის პასუხისმგებელი"}
    )
    out = explain(report, FakeLLM([item]))
    after = out.hunk(hunk.id)
    assert after is not None
    assert after.explained_by == "llm"
    assert after.materiality == "substantive"
    assert out.counts == report.counts


def test_applying_the_same_run_twice_counts_once() -> None:
    from procurement_agent.checks.compare_llm import apply_explanations, request_explanations

    report = report_with_one_rewrite()
    hid = report.unexplained[0].id
    stored = request_explanations(report, FakeLLM([good_item(hid, material=False)]))
    once = apply_explanations(report, stored)
    twice = apply_explanations(once, stored)
    assert twice.counts == once.counts
    assert twice.llm_explained == once.llm_explained == 1
    assert twice.skipped == once.skipped


# --- 9. anchoring findings in the Word document --------------------------------------------------

from docx import Document  # noqa: E402

from procurement_agent.checks.findings import Finding  # noqa: E402


def _docx(tmp_path: Path, paragraphs: list[str], name: str = "doc.docx") -> Path:
    path = tmp_path / name
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    doc.save(path)
    return path


def _comment_count(path: Path) -> int:
    import zipfile

    with zipfile.ZipFile(path) as z:
        if "word/comments.xml" not in z.namelist():
            return 0
        return z.read("word/comments.xml").decode().count("<w:comment ")


_LONG_OLD = "5. " + "მიმწოდებელი ვალდებულია შეასრულოს სამუშაოები ხარისხიანად და დროულად, " * 4
_LONG_NEW = _LONG_OLD.replace("დროულად", "ნებისმიერ დროს", 1)


def test_a_long_changed_paragraph_is_anchored_in_the_word_file(tmp_path: Path) -> None:
    from procurement_agent.checks.compare import compare
    from procurement_agent.report.docx_annotate import annotate_docx

    left_head = "პირველი პუნქტი, რომელიც არ იცვლება."
    report = compare(
        segs([left_head, _LONG_OLD]),  # type: ignore[arg-type]
        segs([left_head, _LONG_NEW]),  # type: ignore[arg-type]
        left_document_id="L",
        right_document_id="R",
    )
    assert len(_LONG_NEW) > 200 and report.findings
    source = _docx(tmp_path, [left_head, _LONG_NEW.strip()])
    _, unanchored = annotate_docx(source, report.findings, tmp_path / "out.docx")
    assert unanchored == []


def test_distinct_long_changed_lines_are_not_merged_into_one_finding() -> None:
    from procurement_agent.checks.compare import compare

    shared = "ა" * 200
    left = [shared + " პირველი ძველი", shared + " მეორე ძველი"]
    right = [shared + " პირველი ახალი", shared + " მეორე ახალი"]
    report = compare(segs(left), segs(right), left_document_id="L", right_document_id="R")  # type: ignore[arg-type]
    changed = [f for f in report.findings if f.rule_id == "CMP.TEXT_CHANGED"]
    assert len(changed) == 2


def test_an_aoa_finding_anchors_on_the_documents_own_words(tmp_path: Path) -> None:
    """The act date finding says '2026-05-05'; the document says '2026 წლის 5 მაისი'."""
    from procurement_agent.checks.aoa import check_aoa
    from procurement_agent.report.docx_annotate import annotate_docx

    act = ACT.replace("2026 წლის 30 აპრილი\n", "2026 წლის 5 მაისი\n")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.ACT_DATE"]
    assert finding.quote and "მაისი" in finding.quote
    source = _docx(tmp_path, act.splitlines())
    _, unanchored = annotate_docx(source, [finding], tmp_path / "out.docx")
    assert unanchored == []


def test_a_findings_span_is_used_when_the_text_is_supplied(tmp_path: Path) -> None:
    from procurement_agent.report.docx_annotate import annotate_docx

    text = "პირველი ხაზი\nმეორე ხაზი რომელიც მონიშვნას საჭიროებს"
    start = text.index("მონიშვნას")
    finding = Finding(
        rule_id="X",
        severity="error",
        message_ka="ka",
        message_en="en",
        actual="normalised form",
        span=(start, start + len("მონიშვნას")),
    )
    source = _docx(tmp_path, text.splitlines())
    _, unanchored = annotate_docx(source, [finding], tmp_path / "a.docx")
    assert unanchored == [finding]  # no text: the span means nothing
    _, unanchored = annotate_docx(source, [finding], tmp_path / "b.docx", text=text)
    assert unanchored == []


def test_a_docm_file_is_refused_as_unsupported(tmp_path: Path) -> None:
    from procurement_agent.report.docx_annotate import UnsupportedDocumentError, annotate_docx

    source = _docx(tmp_path, ["x"], name="macro.docm")
    with pytest.raises(UnsupportedDocumentError):
        annotate_docx(source, [], tmp_path / "out.docx")


def test_a_corrupt_docx_is_refused_as_unsupported(tmp_path: Path) -> None:
    from procurement_agent.report.docx_annotate import UnsupportedDocumentError, annotate_docx

    source = tmp_path / "broken.docx"
    source.write_bytes(b"not a zip at all")
    with pytest.raises(UnsupportedDocumentError):
        annotate_docx(source, [], tmp_path / "out.docx")


def test_splitting_a_run_never_drops_a_drawing(tmp_path: Path) -> None:
    """``run.text = ...`` removes every child of the run but its properties, so splitting a run
    that holds an inline picture deleted the picture from the annotated copy."""
    import zipfile

    from docx.oxml import parse_xml

    from procurement_agent.report.docx_annotate import annotate_docx

    path = tmp_path / "pic.docx"
    doc = Document()
    run = doc.add_paragraph().add_run("ხელმოწერა: დავთ მელაძე")
    run._r.append(
        parse_xml(
            '<w:drawing xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
        )
    )
    doc.save(path)
    finding = Finding(
        rule_id="AOA.DIRECTOR",
        severity="error",
        message_ka="ka",
        message_en="en",
        actual="დავთ მელაძე",
    )
    out, unanchored = annotate_docx(path, [finding], tmp_path / "out.docx")
    assert unanchored == []
    with zipfile.ZipFile(out) as z:
        assert "<w:drawing" in z.read("word/document.xml").decode()


# --- 12. comparison details ----------------------------------------------------------------------


def _cell(
    text: str,
    *,
    row: int = 2,
    col: int = 6,
    formula: str | None = None,
    sheet: str = "WORKS",
    number: float | None = None,
) -> object:
    from openpyxl.utils import get_column_letter

    from procurement_agent.ingestion.segmenter import Segment

    ref = f"{get_column_letter(col)}{row}"
    return Segment(
        index=0,
        text=text,
        kind="sheet_cell",
        path=f"{sheet}!{ref}",
        sheet=sheet,
        row=row,
        col=col,
        formula=formula,
        number=number,
    )


def test_formula_finding_shows_the_formulas_not_the_cell_path() -> None:
    from procurement_agent.checks.compare import compare

    report = compare(
        [_cell("1000", formula="=D2*E2")],  # type: ignore[list-item]
        [_cell("1000", formula="=D2*E2+0")],  # type: ignore[list-item]
        left_document_id="A",
        right_document_id="B",
        left_kind="xlsx",
        right_kind="xlsx",
    )
    [finding] = report.findings
    assert finding.rule_id == "CMP.FORMULA"
    assert finding.expected == "=D2*E2"
    assert finding.actual == "=D2*E2+0"
    assert "WORKS!F2" in finding.message_en


# --- 10. OCR leniency only for scans -------------------------------------------------------------

_IBAN = "საბანკო რეკვიზიტები: ანგარიში GE11VL0000000123456789, ბანკის კოდი VELOGE22."


def test_a_homoglyph_in_a_born_digital_pdf_is_not_downgraded() -> None:
    from procurement_agent.checks.compare import compare

    swapped = _IBAN.replace("VELOGE22", "VEL" + chr(0x041E) + "GE22")  # Cyrillic O
    report = compare(
        segs([_IBAN]),
        segs([swapped]),
        left_document_id="L",  # type: ignore[arg-type]
        right_document_id="R",
        left_kind="pdf",
        right_kind="pdf",
        ocr=False,
    )
    assert report.hunks[0].materiality == "substantive"
    assert any(f.rule_id == "CMP.HOMOGLYPH" and f.severity == "warning" for f in report.findings)
    # a caller that does not say keeps the old reading: every PDF may be a scan
    lenient = compare(
        segs([_IBAN]),
        segs([swapped]),
        left_document_id="L",  # type: ignore[arg-type]
        right_document_id="R",
        left_kind="pdf",
        right_kind="pdf",
    )
    assert lenient.hunks[0].materiality == "cosmetic"


def test_text_layer_detection_tells_a_scan_from_a_born_digital_pdf() -> None:
    from procurement_agent.ingestion.segmenter import pdf_has_text_layer

    fixtures = Path(__file__).parent.parent / "fixtures"
    assert pdf_has_text_layer(fixtures / "offer_ka_en.pdf") is True
    assert pdf_has_text_layer(fixtures / "offer_ka_en_scanned.pdf") is False
    assert pdf_has_text_layer(fixtures / "contract_ka.docx") is False  # not a PDF at all


# --- 11. table cells are not dropped for matching some paragraph elsewhere -----------------------


def _doc_with_table(cells: list[str], paragraphs: list[str], *, inside: list[str]) -> object:
    """A DoclingDocument: loose paragraphs, then a one-row table, with ``inside`` emitted as
    text items *under* the table - which is how Docling's DOCX backend writes table content."""
    from docling_core.types.doc.document import DoclingDocument, TableCell, TableData
    from docling_core.types.doc.labels import DocItemLabel

    doc = DoclingDocument(name="t")
    for p in paragraphs:
        doc.add_text(label=DocItemLabel.PARAGRAPH, text=p)
    data = TableData(
        num_rows=1,
        num_cols=len(cells),
        table_cells=[
            TableCell(
                text=t,
                start_row_offset_idx=0,
                end_row_offset_idx=1,
                start_col_offset_idx=i,
                end_col_offset_idx=i + 1,
            )
            for i, t in enumerate(cells)
        ],
    )
    table = doc.add_table(data=data)
    for t in inside:
        doc.add_text(label=DocItemLabel.PARAGRAPH, text=t, parent=table)
    return doc


def test_a_pdf_table_cell_equal_to_a_paragraph_elsewhere_is_kept() -> None:
    from procurement_agent.ingestion.segmenter import segments_from_docling

    doc = _doc_with_table(["რაოდენობა", "100"], ["ფასი: 100", "100"], inside=[])
    segments = segments_from_docling(doc)  # type: ignore[arg-type]
    assert [s.text for s in segments if s.kind == "table_cell"] == ["რაოდენობა", "100"]


def test_a_docx_table_cell_already_emitted_as_its_own_paragraph_is_not_repeated() -> None:
    from procurement_agent.ingestion.segmenter import segments_from_docling

    doc = _doc_with_table(["რაოდენობა", "100"], [], inside=["რაოდენობა", "100"])
    segments = segments_from_docling(doc)  # type: ignore[arg-type]
    assert [s.text for s in segments] == ["რაოდენობა", "100"]
    assert all(s.kind == "text" for s in segments)


def test_invisible_characters_are_counted_not_just_collected() -> None:
    from procurement_agent.checks.compare import hidden_character_rules

    zwsp = chr(0x200B)
    assert hidden_character_rules(f"a{zwsp}b", f"a{zwsp}b{zwsp}") == ["CMP.INVISIBLE_CHARS"]


def test_cells_of_an_added_sheet_are_not_also_reported_as_moves() -> None:
    from procurement_agent.checks.compare import compare

    text = "Concrete works in foundations"
    # the removed sheet A held the text; the surviving sheet B gained a cell with the same text
    left = [_cell(text, row=5, col=2, sheet="A"), _cell("x", row=1, col=1, sheet="B")]
    right = [_cell("x", row=1, col=1, sheet="B"), _cell(text, row=5, col=2, sheet="B")]
    report = compare(
        left,
        right,
        left_document_id="L",
        right_document_id="R",  # type: ignore[arg-type]
        left_kind="xlsx",
        right_kind="xlsx",
    )
    assert report.counts.moved == 0
    kinds = sorted(h.rules[0] for h in report.hunks)
    assert kinds == ["CMP.SHEET_REMOVED", "CMP.TEXT_ADDED"]


def test_leftover_values_are_paired_by_similarity_not_position() -> None:
    """Two dates in a paragraph, one changed by a day and one dropped: pairing leftovers by
    position reported the surviving date as having changed into an unrelated one."""
    from procurement_agent.checks.compare import deltas_between

    left = "ვადა: 2026 წლის 1 მარტი და 2026 წლის 20 ოქტომბერი."
    right = "ვადა: 2026 წლის 21 ოქტომბერი."
    [delta_a, delta_b] = sorted(
        (d for d in deltas_between(left, right) if d.rule_id == "CMP.DATE"), key=lambda d: d.kind
    )
    assert (delta_a.kind, delta_a.days) == ("changed", 1)
    assert delta_b.kind == "removed"


def test_aligning_twenty_thousand_numeric_cells_is_not_quadratic() -> None:
    """Rows of nothing but numbers all share one row key, SequenceMatcher's worst case: the
    measured cost was 7.5 s for 10 000 such rows before the alignment trimmed the common
    prefix and suffix and anchored on unique rows first."""
    import time

    from procurement_agent.checks.compare import compare
    from procurement_agent.ingestion.segmenter import Segment

    def sheet(skip: int | None = None, bump: int | None = None) -> list[Segment]:
        out: list[Segment] = []
        for r in range(1, 10_001):
            if r == skip:
                continue
            for c in (1, 2):
                v = r * 1.5 + c + (1 if r == bump else 0)
                out.append(
                    Segment(
                        index=len(out),
                        text=f"{v:g}",
                        kind="sheet_cell",
                        path="",
                        sheet="S",
                        row=r,
                        col=c,
                        number=v,
                    )
                )
        return out

    started = time.perf_counter()
    report = compare(
        sheet(),
        sheet(bump=5_000),
        left_document_id="A",
        right_document_id="B",
        left_kind="xlsx",
        right_kind="xlsx",
    )
    assert time.perf_counter() - started < 3.0
    assert report.counts.changed == 2


def test_alignment_keeps_its_answer_on_ordinary_documents() -> None:
    """The fast path must not change what a small comparison reports."""
    from procurement_agent.checks.compare import _opcodes

    a = ["x", "a", "b", "c", "d", "y"]
    b = ["x", "a", "c", "d", "e", "y"]
    import difflib

    assert _opcodes(a, b) == difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()


# --- 15. small things ----------------------------------------------------------------------------


def test_a_sheet_of_nothing_but_uncalculated_formulas_is_still_segmented() -> None:
    from procurement_agent.ingestion.excel_reader import read_formulas, read_workbook
    from procurement_agent.ingestion.segmenter import segments_from_workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "DATA"
    ws.append(["Qty", 3])
    calc = wb.create_sheet("CALC")
    calc["A1"] = "=DATA!B1*2"  # written by a script: a formula with no cached value
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    segments = segments_from_workbook(read_workbook(data), read_formulas(data))
    assert "CALC!A1" in {s.path for s in segments}


def test_document_view_computes_anchors_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from procurement_agent.checks import aoa

    calls = 0
    real = aoa.find_anchors

    def counting(text: str) -> list[object]:
        nonlocal calls
        calls += 1
        return real(text)  # type: ignore[return-value]

    monkeypatch.setattr(aoa, "find_anchors", counting)
    view = aoa.DocumentView.build("a", ACT)
    for _ in range(5):
        view.anchors(aoa.FactType.project_name)
        view.bank("IBAN")
    assert calls == 1


# --- found while hardening -----------------------------------------------------------------------


def test_a_pipe_in_a_cell_is_escaped_for_the_table_only() -> None:
    """The Markdown table needs "\\|"; the comparison and its quotes need what the cell says."""
    from procurement_agent.ingestion.excel_reader import read_workbook, sheet_chunks
    from procurement_agent.ingestion.segmenter import segments_from_workbook

    sheets = read_workbook(_xlsx([["Type A|B"]]))
    assert [s.text for s in segments_from_workbook(sheets)] == ["Type A|B"]
    assert "Type A\\|B" in sheet_chunks(sheets, doc_id="d")[0].text


def test_a_control_character_in_a_finding_does_not_break_the_word_export(tmp_path: Path) -> None:
    from procurement_agent.report.docx_annotate import annotate_docx

    source = _docx(tmp_path, ["საფასური შეადგენს 100 ლარს."])
    finding = Finding(
        rule_id="X",
        severity="error",
        message_ka="ka\x0b",
        message_en="en\x01",
        expected="1\x0b00",
        actual="100",
    )
    out, unanchored = annotate_docx(source, [finding], tmp_path / "out.docx")
    assert unanchored == []
    Document(str(out))  # and it opens again


def test_the_excel_export_looks_hunks_up_once() -> None:
    """``ComparisonReport.hunk`` is a linear scan; calling it per diff row made the export
    quadratic on a 20 000-cell workbook pair."""
    import time

    from procurement_agent.checks.compare import compare
    from procurement_agent.ingestion.segmenter import Segment
    from procurement_agent.report.compare_xlsx import to_xlsx

    def sheet(offset: float) -> list[Segment]:
        return [
            Segment(
                index=r,
                text=f"{r + offset:g}",
                kind="sheet_cell",
                path=f"S!A{r}",
                sheet="S",
                row=r,
                col=1,
                number=r + offset,
            )
            for r in range(1, 4_001)
        ]

    report = compare(
        sheet(0),
        sheet(0.5),
        left_document_id="A",
        right_document_id="B",
        left_kind="xlsx",
        right_kind="xlsx",
    )
    assert len(report.hunks) == 4_000
    started = time.perf_counter()
    to_xlsx(report)
    assert time.perf_counter() - started < 3.0


def test_a_sheet_declaring_the_whole_grid_is_read_in_bounded_time(tmp_path: Path) -> None:
    """Two cells, at XFD1 and A1048576: openpyxl pads every row to the declared width, so an
    uncapped scan of this sheet is a billion empty values."""
    import time

    from procurement_agent.ingestion.excel_reader import read_workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws["A1"] = "start"
    ws["XFD1"] = "far right"
    ws["A1048576"] = "far down"
    buf = io.BytesIO()
    wb.save(buf)
    notes: list[str] = []
    started = time.perf_counter()
    [sheet] = read_workbook(buf.getvalue(), notes=notes)
    assert time.perf_counter() - started < 10.0
    assert [c.ref for c in sheet.cells] == ["A1"]
    assert sheet.truncated_cols


def test_the_declared_dimension_is_not_trusted_when_it_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workbook written without a <dimension> tag still has its caps checked by the scan."""
    from openpyxl.worksheet._read_only import ReadOnlyWorksheet

    from procurement_agent.ingestion import excel_reader

    monkeypatch.setattr(excel_reader, "_MAX_ROWS", 5)
    monkeypatch.setattr(ReadOnlyWorksheet, "max_row", property(lambda self: None))
    monkeypatch.setattr(ReadOnlyWorksheet, "max_column", property(lambda self: None))
    notes: list[str] = []
    [sheet] = excel_reader.read_workbook(_xlsx([[f"r{i}"] for i in range(10)]), notes=notes)
    assert sheet.truncated_rows and len(sheet.cells) == 5


def test_bank_details_laid_out_side_by_side_are_not_mispaired() -> None:
    """A two-column requisites table serialises as code A, code B, account 1, account 2, and
    account 1's nearest code is then B. Pairing by proximity would call a correct act wrong."""
    from procurement_agent.checks.aoa import check_aoa

    act = ACT.replace(
        _ACT_BANK,
        "ბანკის კოდი: AAAAGE22 ბანკის კოდი: BBBBGE22\n"
        "ა/ა: GE29NB0000000101904917 ა/ა: GE11VL0000000123456789",
    )
    report = check_aoa(aoa_text=act, contract_text=_two_bank_contract())
    assert "AOA.BANK" not in [f.rule_id for f in report.errors]
