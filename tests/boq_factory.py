"""Synthetic BOQ workbooks for tests.

Nothing here is client material. The committed ``tests/fixtures/boq_ka.xlsx`` cannot be reused
for comparison tests: it heads its description column ``დასახელება``, which is not in
``_HEADER_SYNONYMS["desc"]``, so ``read_boq`` skips the sheet and returns an empty workbook - a
test written against it would pass vacuously with zero findings.

The column layout mirrors the client's template closely enough to exercise the reader: an item
block split over two columns, and the bare "1 2 3 ..." row real templates carry under the header.
"""

from __future__ import annotations

import io

from openpyxl import Workbook

SECTION = "1. WORKS"

#: (item, description, unit, quantity, materials rate, labor rate)
Line = tuple[str, str, str, float, float, float]

LINES: list[Line] = [
    ("1.1", "Concrete works", "m3", 10, 100.0, 50.0),
    ("1.2", "Steel works", "t", 4, 200.0, 80.0),
    ("1.3", "Painting", "m2", 25, 8.0, 4.0),
]

_HEADERS = [
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


def blank(lines: list[Line] | None = None) -> list[Line]:
    """The template as it goes out: descriptions and units, quantities left for the bidder."""
    return [(item, desc, unit, 0, 0.0, 0.0) for item, desc, unit, _, _, _ in (lines or LINES)]


def build_boq(
    lines: list[Line],
    *,
    priced: bool = True,
    stated_total: float | None = 1234.0,
) -> bytes:
    """A BOQ workbook as bytes, ready to POST to /api/v1/documents.

    ``stated_total=None`` omits the grand-total row, so ``grand_total()`` returns None - the
    bidder who states no bottom line and must therefore never be ranked cheapest.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = SECTION
    for col, title in enumerate(_HEADERS, 1):
        ws.cell(1, col, title)
    for col in range(1, len(_HEADERS) + 1):
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
    sm.cell(1, 1, "ITEM")
    sm.cell(1, 2, "DESCRIPTION")
    sm.cell(1, 3, "TOTAL")
    sm.cell(2, 1, "1")
    sm.cell(2, 2, "WORKS")
    sm.cell(2, 3, sum((m + lab) * q for _, _, _, q, m, lab in lines) if priced else 0)
    if stated_total is not None:
        sm.cell(3, 1, "2")
        sm.cell(3, 2, "TOTAL PRICE INCLUDING VAT")
        sm.cell(3, 3, stated_total)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
