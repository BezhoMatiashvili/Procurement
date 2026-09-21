"""Phase-2a prototype: compare bidders' BOQ workbooks against the tender template.

Deterministic (no LLM). For every section sheet, lines are matched by (sheet, item number,
description). Reports quantity deviations, missing/added lines, and builds a comparison
workbook: a Summary sheet with every section per bidder (highest total red, lowest green) and a
Differences sheet.

    uv run python scripts/boq_compare.py --template "006-BID-26 - BOQ.xlsx" \\
        --bid "Company-1=Company-1.xlsx" --bid "Company-2=Company-2.xlsx" --out comparison.xlsx
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

SKIP_SHEETS = {"COVER PAGE", "INSTRUCTION TO BIDDERS", "SUMMARY"}
RED = PatternFill("solid", fgColor="F8CBAD")
GREEN = PatternFill("solid", fgColor="C6EFCE")
BOLD = Font(bold=True)

Key = tuple[str, str, str]


@dataclass(slots=True)
class Line:
    qty: float | None
    unit: str | None
    row: int


def _num(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def _header_cols(ws) -> dict[str, int]:  # type: ignore[no-untyped-def]
    cols: dict[str, int] = {}
    for row in ws.iter_rows(min_row=1, max_row=10):
        for c in row:
            v = str(c.value or "").strip().lower()
            if v.startswith("description") and "desc" not in cols:
                cols["desc"] = c.column
            elif v == "quantity":
                cols["qty"] = c.column
            elif v == "unit":
                cols["unit"] = c.column
    return cols


def read_lines(path: Path) -> dict[Key, Line]:
    wb = load_workbook(path, data_only=True)
    out: dict[Key, Line] = {}
    for ws in wb.worksheets:
        if ws.title in SKIP_SHEETS:
            continue
        cols = _header_cols(ws)
        if "qty" not in cols or "desc" not in cols:
            continue
        for r in range(1, ws.max_row + 1):
            desc = ws.cell(r, cols["desc"]).value
            if desc is None:
                continue
            no = ws.cell(r, 1).value
            sub = ws.cell(r, 2).value
            key = (
                ws.title,
                str(sub or no or "").strip(),
                re.sub(r"\s+", " ", str(desc)).strip()[:80],
            )
            out[key] = Line(_num(ws.cell(r, cols["qty"]).value), ws.cell(r, cols["unit"]).value, r)
    wb.close()
    return out


def read_summary(path: Path) -> dict[str, float | None]:
    """Section totals from the SUMMARY sheet: description -> TOTAL column value."""
    wb = load_workbook(path, data_only=True)
    ws = wb["SUMMARY"]
    total_col = None
    for row in ws.iter_rows(min_row=1, max_row=10):
        for c in row:
            if str(c.value or "").strip().upper() == "TOTAL":
                total_col = c.column
    out: dict[str, float | None] = {}
    if total_col:
        for r in range(1, ws.max_row + 1):
            item, desc = ws.cell(r, 1).value, ws.cell(r, 2).value
            if desc and item is not None and str(item).strip():
                out[f"{item} {str(desc).strip()}"] = _num(ws.cell(r, total_col).value)
    wb.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--bid", action="append", required=True, help="name=path.xlsx")
    ap.add_argument("--out", default="boq_comparison.xlsx")
    args = ap.parse_args()

    base = read_lines(Path(args.template))
    bids = {n: Path(p) for n, p in (b.split("=", 1) for b in args.bid)}
    print(f"template: {len(base)} lines")

    wb = WorkbookBuilder()
    for name, path in bids.items():
        lines = read_lines(path)
        missing = [k for k in base if k not in lines]
        added = [k for k in lines if k not in base]
        diffs = [
            (k, base[k].qty, lines[k].qty, lines[k].row)
            for k in base
            if k in lines and (base[k].qty or 0) != (lines[k].qty or 0)
        ]
        # template quantity 0/blank means "bidder fills in" (e.g. staffing in Preliminaries)
        real = [d for d in diffs if d[1]]
        fill_in = [d for d in diffs if not d[1]]
        print(
            f"\n{name}: {len(lines)} lines | changed quantities: {len(real)} | "
            f"filled where template blank: {len(fill_in)} | missing: {len(missing)} | "
            f"added: {len(added)}"
        )
        for k, a, b, r in real:
            print(f"   CHANGED {k[0]} [{k[1]}] row {r}: {a} -> {b}   {k[2][:50]}")
        for k in missing:
            print(f"   MISSING {k[0]} [{k[1]}] {k[2][:60]}")
        for k in added:
            print(f"   ADDED   {k[0]} [{k[1]}] row {lines[k].row}: {k[2][:60]}")
        wb.add_bid(name, read_summary(path), real, fill_in, missing, added)

    wb.save(Path(args.out))
    print(f"\ncomparison workbook written to {args.out}")


class WorkbookBuilder:
    def __init__(self) -> None:
        self.wb = Workbook()
        self.summary = self.wb.active
        self.summary.title = "Summary"
        self.diff = self.wb.create_sheet("Differences")
        self.diff.append(
            ["Bidder", "Kind", "Sheet", "Item", "Row", "Template qty", "Bidder qty", "Description"]
        )
        for c in self.diff[1]:
            c.font = BOLD
        self.sections: dict[str, dict[str, float | None]] = {}

    def add_bid(
        self, name: str, summary: dict[str, float | None], real, fill_in, missing, added
    ) -> None:  # type: ignore[no-untyped-def]
        self.sections[name] = summary
        for k, a, b, r in real:
            self.diff.append([name, "quantity changed", k[0], k[1], r, a, b, k[2]])
        for k, a, b, r in fill_in:
            self.diff.append([name, "filled (template blank)", k[0], k[1], r, a, b, k[2]])
        for k in missing:
            self.diff.append([name, "missing line", k[0], k[1], None, None, None, k[2]])
        for k in added:
            self.diff.append([name, "added line", k[0], k[1], None, None, None, k[2]])

    def save(self, path: Path) -> None:
        ws = self.summary
        names = list(self.sections)
        ws.append(["Section", *names])
        for c in ws[1]:
            c.font = BOLD
        rows = list(dict.fromkeys(k for s in self.sections.values() for k in s))
        for label in rows:
            vals = [self.sections[n].get(label) for n in names]
            ws.append([label, *vals])
            nums = [(v, i) for i, v in enumerate(vals) if v]
            if len(nums) >= 2 and ("TOTAL" in label.upper()):
                hi, lo = max(nums)[1], min(nums)[1]
                ws.cell(ws.max_row, hi + 2).fill = RED
                ws.cell(ws.max_row, lo + 2).fill = GREEN
        for i in range(1, len(names) + 2):
            ws.column_dimensions[get_column_letter(i)].width = 34 if i == 1 else 18
        for col in "BCDEFGH":
            self.diff.column_dimensions[col].width = 16
        self.diff.column_dimensions["H"].width = 60
        self.wb.save(path)


if __name__ == "__main__":
    main()
