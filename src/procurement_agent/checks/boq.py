"""Bill-of-quantities comparison: template versus each bidder's returned workbook.

Implements the client's BOQ instructions: compare quantities on every sheet, list added and
removed positions separately per bidder and keep them out of the general analysis, merge all
bidders into the template's sheets stacked vertically, and produce a Summary with the highest
total in red and the lowest in green.

Beyond that it cross-foots the arithmetic. A BOQ is a spreadsheet the bidder controls, so the
totals it reports are a claim, not a fact: every line is recomputed from quantity x rate, every
section from its lines, and the Summary from the sections. Cells where a formula was replaced
by a typed-in number are flagged too, which is only visible because the workbook is read twice,
once for cached values and once for formulas.

Entirely deterministic - no LLM is involved at any point.
"""

from __future__ import annotations

import argparse
import difflib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from procurement_agent.checks.findings import CheckReport, Finding, Severity
from procurement_agent.extraction.normalize import normalize_spaces, parse_number
from procurement_agent.report.xlsx_safe import cell_safe

#: Sheets that carry no priced lines.
SKIP_SHEETS = frozenset({"COVER PAGE", "INSTRUCTION TO BIDDERS", "SUMMARY"})
_SUMMARY = "SUMMARY"

#: Header labels we understand, normalised. The prototype required the exact lowercase strings
#: "quantity" and "unit" and silently skipped any sheet that spelled them differently.
_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "no": ("no", "no.", "item", "item no", "№", "#"),
    "desc": ("description", "descriptions", "description of works", "work description"),
    "qty": ("quantity", "qty", "quantities", "amount of work", "რაოდენობა"),
    "unit": ("unit", "units", "unit of measure", "uom", "ერთეული"),
    "rate_materials": ("materials", "material"),
    "sum_materials": ("materials sum", "material sum"),
    "rate_labor": ("labor", "labour"),
    "sum_labor": ("labor sum", "labour sum"),
    # _norm_label strips surrounding whitespace, so "delivery " needs no entry of its own
    "rate_delivery": ("delivery", "transportation"),
    "sum_delivery": ("delivery sum", "transportation sum"),
    "rate_total": ("rate in us $", "rate in usd", "rate"),
    "amount": ("amount in us $", "amount in usd", "amount", "total"),
}
_LABEL_TO_KEY = {label: key for key, labels in _HEADER_SYNONYMS.items() for label in labels}

_HEADER_SCAN_ROWS = 14
_HEADER_SCAN_COLS = 30
#: Line totals are money; a cent of drift is rounding, not an error.
_MONEY_TOL = 0.01
_RATE_OUTLIER_FACTOR = 3.0
#: a real description contains letters; "2" is a column number
_LETTER = re.compile(r"[A-Za-zა-ჰ]")
#: What sits in an item-number column: a section code ("A", "A1") or a position ("1.1", "2.1.3").
_ITEM_NO = re.compile(r"^(?:[A-Za-z]\d{0,3}|\d{1,3}(?:\.\d{1,3}){0,4})\.?$")
#: The row that closes a priced sheet: "SUB-TOTAL 3. DOORS", "ქვეჯამი 3. კარები".
_SUBTOTAL = re.compile(r"^\s*(?:sub[\s-]?total|ქვეჯამ)", re.IGNORECASE)


def _norm_label(v: object) -> str:
    return normalize_spaces(str(v or "")).strip().lower().rstrip(":")


def _num(v: object) -> float | None:
    if v is None or v == "" or isinstance(v, bool):  # TRUE in a quantity cell is not 1
        return None
    if isinstance(v, int | float):
        return float(v)
    return parse_number(str(v))


def _same_quantity(a: float | None, b: float | None) -> bool:
    """Quantities compared with a tolerance: ``0.1 * 3`` and ``0.3`` are the same measurement.

    A blank and a zero are the same too - both mean "nothing quoted here".
    """
    return math.isclose(a or 0.0, b or 0.0, rel_tol=1e-9, abs_tol=1e-6)


@dataclass(frozen=True, slots=True)
class BoqLine:
    sheet: str
    row: int
    item_no: str
    description: str
    unit: str | None
    quantity: float | None
    rate_materials: float | None = None
    rate_labor: float | None = None
    rate_delivery: float | None = None
    rate_total: float | None = None
    amount: float | None = None
    sum_materials: float | None = None
    sum_labor: float | None = None
    sum_delivery: float | None = None
    #: columns whose value was typed in rather than computed by a formula
    hardcoded: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Identity of a position: its number and description.

        Deliberately excludes unit and quantity. A bidder filling in a blank unit is a unit
        change, reported as such - not a deleted line plus an unrelated new one.
        """
        desc = re.sub(r"\s+", " ", self.description).strip().lower()[:70]
        return f"{self.item_no.strip().lower()}|{desc}"

    @property
    def is_priced(self) -> bool:
        return bool(self.amount) or bool(self.rate_total)


@dataclass(slots=True)
class BoqSheet:
    name: str
    columns: dict[str, int]
    header_row: int = 0
    lines: list[BoqLine] = field(default_factory=list)
    #: The amount on the sheet's own closing SUB-TOTAL row, and that row, when there is one.
    #: The positions read must add up to it: that is how a reader that silently skipped rows
    #: is caught (``reconcile``) instead of shipping a total that looks right.
    subtotal: float | None = None
    subtotal_row: int | None = None

    @property
    def leaf_lines(self) -> list[BoqLine]:
        """Priced positions, excluding the section subtotal rows that sum them."""
        return [line for line in self.lines if line.quantity is not None]

    @property
    def total(self) -> float:
        """Recomputed from the leaf lines, never read off a subtotal the bidder wrote."""
        return sum(line.amount or 0.0 for line in self.leaf_lines)


@dataclass(slots=True)
class BoqWorkbook:
    name: str
    sheets: dict[str, BoqSheet] = field(default_factory=dict)
    summary: dict[str, float | None] = field(default_factory=dict)
    sheet_order: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(s.total for s in self.sheets.values())


def _cell_value(ws: object, cols: dict[str, int], row: int, key: str) -> float | None:
    column = cols.get(key)
    return _num(ws.cell(row, column).value) if column else None  # type: ignore[attr-defined]


def _label_key(value: object) -> str | None:
    return _LABEL_TO_KEY.get(_norm_label(value))


def _is_label_row(ws: object, row: int) -> bool:
    """Whether ``row``, under a header row already found, is more of the header.

    Mostly known labels ("Materials", "Labour Sum"), no numbers and no item number: a header
    row. Anything else is the sheet's content. The unit "item" is itself a header label (the
    Summary's "Item" column), so a data row whose unit reads "item" used to count as header too:
    on every sheet whose first positions are counted in items, the "header" then ran on to row
    14 and those positions were never read - an entire sheet of doors, gone from the quantities,
    the totals and the merged workbook without a word.
    """
    values = [
        ws.cell(row, c).value  # type: ignore[attr-defined]
        for c in range(1, _HEADER_SCAN_COLS + 1)
    ]
    present = [v for v in values if v not in (None, "")]
    if not present or any(isinstance(v, int | float) for v in present):
        return False
    if any(_ITEM_NO.match(str(v).strip()) for v in values[:3] if v not in (None, "")):
        return False
    labels = sum(1 for v in present if _label_key(v))
    return labels > 0 and 2 * labels >= len(present)


def _map_headers(ws: object) -> tuple[dict[str, int], int]:
    """Locate columns by label in the header block, and where that block ends.

    The template splits its header over three rows - group label, sub-label, and a row of bare
    column numbers - so a single-row scan finds "Quantity" but not "Materials Sum". The block
    starts at the first row naming at least two columns, one of them the description or the
    quantity, and runs on only through rows that are themselves headers (``_is_label_row``),
    then past a "1 2 3 ..." column-number row. That row reads as a perfectly good line item,
    and then 5 x 7 != 8 makes every sheet look like it cannot add up.
    """
    cols: dict[str, int] = {}
    last_row = 0
    for r in range(1, _HEADER_SCAN_ROWS + 1):
        found = [
            (c, key)
            for c in range(1, _HEADER_SCAN_COLS + 1)
            if (key := _label_key(ws.cell(r, c).value))  # type: ignore[attr-defined]
        ]
        keys = {key for _, key in found}
        if not cols:
            if len(keys) < 2 or not keys & {"desc", "qty"}:
                continue
        elif not _is_label_row(ws, r):
            break
        for c, key in found:
            cols.setdefault(key, c)
        last_row = r
    if cols:
        last_row = max(last_row, _column_number_row(ws, last_row))
    return cols, last_row


def _column_number_row(ws: object, after: int) -> int:
    """The '1 2 3 4 ...' row some templates put under the header, if present."""
    for r in range(after, min(after + 3, _HEADER_SCAN_ROWS) + 1):
        values = [
            ws.cell(r, c).value  # type: ignore[attr-defined]
            for c in range(1, _HEADER_SCAN_COLS)
        ]
        present = [v for v in values if v not in (None, "")]
        if len(present) >= 4 and all(str(v).strip().isdigit() for v in present):
            return r
    return after


def _item_columns(cols: dict[str, int]) -> tuple[int, ...]:
    """Where a line's item number is, most specific first.

    The client's template heads a two-column block "No": section codes ("A", "A1") in the first
    column and the item numbers ("1.1", "1.2") in the second, so the second is read first. That
    guess is only safe when the second column is not itself a mapped column - on a sheet whose
    description sits right beside "No", the description would otherwise become the item number.
    """
    no_col = cols.get("no", 1)
    if no_col + 1 in cols.values():
        return (no_col,)
    return (no_col + 1, no_col)


def read_boq(path: Path, *, name: str | None = None) -> BoqWorkbook:
    """Read a BOQ workbook twice: cached values, and formulas, so tampering is visible."""
    values = load_workbook(path, data_only=True)
    try:
        formulas = load_workbook(path, data_only=False)
        try:
            return _read_boq(values, formulas, name=name or path.stem)
        finally:
            formulas.close()
    finally:
        values.close()


def _read_boq(values: Workbook, formulas: Workbook, *, name: str) -> BoqWorkbook:
    book = BoqWorkbook(name=name, sheet_order=list(values.sheetnames))
    summary_sheet: str | None = None

    for sheet_name in values.sheetnames:
        if sheet_name.strip().upper() == _SUMMARY and summary_sheet is None:
            summary_sheet = sheet_name
        if sheet_name.strip().upper() in SKIP_SHEETS:
            continue
        ws, wf = values[sheet_name], formulas[sheet_name]
        cols, header_row = _map_headers(ws)
        if "desc" not in cols or "qty" not in cols:
            continue
        sheet = BoqSheet(name=sheet_name, columns=cols, header_row=header_row)
        item_cols = _item_columns(cols)

        for r in range(header_row + 1, ws.max_row + 1):
            desc = ws.cell(r, cols["desc"]).value
            if desc is None or not _LETTER.search(str(desc)):
                continue

            def val(
                key: str, _row: int = r, _ws: object = ws, _cols: dict[str, int] = cols
            ) -> float | None:
                return _cell_value(_ws, _cols, _row, key)

            hardcoded: list[str] = []
            for key in ("sum_materials", "sum_labor", "sum_delivery", "amount"):
                c = cols.get(key)
                if not c:
                    continue
                # the cell's type, not its text: a *string* cell reading "=5" is typed-in text
                if val(key) not in (None, 0) and wf.cell(r, c).data_type != "f":
                    hardcoded.append(key)

            item_no = next(
                (v for c in item_cols if (v := ws.cell(r, c).value) not in (None, "")), None
            )
            sheet.lines.append(
                BoqLine(
                    sheet=sheet_name,
                    row=r,
                    item_no=str(item_no or "").strip(),
                    description=str(desc).strip(),
                    unit=(
                        str(ws.cell(r, cols["unit"]).value).strip()
                        if cols.get("unit") and ws.cell(r, cols["unit"]).value
                        else None
                    ),
                    quantity=val("qty"),
                    rate_materials=val("rate_materials"),
                    rate_labor=val("rate_labor"),
                    rate_delivery=val("rate_delivery"),
                    rate_total=val("rate_total"),
                    amount=val("amount"),
                    sum_materials=val("sum_materials"),
                    sum_labor=val("sum_labor"),
                    sum_delivery=val("sum_delivery"),
                    hardcoded=tuple(hardcoded),
                )
            )
        for line in reversed(sheet.lines):
            if _SUBTOTAL.match(line.description) and line.quantity is None:
                sheet.subtotal, sheet.subtotal_row = line.amount, line.row
                break
        book.sheets[sheet_name] = sheet

    # Matched the way SKIP_SHEETS is: a bidder's "Summary" is still the Summary.
    if summary_sheet is not None:
        book.summary = _read_summary(values[summary_sheet])
    return book


def _read_summary(ws: object) -> dict[str, float | None]:
    """Section and grand totals exactly as the bidder states them.

    These are read, never summed: the Summary nests sections inside groups, so adding the
    column up double-counts. They are what the bidder claims; ``BoqWorkbook.total`` is what the
    line items actually add up to, and the difference between the two is worth seeing.

    Every column is located by its header label. One of the client's bidders returned a Summary
    shifted one column to the right, which a reader that assumed columns A and B silently read
    as all zeroes - a wrong answer that looks like a correct one.
    """
    item_col = desc_col = total_col = None
    for row in ws.iter_rows(min_row=1, max_row=_HEADER_SCAN_ROWS, max_col=_HEADER_SCAN_COLS):  # type: ignore[attr-defined]
        for cell in row:
            label = _norm_label(cell.value)
            if label == "total" and total_col is None:
                total_col = cell.column
            elif label == "item" and item_col is None:
                item_col = cell.column
            elif label == "description" and desc_col is None:
                desc_col = cell.column
    out: dict[str, float | None] = {}
    if total_col is None or desc_col is None:
        return out
    for r in range(1, ws.max_row + 1):  # type: ignore[attr-defined]
        desc = ws.cell(r, desc_col).value  # type: ignore[attr-defined]
        label = str(desc or "").strip()
        if not label or not _LETTER.search(label):
            continue
        value = _num(ws.cell(r, total_col).value)  # type: ignore[attr-defined]
        if value is None:
            continue
        item = ws.cell(r, item_col).value if item_col else None  # type: ignore[attr-defined]
        key = f"{str(item).strip()} {label}" if item not in (None, "") else label
        out[key] = value
    return out


def grand_total(book: BoqWorkbook, *, incl_vat: bool = True) -> float | None:
    """The bidder's own bottom line, as their Summary states it."""
    rows = [(k, v) for k, v in book.summary.items() if v]
    wanted = "incl" if incl_vat else "excl"
    for key, value in reversed(rows):
        low = key.lower()
        if "total price" in low and wanted in low:
            return value
    for key, value in reversed(rows):
        if "total" in key.lower():
            return value
    return None


# --- comparison ---------------------------------------------------------------------------


@dataclass(slots=True)
class LineDiff:
    sheet: str
    key: str
    description: str
    template_qty: float | None
    bid_qty: float | None
    row: int


@dataclass(slots=True)
class BidComparison:
    bidder: str
    quantity_changed: list[LineDiff] = field(default_factory=list)
    filled_blank: list[LineDiff] = field(default_factory=list)
    added: list[BoqLine] = field(default_factory=list)
    removed: list[BoqLine] = field(default_factory=list)
    added_sheets: list[str] = field(default_factory=list)
    removed_sheets: list[str] = field(default_factory=list)
    unpriced_sheets: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    matched: list[tuple[BoqLine, BoqLine]] = field(default_factory=list)


def _align(
    template: list[BoqLine], bid: list[BoqLine]
) -> tuple[list[tuple[BoqLine, BoqLine]], list[BoqLine], list[BoqLine]]:
    """Align two line lists by key.

    Sequence alignment rather than dictionary lookup: it keeps document order, tolerates
    repeated boilerplate descriptions that a dict would silently collapse, and is what turns
    "added/removed position" into a real answer instead of a by-product of key collisions.
    """
    matcher = difflib.SequenceMatcher(
        None, [line.key for line in template], [line.key for line in bid], autojunk=False
    )
    matched: list[tuple[BoqLine, BoqLine]] = []
    removed: list[BoqLine] = []
    added: list[BoqLine] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            matched.extend(zip(template[i1:i2], bid[j1:j2], strict=True))
        elif tag == "replace":
            removed.extend(template[i1:i2])
            added.extend(bid[j1:j2])
        elif tag == "delete":
            removed.extend(template[i1:i2])
        elif tag == "insert":
            added.extend(bid[j1:j2])
    return matched, added, removed


def _finding(
    rule: str,
    severity: Severity,
    ka: str,
    en: str,
    *,
    expected: str | None = None,
    actual: str | None = None,
    confidence: float = 1.0,
) -> Finding:
    return Finding(
        rule_id=rule,
        severity=severity,
        message_ka=ka,
        message_en=en,
        expected=expected,
        actual=actual,
        confidence=confidence,
    )


def compare_bid(template: BoqWorkbook, bid: BoqWorkbook) -> BidComparison:
    out = BidComparison(bidder=bid.name)
    out.added_sheets = [s for s in bid.sheet_order if s not in template.sheet_order]
    out.removed_sheets = [s for s in template.sheet_order if s not in bid.sheet_order]
    for s in out.added_sheets:
        out.findings.append(
            _finding(
                "BOQ.SHEET_ADDED",
                "warning",
                f"დამატებულია გვერდი, რომელიც შაბლონში არ არის: {s}",
                f"sheet not present in the template: {s}",
                actual=s,
            )
        )
    for s in out.removed_sheets:
        out.findings.append(
            _finding(
                "BOQ.SHEET_REMOVED",
                "error",
                f"შაბლონის გვერდი ამოღებულია: {s}",
                f"template sheet is missing: {s}",
                expected=s,
            )
        )

    for sheet_name, t_sheet in template.sheets.items():
        b_sheet = bid.sheets.get(sheet_name)
        if b_sheet is None:
            continue
        matched, added, removed = _align(t_sheet.lines, b_sheet.lines)
        out.matched.extend(matched)
        out.added.extend(added)
        out.removed.extend(removed)

        for t_line, b_line in matched:
            t_unit = (t_line.unit or "").strip().lower()
            b_unit = (b_line.unit or "").strip().lower()
            if t_unit and b_unit and t_unit != b_unit:
                out.findings.append(
                    _finding(
                        "BOQ.UNIT_CHANGED",
                        "error",
                        f"{sheet_name}, სტრიქონი {b_line.row}: ერთეული შეცვლილია "
                        f"{t_line.unit} → {b_line.unit}",
                        f"{sheet_name} row {b_line.row}: unit changed "
                        f"{t_line.unit} -> {b_line.unit}",
                        expected=t_line.unit,
                        actual=b_line.unit,
                    )
                )
            if not _same_quantity(t_line.quantity, b_line.quantity):
                diff = LineDiff(
                    sheet=sheet_name,
                    key=t_line.key,
                    description=t_line.description,
                    template_qty=t_line.quantity,
                    bid_qty=b_line.quantity,
                    row=b_line.row,
                )
                # a blank template quantity means "bidder fills this in" (staffing, insurance)
                (out.quantity_changed if t_line.quantity else out.filled_blank).append(diff)
            out.findings.extend(_arithmetic_findings(b_line))
        # An added position is priced by the bidder like any other and counts toward their
        # total, so its arithmetic is checked too. Keeping it "out of the general analysis"
        # (the client's rule) means out of the quantity comparison - not out of cross-footing.
        for b_line in added:
            out.findings.extend(_arithmetic_findings(b_line))

        priced = [line for line in b_sheet.lines if line.is_priced]
        if not priced and any(line.quantity for line in t_sheet.lines):
            out.unpriced_sheets.append(sheet_name)
        gap = reconcile(b_sheet)
        if gap is not None:
            out.findings.append(gap)

    # Sheets only the bidder has: nothing to compare quantities against, but every line on
    # them still adds into the bidder's total.
    for sheet_name, b_sheet in bid.sheets.items():
        if sheet_name not in template.sheets:
            for b_line in b_sheet.lines:
                out.findings.extend(_arithmetic_findings(b_line))

    for d in out.quantity_changed:
        out.findings.append(
            _finding(
                "BOQ.QUANTITY_CHANGED",
                "error",
                f"{d.sheet}, სტრიქონი {d.row}: მოცულობა შეცვლილია "
                f"{d.template_qty:g} → {d.bid_qty or 0:g} ({d.description[:50]})",
                f"{d.sheet} row {d.row}: quantity changed "
                f"{d.template_qty:g} -> {d.bid_qty or 0:g} ({d.description[:50]})",
                expected=f"{d.template_qty:g}",
                actual=f"{d.bid_qty or 0:g}",
            )
        )
    for line in out.added:
        out.findings.append(
            _finding(
                "BOQ.LINE_ADDED",
                "warning",
                f"{line.sheet}, სტრიქონი {line.row}: დამატებული პოზიცია — {line.description[:60]}",
                f"{line.sheet} row {line.row}: added position - {line.description[:60]}",
                actual=line.description[:60],
            )
        )
    for line in out.removed:
        out.findings.append(
            _finding(
                "BOQ.LINE_REMOVED",
                "error",
                f"{line.sheet}, სტრიქონი {line.row}: ამოღებული პოზიცია — {line.description[:60]}",
                f"{line.sheet} row {line.row}: removed position - {line.description[:60]}",
                expected=line.description[:60],
            )
        )
    for s in out.unpriced_sheets:
        out.findings.append(
            _finding(
                "BOQ.SECTION_UNPRICED",
                "warning",
                f"სექცია ფასდაუდებელია: {s} — ჯამური ფასი შედარებადი არ არის",
                f"section left unpriced: {s} - the total is not comparable like-for-like",
                actual=s,
            )
        )
    return out


def _arithmetic_findings(line: BoqLine) -> list[Finding]:
    """Recompute what the workbook claims. The bidder controls this file."""
    out: list[Finding] = []
    qty = line.quantity

    def check(label: str, got: float | None, want: float | None) -> None:
        if got is None or want is None:
            return
        if abs(got - want) > max(_MONEY_TOL, abs(want) * 1e-6):
            out.append(
                _finding(
                    "BOQ.ARITHMETIC",
                    "error",
                    f"{line.sheet}, სტრიქონი {line.row}: {label} არ ემთხვევა გამოთვლას "
                    f"({got:,.2f} vs {want:,.2f})",
                    f"{line.sheet} row {line.row}: {label} does not match the computation "
                    f"({got:,.2f} vs {want:,.2f})",
                    expected=f"{want:,.2f}",
                    actual=f"{got:,.2f}",
                )
            )

    def times(rate: float | None) -> float | None:
        # No rate column, or an empty rate cell, means there is nothing to recompute from.
        # Treating it as zero made every priced line of a sheet without a Rate column an error.
        return rate * qty if rate is not None and qty is not None else None

    if qty:
        check("materials sum", line.sum_materials, times(line.rate_materials))
        check("labor sum", line.sum_labor, times(line.rate_labor))
        check("delivery sum", line.sum_delivery, times(line.rate_delivery))
        check("line amount", line.amount, times(line.rate_total))
    rates = [line.rate_materials or 0, line.rate_labor or 0, line.rate_delivery or 0]
    if line.rate_total is not None and any(rates):
        check("total rate", line.rate_total, sum(rates))
    if line.hardcoded:
        out.append(
            _finding(
                "BOQ.HARDCODED_TOTAL",
                "warning",
                f"{line.sheet}, სტრიქონი {line.row}: ჯამი ფორმულის ნაცვლად ხელით არის ჩაწერილი "
                f"({', '.join(line.hardcoded)})",
                f"{line.sheet} row {line.row}: total typed in instead of computed "
                f"({', '.join(line.hardcoded)})",
                actual=", ".join(line.hardcoded),
            )
        )
    return out


def reconcile(sheet: BoqSheet) -> Finding | None:
    """The positions read off a sheet must add up to the sheet's own SUB-TOTAL row.

    Either the bidder's workbook does not add up, or this reader missed or misread a row; both
    must be said out loud. A reader that dropped the first positions of four sheets once looked
    exactly like a correct one, because nothing ever compared what it read with what the
    workbook itself states.
    """
    if sheet.subtotal is None:
        return None
    lines_sum = sheet.total
    diff = lines_sum - sheet.subtotal
    if abs(diff) <= max(_MONEY_TOL, abs(sheet.subtotal) * 1e-6):
        return None
    return _finding(
        "BOQ.RECONCILE",
        "error",
        f"{sheet.name}: პოზიციების ჯამი {lines_sum:,.2f}-ია, ხოლო გვერდის ქვეჯამი "
        f"({sheet.subtotal_row} სტრიქონი) — {sheet.subtotal:,.2f}; სხვაობა {diff:,.2f}",
        f"{sheet.name}: the positions add up to {lines_sum:,.2f}, but the sheet's SUB-TOTAL "
        f"row (row {sheet.subtotal_row}) states {sheet.subtotal:,.2f} - a difference of "
        f"{diff:,.2f}",
        expected=f"{sheet.subtotal:,.2f}",
        actual=f"{lines_sum:,.2f}",
    )


def compare_all(template: BoqWorkbook, bids: list[BoqWorkbook]) -> list[BidComparison]:
    return [compare_bid(template, b) for b in bids]


def summary_report(template: BoqWorkbook, comparisons: list[BidComparison]) -> CheckReport:
    findings = [f for c in comparisons for f in c.findings]
    return CheckReport(
        document_id=template.name,
        findings=findings,
        summary={c.bidder: f"{len(c.findings)} finding(s)" for c in comparisons},
    )


# --- unit rates ---------------------------------------------------------------------------


def rate_outliers(template: BoqWorkbook, comparisons: list[BidComparison]) -> list[Finding]:
    """Flag a unit rate that stands far apart from what the other bidders quoted for the line.

    Like-for-like comparison of the bottom line is misleading when one bidder prices a position
    an order of magnitude differently; this is what makes the difference visible per position
    rather than only in the total.
    """
    # Keyed by the template position (sheet, row), which is unique. The item-and-description
    # key is not: a template that repeats a position ("Excavation" under two sections) pooled
    # both positions' rates into one median.
    by_line: dict[tuple[str, int], list[tuple[str, BoqLine]]] = {}
    for comp in comparisons:
        for t_line, b_line in comp.matched:
            if b_line.rate_total:
                by_line.setdefault((t_line.sheet, t_line.row), []).append((comp.bidder, b_line))

    out: list[Finding] = []
    for entries in by_line.values():
        if len(entries) < 3:
            continue
        rates = sorted(line.rate_total or 0.0 for _, line in entries)
        median = rates[len(rates) // 2]
        if median <= 0:
            continue
        for bidder, line in entries:
            rate = line.rate_total or 0.0
            if rate > median * _RATE_OUTLIER_FACTOR or rate * _RATE_OUTLIER_FACTOR < median:
                direction_ka = "მაღალია" if rate > median else "დაბალია"
                direction_en = "above" if rate > median else "below"
                out.append(
                    _finding(
                        "BOQ.RATE_OUTLIER",
                        "warning",
                        f"{bidder}: {line.sheet}, სტრიქონი {line.row} — ერთეულის ფასი "
                        f"{rate:,.2f} მნიშვნელოვნად {direction_ka} მედიანაზე ({median:,.2f})",
                        f"{bidder}: {line.sheet} row {line.row} - unit rate {rate:,.2f} is far "
                        f"{direction_en} the median ({median:,.2f})",
                        expected=f"{median:,.2f}",
                        actual=f"{rate:,.2f}",
                        confidence=0.7,
                    )
                )
    return out


# --- merged workbook ----------------------------------------------------------------------

_HIGH = PatternFill("solid", fgColor="F4CCCC")
_LOW = PatternFill("solid", fgColor="D9EAD3")
_TEMPLATE_ROW = PatternFill("solid", fgColor="EFEFEF")
_HEAD = PatternFill("solid", fgColor="D0D7E5")
_BOLD = Font(bold=True)

_MERGED_HEADERS = (
    ("Item", 10),
    ("Description / აღწერა", 52),
    ("Unit", 8),
    ("Template qty", 12),
    ("Bidder", 14),
    ("Qty", 10),
    ("Δ qty", 9),
    ("Materials", 11),
    ("Labor", 11),
    ("Delivery", 11),
    ("Unit rate", 12),
    ("Amount", 14),
)


def _put(ws: object, row: int, col: int, value: object) -> None:
    """Write one cell of the merged workbook. Every value goes through here.

    Descriptions, units, item numbers, sheet names, Summary labels and finding messages all
    come from a workbook the bidder controls; see ``report/xlsx_safe`` for what an unescaped
    ``=`` in one of them would do in the file the buyer opens.
    """
    ws.cell(row, col, cell_safe(value))  # type: ignore[attr-defined]


def _sheet_title(name: str) -> str:
    """A template sheet name as a worksheet title: control characters out, 31 characters."""
    title = str(cell_safe(name)).removeprefix("'")[:31]
    return title or "Sheet"


def _style_header(ws: object, row: int = 1) -> None:
    for col, (title, width) in enumerate(_MERGED_HEADERS, start=1):
        cell = ws.cell(row, col, title)  # type: ignore[attr-defined]
        cell.font = _BOLD
        cell.fill = _HEAD
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = width  # type: ignore[attr-defined]
    ws.freeze_panes = ws.cell(row + 1, 1)  # type: ignore[attr-defined]


def _colour_extremes(ws: object, rows: list[int], col: int) -> None:
    """Highest red, lowest green - the client's last two BOQ rules."""
    values = [(ws.cell(r, col).value, r) for r in rows]  # type: ignore[attr-defined]
    numeric = [(v, r) for v, r in values if isinstance(v, int | float) and v]
    if len(numeric) < 2:
        return
    ws.cell(max(numeric)[1], col).fill = _HIGH  # type: ignore[attr-defined]
    ws.cell(min(numeric)[1], col).fill = _LOW  # type: ignore[attr-defined]


def write_merged_workbook(
    template: BoqWorkbook,
    bids: list[BoqWorkbook],
    comparisons: list[BidComparison],
    out_path: Path,
    *,
    outliers: list[Finding] | None = None,
) -> Path:
    """All bidders stacked vertically under every template line, plus Summary and reports.

    A new workbook rather than an edited copy of the template on purpose: inserting rows into
    the template would shift the ranges its own SUM formulas point at, which openpyxl does not
    rewrite, so the file would open with quietly wrong totals.
    """
    wb = Workbook()
    default_sheet = wb.active
    if default_sheet is not None:
        wb.remove(default_sheet)

    _write_summary_sheet(wb, template, bids)

    # Keyed by the template position, not by item-and-description: a template that repeats a
    # position collapsed both onto the last bidder line, and the first showed the second's price.
    # ``comparisons`` is ``compare_all(template, bids)``: one per bid, in the same order, so they
    # are paired by position - two uploads with the same file name are still two bidders.
    matched_by_bidder: list[dict[tuple[str, int], BoqLine]] = [
        {(t.sheet, t.row): b for t, b in c.matched} for c in comparisons
    ]
    if len(matched_by_bidder) != len(bids):
        msg = (
            f"{len(bids)} bids but {len(comparisons)} comparisons; pass compare_all(template, bids)"
        )
        raise ValueError(msg)

    for sheet_name, t_sheet in template.sheets.items():
        ws = wb.create_sheet(_sheet_title(sheet_name))
        _style_header(ws)
        row = 2
        for t_line in t_sheet.lines:
            _put(ws, row, 1, t_line.item_no)
            _put(ws, row, 2, t_line.description[:250])
            _put(ws, row, 3, t_line.unit)
            _put(ws, row, 4, t_line.quantity)
            _put(ws, row, 5, "— template —")
            for col in range(1, len(_MERGED_HEADERS) + 1):
                ws.cell(row, col).fill = _TEMPLATE_ROW
            row += 1

            bidder_rows: list[int] = []
            for bid, matched in zip(bids, matched_by_bidder, strict=True):
                line = matched.get((t_line.sheet, t_line.row))
                _put(ws, row, 5, bid.name)
                if line is not None:
                    delta = None
                    if line.quantity is not None and t_line.quantity is not None:
                        delta = round(line.quantity - t_line.quantity, 6) or None
                    _put(ws, row, 6, line.quantity)
                    _put(ws, row, 7, delta)
                    _put(ws, row, 8, line.rate_materials)
                    _put(ws, row, 9, line.rate_labor)
                    _put(ws, row, 10, line.rate_delivery)
                    _put(ws, row, 11, line.rate_total)
                    _put(ws, row, 12, line.amount)
                    if delta:
                        ws.cell(row, 7).fill = _HIGH
                else:
                    _put(ws, row, 6, "— missing —")
                    ws.cell(row, 6).fill = _HIGH
                bidder_rows.append(row)
                row += 1
            # amounts are comparable on every row that carries one, including the section
            # subtotals; unit rates only mean something on a line with a quantity
            _colour_extremes(ws, bidder_rows, 12)
            if t_line.quantity:
                _colour_extremes(ws, bidder_rows, 11)
        row += 1

    _write_report_sheets(wb, comparisons, outliers or [])
    wb.save(out_path)
    return out_path


def _write_summary_sheet(wb: Workbook, template: BoqWorkbook, bids: list[BoqWorkbook]) -> None:
    ws = wb.create_sheet("SUMMARY")
    ws.cell(1, 1, "Section / სექცია").font = _BOLD
    ws.cell(1, 2, "Bidder / კომპანია").font = _BOLD
    ws.cell(1, 3, "Total, USD").font = _BOLD
    for col, width in ((1, 46), (2, 18), (3, 16)):
        ws.column_dimensions[get_column_letter(col)].width = width
    for cell in ws[1]:
        cell.fill = _HEAD
    ws.freeze_panes = ws.cell(2, 1)

    labels: list[str] = []
    for bid in bids:
        for key in bid.summary:
            if key not in labels:
                labels.append(key)

    row = 2
    for label in labels:
        rows: list[int] = []
        for bid in bids:
            _put(ws, row, 1, label if not rows else None)
            _put(ws, row, 2, bid.name)
            _put(ws, row, 3, bid.summary.get(label))
            rows.append(row)
            row += 1
        if "total" in label.lower():
            for r in rows:
                ws.cell(r, 2).font = _BOLD
            _colour_extremes(ws, rows, 3)
        row += 1

    ws.cell(row, 1, "Line items recomputed (excl. overheads, profit, VAT)").font = _BOLD
    row += 1
    rows = []
    for bid in bids:
        _put(ws, row, 2, bid.name)
        _put(ws, row, 3, round(bid.total, 2))
        rows.append(row)
        row += 1
    _colour_extremes(ws, rows, 3)


def _write_report_sheets(
    wb: Workbook, comparisons: list[BidComparison], outliers: list[Finding]
) -> None:
    ws = wb.create_sheet("Quantity differences")
    for col, title in enumerate(
        ("Bidder", "Sheet", "Row", "Description", "Template qty", "Bid qty", "Kind"), 1
    ):
        ws.cell(1, col, title).font = _BOLD
        ws.cell(1, col).fill = _HEAD
    ws.column_dimensions["D"].width = 60
    row = 2
    for comp in comparisons:
        for kind, diffs in (
            ("quantity changed", comp.quantity_changed),
            ("filled (template blank)", comp.filled_blank),
        ):
            for d in diffs:
                for col, value in enumerate(
                    (
                        comp.bidder,
                        d.sheet,
                        d.row,
                        d.description[:200],
                        d.template_qty,
                        d.bid_qty,
                        kind,
                    ),
                    1,
                ):
                    _put(ws, row, col, value)
                row += 1

    ws = wb.create_sheet("Added and removed")
    for col, title in enumerate(("Bidder", "Kind", "Sheet", "Row", "Description"), 1):
        ws.cell(1, col, title).font = _BOLD
        ws.cell(1, col).fill = _HEAD
    ws.column_dimensions["E"].width = 70
    row = 2
    for comp in comparisons:
        for sheet in comp.added_sheets:
            for col, value in enumerate((comp.bidder, "added sheet", sheet, None, None), 1):
                _put(ws, row, col, value)
            row += 1
        for sheet in comp.removed_sheets:
            for col, value in enumerate((comp.bidder, "removed sheet", sheet, None, None), 1):
                _put(ws, row, col, value)
            row += 1
        for kind, lines in (("added line", comp.added), ("removed line", comp.removed)):
            for line in lines:
                cells: tuple[str | int | None, ...] = (
                    comp.bidder,
                    kind,
                    line.sheet,
                    line.row,
                    line.description[:200],
                )
                for col, value in enumerate(cells, 1):
                    _put(ws, row, col, value)
                row += 1

    ws = wb.create_sheet("Findings")
    for col, title in enumerate(("Bidder", "Severity", "Rule", "Message (EN)", "შეტყობინება"), 1):
        ws.cell(1, col, title).font = _BOLD
        ws.cell(1, col).fill = _HEAD
    for col, width in ((4, 80), (5, 80)):
        ws.column_dimensions[get_column_letter(col)].width = width
    row = 2
    rows: list[tuple[str, Finding]] = [(c.bidder, f) for c in comparisons for f in c.findings]
    # the outlier message names its bidder; the row is "all bidders" because the finding is
    # about how the quotes compare, not about one workbook
    rows.extend(("(all bidders)", f) for f in outliers)
    for bidder, f in rows:
        for col, value in enumerate((bidder, f.severity, f.rule_id, f.message_en, f.message_ka), 1):
            _put(ws, row, col, value)
        if f.severity == "error":
            ws.cell(row, 2).fill = _HIGH
        row += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare bidder BOQ workbooks to the template.")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--bids", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, default=Path("boq_merged.xlsx"))
    args = parser.parse_args(argv)

    template = read_boq(args.template, name="TEMPLATE")
    bids = [read_boq(p) for p in args.bids]
    comparisons = compare_all(template, bids)
    # Outliers compare the bidders with each other, so they belong to no one bidder. Filing
    # them under the first one (as this once did) charged that bidder with everyone's prices.
    outliers = rate_outliers(template, comparisons)

    for comp in comparisons:
        errors = sum(1 for f in comp.findings if f.severity == "error")
        print(f"{comp.bidder}: {len(comp.findings)} finding(s), {errors} error(s)")
    print(f"unit-rate outliers across bidders: {len(outliers)}")
    path = write_merged_workbook(template, bids, comparisons, args.out, outliers=outliers)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
