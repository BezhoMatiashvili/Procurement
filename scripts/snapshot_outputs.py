#!/usr/bin/env python
"""Dump what every module says about every test document, so any change can be diffed.

    uv run python scripts/snapshot_outputs.py --out eval_runs/snapshot-before.json
    uv run python scripts/snapshot_outputs.py --out eval_runs/snapshot-after.json \\
        --diff eval_runs/snapshot-before.json

The evaluation scores against gold; this records *everything* the four modules return - AOA
findings with their corrections and spans, the comparison's findings, BOQ findings and totals,
and every extracted fact - over every set found under ``samples/``. A change that moves an
output nobody labelled shows up here, which is how a fix is kept from quietly breaking something
the gold does not cover.

Sets are discovered by layout, never by file name (the client's file names are identifiers):
an ``AOA and Contract`` folder holds one contract (``*ontract*``) and acts (``*AoA*``), the
correct act being the one whose name says ``Correct``; a ``BOQ`` folder holds a template
(``*BOQ.xlsx``) and bids. The output lands in ``eval_runs/`` (git-ignored): it contains the
documents' contents.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from procurement_agent.checks.aoa import check_aoa  # noqa: E402
from procurement_agent.checks.boq import compare_all, grand_total, read_boq  # noqa: E402
from procurement_agent.checks.compare import compare  # noqa: E402
from procurement_agent.config import Settings  # noqa: E402
from procurement_agent.eval.runner import _segments_for, load_chunks_for  # noqa: E402
from procurement_agent.extraction.pipeline import chunks_text, run_extraction  # noqa: E402

SAMPLES = ROOT / "samples"
CACHE = ROOT / ".eval_cache"


def _finding(f: Any) -> dict[str, Any]:
    return {
        "rule": f.rule_id,
        "severity": f.severity,
        "expected": f.expected,
        "actual": f.actual,
        "quote": f.quote,
        "span": list(f.span) if f.span else None,
        "needs_review": f.needs_review,
        "en": f.message_en,
    }


def _skips(report: Any) -> list[str]:
    return sorted(s.rule_id for s in report.skipped)


def _aoa_sets() -> list[Path]:
    return sorted(p for p in SAMPLES.glob("*/AOA and Contract") if p.is_dir())


def _r(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _boq_sets() -> list[Path]:
    return sorted(p for p in SAMPLES.glob("*/BOQ") if p.is_dir())


def _contract_of(folder: Path) -> Path | None:
    found = sorted(folder.glob("*ontract*.docx"))
    return found[0] if found else None


def _acts_of(folder: Path) -> list[Path]:
    return sorted(folder.glob("*AoA*.docx"))


def _correct_of(acts: list[Path]) -> Path | None:
    return next((a for a in acts if "correct" in a.stem.lower()), None)


def snapshot(settings: Settings) -> dict[str, Any]:
    out: dict[str, Any] = {"aoa": {}, "compare": {}, "boq": {}, "extraction": {}}

    def text(p: Path) -> str:
        return chunks_text(load_chunks_for(p, settings, CACHE))

    for folder in _aoa_sets():
        set_name = folder.parent.name
        contract = _contract_of(folder)
        acts = _acts_of(folder)
        contract_text = text(contract) if contract else None
        for act in acts:
            rep = check_aoa(aoa_text=text(act), contract_text=contract_text)
            out["aoa"][f"{set_name}/{act.stem}"] = {
                "findings": [_finding(f) for f in rep.findings],
                "skipped": _skips(rep),
                "summary": dict(rep.summary),
            }
        correct = _correct_of(acts)
        pairs: list[tuple[Path, Path]] = []
        if correct is not None:
            pairs = [(correct, a) for a in acts if a != correct]
            if contract:
                pairs.append((contract, correct))
        for left, right in pairs:
            rep = compare(
                _segments_for(left, settings, CACHE),
                _segments_for(right, settings, CACHE),
                left_document_id="L",
                right_document_id="R",
                left_kind="docx",
                right_kind="docx",
            )
            out["compare"][f"{set_name}/{left.stem} -> {right.stem}"] = {
                "similarity": round(rep.similarity, 4),
                "findings": [_finding(f) for f in rep.findings],
                "skipped": _skips(rep),
            }
        for doc in ([contract] if contract else []) + acts:
            res = run_extraction(
                document_id="x",
                chunks=load_chunks_for(doc, settings, CACHE),
                settings=settings,
                llm=None,
            )
            out["extraction"][f"{set_name}/{doc.stem}"] = [
                {
                    "type": f.type.value,
                    "value": f.value,
                    "normalized": None if f.normalized is None else str(f.normalized),
                    "source": f.source,
                    "confidence": round(f.confidence, 3),
                    "conflict": f.conflict,
                }
                for f in res.facts
            ]

    for folder in _boq_sets():
        set_name = folder.parent.name
        templates = sorted(folder.glob("*BOQ.xlsx"))
        if not templates:
            continue
        template = read_boq(templates[0])
        bids = [read_boq(p) for p in sorted(folder.glob("*.xlsx")) if p != templates[0]]
        comps = compare_all(template, bids)
        entry: dict[str, Any] = {
            "template_lines": {n: len(s.lines) for n, s in template.sheets.items()},
            "bids": {},
        }
        for bid, comp in zip(bids, comps, strict=True):
            entry["bids"][bid.name] = {
                "lines": {n: len(s.lines) for n, s in bid.sheets.items()},
                "sheet_totals": {n: round(s.total, 2) for n, s in bid.sheets.items()},
                "recomputed_total": round(bid.total, 2),
                "stated_incl_vat": grand_total(bid),
                "stated_excl_vat": grand_total(bid, incl_vat=False),
                "stated_total_cost": comp.stated_total_cost,
                "markup_factor": comp.markup_factor,
                "evaluated_cost": round(comp.evaluated_cost, 2),
                "evaluated_total": _r(comp.evaluated_total),
                "common_scope_cost": _r(comp.common_scope_cost),
                "common_scope_total": _r(comp.common_scope_total),
                "common_scope_positions": comp.common_scope_positions,
                "colour": comp.colour,
                "findings": [_finding(f) for f in comp.findings],
            }
        out["boq"][set_name] = entry
    return out


def _flatten(obj: Any, prefix: str = "") -> dict[str, str]:
    """Every leaf of the snapshot under a path, so two snapshots diff line by line."""
    flat: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            flat.update(_flatten(v, f"{prefix}/{k}" if prefix else str(k)))
    elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
        for item in obj:
            flat[f"{prefix}[{json.dumps(item, ensure_ascii=False, sort_keys=True)}]"] = "present"
    else:
        flat[prefix] = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return flat


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    a, b = _flatten(old), _flatten(new)
    lines = []
    for key in sorted(set(a) | set(b)):
        if a.get(key) == b.get(key):
            continue
        if key not in a:
            lines.append(f"+ {key} = {b[key]}")
        elif key not in b:
            lines.append(f"- {key} = {a[key]}")
        else:
            lines.append(f"~ {key}: {a[key]} -> {b[key]}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--diff", type=Path, default=None, help="an earlier snapshot to diff")
    args = parser.parse_args(argv)
    settings = Settings(env="dev", docling_artifacts_path=ROOT / "models" / "docling")
    snap = snapshot(settings)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {args.out}")
    if args.diff:
        changes = diff(json.loads(args.diff.read_text(encoding="utf-8")), snap)
        print("\n".join(changes) if changes else "no differences")
        print(f"{len(changes)} difference(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
