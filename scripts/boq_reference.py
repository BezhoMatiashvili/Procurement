#!/usr/bin/env python3
"""Independent BOQ reference reader: writes the BOQ gold of docs/GOLD_SPEC.md section 5.

Reads a tender template and the bidders' workbooks with openpyxl only (each workbook twice:
cached values and formulas). It deliberately imports nothing from ``procurement_agent`` - the
point is a second, independent reading to score ``checks/boq.py`` against.

Usage (from the repo root):
    uv run --offline python scripts/boq_reference.py <dir with template + bids> --out <gold.json>
        [--template <path>] [--verbose]

The template is the one workbook whose positions carry no prices (or ``--template``); every
other ``.xlsx`` in the directory is a bid, named by its file stem. Nothing about a particular
project is hard-coded: sheets, columns, rows and Summary lines are found by their labels and
by the structure of the formulas.
"""

from __future__ import annotations

import argparse
import difflib
import itertools
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl
from openpyxl.utils import column_index_from_string

CENT = 0.005

# ----------------------------------------------------------------------------------- folding

#: Quote characters and dashes, by code point so the source stays plain ASCII punctuation.
_QUOTE_CHARS = (0x201E, 0x201C, 0x201D, 0x22, 0x27, 0x2018, 0x2019, 0xAB, 0xBB, 0x201A)
_QUOTES = dict.fromkeys(_QUOTE_CHARS, None)
_DASHES = dict.fromkeys((0x2013, 0x2014, 0x2011, 0x2212), "-")
_NBSP = chr(0xA0)


def fold(value: object) -> str:
    """GOLD_SPEC section 1: NFC, no quotes, one dash, one space, trimmed, Latin upper-cased."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFC", str(value)).translate(_QUOTES).translate(_DASHES)
    s = re.sub(r"\s+", " ", s.replace(chr(0xA0), " ")).strip()
    return "".join(
        ch.upper() if ch.isascii() or unicodedata.name(ch, "").startswith("LATIN") else ch
        for ch in s
    )


# ------------------------------------------------------------------------------ cell values


class Text(str):
    """Text found in a number cell: never read as 0."""


def number(value: object) -> float | Text | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    return Text(s)


def is_num(x: object) -> bool:
    return isinstance(x, float)


def nz(x: object) -> bool:
    return is_num(x) and abs(x) > 1e-9  # type: ignore[arg-type]


def same_money(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < CENT


def same_qty(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def r2(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


_REF = re.compile(
    r"(?:'[^']+'|[A-Za-z0-9_.]+)?!?\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?"
)
_CELL = re.compile(r"(?<![A-Za-z_'!])\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?(?![\w(])")


def refs_of(formula: str) -> list[tuple[int, int, int, int]]:
    """Same-sheet cell references in a formula as (col1, row1, col2, row2); strings removed."""
    body = re.sub(r'"[^"]*"', "", formula)
    if "!" in body:  # cross-sheet references are not same-sheet rows
        body = re.sub(
            r"(?:'[^']+'|[A-Za-z0-9_.]+)!\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?", "", body
        )
    out = []
    for m in _CELL.finditer(body):
        c1, r1 = column_index_from_string(m.group(1)), int(m.group(2))
        if m.group(3):
            c2, r2_ = column_index_from_string(m.group(3)), int(m.group(4))
        else:
            c2, r2_ = c1, r1
        out.append((c1, r1, c2, r2_))
    return out


def formula_of(cell: object) -> str | None:
    v = getattr(cell, "value", None)
    if isinstance(v, str) and v.startswith("="):
        return v
    if v is not None and type(v).__name__ == "ArrayFormula":
        return str(getattr(v, "text", "")) or None
    return None


def own_row_only(formula: str, row: int) -> bool:
    refs = refs_of(formula)
    return bool(refs) and all(r1 == row and r2_ == row for _, r1, _, r2_ in refs)


# ------------------------------------------------------------------------------ sheet model

LABELS = {
    "desc": {"DESCRIPTION"},
    "no": {"NO", "NO.", "ITEM", "#"},
    "qty": {"QUANTITY", "QTY", "QTY."},
    "unit": {"UNIT"},
    "mat": {"MATERIALS", "MATERIAL"},
    "lab": {"LABOUR", "LABOR"},
    "dlv": {"DELIVERY"},
    "mat_sum": {"MATERIALS SUM", "MATERIAL SUM"},
    "lab_sum": {"LABOUR SUM", "LABOR SUM"},
    "dlv_sum": {"DELIVERY SUM"},
}


def label_key(text: str) -> str | None:
    for key, names in LABELS.items():
        if text in names:
            return key
    if text.startswith("RATE IN"):
        return "rate"
    if text.startswith("AMOUNT"):
        return "amount"
    if text.startswith("NOTE"):
        return "notes"
    return None


@dataclass
class Row:
    row: int
    item: str
    desc: str
    qty: float | Text | None
    qty_formula: str | None
    unit: str
    mat: float | Text | None = None
    lab: float | Text | None = None
    dlv: float | Text | None = None
    rate: float | Text | None = None
    amount: float | Text | None = None
    amount_formula: str | None = None
    sum_formulas: tuple[str, ...] = ()
    kind: str = ""  # template only: position | subtotal | parent | heading
    numeric_item: bool = False

    @property
    def fdesc(self) -> str:
        return fold(self.desc)

    @property
    def has_content(self) -> bool:
        return bool(self.item or self.desc)


@dataclass
class Sheet:
    name: str
    cols: dict[str, int]
    header_row: int
    number_row: int
    subtotal_row: int | None
    subtotal_value: float | Text | None
    subtotal_formula: str | None
    rows: list[Row]
    below_subtotal: list[tuple[int, float]] = field(default_factory=list)


@dataclass
class SummaryInfo:
    sheet: str
    total_col: int
    rows: dict[str, tuple[int, float | Text | None]]  # role -> (row, value)
    notes: list[str]
    links: list[dict]
    checks: list[dict]


@dataclass
class Book:
    path: Path
    name: str
    sheet_names: list[str]
    sheets: dict[str, Sheet]
    summary: SummaryInfo | None
    uncached: int
    values: object
    formulas: object


def find_header(ws) -> tuple[int, int, dict[str, int]] | None:
    """(header row, column-number row, columns) of a priced sheet, or None."""
    max_scan = min(ws.max_row, 40)
    for hr in range(1, max_scan + 1):
        texts = {c: fold(ws.cell(hr, c).value) for c in range(1, ws.max_column + 1)}
        if "DESCRIPTION" not in texts.values() or not any(
            label_key(t) == "qty" for t in texts.values()
        ):
            continue
        # the column-number row: 1, 2, 3 ... left to right
        for nr in range(hr + 1, min(hr + 6, ws.max_row) + 1):
            seq = []
            for c in range(1, ws.max_column + 1):
                v = ws.cell(nr, c).value
                if (
                    isinstance(v, (int, float))
                    and not isinstance(v, bool)
                    and float(v).is_integer()
                ):
                    seq.append(int(v))
                elif isinstance(v, str) and v.strip().isdigit():
                    seq.append(int(v.strip()))
            if len(seq) >= 5 and seq[0] == 1 and all(b > a for a, b in itertools.pairwise(seq)):
                cols: dict[str, int] = {}
                for lr in range(hr, nr):
                    for c in range(1, ws.max_column + 1):
                        k = label_key(fold(ws.cell(lr, c).value))
                        if k and k not in cols:
                            cols[k] = c
                if {"desc", "qty", "amount"} <= cols.keys():
                    return hr, nr, cols
                return None
        return None
    return None


def cell_text(ws, r: int, c: int | None) -> str:
    if c is None:
        return ""
    v = ws.cell(r, c).value
    if v is None:
        return ""
    return str(v).strip()


def read_sheet(ws_v, ws_f, name: str, hdr) -> Sheet:
    hr, nr, cols = hdr
    desc_c = cols["desc"]
    no_c = cols.get("no", 1)
    item_cols = list(range(no_c, desc_c)) or [1]
    labelled = set(cols.values())
    ka_c = desc_c + 1 if desc_c + 1 not in labelled else None

    def val(r, key):
        c = cols.get(key)
        return number(ws_v.cell(r, c).value) if c else None

    rows: list[Row] = []
    subtotal_rows: list[int] = []
    last = ws_v.max_row
    for r in range(nr + 1, last + 1):
        texts = [
            fold(ws_v.cell(r, c).value) for c in range(1, min(ws_v.max_column, desc_c + 2) + 1)
        ]
        if any(t.startswith("SUB-TOTAL") or t.startswith("SUBTOTAL") for t in texts):
            subtotal_rows.append(r)
    end = subtotal_rows[-1] if subtotal_rows else last + 1
    for r in range(nr + 1, end):
        item, numeric_item = "", False
        for c in item_cols:
            v = ws_v.cell(r, c).value
            if v is not None and str(v).strip():
                numeric_item = isinstance(v, (int, float)) and not isinstance(v, bool)
                item = str(v).strip()
                break
        desc = cell_text(ws_v, r, desc_c) or cell_text(ws_v, r, ka_c)
        qc = cols["qty"]
        row = Row(
            row=r,
            item=item,
            desc=desc,
            qty=number(ws_v.cell(r, qc).value),
            qty_formula=formula_of(ws_f.cell(r, qc)),
            unit=cell_text(ws_v, r, cols.get("unit")),
            mat=val(r, "mat"),
            lab=val(r, "lab"),
            dlv=val(r, "dlv"),
            rate=val(r, "rate"),
            amount=val(r, "amount"),
            amount_formula=formula_of(ws_f.cell(r, cols["amount"])),
            sum_formulas=tuple(
                f
                for k in ("mat_sum", "lab_sum", "dlv_sum")
                if k in cols
                for f in [formula_of(ws_f.cell(r, cols[k]))]
                if f
            ),
            numeric_item=numeric_item,
        )
        if isinstance(row.qty, Text) and not str(row.qty).strip():
            row.qty = None
        empty = not (
            row.item
            or row.desc
            or row.unit
            or row.qty is not None
            or nz(row.amount)
            or isinstance(row.amount, Text)
        )
        if not empty:
            rows.append(row)
    st_row = subtotal_rows[-1] if subtotal_rows else None
    below = []
    if st_row:
        for r in range(st_row + 1, last + 1):
            a = number(ws_v.cell(r, cols["amount"]).value)
            if nz(a):
                below.append((r, a))
    return Sheet(
        name=name,
        cols=cols,
        header_row=hr,
        number_row=nr,
        subtotal_row=st_row,
        subtotal_value=number(ws_v.cell(st_row, cols["amount"]).value) if st_row else None,
        subtotal_formula=formula_of(ws_f.cell(st_row, cols["amount"])) if st_row else None,
        rows=rows,
        below_subtotal=below,
    )


def classify_template(sheet: Sheet) -> None:
    items = [r.item for r in sheet.rows if r.item]
    for r in sheet.rows:
        f = r.amount_formula
        sums_other = any(not own_row_only(sf, r.row) for sf in r.sum_formulas if refs_of(sf))
        if f is not None:
            refs = refs_of(f)
            if refs and all(r1 == r.row and r2_ == r.row for _, r1, _, r2_ in refs):
                # spec section 5: an amount that is a product of its own row makes a position, even
                # when the row's SUM columns also add the rows under it
                r.kind = "position"
            else:
                r.kind = "subtotal"
        elif is_num(r.amount):
            r.kind = "position"
        elif r.qty is not None or r.unit:
            qty_over_rows = (
                bool(r.qty_formula)
                and not own_row_only(r.qty_formula, r.row)
                and bool(
                    refs_of(r.qty_formula or "")
                    and any(r1 != r.row for _, r1, _, _ in refs_of(r.qty_formula or ""))
                )
            )
            children = bool(r.item) and any(i.startswith(r.item + ".") for i in items)
            r.kind = "parent" if (qty_over_rows or children or sums_other) else "position"
        else:
            r.kind = "heading"


# ---------------------------------------------------------------------------- summary sheet

#: GOLD_SPEC 5: the Summary's rows by their usual labels (folded: Latin upper-cased). The tax by
#: its names, never the letters inside a word ("PRIVATE").
_TAX = (
    r"(?<![A-Zა-ჰ])(?:VAT|VALUE[ -]ADDED[ -]TAX|დღგ|"
    r"დამატებული ღირებულების გადასახად[ა-ჰ]*)(?![A-Zა-ჰ])"
)
_PCT = r"(?:\d{1,2}(?:[.,]\d+)?\s*%\s*)?"
INCL_VAT = re.compile(
    rf"\b(?:INCL(?:\.|UDING|USIVE)?|WITH)\s*(?:OF )?{_PCT}{_TAX}|{_TAX} INCL"
    r"|(?:დღგ(?:-?ის|-?ს)?|გადასახადის)\s*ჩათვლით"
)
EXCL_VAT = re.compile(
    rf"\b(?:EXCL(?:\.|UDING|USIVE)?|WITHOUT|BEFORE|NET OF)\s*(?:OF )?{_PCT}{_TAX}"
    r"|(?:დღგ(?:-?ის|-?ს)?|გადასახადის)\s*გარეშე"
)
VAT_ROW = re.compile(rf"(?:ADD )?{_PCT}{_TAX}")


def read_summary(ws_v, ws_f, sheet_names: list[str], priced: set[str]) -> SummaryInfo | None:
    total_col = desc_col = None
    total_row = 0
    for r in range(1, min(ws_v.max_row, 20) + 1):
        for c in range(1, ws_v.max_column + 1):
            t = fold(ws_v.cell(r, c).value)
            if t == "TOTAL" and total_col is None:
                total_col, total_row = c, r
            if t == "DESCRIPTION" and desc_col is None:
                desc_col = c
    if total_col is None:
        return None
    roles: dict[str, tuple[int, float | Text | None]] = {}
    notes: list[str] = []
    links: list[dict] = []
    label_of: dict[int, str] = {}
    for r in range(total_row + 1, ws_v.max_row + 1):
        label = ""
        for c in ([desc_col] if desc_col else []) + list(range(1, total_col)):
            t = fold(ws_v.cell(r, c).value)
            if t and not re.fullmatch(r"[A-Z]?\d+(\.\d+)*", t):
                label = t
                break
        label_of[r] = label
        v = number(ws_v.cell(r, total_col).value)
        role = None
        if label.startswith("TOTAL COST"):
            role = "total_cost"
        elif "OVERHEAD" in label:
            role = "overheads"
        elif "PROFIT" in label:
            role = "profit"
        elif EXCL_VAT.search(label):
            role = "excl_vat"
        elif INCL_VAT.search(label):
            role = "incl_vat"
        elif VAT_ROW.match(label):
            role = "vat"
        if role:
            if role in roles:
                notes.append(f"Summary: second row labelled like {role} at row {r} ignored")
            else:
                roles[role] = (r, v)
        # links to a priced sheet's row
        linked = []
        for c in range(1, total_col):
            f = formula_of(ws_f.cell(r, c))
            if not f:
                continue
            for m in re.finditer(r"(?:'([^']+)'|([A-Za-z0-9_.]+))!\$?([A-Z]{1,3})\$?(\d+)", f):
                sh = m.group(1) or m.group(2)
                if sh in sheet_names:
                    linked.append((sh, int(m.group(4))))
        if linked:
            sh_rows = sorted(set(linked))
            links.append(
                {
                    "row": r,
                    "label": label,
                    "refs": [list(x) for x in sh_rows],
                    "stated": v if is_num(v) else None,
                }
            )
    checks: list[dict] = []

    def gv(role):
        x = roles.get(role)
        return x[1] if x and is_num(x[1]) else None

    tc_row = roles.get("total_cost", (None, None))[0]
    if tc_row:
        f = formula_of(ws_f.cell(tc_row, total_col)) or ""
        top = sorted({r1 for c1, r1, c2, r2_ in refs_of(f) for r1 in range(r1, r2_ + 1)})
        vals = [number(ws_v.cell(r, total_col).value) for r in top]
        s = math.fsum(x for x in vals if is_num(x))
        checks.append(
            {
                "check": "total_cost = sum of top-level sections",
                "rows": top,
                "expected": r2(s),
                "stated": r2(gv("total_cost")),
                "ok": same_money(s, gv("total_cost")),
            }
        )
    tc, oh, pr, ex, vat, inc = (
        gv(k) for k in ("total_cost", "overheads", "profit", "excl_vat", "vat", "incl_vat")
    )
    if None not in (tc, ex):
        exp = math.fsum([tc, oh or 0.0, pr or 0.0])
        checks.append(
            {
                "check": "excl_vat = total_cost + overheads + profit",
                "expected": r2(exp),
                "stated": r2(ex),
                "ok": same_money(exp, ex),
            }
        )
    if None not in (ex, inc):
        exp = math.fsum([ex, vat or 0.0])
        checks.append(
            {
                "check": "incl_vat = excl_vat + vat",
                "expected": r2(exp),
                "stated": r2(inc),
                "ok": same_money(exp, inc),
            }
        )
    return SummaryInfo(
        sheet="", total_col=total_col, rows=roles, notes=notes, links=links, checks=checks
    )


# ------------------------------------------------------------------------------------ books


def read_book(path: Path, name: str) -> Book:
    wv = openpyxl.load_workbook(path, data_only=True)
    wf = openpyxl.load_workbook(path, data_only=False)
    sheets: dict[str, Sheet] = {}
    uncached = 0
    for ws in wf.worksheets:
        wsv = wv[ws.title]
        for row in ws.iter_rows():
            for cell in row:
                if formula_of(cell) and wsv[cell.coordinate].value is None:
                    uncached += 1
        hdr = find_header(wsv)
        if hdr:
            sheets[ws.title] = read_sheet(wsv, ws, ws.title, hdr)
    names = [ws.title for ws in wf.worksheets]
    summary = None
    cands = [n for n in names if fold(n) == "SUMMARY"] or [n for n in names if "SUMMARY" in fold(n)]
    if not cands:  # the sheet that links to the most priced sheets
        best = (0, None)
        for n in names:
            if n in sheets:
                continue
            txt = " ".join(str(c.value) for row in wf[n].iter_rows() for c in row if formula_of(c))
            k = sum(1 for s in sheets if s in txt)
            best = max(best, (k, n), key=lambda x: x[0])
        cands = [best[1]] if best[1] else []
    if cands:
        summary = read_summary(wv[cands[0]], wf[cands[0]], names, set(sheets))
        if summary:
            summary.sheet = cands[0]
    return Book(
        path=path,
        name=name,
        sheet_names=names,
        sheets=sheets,
        summary=summary,
        uncached=uncached,
        values=wv,
        formulas=wf,
    )


def unpriced_template(book: Book) -> bool:
    for sh in book.sheets.values():
        for r in sh.rows:
            if nz(r.amount) or nz(r.rate) or nz(r.mat) or nz(r.lab) or nz(r.dlv):
                return False
    return True


# --------------------------------------------------------------------------------- matching


def match_rows(t_rows: list[Row], b_rows: list[Row]) -> tuple[dict[int, Row], dict[int, float]]:
    """template row number -> bid row, one to one; plus the similarity of re-worded matches."""
    out: dict[int, Row] = {}
    ratio: dict[int, float] = {}
    used: set[int] = set()
    cand_t = [t for t in t_rows if t.has_content]
    cand_b = [b for b in b_rows if b.has_content]

    def assign(pairs):
        for t, b, rt in sorted(pairs, key=lambda x: (-x[2], abs(x[0].row - x[1].row))):
            if t.row in out or b.row in used:
                continue
            out[t.row] = b
            used.add(b.row)
            if rt < 1.0:
                ratio[t.row] = rt

    # 1. item number + description
    assign([(t, b, 1.0) for t in cand_t for b in cand_b if t.item == b.item and t.fdesc == b.fdesc])
    # 2. same item number, descriptions >= 0.9 alike
    pairs = []
    for t in cand_t:
        if t.row in out or not t.item:
            continue
        for b in cand_b:
            if b.row in used or b.item != t.item:
                continue
            rt = difflib.SequenceMatcher(None, t.fdesc, b.fdesc).ratio()
            if rt >= 0.9:
                pairs.append((t, b, rt))
    assign(pairs)
    return out, ratio


# ------------------------------------------------------------------------------- bid check


def unit_rate(b: Row, has_rate_col: bool) -> float | Text | None:
    if isinstance(b.rate, Text):
        return b.rate
    if is_num(b.rate):
        return b.rate
    parts = [b.mat, b.lab, b.dlv]
    for p in parts:
        if isinstance(p, Text):
            return p
    if any(is_num(p) for p in parts):
        return math.fsum(p for p in parts if is_num(p))
    if not has_rate_col and is_num(b.amount) and nz(b.qty):
        return b.amount / b.qty  # type: ignore[operator]
    return None


def expand_subtotal(ws_f, ws_v, col: int, formula: str, row: int, depth: int = 0) -> list[int]:
    """Rows whose amount a subtotal formula finally adds (leaves), same column, same sheet."""
    out: list[int] = []
    if depth > 6:
        return out
    for c1, r1, c2, r2_ in refs_of(formula):
        if not (c1 <= col <= c2):
            continue
        for rr in range(r1, r2_ + 1):
            if rr == row:
                continue
            f = formula_of(ws_f.cell(rr, col))
            if f and not own_row_only(f, rr) and refs_of(f):
                out.extend(expand_subtotal(ws_f, ws_v, col, f, rr, depth + 1))
            else:
                out.append(rr)
    return out


def section_groups(sheet: Sheet) -> dict[int, str]:
    """GOLD_SPEC 5, section groups: a section row that is itself a position ("A8") and the
    positions numbered under it ("8.1", "8.2") are one group; any other position is its own."""
    groups: dict[int, str] = {}
    section: tuple[str, str] | None = None
    for r in sheet.rows:
        if r.kind != "position":
            continue
        item = (r.item or "").strip()
        m = re.fullmatch(r"[A-Z](\d+)", item)
        if m:
            section = (item, m.group(1))
            groups[r.row] = item
        elif section is not None and item.startswith(section[1] + "."):
            groups[r.row] = section[0]
        else:
            groups[r.row] = f"r{r.row}"
    return groups


def check_bid(tpl: Book, bid: Book, placeholders: set[tuple[str, int]] | None = None) -> dict:
    placeholders = placeholders or set()
    res: dict = {
        "path": str(bid.path),
        "evaluable": bid.uncached == 0,
        "quantity_changed": [],
        "unit_changed": [],
        "unit_filled": [],
        "added": [],
        "removed": [],
        "filled_blank": [],
        "sheets_added": [n for n in bid.sheet_names if n not in tpl.sheet_names],
        "sheets_removed": [n for n in tpl.sheet_names if n not in bid.sheet_names],
        "unpriced_positions": [],
        "unfilled_positions": [],
        "description_changed": [],
        "heading_priced": [],
        "non_numeric": [],
        "numeric_item_numbers": [],
        "sheet_amounts": {},
        "sheet_subtotals": {},
        "positions_priced": [],
        "position_costs": {},
        "priced_optional": [],
        "groups_priced": [],
    }
    if bid.uncached:
        res["no_cached_values"] = bid.uncached
    notes: list[str] = []
    costs: dict[str, float] = {}
    priced: list[str] = []
    for sname, tsh in tpl.sheets.items():
        bsh = bid.sheets.get(sname)
        if bsh is None:
            if sname in bid.sheet_names:
                notes.append(f"{sname}: sheet present but its header block was not recognised")
            continue
        has_rate = "rate" in bsh.cols or any(k in bsh.cols for k in ("mat", "lab", "dlv"))
        m, ratio = match_rows(tsh.rows, bsh.rows)
        matched_b = {b.row for b in m.values()}
        leaf_rows: dict[int, float] = {}
        for t in tsh.rows:
            b = m.get(t.row)
            if t.kind != "position":
                if (
                    b is not None
                    and t.kind in ("heading", "parent")
                    and (nz(b.amount) or nz(b.rate))
                ):
                    res["heading_priced"].append(
                        {
                            "sheet": sname,
                            "item": t.item,
                            "row": b.row,
                            "amount": r2(b.amount) if is_num(b.amount) else None,
                        }
                    )
                continue
            key = f"{sname}|{t.row}"
            if b is None:
                res["removed"].append({"sheet": sname, "item": t.item, "row": t.row})
                continue
            if b.numeric_item:
                res["numeric_item_numbers"].append({"sheet": sname, "row": b.row, "item": b.item})
            if t.row in ratio:
                res["description_changed"].append(
                    {
                        "sheet": sname,
                        "item": t.item,
                        "row": b.row,
                        "similarity": round(ratio[t.row], 3),
                    }
                )
            if is_num(b.amount):
                leaf_rows[b.row] = b.amount  # type: ignore[assignment]
            if not t.unit and b.unit:
                res["unit_filled"].append({"sheet": sname, "item": t.item, "bid_unit": b.unit})
            elif fold(t.unit) != fold(b.unit):
                res["unit_changed"].append(
                    {"sheet": sname, "item": t.item, "template_unit": t.unit, "bid_unit": b.unit}
                )
            for col_key, v in (
                ("qty", b.qty),
                ("rate", b.rate),
                ("amount", b.amount),
                ("materials", b.mat),
                ("labour", b.lab),
                ("delivery", b.dlv),
            ):
                if isinstance(v, Text):
                    res["non_numeric"].append(
                        {
                            "sheet": sname,
                            "item": t.item,
                            "row": b.row,
                            "column": col_key,
                            "text": str(v),
                        }
                    )
            r = unit_rate(b, has_rate)
            if (sname, t.row) in placeholders:
                # GOLD_SPEC 5: optional; priced, it is the bidder's own additional work - kept out
                # of the evaluated cost and the common scope like an added line
                if nz(b.amount) or (not isinstance(r, Text) and nz(r)):
                    res["priced_optional"].append(
                        {
                            "sheet": sname,
                            "item": t.item,
                            "row": b.row,
                            "amount": r2(b.amount) if is_num(b.amount) else None,
                        }
                    )
                costs[key] = 0.0
                continue
            tq = t.qty if is_num(t.qty) else None
            blank_tpl = tq is None or abs(tq) < 1e-12
            if blank_tpl:
                if nz(b.qty) or nz(b.amount):
                    amt = b.amount if is_num(b.amount) else 0.0
                    res["filled_blank"].append(
                        {
                            "sheet": sname,
                            "item": t.item,
                            "bid_qty": b.qty if is_num(b.qty) else None,
                            "amount": r2(amt),
                        }
                    )
                    costs[key] = amt  # type: ignore[assignment]
                    if nz(amt) and not isinstance(r, Text):
                        priced.append(key)
                else:
                    res["unfilled_positions"].append({"sheet": sname, "item": t.item})
                continue
            if isinstance(b.qty, Text):
                pass
            elif not is_num(b.qty) or not same_qty(tq, b.qty):  # type: ignore[arg-type]
                res["quantity_changed"].append(
                    {
                        "sheet": sname,
                        "item": t.item,
                        "template_qty": tq,
                        "bid_qty": b.qty if is_num(b.qty) else None,
                    }
                )
            if isinstance(r, Text) or not nz(r):
                res["unpriced_positions"].append({"sheet": sname, "item": t.item, "row": t.row})
                costs[key] = 0.0
                continue
            costs[key] = tq * r  # type: ignore[operator]
            priced.append(key)
        # added lines: bid rows carrying something that match no template row
        for b in bsh.rows:
            if b.row in matched_b or not b.has_content:
                continue
            if b.qty is None and not b.unit and not nz(b.amount) and not nz(b.rate):
                notes.append(
                    f"{sname}: bid row {b.row} ({b.item or '-'}) is text only and matches no "
                    "template row"
                )
                continue
            own = bool(b.amount_formula) and own_row_only(b.amount_formula or "", b.row)
            res["added"].append(
                {
                    "sheet": sname,
                    "item": b.item,
                    "row": b.row,
                    "qty": b.qty if is_num(b.qty) else None,
                    "unit": b.unit,
                    "amount": r2(b.amount) if is_num(b.amount) else None,
                }
            )
            if is_num(b.amount) and (own or not b.amount_formula):
                leaf_rows[b.row] = b.amount  # type: ignore[assignment]
        # sheet amounts vs the sheet's own SUB-TOTAL
        amt = math.fsum(leaf_rows.values())
        res["sheet_amounts"][sname] = round(amt, 2)
        st = bsh.subtotal_value if is_num(bsh.subtotal_value) else None
        res["sheet_subtotals"][sname] = r2(st)
        if st is None:
            notes.append(f"{sname}: no numeric SUB-TOTAL amount (row {bsh.subtotal_row})")
        elif not same_money(amt, st):
            notes.append(explain_gap(bid, bsh, leaf_rows, amt, st, res["heading_priced"], sname))
        for rr, a in bsh.below_subtotal:
            notes.append(f"{sname}: amount {a:.2f} below the SUB-TOTAL at bid row {rr} (not read)")
    # GOLD_SPEC 5, section groups: a group is priced when any member is; an unpriced member of a
    # priced group is not listed as unpriced
    group_of = {
        f"{sn}|{row}": f"{sn}|{g}"
        for sn, tsh in tpl.sheets.items()
        for row, g in section_groups(tsh).items()
    }
    groups_priced = sorted({group_of[k] for k in priced if k in group_of})
    res["groups_priced"] = groups_priced
    res["unpriced_positions"] = [
        u
        for u in res["unpriced_positions"]
        if group_of.get(f"{u['sheet']}|{u['row']}") not in groups_priced
    ]
    res["unpriced_sheets"] = [
        sname
        for sname, tsh in tpl.sheets.items()
        if sname in bid.sheets
        and any(t.kind == "position" for t in tsh.rows)
        and not any(k.split("|")[0] == sname for k in priced)
        and not any(h["sheet"] == sname for h in res["heading_priced"])
    ]
    res["position_costs"] = {k: round(v, 6) for k, v in costs.items()}
    res["positions_priced"] = priced
    res["evaluated_cost"] = round(math.fsum(costs.values()), 2)
    # Summary
    s = bid.summary
    stated = {"total_cost": None, "excl_vat": None, "incl_vat": None}
    if s:
        for k in stated:
            x = s.rows.get(k)
            stated[k] = r2(x[1]) if x and is_num(x[1]) else None
        res["summary_checks"] = s.checks
        notes += list(s.notes)
        mism = []
        for ln in s.links:
            vals = []
            for sh, rr in ln["refs"]:
                bsh = bid.sheets.get(sh)
                if bsh is None:
                    continue
                v = number(bid.values[sh].cell(rr, bsh.cols["amount"]).value)
                vals.append(v if is_num(v) else 0.0)
            if vals and ln["stated"] is not None and not same_money(math.fsum(vals), ln["stated"]):
                mism.append(
                    {
                        "summary_row": ln["row"],
                        "refs": ln["refs"],
                        "stated": r2(ln["stated"]),
                        "sheet_amount": round(math.fsum(vals), 2),
                    }
                )
        res["summary_vs_sheets"] = mism
        for c in s.checks:
            if not c["ok"]:
                notes.append(
                    f"Summary: {c['check']} fails (expected {c['expected']}, stated {c['stated']})"
                )
        if mism:
            notes.append(
                f"Summary: {len(mism)} row(s) state a value other than the linked sheet row's "
                "amount (the Summary links to the materials/labour/delivery sum columns)"
            )
    else:
        notes.append("no Summary sheet found")
    res["stated"] = stated
    mf = (
        (stated["incl_vat"] / stated["total_cost"])
        if stated["incl_vat"] and stated["total_cost"]
        else None
    )
    if s and s.rows.get("incl_vat") and s.rows.get("total_cost"):
        a, b = s.rows["incl_vat"][1], s.rows["total_cost"][1]
        mf = a / b if is_num(a) and nz(b) else None  # full precision
    res["markup_factor"] = round(mf, 6) if mf else None
    res["evaluated_total"] = round(math.fsum(costs.values()) * mf, 2) if mf else None
    res["notes"] = notes
    return res


def explain_gap(
    bid: Book,
    bsh: Sheet,
    leaf_rows: dict[int, float],
    amt: float,
    st: float,
    heading_priced: list,
    sname: str,
) -> str:
    ws_f = bid.formulas[sname]
    ws_v = bid.values[sname]
    col = bsh.cols["amount"]
    f = bsh.subtotal_formula
    base = (
        f"{sname}: sheet_amounts {amt:.2f} != SUB-TOTAL {st:.2f} (row {bsh.subtotal_row}, "
        f"diff {st - amt:+.2f}). "
    )
    if not f:
        return base + "The SUB-TOTAL cell is a typed number, not a formula: bidder arithmetic."
    covered = expand_subtotal(ws_f, ws_v, col, f, bsh.subtotal_row or 0)
    cov = set(covered)
    dup = sorted({r for r in covered if covered.count(r) > 1})
    missing = {r: a for r, a in leaf_rows.items() if r not in cov and abs(a) > 1e-9}
    extra = {}
    for r in cov - set(leaf_rows):
        v = number(ws_v.cell(r, col).value)
        if nz(v):
            extra[r] = v
    parts = []
    if missing:
        parts.append(
            "the SUB-TOTAL formula leaves out bid rows "
            + ", ".join(f"{r} ({a:.2f})" for r, a in sorted(missing.items()))
        )
    if extra:
        parts.append(
            "it adds rows that are not positions "
            + ", ".join(f"{r} ({a:.2f})" for r, a in sorted(extra.items()))
        )
    if dup:
        parts.append(f"it counts rows {dup} twice")
    explained = (
        math.fsum(extra.values())
        - math.fsum(missing.values())
        + math.fsum(
            (number(ws_v.cell(r, col).value) or 0.0) * (covered.count(r) - 1)
            for r in dup
            if is_num(number(ws_v.cell(r, col).value))
        )
    )
    if parts and same_money(amt + explained, st):
        return base + "; ".join(parts) + " - fully explains the gap: bidder's own sheet arithmetic."
    return (
        base
        + ("; ".join(parts) + " - " if parts else "")
        + (
            f"UNEXPLAINED by the subtotal formula ({f[:60]}): reader or cached-value problem, "
            "investigate."
        )
    )


# ----------------------------------------------------------------------------- whole set


def build_gold(folder: Path, template: Path | None, verbose: bool = False) -> dict:
    files = sorted(p for p in folder.glob("*.xlsx") if not p.name.startswith("~$"))
    books = {p: read_book(p, p.stem) for p in files}
    if template is None:
        cands = [p for p, b in books.items() if b.sheets and unpriced_template(b)]
        if len(cands) != 1:
            raise SystemExit(
                f"cannot tell the template apart ({len(cands)} unpriced workbooks); use --template"
            )
        template = cands[0]
    template = next(p for p in books if p.resolve() == template.resolve())
    tpl = books.pop(template)
    for sh in tpl.sheets.values():
        classify_template(sh)
    # placeholder positions: blank template quantity under an "any other items" heading, or a row
    # that itself invites the bidder to list extras
    # ("Other costs if necessary (...provide description)")
    positions: dict[str, list] = {}
    for sname, sh in tpl.sheets.items():
        heading = ""
        out = []
        for r in sh.rows:
            if r.kind in ("heading", "subtotal", "parent"):
                heading = r.fdesc
            if r.kind != "position":
                continue
            p = {
                "row": r.row,
                "item": r.item,
                "qty": r.qty if is_num(r.qty) else None,
                "unit": r.unit,
                "description": r.desc,
            }
            if not r.amount_formula and not is_num(r.amount):
                p["missing_amount_formula"] = True
            # descriptive only (the quantity may be blank or 1): classification is unchanged
            if any(k in heading for k in PLACEHOLDER_HEADINGS) or any(
                k in r.fdesc for k in PLACEHOLDER_ROWS
            ):
                p["placeholder"] = True
            out.append(p)
        positions[sname] = out
    gold: dict = {
        "template": str(template),
        "positions": positions,
        "template_rows": {
            s: {
                k: [r.row for r in sh.rows if r.kind == k]
                for k in ("subtotal", "parent", "heading")
            }
            for s, sh in tpl.sheets.items()
        },
        "template_subtotal_rows": {s: sh.subtotal_row for s, sh in tpl.sheets.items()},
        "bids": {},
    }
    placeholder_rows = {
        (s, p["row"]) for s, ps in positions.items() for p in ps if p.get("placeholder")
    }
    for b in books.values():
        gold["bids"][b.name] = check_bid(tpl, b, placeholder_rows)
    # common scope over evaluable bids, taken over section groups (GOLD_SPEC 5)
    ev = {n: g for n, g in gold["bids"].items() if g["evaluable"]}
    group_of = {
        f"{sn}|{row}": f"{sn}|{g}"
        for sn, tsh in tpl.sheets.items()
        for row, g in section_groups(tsh).items()
    }
    all_keys = [
        f"{s}|{p['row']}"
        for s, ps in positions.items()
        for p in ps
        if (s, p["row"]) not in placeholder_rows
    ]
    common = [
        k for k in all_keys if ev and all(group_of[k] in g["groups_priced"] for g in ev.values())
    ]
    gold["common_scope"] = [
        {
            "sheet": k.split("|")[0],
            "item": next(
                p["item"] for p in positions[k.split("|")[0]] if p["row"] == int(k.split("|")[1])
            ),
            "row": int(k.split("|")[1]),
        }
        for k in common
    ]
    for n, g in gold["bids"].items():
        if n not in ev:
            g["common_scope_cost"] = g["common_scope_total"] = None
            continue
        c = math.fsum(g["position_costs"].get(k, 0.0) for k in common)
        g["common_scope_cost"] = round(c, 2)
        g["common_scope_total"] = round(c * g["markup_factor"], 2) if g["markup_factor"] else None
    totals = {
        n: g["common_scope_total"] for n, g in ev.items() if g.get("common_scope_total") is not None
    }
    colours: dict = {"red": [], "green": []}
    cents = {n: round(v, 2) for n, v in totals.items()}
    if len(cents) >= 2 and len(set(cents.values())) >= 2:
        hi, lo = max(cents.values()), min(cents.values())
        colours["red"] = sorted(n for n, v in cents.items() if v == hi)
        colours["green"] = sorted(n for n, v in cents.items() if v == lo)
    gold["colours"] = colours
    gold["notes"] = [f"{n}: {x}" for n, g in gold["bids"].items() for x in g["notes"]]
    gold["decisions"] = DECISIONS
    return gold


#: a heading inviting the bidder's own items, in either language ("აღწერაში არ შეტანილი" -
#: not in the description)
PLACEHOLDER_HEADINGS = ("UNDESCRIBED", "ANY OTHER", "აღწერაში არ შეტანილ")
PLACEHOLDER_ROWS = ("ANY OTHER ITEMS", "TENDER IS TO LIST", "OTHER COSTS IF NECESSARY")

DECISIONS = [
    "A template row is a position when its amount cell is a formula over its own row only, or a "
    "typed number; a formula over other rows makes it a subtotal.",
    "A row whose amount is a product of its own row is a position even when its own "
    "materials/labour/delivery SUM columns add the rows under it (spec section 5, literally: a "
    "sub-heading the template also gave a quantity, unit and rate). Its SUM columns only make a "
    "row without an amount cell a parent.",
    "A row with no amount cell but a quantity or unit is a position (missing amount formula), "
    "unless "
    "its quantity is a formula over other rows or other items are numbered under it (a parent).",
    "The item number is the first non-empty cell between the No column and the Description column.",
    "Positions match on item number + folded description, then same item number with "
    "difflib ratio >= 0.9 (description_changed); one to one, nearest row first.",
    "A blank bid unit against a template unit is a unit change with bid_unit ''; a unit the bidder "
    "wrote where the template left it blank is listed under unit_filled, not as a change.",
    "A template quantity of blank or 0 makes the position 'filled_blank' when the bidder gives a "
    "quantity or amount (cost = the bidder's amount), else 'unfilled_positions'; never a "
    "quantity change.",
    "A bid quantity left blank where the template has one is a quantity change with bid_qty null.",
    'Placeholder rows (GOLD_SPEC 5) carry "placeholder": true - a row (any quantity) under an '
    "'any other / undescribed items' heading, or one whose own text invites extras ('other costs "
    "if necessary'). They are optional: left blank, nothing is missing; priced, they are the "
    "bidder's own additional work, listed under priced_optional and kept out of evaluated_cost "
    "and the common scope, like an added line (client rule 3).",
    "Section groups (GOLD_SPEC 5): a section row that is itself a position ('A9') and the "
    "positions numbered under it ('9.1', '9.2') are one group, priced when any member is; an "
    "unpriced member of a priced group is not listed as unpriced, and the common scope is taken "
    "over groups (every member of a group every evaluable bidder priced).",
    "sheet_amounts = sum of the bid's own amount cells over rows matched to template positions "
    "plus "
    "added lines; sheet_subtotals = the amount cell of the bid's SUB-TOTAL row.",
    "sheets_added/sheets_removed compare every sheet name, hidden sheets included.",
    "Colours compare common_scope_total rounded to the cent; ties share a colour; no colour with "
    "fewer than two evaluable bidders or when all tie.",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", type=Path)
    ap.add_argument("--template", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)
    gold = build_gold(a.folder, a.template, a.verbose)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"wrote {a.out}: {sum(len(v) for v in gold['positions'].values())} positions, "
        f"{len(gold['bids'])} bids, {len(gold['common_scope'])} in the common scope, "
        f"{len(gold['notes'])} notes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
