#!/usr/bin/env python3
"""BOQ mutation harness (docs/GOLD_SPEC.md section 5).

For every template (``samples/*/BOQ/*BOQ.xlsx``) the harness builds a CLEAN priced bid from the
template itself: every position's rate cells get deterministic plausible numbers, the Summary's
overheads and profit percentages are filled, every formula is kept, and LibreOffice recalculates
the file so cached values exist. The clean bid must produce no finding at all.

Each mutation class then plants one realistic bidder change into a copy of the clean bid and
states what the BOQ check must report (rule, sheet, item, values) and what the evaluated total
must be. The gold is computed from the harness's own numbers (template quantity x the rates it
wrote x the markup it set), never from what ``read_boq`` returns. Cell edits are made with
openpyxl and recalculated with ``soffice --convert-to`` (as samples/gen_test_set_2.py does);
structural edits (a row deleted or inserted, the Summary shifted) are made through LibreOffice's
own UNO API so every formula reference moves the way it does when a bidder edits in Excel.

Every mutant is validated before it is scored: the planted change must be in the file, no cell
may hold a spreadsheet error, cached values must exist, every untouched position must keep the
clean bid's amount, and every SUB-TOTAL must equal the sum of its rows. A mutant that fails
validation is a harness defect: it is reported as invalid and left out of the percentages.

For every mutant the merged workbook is written too (``write_merged_workbook`` into a temp
dir) and opened: every template position must carry a row for the bidder, and the leaderboard's
evaluated total must equal the gold, i.e. added lines never count.

The two bidders compared in each case are the mutant ("bid") and the clean bid ("control"); the
control must stay finding-free in every case.

Mutants carry client values, so they live under ``samples/mutations/boq/`` (git-ignored); the
JSON written to ``eval_runs/`` holds aggregates and rule ids only.

    uv run --offline python scripts/mutate_boq.py --seed 1 --per-class 5
    uv run --offline python scripts/mutate_boq.py --classes quantity,removed --per-class 2 --show --keep-all
"""

# Quote characters are folded on purpose, rates only need to be reproducible (not secret), and
# ``B`` is the product module imported lazily because it changes under concurrent work.
# ruff: noqa: E501, RUF001, S101, S311, N803, N812, N818

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import openpyxl  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

SOFFICE = shutil.which("soffice") or "/usr/bin/soffice"
UNO_PYTHON = "/usr/bin/python3"  # the system Python carries LibreOffice's ``uno`` bridge
OVERHEAD, PROFIT = 0.07, 0.05  # written into the clean bid's Summary
MONEY_TOL = 0.02
SPREADSHEET_ERRORS = ("#VALUE!", "#REF!", "#NAME?", "#DIV/0!", "#N/A", "#NUM!", "#NULL!")
UNKNOWN_QTY_HEADER = "Menge"  # not a quantity synonym the reader knows (boq._HEADER_SYNONYMS)
ADDED_DESCRIPTION = "Additional works proposed by the Contractor"

CLASSES = (
    "quantity",
    "unit",
    "removed",
    "added",
    "description",
    "swap",
    "hardcoded",
    "arithmetic",
    "summary_shift",
    "header_alias",
    "header_unknown",
    "non_numeric",
    "merged_desc",
    "hidden_row",
    "no_cache",
    "reconcile",
    "filled_blank",
)
#: classes whose correct result is "no finding, same totals"
NEGATIVE = {"swap", "summary_shift", "header_alias", "merged_desc", "hidden_row"}


# ------------------------------------------------------------------------------------------
# independent reading of the template (the gold side; never read_boq)


def _label(v: object) -> str:
    return re.sub(r"\s+", " ", v).strip().lower() if isinstance(v, str) else ""


def _is_formula(v: object) -> bool:
    return isinstance(v, str) and v.startswith("=")


def _number(v: object) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        return float(v)
    return None


def _item_str(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    s = re.sub("[„“”\"'‘’«»‚]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


_UNIT_GROUPS = {
    **dict.fromkeys(("m2", "m²", "sqm"), "m2"),
    **dict.fromkeys(("m3", "m³", "cbm"), "m3"),
    **dict.fromkeys(("lm", "rm", "m"), "lm"),
    **dict.fromkeys(("pcs", "pc", "no", "nr", "item", "set", "ls", "lot", "each", "ea"), "count"),
}


def _unit_group(u: object) -> str:
    t = re.sub(r"[\s.]", "", str(u or "").lower())
    return _UNIT_GROUPS.get(t, t)


HEADER_WORDS: dict[str, Callable[[str], bool]] = {
    "desc": lambda t: t.startswith("description"),
    "qty": lambda t: t.startswith("quantit"),
    "unit": lambda t: t == "unit",
    "rm": lambda t: t == "materials",
    "sm": lambda t: t == "materials sum",
    "rl": lambda t: t in ("labour", "labor"),
    "sl": lambda t: t in ("labour sum", "labor sum"),
    "rd": lambda t: t == "delivery",
    "sd": lambda t: t == "delivery sum",
    "rate": lambda t: t.startswith("rate in"),
    "amount": lambda t: t.startswith("amount in"),
}
RATE_KEYS = ("rm", "rl", "rd")


@dataclass
class Pos:
    sheet: str
    row: int
    item: str
    desc: str
    qty: float | None
    unit: str | None
    amount_formula: str | None  # the template's own-row product formula, if any
    rate: float = 0.0
    rates: dict[str, float] = field(default_factory=dict)

    @property
    def cost(self) -> float:
        return (self.qty or 0.0) * self.rate

    @property
    def eligible(self) -> bool:
        """Priced at a template quantity through an own-row amount formula."""
        return bool(self.qty) and self.rate > 0 and self.amount_formula is not None


@dataclass
class SheetLayout:
    name: str
    cols: dict[str, int]
    qty_header: tuple[int, int]
    header_last: int
    positions: list[Pos] = field(default_factory=list)
    subtotal_row: int | None = None


@dataclass
class SummaryLayout:
    sheet: str
    value_col: int
    total_row: int
    incl_row: int
    oh_cell: tuple[int, int] | None
    pr_cell: tuple[int, int] | None
    vat: float


def _own_row_product(formula: str, row: int) -> bool:
    if "SUM(" in formula.upper() or "*" not in formula:
        return False
    rows = {int(m) for m in re.findall(r"\$?[A-Z]{1,3}\$?(\d+)", formula.upper())}
    return rows == {row}


#: rows the template leaves for the bidder's own items; a clean bid does not price them
PLACEHOLDER = re.compile(
    r"any other|list(?:ed)? here|to be listed|undescribed|other costs if necessary|sundry", re.I
)


def discover_sheet(ws: Any, wsv: Any | None = None) -> SheetLayout | None:
    cols: dict[str, int] = {}
    qty_header = (0, 0)
    last = 0
    for r in range(1, 16):
        for c in range(1, min(ws.max_column, 60) + 1):
            t = _label(ws.cell(r, c).value)
            if not t:
                continue
            for key, pred in HEADER_WORDS.items():
                if key not in cols and pred(t):
                    cols[key] = c
                    last = max(last, r)
                    if key == "qty":
                        qty_header = (r, c)
    if not {"desc", "qty", "unit", "amount"} <= cols.keys():
        return None
    lay = SheetLayout(ws.title, cols, qty_header, last)
    for r in range(last + 1, ws.max_row + 1):
        desc = ws.cell(r, cols["desc"]).value
        if _is_formula(desc):
            if re.search(r"SUB-?\s?TOTAL", desc.upper()):
                lay.subtotal_row = r
            continue
        if not isinstance(desc, str) or not re.search(r"[^\W\d_]", desc):
            continue
        amount = ws.cell(r, cols["amount"]).value
        qty_v = ws.cell(r, cols["qty"]).value
        unit_v = ws.cell(r, cols["unit"]).value
        if _is_formula(qty_v) and "SUM(" in qty_v.upper():
            continue  # a parent: its quantity is the sum of the rows under it
        own = _is_formula(amount) and _own_row_product(amount, r)
        if not (
            own
            or _number(amount) is not None
            or (amount is None and (qty_v is not None or unit_v is not None))
        ):
            continue
        item = _item_str(ws.cell(r, 2).value) or _item_str(ws.cell(r, 1).value)
        qty = _number(qty_v)
        if qty is None and _is_formula(qty_v) and wsv is not None:
            qty = _number(
                wsv.cell(r, cols["qty"]).value
            )  # "=60-49": the template's own cached value
        lay.positions.append(
            Pos(
                ws.title,
                r,
                item,
                desc.strip(),
                qty,
                None if unit_v is None else str(unit_v).strip(),
                amount if own else None,
            )
        )
    return lay


def discover_summary(wb: Any) -> SummaryLayout | None:
    ws = next((w for w in wb.worksheets if w.title.strip().upper() == "SUMMARY"), None)
    if ws is None:
        return None
    rows: dict[str, int] = {}
    oh = pr = None
    value_col = 0
    vat = 0.0
    for r in range(1, ws.max_row + 1):
        for c in range(1, min(ws.max_column, 12) + 1):
            t = _label(ws.cell(r, c).value)
            if not t:
                continue
            if t.startswith("total cost"):
                rows["total"] = r
            elif "including vat" in t:
                rows["incl"] = r
            elif t == "vat":
                for c2 in range(c + 1, min(ws.max_column, 20) + 1):
                    n = _number(ws.cell(r, c2).value)
                    if n is not None and 0 < n < 1:
                        vat = n
            elif "overhead" in t or "profit" in t:
                for c2 in range(c + 1, min(ws.max_column, 20) + 1):
                    f = ws.cell(r, c2).value
                    if _is_formula(f):
                        value_col = c2
                        for col_s, row_s in re.findall(r"\$?([A-Z]{1,3})\$?(\d+)", f.upper()):
                            cell = ws[f"{col_s}{row_s}"]
                            if int(row_s) == r and cell.value is None:
                                if "overhead" in t:
                                    oh = (r, cell.column)
                                else:
                                    pr = (r, cell.column)
                        break
    if not ({"total", "incl"} <= rows.keys() and value_col):
        return None
    return SummaryLayout(ws.title, value_col, rows["total"], rows["incl"], oh, pr, vat)


# ------------------------------------------------------------------------------------------
# LibreOffice


def recalc(paths: list[Path]) -> None:
    """openpyxl drops cached values; LibreOffice recomputes and stores them (gen_test_set_2._recalc)."""
    if not paths:
        return
    names = [p.name for p in paths]
    if len(set(names)) != len(names):
        raise ValueError("recalc needs unique basenames")
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "profile"
        out = Path(tmp) / "out"
        out.mkdir()
        for i in range(0, len(paths), 25):
            chunk = paths[i : i + 25]
            subprocess.run(
                [
                    SOFFICE,
                    "--headless",
                    "--norestore",
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--calc",
                    "--convert-to",
                    "xlsx:Calc MS Excel 2007 XML",
                    "--outdir",
                    str(out),
                    *map(str, chunk),
                ],
                check=True,
                capture_output=True,
                timeout=600,
            )
        for p in paths:
            produced = out / p.name
            if not produced.exists():
                raise RuntimeError(f"LibreOffice produced no output for {p.name}")
            shutil.move(str(produced), p)


UNO_HELPER = r"""
import json, os, subprocess, sys, time
import uno
from com.sun.star.beans import PropertyValue

def prop(name, value):
    p = PropertyValue(); p.Name = name; p.Value = value; return p

jobs = json.load(open(sys.argv[1]))
profile = sys.argv[2]
pipe = "boqmut%d" % os.getpid()
url = "uno:pipe,name=%s;urp;StarOffice.ComponentContext" % pipe
proc = subprocess.Popen([sys.argv[3], "--headless", "--invisible", "--norestore", "--nologo", "--nodefault",
                         "-env:UserInstallation=" + profile, "--accept=pipe,name=%s;urp;" % pipe],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
desktop = None
try:
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
    ctx = None
    for _ in range(240):
        try:
            ctx = resolver.resolve(url)
            break
        except Exception:
            time.sleep(0.25)
    if ctx is None:
        raise SystemExit("soffice did not accept a UNO connection")
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    for job in jobs:
        doc = desktop.loadComponentFromURL(uno.systemPathToFileUrl(job["in"]), "_blank", 0, (prop("Hidden", True),))
        try:
            for op in job["ops"]:
                sh = doc.Sheets.getByName(op["sheet"])
                if op["op"] == "delete_row":
                    sh.Rows.removeByIndex(op["row"] - 1, 1)
                elif op["op"] == "insert_row":
                    sh.Rows.insertByIndex(op["row"] - 1, 1)
                    for col, value in op["cells"]:
                        cell = sh.getCellByPosition(col - 1, op["row"] - 1)
                        if isinstance(value, str) and value.startswith("="):
                            cell.setFormula(value)
                        elif isinstance(value, str):
                            cell.setString(value)
                        else:
                            cell.setValue(value)
                elif op["op"] == "shift":
                    sh.Rows.insertByIndex(0, 1)
                    sh.Columns.insertByIndex(0, 1)
                else:
                    raise SystemExit("unknown op " + op["op"])
            doc.calculateAll()
            doc.storeToURL(uno.systemPathToFileUrl(job["out"]), (prop("FilterName", "Calc MS Excel 2007 XML"),))
        finally:
            doc.close(True)
finally:
    if desktop is not None:
        try:
            desktop.terminate()
        except Exception:
            pass
    try:
        proc.wait(15)
    except Exception:
        proc.kill()
"""


def run_uno(jobs: list[dict[str, Any]]) -> None:
    if not jobs:
        return
    with tempfile.TemporaryDirectory() as tmp:
        helper = Path(tmp) / "uno_ops.py"
        helper.write_text(UNO_HELPER, encoding="utf-8")
        spec = Path(tmp) / "jobs.json"
        spec.write_text(json.dumps(jobs), encoding="utf-8")
        profile = (Path(tmp) / "profile").as_uri()
        subprocess.run(
            [UNO_PYTHON, str(helper), str(spec), profile, SOFFICE],
            check=True,
            capture_output=True,
            timeout=900,
        )
    for job in jobs:
        if not Path(job["out"]).exists():
            raise RuntimeError(f"LibreOffice UNO produced no output for {Path(job['out']).name}")


# ------------------------------------------------------------------------------------------
# the clean base bid


@dataclass
class Base:
    bid: str  # "b0", "b1": never the client's folder or file name
    template: Path
    path: Path
    layouts: dict[str, SheetLayout]
    summary: SummaryLayout
    markup: float
    cost: float
    stated_incl: float | None = None
    amounts: dict[tuple[str, int], float] = field(default_factory=dict)  # cached, clean bid
    subtotals: dict[str, float] = field(default_factory=dict)
    checks: dict[str, Any] = field(default_factory=dict)

    @property
    def positions(self) -> list[Pos]:
        return [p for lay in self.layouts.values() for p in lay.positions]


def _rates_for(sheet: str, row: int) -> dict[str, float]:
    rng = random.Random(zlib.crc32(f"{sheet}|{row}".encode()))
    m = round(rng.uniform(8, 450), 2)
    return {
        "rm": m,
        "rl": round(m * rng.uniform(0.2, 0.7), 2),
        "rd": round(m * rng.uniform(0.01, 0.08), 2),
    }


#: the canonical formula of each computed column of a position row
_CANONICAL = {
    "sm": ("rm", "*", "qty"),
    "sl": ("rl", "*", "qty"),
    "sd": ("rd", "*", "qty"),
    "amount": ("rate", "*", "qty"),
}


def _canonical_formulas(ws: Any, lay: SheetLayout, row: int) -> int:
    """Rewrite a position row's existing formulas to the canonical pattern; return how many changed.

    The client's template carries broken sub-sum formulas of its own (a delivery sum written as
    materials + labour + delivery, or negated). A bid copied from it inherits them; the clean
    base must not, or every mutant would start with arithmetic errors nobody planted.
    """
    want: dict[str, str] = {}
    c = {k: f"{get_column_letter(v)}{row}" for k, v in lay.cols.items()}
    for key, (a, _, b) in _CANONICAL.items():
        if key in c and a in c and b in c:
            want[key] = f"={c[a]}*{c[b]}"
    if "rate" in c and all(k in c for k in RATE_KEYS):
        want["rate"] = f"={c['rm']}+{c['rl']}+{c['rd']}"
    changed = 0
    for key, formula in want.items():
        cell = ws.cell(row, lay.cols[key])
        if (
            _is_formula(cell.value)
            and cell.value.replace("$", "").replace(" ", "").upper() != formula
        ):
            cell.value = formula
            changed += 1
    return changed


def _repair_section_sums(ws: Any, lay: SheetLayout) -> int:
    """Extend a section subtotal's ``=SUM(P13:P19)`` to the section's last position.

    The template has a section whose SUM range stops short of its positions; every bid copied
    from it has a SUB-TOTAL that does not add up. The clean base must add up.
    """
    amount_col = get_column_letter(lay.cols["amount"])
    pos_rows = sorted(p.row for p in lay.positions)
    sec_rows = sorted(
        r
        for r in range(lay.header_last + 1, (lay.subtotal_row or ws.max_row))
        if _is_formula(v := ws.cell(r, lay.cols["amount"]).value) and v.upper().startswith("=SUM(")
    )
    changed = 0
    for i, r in enumerate(sec_rows):
        end = sec_rows[i + 1] if i + 1 < len(sec_rows) else (lay.subtotal_row or ws.max_row + 1)
        inside = [x for x in pos_rows if r < x < end]
        if not inside:
            continue
        m = re.fullmatch(
            rf"=SUM\({amount_col}(\d+):{amount_col}(\d+)\)",
            ws.cell(r, lay.cols["amount"]).value.upper(),
        )
        if not m or int(m.group(2)) >= inside[-1]:
            continue
        lo, hi = m.group(1), m.group(2)
        for c in range(1, ws.max_column + 1):
            v = ws.cell(r, c).value
            if _is_formula(v):
                new = re.sub(rf"(\$?[A-Z]{{1,3}}\$?){hi}(?!\d)", rf"\g<1>{inside[-1]}", v)
                if new != v:
                    ws.cell(r, c).value = new
                    changed += 1
        _ = lo
    return changed


def build_base(bid: str, template: Path, out: Path, rebuild: bool) -> Base:
    wb = openpyxl.load_workbook(template)
    wtv = openpyxl.load_workbook(template, data_only=True)
    layouts = {
        ws.title: lay
        for ws in wb.worksheets
        if (lay := discover_sheet(ws, wtv[ws.title])) is not None
    }
    summary = discover_summary(wb)
    if summary is None or summary.oh_cell is None or summary.pr_cell is None:
        raise RuntimeError(f"{bid}: Summary layout not recognised")
    repaired = 0
    for lay in layouts.values():
        ws = wb[lay.name]
        repaired += _repair_section_sums(ws, lay)
        for p in lay.positions:
            if PLACEHOLDER.search(p.desc):
                continue
            vals = _rates_for(lay.name, p.row)
            keys = [k for k in RATE_KEYS if k in lay.cols]
            if keys:
                for k in keys:
                    cell = ws.cell(p.row, lay.cols[k])
                    if _is_formula(cell.value):
                        continue
                    cell.value = vals[k]
                    p.rates[k] = vals[k]
            elif "rate" in lay.cols and not _is_formula(ws.cell(p.row, lay.cols["rate"]).value):
                total = round(sum(vals.values()), 2)
                ws.cell(p.row, lay.cols["rate"]).value = total
                p.rates["rate"] = total
            p.rate = round(sum(p.rates.values()), 2)
            repaired += _canonical_formulas(ws, lay, p.row)
    ws = wb[summary.sheet]
    ws.cell(*summary.oh_cell).value = OVERHEAD
    ws.cell(*summary.pr_cell).value = PROFIT
    if rebuild or not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        wb.save(out)
        recalc([out])
    markup = (1 + OVERHEAD + PROFIT) * (1 + summary.vat)
    base = Base(
        bid,
        template,
        out,
        layouts,
        summary,
        markup,
        sum(p.cost for p in (q for lay in layouts.values() for q in lay.positions)),
    )
    wv = openpyxl.load_workbook(out, data_only=True)
    for lay in layouts.values():
        ws = wv[lay.name]
        for p in lay.positions:
            a = _number(ws.cell(p.row, lay.cols["amount"]).value)
            if a is not None:
                base.amounts[(lay.name, p.row)] = a
        if lay.subtotal_row:
            base.subtotals[lay.name] = (
                _number(ws.cell(lay.subtotal_row, lay.cols["amount"]).value) or 0.0
            )
    sws = wv[summary.sheet]
    base.stated_incl = _number(sws.cell(summary.incl_row, summary.value_col).value)
    stated_total = _number(sws.cell(summary.total_row, summary.value_col).value)
    base.checks["template_formulas_repaired"] = repaired
    base.checks["stated_markup_ok"] = bool(
        base.stated_incl and stated_total and abs(base.stated_incl / stated_total - markup) < 1e-9
    )
    base.checks["amounts_ok"] = all(
        abs(base.amounts.get((p.sheet, p.row), -1) - p.cost) <= MONEY_TOL
        for p in base.positions
        if p.amount_formula
    )
    return base


def check_base(base: Base) -> dict[str, Any]:
    """The clean bid against the template: no finding, and the reader sees our positions."""
    from procurement_agent.checks import boq as B

    tmpl = B.read_boq(base.template, name="template")
    ctrl = B.read_boq(base.path, name="control")
    comp = B.compare_all(tmpl, [ctrl])[0]
    mine = {(p.sheet, p.row) for p in base.positions}
    theirs = {
        (ln.sheet, ln.row) for s in tmpl.sheets.values() for ln in s.lines if ln.kind == "position"
    }
    out = {
        "positions_harness": len(mine),
        "positions_reader": len(theirs),
        "only_harness": sorted(mine - theirs),
        "only_reader": sorted(theirs - mine),
        "base_findings": [(f.rule_id, f.message_en) for f in comp.findings],
        "template_findings": [f.rule_id for f in B.template_findings(tmpl)],
        "evaluated_total": comp.evaluated_total,
        "expected_total": base.cost * base.markup,
        **base.checks,
    }
    out["total_ok"] = (
        comp.evaluated_total is not None
        and abs(comp.evaluated_total - base.cost * base.markup) <= MONEY_TOL
    )
    return out


# ------------------------------------------------------------------------------------------
# mutation plans


@dataclass
class Exp:
    rule: str
    sheet: str | None = None
    item: str | None = None
    row: int | None = None
    expected: float | str | None = None
    actual: float | str | None = None
    struct: Callable[[Any], bool] | None = None


@dataclass
class Allow:
    """A finding that is a true consequence of the planted change, not a false positive."""

    rule: str | None
    sheet: str | None  # None: a finding that names no sheet
    item: str | None = None
    row: int | None = None
    summary: bool = False  # a Summary-level finding ("Summary: row 10 ... against sheet ...")

    def admits(self, f: Any, sheets: list[str]) -> bool:
        msg = f.message_en or ""
        if self.rule is not None and self.rule != f.rule_id:
            return False
        if self.summary:
            return msg.startswith("Summary")
        if self.sheet is None:
            return _names_no_sheet(f, sheets)
        if not _mentions(f, self.sheet) or msg.startswith("Summary"):
            return False
        if self.item is None and self.row is None:
            return True
        return _loc_ok(f, Exp(f.rule_id, self.sheet, self.item or None, self.row))


@dataclass
class Plan:
    cls: str
    idx: int
    base: Base
    kind: str  # "cells" (openpyxl + recalc), "uno", "nocache"
    note: str
    edit: Callable[[Any], None] | None = None
    ops: list[dict[str, Any]] = field(default_factory=list)
    expect: list[Exp] = field(default_factory=list)
    allow: list[Allow] = field(default_factory=list)
    cost_delta: float = 0.0
    touched: set[tuple[str, int]] = field(default_factory=set)
    row_map: Callable[[str, int], int | None] = lambda s, r: r
    verify: Callable[[Any, Any], list[str]] | None = None
    subtotal_delta: dict[str, float] = field(default_factory=dict)
    skip_subtotal: set[str] = field(default_factory=set)
    summary_offset: tuple[int, int] = (0, 0)
    same_stated: bool = False
    path: Path | None = None


class NoCandidate(Exception):
    pass


def _choose(rng: random.Random, items: list[Any]) -> Any:
    if not items:
        raise NoCandidate
    return rng.choice(items)


def _choose_sheet_first(rng: random.Random, positions: list[Pos]) -> Pos:
    """Uniform over sheets, then over positions: small sheets get their share."""
    sheets = sorted({p.sheet for p in positions})
    sheet = _choose(rng, sheets)
    return _choose(rng, [p for p in positions if p.sheet == sheet])


def _col(lay: SheetLayout, key: str) -> str:
    return get_column_letter(lay.cols[key])


def _same_section(lay: SheetLayout, p: Pos) -> list[Pos]:
    """The run of positions on consecutive rows around ``p`` (one SUM range in the template)."""
    rows = {q.row: q for q in lay.positions}
    run = [p]
    r = p.row - 1
    while r in rows:
        run.insert(0, rows[r])
        r -= 1
    r = p.row + 1
    while r in rows:
        run.append(rows[r])
        r += 1
    return run


def _cell_check(sheet: str, row: int, col: int, want: object) -> Callable[[Any, Any], list[str]]:
    def verify(wf: Any, wv: Any) -> list[str]:
        got = wf[sheet].cell(row, col).value
        if isinstance(want, float):
            ok = _number(got) is not None and abs(_number(got) - want) < 1e-9  # type: ignore[operator]
        else:
            ok = got == want
        return (
            []
            if ok
            else [f"planted value missing at {sheet}!{get_column_letter(col)}{row}: {got!r}"]
        )

    return verify


def plan_case(cls: str, idx: int, base: Base, seed: int) -> Plan:
    rng = random.Random(f"{seed}|{cls}|{idx}|{base.bid}")
    lays = base.layouts
    eligible = [p for p in base.positions if p.eligible]
    plan = Plan(cls, idx, base, "cells", "")

    if cls == "quantity":
        if idx % 2 == 0:
            firsts = [
                p
                for p in eligible
                if _fold(p.unit or "") == "item"
                and p in [q for q in lays[p.sheet].positions if q.eligible][:3]
            ]
            p = _choose(rng, firsts or [q for q in eligible if _fold(q.unit or "") == "item"])
        else:
            p = _choose_sheet_first(rng, eligible)
        lay = lays[p.sheet]
        assert p.qty is not None
        if _unit_group(p.unit) == "count":  # counted things change by whole units
            new = p.qty + rng.choice([1.0, 2.0, 3.0] + ([-1.0] if p.qty > 1 else []))
        else:
            new = round(p.qty * rng.choice([1.1, 0.9, 1.25, 2.0]), 2)
        if new == p.qty:
            new = p.qty + 1
        plan.edit = lambda wb: setattr(wb[p.sheet].cell(p.row, lay.cols["qty"]), "value", new)
        plan.verify = _cell_check(p.sheet, p.row, lay.cols["qty"], new)
        plan.touched = {(p.sheet, p.row)}
        plan.subtotal_delta = {p.sheet: (new - p.qty) * p.rate}
        plan.expect = [
            Exp(
                "BOQ.QUANTITY_CHANGED",
                p.sheet,
                p.item,
                p.row,
                p.qty,
                new,
                struct=lambda c, p=p, new=new: any(
                    d.sheet == p.sheet
                    and _item_str(d.item_no) == p.item
                    and _feq(d.template_qty, p.qty)
                    and _feq(d.bid_qty, new)
                    for d in getattr(c, "quantity_changed", [])
                ),
            )
        ]
        plan.note = f"{p.sheet} row {p.row} ({p.item}, unit {p.unit}): quantity {p.qty} -> {new}"

    elif cls == "unit":
        p = _choose_sheet_first(rng, eligible)
        lay = lays[p.sheet]
        new = _choose(
            rng,
            [
                u
                for u in ("kg", "t", "m3", "m2", "lm", "pcs", "set", "item", "hr")
                if _unit_group(u) != _unit_group(p.unit)
            ],
        )
        plan.edit = lambda wb: setattr(wb[p.sheet].cell(p.row, lay.cols["unit"]), "value", new)
        plan.verify = _cell_check(p.sheet, p.row, lay.cols["unit"], new)
        plan.expect = [Exp("BOQ.UNIT_CHANGED", p.sheet, p.item, p.row, p.unit, new)]
        plan.note = f"{p.sheet} row {p.row} ({p.item}): unit {p.unit!r} -> {new!r}"

    elif cls == "removed":
        p = _choose_sheet_first(
            rng, [q for q in eligible if len(_same_section(lays[q.sheet], q)) >= 2]
        )
        plan.kind = "uno"
        plan.ops = [{"op": "delete_row", "sheet": p.sheet, "row": p.row}]
        plan.cost_delta = -p.cost
        plan.touched = {(p.sheet, p.row)}
        plan.row_map = lambda s, r, p=p: (
            r if s != p.sheet or r < p.row else (None if r == p.row else r - 1)
        )
        plan.subtotal_delta = {p.sheet: -p.cost}

        def verify(wf: Any, wv: Any, p: Pos = p) -> list[str]:
            ws = wf[p.sheet]
            items = {_item_str(ws.cell(r, 2).value) for r in range(1, ws.max_row + 1)}
            return [f"item {p.item} still present"] if p.item in items else []

        plan.verify = verify
        plan.expect = [
            Exp(
                "BOQ.LINE_REMOVED",
                p.sheet,
                p.item,
                p.row,
                struct=lambda c, p=p: any(
                    ln.sheet == p.sheet and _item_str(ln.item_no) == p.item
                    for ln in getattr(c, "removed", [])
                ),
            )
        ]
        plan.note = f"{p.sheet} row {p.row} ({p.item}) deleted; cost {p.cost:,.2f}"

    elif cls == "added":
        pairs = [
            (p, q) for p in eligible for q in eligible if q.sheet == p.sheet and q.row == p.row + 1
        ]
        p, q = _choose(rng, pairs)
        lay = lays[p.sheet]
        existing = {x.item for x in lay.positions}
        item = f"{p.item}.1"
        while item in existing:
            item += ".1"
        new_row = q.row
        qty = float(rng.choice([3, 10, 25]))
        rates = {k: round(v * 0.8, 2) for k, v in _rates_for(p.sheet, 10_000 + p.row).items()}
        wf = openpyxl.load_workbook(base.path)
        ws = wf[p.sheet]
        cells: list[tuple[int, Any]] = [
            (2, item),
            (lay.cols["desc"], ADDED_DESCRIPTION),
            (lay.cols["qty"], qty),
        ]
        if p.unit:
            cells.append((lay.cols["unit"], p.unit))
        for k in RATE_KEYS:
            if k in lay.cols and k in p.rates:
                cells.append((lay.cols[k], rates[k]))
        set_cols = {c for c, _ in cells}
        for c in range(1, ws.max_column + 1):
            v = ws.cell(p.row, c).value
            own = _is_formula(v) and {
                int(x) for x in re.findall(r"\$?[A-Z]{1,3}\$?(\d+)", v.upper())
            } == {p.row}
            if own and c not in set_cols and re.fullmatch(r"=[\$A-Z0-9*+\-/() .]+", v):
                cells.append(
                    (c, re.sub(rf"(\$?[A-Z]{{1,3}}\$?){p.row}(?!\d)", rf"\g<1>{new_row}", v))
                )
        added_amount = qty * sum(rates[k] for k in RATE_KEYS if k in lay.cols and k in p.rates)
        plan.kind = "uno"
        plan.ops = [{"op": "insert_row", "sheet": p.sheet, "row": new_row, "cells": cells}]
        plan.row_map = lambda s, r, p=p, new_row=new_row: (
            r if s != p.sheet or r < new_row else r + 1
        )
        plan.subtotal_delta = {p.sheet: added_amount}

        def verify(
            wf: Any,
            wv: Any,
            p: Pos = p,
            new_row: int = new_row,
            item: str = item,
            amt: float = added_amount,
            lay: SheetLayout = lay,
        ) -> list[str]:
            probs = []
            if _item_str(wf[p.sheet].cell(new_row, 2).value) != item:
                probs.append(f"added item not at row {new_row}")
            got = _number(wv[p.sheet].cell(new_row, lay.cols["amount"]).value)
            if got is None or abs(got - amt) > MONEY_TOL:
                probs.append(f"added amount {got} != {amt}")
            return probs

        plan.verify = verify
        plan.expect = [
            Exp(
                "BOQ.LINE_ADDED",
                p.sheet,
                item,
                new_row,
                struct=lambda c, p=p, item=item: any(
                    ln.sheet == p.sheet and _item_str(ln.item_no) == item
                    for ln in getattr(c, "added", [])
                ),
            )
        ]
        plan.note = f"{p.sheet}: line {item} inserted at row {new_row} (qty {qty}, amount {added_amount:,.2f}); must not count"
        plan.checks_added = added_amount  # type: ignore[attr-defined]

    elif cls == "description":
        cands = [q for q in eligible if len(q.desc) >= 25 and re.search(r"[A-Za-z]{5,}", q.desc)]
        p = _choose_sheet_first(rng, cands)
        lay = lays[p.sheet]
        spots = [
            m.start() + i
            for m in re.finditer(r"[A-Za-z]{5,}", p.desc)
            for i in range(1, len(m.group()) - 2)
            if p.desc[m.start() + i].lower() != p.desc[m.start() + i + 1].lower()
        ]
        k = _choose(rng, spots)
        new = p.desc[:k] + p.desc[k + 1] + p.desc[k] + p.desc[k + 2 :]
        plan.edit = lambda wb: setattr(wb[p.sheet].cell(p.row, lay.cols["desc"]), "value", new)
        plan.verify = _cell_check(p.sheet, p.row, lay.cols["desc"], new)
        plan.expect = [Exp("BOQ.DESCRIPTION_CHANGED", p.sheet, p.item, p.row)]
        plan.note = f"{p.sheet} row {p.row} ({p.item}): description {p.desc!r} -> {new!r}"

    elif cls == "swap":
        wf = openpyxl.load_workbook(base.path)
        merged_rows = {
            (ws.title, r)
            for ws in wf.worksheets
            for m in ws.merged_cells.ranges
            for r in range(m.min_row, m.max_row + 1)
        }
        pairs = [
            (p, q)
            for p in eligible
            for q in eligible
            if q.sheet == p.sheet
            and q.row == p.row + 1
            and q.item != p.item
            and (p.sheet, p.row) not in merged_rows
            and (q.sheet, q.row) not in merged_rows
        ]
        p, q = _choose(rng, pairs)

        def edit(wb: Any, p: Pos = p, q: Pos = q) -> None:
            ws = wb[p.sheet]
            for c in range(1, ws.max_column + 1):
                a, b = ws.cell(p.row, c), ws.cell(q.row, c)
                if _is_formula(a.value) or _is_formula(b.value):
                    continue
                a.value, b.value = b.value, a.value

        plan.edit = edit
        plan.touched = {(p.sheet, p.row), (q.sheet, q.row)}
        plan.verify = _cell_check(p.sheet, p.row, 2, q.item)
        plan.note = f"{p.sheet}: rows {p.row} ({p.item}) and {q.row} ({q.item}) swapped"

    elif cls == "hardcoded":
        p = _choose_sheet_first(rng, eligible)
        lay = lays[p.sheet]
        value = round(p.cost, 2)
        plan.edit = lambda wb: setattr(wb[p.sheet].cell(p.row, lay.cols["amount"]), "value", value)
        plan.verify = _cell_check(p.sheet, p.row, lay.cols["amount"], value)
        plan.touched = {(p.sheet, p.row)}
        plan.expect = [Exp("BOQ.HARDCODED_TOTAL", p.sheet, p.item, p.row)]
        plan.note = f"{p.sheet} row {p.row} ({p.item}): amount formula {p.amount_formula} typed over with {value}"

    elif cls == "arithmetic":
        p = _choose_sheet_first(
            rng,
            [
                q
                for q in eligible
                if any(o.sheet == q.sheet and o.eligible and o.qty != q.qty for o in base.positions)
            ],
        )
        lay = lays[p.sheet]
        if idx % 2 == 0:  # a formula copied from the wrong row
            o = _choose(rng, [o for o in eligible if o.sheet == p.sheet and o.qty != p.qty])
            value: Any = f"={_col(lay, 'rate')}{p.row}*{_col(lay, 'qty')}{o.row}"
            amount = p.rate * (o.qty or 0.0)
            plan.allow = [Allow("BOQ.HARDCODED_TOTAL", p.sheet, p.item, p.row)]
            plan.expect = [Exp("BOQ.ARITHMETIC", p.sheet, p.item, p.row, p.cost, amount)]
            plan.note = f"{p.sheet} row {p.row} ({p.item}): amount formula {p.amount_formula} -> {value} ({amount:,.2f} vs {p.cost:,.2f})"
        else:  # a wrong number typed over the formula
            amount = round(p.cost * rng.choice([1.1, 0.9, 1.5]), 2)
            value = amount
            plan.expect = [
                Exp("BOQ.ARITHMETIC", p.sheet, p.item, p.row, p.cost, amount),
                Exp("BOQ.HARDCODED_TOTAL", p.sheet, p.item, p.row),
            ]
            plan.note = f"{p.sheet} row {p.row} ({p.item}): amount typed as {amount:,.2f} (qty x rate = {p.cost:,.2f})"
        plan.edit = lambda wb: setattr(wb[p.sheet].cell(p.row, lay.cols["amount"]), "value", value)
        plan.verify = _cell_check(p.sheet, p.row, lay.cols["amount"], value)
        plan.touched = {(p.sheet, p.row)}
        plan.subtotal_delta = {p.sheet: amount - p.cost}
        # the Summary sums the materials/labour/delivery columns, the sheet's SUB-TOTAL the amount
        # column: after a wrong amount the two disagree, and saying so is correct
        plan.allow.append(Allow("BOQ.RECONCILE", None, summary=True))

    elif cls == "summary_shift":
        plan.kind = "uno"
        plan.ops = [{"op": "shift", "sheet": base.summary.sheet}]
        plan.summary_offset = (1, 1)
        plan.same_stated = True
        plan.note = "Summary: one row inserted above and one column left of everything"

    elif cls in ("header_alias", "header_unknown"):
        sheets = sorted(lays)
        sheet = sheets[(idx + rng.randrange(len(sheets))) % len(sheets)] if idx else sheets[0]
        lay = lays[sheet]
        word = "Qty." if cls == "header_alias" else UNKNOWN_QTY_HEADER
        r, c = lay.qty_header
        plan.edit = lambda wb: setattr(wb[sheet].cell(r, c), "value", word)
        plan.verify = _cell_check(sheet, r, c, word)
        plan.note = f"{sheet}: Quantity header renamed {word!r}"
        if cls == "header_unknown":
            plan.expect = [Exp("BOQ.SHEET_UNREADABLE", sheet)]
            # decision: anything said about the unreadable sheet itself, and Summary-level
            # reconciliation it can no longer support, is not a false positive
            plan.allow = [
                Allow(None, sheet),
                Allow("BOQ.RECONCILE", None),
                Allow("BOQ.RECONCILE", None, summary=True),
            ]
            plan.cost_delta = -sum(p.cost for p in lay.positions)

    elif cls == "non_numeric":
        token = ("incl.", "-", "N/A")[idx % 3]
        p = _choose_sheet_first(rng, eligible)
        lay = lays[p.sheet]
        key = "qty" if idx % 2 == 0 else _choose(rng, [k for k in ("rm", "rl") if k in p.rates])
        col = lay.cols[key]

        def edit(wb: Any, p: Pos = p, col: int = col, token: str = token) -> None:
            ws = wb[p.sheet]
            dirty = {get_column_letter(col)}
            ws.cell(p.row, col).value = token
            changed = True
            while changed:  # every formula of the row that reads a text cell becomes text too
                changed = False
                for c in range(1, ws.max_column + 1):
                    v = ws.cell(p.row, c).value
                    if _is_formula(v) and any(
                        re.search(rf"(?<![A-Z$])\$?{d}\$?{p.row}(?!\d)", v.upper()) for d in dirty
                    ):
                        ws.cell(p.row, c).value = token
                        dirty.add(get_column_letter(c))
                        changed = True

        plan.edit = edit
        plan.verify = _cell_check(p.sheet, p.row, col, token)
        plan.touched = {(p.sheet, p.row)}
        plan.cost_delta = -p.cost
        plan.subtotal_delta = {p.sheet: -p.cost}
        # decision: every text cell of the planted row may be named (one finding per cell), and
        # the Summary (materials/labour/delivery columns) no longer matches the amount column
        plan.allow = [
            Allow("BOQ.NON_NUMERIC", p.sheet, p.item, p.row),
            Allow("BOQ.RECONCILE", None, summary=True),
        ]
        plan.expect = [
            Exp(
                "BOQ.NON_NUMERIC",
                p.sheet,
                p.item,
                p.row,
                struct=lambda c, p=p: (
                    not any(
                        d.sheet == p.sheet
                        and _item_str(d.item_no) == p.item
                        and _feq(d.bid_qty, 0.0)
                        for d in getattr(c, "quantity_changed", [])
                    )
                ),
            )
        ]
        plan.note = f"{p.sheet} row {p.row} ({p.item}): {token!r} typed into {key}"

    elif cls == "merged_desc":
        wf = openpyxl.load_workbook(base.path)

        def merge_ref(p: Pos) -> str | None:
            """The description cell merged over empty cells only: down one row, else rightwards."""
            ws, lay = wf[p.sheet], lays[p.sheet]
            dc, col = lay.cols["desc"], get_column_letter(lays[p.sheet].cols["desc"])
            if all(ws.cell(p.row + 1, c).value is None for c in range(1, ws.max_column + 1)):
                return f"{col}{p.row}:{col}{p.row + 1}"
            right = dc
            while right + 1 < lay.cols["qty"] and ws.cell(p.row, right + 1).value is None:
                right += 1
            return f"{col}{p.row}:{get_column_letter(right)}{p.row}" if right > dc else None

        p = _choose_sheet_first(rng, [q for q in eligible if merge_ref(q)])
        rng_ref = merge_ref(p)
        assert rng_ref is not None

        def edit(wb: Any, ref: str = rng_ref, p: Pos = p) -> None:
            wb[p.sheet].merge_cells(ref)

        def verify(wf2: Any, wv: Any, ref: str = rng_ref, p: Pos = p) -> list[str]:
            return (
                []
                if any(str(m) == ref for m in wf2[p.sheet].merged_cells.ranges)
                else [f"{ref} not merged"]
            )

        plan.edit = edit
        plan.verify = verify
        plan.note = f"{p.sheet} row {p.row} ({p.item}): description cell merged {rng_ref}"

    elif cls == "hidden_row":
        p = _choose_sheet_first(rng, eligible)

        def edit(wb: Any, p: Pos = p) -> None:
            wb[p.sheet].row_dimensions[p.row].hidden = True

        def verify(wf: Any, wv: Any, p: Pos = p) -> list[str]:
            return [] if wf[p.sheet].row_dimensions[p.row].hidden else ["row not hidden"]

        plan.edit, plan.verify = edit, verify
        plan.note = f"{p.sheet} row {p.row} ({p.item}) hidden"

    elif cls == "no_cache":
        plan.kind = "nocache"
        plan.edit = lambda wb: None
        plan.note = "clean bid re-saved by openpyxl: every formula without a cached value"

        def verify(wf: Any, wv: Any) -> list[str]:
            vals = [
                wv[p.sheet].cell(p.row, lays[p.sheet].cols["amount"]).value for p in eligible[:20]
            ]
            return [] if all(v is None for v in vals) else ["cached values still present"]

        plan.verify = verify

    elif cls == "reconcile":
        sheets = sorted(
            s
            for s, lay in lays.items()
            if lay.subtotal_row and any(p.eligible for p in lay.positions)
        )
        sheet = _choose(rng, sheets)
        lay = lays[sheet]
        p = _choose(rng, [q for q in lay.positions if q.eligible])
        wf = openpyxl.load_workbook(base.path)
        assert lay.subtotal_row is not None
        old = wf[sheet].cell(lay.subtotal_row, lay.cols["amount"]).value
        if not _is_formula(old):
            raise NoCandidate
        new_f = f"{old}-{_col(lay, 'amount')}{p.row}"
        row_s = lay.subtotal_row
        plan.edit = lambda wb: setattr(wb[sheet].cell(row_s, lay.cols["amount"]), "value", new_f)
        plan.verify = _cell_check(sheet, row_s, lay.cols["amount"], new_f)
        plan.subtotal_delta = {sheet: -p.cost}
        stated = base.subtotals.get(sheet, 0.0) - p.cost
        lines = base.subtotals.get(sheet, 0.0)
        plan.expect = [Exp("BOQ.RECONCILE", sheet, None, row_s, stated, lines)]
        plan.allow = [Allow("BOQ.RECONCILE", None, summary=True)]
        plan.note = f"{sheet}: SUB-TOTAL row {row_s} formula {old} -> {new_f} (misses {p.item})"

    elif cls == "filled_blank":
        cands = [q for q in base.positions if not q.qty and q.rate > 0 and q.amount_formula]
        p = _choose_sheet_first(rng, cands)
        lay = lays[p.sheet]
        qty = float(rng.choice([1, 6, 12]))
        amount = qty * p.rate
        plan.edit = lambda wb: setattr(wb[p.sheet].cell(p.row, lay.cols["qty"]), "value", qty)
        plan.verify = _cell_check(p.sheet, p.row, lay.cols["qty"], qty)
        plan.touched = {(p.sheet, p.row)}
        plan.cost_delta = amount
        plan.subtotal_delta = {p.sheet: amount}
        plan.expect = [
            Exp(
                "BOQ.FILLED_BLANK",
                p.sheet,
                p.item,
                p.row,
                struct=lambda c, p=p, qty=qty, amount=amount: any(
                    d.sheet == p.sheet
                    and _item_str(d.item_no) == p.item
                    and _feq(d.bid_qty, qty)
                    and (getattr(d, "amount", None) is None or abs(d.amount - amount) <= MONEY_TOL)
                    for d in getattr(c, "filled_blank", [])
                ),
            )
        ]
        plan.note = f"{p.sheet} row {p.row} ({p.item}): blank template quantity filled with {qty} (amount {amount:,.2f})"
    else:
        raise ValueError(cls)
    return plan


def _feq(a: object, b: object) -> bool:
    x, y = _number(a), _number(b)
    return x is not None and y is not None and abs(x - y) <= 1e-9 * max(1.0, abs(y))


# ------------------------------------------------------------------------------------------
# validation of a mutant (harness side)


def validate(plan: Plan) -> list[str]:
    assert plan.path is not None
    base = plan.base
    wf = openpyxl.load_workbook(plan.path)
    wv = openpyxl.load_workbook(plan.path, data_only=True)
    probs: list[str] = []
    if plan.verify:
        probs += plan.verify(wf, wv)
    if plan.kind == "nocache":
        return probs
    sheets = [*base.layouts, base.summary.sheet]
    for name in sheets:
        for row in wv[name].iter_rows():
            for c in row:
                if isinstance(c.value, str) and (
                    c.value in SPREADSHEET_ERRORS or c.value.startswith("Err:")
                ):
                    probs.append(f"{name}!{c.coordinate} holds {c.value}")
                    break
    for p in base.positions:
        if (p.sheet, p.row) in plan.touched or not p.amount_formula:
            continue
        r = plan.row_map(p.sheet, p.row)
        if r is None:
            continue
        lay = base.layouts[p.sheet]
        got = _number(wv[p.sheet].cell(r, lay.cols["amount"]).value)
        want = base.amounts.get((p.sheet, p.row))
        if got is None or want is None or abs(got - want) > MONEY_TOL:
            probs.append(f"untouched {p.sheet} {p.item}: amount {got} != clean {want}")
    for name, lay in base.layouts.items():
        if not lay.subtotal_row or name in plan.skip_subtotal:
            continue
        r = plan.row_map(name, lay.subtotal_row)
        if r is None:
            continue
        got = _number(wv[name].cell(r, lay.cols["amount"]).value)
        want = base.subtotals.get(name, 0.0) + plan.subtotal_delta.get(name, 0.0)
        if got is None or abs(got - want) > MONEY_TOL:
            probs.append(f"{name} SUB-TOTAL cached {got} != expected {want:,.2f}")
    s = base.summary
    dr, dc = plan.summary_offset
    sws = wv[s.sheet]
    incl = _number(sws.cell(s.incl_row + dr, s.value_col + dc).value)
    total = _number(sws.cell(s.total_row + dr, s.value_col + dc).value)
    if not incl or not total or abs(incl / total - base.markup) > 1e-9:
        probs.append(f"Summary markup {incl}/{total} != {base.markup:.6f}")
    if plan.same_stated and (
        incl is None or base.stated_incl is None or abs(incl - base.stated_incl) > MONEY_TOL
    ):
        probs.append(f"Summary incl. VAT {incl} != clean {base.stated_incl}")
    return probs


# ------------------------------------------------------------------------------------------
# scoring (product side)


def _num_text(s: object) -> float | None:
    if isinstance(s, int | float) and not isinstance(s, bool):
        return float(s)
    if s is None:
        return None
    t = str(s).replace(",", "").replace(" ", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _val_ok(got: object, want: object) -> bool:
    if want is None:
        return True
    if isinstance(want, float):
        g = _num_text(got)
        return g is not None and abs(g - want) <= max(MONEY_TOL, abs(want) * 1e-9)
    return got is not None and _fold(str(got)) == _fold(str(want))


def _mentions(f: Any, sheet: str | None) -> bool:
    msg = f.message_en or ""
    return sheet is None or sheet in msg


def _loc_ok(f: Any, e: Exp) -> bool:
    msg = f.message_en or ""
    if e.item is None and e.row is None:
        return True
    if e.item and f"({e.item})" in msg:
        return True
    return e.row is not None and re.search(rf"\brow {e.row}\b", msg) is not None


def _names_no_sheet(f: Any, sheets: list[str]) -> bool:
    msg = f.message_en or ""
    return not any(s in msg for s in sheets)


@dataclass
class Result:
    cls: str
    idx: int
    base: str
    note: str
    valid: bool = True
    invalid: list[str] = field(default_factory=list)
    error: str | None = None
    detected: bool = False
    exact: bool = False
    fps: list[tuple[str, str]] = field(default_factory=list)
    ctrl_fps: list[tuple[str, str]] = field(default_factory=list)
    total_ok: bool = False
    merged_ok: bool = False
    merged_probs: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    evaluated_total: float | None = None
    expected_total: float | None = None
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return (
            self.valid
            and not self.error
            and self.exact
            and not self.fps
            and self.total_ok
            and self.merged_ok
            and not self.ctrl_fps
        )


def score(plan: Plan, tmpl: Any, ctrl_book: Any, tmp: Path) -> Result:
    from procurement_agent.checks import boq as B

    t0 = time.time()
    base = plan.base
    res = Result(plan.cls, plan.idx, base.bid, plan.note)
    assert plan.path is not None
    bid = B.read_boq(plan.path, name="bid")
    comps = B.compare_all(tmpl, [bid, ctrl_book])
    comp, ctrl = comps[0], comps[1]
    findings = list(comp.findings)
    sheets = list(base.layouts)
    used: set[int] = set()
    exact_all = True
    detected_all = True
    nocache_flag = any(f.rule_id == "BOQ.NO_CACHED_VALUES" for f in findings)
    expect = plan.expect
    if plan.kind == "nocache" and nocache_flag:
        expect = [Exp("BOQ.NO_CACHED_VALUES")]
    for e in expect:
        cands = [
            i
            for i, f in enumerate(findings)
            if i not in used and f.rule_id == e.rule and (_mentions(f, e.sheet) or e.sheet is None)
        ]
        exact = [
            i
            for i in cands
            if _loc_ok(findings[i], e)
            and _val_ok(findings[i].expected, e.expected)
            and _val_ok(findings[i].actual, e.actual)
        ]
        struct_ok = True
        if e.struct is not None:
            try:
                struct_ok = bool(e.struct(comp))
            except Exception as exc:  # the product changed a field under us
                struct_ok = False
                res.misses.append(f"{e.rule}: structured check raised {exc!r}")
        if not cands:
            detected_all = False
            exact_all = False
            res.misses.append(f"missing {e.rule} on {e.sheet} {e.item or ''}")
            continue
        if exact:
            used.add(exact[0])
        else:
            used.add(cands[0])
            exact_all = False
            f = findings[cands[0]]
            res.misses.append(
                f"inexact {e.rule}: want {e.expected!r}->{e.actual!r} at {e.item or e.row}; got {f.expected!r}->{f.actual!r}: {f.message_en[:140]}"
            )
        if not struct_ok:
            exact_all = False
            res.misses.append(f"{e.rule}: structured list on the comparison does not carry it")
    for i, f in enumerate(findings):
        if i in used:
            continue
        if not any(a.admits(f, sheets) for a in plan.allow):
            res.fps.append((f.rule_id, (f.message_en or "")[:160]))
    res.ctrl_fps = [(f.rule_id, (f.message_en or "")[:160]) for f in ctrl.findings]
    res.detected = detected_all
    res.exact = exact_all
    res.expected_total = (base.cost + plan.cost_delta) * base.markup
    res.evaluated_total = comp.evaluated_total
    if plan.kind == "nocache" and nocache_flag:
        res.total_ok = True  # not evaluable: reported, not scored
    else:
        res.total_ok = (
            comp.evaluated_total is not None
            and abs(comp.evaluated_total - res.expected_total) <= MONEY_TOL
        )
        if not res.total_ok:
            res.misses.append(
                f"evaluated total {comp.evaluated_total} != gold {res.expected_total:,.2f}"
            )
    if plan.same_stated:
        st = getattr(comp, "stated_incl_vat", None)
        if st is None or base.stated_incl is None or abs(st - base.stated_incl) > MONEY_TOL:
            res.total_ok = False
            res.misses.append(
                f"stated incl. VAT read as {st}, the Summary states {base.stated_incl}"
            )
    if plan.cls in NEGATIVE:  # nothing to find: exact means no finding and the gold total
        res.exact = not res.fps and res.total_ok
    res.merged_probs = check_merged(B, tmpl, [bid, ctrl_book], comps, plan, res, tmp)
    res.merged_ok = not res.merged_probs
    res.seconds = time.time() - t0
    return res


def check_merged(
    B: Any, tmpl: Any, bids: list[Any], comps: list[Any], plan: Plan, res: Result, tmp: Path
) -> list[str]:
    out = tmp / f"merged-{plan.cls}-{plan.idx}-{plan.base.bid}.xlsx"
    B.write_merged_workbook(tmpl, bids, comps, out)
    wb = openpyxl.load_workbook(out, data_only=True)
    probs: list[str] = []
    marker = "— template —"
    line_sheets = [
        ws
        for ws in wb.worksheets
        if any(ws.cell(r, 5).value == marker for r in range(1, min(ws.max_row, 400) + 1))
    ]
    t_names = list(tmpl.sheets)
    if len(line_sheets) != len(t_names):
        probs.append(
            f"{len(line_sheets)} line sheets in the merged workbook for {len(t_names)} template sheets"
        )
    harness_counts = {name: len(lay.positions) for name, lay in plan.base.layouts.items()}
    for ws, name in zip(line_sheets, t_names, strict=False):
        blocks: list[tuple[str, set[str]]] = []
        for r in range(1, ws.max_row + 1):
            who = ws.cell(r, 5).value
            if who == marker:
                blocks.append((str(ws.cell(r, 14).value or ""), set()))
            elif blocks and who:
                blocks[-1][1].add(str(who))
            elif blocks and who is None and ws.cell(r, 1).value is None:
                blocks.append(("", set()))  # a gap ends the block (the added-lines block follows)
        positions = [b for b in blocks if b[0] == "position"]
        if name in harness_counts and len(positions) != harness_counts[name]:
            probs.append(
                f"{name}: {len(positions)} template positions in the merged sheet, harness counts {harness_counts[name]}"
            )
        lacking = sum(1 for _, who in positions if "bid" not in who or "control" not in who)
        if lacking:
            probs.append(f"{name}: {lacking} template position(s) without a row for each bidder")
    board = None
    for ws in wb.worksheets:
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 200)):
            cols = {str(c.value).strip(): c.column for c in row if isinstance(c.value, str)}
            if "Evaluated total" in cols:
                board = (ws, row[0].row, cols)
                break
        if board:
            break
    if board is None:
        probs.append("no leaderboard with an 'Evaluated total' column")
        return probs
    ws, hdr, cols = board
    bidder_col = next((c for t, c in cols.items() if t.lower().startswith("bidder")), 2)
    got = "absent"
    for r in range(hdr + 1, min(ws.max_row, hdr + 50) + 1):
        if ws.cell(r, bidder_col).value == "bid":
            got = ws.cell(r, cols["Evaluated total"]).value
            break
    if got == "absent":
        probs.append("bidder missing from the leaderboard")
    elif (
        plan.kind == "nocache"
        and got is None
        and any(f.rule_id == "BOQ.NO_CACHED_VALUES" for f in comps[0].findings)
    ):
        pass
    elif (
        _num_text(got) is None
        or res.expected_total is None
        or abs(_num_text(got) - res.expected_total) > MONEY_TOL
    ):  # type: ignore[operator]
        added = getattr(plan, "checks_added", None)
        why = ""
        if (
            added is not None
            and _num_text(got) is not None
            and res.expected_total is not None
            and abs(_num_text(got) - res.expected_total - added * plan.base.markup) <= 1
        ):  # type: ignore[operator]
            why = " (the added line was counted)"
        probs.append(f"leaderboard evaluated total {got} != gold {res.expected_total:,.2f}{why}")
    return probs


# ------------------------------------------------------------------------------------------
# driver


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}" if d else "  n/a"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--per-class", type=int, default=5)
    ap.add_argument("--classes", default=",".join(CLASSES))
    ap.add_argument(
        "--out", type=Path, default=None, help="default eval_runs/mutate_boq-<seed>.json"
    )
    ap.add_argument("--keep-failures", type=Path, default=ROOT / "samples/mutations/boq")
    ap.add_argument(
        "--keep-all", action="store_true", help="keep every mutant, not only the failures"
    )
    ap.add_argument("--rebuild-base", action="store_true")
    ap.add_argument("--show", action="store_true", help="print every case's planted change")
    args = ap.parse_args(argv)

    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    unknown = sorted(set(classes) - set(CLASSES))
    if unknown:
        ap.error(f"unknown classes: {', '.join(unknown)}")
    keep_root = args.keep_failures.resolve()
    if ROOT / "samples" not in (keep_root, *keep_root.parents):
        ap.error("--keep-failures must be under samples/ (mutants carry client values)")
    work = keep_root / f"seed-{args.seed}"
    if work.exists():
        for old in work.glob("c-*.xlsx"):
            old.unlink()
    work.mkdir(parents=True, exist_ok=True)

    templates = sorted(ROOT.glob("samples/*/BOQ/*BOQ.xlsx"))
    if not templates:
        print("no templates under samples/*/BOQ/", file=sys.stderr)
        return 2
    t_start = time.time()
    bases = []
    for i, t in enumerate(templates):
        crc = zlib.crc32(t.read_bytes()) & 0xFFFFFFFF
        bases.append(
            build_base(f"b{i}", t, keep_root / "base" / f"b{i}-{crc:08x}.xlsx", args.rebuild_base)
        )
    print(f"bases built in {time.time() - t_start:.0f}s")

    base_report = {}
    for b in bases:
        chk = check_base(b)
        base_report[b.bid] = chk
        flag = (
            "CLEAN"
            if not chk["base_findings"]
            and chk["total_ok"]
            and not chk["only_harness"]
            and not chk["only_reader"]
            else "NOT CLEAN"
        )
        print(
            f"{b.bid}: {flag}  positions harness {chk['positions_harness']} / reader {chk['positions_reader']}; "
            f"clean-bid findings {len(chk['base_findings'])}; template findings {Counter(chk['template_findings'])}; "
            f"evaluated {chk['evaluated_total']} vs gold {chk['expected_total']:,.2f}; stated markup ok {chk['stated_markup_ok']}; cached amounts ok {chk['amounts_ok']}"
        )
        for rule, msg in chk["base_findings"][:10]:
            print(f"    clean-bid finding {rule}: {msg[:150]}")
        for s, r in chk["only_harness"][:5]:
            print(f"    only the harness sees a position at {s} row {r}")
        for s, r in chk["only_reader"][:5]:
            print(f"    only the reader sees a position at {s} row {r}")

    plans: list[Plan] = []
    skipped: Counter[str] = Counter()
    for cls in classes:
        for idx in range(args.per_class):
            base = bases[idx % len(bases)]
            try:
                plan = plan_case(cls, idx, base, args.seed)
            except NoCandidate:
                skipped[cls] += 1
                continue
            plan.path = work / f"c-{cls}-{idx}-{base.bid}.xlsx"
            plans.append(plan)

    cell_paths: list[Path] = []
    uno_jobs: list[dict[str, Any]] = []
    for plan in plans:
        assert plan.path is not None
        if plan.kind == "uno":
            uno_jobs.append({"in": str(plan.base.path), "out": str(plan.path), "ops": plan.ops})
            continue
        try:
            wb = openpyxl.load_workbook(plan.base.path)
            if plan.edit:
                plan.edit(wb)
            wb.save(plan.path)
        except Exception as exc:  # a harness defect: reported as invalid, the run goes on
            plan.edit_error = repr(exc)  # type: ignore[attr-defined]
            continue
        if plan.kind == "cells":
            cell_paths.append(plan.path)
    t1 = time.time()
    recalc(cell_paths)
    run_uno(uno_jobs)
    print(f"{len(plans)} mutants written and recalculated in {time.time() - t1:.0f}s")

    from procurement_agent.checks import boq as B

    tmpls = {b.bid: B.read_boq(b.template, name="template") for b in bases}
    ctrls = {b.bid: B.read_boq(b.path, name="control") for b in bases}
    results: list[Result] = []
    with tempfile.TemporaryDirectory() as tmp:
        for plan in plans:
            try:
                probs = (
                    [f"edit failed: {plan.edit_error}"]
                    if getattr(plan, "edit_error", None)
                    else validate(plan)
                )
            except Exception as exc:
                probs = [f"validation crashed: {exc!r}"]
            if probs:
                results.append(
                    Result(plan.cls, plan.idx, plan.base.bid, plan.note, valid=False, invalid=probs)
                )
            else:
                try:
                    results.append(
                        score(plan, tmpls[plan.base.bid], ctrls[plan.base.bid], Path(tmp))
                    )
                except Exception as exc:
                    r = Result(plan.cls, plan.idx, plan.base.bid, plan.note, error=f"{exc!r}")
                    r.misses.append(traceback.format_exc(limit=3).splitlines()[-1])
                    results.append(r)
            if args.show:
                last = results[-1]
                print(
                    f"  [{'PASS' if last.passed else 'FAIL'}] {plan.cls}#{plan.idx} {plan.base.bid}: {plan.note}"
                )

    # ---- table
    print()
    print(f"BOQ mutation harness  seed={args.seed}  per-class={args.per_class}  bases={len(bases)}")
    hdr = f"{'class':15} {'n':>3} {'inv':>3} {'err':>3} {'detect%':>7} {'exact%':>7} {'FP':>4} {'total%':>7} {'merged%':>8} {'ctrlFP':>6} {'pass%':>6}"
    print(hdr)
    print("-" * len(hdr))
    agg: dict[str, Any] = {}
    for cls in classes:
        rs = [r for r in results if r.cls == cls]
        ok = [r for r in rs if r.valid and not r.error]
        n = len(ok)
        det = sum(r.detected for r in ok)
        exa = sum(r.exact for r in ok)
        fp = sum(len(r.fps) for r in ok)
        tot = sum(r.total_ok for r in ok)
        mer = sum(r.merged_ok for r in ok)
        cfp = sum(len(r.ctrl_fps) for r in ok)
        pas = sum(r.passed for r in ok)
        neg = cls in NEGATIVE
        print(
            f"{cls:15} {n:>3} {len(rs) - len([r for r in rs if r.valid]):>3} {sum(1 for r in rs if r.error):>3} "
            f"{'  (neg)' if neg else _pct(det, n):>7} {_pct(exa, n):>7} {fp:>4} {_pct(tot, n):>7} {_pct(mer, n):>8} {cfp:>6} {_pct(pas, n):>6}"
        )
        agg[cls] = {
            "n": n,
            "invalid": sum(1 for r in rs if not r.valid),
            "errors": sum(1 for r in rs if r.error),
            "skipped_no_candidate": skipped.get(cls, 0),
            "negative_class": neg,
            "detected": None if neg else (det / n if n else None),
            "exact": exa / n if n else None,
            "false_positives": fp,
            "fp_rules": dict(Counter(rule for r in ok for rule, _ in r.fps)),
            "total_ok": tot / n if n else None,
            "merged_ok": mer / n if n else None,
            "control_false_positives": cfp,
            "pass": pas / n if n else None,
        }
    allv = [r for r in results if r.valid and not r.error]
    print("-" * len(hdr))
    print(
        f"{'ALL':15} {len(allv):>3} {sum(1 for r in results if not r.valid):>3} {sum(1 for r in results if r.error):>3} "
        f"{'':>7} {_pct(sum(r.exact for r in allv), len(allv)):>7} {sum(len(r.fps) for r in allv):>4} "
        f"{_pct(sum(r.total_ok for r in allv), len(allv)):>7} {_pct(sum(r.merged_ok for r in allv), len(allv)):>8} "
        f"{sum(len(r.ctrl_fps) for r in allv):>6} {_pct(sum(r.passed for r in allv), len(allv)):>6}"
    )
    print(
        "negative classes (neg): pass = no unexpected finding and the gold total; 'exact%' there means exactly that."
    )
    if skipped:
        print(f"no candidate position for: {dict(skipped)}")

    # ---- failure examples
    fails = [r for r in results if not r.passed]
    if fails:
        print("\nfailures (up to 3 per class):")
        shown: Counter[str] = Counter()
        for r in fails:
            if shown[r.cls] >= 3:
                continue
            shown[r.cls] += 1
            print(f"- {r.cls}#{r.idx} {r.base}: {r.note}")
            for p in r.invalid[:4]:
                print(f"    INVALID (harness): {p}")
            if r.error:
                print(f"    ERROR: {r.error}")
            for m in r.misses[:4]:
                print(f"    miss: {m}")
            for rule, msg in r.fps[:4]:
                print(f"    FP {rule}: {msg}")
            for rule, msg in r.ctrl_fps[:2]:
                print(f"    control FP {rule}: {msg}")
            for m in r.merged_probs[:3]:
                print(f"    merged: {m}")

    # ---- keep / clean mutants, write outputs
    failed_paths = {f"c-{r.cls}-{r.idx}-{r.base}.xlsx" for r in fails}
    if not args.keep_all:
        for p in work.glob("c-*.xlsx"):
            if p.name not in failed_paths:
                p.unlink()
    details = [
        dict(r.__dict__) | {"passed": r.passed, "file": f"c-{r.cls}-{r.idx}-{r.base}.xlsx"}
        for r in results
    ]
    (work / "details.json").write_text(
        json.dumps(
            {
                "bases": {b.bid: str(b.template.relative_to(ROOT)) for b in bases},
                "base_checks": base_report,
                "cases": details,
            },
            ensure_ascii=False,
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    out = args.out or ROOT / "eval_runs" / f"mutate_boq-{args.seed}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "harness": "mutate_boq",
                "seed": args.seed,
                "per_class": args.per_class,
                "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "bases": {
                    b.bid: {
                        "clean": not base_report[b.bid]["base_findings"]
                        and base_report[b.bid]["total_ok"],
                        "clean_bid_finding_rules": sorted(
                            {r for r, _ in base_report[b.bid]["base_findings"]}
                        ),
                        "positions_harness": base_report[b.bid]["positions_harness"],
                        "positions_reader": base_report[b.bid]["positions_reader"],
                        "position_disagreements": len(base_report[b.bid]["only_harness"])
                        + len(base_report[b.bid]["only_reader"]),
                    }
                    for b in bases
                },
                "classes": agg,
                "overall": {
                    "n": len(allv),
                    "pass": sum(r.passed for r in allv) / len(allv) if allv else None,
                    "invalid": sum(1 for r in results if not r.valid),
                    "errors": sum(1 for r in results if r.error),
                },
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(
        f"\nwrote {out.relative_to(ROOT)} (aggregates) and {work.relative_to(ROOT)}/details.json (per case); "
        f"{len(failed_paths)} failing mutant(s) kept in {work.relative_to(ROOT)}/"
    )
    print(f"total {time.time() - t_start:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
