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
from openpyxl import Workbook

import tests.boq_factory as factory
from procurement_agent.checks.boq import (
    BidComparison,
    BoqWorkbook,
    _label_key,
    _line_kind,
    _summary_columns,
    compare_all,
    read_boq,
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
) -> tuple[Cells, float]:
    """The bill at ``factor`` x the rates (None: the template, unpriced), a ``slip`` added into
    an amount formula by item; returns the cells and the sheet's closing total."""
    cells: Cells = {(1, c): h for c, h in enumerate(head, 1)}
    total = 0.0
    for r, (item, desc, unit, qty, rate) in enumerate(_LINES, 2):
        cells.update({(r, 1): item, (r, 2): desc, (r, 3): unit, (r, 4): qty})
        extra = (slip or {}).get(item, 0.0)
        if factor is None:
            cells[(r, 6)] = Fx(f"=D{r}*E{r}", 0.0)
            continue
        cells[(r, 5)] = rate * factor
        amount = qty * rate * factor + extra
        cells[(r, 6)] = Fx(f"=D{r}*E{r}" + (f"+{extra:g}" if extra else ""), amount)
        total += amount
    last = len(_LINES) + 1
    cells[(last + 1, 2)] = close
    cells[(last + 1, 6)] = Fx(f"=SUM(F2:F{last})", total)
    return cells, total


def _summary(cost: float, *, head: tuple[str, ...] = _SUMMARY_HEAD) -> Cells:
    """Item | Description | Total, one section linked to the bill's closing row, and the chain."""
    closing = len(_LINES) + 2
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
