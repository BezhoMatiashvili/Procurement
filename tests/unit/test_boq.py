"""BOQ comparison. Workbooks are built in-memory; no client file is ever read here.

Most tests build the client's real layout (``tests.boq_factory.build_real_boq``): a three-row
header with a "1 2 3" row, group rows summing ranges, positions whose amounts are formulas of
their own row, "bidder fills in" positions with no quantity, a SUB-TOTAL row and a Summary
linked to the sheets. Every expected total is derived from the factory's own data
(``REAL_SHEETS``), never copied from what the code under test printed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from procurement_agent.checks.boq import (
    MERGED_COLUMNS,
    BoqWorkbook,
    SummaryRow,
    compare_all,
    compare_bid,
    grand_total,
    norm_unit,
    rate_outliers,
    read_boq,
    template_findings,
    write_merged_workbook,
)
from tests.boq_factory import CIVIL, DOORS, REAL_SHEETS, Bid, build_real_boq, real_positions

SECTION = "1. WORKS"
# (item, description, unit, qty, materials rate, labor rate)
LINES = [
    ("1.1", "Concrete works", "m3", 10, 100.0, 50.0),
    ("1.2", "Steel works", "t", 4, 200.0, 80.0),
    ("1.3", "Painting", "m2", 25, 8.0, 4.0),
]
#: overheads 10% + profit 5% on the cost, then 18% VAT - the factory's defaults
MARKUP = 1.15 * 1.18


def _build(path: Path, lines: list[tuple], *, priced: bool = True, summary_shift: int = 0) -> Path:
    """A flat sheet of typed-in numbers: the shape of a hand-made or re-exported BOQ."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = SECTION
    headers = [
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
    for col, title in enumerate(headers, 1):
        ws.cell(1, col, title)
    for col in range(1, len(headers) + 1):
        ws.cell(2, col, col)  # the bare column-number row real templates carry
    for r, (item, desc, unit, qty, mat, lab) in enumerate(lines, start=3):
        ws.cell(r, 2, item)
        ws.cell(r, 3, desc)
        ws.cell(r, 5, qty)
        ws.cell(r, 6, unit)
        if priced:
            ws.cell(r, 7, mat)
            ws.cell(r, 8, mat * qty)
            ws.cell(r, 9, lab)
            ws.cell(r, 10, lab * qty)
            ws.cell(r, 11, mat + lab)
            ws.cell(r, 12, (mat + lab) * qty)

    sm = wb.create_sheet("SUMMARY")
    off = summary_shift
    sm.cell(1, 1 + off, "ITEM")
    sm.cell(1, 2 + off, "DESCRIPTION")
    sm.cell(1, 3 + off, "TOTAL")
    sm.cell(2, 1 + off, "1")
    sm.cell(2, 2 + off, "WORKS")
    sm.cell(2, 3 + off, sum((m + lab) * q for _, _, _, q, m, lab in lines) if priced else 0)
    sm.cell(3, 1 + off, "2")
    sm.cell(3, 2 + off, "TOTAL PRICE INCLUDING VAT")
    sm.cell(3, 3 + off, 1234.0 if priced else 0)
    wb.save(path)
    return path


def _real(tmp_path: Path, name: str, bid: Bid | None = None, **kw: bool) -> BoqWorkbook:
    path = tmp_path / f"{name}.xlsx"
    path.write_bytes(build_real_boq(bid, **kw))
    return read_boq(path, name=name)


def _cost(factor: float = 1.0, *, skip: set[tuple[str, str]] = frozenset(), sheets=None) -> float:
    """Template quantity x rate over the factory's positions - the expected evaluated cost."""
    return sum(
        (qty or 0) * rate * factor
        for sheet, item, qty, rate in real_positions()
        if (sheet, item) not in skip and (sheets is None or sheet in sheets)
    )


def _rules(comp: object) -> list[str]:
    return [f.rule_id for f in comp.findings]  # type: ignore[attr-defined]


@pytest.fixture
def template(tmp_path: Path) -> Path:
    """The blank template the client sends out: descriptions and units, no quantities."""
    blank = [(item, desc, unit, 0, 0.0, 0.0) for item, desc, unit, _, _, _ in LINES]
    return _build(tmp_path / "template.xlsx", blank, priced=False)


@pytest.fixture
def good_bid(tmp_path: Path) -> Path:
    return _build(tmp_path / "bid.xlsx", LINES)


# --- reading --------------------------------------------------------------------------------


def test_the_column_number_row_is_not_read_as_a_line(template: Path) -> None:
    """'1 2 3 4 ...' under the header parses as a perfectly good line whose arithmetic never
    adds up, which makes every sheet look broken."""
    book = read_boq(template)
    assert [line.item_no for line in book.sheets[SECTION].lines] == ["1.1", "1.2", "1.3"]


def test_positions_subtotals_and_headings_follow_the_template_amount_cells(
    tmp_path: Path,
) -> None:
    """GOLD_SPEC 5: a product of its own row is a position, a SUM of other rows a subtotal, a
    row with no amount a heading."""
    sheet = _real(tmp_path, "T").sheets[CIVIL]
    kinds = {(line.item_no or line.description): line.kind for line in sheet.lines}
    assert kinds["1.1"] == kinds["1.5"] == kinds["2.1"] == "position"
    assert kinds["A1"] == kinds["A2"] == "subtotal"
    assert kinds["Wall works"] == kinds["A"] == "heading"
    assert kinds[f"SUB-TOTAL {CIVIL}"] == "subtotal"
    expected = [row[0] for row in REAL_SHEETS[CIVIL] if len(row) == 5]
    assert [line.item_no for line in sheet.leaf_lines] == expected


def test_a_row_with_a_quantity_but_no_amount_formula_is_still_a_position(tmp_path: Path) -> None:
    """The client's template has a position with a quantity and no amount formula; everyone
    priced it, and reading it as a heading dropped it from every evaluation."""
    path = tmp_path / "t.xlsx"
    path.write_bytes(build_real_boq(cached=False))
    wb = load_workbook(path)
    ws = wb[CIVIL]
    row = next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 2).value == "1.3")
    ws.cell(row, 16).value = None
    wb.save(path)
    line = next(ln for ln in read_boq(path).sheets[CIVIL].lines if ln.item_no == "1.3")
    assert line.kind == "position"


def test_the_summary_is_read_as_rows_not_keyed_by_label(tmp_path: Path) -> None:
    """The same group label appears under every section; keyed by label, the Doors row overwrote
    the Civil one and the merged Summary showed the wrong section's value."""
    book = _real(tmp_path, "B", Bid())
    a1 = [r for r in book.summary_rows if r.item == "A1"]
    assert [r.refs[0][0] for r in a1] == [CIVIL, DOORS]
    assert a1[0].value == pytest.approx(_cost(sheets={CIVIL}))
    assert a1[1].value == pytest.approx(_cost(sheets={DOORS}))
    labelled = [r for r in book.summary_rows if r.value is not None]
    assert len(book.summary) == len(labelled)  # nothing collapsed


def test_grand_total_reads_the_incl_vat_row_and_never_falls_back(tmp_path: Path) -> None:
    book = _real(tmp_path, "B", Bid())
    assert grand_total(book) == pytest.approx(_cost() * MARKUP)
    assert grand_total(book, incl_vat=False) == pytest.approx(_cost() * 1.15)
    excl_only = BoqWorkbook(
        name="X",
        summary_rows=[SummaryRow(row=5, item="12", label="TOTAL PRICE EXCL. VAT", value=100.0)],
    )
    assert grand_total(excl_only) is None
    assert grand_total(excl_only, incl_vat=False) == 100.0


def test_summary_is_read_even_when_the_sheet_is_shifted(tmp_path: Path) -> None:
    """One of the client's bidders returned a Summary shifted one column right; a reader that
    assumed columns A and B saw all zeroes - a wrong answer that looks like a right one."""
    normal = read_boq(_build(tmp_path / "s1.xlsx", LINES))
    shifted = read_boq(_build(tmp_path / "s2.xlsx", LINES, summary_shift=1))
    assert grand_total(normal) == grand_total(shifted) == 1234.0


# --- the evaluation (GOLD_SPEC 5) -------------------------------------------------------------


def test_the_ranking_is_on_the_common_scope_not_the_stated_total(tmp_path: Path) -> None:
    """A bid that leaves a whole section out states the lowest total, but only the positions
    every bidder priced are compared, and on those it is the most expensive."""
    t = _real(tmp_path, "T")
    full = _real(tmp_path, "Full", Bid())
    cheaper = _real(tmp_path, "Cheaper", Bid(factor=0.9))
    partial = _real(tmp_path, "Partial", Bid(factor=1.02, unpriced_sheets={DOORS}))
    comps = {c.bidder: c for c in compare_all(t, [full, cheaper, partial])}

    # the stated totals would crown the partial bid
    assert grand_total(partial) < grand_total(cheaper) < grand_total(full)  # type: ignore[operator]
    civil = _cost(sheets={CIVIL})
    for name, factor, cost in (
        ("Full", 1.0, _cost()),
        ("Cheaper", 0.9, _cost(0.9)),
        ("Partial", 1.02, civil * 1.02),
    ):
        comp = comps[name]
        assert comp.markup_factor == pytest.approx(MARKUP)
        assert comp.evaluated_cost == pytest.approx(cost, abs=0.005)
        assert comp.evaluated_total == pytest.approx(cost * MARKUP, abs=0.005)
        assert comp.common_scope_total == pytest.approx(civil * factor * MARKUP, abs=0.005)
    assert comps["Full"].common_scope_positions == 5  # the civil positions with a quantity
    assert (comps["Partial"].colour, comps["Cheaper"].colour, comps["Full"].colour) == (
        "red",
        "green",
        None,
    )
    assert not comps["Partial"].complete and comps["Full"].complete
    assert "BOQ.SECTION_UNPRICED" in _rules(comps["Partial"])


def test_tied_bidders_share_a_colour(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    bids = [
        _real(tmp_path, "A", Bid()),
        _real(tmp_path, "B", Bid()),
        _real(tmp_path, "C", Bid(factor=0.5)),
    ]
    assert [c.colour for c in compare_all(t, bids)] == ["red", "red", "green"]


def test_nobody_is_coloured_alone_or_when_everyone_ties(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    assert [c.colour for c in compare_all(t, [_real(tmp_path, "A", Bid())])] == [None]
    same = [_real(tmp_path, "A", Bid()), _real(tmp_path, "B", Bid())]
    assert [c.colour for c in compare_all(t, same)] == [None, None]


def test_a_changed_quantity_is_evaluated_at_the_template_quantity(tmp_path: Path) -> None:
    """Client rule 2: where quantities differ, analyse only by the template's."""
    t = _real(tmp_path, "T")
    bid = _real(tmp_path, "B", Bid(qty={(CIVIL, "1.3"): 132, (CIVIL, "1.5"): 240}))
    comp = compare_bid(t, bid)
    assert [(d.item_no, d.template_qty, d.bid_qty) for d in comp.quantity_changed] == [
        ("1.3", 120.0, 132.0),
        ("1.5", 250.0, 240.0),
    ]
    assert comp.evaluated_cost == pytest.approx(_cost(), abs=0.005)
    own_minus_template = (132 - 120) * 21.5 + (240 - 250) * 41.0
    assert comp.difference["quantities"] == pytest.approx(own_minus_template * MARKUP, abs=0.01)
    assert grand_total(bid) - comp.evaluated_total == pytest.approx(  # type: ignore[operator]
        sum(comp.difference.values()), abs=0.02
    )
    message = next(f for f in comp.findings if f.rule_id == "BOQ.QUANTITY_CHANGED").message_en
    assert "(1.3)" in message and "120 -> 132" in message


def test_quantities_print_as_plain_numbers(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    comp = compare_bid(t, _real(tmp_path, "B", Bid(qty={(CIVIL, "1.5"): 1234567.5})))
    finding = next(f for f in comp.findings if f.rule_id == "BOQ.QUANTITY_CHANGED")
    assert "1,234,567.5" in finding.message_en and "e+" not in finding.message_en
    assert finding.actual == "1,234,567.5"


def test_an_added_line_is_listed_and_never_counted(tmp_path: Path) -> None:
    """Client rule 3: an added position is listed for the bidder and kept out of the analysis;
    the stated total still carries it, and the difference says so."""
    t = _real(tmp_path, "T")
    extra = ("1.4.1", "Stainless steel skirting", "lm", 45, 20.0)
    comp = compare_bid(t, _real(tmp_path, "B", Bid(add={(CIVIL, "1.4"): extra})))
    assert [line.item_no for line in comp.added] == ["1.4.1"]
    assert not comp.removed
    assert comp.evaluated_cost == pytest.approx(_cost(), abs=0.005)
    assert comp.difference["added_lines"] == pytest.approx(45 * 20.0 * MARKUP, abs=0.01)
    assert "BOQ.LINE_ADDED" in _rules(comp)
    assert "BOQ.RECONCILE" not in _rules(comp)


def test_a_removed_position_counts_nothing_and_leaves_the_common_scope(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    full = _real(tmp_path, "Full", Bid())
    dropped = _real(tmp_path, "Dropped", Bid(drop={(CIVIL, "1.4")}))
    comps = compare_all(t, [full, dropped])
    assert [line.item_no for line in comps[1].removed] == ["1.4"]
    assert comps[1].evaluated_cost == pytest.approx(_cost(skip={(CIVIL, "1.4")}), abs=0.005)
    assert comps[0].common_scope_cost == pytest.approx(_cost(skip={(CIVIL, "1.4")}), abs=0.005)
    assert not comps[1].complete
    assert "BOQ.LINE_REMOVED" in _rules(comps[1])


def test_a_filled_in_blank_quantity_is_evaluated_at_the_bidders_amount(tmp_path: Path) -> None:
    """Where the template leaves the quantity blank the bidder's own amount counts, and the line
    is listed - an info, not an error."""
    t = _real(tmp_path, "T")
    bid = Bid(qty={(CIVIL, "1.6"): 3}, rates={(CIVIL, "1.6"): 100.0})
    comp = compare_bid(t, _real(tmp_path, "B", bid))
    [filled] = comp.filled_blank
    assert (filled.item_no, filled.bid_qty, filled.amount) == ("1.6", 3.0, 300.0)
    assert comp.evaluated_cost == pytest.approx(_cost() + 300.0, abs=0.005)
    finding = next(f for f in comp.findings if f.rule_id == "BOQ.FILLED_BLANK")
    assert finding.severity == "info"
    assert "BOQ.QUANTITY_CHANGED" not in _rules(comp)


def test_a_priced_heading_is_flagged_and_not_evaluated(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    bid = Bid(heading_price={(CIVIL, "Floor works"): (1, 500.0)})
    comp = compare_bid(t, _real(tmp_path, "B", bid))
    assert [line.description for line in comp.priced_headings] == ["Floor works"]
    assert comp.evaluated_cost == pytest.approx(_cost(), abs=0.005)
    assert comp.difference["priced_headings"] == pytest.approx(500.0 * MARKUP, abs=0.01)
    assert "BOQ.HEADING_PRICED" in _rules(comp)
    assert "BOQ.RECONCILE" not in _rules(comp)  # the SUB-TOTAL includes it, and so do we


def test_a_partly_unpriced_section_lists_its_positions(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    comp = compare_bid(t, _real(tmp_path, "B", Bid(unpriced={(CIVIL, "1.3")})))
    [finding] = [f for f in comp.findings if f.rule_id == "BOQ.POSITIONS_UNPRICED"]
    assert CIVIL in finding.message_en and "1.3" in finding.message_en
    assert [line.item_no for line in comp.unpriced_positions] == ["1.3"]
    assert "BOQ.SECTION_UNPRICED" not in _rules(comp)
    # the placeholders for "other items" are optional; the fill-in quantity left blank is listed
    assert [line.item_no for line in comp.unfilled_positions] == ["1.6"]


def test_a_priced_placeholder_is_the_bidders_own_addition(tmp_path: Path) -> None:
    """ "Other works to be listed here by the tenderer": what a bidder lists there is work the
    template does not describe - listed, and kept out of the evaluation like an added line."""
    t = _real(tmp_path, "T")
    bid = Bid(qty={(CIVIL, "2.1"): 3}, rates={(CIVIL, "2.1"): 100.0})
    comp = compare_bid(t, _real(tmp_path, "B", bid))
    assert [line.item_no for line in comp.priced_optional] == ["2.1"]
    assert comp.evaluated_cost == pytest.approx(_cost(), abs=0.005)
    assert comp.difference["optional_items"] == pytest.approx(300.0 * MARKUP, abs=0.01)
    finding = next(f for f in comp.findings if f.rule_id == "BOQ.OPTIONAL_PRICED")
    assert finding.severity == "info"
    assert "BOQ.POSITIONS_UNPRICED" not in _rules(comp)


def test_a_section_row_and_its_items_are_priced_as_one() -> None:
    from procurement_agent.checks.boq import BoqLine, _position_groups

    def line(row: int, item: str) -> BoqLine:
        return BoqLine(sheet="S", row=row, item_no=item, description="x", unit="item", quantity=1.0)

    groups = _position_groups([line(10, "7.1"), line(11, "A8"), line(12, "8.1"), line(13, "9.1")])
    assert groups[11] == groups[12] == "A8"
    assert groups[10] != groups[13] and "A8" not in (groups[10], groups[13])


def test_a_quantity_that_adds_up_the_rows_below_is_a_parent_not_a_position() -> None:
    from openpyxl import Workbook

    from procurement_agent.checks.boq import _line_kind

    values, formulas = Workbook(), Workbook()
    formulas.active.cell(12, 7, "=SUM(G13:G19)")  # type: ignore[union-attr]
    cols = {"desc": 3, "qty": 7, "amount": 16}
    kind = _line_kind(values.active, formulas.active, cols, 12, "Lighting fixtures", True)
    assert kind == "heading"


# --- defects the reader used to hide ---------------------------------------------------------


def test_text_in_a_number_cell_is_reported_and_never_read_as_zero(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    others = _real(tmp_path, "Other", Bid())
    words = _real(
        tmp_path,
        "Words",
        Bid(rate_text={(CIVIL, "1.3"): "incl."}, qty_text={(CIVIL, "1.1"): "30 m2"}),
    )
    comps = compare_all(t, [others, words])
    comp = comps[1]
    texts = sorted(f.actual for f in comp.findings if f.rule_id == "BOQ.NON_NUMERIC")
    assert texts == ["30 m2", "incl."]
    # the quantity in words is not "changed to 0"; the rate in words is not a price of 0
    assert "BOQ.QUANTITY_CHANGED" not in _rules(comp)
    status = {p.item_no: p.status for p in comp.positions if p.sheet == CIVIL}
    # GOLD_SPEC 5: text where a quantity or a rate belongs leaves the position not priced
    assert status["1.3"] == "non_numeric" and status["1.1"] == "non_numeric"
    assert (CIVIL, "1.3") not in {
        (p.sheet, p.item_no) for p in comps[0].positions if p.in_common_scope
    }


def test_units_are_compared_after_folding_their_spellings(tmp_path: Path) -> None:
    assert norm_unit("m²") == norm_unit("sq.m") == norm_unit("M2") == "m2"
    assert norm_unit("m³") == norm_unit("m3") and norm_unit("l.m.") == norm_unit("rm") == "lm"
    assert norm_unit("item") != norm_unit("pcs")
    t = _real(tmp_path, "T")
    bid = Bid(unit={(CIVIL, "1.1"): "m²", (CIVIL, "1.4"): "l.m.", (DOORS, "1.1"): "pcs"})
    changed = compare_bid(t, _real(tmp_path, "B", bid)).findings
    units = [f.actual for f in changed if f.rule_id == "BOQ.UNIT_CHANGED"]
    assert units == ["pcs"]


def test_a_typo_in_a_description_is_the_same_position(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    bid = Bid(desc={(CIVIL, "1.3"): "Bitumen damp-prof membrane"})
    comp = compare_bid(t, _real(tmp_path, "B", bid))
    assert not comp.added and not comp.removed
    assert [(a.item_no, b.description) for a, b in comp.description_changed] == [
        ("1.3", "Bitumen damp-prof membrane")
    ]
    assert "BOQ.DESCRIPTION_CHANGED" in _rules(comp)
    assert comp.evaluated_cost == pytest.approx(_cost(), abs=0.005)


def test_swapped_positions_still_match(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    comp = compare_bid(t, _real(tmp_path, "B", Bid(swap=(CIVIL, "1.1", "1.3"))))
    assert not comp.added and not comp.removed and not comp.description_changed
    assert comp.evaluated_cost == pytest.approx(_cost(), abs=0.005)


def test_near_twin_descriptions_do_not_cross_over(tmp_path: Path) -> None:
    """Two partitions that differ in one word, swapped, one of them with a typo: each must
    pair with its own item, and only the typo'd one is a changed description."""
    t = _real(tmp_path, "T")
    bid = Bid(
        swap=(CIVIL, "1.1", "1.2"),
        desc={(CIVIL, "1.2"): "Plasterboard wall, double layer, 125mm"},
    )
    comp = compare_bid(t, _real(tmp_path, "B", bid))
    assert not comp.added and not comp.removed
    pairs = {a.item_no: b.item_no for a, b in comp.matched if a.item_no in ("1.1", "1.2")}
    assert pairs == {"1.1": "1.1", "1.2": "1.2"}
    assert [a.item_no for a, _ in comp.description_changed] == ["1.2"]


def test_a_bid_sheet_whose_headers_are_not_recognised_is_an_error(tmp_path: Path) -> None:
    """It used to be skipped without a word and the bidder stayed comparable."""
    t = _real(tmp_path, "T")
    others = _real(tmp_path, "Other", Bid())
    renamed = _real(tmp_path, "Renamed", Bid(header_rename={DOORS: "Qnty"}))
    comps = compare_all(t, [others, renamed])
    comp = comps[1]
    [finding] = [f for f in comp.findings if f.rule_id == "BOQ.SHEET_UNREADABLE"]
    assert finding.severity == "error" and DOORS in finding.message_en
    assert comp.unreadable_sheets == [DOORS] and not comp.complete
    assert {p.status for p in comp.positions if p.sheet == DOORS} == {"sheet_unreadable"}
    assert comp.evaluated_cost == pytest.approx(_cost(sheets={CIVIL}), abs=0.005)
    assert comps[0].common_scope_cost == pytest.approx(_cost(sheets={CIVIL}), abs=0.005)


def test_the_template_reports_its_unreadable_and_helper_sheets(tmp_path: Path) -> None:
    t = _real(tmp_path, "T", Bid(header_rename={DOORS: "Qnty"}), priced=False, helper_sheet=True)
    assert t.unreadable_sheets == [DOORS] and t.helper_sheets == ["Calc"]
    found = {(f.rule_id, f.severity) for f in template_findings(t)}
    assert found == {("BOQ.SHEET_UNREADABLE", "error"), ("BOQ.SHEET_SKIPPED", "info")}
    # a bid without the helper sheet has removed nothing that is priced
    comp = compare_bid(t, _real(tmp_path, "B", Bid()))
    removed = [f for f in comp.findings if f.rule_id == "BOQ.SHEET_REMOVED"]
    assert [f.severity for f in removed] == ["warning"]


def test_a_workbook_saved_without_values_is_reported_not_compared(tmp_path: Path) -> None:
    """openpyxl and scripts save formulas without their results; every number then reads as
    None, which used to come out as a flood of false 'unpriced' and 'changed' findings."""
    t = _real(tmp_path, "T")
    good = _real(tmp_path, "Good", Bid())
    cheap = _real(tmp_path, "Cheap", Bid(factor=0.5))
    raw = _real(tmp_path, "Raw", Bid(factor=0.1), cached=False)
    comps = compare_all(t, [good, cheap, raw])
    assert _rules(comps[2]) == ["BOQ.NO_CACHED_VALUES"]
    assert comps[2].findings[0].severity == "error"
    assert not comps[2].evaluable and comps[2].colour is None
    assert (comps[0].colour, comps[1].colour) == ("red", "green")

    raw_template = _real(tmp_path, "RawT", cached=False)
    assert [f.rule_id for f in template_findings(raw_template)] == ["BOQ.NO_CACHED_VALUES"]
    assert not compare_bid(raw_template, good).evaluable


def test_the_summary_is_reconciled_row_by_row(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    clean = compare_bid(t, _real(tmp_path, "Clean", Bid()))
    assert "BOQ.RECONCILE" not in _rules(clean)

    section = compare_bid(t, _real(tmp_path, "S", Bid(summary_typed={"CIVIL WORKS": 1.0})))
    [gap] = [f for f in section.findings if f.rule_id == "BOQ.RECONCILE"]
    assert "row 9" in gap.message_en and CIVIL in gap.message_en

    vat = compare_bid(t, _real(tmp_path, "V", Bid(summary_typed={"VAT": 1.0})))
    messages = [f.message_en for f in vat.findings if f.rule_id == "BOQ.RECONCILE"]
    assert any("(VAT)" in m for m in messages)

    excl = compare_bid(t, _real(tmp_path, "E", Bid(summary_typed={"TOTAL PRICE EXCL. VAT": 5.0})))
    assert any(
        "TOTAL PRICE EXCL. VAT" in f.message_en
        for f in excl.findings
        if f.rule_id == "BOQ.RECONCILE"
    )


# --- flat workbooks and the rest --------------------------------------------------------------


def test_a_faithful_bid_produces_no_errors(template: Path, good_bid: Path) -> None:
    comp = compare_bid(read_boq(template), read_boq(good_bid))
    assert [f for f in comp.findings if f.severity == "error"] == []
    assert not comp.added and not comp.removed


def test_a_changed_quantity_is_reported(tmp_path: Path) -> None:
    priced_template = _build(tmp_path / "t2.xlsx", LINES)
    changed = list(LINES)
    changed[1] = ("1.2", "Steel works", "t", 99, 200.0, 80.0)
    comp = compare_bid(read_boq(priced_template), read_boq(_build(tmp_path / "b2.xlsx", changed)))
    assert [d.bid_qty for d in comp.quantity_changed] == [99]
    assert "BOQ.QUANTITY_CHANGED" in _rules(comp)


def test_filling_a_blank_template_quantity_is_not_an_error(template: Path, good_bid: Path) -> None:
    """A blank quantity in the template means 'bidder fills this in'."""
    comp = compare_bid(read_boq(template), read_boq(good_bid))
    assert len(comp.filled_blank) == len(LINES)
    assert "BOQ.QUANTITY_CHANGED" not in _rules(comp)


def test_an_added_and_a_removed_position_are_distinguished(tmp_path: Path) -> None:
    t = _build(tmp_path / "t3.xlsx", LINES)
    bid_lines = [LINES[0], ("1.9", "Extra scaffolding", "m2", 5, 3.0, 1.0), LINES[2]]
    comp = compare_bid(read_boq(t), read_boq(_build(tmp_path / "b3.xlsx", bid_lines)))
    assert [line.description for line in comp.added] == ["Extra scaffolding"]
    assert [line.description for line in comp.removed] == ["Steel works"]


def test_a_changed_unit_is_not_reported_as_add_plus_remove(tmp_path: Path) -> None:
    t = _build(tmp_path / "t4.xlsx", LINES)
    changed = list(LINES)
    changed[0] = ("1.1", "Concrete works", "m2", 10, 100.0, 50.0)
    comp = compare_bid(read_boq(t), read_boq(_build(tmp_path / "b4.xlsx", changed)))
    assert not comp.added and not comp.removed
    assert "BOQ.UNIT_CHANGED" in _rules(comp)


def test_a_total_that_does_not_add_up_is_caught(tmp_path: Path) -> None:
    """The bidder controls this file, so its totals are a claim to be rechecked."""
    t = _build(tmp_path / "t5.xlsx", LINES)
    b = _build(tmp_path / "b5.xlsx", LINES)
    wb = load_workbook(b)
    wb[SECTION].cell(3, 12, 999999)  # overwrite the line amount
    wb.save(b)
    comp = compare_bid(read_boq(t), read_boq(b))
    arithmetic = [f for f in comp.findings if f.rule_id == "BOQ.ARITHMETIC"]
    assert arithmetic and "999,999.00" in (arithmetic[0].actual or "")


def test_an_unpriced_section_is_flagged(tmp_path: Path) -> None:
    unpriced = _build(tmp_path / "b6.xlsx", LINES, priced=False)
    comp = compare_bid(read_boq(_build(tmp_path / "t6.xlsx", LINES)), read_boq(unpriced))
    assert "BOQ.SECTION_UNPRICED" in _rules(comp)
    assert [line.item_no for line in comp.unpriced_positions] == ["1.1", "1.2", "1.3"]


def test_rate_outlier_needs_at_least_three_bidders(tmp_path: Path) -> None:
    t = read_boq(_build(tmp_path / "t7.xlsx", LINES))
    wild = list(LINES)
    wild[0] = ("1.1", "Concrete works", "m3", 10, 10000.0, 50.0)
    bids = [
        read_boq(_build(tmp_path / f"b7{i}.xlsx", lines), name=f"C{i}")
        for i, lines in enumerate([LINES, LINES, wild])
    ]
    findings = rate_outliers(t, compare_all(t, bids))
    assert {f.rule_id for f in findings} == {"BOQ.RATE_OUTLIER"}
    assert "C2" in findings[0].message_en


# --- the merged workbook ----------------------------------------------------------------------


def test_merged_workbook_stacks_every_bidder_under_each_line(tmp_path: Path) -> None:
    t = read_boq(_build(tmp_path / "t8.xlsx", LINES), name="TEMPLATE")
    bids = [read_boq(_build(tmp_path / f"b8{i}.xlsx", LINES), name=f"C{i}") for i in range(3)]
    out = write_merged_workbook(t, bids, compare_all(t, bids), tmp_path / "merged.xlsx")
    wb = load_workbook(out)
    assert "SUMMARY" in wb.sheetnames and SECTION in wb.sheetnames
    ws = wb[SECTION]
    assert ws.cell(2, 5).value == "— template —"
    assert [ws.cell(r, 5).value for r in (3, 4, 5)] == ["C0", "C1", "C2"]
    # one template row plus one row per bidder, for each line
    assert ws.max_row >= len(LINES) * (1 + len(bids))


def test_the_merged_summary_colours_the_common_scope_total(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    bids = [
        _real(tmp_path, "Expensive", Bid(factor=1.5)),
        _real(tmp_path, "Cheap", Bid(factor=0.8)),
        _real(tmp_path, "Partial", Bid(unpriced_sheets={DOORS})),
    ]
    comps = compare_all(t, bids)
    out = write_merged_workbook(t, bids, comps, tmp_path / "m.xlsx")
    ws = load_workbook(out)["SUMMARY"]
    header = next(
        r for r in range(1, ws.max_row + 1) if ws.cell(r, 6).value == "Common-scope total"
    )
    rows = {ws.cell(r, 2).value: r for r in range(header + 1, header + 4)}
    assert set(rows) == {"Expensive", "Cheap", "Partial"}
    fill = {name: ws.cell(r, 6).fill.fgColor.rgb for name, r in rows.items()}
    assert "F4CCCC" in str(fill["Expensive"]) and "D9EAD3" in str(fill["Cheap"])
    assert "F4CCCC" not in str(fill["Partial"]) and "D9EAD3" not in str(fill["Partial"])
    civil = _cost(sheets={CIVIL})
    assert ws.cell(rows["Cheap"], 6).value == pytest.approx(round(civil * 0.8 * MARKUP, 2))
    # the section rows repeat per section; each bidder's Doors "A1" row is the Doors value
    a1_rows = [r for r in range(1, header) if ws.cell(r, 1).value == "A1 Block 1"]
    assert len(a1_rows) == 2
    assert ws.cell(a1_rows[1], 3).value == pytest.approx(_cost(1.5, sheets={DOORS}))
    excluded = [
        ws.cell(r, 1).value
        for r in range(header, ws.max_row + 1)
        if ws.cell(r, 2).value == "Partial"
    ]
    assert "EXCLUDED: not priced" in excluded


def test_the_merged_sheets_keep_every_position_and_mark_the_exclusions(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    extra = ("1.4.1", "Stainless steel skirting", "lm", 45, 20.0)
    bids = [
        _real(tmp_path, "A", Bid()),
        _real(tmp_path, "B", Bid(drop={(CIVIL, "1.2")}, add={(CIVIL, "1.4"): extra})),
    ]
    out = write_merged_workbook(t, bids, compare_all(t, bids), tmp_path / "m.xlsx")
    ws = load_workbook(out)[CIVIL]
    rows: dict[tuple[object, object], object] = {}
    item = None
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, 5).value == "— template —":
            item = ws.cell(r, 1).value
        elif item is not None:
            rows[(item, ws.cell(r, 5).value)] = ws.cell(r, MERGED_COLUMNS["analysis"]).value
    assert rows[("1.2", "B")] == "EXCLUDED: removed by the bidder"
    assert rows[("1.2", "A")] == "evaluated; outside common scope"
    assert rows[("1.1", "A")] == "evaluated; common scope"
    added = [r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value == "1.4.1"]
    analysis = ws.cell(added[0], MERGED_COLUMNS["analysis"]).value
    assert len(added) == 1 and analysis == "EXCLUDED: added by the bidder"


def _amount_fills(path: Path) -> dict[str, str]:
    """Bidder -> the fill of their amount cell on the first line of the merged sheet."""
    ws = load_workbook(path)[SECTION]
    amount = MERGED_COLUMNS["amount"]
    return {str(ws.cell(r, 5).value): str(ws.cell(r, amount).fill.fgColor.rgb) for r in (3, 4, 5)}


def test_merged_line_colours_never_split_a_tie(tmp_path: Path) -> None:
    """With every bidder equal on a line, max() and min() used to pick two different rows, so
    identical amounts came out one red and one green."""
    t = read_boq(_build(tmp_path / "t.xlsx", LINES), name="TEMPLATE")
    same = [read_boq(_build(tmp_path / f"s{i}.xlsx", LINES), name=f"C{i}") for i in range(3)]
    out = write_merged_workbook(t, same, compare_all(t, same), tmp_path / "same.xlsx")
    assert set(_amount_fills(out).values()) == {"00000000"}

    cheap = [(i, d, u, q, m / 2, lab / 2) for i, d, u, q, m, lab in LINES]
    bids = [*same[:2], read_boq(_build(tmp_path / "c.xlsx", cheap), name="C2")]
    out = write_merged_workbook(t, bids, compare_all(t, bids), tmp_path / "tie.xlsx")
    fills = _amount_fills(out)
    assert "F4CCCC" in fills["C0"] and "F4CCCC" in fills["C1"]
    assert "D9EAD3" in fills["C2"]


def test_a_unit_left_blank_is_reported(tmp_path: Path) -> None:
    """A bidder who clears the unit cell has not kept the template's unit: said, as a warning,
    beside a different unit which stays an error."""
    t = _real(tmp_path, "T")
    bid = Bid(unit={(CIVIL, "1.1"): "", (DOORS, "1.1"): "pcs"})
    changed = {
        (f.actual, f.severity)
        for f in compare_bid(t, _real(tmp_path, "B", bid)).findings
        if f.rule_id == "BOQ.UNIT_CHANGED"
    }
    assert changed == {("", "warning"), ("pcs", "error")}


def test_sundry_items_are_not_a_placeholder() -> None:
    """ "სხვადასხვა ხარჯები" (sundry items) is work the template describes; only "სხვა ხარჯები"
    (other costs) invites the bidder's own."""
    from procurement_agent.checks.boq import _PLACEHOLDER

    assert _PLACEHOLDER.search("სხვადასხვა ხარჯები") is None
    assert _PLACEHOLDER.search("სხვა ხარჯები, საჭიროების შემთხვევაში") is not None


def test_a_quantity_header_written_short_is_still_read(tmp_path: Path) -> None:
    """ "Qty." is the quantity: a sheet whose header abbreviates it is read, not dropped."""
    t = _real(tmp_path, "T")
    comp = compare_bid(t, _real(tmp_path, "B", Bid(header_rename={CIVIL: "Qty."})))
    assert "BOQ.SHEET_UNREADABLE" not in _rules(comp)
    assert comp.evaluated_cost == pytest.approx(_cost(), abs=0.005)


def test_an_amount_formula_pointing_at_another_row_is_still_a_position() -> None:
    """``=O23*G19`` on row 23 is a position's amount with its arithmetic wrong (BOQ.ARITHMETIC),
    not a subtotal: counted as one, the row vanished from the sheet's sum and the SUB-TOTAL
    seemed not to add up."""
    from openpyxl import Workbook

    from procurement_agent.checks.boq import _line_kind

    values, formulas = Workbook(), Workbook()
    formulas.active.cell(23, 16, "=O23*G19")  # type: ignore[union-attr]
    formulas.active.cell(30, 16, "=SUM(P13:P29)")  # type: ignore[union-attr]
    cols = {"desc": 3, "qty": 7, "amount": 16}
    assert _line_kind(values.active, formulas.active, cols, 23, "Door stopper", True) == "position"
    assert _line_kind(values.active, formulas.active, cols, 30, "Group total", False) == "subtotal"


def test_text_in_a_component_rate_matters_only_without_a_total_rate() -> None:
    from procurement_agent.checks.boq import BoqLine, _rate_text_blocks

    def line(**kw: object) -> BoqLine:
        return BoqLine(  # type: ignore[arg-type]
            sheet="S", row=12, item_no="1.1", description="x", unit="m2", quantity=1.0, **kw
        )

    labour_dash = (("rate_labor", "-"),)
    # the bidder's rate is stated: the dash under labour means no labour cost
    assert not _rate_text_blocks(line(rate_total=150.0, non_numeric=labour_dash))
    # the rate must be built from the parts, and one part is text: not priced
    assert _rate_text_blocks(line(rate_materials=100.0, non_numeric=labour_dash))
    assert _rate_text_blocks(line(rate_total=150.0, non_numeric=(("qty", "incl."),)))
