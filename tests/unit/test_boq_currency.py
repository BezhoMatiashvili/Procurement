"""A bid priced in another currency than the template's (red team R3, BOQ-R3-03).

Ranked on its bare numbers, a bid in lari stood beside bids in dollars as if its prices were
dollars: the colours and the leaderboard compared 2.7 lari with 1 dollar. The currency each
price header names is read; a bid whose currency differs from the template's is reported and
left out of the colours, its own figures kept.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from procurement_agent.checks.boq import BoqWorkbook, compare_all, read_boq
from tests.boq_factory import Bid, build_real_boq


def _real(tmp_path: Path, name: str, bid: Bid | None = None) -> BoqWorkbook:
    path = tmp_path / f"{name}.xlsx"
    path.write_bytes(build_real_boq(bid))
    return read_boq(path, name=name)


def test_the_currency_the_price_headers_name_is_read(tmp_path: Path) -> None:
    assert _real(tmp_path, "T").currencies == ("USD",)
    assert _real(tmp_path, "L", Bid(currency="GEL")).currencies == ("GEL",)
    assert _real(tmp_path, "D", Bid(currency="აშშ დოლარი")).currencies == ("USD",)
    assert _real(tmp_path, "S", Bid(currency="US $")).currencies == ("USD",)
    assert _real(tmp_path, "K", Bid(currency="ლარი")).currencies == ("GEL",)


def test_a_bid_in_another_currency_is_reported_and_left_out_of_the_colours(
    tmp_path: Path,
) -> None:
    t = _real(tmp_path, "T")
    bids = [
        _real(tmp_path, "A", Bid()),
        _real(tmp_path, "B", Bid(factor=0.9)),
        # the cheapest number, in lari: it must not be coloured green against dollars
        _real(tmp_path, "C", Bid(factor=0.5, currency="GEL")),
    ]
    comps = {c.bidder: c for c in compare_all(t, bids)}
    [finding] = [f for f in comps["C"].findings if f.rule_id == "BOQ.CURRENCY"]
    assert finding.severity == "warning"
    assert (finding.expected, finding.actual) == ("USD", "GEL")
    assert "GEL" in finding.message_en and "USD" in finding.message_en
    assert comps["C"].colour is None
    assert comps["C"].common_scope_total is None
    assert comps["C"].evaluated_total is not None  # its own figures are kept
    assert (comps["A"].colour, comps["B"].colour) == ("red", "green")
    assert not any(f.rule_id == "BOQ.CURRENCY" for c in ("A", "B") for f in comps[c].findings)


def test_the_same_currency_written_another_way_is_no_difference(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    bids = [
        _real(tmp_path, "A", Bid(currency="აშშ დოლარი")),
        _real(tmp_path, "B", Bid(factor=0.9, currency="$")),
    ]
    comps = compare_all(t, bids)
    assert not any(f.rule_id == "BOQ.CURRENCY" for c in comps for f in c.findings)
    assert [c.colour for c in comps] == ["red", "green"]


def _plain(
    path: Path, rate_header: str, amount_header: str, notes_header: str = "Notes"
) -> BoqWorkbook:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "1. WORKS"
    for col, title in enumerate(
        ["No", "Description", "Unit", "Quantity", rate_header, amount_header, notes_header], 1
    ):
        ws.cell(1, col, title)
    ws.cell(2, 1, "1.1")
    ws.cell(2, 2, "Concrete works")
    ws.cell(2, 3, "m3")
    ws.cell(2, 4, 10)
    ws.cell(2, 5, 150.0)
    ws.cell(2, 6, 1500.0)
    wb.save(path)
    return read_boq(path, name=path.stem)


def test_a_template_that_names_no_currency_sets_no_reference(tmp_path: Path) -> None:
    """A header that names no currency takes the template's; a template naming none gives
    nothing to hold a bid against, so no bid is reported for the currency it names."""
    t = _plain(tmp_path / "t.xlsx", "Rate", "Amount")
    assert t.currencies == ()
    b = _plain(tmp_path / "b.xlsx", "Rate, GEL", "Amount, GEL")
    assert b.currencies == ("GEL",)
    [comp] = compare_all(t, [b])
    assert not any(f.rule_id == "BOQ.CURRENCY" for f in comp.findings)


def test_a_title_naming_a_currency_outside_the_price_columns_is_not_read(tmp_path: Path) -> None:
    """Only the price columns' headers count: another column's header mentioning a currency
    ("Notes (prices in GEL are indicative)") says nothing of how the bid is priced."""
    b = _plain(
        tmp_path / "b.xlsx",
        "Rate in USD",
        "Amount in USD",
        notes_header="Notes (prices in GEL are indicative)",
    )
    assert b.currencies == ("USD",)
