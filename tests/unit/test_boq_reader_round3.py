"""BOQ bills the third red-team round found the reader got wrong.

Each test is one such way: a number typed as text with a stray separator, a unit written as a
plural or a full name, a Summary VAT row worded "Total VAT", a line amount headed "Cost" or
"თანხა", a unit written where the template left none, bidders tied on a half cent, a machinery
cost component, and a bid-form block above the bill. Every workbook is synthetic and built in
memory; every expected number is worked out here from the test's own data.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from procurement_agent.checks.boq import (
    BidComparison,
    BoqLine,
    BoqWorkbook,
    SummaryRow,
    _arithmetic_findings,
    _colour,
    _colour_extremes,
    _label_key,
    _num,
    _plays,
    _summary_columns,
    _unit_rate,
    cents,
    compare_all,
    compare_bid,
    grand_total,
    norm_unit,
    read_boq,
    reconcile_summary,
    summary_roles,
    template_findings,
)
from tests.boq_factory import Bid, build_real_boq
from tests.unit.test_boq_reader_variants import Cells, Fx, _rules, _save

_HEAD = ("No", "Description", "Unit", "Quantity", "Rate", "Amount")

# --- numbers typed as text --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1,250.50", 1250.5),
        ("1.250,50", 1250.5),
        ("1 250", 1250.0),
        ("1 250,50", 1250.5),
        ("1 250 000,5", 1250000.5),
        ("1.250.000", 1250000.0),
        ("1.250.000,75", 1250000.75),
        ("12.3450", 12.345),
        ("0.125", 0.125),
        ("0,5", 0.5),
        ("-12.5", -12.5),
        ("12,50", 12.5),
        ("1234.567", 1234.567),  # four digits before: no thousands group, three decimals
        (" 42 ", 42.0),
    ],
)
def test_number_text_that_reads_one_way_is_that_number(text: str, value: float) -> None:
    assert _num(text) == pytest.approx(value)


@pytest.mark.parametrize(
    "text",
    [
        "1450.00.",  # read as 145,000 by stripping every separator: a 100x rate
        "1,450.0.",
        "14.50.0",
        "12.50.",
        "1.25.",
        "1.2.3",
        "7.5,",
        "12,50,",
        "1.23,4",
        "1,250.000.5",  # two grouping characters mixed
        "12,34,567",  # groups that are not threes
        "1 250.000",  # one more group, or three decimals?
        ".5",
        "1250.",
        "10 m3",
    ],
)
def test_number_text_with_a_stray_or_doubled_separator_stays_text(text: str) -> None:
    assert _num(text) is None


# --- units: every name of one measure -----------------------------------------------------------

#: per measure: its abbreviations, its names singular and plural, English and Georgian
_MEASURES = {
    "lm": (
        "m",
        "lm",
        "metre",
        "metres",
        "meter",
        "meters",
        "running metre",
        "linear metres",
        "მ",
        "გრძ.მ",
        "მ.გრძ.",
        "მეტრი",
    ),
    "m2": ("m2", "sq.m", "square metre", "square meters", "მ2", "კვ.მ"),
    "m3": ("m3", "cum", "cubic metre", "cubic meters", "მ3", "კუბ.მ"),
    "km": ("km", "kms", "kilometre", "kilometers", "კმ"),
    "kg": ("kg", "kgs", "kilogram", "kilograms", "კგ", "კილოგრამი"),
    "t": ("t", "tn", "ton", "tonnes", "ტ", "ტონ.", "ტონა"),
    "l": ("l", "lt", "ltr", "ltrs", "litre", "liters", "ლ", "ლიტრი"),
    "pcs": ("pcs", "pc", "nr", "nos", "no.", "number", "pieces", "ც", "ცალ.", "ცალი"),
}


@pytest.mark.parametrize(
    ("measure", "spelling"),
    [(m, s) for m, spellings in _MEASURES.items() for s in spellings],
)
def test_every_name_of_a_measure_folds_to_that_measure(measure: str, spelling: str) -> None:
    assert norm_unit(spelling) == measure


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("ls", "l"),  # a lump sum is not a litre, even with an "s" dropped
        ("lts", "ls"),
        ("item", "pcs"),
        ("each", "pcs"),  # a count by another name stays a question, like "item"
        ("nos", "no-go"),
        ("metres", "m2"),
        ("square metre", "cubic metre"),
        ("kms", "km2"),
        ("kgs", "t"),
        ("ტონ", "ტ2"),
    ],
)
def test_names_of_different_measures_stay_apart(a: str, b: str) -> None:
    assert norm_unit(a) != norm_unit(b)


# --- the Summary's VAT row worded "Total VAT" -----------------------------------------------------


@pytest.mark.parametrize(
    ("role", "label"),
    [
        ("vat", "Total VAT"),
        ("vat", "Total VAT 18%"),
        ("vat", "VAT amount"),
        ("vat", "Amount of VAT"),
        ("vat", "სულ დღგ"),
        ("incl", "VAT-inclusive total"),
        ("incl", "Total VAT inclusive"),
        ("excl", "VAT-exclusive total"),
        ("excl", "Total VAT exclusive"),
    ],
)
def test_a_vat_row_or_total_worded_around_the_tax_name_plays_its_part(
    role: str, label: str
) -> None:
    assert _plays(role, label)


@pytest.mark.parametrize(
    ("role", "label"),
    [
        ("vat", "VAT-inclusive total"),
        ("vat", "Total VAT inclusive"),
        ("vat", "VAT-exclusive total"),
        ("vat", "Total excl. VAT"),
        ("vat", "Total incl. VAT"),
        ("vat", "Total cost"),
        ("vat", "Total private works"),
        ("cost", "Total VAT"),
        ("incl", "Total VAT"),
        ("excl", "Total VAT"),
        ("incl", "VAT-exclusive total"),
    ],
)
def test_a_row_worded_near_the_vat_row_does_not_take_its_part(role: str, label: str) -> None:
    assert not _plays(role, label)


@pytest.mark.parametrize(
    ("vat_label", "incl_label"),
    [("Total VAT", "TOTAL PRICE INCLUDING VAT"), ("VAT", "VAT-inclusive total")],
)
def test_a_chain_with_its_vat_row_or_total_worded_so_reconciles_and_ranks(
    vat_label: str, incl_label: str
) -> None:
    book = BoqWorkbook(
        name="B",
        summary_sheet="SUMMARY",
        summary_rows=[
            SummaryRow(9, "1", "WORKS", 2000.0),
            SummaryRow(10, "", "TOTAL COST", 2000.0),
            SummaryRow(11, "", "TOTAL PRICE EXCL. VAT", 2000.0),
            SummaryRow(12, "", vat_label, 360.0, rate=0.18),
            SummaryRow(13, "", incl_label, 2360.0),
        ],
    )
    roles = summary_roles(book)
    assert (roles["vat"].row, roles["incl"].row) == (12, 13)
    assert grand_total(book) == 2360.0
    assert reconcile_summary(book) == []


# --- bidders tied to the cent ---------------------------------------------------------------------

#: the same half-cent total as two different float sums leave it
_TIED = (32574.785, 32574.785000000003)


def test_money_rounds_to_the_cent_half_up_whatever_float_noise_it_carries() -> None:
    assert [cents(x) for x in _TIED] == [32574.79, 32574.79]
    assert cents(0.125) == 0.13 and cents(-2.675) == -2.68
    assert cents(1999.994999) == 1999.99  # not a half cent: rounds down


def test_bidders_tied_to_the_cent_share_their_colour() -> None:
    comps = [BidComparison(bidder=n) for n in ("A", "B", "C")]
    for comp, total in zip(comps, (*_TIED, 34201.64), strict=True):
        comp.common_scope_total, comp.common_scope_positions = total, 5
    _colour(comps)
    assert [c.colour for c in comps] == ["green", "green", "red"]


def test_tied_cells_of_the_merged_workbook_share_their_colour() -> None:
    ws = Workbook().active
    assert ws is not None
    for r, v in enumerate((*_TIED, 34201.64), start=1):
        ws.cell(r, 1, v)
    _colour_extremes(ws, [1, 2, 3], 1)
    fills = [ws.cell(r, 1).fill.fgColor.rgb for r in (1, 2, 3)]
    assert fills[0] == fills[1] != fills[2]


# --- a unit written where the template left none --------------------------------------------------


def _unit_sheet(units: tuple[str | None, str | None, str | None]) -> Cells:
    """Three positions at 10 x 5.00, with the given units."""
    cells: Cells = {(1, c): h for c, h in enumerate(_HEAD, 1)}
    for i, (item, unit) in enumerate(zip(("2.1", "2.2", "2.3"), units, strict=True)):
        r = i + 2
        cells.update({(r, 1): item, (r, 2): f"Partition wall type {i + 1}", (r, 4): 10.0})
        if unit is not None:
            cells[(r, 3)] = unit
        cells[(r, 5)] = 5.0
        cells[(r, 6)] = Fx(f"=D{r}*E{r}", 50.0)
    return cells


def test_a_unit_written_where_the_template_left_none_is_listed_apart(tmp_path: Path) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _unit_sheet(("m2", None, None))}))
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": _unit_sheet(("m2", "m3", None))}))
    comp = compare_bid(t, b)
    assert [(ln.sheet, ln.item_no, ln.unit) for ln in comp.unit_filled] == [
        ("1. WORKS", "2.2", "m3")
    ]
    [note] = [f for f in comp.findings if f.rule_id == "BOQ.UNIT_FILLED"]
    assert note.severity == "info" and "2.2 (m3)" in note.message_en
    # never a unit change, and the position is still evaluated as the template's
    assert "BOQ.UNIT_CHANGED" not in _rules(comp)
    assert comp.evaluated_cost == pytest.approx(3 * 10 * 5.0)


def test_a_unit_the_template_states_is_never_listed_as_filled(tmp_path: Path) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _unit_sheet(("m2", "m2", None))}))
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": _unit_sheet(("m2", "m3", None))}))
    comp = compare_bid(t, b)
    assert comp.unit_filled == []
    assert _rules(comp).count("BOQ.UNIT_CHANGED") == 1


# --- the line amount headed "Cost", "Sum", "თანხა" ------------------------------------------------


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("Cost", "amount"),
        ("Total cost", "amount"),
        ("Total cost, USD", "amount"),
        ("Sum", "amount"),
        ("თანხა", "amount"),
        ("სულ ღირებულება", "amount"),
        ("ჯამური ღირებულება ლარში", "amount"),
        ("Unit cost", "rate_total"),  # the rate, never the line amount
    ],
)
def test_the_line_amount_is_read_by_its_usual_names(label: str, key: str) -> None:
    assert _label_key(label) == key


@pytest.mark.parametrize(
    "label", ["Cost code", "Costs excl. VAT", "Summary", "Sum insured", "თანხის წყარო"]
)
def test_a_header_only_resembling_the_amount_is_not_the_amount(label: str) -> None:
    assert _label_key(label) is None


def _amount_sheet(amount_label: str, typed: float) -> Cells:
    """1.1 at 210 x 38.50 whose amount is typed as ``typed``, and 1.2 at 10 x 5.00."""
    cells: Cells = {(1, c): h for c, h in enumerate((*_HEAD[:5], amount_label), 1)}
    cells.update({(2, 1): "1.1", (2, 2): "Floor tiling", (2, 3): "m2", (2, 4): 210.0})
    cells.update({(2, 5): 38.5, (2, 6): typed})
    cells.update({(3, 1): "1.2", (3, 2): "Skirting", (3, 3): "m", (3, 4): 10.0, (3, 5): 5.0})
    cells[(3, 6)] = Fx("=D3*E3", 50.0)
    return cells


@pytest.mark.parametrize("label", ["Cost", "თანხა", "Total cost in USD"])
def test_a_line_amount_headed_otherwise_is_still_checked(tmp_path: Path, label: str) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _amount_sheet(label, 8085.0)}))
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": _amount_sheet(label, 6085.0)}))
    assert t.sheets["1. WORKS"].columns["amount"] == 6
    [err] = [f for f in compare_bid(t, b).findings if f.rule_id == "BOQ.ARITHMETIC"]
    assert err.expected == "8,085.00" and err.actual == "6,085.00"
    assert "BOQ.AMOUNTS_NOT_READ" not in [f.rule_id for f in template_findings(t)]


def test_a_sheet_whose_line_amount_is_not_recognised_says_so(tmp_path: Path) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _amount_sheet("Extension", 8085.0)}))
    assert "amount" not in t.sheets["1. WORKS"].columns
    [note] = [f for f in template_findings(t) if f.rule_id == "BOQ.AMOUNTS_NOT_READ"]
    assert note.severity == "info" and "1. WORKS" in note.message_en


# --- cost components: machinery, and one this reader has no name for -----------------------------

#: (item, description, unit, quantity, unit rate)
_WORKS = [("3.1", "Trench excavation", "m3", 120.0, 14.0), ("3.2", "Backfilling", "m3", 80.0, 6.5)]


def _estimate(parts: tuple[str, ...], split: tuple[float, ...] | None, *, ka: bool) -> Cells:
    """A Georgian estimate: each component a group over its own rate and sum, a closing total.

    No | Description | Unit | Qty | <part 1> (Rate | Sum) | <part 2> (Rate | Sum) | ... | Total
    """
    head = ("№", "დასახელება", "განზ.", "რაოდენობა") if ka else (*_HEAD[:3], "Quantity")
    rate, total_sum, total = ("ერთ. ფასი", "სულ", "ჯამი") if ka else ("Rate", "Amount", "Total")
    cells: Cells = {(1, c): h for c, h in enumerate(head, 1)}
    last = 5 + 2 * len(parts)
    for i, part in enumerate(parts):
        cells.update({(1, 5 + 2 * i): part, (2, 5 + 2 * i): rate, (2, 6 + 2 * i): total_sum})
    cells[(1, last)] = total
    for r, (item, text, unit, qty, price) in enumerate(_WORKS, start=3):
        cells.update({(r, 1): item, (r, 2): text, (r, 3): unit, (r, 4): qty})
        for i, share in enumerate(split or (0.0,) * len(parts)):
            rate_col = get_column_letter(5 + 2 * i)
            if split:
                cells[(r, 5 + 2 * i)] = price * share
            cells[(r, 6 + 2 * i)] = Fx(f"=D{r}*{rate_col}{r}", qty * price * share)
        refs = "+".join(f"{get_column_letter(6 + 2 * i)}{r}" for i in range(len(parts)))
        cells[(r, last)] = Fx(f"={refs}", qty * price * sum(split or ()))
    return cells


_COST = sum(q * p for _, _, _, q, p in _WORKS)


@pytest.mark.parametrize(
    ("parts", "ka"),
    [
        (("მასალა", "ხელფასი", "მანქანა-მექანიზმები"), True),
        (("Materials", "Labour", "Plant"), False),
        (("Materials", "Labour", "Equipment"), False),
    ],
)
def test_a_machinery_component_is_part_of_the_rate(
    tmp_path: Path, parts: tuple[str, ...], ka: bool
) -> None:
    """The machinery "Rate" under its own group is the machinery rate: bound as the unit rate,
    it priced every line at the machines' share alone and flipped the colours."""
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _estimate(parts, None, ka=ka)}))
    a = read_boq(_save(tmp_path / "a.xlsx", {"1. WORKS": _estimate(parts, (0.5, 0.2, 0.3), ka=ka)}))
    c = read_boq(
        _save(tmp_path / "c.xlsx", {"1. WORKS": _estimate(parts, (0.45, 0.4, 0.2), ka=ka)})
    )
    cols = t.sheets["1. WORKS"].columns
    assert (cols["rate_machinery"], cols["sum_machinery"], cols["amount"]) == (9, 10, 11)
    assert "rate_total" not in cols
    comps = compare_all(t, [a, c])
    assert [x.evaluated_cost for x in comps] == [pytest.approx(_COST), pytest.approx(_COST * 1.05)]
    assert all(x.findings == [] for x in comps)


def test_a_machinery_column_is_added_into_the_stated_rate() -> None:
    line = BoqLine(
        sheet="S",
        row=2,
        item_no="1",
        description="Works",
        unit="m3",
        quantity=10.0,
        rate_materials=7.0,
        rate_labor=2.8,
        rate_machinery=4.2,
        rate_total=14.0,
        amount=140.0,
    )
    assert _arithmetic_findings(line, ()) == []
    assert _unit_rate(line, {}) == 14.0
    wrong = BoqLine(
        sheet="S",
        row=2,
        item_no="1",
        description="Works",
        unit="m3",
        quantity=10.0,
        rate_materials=7.0,
        rate_labor=2.8,
        rate_machinery=4.2,
        rate_total=15.0,
        amount=150.0,
    )
    assert [f.expected for f in _arithmetic_findings(wrong, ())] == ["14.00"]


def test_a_component_this_reader_has_no_name_for_never_binds_the_unit_rate(
    tmp_path: Path,
) -> None:
    """ "Energy" over Rate | Amount beside materials and labour: its sub-columns are neither the
    unit rate nor the line amount, the line is priced at its total over its quantity, and the
    reader says which columns it did not read."""
    parts = ("Materials", "Labour", "Energy")
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _estimate(parts, None, ka=False)}))
    a = read_boq(
        _save(tmp_path / "a.xlsx", {"1. WORKS": _estimate(parts, (0.5, 0.2, 0.3), ka=False)})
    )
    sheet = t.sheets["1. WORKS"]
    assert "rate_total" not in sheet.columns and sheet.columns["amount"] == 11
    # the template's rates are blank: its Energy amounts are what shows the column is priced
    assert [c for c, _ in sheet.unread_price_columns] == [10]
    assert [c for c, _ in a.sheets["1. WORKS"].unread_price_columns] == [9, 10]
    [note] = [f for f in template_findings(t) if f.rule_id == "BOQ.COLUMNS_NOT_READ"]
    assert "Energy" in note.message_en
    comp = compare_bid(t, a)
    assert comp.evaluated_cost == pytest.approx(_COST)
    assert "BOQ.ARITHMETIC" not in _rules(comp)


def test_a_lone_unknown_label_over_rate_and_amount_is_still_the_rate_and_amount(
    tmp_path: Path,
) -> None:
    cells: Cells = {(1, c): h for c, h in enumerate(_HEAD[:4], 1)}
    cells.update({(1, 5): "Bidder's price", (2, 5): "Rate", (2, 6): "Amount"})
    cells.update({(3, 1): "1.1", (3, 2): "Formwork", (3, 3): "m2", (3, 4): 10.0, (3, 5): 3.0})
    cells[(3, 6)] = Fx("=D3*E3", 30.0)
    sheet = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": cells})).sheets["1. WORKS"]
    assert (sheet.columns["rate_total"], sheet.columns["amount"]) == (5, 6)
    assert sheet.unread_price_columns == ()


def _one_row_components(extra: str, value: object, rate: float) -> Cells:
    """No | Description | Unit | Qty | Materials | Labour | <extra> | Rate | Amount."""
    head = (*_HEAD[:4], "Materials", "Labour", extra, "Rate", "Amount")
    cells: Cells = {(1, c): h for c, h in enumerate(head, 1)}
    cells.update({(2, 1): "1.1", (2, 2): "Brick wall", (2, 3): "m2", (2, 4): 10.0})
    cells.update({(2, 5): 7.0, (2, 6): 2.8, (2, 7): value, (2, 8): rate})
    cells[(2, 9)] = Fx("=D2*H2", 10.0 * rate)
    return cells


def test_a_price_column_this_reader_does_not_know_stops_the_rate_being_held_to_the_rest(
    tmp_path: Path,
) -> None:
    """A rate that adds a column the reader did not recognise is no arithmetic error of the
    bidder's: it was "total rate does not match (14.00 vs 9.80)" on every faithful line."""
    cells = _one_row_components("Site costs", 4.2, 14.0)
    book = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": cells}))
    sheet = book.sheets["1. WORKS"]
    assert sheet.unread_price_columns == ((7, "Site costs"),)
    [line] = sheet.lines
    assert line.partial_rate and _arithmetic_findings(line, ()) == []


def test_a_text_column_inside_the_price_block_keeps_the_rate_check(tmp_path: Path) -> None:
    """Remarks written between the rates hold no price: a total rate that does not add up is
    still the bidder's arithmetic."""
    cells = _one_row_components("Remarks", "as drawing", 14.0)
    [line] = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": cells})).sheets["1. WORKS"].lines
    assert not line.partial_rate
    assert [f.expected for f in _arithmetic_findings(line, ())] == ["9.80"]


# --- a bid-form block above the bill ----------------------------------------------------------

#: what a bidder fills in above the bill: all invented
_BID_FORM = (
    "BID FORM",
    "Bidder: Sample Works Ltd",
    "Registration code: 000111222",
    "Address: 7 Invented Lane",
    "Tender No: T-0000",
    "Date: 01.02.2026",
    "Bid validity: 60 days",
    "Currency: USD",
    "Signatory: B. Sample",
    "Stamp:",
    "Phone: +000 000 000",
    "E-mail: bids@sample.invalid",
    "Contact: C. Sample",
    "Bank: Sample Bank",
    "Account: XX00SAMPLE0000",
    "Notes: none",
    "Prepared by: D. Sample",
    "Checked by: E. Sample",
)


def _below_form(rows: int, cells: Cells) -> Cells:
    """``cells`` moved down under a bid form of ``rows`` rows."""
    out: Cells = {(r + rows, c): v for (r, c), v in cells.items()}
    for i in range(rows):
        out[(i + 1, 1)] = _BID_FORM[i % len(_BID_FORM)]
    for (r, c), v in list(out.items()):
        if isinstance(v, Fx):  # formulas move with their rows
            text = re.sub(
                r"([A-Z])(\d+)", lambda m: f"{m.group(1)}{int(m.group(2)) + rows}", v.text
            )
            out[(r, c)] = Fx(text, v.value)
    return out


def _grouped_rate_sheet() -> Cells:
    """A two-row header - "Unit rate" over Materials | Total - and one position."""
    cells: Cells = {(1, c): h for c, h in enumerate(_HEAD[:4], 1)}
    cells.update({(1, 5): "Unit rate", (1, 7): "Amount", (2, 5): "Materials", (2, 6): "Total"})
    cells.update({(3, 1): "1.1", (3, 2): "Wall render", (3, 3): "m2", (3, 4): 10.0})
    cells.update({(3, 5): 5.0, (3, 6): 5.0, (3, 7): Fx("=D3*F3", 50.0)})
    return cells


@pytest.mark.parametrize("rows", [0, 13, 19])
def test_a_header_pushed_down_by_a_bid_form_keeps_all_its_rows(tmp_path: Path, rows: int) -> None:
    """At row 14, the header's second row fell outside a 14-row window: "Unit rate" bound the
    materials column as the unit rate. At row 20 the sheet was unreadable."""
    cells = _below_form(rows, _grouped_rate_sheet())
    sheet = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": cells})).sheets["1. WORKS"]
    assert (sheet.columns["rate_materials"], sheet.columns["rate_total"]) == (5, 6)
    assert sheet.header_row == rows + 2
    assert [(ln.item_no, ln.rate_total) for ln in sheet.lines] == [("1.1", 5.0)]


def test_the_bid_form_rows_themselves_are_neither_header_nor_lines(tmp_path: Path) -> None:
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": _grouped_rate_sheet()}))
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": _below_form(18, _grouped_rate_sheet())}))
    comp = compare_bid(t, b)
    assert comp.evaluated_cost == pytest.approx(50.0)
    assert comp.findings == []


def _summary_sheet(top: int) -> Cells:
    """The client-style Summary with its header on row ``top``: "AMOUNT EXCL. VAT" spanning
    the materials, labour, transport and TOTAL sub-columns under it."""
    cells: Cells = {(top, 1): "ITEM", (top, 2): "DESCRIPTION", (top, 3): "AMOUNT EXCL. VAT"}
    for c, label in enumerate(("MATERIALS", "LABOUR", "TRANSPORT", "TOTAL"), 3):
        cells[(top + 1, c)] = label
    cells.update({(top + 2, 1): "1", (top + 2, 2): "WORKS", (top + 2, 6): 1000.0})
    cells.update({(top + 3, 2): "TOTAL COST", (top + 3, 6): 1000.0})
    return cells


@pytest.mark.parametrize("top", [1, 13, 14, 20])
def test_the_summary_total_column_is_found_wherever_its_header_sits(top: int) -> None:
    ws = Workbook().active
    assert ws is not None
    for (r, c), v in _below_form(top - 1, _summary_sheet(1)).items():
        ws.cell(r, c, v)
    ws.merge_cells(start_row=top, start_column=3, end_row=top, end_column=5)
    assert _summary_columns(ws) == (1, 2, 6, top + 1)


def test_a_summary_row_labelled_total_is_never_the_total_column() -> None:
    """The column is looked for in the header's own rows, and never in the description column:
    a "Total" row further down named the descriptions as the total."""
    ws = Workbook().active
    assert ws is not None
    for c, label in enumerate(("Item", "Description", "Amount, USD"), 1):
        ws.cell(1, c, label)
    ws.cell(2, 1, "1")
    ws.cell(2, 2, "WORKS")
    ws.cell(2, 3, 1000.0)
    ws.cell(4, 2, "Total")
    ws.cell(4, 3, 1000.0)
    assert _summary_columns(ws) == (1, 2, 3, 1)


@pytest.mark.parametrize(
    ("role", "value", "text", "said"),
    [
        ("incl", None, None, "states no number"),
        ("cost", None, "#DIV/0!", "states the text '#DIV/0!'"),
        ("cost", 0.0, None, "states 0"),
    ],
)
def test_a_summary_row_with_no_value_in_the_total_column_is_named_not_called_missing(
    tmp_path: Path, role: str, value: float | None, text: str | None, said: str
) -> None:
    t_path, b_path = tmp_path / "t.xlsx", tmp_path / "b.xlsx"
    t_path.write_bytes(build_real_boq())
    b_path.write_bytes(build_real_boq(Bid()))
    t, b = read_boq(t_path, name="T"), read_boq(b_path, name="B")
    row = summary_roles(b)[role]
    b.summary_rows = [
        replace(r, value=value, non_numeric=text) if r.row == row.row else r for r in b.summary_rows
    ]
    [note] = [f for f in compare_bid(t, b).findings if f.rule_id == "BOQ.NOT_RANKED"]
    assert f"row {row.row}" in note.message_en and said in note.message_en
    assert "no Summary row states" not in note.message_en


def test_positions_on_a_sheet_whose_prices_are_not_read_are_never_called_unpriced(
    tmp_path: Path,
) -> None:
    """The reader's gap is said once (BOQ.PRICES_NOT_READ); "5 position(s) left unpriced" beside
    it blamed a bidder who priced every line."""
    head = (*_HEAD[:4], "Tarif", "Summe")
    t_cells: Cells = {(1, c): h for c, h in enumerate(_HEAD, 1)}
    b_cells: Cells = {(1, c): h for c, h in enumerate(head, 1)}
    for cells in (t_cells, b_cells):
        cells.update({(2, 1): "1.1", (2, 2): "Wall render", (2, 3): "m2", (2, 4): 10.0})
        cells.update({(2, 5): 5.0, (2, 6): Fx("=D2*E2", 50.0)})
    t = read_boq(_save(tmp_path / "t.xlsx", {"1. WORKS": t_cells}))
    b = read_boq(_save(tmp_path / "b.xlsx", {"1. WORKS": b_cells}))
    comp = compare_bid(t, b)
    rules = _rules(comp)
    assert "BOQ.PRICES_NOT_READ" in rules and "BOQ.POSITIONS_UNPRICED" not in rules
    assert comp.unpriced_positions == [] and not comp.complete
    assert [p.status for p in comp.positions] == ["prices_not_read"]
