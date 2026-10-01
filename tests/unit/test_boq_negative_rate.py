"""A negative rate on a template position (red team R2, BOQ-R2-13).

It counted as a price and lowered the bidder's total, and its common-scope total, with nothing
said. A negative rate on a position of work is no price for that work: it is reported, and the
position is left out of the evaluation like text in a number cell, so the bid is incomplete.
"""

from __future__ import annotations

from pathlib import Path

from procurement_agent.checks.boq import BoqWorkbook, compare_all, compare_bid, read_boq
from tests.boq_factory import CIVIL, Bid, build_real_boq


def _real(tmp_path: Path, name: str, bid: Bid | None = None) -> BoqWorkbook:
    path = tmp_path / f"{name}.xlsx"
    path.write_bytes(build_real_boq(bid))
    return read_boq(path, name=name)


def test_a_negative_rate_is_reported_and_not_evaluated(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    honest = compare_bid(t, _real(tmp_path, "A", Bid()))
    comp = compare_bid(t, _real(tmp_path, "B", Bid(rates={(CIVIL, "1.3"): -21.5})))
    [finding] = [f for f in comp.findings if f.rule_id == "BOQ.NEGATIVE_RATE"]
    assert finding.severity == "warning"
    assert "1.3" in finding.message_en and "-21.50" in finding.message_en
    [pos] = [p for p in comp.positions if p.sheet == CIVIL and p.item_no == "1.3"]
    assert pos.status == "negative" and pos.cost == 0.0 and not pos.priced
    # the position costs nothing, never minus its amount
    assert comp.evaluated_cost == honest.evaluated_cost - 120 * 21.5
    assert not comp.complete


def test_the_negative_position_leaves_the_common_scope(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    bids = [_real(tmp_path, "A", Bid()), _real(tmp_path, "B", Bid(rates={(CIVIL, "1.3"): -1.0}))]
    a, b = compare_all(t, bids)
    for comp in (a, b):
        [pos] = [p for p in comp.positions if p.sheet == CIVIL and p.item_no == "1.3"]
        assert not pos.in_common_scope


def test_a_positive_rate_says_nothing(tmp_path: Path) -> None:
    t = _real(tmp_path, "T")
    comp = compare_bid(t, _real(tmp_path, "A", Bid()))
    assert not any(f.rule_id == "BOQ.NEGATIVE_RATE" for f in comp.findings)
