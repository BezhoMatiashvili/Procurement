#!/usr/bin/env python
"""Measure extraction and check accuracy against a gold set.

    uv run python scripts/evaluate.py --set synthetic
    uv run python scripts/evaluate.py --set client --out eval_runs/baseline.json
    uv run python scripts/evaluate.py --set client --compare eval_runs/baseline.json
    uv run python scripts/evaluate.py --set fallback --model gemma3:4b-it-qat

``--set client`` reads gold files from ``samples/gold/`` (git-ignored, because the expected
values for the client's documents are the confidential data itself). ``--set synthetic`` reads
the committed ones under ``tests/fixtures/gold/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from procurement_agent.config import Settings  # noqa: E402
from procurement_agent.eval.gold import load_gold_set  # noqa: E402
from procurement_agent.eval.runner import (  # noqa: E402
    RunConfig,
    compare,
    format_table,
    run_set,
    write_report,
)
from procurement_agent.llm.ollama_client import OllamaClient  # noqa: E402

GOLD_DIRS = {
    "synthetic": ROOT / "tests" / "fixtures" / "gold",
    "client": ROOT / "samples" / "gold",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="set_name", default="synthetic", choices=[*GOLD_DIRS, "all"])
    parser.add_argument("--model", default=None, help="override the LLM model to evaluate")
    parser.add_argument("--llm", action="store_true", help="enable the LLM pass (slow on CPU)")
    parser.add_argument("--out", type=Path, default=None, help="write the run as JSON")
    parser.add_argument("--compare", type=Path, default=None, help="diff against a previous run")
    parser.add_argument("--fail-under", type=float, default=None, help="minimum overall F1")
    args = parser.parse_args(argv)

    settings = Settings(
        env="dev",
        docling_artifacts_path=Path("./models/docling"),
        llm_model=args.model or Settings().llm_model,
        llm_fast_model=args.model or Settings().llm_fast_model,
    )
    llm = OllamaClient(settings) if args.llm else None
    config = RunConfig(model=args.model, use_llm=args.llm)

    names = list(GOLD_DIRS) if args.set_name == "all" else [args.set_name]
    worst = 1.0
    for name in names:
        directory = GOLD_DIRS[name]
        if not directory.exists():
            print(f"[skip] {name}: no gold files at {directory}")
            continue
        gold_set = load_gold_set(directory, name=name, root=ROOT)
        if not len(gold_set):
            print(f"[skip] {name}: no gold files at {directory}")
            continue
        report = run_set(gold_set, settings=settings, llm=llm, config=config)
        print(format_table(report))
        print()
        if args.out:
            print(f"wrote {write_report(report, args.out)}")
        if args.compare and args.compare.exists():
            print(compare(json.loads(args.compare.read_text(encoding="utf-8")), report))
        by_field = report.score.by_field()
        tp = sum(f.true_positives for f in by_field.values())
        fp = sum(f.false_positives for f in by_field.values())
        expected = sum(f.expected for f in by_field.values())
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / expected if expected else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        worst = min(worst, f1)
        if not report.score.clean:
            print("FAIL: rule findings did not match the gold set", file=sys.stderr)
            return 1

    if args.fail_under is not None and worst < args.fail_under:
        print(f"FAIL: overall F1 {worst:.2%} is below {args.fail_under:.2%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
