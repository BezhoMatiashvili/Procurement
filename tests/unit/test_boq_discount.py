"""A discount written below the total incl. VAT (red team R2, BOQ-R2-12).

The bid's offer is then the discounted total, but the chain ends at the total incl. VAT and
the bid was ranked on it - before the discount - with nothing said. Which total a bid offers
is the client's call, so the discount is reported (a warning naming both totals), never left
silent. A discount inside the chain (before the total excl. VAT) is part of it and is ranked.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.boq import BoqWorkbook, SummaryRow, reconcile_summary


def _book(rows: list[SummaryRow]) -> BoqWorkbook:
    return BoqWorkbook(name="B", summary_rows=rows, summary_sheet="SUMMARY")


_CHAIN = [
    SummaryRow(row=3, item="", label="TOTAL COST", value=1000.0),
    SummaryRow(row=4, item="", label="Total excl. VAT", value=1000.0),
    SummaryRow(row=5, item="", label="VAT 18%", value=180.0, label_rate=0.18),
    SummaryRow(row=6, item="", label="Total incl. VAT", value=1180.0),
]


@pytest.mark.parametrize("label", ["Discount 5%", "Rebate", "ფასდაკლება 5%"])
def test_a_discount_below_the_total_incl_vat_is_reported(label: str) -> None:
    rows = [
        *_CHAIN,
        SummaryRow(row=7, item="", label=label, value=-59.0),
        SummaryRow(row=8, item="", label="Total incl. VAT after discount", value=1121.0),
    ]
    [finding] = [f for f in reconcile_summary(_book(rows)) if f.rule_id == "BOQ.DISCOUNT"]
    assert finding.severity == "warning"
    assert "row 7" in finding.message_en and "1,180.00" in finding.message_en
    assert "1,121.00" in finding.message_en  # the total after it, when the Summary states one
    assert finding.actual == "-59.00"


def test_a_discount_inside_the_chain_is_part_of_it() -> None:
    rows = [
        SummaryRow(row=3, item="", label="TOTAL COST", value=1000.0),
        SummaryRow(row=4, item="", label="Discount 5%", value=-50.0),
        SummaryRow(row=5, item="", label="Total excl. VAT", value=950.0),
        SummaryRow(row=6, item="", label="VAT 18%", value=171.0, label_rate=0.18),
        SummaryRow(row=7, item="", label="Total incl. VAT", value=1121.0),
    ]
    assert reconcile_summary(_book(rows)) == []


def test_an_empty_discount_row_or_a_note_below_the_total_says_nothing() -> None:
    rows = [
        *_CHAIN,
        SummaryRow(row=7, item="", label="Discount", value=0.0),
        SummaryRow(row=8, item="", label="Discount", value=None),
        SummaryRow(row=9, item="", label="Advance payment 20%", value=236.0),
    ]
    assert not any(f.rule_id == "BOQ.DISCOUNT" for f in reconcile_summary(_book(rows)))
