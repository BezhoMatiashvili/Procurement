#!/usr/bin/env python3
"""Score the product's BOQ comparison (checks/boq.py) against a BOQ gold file.

The gold is written by ``scripts/boq_reference.py`` (docs/GOLD_SPEC.md section 5). This script
runs the product on the same template and bids, compares every list and total per bidder, and
checks the merged workbook (every template position present for every bidder).

Usage (from the repo root):
    uv run --offline python scripts/evaluate_boq.py samples/gold_boq/<set>.json [--out result.json]
        [--details N]

Exit code 1 on any mismatch. Nothing about a particular project is hard-coded.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

NOT_IMPL = "not implemented"
#: the product differs from GOLD_SPEC on a point where it deliberately follows another policy
#: (placeholders optional, section groups, parent rows); still a failure until the spec rules on it
POLICY = "POLICY"


#: Quote characters and dashes, by code point so the source stays plain ASCII punctuation.
_QUOTES = dict.fromkeys(
    (0x201E, 0x201C, 0x201D, 0x22, 0x27, 0x2018, 0x2019, 0xAB, 0xBB, 0x201A), None
)
_DASHES = dict.fromkeys((0x2013, 0x2014, 0x2011, 0x2212), "-")


def fold(value: object) -> str:
    if value is None:
        return ""
    s = unicodedata.normalize("NFC", str(value))
    s = s.translate(_QUOTES)
    s = s.translate(_DASHES)
    s = re.sub(r"\s+", " ", s.replace(chr(0xA0), " ")).strip()
    return "".join(
        ch.upper() if ch.isascii() or unicodedata.name(ch, "").startswith("LATIN") else ch
        for ch in s
    )


def money_eq(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    a, b = float(a), float(b)
    # gold is stored rounded to the cent; a raw value that sits on the half cent (x.585) rounds
    # either way depending on float summation order, so the raw values may differ by 0.005
    return abs(round(a, 2) - round(b, 2)) < 0.0051 or abs(a - b) <= 0.0051


def qty_key(x) -> str:
    return "-" if x is None else f"{float(x):.6g}"


def item_of(line) -> str:
    it = getattr(line, "item_no", None)
    if it:
        return str(it).strip()
    key = getattr(line, "key", "") or ""
    return key.split("|")[0].strip()


class Scorer:
    def __init__(self, gold: dict, details: int):
        self.gold = gold
        self.details = details
        self.rows: list[dict] = []
        self.placeholders = {
            (s, p["item"])
            for s, ps in gold["positions"].items()
            for p in ps
            if p.get("placeholder")
        }
        # section groups: a position "A<n>" and the positions numbered "<n>.k" under it
        self.group_of: dict[tuple[str, str], str] = {}
        for s, ps in gold["positions"].items():
            parent = None
            for p in ps:
                m = re.fullmatch(r"[A-Z](\d+)", p["item"])
                if m:
                    parent = (p["item"], m.group(1))
                    self.group_of[(s, p["item"])] = p["item"]
                elif parent and p["item"].startswith(parent[1] + "."):
                    self.group_of[(s, p["item"])] = parent[0]
        self.priced_groups: set[tuple[str, str]] = set()  # (sheet, group) the current bidder priced

    def set_bidder(self, bidder: str) -> None:
        """Remember which section groups the bidder priced (gold side), for the group tag."""
        g = self.gold["bids"][bidder]
        costs = g.get("position_costs") or {}
        self.priced_groups = set()
        for s, ps in self.gold["positions"].items():
            for p in ps:
                grp = self.group_of.get((s, p["item"]))
                if grp and costs.get(f"{s}|{p['row']}"):
                    self.priced_groups.add((s, grp))

    def policy(self, key, field: str, side: str) -> str | None:
        """The spec-vs-product divergence that explains one differing key, if any.

        GOLD_SPEC section 5 is the definition of correct; these are places where the product
        deliberately does something else and the spec has no ruling yet. They still fail."""
        if not isinstance(key, tuple) or len(key) < 2:
            return None
        k = (key[0], key[1])
        if k in self.placeholders:
            return "placeholder"
        if field == "unpriced_positions" and side == "missing":
            grp = self.group_of.get(k)
            if grp and (k[0], grp) in self.priced_groups:
                return "section group"
        return None

    def sets(self, bidder: str, field: str, gold_keys, prod_keys) -> None:
        if prod_keys is None:
            self.rows.append(
                {
                    "bidder": bidder,
                    "field": field,
                    "status": NOT_IMPL,
                    "gold": len(gold_keys),
                    "product": None,
                    "missing": [],
                    "unexpected": [],
                }
            )
            return
        g, p = set(gold_keys), set(prod_keys)
        miss, unexp = sorted(g - p, key=str), sorted(p - g, key=str)
        tags = [self.policy(m, field, "missing") for m in miss] + [
            self.policy(u, field, "unexpected") for u in unexp
        ]
        status = "OK" if not tags else POLICY if all(tags) else "MISMATCH"

        def fmt(k: object, side: str) -> str:
            t = self.policy(k, field, side)
            return f"{k}" + (f" [policy: {t}]" if t else "")

        self.rows.append(
            {
                "bidder": bidder,
                "field": field,
                "status": status,
                "gold": len(g),
                "product": len(p),
                "missing": [fmt(m, "missing") for m in miss],
                "unexpected": [fmt(u, "unexpected") for u in unexp],
            }
        )

    def value(
        self,
        bidder: str,
        field: str,
        gold_v,
        prod_v,
        *,
        implemented: bool = True,
        rel: bool = False,
    ) -> None:
        if not implemented:
            self.rows.append(
                {
                    "bidder": bidder,
                    "field": field,
                    "status": NOT_IMPL,
                    "gold": gold_v,
                    "product": None,
                    "missing": [],
                    "unexpected": [],
                }
            )
            return
        if rel:
            ok = (gold_v is None and prod_v is None) or (
                gold_v is not None
                and prod_v is not None
                and abs(float(gold_v) - float(prod_v)) <= 1e-6 * max(1.0, abs(float(gold_v)))
            )
        elif isinstance(gold_v, (int, float)) or isinstance(prod_v, (int, float)):
            ok = money_eq(gold_v, prod_v)
        else:
            ok = gold_v == prod_v
        pv = round(prod_v, 6) if isinstance(prod_v, float) else prod_v
        self.rows.append(
            {
                "bidder": bidder,
                "field": field,
                "status": "OK" if ok else "MISMATCH",
                "gold": gold_v,
                "product": pv,
                "missing": [],
                "unexpected": [],
            }
        )


def unit_changes_from_findings(comp, sheet_names: list[str]):
    out = []
    for f in getattr(comp, "findings", []) or []:
        if getattr(f, "rule_id", "") != "BOQ.UNIT_CHANGED":
            continue
        msg = getattr(f, "message_en", "") or ""
        sheet = next(
            (s for s in sorted(sheet_names, key=len, reverse=True) if msg.startswith(s)), "?"
        )
        m = re.search(r"\(([^)]*)\)", msg[len(sheet) :] if sheet != "?" else msg)
        item = m.group(1).strip() if m else "?"
        out.append((sheet, item, fold(getattr(f, "expected", "")), fold(getattr(f, "actual", ""))))
    return out


def check_merged(boq, tpl, bids, comps, gold) -> dict:
    """Every template position must appear with every bidder under it in the merged workbook."""
    import openpyxl

    res = {"status": "OK", "missing": [], "sheets_missing": [], "error": None, "found": 0}
    try:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "merged.xlsx"
            boq.write_merged_workbook(tpl, bids, comps, out)
            wb = openpyxl.load_workbook(out, data_only=True)
            bidders = list(gold["bids"])
            for sname, positions in gold["positions"].items():
                if sname not in wb.sheetnames:
                    res["sheets_missing"].append(sname)
                    continue
                ws = wb[sname]
                header = [fold(c.value) for c in ws[1]]
                item_c = next((i for i, h in enumerate(header) if h in ("ITEM", "NO", "NO.")), 0)
                bid_c = next((i for i, h in enumerate(header) if h.startswith("BIDDER")), None)
                desc_c = next((i for i, h in enumerate(header) if h.startswith("DESCRIPTION")), 1)
                if bid_c is None:
                    res["error"] = f"{sname}: no Bidder column in the merged sheet"
                    continue
                blocks: list[tuple[str, str, set]] = []
                for row in ws.iter_rows(min_row=2, values_only=True):
                    who = str(row[bid_c] or "").strip()
                    it = str(row[item_c] or "").strip()
                    if it or (row[desc_c] and not who.strip()) or "TEMPLATE" in fold(who):
                        blocks.append((it, fold(row[desc_c]), set()))
                    if who and blocks and "TEMPLATE" not in fold(who):
                        blocks[-1][2].add(who)
                used: set[int] = set()
                for p in positions:
                    cand = [
                        i
                        for i, (it, ds, _) in enumerate(blocks)
                        if it == p["item"] and i not in used
                    ]
                    if len(cand) > 1:
                        pd = fold(p.get("description", ""))
                        cand = [i for i in cand if blocks[i][1].startswith(pd[:30])] or cand
                    if not cand:
                        res["missing"].append(
                            f"{sname} {p['item']} (row {p['row']}): no template line"
                        )
                        continue
                    used.add(cand[0])
                    lacking = [b for b in bidders if b not in blocks[cand[0]][2]]
                    res["found"] += len(bidders) - len(lacking)
                    if lacking:
                        res["missing"].append(
                            f"{sname} {p['item']} (row {p['row']}): no line for {lacking}"
                        )
    except Exception as exc:  # the product is being changed concurrently: report, don't crash
        res["error"] = f"{type(exc).__name__}: {exc}"
    if res["missing"] or res["sheets_missing"] or res["error"]:
        res["status"] = "MISMATCH"
    return res


def evaluate(gold: dict, details: int) -> dict:
    boq = importlib.import_module("procurement_agent.checks.boq")
    tpl_path = Path(gold["template"])
    tpl = boq.read_boq(tpl_path, name=tpl_path.stem)
    names = list(gold["bids"])
    bids = [boq.read_boq(Path(gold["bids"][n]["path"]), name=n) for n in names]
    comps = boq.compare_all(tpl, bids)
    by_name = {getattr(c, "bidder", None): c for c in comps}
    sc = Scorer(gold, details)
    sheet_names = list(gold["positions"])
    tpl_positions = {(s, p["row"]): p for s, ps in gold["positions"].items() for p in ps}

    for n, book in zip(names, bids, strict=True):
        g = gold["bids"][n]
        c = by_name.get(n) or comps[names.index(n)]
        sc.set_bidder(n)
        sc.sets(
            n,
            "quantity_changed",
            [
                (x["sheet"], x["item"], qty_key(x["template_qty"]), qty_key(x["bid_qty"]))
                for x in g["quantity_changed"]
            ],
            [
                (d.sheet, item_of(d), qty_key(d.template_qty), qty_key(d.bid_qty))
                for d in c.quantity_changed
            ],
        )
        uc = getattr(c, "unit_changed", None)
        prod_uc = (
            [
                (
                    d.sheet,
                    item_of(d),
                    fold(getattr(d, "template_unit", "")),
                    fold(getattr(d, "bid_unit", "")),
                )
                for d in uc
            ]
            if uc is not None
            else unit_changes_from_findings(c, sheet_names)
        )
        sc.sets(
            n,
            "unit_changed",
            [
                (x["sheet"], x["item"], fold(x["template_unit"]), fold(x["bid_unit"]))
                for x in g["unit_changed"]
            ],
            prod_uc,
        )
        uf_prod = getattr(c, "unit_filled", None)
        sc.sets(
            n,
            "unit_filled",
            [(x["sheet"], x["item"], fold(x["bid_unit"])) for x in g.get("unit_filled", [])],
            None if uf_prod is None else [(ln.sheet, item_of(ln), fold(ln.unit)) for ln in uf_prod],
        )
        sc.sets(
            n,
            "added",
            [(x["sheet"], x["item"], x["row"]) for x in g["added"]],
            [(ln.sheet, item_of(ln), ln.row) for ln in c.added],
        )
        sc.sets(
            n,
            "removed",
            [(x["sheet"], x["item"]) for x in g["removed"]],
            [(ln.sheet, item_of(ln)) for ln in c.removed],
        )
        sc.sets(
            n,
            "filled_blank",
            [(x["sheet"], x["item"]) for x in g["filled_blank"]],
            [(d.sheet, item_of(d)) for d in c.filled_blank],
        )
        gfa = {(x["sheet"], x["item"]): x["amount"] for x in g["filled_blank"]}
        bad = [
            f"{k}: gold {gfa[k]} product {round(d.amount or 0.0, 2)}"
            for d in c.filled_blank
            for k in [(d.sheet, item_of(d))]
            if k in gfa and not money_eq(gfa[k], d.amount or 0.0)
        ]
        sc.rows.append(
            {
                "bidder": n,
                "field": "filled_blank amounts",
                "status": "MISMATCH" if bad else "OK",
                "gold": len(gfa),
                "product": len(c.filled_blank),
                "missing": bad,
                "unexpected": [],
            }
        )
        sc.sets(n, "sheets_added", g["sheets_added"], list(c.added_sheets))
        sc.sets(n, "sheets_removed", g["sheets_removed"], list(c.removed_sheets))

        def opt(attr: str, c: object = c) -> object:
            return getattr(c, attr, None)

        # every template position evaluated for this bidder, by (sheet, template row): catches a
        # reader that drops rows (e.g. the first positions of a sheet whose unit is "item")
        pe = opt("positions")
        sc.sets(
            n,
            "positions (sheet, template row)",
            [(s, p["row"]) for s, ps in gold["positions"].items() for p in ps],
            None if pe is None else [(p.sheet, p.row) for p in pe],
        )
        up = opt("unpriced_positions")
        sc.sets(
            n,
            "unpriced_positions",
            [(x["sheet"], x["item"]) for x in g["unpriced_positions"]],
            None if up is None else [(ln.sheet, item_of(ln)) for ln in up],
        )
        uf = opt("unfilled_positions")
        sc.sets(
            n,
            "unfilled_positions",
            [(x["sheet"], x["item"]) for x in g["unfilled_positions"]],
            None if uf is None else [(ln.sheet, item_of(ln)) for ln in uf],
        )
        ph = opt("priced_headings")
        sc.sets(
            n,
            "heading_priced",
            [(x["sheet"], x["item"]) for x in g["heading_priced"]],
            None if ph is None else [(ln.sheet, item_of(ln)) for ln in ph],
        )
        dc = opt("description_changed")
        sc.sets(
            n,
            "description_changed",
            [(x["sheet"], x["item"]) for x in g["description_changed"]],
            None if dc is None else [(t.sheet, item_of(t)) for t, _b in dc],
        )
        us = opt("unpriced_sheets")
        sc.sets(
            n, "unpriced_sheets", g.get("unpriced_sheets", []), None if us is None else list(us)
        )
        # per-sheet totals: the SUB-TOTAL row as read, and the sum of the bidder's own amounts
        sheets = getattr(book, "sheets", {}) or {}
        st_bad, am_bad = [], []
        pos_rows = {(s, r) for (s, r) in tpl_positions}
        prod_amounts: dict[str, float] = {}
        for t, b in getattr(c, "matched", []) or []:
            if (t.sheet, t.row) in pos_rows and isinstance(
                getattr(b, "amount", None), (int, float)
            ):
                prod_amounts[t.sheet] = prod_amounts.get(t.sheet, 0.0) + b.amount
        for ln in c.added:
            if isinstance(getattr(ln, "amount", None), (int, float)):
                prod_amounts[ln.sheet] = prod_amounts.get(ln.sheet, 0.0) + ln.amount
        for s, gv in g["sheet_subtotals"].items():
            pv = getattr(sheets.get(s), "subtotal", None)
            if not money_eq(gv, pv):
                st_bad.append(f"{s}: gold {gv} product {None if pv is None else round(pv, 2)}")
        for s, gv in g["sheet_amounts"].items():
            pv = prod_amounts.get(s, 0.0)
            if not money_eq(gv, pv):
                am_bad.append(f"{s}: gold {gv} product {round(pv, 2)}")
        sc.rows.append(
            {
                "bidder": n,
                "field": "sheet_subtotals",
                "status": "MISMATCH" if st_bad else "OK",
                "gold": len(g["sheet_subtotals"]),
                "product": len(sheets),
                "missing": st_bad,
                "unexpected": [],
            }
        )
        sc.rows.append(
            {
                "bidder": n,
                "field": "sheet_amounts",
                "status": "MISMATCH" if am_bad else "OK",
                "gold": len(g["sheet_amounts"]),
                "product": len(prod_amounts),
                "missing": am_bad,
                "unexpected": [],
            }
        )
        for key, attr in (
            ("total_cost", "stated_total_cost"),
            ("excl_vat", "stated_excl_vat"),
            ("incl_vat", "stated_incl_vat"),
        ):
            has = hasattr(c, attr)
            sc.value(n, f"stated.{key}", g["stated"][key], getattr(c, attr, None), implemented=has)
        sc.value(
            n,
            "markup_factor",
            g["markup_factor"],
            getattr(c, "markup_factor", None),
            implemented=hasattr(c, "markup_factor"),
            rel=True,
        )
        # placeholder rows the bidder priced: the gold counts them (filled_blank), the product
        # leaves them out as optional; a gap of exactly that amount is the placeholder policy
        ph_amt = sum(
            x["amount"] or 0.0
            for x in g["filled_blank"]
            if (x["sheet"], x["item"]) in sc.placeholders
        )
        for attr in (
            "evaluated_cost",
            "evaluated_total",
            "common_scope_cost",
            "common_scope_total",
        ):
            sc.value(n, attr, g.get(attr), getattr(c, attr, None), implemented=hasattr(c, attr))
            row = sc.rows[-1]
            pv, gv = getattr(c, attr, None), g.get(attr)
            if row["status"] == "MISMATCH" and ph_amt and pv is not None and gv is not None:
                factor = g.get("markup_factor") or 1.0 if attr.endswith("total") else 1.0
                if abs((gv - pv) - ph_amt * factor) <= 0.02:
                    row["status"] = POLICY
                    row["missing"] = [
                        f"gap {gv - pv:.2f} = priced placeholders {ph_amt:.2f}"
                        + (f" x markup {factor}" if factor != 1.0 else "")
                        + " [policy: placeholder]"
                    ]
        gold_colour = (
            "red"
            if n in gold["colours"]["red"]
            else "green"
            if n in gold["colours"]["green"]
            else None
        )
        sc.value(
            n, "colour", gold_colour, getattr(c, "colour", None), implemented=hasattr(c, "colour")
        )

    # the common scope (same for every bidder)
    first = comps[0] if comps else None
    pe = getattr(first, "positions", None) if first is not None else None
    if pe is None or (pe and not hasattr(pe[0], "in_common_scope")):
        sc.sets("*", "common_scope", [(x["sheet"], x["row"]) for x in gold["common_scope"]], None)
    else:
        sc.sets(
            "*",
            "common_scope",
            [(x["sheet"], x["row"]) for x in gold["common_scope"]],
            [(p.sheet, p.row) for p in pe if p.in_common_scope],
        )
    merged = check_merged(boq, tpl, bids, comps, gold)
    sc.rows.append(
        {
            "bidder": "*",
            "field": "merged workbook: position x bidder lines",
            "status": merged["status"],
            "gold": sum(len(v) for v in gold["positions"].values()) * len(gold["bids"]),
            "product": merged["found"],
            "missing": merged["missing"]
            + merged["sheets_missing"]
            + ([merged["error"]] if merged["error"] else []),
            "unexpected": [],
        }
    )
    return {"gold": None, "rows": sc.rows}


def render(rows: list[dict], details: int) -> str:
    out = [f"{'bidder':<22} {'field':<42} {'status':<16} {'gold':>12} {'product':>14}", "-" * 110]
    for r in rows:
        gv = (
            r["gold"]
            if not isinstance(r["gold"], float)
            else f"{r['gold']:.4f}".rstrip("0").rstrip(".")
        )
        pv = (
            r["product"]
            if not isinstance(r["product"], float)
            else f"{r['product']:.4f}".rstrip("0").rstrip(".")
        )
        out.append(
            f"{str(r['bidder'])[:22]:<22} {r['field'][:42]:<42} {r['status']:<16} "
            f"{str(gv)[:12]:>12} {str(pv)[:14]:>14}"
        )
        for label in ("missing", "unexpected"):
            items = r[label]
            if items and details:
                shown = items[:details]
                more = f" (+{len(items) - len(shown)} more)" if len(items) > len(shown) else ""
                out.append(f"{'':<23}{label}: " + "; ".join(map(str, shown)) + more)
    n_bad = sum(r["status"] == "MISMATCH" for r in rows)
    n_pol = sum(r["status"] == POLICY for r in rows)
    n_ni = sum(r["status"] == NOT_IMPL for r in rows)
    out.append("-" * 110)
    out.append(
        f"{len(rows)} checks: {len(rows) - n_bad - n_pol - n_ni} OK, {n_bad} MISMATCH, "
        f"{n_pol} {POLICY} (product follows a policy GOLD_SPEC does not state), {n_ni} {NOT_IMPL}"
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("gold", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument(
        "--details", type=int, default=6, help="list at most N differing keys per check"
    )
    a = ap.parse_args(argv)
    gold = json.loads(a.gold.read_text(encoding="utf-8"))
    res = evaluate(gold, a.details)
    res["gold"] = str(a.gold)
    print(f"BOQ evaluation against {a.gold}")
    print(render(res["rows"], a.details))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if any(r["status"] in ("MISMATCH", POLICY) for r in res["rows"]) else 0


if __name__ == "__main__":
    sys.exit(main())
