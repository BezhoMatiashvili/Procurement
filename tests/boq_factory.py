"""Synthetic BOQ workbooks for tests.

Nothing here is client material. The committed ``tests/fixtures/boq_ka.xlsx`` cannot be reused
for comparison tests: it heads its description column ``დასახელება``, which is not in
``_HEADER_SYNONYMS["desc"]``, so ``read_boq`` skips the sheet and returns an empty workbook - a
test written against it would pass vacuously with zero findings.

Two builders:

* ``build_boq`` - one flat sheet of typed-in numbers, a small Summary. Quick, and enough for
  the API and page tests that only need "a bid".
* ``build_real_boq`` - the client's real layout: a header over three rows with a "1 2 3" row
  under it, section and group rows whose amounts are SUM formulas, positions whose amounts are
  products of their own row, a group of "bidder fills in" positions with no quantity, a closing
  SUB-TOTAL row, and a Summary whose rows link to those sheets and run TOTAL COST -> overheads
  -> profit -> excl. VAT -> VAT -> incl. VAT - with the section labels repeated under every
  section, as the client's are. Every formula carries its calculated value, the way Excel saves
  it (openpyxl cannot write one, so ``_with_cached_values`` puts them into the file);
  ``cached=False`` leaves them out, which is what a workbook saved by a script looks like.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

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

    ``stated_total`` is the total incl. VAT; the Summary states the chain that leads to it
    (TOTAL COST = the lines, overheads, excl. VAT, 18% VAT), so the bidder's markup is
    ``stated_total / cost``. ``stated_total=None`` states no chain and no total, so
    ``grand_total()`` returns None - the bidder who states no bottom line and must therefore
    never be ranked cheapest.
    """
    wb = Workbook()
    ws = wb.active
    assert ws is not None
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

    cost = sum((m + lab) * q for _, _, _, q, m, lab in lines) if priced else 0.0
    sm = wb.create_sheet("SUMMARY")
    sm.cell(1, 1, "ITEM")
    sm.cell(1, 2, "DESCRIPTION")
    sm.cell(1, 3, "TOTAL")
    rows: list[tuple[str, str, float]] = [("1", "WORKS", cost)]
    if stated_total is not None:
        excl = stated_total / 1.18
        rows += [
            ("2", "TOTAL COST", cost),
            ("3", "Overheads", excl - cost),
            ("4", "TOTAL PRICE EXCL. VAT", excl),
            ("5", "VAT", stated_total - excl),
            ("6", "TOTAL PRICE INCLUDING VAT", stated_total),
        ]
    for r, (item, label, value) in enumerate(rows, start=2):
        sm.cell(r, 1, item)
        sm.cell(r, 2, label)
        sm.cell(r, 3, value)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- the client's real layout ---------------------------------------------------------------

CIVIL = "2. CIVIL WORKS"
DOORS = "3. Doors"

#: One sheet's content: a heading ("H", text), a group ("A2", text), or a position
#: (item, description, unit, template quantity - None where the bidder fills it in, rate).
Row = tuple[str, str] | tuple[str, str, str, float | None, float]

REAL_SHEETS: dict[str, list[Row]] = {
    CIVIL: [
        ("A1", "Block 1"),
        ("H", "Wall works"),
        ("1.1", "Plasterboard wall, single layer, 75 mm", "m2", 40, 45.0),
        ("1.2", "Plasterboard wall, double layer, 125 mm", "m2", 64.5, 32.0),
        ("1.3", "Bitumen damp-proof membrane", "m2", 120, 21.5),
        ("1.4", "Aluminium edge trim for wall ends", "lm", 55, 17.0),
        ("H", "Floor works"),
        ("1.5", "Levelling screed 60 mm", "m2", 250, 41.0),
        # a quantity the template leaves to the bidder, like the client's staffing lines
        ("1.6", "Site supervision, months as proposed by the tenderer", "month", None, 0.0),
        ("A2", "Other items"),
        ("2.1", "Other works to be listed here by the tenderer", "item", None, 0.0),
    ],
    DOORS: [
        ("A1", "Block 1"),
        ("1.1", "Steel door, single leaf", "item", 5, 820.0),
        ("1.2", "Overhead door closer", "item", 5, 140.0),
        ("1.3", "Wall-mounted door stop", "pcs", 8, 11.5),
        ("A2", "Other items"),
        ("2.1", "Other works to be listed here by the tenderer", "item", None, 0.0),
    ],
}
SECTION_TITLES = {CIVIL: "CIVIL WORKS", DOORS: "DOORS"}


def real_positions() -> list[tuple[str, str, float | None, float]]:
    """(sheet, item, template quantity, default rate) for every position of the real layout."""
    return [
        (sheet, row[0], row[3], row[4])  # type: ignore[misc]
        for sheet, rows in REAL_SHEETS.items()
        for row in rows
        if len(row) == 5
    ]


@dataclass
class Bid:
    """How a bid departs from the template. Every field defaults to "as the template says".

    Keys are (sheet, item). A rate of 0 or ``unpriced`` leaves the position blank.
    """

    factor: float = 1.0
    rates: dict[tuple[str, str], float] = field(default_factory=dict)
    qty: dict[tuple[str, str], float] = field(default_factory=dict)
    unit: dict[tuple[str, str], str] = field(default_factory=dict)
    desc: dict[tuple[str, str], str] = field(default_factory=dict)
    unpriced: set[tuple[str, str]] = field(default_factory=set)
    unpriced_sheets: set[str] = field(default_factory=set)
    drop: set[tuple[str, str]] = field(default_factory=set)
    #: text typed into the rate cell, e.g. "incl."
    rate_text: dict[tuple[str, str], str] = field(default_factory=dict)
    #: text typed into the quantity cell, e.g. "10 m3"
    qty_text: dict[tuple[str, str], str] = field(default_factory=dict)
    #: (sheet, after item) -> (item, description, unit, quantity, rate): a line the bidder adds
    add: dict[tuple[str, str], tuple[str, str, str, float, float]] = field(default_factory=dict)
    #: swap the rows of two positions on a sheet
    swap: tuple[str, str, str] | None = None
    #: (sheet, heading text) -> (quantity, rate): a price put on a heading row
    heading_price: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)
    #: Summary label -> a number typed over the formula (the bidder's own arithmetic)
    summary_typed: dict[str, float] = field(default_factory=dict)
    #: sheet -> the text its "Quantity" header is renamed to
    header_rename: dict[str, str] = field(default_factory=dict)
    overheads: float = 0.10
    profit: float = 0.05
    vat: float = 0.18
    #: the currency the price headers name ("Rate in USD")
    currency: str = "USD"


def build_real_boq(
    bid: Bid | None = None,
    *,
    priced: bool = True,
    cached: bool = True,
    helper_sheet: bool = False,
) -> bytes:
    """A workbook in the client's layout. ``priced=False`` (or ``bid=None``) is the template."""
    spec = bid or Bid()
    priced = priced and bid is not None
    wb = Workbook()
    default = wb.active
    assert default is not None
    wb.remove(default)
    cache: dict[tuple[int, str], object] = {}
    sm = wb.create_sheet("SUMMARY")
    subtotal_rows: dict[str, tuple[int, list[int]]] = {}
    sums: dict[str, dict[int, dict[str, float]]] = {}

    for sheet_name, rows in REAL_SHEETS.items():
        ws = wb.create_sheet(sheet_name)
        index = len(wb.sheetnames) - 1
        sums[sheet_name] = {}
        subtotal_rows[sheet_name] = _write_sheet(
            ws, index, sheet_name, rows, spec, priced, cache, sums[sheet_name]
        )
    if helper_sheet:
        hs = wb.create_sheet("Calc")
        hs.cell(1, 1, "Pile calculation")
        hs.cell(2, 1, "N")
        hs.cell(2, 2, "Name")
        hs.cell(3, 1, 1)
        hs.cell(3, 2, "Pile P-1")
    _write_summary(sm, 0, spec, priced, cache, subtotal_rows, sums)

    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    return _with_cached_values(data, cache) if cached else data


def _write_sheet(
    ws: object,
    index: int,
    sheet_name: str,
    rows: list[Row],
    spec: Bid,
    priced: bool,
    cache: dict[tuple[int, str], object],
    sums: dict[int, dict[str, float]],
) -> tuple[int, list[int]]:
    """One priced sheet; returns (SUB-TOTAL row, group rows)."""

    def put(row: int, col: int, value: object, cached: object = None) -> None:
        cell = ws.cell(row, col, value)  # type: ignore[attr-defined]
        if isinstance(value, str) and value.startswith("="):
            cache[(index, cell.coordinate)] = cached

    put(1, 1, "Example project")
    put(2, 11, f"rates in {spec.currency} without VAT")
    put(4, 1, "THE BILLS OF QUANTITIES")
    put(5, 1, sheet_name)
    quantity = spec.header_rename.get(sheet_name, "Quantity")
    for col, title in {
        1: "No",
        3: "Description",
        5: "CODE",
        7: quantity,
        8: "Unit",
        9: f"Unit rates in {spec.currency} (excl. VAT)",
        15: f"Totals in {spec.currency} (excl. VAT)",
        17: "Notes",
    }.items():
        put(6, col, title)
    for col, title in {
        9: "Materials",
        10: "Materials Sum",
        11: "Labour",
        12: "Labour Sum",
        13: "Delivery",
        14: "Delivery Sum",
        15: f"Rate in {spec.currency}",
        16: f"Amount in {spec.currency}",
    }.items():
        put(7, col, title)
    for col in range(1, 18):
        put(8, col, col)
    put(9, 1, "A")
    put(9, 3, SECTION_TITLES[sheet_name])

    entries: list[Row] = []
    for entry in rows:
        if len(entry) == 5 and (sheet_name, entry[0]) in spec.drop:
            continue
        entries.append(entry)
        if len(entry) == 5 and (sheet_name, entry[0]) in spec.add:
            entries.append(spec.add[(sheet_name, entry[0])])
    if spec.swap and spec.swap[0] == sheet_name:
        items = [e[0] if len(e) == 5 else None for e in entries]
        i, j = items.index(spec.swap[1]), items.index(spec.swap[2])
        entries[i], entries[j] = entries[j], entries[i]

    row = 11
    groups: list[tuple[int, list[int]]] = []
    for entry in entries:
        if entry[0] == "H":
            put(row, 3, entry[1])
            price = spec.heading_price.get((sheet_name, entry[1]))
            if price is not None and priced:
                _position(put, row, price[0], price[1], sums, text=None)
            row += 1
            continue
        if len(entry) == 2:
            if groups:
                row += 1
            put(row, 1, entry[0])
            put(row, 3, entry[1])
            groups.append((row, []))
            row += 1
            continue
        item, desc, unit, qty, rate = entry  # type: ignore[misc]
        key = (sheet_name, item)
        put(row, 2, item)
        put(row, 3, spec.desc.get(key, desc))
        bid_qty = spec.qty.get(key, qty) if priced else qty
        text = spec.qty_text.get(key) if priced else None
        if text is not None:
            put(row, 7, text)
        elif bid_qty is not None:
            put(row, 7, bid_qty)
        put(row, 8, spec.unit.get(key, unit) if priced else unit)
        r = spec.rates.get(key, rate * spec.factor)
        if not priced or key in spec.unpriced or sheet_name in spec.unpriced_sheets:
            r = 0.0
        _position(
            put,
            row,
            bid_qty if text is None else None,
            r,
            sums,
            text=spec.rate_text.get(key) if priced else None,
        )
        groups[-1][1].append(row)
        row += 1

    # A group sums the range under it, the way the client's template does: a price put on a
    # heading row inside the range lands in the SUB-TOTAL too.
    for group_row, members in groups:
        last = max(members, default=group_row + 1)
        for col in (10, 12, 14, 16):
            letter = "JLNP"[(col - 10) // 2]
            value = sum(sums.get(r, {}).get(letter, 0.0) for r in range(group_row + 1, last + 1))
            put(group_row, col, f"=SUM({letter}{group_row + 1}:{letter}{last})", value)
            sums.setdefault(group_row, {})[letter] = value
    row += 1
    put(row, 3, '="SUB-TOTAL "&A5&""', f"SUB-TOTAL {sheet_name}")
    for col in (10, 12, 14, 16):
        letter = "JLNP"[(col - 10) // 2]
        value = sum(sums[g][letter] for g, _ in groups)
        put(row, col, "=" + "+".join(f"{letter}{g}" for g, _ in groups), value)
        sums.setdefault(row, {})[letter] = value
    return row, [g for g, _ in groups]


def _position(
    put: object,
    row: int,
    qty: float | None,
    rate: float,
    sums: dict[int, dict[str, float]],
    *,
    text: str | None,
) -> None:
    """Rates in I (materials), formulas in J..P - the client's column block."""
    q = qty or 0.0
    if text is not None:
        put(row, 9, text)  # type: ignore[operator]
        rate = 0.0
    elif rate:
        put(row, 9, rate)  # type: ignore[operator]
    put(row, 10, f"=I{row}*G{row}", rate * q)  # type: ignore[operator]
    put(row, 12, f"=K{row}*G{row}", 0)  # type: ignore[operator]
    put(row, 14, f"=M{row}*G{row}", 0)  # type: ignore[operator]
    put(row, 15, f"=I{row}+K{row}+M{row}", rate)  # type: ignore[operator]
    put(row, 16, f"=O{row}*G{row}", rate * q)  # type: ignore[operator]
    sums[row] = {"J": rate * q, "L": 0.0, "N": 0.0, "P": rate * q}


def _write_summary(
    sm: object,
    index: int,
    spec: Bid,
    priced: bool,
    cache: dict[tuple[int, str], object],
    subtotal_rows: dict[str, tuple[int, list[int]]],
    sums: dict[str, dict[int, dict[str, float]]],
) -> None:
    def put(row: int, col: int, value: object, cached: object = None) -> None:
        cell = sm.cell(row, col, value)  # type: ignore[attr-defined]
        if isinstance(value, str) and value.startswith("="):
            cache[(index, cell.coordinate)] = cached

    put(1, 1, "Example project")
    put(5, 1, "SUMMARY")
    put(6, 1, "ITEM")
    put(6, 2, "DESCRIPTION")
    put(6, 3, "AMOUNT EXCL. VAT")
    put(6, 7, "SHARE")
    put(7, 3, "MATERIALS")
    put(7, 4, "LABOUR")
    put(7, 5, "TRANSPORT")
    put(7, 6, "TOTAL")
    for col in range(1, 9):
        put(8, col, col)

    row = 9
    section_rows: list[int] = []
    section_totals: list[float] = []
    for number, (sheet_name, (sub_row, group_rows)) in enumerate(subtotal_rows.items(), start=2):
        for i, (linked, item, label) in enumerate(
            [
                (sub_row, str(number), SECTION_TITLES[sheet_name]),
                *(
                    (g, f"A{k}", ("Block 1", "Other items")[k - 1])
                    for k, g in enumerate(group_rows, start=1)
                ),
            ]
        ):
            put(row, 1, item)
            put(row, 2, label)
            total = 0.0
            for col, letter in ((3, "J"), (4, "L"), (5, "N")):
                value = sums[sheet_name][linked][letter]
                put(row, col, f"='{sheet_name}'!{letter}{linked}", value)
                total += value
            stated = _total(put, spec, row, f"=SUM(C{row}:E{row})", total, item, label)
            if i == 0:
                section_rows.append(row)
                section_totals.append(stated)
            row += 1
    cost = sum(section_totals)
    cost_row = row
    put(row, 1, str(len(section_rows) + 2))
    put(row, 2, "TOTAL COST ")
    cost = _total(
        put,
        spec,
        row,
        "=SUM(" + ",".join(f"F{r}" for r in section_rows) + ")",
        cost,
        "",
        "TOTAL COST",
    )
    row += 1
    overheads = spec.overheads if priced else 0.0
    profit = spec.profit if priced else 0.0
    put(row, 2, "Overheads")
    oh = _total(put, spec, row, f"=F{cost_row}*G{row}", cost * overheads, "", "Overheads")
    if priced:
        put(row, 7, overheads)
    row += 1
    put(row, 2, "Profit")
    pr = _total(put, spec, row, f"=F{cost_row}*G{row}", cost * profit, "", "Profit")
    if priced:
        put(row, 7, profit)
    row += 1
    excl_row = row
    put(row, 2, "TOTAL PRICE EXCL. VAT")
    excl = _total(
        put, spec, row, f"=SUM(F{cost_row}:F{row - 1})", cost + oh + pr, "", "TOTAL PRICE EXCL. VAT"
    )
    row += 1
    put(row, 2, "VAT")
    vat = _total(put, spec, row, f"=F{excl_row}*G{row}", excl * spec.vat, "", "VAT")
    put(row, 7, spec.vat)
    row += 1
    put(row, 2, "TOTAL PRICE INCLUDING VAT")
    _total(
        put, spec, row, f"=SUM(F{excl_row}:F{row - 1})", excl + vat, "", "TOTAL PRICE INCLUDING VAT"
    )


def _total(
    put: object, spec: Bid, row: int, formula: str, value: float, item: str, label: str
) -> float:
    """The TOTAL cell of a Summary row: the formula, or the number a bidder typed over it."""
    typed = spec.summary_typed.get(label)
    if typed is not None:
        put(row, 6, typed)  # type: ignore[operator]
        return typed
    put(row, 6, formula, value)  # type: ignore[operator]
    return value


_CELL = re.compile(r'<c r="([A-Z]+\d+)"([^>]*)><f>(.*?)</f><v\s*/?>(?:</v>)?</c>', re.S)


def _with_cached_values(data: bytes, cache: dict[tuple[int, str], object]) -> bytes:
    """Put each formula's calculated value into the saved file, as Excel does on save."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            content = src.read(info.filename)
            m = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", info.filename)
            if m:
                index = int(m.group(1)) - 1

                def fill(match: re.Match[str], index: int = index) -> str:
                    coord, attrs, formula = match.group(1), match.group(2), match.group(3)
                    value = cache.get((index, coord))
                    if value is None:
                        return match.group(0)
                    if isinstance(value, str):
                        return (
                            f'<c r="{coord}"{attrs} t="str"><f>{formula}</f>'
                            f"<v>{escape(value)}</v></c>"
                        )
                    return f'<c r="{coord}"{attrs}><f>{formula}</f><v>{float(value)!r}</v></c>'

                content = _CELL.sub(fill, content.decode("utf-8")).encode("utf-8")
            dst.writestr(info, content)
    return out.getvalue()
