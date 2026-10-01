"""TOTAL COST by its usual names (red team R3, BOQ-R3-12).

"Total construction cost" and "Total direct cost" were not read as TOTAL COST, and the bid was
left unranked. TOTAL COST goes by its usual names as overheads and profit do: "total", then at
most one qualifier (construction, direct, works, civil works, project), then "cost(s)"; and the
Georgian "პირდაპირი ხარჯები სულ", "სამშენებლო სამუშაოების ღირებულება". "Indirect costs" stays
the overheads.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.boq import BoqWorkbook, SummaryRow, summary_roles


def _roles(cost_label: str) -> dict[str, int]:
    rows = [
        SummaryRow(row=2, item="1", label="WORKS", value=1000.0),
        SummaryRow(row=3, item="", label=cost_label, value=1000.0),
        SummaryRow(row=4, item="", label="Overheads 10%", value=100.0, label_rate=0.10),
        SummaryRow(row=5, item="", label="Total excl. VAT", value=1100.0),
        SummaryRow(row=6, item="", label="VAT 18%", value=198.0, label_rate=0.18),
        SummaryRow(row=7, item="", label="Total incl. VAT", value=1298.0),
    ]
    book = BoqWorkbook(name="B", summary_rows=rows, summary_sheet="SUMMARY")
    return {role: row.row for role, row in summary_roles(book).items()}


@pytest.mark.parametrize(
    "label",
    [
        "TOTAL COST",
        "Total costs",
        "Total construction cost",
        "TOTAL DIRECT COST",
        "Total works cost",
        "Total civil works cost",
        "Total project cost",
        "პირდაპირი ხარჯები სულ",
        "სამშენებლო სამუშაოების ღირებულება",
        "ჯამური ღირებულება",
    ],
)
def test_total_cost_by_its_usual_names(label: str) -> None:
    roles = _roles(label)
    assert roles["cost"] == 3
    assert (roles["overheads"], roles["excl"], roles["vat"], roles["incl"]) == (4, 5, 6, 7)


@pytest.mark.parametrize(
    "label", ["Indirect costs", "Total indirect costs", "Cost of materials", "Total"]
)
def test_other_rows_are_not_total_cost(label: str) -> None:
    assert _roles(label).get("cost") != 3
