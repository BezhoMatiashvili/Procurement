"""BOQ bills the fourth red-team round found the reader got wrong.

Each test is one such way: a header written in English and Georgian side by side, or worded
"Description of works" / "Price" / "ჯამური ფასი", a Summary headed so, a closing total labelled
outside the Description column, a contingency or VAT row computed from the sub-total, a VAT
spelt "V.A.T.", a share-of-total column beside the Summary's TOTAL, a per-hundred unit typed on
a Georgian keyboard, and the policies the lead decided for round 4. Every workbook is synthetic
and built in memory; every expected number is worked out here from the test's own data.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

import tests.boq_factory as factory
from procurement_agent.checks.boq import (
    _PLACEHOLDER,
    BidComparison,
    BoqWorkbook,
    _label_key,
    _line_kind,
    _plays,
    _summary_columns,
    compare_all,
    norm_unit,
    read_boq,
    reconcile_summary,
    template_findings,
)
from tests.unit.test_boq_reader_variants import Cells, Fx, _rules, _save

#: (item, description, unit, quantity, the bidder's rate)
_LINES = (
    ("1.1", "Trench excavation", "m3", 120.0, 14.0),
    ("1.2", "Blinding concrete", "m3", 30.0, 198.0),
    ("1.3", "Brick wall", "m2", 70.0, 43.5),
    ("1.4", "Wall plaster", "m2", 290.0, 21.0),
    ("1.5", "Floor screed", "m2", 250.0, 29.0),
)
_COST = sum(q * r for *_, q, r in _LINES)  # 1,680 + 5,940 + 3,045 + 6,090 + 7,250 = 24,005
_HEAD = ("No", "Description", "Unit", "Quantity", "Rate", "Amount")
_SUMMARY_HEAD = ("Item", "Description", "Total")
_SHEET = "1. WORKS"
#: overheads 10% and profit 5% on the cost, then VAT 18%
_MARKUP = 1.15 * 1.18


def _bill(
    factor: float | None,
    *,
    head: tuple[str, ...] = _HEAD,
    slip: dict[str, float] | None = None,
    close: str = "Sub-total",
    lines: tuple[tuple[object, str, str, float, float], ...] = _LINES,
) -> tuple[Cells, float]:
    """The bill at ``factor`` x the rates (None: the template, unpriced), a ``slip`` added into
    an amount formula by item; returns the cells and the sheet's closing total."""
    cells: Cells = {(1, c): h for c, h in enumerate(head, 1)}
    total = 0.0
    for r, (item, desc, unit, qty, rate) in enumerate(lines, 2):
        cells.update({(r, 1): item, (r, 2): desc, (r, 3): unit, (r, 4): qty})
        extra = (slip or {}).get(str(item), 0.0)
        if factor is None:
            cells[(r, 6)] = Fx(f"=D{r}*E{r}", 0.0)
            continue
        cells[(r, 5)] = rate * factor
        amount = qty * rate * factor + extra
        cells[(r, 6)] = Fx(f"=D{r}*E{r}" + (f"+{extra:g}" if extra else ""), amount)
        total += amount
    last = len(lines) + 1
    cells[(last + 1, 2)] = close
    cells[(last + 1, 6)] = Fx(f"=SUM(F2:F{last})", total)
    return cells, total


def _summary(
    cost: float, *, head: tuple[str, ...] = _SUMMARY_HEAD, closing: int = len(_LINES) + 2
) -> Cells:
    """Item | Description | Total, one section linked to the bill's closing row, and the chain."""
    oh, pr = cost * 0.10, cost * 0.05
    excl = cost + oh + pr
    vat = excl * 0.18
    cells: Cells = {(1, c): h for c, h in enumerate(head, 1)}
    rows = (
        ("1", "WORKS", Fx(f"='{_SHEET}'!F{closing}", cost)),
        ("", "TOTAL COST", Fx("=C2", cost)),
        ("", "Overheads 10%", Fx("=C3*0.1", oh)),
        ("", "Profit 5%", Fx("=C3*0.05", pr)),
        ("", "TOTAL PRICE EXCL. VAT", Fx("=C3+C4+C5", excl)),
        ("", "VAT 18%", Fx("=C6*0.18", vat)),
        ("", "TOTAL PRICE INCLUDING VAT", Fx("=C6+C7", excl + vat)),
    )
    for r, (item, label, value) in enumerate(rows, 2):
        if item:
            cells[(r, 1)] = item
        cells[(r, 2)] = label
        cells[(r, 3)] = value
    return cells


def _save_merged(path: Path, sheets: dict[str, Cells], merge: dict[str, str]) -> Path:
    """``_save`` with cell ranges merged (``{sheet: "A7:E7"}``), cached values kept."""
    wb = Workbook()
    default = wb.active
    assert default is not None
    wb.remove(default)
    cache: dict[tuple[int, str], object] = {}
    for index, (name, cells) in enumerate(sheets.items()):
        ws = wb.create_sheet(name)
        for (r, c), v in cells.items():
            cell = ws.cell(r, c, v.text if isinstance(v, Fx) else v)
            if isinstance(v, Fx):
                cache[(index, cell.coordinate)] = v.value
        if name in merge:
            ws.merge_cells(merge[name])
    buf = io.BytesIO()
    wb.save(buf)
    path.write_bytes(factory._with_cached_values(buf.getvalue(), cache))
    return path


def _workbook(
    path: Path,
    factor: float | None,
    *,
    head: tuple[str, ...] = _HEAD,
    summary_head: tuple[str, ...] = _SUMMARY_HEAD,
    slip: dict[str, float] | None = None,
) -> Path:
    bill, total = _bill(factor, head=head, slip=slip)
    return _save(path, {"SUMMARY": _summary(total, head=summary_head), _SHEET: bill})


def _compare(
    tmp_path: Path, bids: dict[str, tuple[float, dict[str, float] | None]], **layout: object
) -> tuple[BoqWorkbook, list[BidComparison]]:
    """The template and each bid (``factor``, ``slip``) under one layout, compared."""
    t = read_boq(_workbook(tmp_path / "t.xlsx", None, **layout), name="T")  # type: ignore[arg-type]
    books = [
        read_boq(_workbook(tmp_path / f"{n}.xlsx", f, slip=s, **layout), name=n)  # type: ignore[arg-type]
        for n, (f, s) in bids.items()
    ]
    return t, compare_all(t, books)


# --- R4-02: a header written in two languages, or worded the usual other ways ------------------


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("Description / აღწერა", "desc"),
        ("Description\nაღწერა", "desc"),
        ("Unit / ერთეული", "unit"),
        ("Quantity / რაოდენობა", "qty"),
        ("Unit rate / ერთეულის ფასი", "rate_total"),
        ("Amount / ღირებულება", "amount"),
        ("Amount | ღირებულება", "amount"),
        ("Unit rate - USD", "rate_total"),
        ("რაოდ.", "qty"),
        ("რ-ბა", "qty"),
        ("Quantity of work", "qty"),
        ("Quantity of works", "qty"),
        ("Description of work", "desc"),
        ("Description of Goods", "desc"),
        ("Description of the services", "desc"),
        ("Item description", "desc"),
        ("Work description", "desc"),
        ("Name of works", "desc"),
        ("Name of goods", "desc"),
        ("სამუშაოს დასახელება", "desc"),
        ("საქონლის დასახელება", "desc"),
        ("ზ/ე", "unit"),
    ],
)
def test_a_column_label_in_two_languages_or_worded_the_usual_way_is_read(
    label: str, key: str
) -> None:
    assert _label_key(label) == key


@pytest.mark.parametrize(
    "label",
    [
        "Description / Unit",  # two columns named in one cell: neither
        "Quantity / Rate",
        "Name",  # a bid form's "Name" is no description column
        "Name of bidder",
        "Name of the company",
        "Description of the bidder's offer",
        "Unit Price (excl. VAT)",  # a label that runs on after what it names (GOLD_SPEC 5)
        "Quantity remarks",
        "Price",  # bound only by where it stands (``_map_headers``)
        "ფასი",
    ],
)
def test_a_label_naming_no_one_column_is_not_read_as_one(label: str) -> None:
    assert _label_key(label) is None


_BILINGUAL = (
    "No",
    "Description / აღწერა",
    "Unit / ერთეული",
    "Quantity / რაოდენობა",
    "Unit rate / ერთეულის ფასი",
    "Amount / ღირებულება",
)
_TWO_LINES = tuple(label.replace(" / ", "\n") for label in _BILINGUAL)


@pytest.mark.parametrize(
    "head",
    [
        _BILINGUAL,
        _TWO_LINES,
        ("No", "Description of Goods", "Unit", "Quantity", "Unit Price", "Total Price"),
        ("No", "Name of works", "Unit", "Quantity of work", "Rate", "Amount"),
        ("№", "Item description", "ერთეული", "რაოდ.", "ერთეულის ფასი", "ღირებულება"),
        ("№", "დასახელება", "ერთეული", "რ-ბა", "ერთეულის ფასი", "ღირებულება"),
    ],
)
def test_a_bill_headed_in_two_languages_is_read_and_its_slip_reported(
    tmp_path: Path, head: tuple[str, ...]
) -> None:
    t, (a, b) = _compare(tmp_path, {"A": (1.0, {"1.4": 2000.0}), "B": (0.9, None)}, head=head)
    assert t.sheets[_SHEET].columns == {
        "no": 1,
        "desc": 2,
        "unit": 3,
        "qty": 4,
        "rate_total": 5,
        "amount": 6,
    }
    assert "BOQ.SHEET_UNREADABLE" not in [f.rule_id for f in template_findings(t)]
    [slip] = [f for f in a.findings if f.rule_id == "BOQ.ARITHMETIC"]
    assert (slip.actual, slip.expected) == ("8,090.00", "6,090.00")
    assert a.evaluated_cost == pytest.approx(_COST)
    assert b.evaluated_cost == pytest.approx(_COST * 0.9)
    assert (a.colour, b.colour) == ("red", "green")


# --- R4-08: "Price" by where it stands, and the Georgian total-price names --------------------


@pytest.mark.parametrize("label", ["ჯამური ფასი", "საერთო ფასი", "სულ ფასი", "საერთო ღირებულება"])
def test_the_georgian_total_price_names_are_the_line_amount(label: str) -> None:
    assert _label_key(label) == "amount"


@pytest.mark.parametrize("price", ["Price", "ფასი", "Price, GEL"])
def test_a_price_column_before_the_amount_is_the_unit_rate(tmp_path: Path, price: str) -> None:
    head = ("No", "Description", "Unit", "Qty", price, "Amount")
    t, (a, b) = _compare(tmp_path, {"A": (1.0, {"1.4": 2000.0}), "B": (0.9, None)}, head=head)
    assert (a.colour, b.colour) == ("red", "green")
    assert t.sheets[_SHEET].columns["rate_total"] == 5
    assert t.sheets[_SHEET].columns["amount"] == 6
    [slip] = [f for f in a.findings if f.rule_id == "BOQ.ARITHMETIC"]
    assert (slip.actual, slip.expected) == ("8,090.00", "6,090.00")
    assert a.evaluated_cost == pytest.approx(_COST)  # the rate, never amount / quantity
    assert "BOQ.COLUMNS_NOT_READ" not in [f.rule_id for f in template_findings(t)]


def test_a_price_column_with_no_amount_after_it_is_the_line_amount(tmp_path: Path) -> None:
    head = ("No", "Description", "Unit", "Qty", "Unit rate", "Price")
    t, _ = _compare(tmp_path, {"A": (1.0, None)}, head=head)
    assert t.sheets[_SHEET].columns["rate_total"] == 5
    assert t.sheets[_SHEET].columns["amount"] == 6


def test_a_price_column_beside_a_rate_already_read_takes_no_key(tmp_path: Path) -> None:
    """ "Unit rate | Price | Amount": the rate and the amount are read; "Price" between them is
    a column this reader cannot place, and is said so - it never takes a key twice over."""
    head = ("No", "Description", "Unit", "Qty", "Unit rate", "Price", "Amount")
    cells: Cells = {(1, c): h for c, h in enumerate(head, 1)}
    cells.update({(2, 1): "1.1", (2, 2): "Excavation", (2, 3): "m3", (2, 4): 10.0})
    cells.update({(2, 5): 4.0, (2, 6): 4.0, (2, 7): Fx("=D2*E2", 40.0)})
    t = read_boq(_save(tmp_path / "t.xlsx", {_SHEET: cells}))
    cols = t.sheets[_SHEET].columns
    assert (cols["rate_total"], cols["amount"]) == (5, 7)
    assert 6 not in cols.values()
    assert "BOQ.COLUMNS_NOT_READ" in [f.rule_id for f in template_findings(t)]


# --- R4-01: the Summary's header by the same names as the bill's ------------------------------


@pytest.mark.parametrize(
    "head",
    [
        ("Item", "Description of works", "Total"),
        ("№", "სამუშაოს დასახელება", "ღირებულება"),
        ("№", "სამუშაოების დასახელება", "ღირებულება"),
        ("No.", "Item description", "Amount"),
        ("Item", "Description", "Cost (GEL)"),
        ("Item", "Description", "Price"),
        ("№", "დასახელება", "ფასი"),
        ("No", "Description", "Sum"),
        ("№", "დასახელება", "ჯამური ფასი"),
        ("Item", "Description / აღწერა", "Total / სულ"),
    ],
)
def test_a_summary_headed_the_usual_other_ways_is_read_and_ranks(
    tmp_path: Path, head: tuple[str, ...]
) -> None:
    _, (a, b) = _compare(tmp_path, {"A": (1.0, None), "B": (0.9, None)}, summary_head=head)
    assert "BOQ.NOT_RANKED" not in _rules(a) + _rules(b)
    assert a.markup_factor == pytest.approx(_MARKUP)
    assert a.common_scope_total == pytest.approx(_COST * _MARKUP)
    assert (a.colour, b.colour) == ("red", "green")


def test_a_summary_column_only_starting_like_a_sum_is_not_the_total() -> None:
    """ "Summary notes" starts with "sum": read by a prefix it named the notes as the total."""

    ws = Workbook().active
    assert ws is not None
    for c, label in enumerate(("Item", "Description", "Summary notes", "Sum"), 1):
        ws.cell(1, c, label)
    ws.cell(2, 2, "WORKS")
    assert _summary_columns(ws) == (1, 2, 4, 1)


def test_a_summary_whose_header_is_not_recognised_says_so(tmp_path: Path) -> None:
    """The incl.-VAT row is there: "no Summary row states the total incl. VAT" sent the reader
    looking for the wrong thing. The header that was not recognised is what is named."""
    head = ("Ref", "Heading text", "Figure")
    _, (a,) = _compare(tmp_path, {"A": (1.0, None)}, summary_head=head)
    [note] = [f for f in a.findings if f.rule_id == "BOQ.NOT_RANKED"]
    assert "header" in note.message_en and "SUMMARY" in note.message_en
    assert "no Summary row states" not in note.message_en


# --- R4-04: a contingency or VAT row computed from the sub-total is no position ----------------


def test_a_product_over_another_row_without_a_measure_is_a_subtotal() -> None:
    """``=F7*E8`` on a row with no quantity and no unit multiplies the sub-total by a rate: a
    contingency or VAT row. With a measure it stays the position whose formula slipped."""
    values, formulas = Workbook(), Workbook()
    wf = formulas.active
    assert wf is not None
    wf.cell(8, 6, "=F7*E8")
    wf.cell(9, 6, "=F7*0.18")
    cols = {"desc": 2, "qty": 4, "rate_total": 5, "amount": 6}
    assert _line_kind(values.active, wf, cols, 8, "Contingency 10%", False) == "subtotal"
    assert _line_kind(values.active, wf, cols, 8, "Door stopper", True) == "position"
    assert _line_kind(values.active, wf, cols, 9, "VAT 18%", False) == "subtotal"


def _contingency_bill(factor: float | None) -> tuple[Cells, float]:
    """The bill, its Sub-total, a contingency 10% =F7*E8 and the total carried over."""
    cells, total = _bill(factor)
    sub = len(_LINES) + 2  # 7
    cells[(sub + 1, 2)] = "Contingency 10%"
    cells[(sub + 1, 5)] = 0.10
    cells[(sub + 1, 6)] = Fx(f"=F{sub}*E{sub + 1}", total * 0.10)
    cells[(sub + 2, 2)] = "TOTAL CARRIED TO SUMMARY"
    cells[(sub + 2, 6)] = Fx(f"=F{sub}+F{sub + 1}", total * 1.10)
    return cells, total * 1.10


def test_a_contingency_row_on_the_bill_is_neither_a_position_nor_a_gap(tmp_path: Path) -> None:
    paths = {}
    for name, factor in (("t", None), ("A", 1.0), ("B", 0.9)):
        cells, carried = _contingency_bill(factor)
        summary = _summary(carried)
        summary[(2, 3)] = Fx(f"='{_SHEET}'!F{len(_LINES) + 4}", carried)
        paths[name] = _save(tmp_path / f"{name}.xlsx", {"SUMMARY": summary, _SHEET: cells})
    t = read_boq(paths["t"], name="T")
    assert [ln.kind for ln in t.sheets[_SHEET].lines[-3:]] == ["subtotal"] * 3
    a, b = compare_all(t, [read_boq(paths[n], name=n) for n in ("A", "B")])
    assert len(t.sheets[_SHEET].leaf_lines) == len(_LINES)
    assert a.evaluated_cost == pytest.approx(_COST)
    assert b.evaluated_cost == pytest.approx(_COST * 0.9)
    for comp in (a, b):
        assert "BOQ.RECONCILE" not in _rules(comp)
        assert comp.filled_blank == []


# --- R4-07: a section sub-total in the middle of the sheet does not close it -------------------


def _sectioned_bill(factor: float | None, *, short: bool = False) -> tuple[Cells, float]:
    """1.1-1.2, "Sub-total Section 1", a heading, 2.1-2.3, and the grand total the Summary
    links to (``short``: its SUM leaves the last position out)."""
    cells: Cells = {(1, c): h for c, h in enumerate(_HEAD, 1)}
    rows = {3: _LINES[0], 4: _LINES[1], 7: _LINES[2], 8: _LINES[3], 9: _LINES[4]}
    cells[(2, 2)] = "Section 1"
    cells[(6, 2)] = "Section 2"
    amounts: dict[int, float] = {}
    for r, (item, desc, unit, qty, rate) in rows.items():
        cells.update({(r, 1): item, (r, 2): desc, (r, 3): unit, (r, 4): qty})
        amounts[r] = 0.0 if factor is None else qty * rate * factor
        if factor is not None:
            cells[(r, 5)] = rate * factor
        cells[(r, 6)] = Fx(f"=D{r}*E{r}", amounts[r])
    sec1 = amounts[3] + amounts[4]
    cells[(5, 2)] = "Sub-total Section 1"
    cells[(5, 6)] = Fx("=SUM(F3:F4)", sec1)
    last = 8 if short else 9
    grand = sec1 + sum(amounts[r] for r in range(7, last + 1))
    cells[(10, 2)] = "TOTAL CARRIED TO SUMMARY"
    cells[(10, 6)] = Fx(f"=F5+SUM(F7:F{last})", grand)
    return cells, grand


def _sectioned(tmp_path: Path, short: bool = False) -> tuple[BoqWorkbook, list[BidComparison]]:
    paths = {}
    for name, factor in (("t", None), ("A", 1.0), ("B", 0.9)):
        cells, grand = _sectioned_bill(factor, short=short and name == "A")
        summary = _summary(grand)
        summary[(2, 3)] = Fx(f"='{_SHEET}'!F10", grand)
        paths[name] = _save(tmp_path / f"{name}.xlsx", {"SUMMARY": summary, _SHEET: cells})
    t = read_boq(paths["t"], name="T")
    return t, compare_all(t, [read_boq(paths[n], name=n) for n in ("A", "B")])


def test_one_section_sub_total_with_positions_after_it_does_not_close_the_sheet(
    tmp_path: Path,
) -> None:
    t, (a, b) = _sectioned(tmp_path)
    assert t.sheets[_SHEET].subtotal_row == 10
    assert "BOQ.RECONCILE" not in _rules(a) + _rules(b)
    assert (a.colour, b.colour) == ("red", "green")


def test_the_grand_total_closing_a_sectioned_sheet_is_still_held_to_its_positions(
    tmp_path: Path,
) -> None:
    _, (a, _b) = _sectioned(tmp_path, short=True)
    [gap] = [f for f in a.findings if f.rule_id == "BOQ.RECONCILE" and "row 10" in f.message_en]
    assert gap.actual == "24,005.00" and gap.expected == "16,755.00"


def test_a_single_sub_total_after_the_last_position_still_closes_the_sheet(
    tmp_path: Path,
) -> None:
    """Below every position, the one SUB-TOTAL closes the sheet whatever follows it (a VAT row,
    a total carried over): the contingency bill above is closed at its Sub-total."""
    cells, _ = _contingency_bill(None)
    t = read_boq(_save(tmp_path / "t.xlsx", {_SHEET: cells}))
    assert t.sheets[_SHEET].subtotal_row == len(_LINES) + 2


# --- R4-05: a closing total labelled outside the Description column ---------------------------


def _label_elsewhere_bill(factor: float | None, column: int, *, short: bool) -> Cells:
    cells, total = _bill(factor)
    closing = len(_LINES) + 2
    del cells[(closing, 2)]
    cells[(closing, column)] = "TOTAL CARRIED TO SUMMARY"
    if short:  # the SUM leaves 1.5 out
        cells[(closing, 6)] = Fx(f"=SUM(F2:F{closing - 2})", total - _LINES[4][3] * _LINES[4][4])
    return cells


@pytest.mark.parametrize(("column", "merged"), [(1, True), (1, False), (5, False), (3, False)])
def test_a_closing_total_labelled_outside_the_description_column_is_read(
    tmp_path: Path, column: int, merged: bool
) -> None:
    closing = len(_LINES) + 2
    paths = {}
    for name, factor in (("t", None), ("A", 1.0)):
        cells = _label_elsewhere_bill(factor, column, short=name == "A")
        total = cells[(closing, 6)].value  # type: ignore[union-attr]
        sheets = {"SUMMARY": _summary(total), _SHEET: cells}
        merge = {_SHEET: f"A{closing}:E{closing}"} if merged else {}
        paths[name] = _save_merged(tmp_path / f"{name}.xlsx", sheets, merge)
    t = read_boq(paths["t"], name="T")
    closing_line = next(ln for ln in t.sheets[_SHEET].lines if ln.row == closing)
    assert t.sheets[_SHEET].subtotal_row == closing
    assert closing_line.kind == "subtotal" and closing_line.item_no == ""
    (a,) = compare_all(t, [read_boq(paths["A"], name="A")])
    [gap] = [f for f in a.findings if f.rule_id == "BOQ.RECONCILE" and _SHEET in f.message_en]
    assert gap.actual == "24,005.00" and gap.expected == "16,755.00"


def test_a_note_beside_an_empty_description_is_still_no_line(tmp_path: Path) -> None:
    """Only a sum row closing the bill is read by a label outside the Description column: a
    note typed in the item column, or a stray number beside one, is no line."""
    cells, total = _bill(1.0)
    cells[(20, 1)] = "Prices valid for 90 days"
    cells[(21, 1)] = "Note"
    cells[(21, 6)] = 125.0
    t = read_boq(_save(tmp_path / "t.xlsx", {"SUMMARY": _summary(total), _SHEET: cells}))
    assert all(ln.row < 20 for ln in t.sheets[_SHEET].lines)


# --- R4-11: a share-of-total column beside the Summary's TOTAL is no rate ---------------------


def _shared_summary(cost: float, *, overheads: float | None = None) -> Cells:
    """``_summary`` with "% of total" in column D on every row (=C{r}/$C$8)."""
    cells = _summary(cost)
    if overheads is not None:  # the overheads row states another sum than 10% of the cost
        oh = cells[(4, 3)]
        assert isinstance(oh, Fx)
        cells[(4, 3)] = Fx("=C3*0.1+100", overheads)
    grand = cells[(8, 3)]
    assert isinstance(grand, Fx)
    cells[(1, 4)] = "% of total"
    for r in range(2, 9):
        value = cells[(r, 3)]
        assert isinstance(value, Fx)
        cells[(r, 4)] = Fx(f"=C{r}/$C$8", float(value.value) / float(grand.value))
    return cells


def test_a_share_of_the_grand_total_beside_the_summary_is_not_read_as_a_rate(
    tmp_path: Path,
) -> None:
    bill, total = _bill(1.0)
    b = read_boq(_save(tmp_path / "b.xlsx", {"SUMMARY": _shared_summary(total), _SHEET: bill}))
    rows = {r.label: r for r in b.summary_rows}
    assert rows["Overheads 10%"].label_rate == pytest.approx(0.10)
    assert rows["Overheads 10%"].rate is None  # =C4/$C$8 divides the row's own total
    assert reconcile_summary(b) == []


def test_an_overheads_sum_off_its_labelled_rate_is_still_reported(tmp_path: Path) -> None:
    bill, total = _bill(1.0)
    cells = _shared_summary(total, overheads=total * 0.1 + 100)
    b = read_boq(_save(tmp_path / "b.xlsx", {"SUMMARY": cells, _SHEET: bill}))
    gaps = [f for f in reconcile_summary(b) if "row 4 (Overheads 10%)" in f.message_en]
    assert gaps and gaps[0].expected == "2,400.50"


@pytest.mark.parametrize("label", ["Profit 8%", "გეგმიური დაგროვება 8%"])
def test_profit_labelled_with_its_rate_on_cost_and_overheads_is_no_gap(label: str) -> None:
    """Georgian estimates put profit on cost + overheads. A rate read from the label only (no
    rate column) must not turn that faithful row into an error; on neither base it still is."""
    from procurement_agent.checks.boq import SummaryRow

    def chain(profit: float) -> BoqWorkbook:
        rows = [
            SummaryRow(row=3, item="", label="TOTAL COST", value=1000.0),
            SummaryRow(row=4, item="", label="Overheads 10%", value=100.0, label_rate=0.10),
            SummaryRow(row=5, item="", label=label, value=profit, label_rate=0.08),
        ]
        return BoqWorkbook(name="B", summary_rows=rows, summary_sheet="SUMMARY")

    assert reconcile_summary(chain(88.0)) == []  # 8% of 1,100
    assert reconcile_summary(chain(80.0)) == []  # 8% of 1,000
    [gap] = reconcile_summary(chain(95.0))
    assert "row 5" in gap.message_en


def test_profit_typed_beside_its_total_on_cost_and_overheads_is_no_gap() -> None:
    """The profit rate typed in the column beside the total, as most Georgian estimates set it
    out, holds on cost + overheads as a label's rate does: a faithful bid computed so was
    reported a Summary gap on every such bid. On neither base it still is."""
    from procurement_agent.checks.boq import SummaryRow

    def chain(profit: float) -> BoqWorkbook:
        rows = [
            SummaryRow(row=3, item="", label="TOTAL COST", value=1000.0),
            SummaryRow(row=4, item="", label="Overheads", value=100.0, rate=0.10),
            SummaryRow(row=5, item="", label="Profit", value=profit, rate=0.08),
        ]
        return BoqWorkbook(name="B", summary_rows=rows, summary_sheet="SUMMARY")

    assert reconcile_summary(chain(88.0)) == []  # 8% of 1,100
    assert reconcile_summary(chain(80.0)) == []  # 8% of 1,000
    [gap] = reconcile_summary(chain(95.0))
    assert "row 5" in gap.message_en


def test_a_typed_rate_beside_the_total_still_checks_its_row() -> None:
    """A rate typed in the column (0.12) where the label states none is the row's rate."""
    from procurement_agent.checks.boq import SummaryRow

    rows = [
        SummaryRow(row=3, item="", label="TOTAL COST", value=1000.0),
        SummaryRow(row=4, item="", label="Overheads", value=100.0, rate=0.12),
    ]
    book = BoqWorkbook(name="B", summary_rows=rows, summary_sheet="SUMMARY")
    [gap] = reconcile_summary(book)
    assert gap.expected == "120.00"


# --- R4-06: the tax spelt with dots ---------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "label"),
    [
        ("vat", "V.A.T. 18%"),
        ("vat", "Add V.A.T."),
        ("vat", "V. A. T."),
        ("vat", "დ.ღ.გ. 18%"),
        ("vat", "დ.ღ.გ 18%"),
        ("incl", "Total incl. V.A.T."),
        ("incl", "სულ დ.ღ.გ.-ს ჩათვლით"),
        ("incl", "სულ დ.ღ.გ-ის ჩათვლით"),
        ("excl", "Total excl. V.A.T."),
        ("excl", "სულ დ.ღ.გ.-ს გარეშე"),
    ],
)
def test_the_tax_spelt_with_dots_plays_its_part(role: str, label: str) -> None:
    assert _plays(role, label)


@pytest.mark.parametrize(
    "label", ["V.A.Tech services", "Private works", "Vatican tiles", "Elevat. shaft", "დ.ღ.გამოცდა"]
)
def test_letters_only_resembling_the_dotted_tax_are_not_the_vat_row(label: str) -> None:
    assert not _plays("vat", label)


def test_a_summary_chain_with_the_tax_spelt_with_dots_reconciles_and_ranks(
    tmp_path: Path,
) -> None:
    labels = {6: "Total excl. V.A.T.", 7: "V.A.T. 18%", 8: "Total incl. V.A.T."}
    paths = {}
    for name, factor in (("t", None), ("A", 1.0), ("B", 0.9)):
        bill, total = _bill(factor)
        summary = _summary(total)
        summary.update({(r, 2): label for r, label in labels.items()})
        paths[name] = _save(tmp_path / f"{name}.xlsx", {"SUMMARY": summary, _SHEET: bill})
    t = read_boq(paths["t"], name="T")
    a, b = compare_all(t, [read_boq(paths[n], name=n) for n in ("A", "B")])
    for comp in (a, b):
        assert "BOQ.RECONCILE" not in _rules(comp)
        assert "BOQ.NOT_RANKED" not in _rules(comp)
    assert a.markup_factor == pytest.approx(_MARKUP)
    assert (a.colour, b.colour) == ("red", "green")


# --- R4-03: a Summary section adding the bill's amount column --------------------------------


def _range_summary(total: float, stated: float, formula: str) -> Cells:
    cells = _summary(total)
    cells[(2, 3)] = Fx(formula, stated)
    return cells


@pytest.mark.parametrize(
    "formula",
    [f"=SUM('{_SHEET}'!F2:F6)", f"=SUM('{_SHEET}'!$F$2:$F$6)", f"=+SUM({_SHEET[3:]}!F2:F6)"],
)
def test_a_summary_row_adding_a_range_of_the_bill_is_held_against_the_range(
    tmp_path: Path, formula: str
) -> None:
    bill, total = _bill(1.0)
    sheet = _SHEET if "WORKS!" not in formula else "WORKS"
    summary = _range_summary(total, total, formula)
    b = read_boq(_save(tmp_path / "b.xlsx", {"SUMMARY": summary, sheet: bill}))
    assert reconcile_summary(b) == []
    assert b.sheets[sheet].subtotal_row == len(_LINES) + 2  # the range's first row is no link


def test_a_summary_row_stating_other_than_its_range_adds_up_is_reported(tmp_path: Path) -> None:
    bill, total = _bill(1.0)
    # a sum of the range whose cached value is not what the range adds up to
    summary = _range_summary(total, total + 500, f"=SUM('{_SHEET}'!F2:F6)")
    b = read_boq(_save(tmp_path / "b.xlsx", {"SUMMARY": summary, _SHEET: bill}))
    [gap] = [f for f in reconcile_summary(b) if "row 2 (WORKS)" in f.message_en]
    assert gap.expected == "24,005.00" and gap.actual == "24,505.00"
    assert "rows 2-6" in gap.message_en


def test_a_summary_row_scaling_a_range_is_not_held_against_it(tmp_path: Path) -> None:
    """``=SUM(range)*0.1`` (a contingency on the works) states no sum of the range."""
    bill, total = _bill(1.0)
    summary = _range_summary(total, total * 0.1, f"=SUM('{_SHEET}'!F2:F6)*0.1")
    b = read_boq(_save(tmp_path / "b.xlsx", {"SUMMARY": summary, _SHEET: bill}))
    assert not [f for f in reconcile_summary(b) if "row 2 (WORKS)" in f.message_en]


# --- R4-16 (policy): a single-sheet price schedule is ranked on its own closing rows -----------


def _schedule(
    factor: float | None, *, vat_cell: bool = False, vat_slip: float = 0.0, incl: bool = True
) -> Cells:
    """One sheet, no Summary: the bill, "Total", "VAT 18%" (=F7*0.18, or =F7*E8 with the rate
    typed in E8) and "Total incl. VAT"."""
    cells, total = _bill(factor, close="Total")
    sub = len(_LINES) + 2
    vat = total * 0.18 + vat_slip
    cells[(sub + 1, 2)] = "VAT 18%"
    if vat_cell:
        cells[(sub + 1, 5)] = 0.18
    formula = f"=F{sub}*E{sub + 1}" if vat_cell else f"=F{sub}*0.18"
    cells[(sub + 1, 6)] = Fx(formula + (f"+{vat_slip:g}" if vat_slip else ""), vat)
    if incl:
        cells[(sub + 2, 2)] = "Total incl. VAT"
        cells[(sub + 2, 6)] = Fx(f"=F{sub}+F{sub + 1}", total + vat)
    return cells


def _schedules(
    tmp_path: Path, **kw: object
) -> tuple[BoqWorkbook, list[BidComparison], dict[str, BoqWorkbook]]:
    slip = kw.pop("vat_slip", 0.0)
    books = {}
    for name, factor in (("t", None), ("A", 1.0), ("B", 0.9)):
        cells = _schedule(factor, vat_slip=slip if name == "A" else 0.0, **kw)  # type: ignore[arg-type]
        books[name] = read_boq(_save(tmp_path / f"{name}.xlsx", {_SHEET: cells}), name=name)
    t = books.pop("t")
    return t, compare_all(t, list(books.values())), books


@pytest.mark.parametrize("vat_cell", [False, True])
def test_a_single_sheet_schedule_is_ranked_on_its_own_closing_rows(
    tmp_path: Path, vat_cell: bool
) -> None:
    t, (a, b), _ = _schedules(tmp_path, vat_cell=vat_cell)
    assert len(t.sheets[_SHEET].leaf_lines) == len(_LINES)  # VAT is never a position
    assert a.evaluated_cost == pytest.approx(_COST)
    assert a.markup_factor == pytest.approx(1.18)
    assert a.common_scope_total == pytest.approx(_COST * 1.18)
    assert b.common_scope_total == pytest.approx(_COST * 0.9 * 1.18)
    assert (a.colour, b.colour) == ("red", "green")
    for comp in (a, b):
        assert not {"BOQ.NOT_RANKED", "BOQ.RECONCILE", "BOQ.FILLED_BLANK"} & set(_rules(comp))


def test_a_contingency_between_the_bills_total_and_its_vat_is_taxed_with_it(
    tmp_path: Path,
) -> None:
    """Total, Contingency 5%, VAT on both, Total incl. VAT: the contingency is part of what VAT
    is added to, and of the bidder's markup - never a gap in a faithful chain."""
    books = []
    for name, factor in (("t", None), ("A", 1.0), ("B", 0.9)):
        cells, total = _bill(factor, close="Total")
        sub = len(_LINES) + 2
        cont, vat = total * 0.05, total * 1.05 * 0.18
        rows = (
            ("Contingency 5%", f"=F{sub}*0.05", cont),
            ("VAT 18%", f"=(F{sub}+F{sub + 1})*0.18", vat),
            ("Total incl. VAT", f"=F{sub}+F{sub + 1}+F{sub + 2}", total * 1.05 + vat),
        )
        for r, (label, formula, value) in enumerate(rows, sub + 1):
            cells[(r, 2)] = label
            cells[(r, 6)] = Fx(formula, value)
        books.append(read_boq(_save(tmp_path / f"{name}.xlsx", {_SHEET: cells}), name=name))
    a, b = compare_all(books[0], books[1:])
    assert a.markup_factor == pytest.approx(1.05 * 1.18)
    assert "BOQ.RECONCILE" not in _rules(a) + _rules(b)
    assert (a.colour, b.colour) == ("red", "green")


def test_a_vat_row_under_the_bill_off_its_rate_is_reported(tmp_path: Path) -> None:
    _, (a, b), _ = _schedules(tmp_path, vat_slip=50.0)
    [gap] = [f for f in a.findings if f.rule_id == "BOQ.RECONCILE"]
    assert "row 8 (VAT 18%)" in gap.message_en and "row 7 x 0.18" in gap.message_en
    assert gap.actual == "4,370.90" and gap.expected == "4,320.90"
    assert "BOQ.RECONCILE" not in _rules(b)


def test_a_workbook_with_no_summary_and_no_total_incl_vat_says_why_it_is_not_ranked(
    tmp_path: Path,
) -> None:
    """Said where the bid is ranked or not (the skip note, the merged workbook's "Why"), never
    "no total incl. VAT on its Summary sheet" of a workbook that has none."""
    from procurement_agent.api.checks import _no_basis_note
    from procurement_agent.checks.boq import _explain

    _, (a, b), _ = _schedules(tmp_path, incl=False)
    for comp in (a, b):
        assert comp.markup_factor is None and comp.totals_from is None
        skip = _no_basis_note(comp)
        assert skip is not None and "no Summary sheet" in skip.reason_en
        assert "on its Summary sheet" not in skip.reason_en
        assert "no Summary sheet" in _explain(comp)
    assert (a.colour, b.colour) == (None, None)


def test_a_single_sheet_schedule_states_its_totals_under_the_bill(tmp_path: Path) -> None:
    from procurement_agent.api.checks import _no_basis_note

    _, (a, _b), _ = _schedules(tmp_path)
    assert a.totals_from == "bill"
    assert _no_basis_note(a) is None


def test_a_bid_dropping_the_templates_summary_is_not_ranked_on_its_bill(tmp_path: Path) -> None:
    """The template has a Summary; a bid deleting it is not ranked (GOLD_SPEC 5), whatever
    totals it writes under its bill - never told so and coloured at once."""
    bill, total = _bill(None)
    t = read_boq(_save(tmp_path / "t.xlsx", {"SUMMARY": _summary(total), _SHEET: bill}), name="T")
    a = read_boq(_save(tmp_path / "a.xlsx", {_SHEET: _schedule(1.0)}), name="A")
    b_bill, b_total = _bill(0.9)
    b_path = _save(tmp_path / "b.xlsx", {"SUMMARY": _summary(b_total), _SHEET: b_bill})
    a_comp, b_comp = compare_all(t, [a, read_boq(b_path, name="B")])
    assert "BOQ.SHEET_REMOVED" in _rules(a_comp)
    assert a_comp.markup_factor is None and a_comp.colour is None
    assert "BOQ.RECONCILE" not in _rules(a_comp)
    assert b_comp.colour is None  # one ranked bidder: no colours


def test_a_workbook_with_several_priced_sheets_and_no_summary_is_not_ranked_on_one(
    tmp_path: Path,
) -> None:
    """Only a single-sheet schedule states its totals under its bill: with two priced sheets the
    totals of one are not the bid's."""
    books = {}
    for name, factor in (("t", None), ("A", 1.0)):
        sheets = {_SHEET: _schedule(factor), "2. MORE": _bill(factor)[0]}
        books[name] = read_boq(_save(tmp_path / f"{name}.xlsx", sheets), name=name)
    assert books["A"].summary_rows == [] and books["A"].summary_in_bill is None
    (a,) = compare_all(books["t"], [books["A"]])
    assert a.markup_factor is None and a.totals_from is None


# --- R4-09 / R4-18: a unit with a multiplier, and the lump sum's names --------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("100 m2", "100 მ2"),
        ("1000 m3", "1000 მ3"),
        ("100 m", "100 მ"),
        ("100 m", "100 lm"),
        ("100 m2", "100m²"),
        ("100 м2", "100 m2"),  # Cyrillic
        ("1,5 t", "1,5 ტ"),
        ("LS", "Lump sum"),
        ("LS", "lump-sum"),
        ("L.S.", "Lumpsum"),
        ("lump sum", "Lump Sum"),
    ],
)
def test_one_unit_typed_another_way_is_one_unit(a: str, b: str) -> None:
    assert norm_unit(a) == norm_unit(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("100 m2", "m2"),  # per hundred is not per one
        ("100 m2", "1000 m2"),
        ("100 m2", "100 m3"),
        ("LS", "item"),  # a lump sum is not a count, a litre or a set
        ("LS", "l"),
        ("Lump sum", "set"),
        ("Lump sum", "pcs"),
        ("lump-sum", "each"),
    ],
)
def test_different_units_with_a_multiplier_or_a_lump_sum_stay_apart(a: str, b: str) -> None:
    assert norm_unit(a) != norm_unit(b)


def test_units_typed_on_a_georgian_keyboard_are_no_unit_change(tmp_path: Path) -> None:
    def units(path: Path, factor: float | None, names: tuple[str, ...]) -> Path:
        bill, total = _bill(factor)
        for r, name in enumerate(names, 2):
            bill[(r, 3)] = name
        return _save(path, {"SUMMARY": _summary(total), _SHEET: bill})

    latin = ("100 m2", "1000 m3", "100 m", "LS", "m2")
    georgian = ("100 მ2", "1000 მ3", "100 მ", "Lump sum", "მ2")
    t = read_boq(units(tmp_path / "t.xlsx", None, latin), name="T")
    (k,) = compare_all(t, [read_boq(units(tmp_path / "k.xlsx", 1.0, georgian), name="K")])
    assert "BOQ.UNIT_CHANGED" not in _rules(k)


# --- R4-10: "to be specified by the Engineer" asks nothing of the bidder ------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Other floor finishes: ceramic tiles, type and colour to be specified by the Engineer",
        "Other fittings, to be specified by the Employer",
        "Others as listed in the drawings, to be specified by the Supervisor",
        "სხვა მოპირკეთება, ფერი დამკვეთის მიერ დაკონკრეტდება",
    ],
)
def test_a_position_specified_by_the_engineer_is_described_work(text: str) -> None:
    assert not _PLACEHOLDER.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "Other works (please specify)",
        "Other items, to be specified by the bidder",
        "Others - to be listed by the tenderer",
        "Other costs to be specified",
        "Other items not included above",
    ],
)
def test_an_other_position_asking_the_bidder_is_still_a_placeholder(text: str) -> None:
    assert _PLACEHOLDER.search(text)


# --- R4-12: a bill priced by its component rates only -----------------------------------------


def _components_bill(rates: tuple[float, float, float] | None) -> Cells:
    head = ("No", "Description", "Unit", "Qty", "Materials", "Labour", "Machinery")
    cells: Cells = {(1, c): h for c, h in enumerate(head, 1)}
    for r, (item, desc, unit, qty, _) in enumerate(_LINES[:2], 2):
        cells.update({(r, 1): item, (r, 2): desc, (r, 3): unit, (r, 4): qty})
        if rates is not None:
            cells.update({(r, 5): rates[0], (r, 6): rates[1], (r, 7): rates[2]})
    return cells


def test_a_bill_priced_by_its_components_only_is_priced_and_shown_whole(tmp_path: Path) -> None:
    from procurement_agent.checks.boq import MERGED_COLUMNS, write_merged_workbook

    t = read_boq(_save(tmp_path / "t.xlsx", {_SHEET: _components_bill(None)}), name="T")
    a = read_boq(_save(tmp_path / "a.xlsx", {_SHEET: _components_bill((7.0, 4.2, 2.8))}), name="A")
    (comp,) = compare_all(t, [a])
    assert comp.evaluated_cost == pytest.approx((120.0 + 30.0) * 14.0)
    assert "BOQ.SECTION_UNPRICED" not in _rules(comp)
    assert comp.unpriced_sheets == []

    out = write_merged_workbook(t, [a], [comp], tmp_path / "m.xlsx")
    ws = load_workbook(out)[_SHEET]
    header = [c.value for c in ws[1]]
    assert "Machinery" in header
    a_row = next(
        r for r in range(2, ws.max_row + 1) if ws.cell(r, MERGED_COLUMNS["bidder"]).value == "A"
    )
    assert ws.cell(a_row, MERGED_COLUMNS["rate_machinery"]).value == pytest.approx(2.8)
    assert ws.cell(a_row, MERGED_COLUMNS["rate_total"]).value == pytest.approx(14.0)
    assert "component rates added" in ws.cell(a_row, MERGED_COLUMNS["analysis"]).value


def test_a_bill_with_no_price_at_all_is_still_unpriced(tmp_path: Path) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {_SHEET: _components_bill(None)}), name="T")
    a = read_boq(_save(tmp_path / "a.xlsx", {_SHEET: _components_bill((0.0, 0.0, 0.0))}), name="A")
    (comp,) = compare_all(t, [a])
    assert comp.unpriced_sheets == [_SHEET]


# --- R4-13 (policy): the offered make or model written after the template's description -------

_GOODS = (
    ("1", "Desktop monitor 24 inch IPS", "pcs", 30.0, 410.0),
    ("2", "Laser printer A4 mono", "pcs", 6.0, 520.0),
    ("3", "Network switch 24 port", "pcs", 4.0, 690.0),
    ("4", "UPS 1500 VA", "pcs", 8.0, 330.0),
    ("5", "Keyboard and mouse set", "set", 30.0, 45.0),
)
_GOODS_COST = sum(q * r for *_, q, r in _GOODS)


def _goods(
    tmp_path: Path, bids: dict[str, tuple[float, tuple[tuple[object, str, str, float, float], ...]]]
) -> tuple[BoqWorkbook, list[BidComparison]]:
    closing = len(_GOODS) + 2
    bill, total = _bill(None, lines=_GOODS)
    t_path = _save(tmp_path / "t.xlsx", {"SUMMARY": _summary(total, closing=closing), _SHEET: bill})
    books = []
    for name, (factor, lines) in bids.items():
        bill, total = _bill(factor, lines=lines)
        summary = _summary(total, closing=closing)
        books.append(read_boq(_save(tmp_path / f"{name}.xlsx", {"SUMMARY": summary, _SHEET: bill})))
    t = read_boq(t_path, name="T")
    return t, compare_all(t, books)


_OFFERS = (
    " / Model M24-I",
    " - offered: LP-2040 (brand Y)",
    ", model SW-24G",
    " (UPS-1500X)",
    ": KM-7",
)


def test_a_bid_writing_its_offer_after_the_template_description_keeps_the_position(
    tmp_path: Path,
) -> None:
    offered = tuple(
        (item, desc + offer, unit, qty, rate)
        for (item, desc, unit, qty, rate), offer in zip(_GOODS, _OFFERS, strict=True)
    )
    _, (a, b) = _goods(tmp_path, {"A": (1.0, _GOODS), "B": (0.9, offered)})
    assert b.added == [] and b.removed == []
    assert b.evaluated_cost == pytest.approx(_GOODS_COST * 0.9)
    assert b.common_scope_positions == len(_GOODS)
    notes = [f for f in b.findings if f.rule_id == "BOQ.DESCRIPTION_CHANGED"]
    assert len(notes) == len(_GOODS) and {f.severity for f in notes} == {"info"}
    assert (a.colour, b.colour) == ("red", "green")


@pytest.mark.parametrize(
    ("template", "bid"),
    [
        ("Pipe 20", "Pipe 200 mm"),  # the template's text runs on into another word
        ("Desktop monitor 24 inch", "Desktop monitor 24 inches IPS"),
        ("UPS 1500 VA", "Line-interactive UPS 1500 VA"),  # not where the template's text starts
    ],
)
def test_a_bid_description_only_starting_like_the_templates_is_another_position(
    tmp_path: Path, template: str, bid: str
) -> None:
    t_lines = (("1", template, "pcs", 2.0, 10.0), ("2", "Cable tray", "m", 5.0, 3.0))
    b_lines = (("1", bid, "pcs", 2.0, 10.0), ("2", "Cable tray", "m", 5.0, 3.0))
    t = read_boq(_save(tmp_path / "t.xlsx", {_SHEET: _bill(None, lines=t_lines)[0]}))
    b = read_boq(_save(tmp_path / "b.xlsx", {_SHEET: _bill(1.0, lines=b_lines)[0]}))
    (comp,) = compare_all(t, [b])
    assert [ln.item_no for ln in comp.removed] == ["1"]
    assert [ln.item_no for ln in comp.added] == ["1"]


def test_an_offer_under_another_item_number_is_not_the_position(tmp_path: Path) -> None:
    t_lines = (("1", "Laser printer A4 mono", "pcs", 2.0, 10.0),)
    b_lines = (("7", "Laser printer A4 mono / LP-2040", "pcs", 2.0, 10.0),)
    t = read_boq(_save(tmp_path / "t.xlsx", {_SHEET: _bill(None, lines=t_lines)[0]}))
    b = read_boq(_save(tmp_path / "b.xlsx", {_SHEET: _bill(1.0, lines=b_lines)[0]}))
    (comp,) = compare_all(t, [b])
    assert len(comp.removed) == 1 and len(comp.added) == 1


# --- R4-19 (policy): an item number the bidder's spreadsheet stored as a number ----------------

_ELEVEN = tuple(
    (f"1.{n}", f"Work item number {n} described in full", "m2", 10.0 + n, 5.0 + n)
    for n in range(1, 12)
)


def test_an_item_number_stored_as_a_number_is_still_the_position(tmp_path: Path) -> None:
    """ "1.10" typed into a General cell reads 1.1: the same line, word for word, in its place."""
    numbers = tuple((float(item), *rest) for item, *rest in _ELEVEN)
    t = read_boq(_save(tmp_path / "t.xlsx", {_SHEET: _bill(None, lines=_ELEVEN)[0]}))
    b = read_boq(_save(tmp_path / "b.xlsx", {_SHEET: _bill(1.0, lines=numbers)[0]}))
    assert [ln.item_no for ln in b.sheets[_SHEET].leaf_lines][9] == "1.1"
    (comp,) = compare_all(t, [b])
    assert comp.added == [] and comp.removed == []
    assert comp.common_scope_positions == len(_ELEVEN)
    assert comp.evaluated_cost == pytest.approx(sum(q * r for *_, q, r in _ELEVEN))


@pytest.mark.parametrize("case", ["text", "description"])
def test_an_item_retyped_or_redescribed_is_not_taken_for_a_stored_number(
    tmp_path: Path, case: str
) -> None:
    """Typed as the text "1.1", it is a renumbering (by design); a number on another line's
    description is another line."""
    lines = list(_ELEVEN)
    item, desc, *rest = lines[9]
    if case == "text":
        lines[9] = ("1.1", desc, *rest)  # type: ignore[assignment]
    else:
        lines[9] = (1.1, "Another work item altogether", *rest)  # type: ignore[assignment]
    t = read_boq(_save(tmp_path / "t.xlsx", {_SHEET: _bill(None, lines=_ELEVEN)[0]}))
    b = read_boq(_save(tmp_path / "b.xlsx", {_SHEET: _bill(1.0, lines=tuple(lines))[0]}))
    (comp,) = compare_all(t, [b])
    assert [ln.item_no for ln in comp.removed] == [item]


# --- R4-14 (policy): a position whose quantity adds up earlier positions ----------------------


def _linked_take_off(factor: float | None) -> tuple[Cells, float]:
    """1.1-1.5, then 1.6 "Painting" whose area is the two plastering areas (=D4+D5) at its own
    rate (=D7*E7), and the Total."""
    cells, _ = _bill(factor)
    del cells[(7, 2)], cells[(7, 6)]
    rate = 6.0 * (factor or 0.0)
    area = _LINES[2][3] + _LINES[3][3]
    cells.update({(7, 1): "1.6", (7, 2): "Painting", (7, 3): "m2", (7, 4): Fx("=D4+D5", area)})
    if factor is not None:
        cells[(7, 5)] = rate
    cells[(7, 6)] = Fx("=D7*E7", area * rate)
    total = (_COST * factor if factor is not None else 0.0) + area * rate
    cells[(8, 2)] = "Total"
    cells[(8, 6)] = Fx("=SUM(F2:F7)", total)
    return cells, total


def test_a_position_whose_quantity_adds_earlier_positions_is_evaluated(tmp_path: Path) -> None:
    paths = {}
    for name, factor in (("t", None), ("A", 1.0), ("B", 0.9)):
        cells, total = _linked_take_off(factor)
        sheets = {"SUMMARY": _summary(total, closing=8), _SHEET: cells}
        paths[name] = _save(tmp_path / f"{name}.xlsx", sheets)
    t = read_boq(paths["t"], name="T")
    assert next(ln.kind for ln in t.sheets[_SHEET].lines if ln.item_no == "1.6") == "position"
    a, b = compare_all(t, [read_boq(paths[n], name=n) for n in ("A", "B")])
    painting = (_LINES[2][3] + _LINES[3][3]) * 6.0
    assert a.evaluated_cost == pytest.approx(_COST + painting)
    assert b.evaluated_cost == pytest.approx((_COST + painting) * 0.9)
    for comp in (a, b):
        assert not {"BOQ.RECONCILE", "BOQ.HEADING_PRICED"} & set(_rules(comp))


def test_a_row_whose_quantity_adds_rows_and_which_has_no_amount_is_still_a_parent() -> None:
    values, formulas = Workbook(), Workbook()
    wf = formulas.active
    assert wf is not None
    wf.cell(6, 4, "=D2+D3")
    wf.cell(7, 4, "=SUM(D8:D9)")
    wf.cell(7, 6, "=SUM(F8:F9)")
    cols = {"desc": 2, "qty": 4, "rate_total": 5, "amount": 6}
    assert _line_kind(values.active, wf, cols, 6, "Painting", True) == "heading"
    assert _line_kind(values.active, wf, cols, 7, "Lighting", True) == "heading"
