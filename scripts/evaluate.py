#!/usr/bin/env python
"""Measure extraction and check accuracy against a gold set.

    uv run python scripts/evaluate.py --set synthetic
    uv run python scripts/evaluate.py --set client --out eval_runs/baseline.json
    uv run python scripts/evaluate.py --set client --compare eval_runs/baseline.json
    uv run python scripts/evaluate.py --set client --llm --model gemma3:4b --choices
    uv run python scripts/evaluate.py --set client_v2 --strict --out samples/gold_work/strict.json

``--strict`` adds the scoring of ``docs/GOLD_SPEC.md`` next to the lenient numbers: canonical
values only, every target type scored for every document (a type the gold does not list must
be absent), and findings matched on their words, corrections and Word-comment counts. The
lenient ``fields`` of a run report keep their meaning; the strict ones go under ``strict``.
A strict report lists the values that were missed or wrong and the words each finding quotes,
i.e. the documents' own contents: write it for a client set under ``samples/`` (git-ignored).

``--set client`` reads gold files from ``samples/gold/`` (git-ignored, because the expected
values for the client's documents are the confidential data itself). ``--set synthetic`` reads
the committed ones under ``tests/fixtures/gold/``.

This docstring used to advertise ``--set fallback``, which argparse has always rejected: the
only sets are the two gold directories above. The intent behind it — scoring just the fields no
anchor or rule resolved, as the corpus for comparing models — is served by ``fallback_fields``
in the report, and cannot become a set of its own until those fields have gold labels. Until
then a fallback-only run would score an empty corpus and report a confident zero.
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
    strict_findings_ok,
    write_report,
)
from procurement_agent.llm.ollama_client import OllamaClient  # noqa: E402

GOLD_DIRS = {
    "synthetic": ROOT / "tests" / "fixtures" / "gold",
    "client": ROOT / "samples" / "gold",
    # GOLD_SPEC gold, written for strict scoring (git-ignored like the client set)
    "client_v2": ROOT / "samples" / "gold_v2" / "client",
    "ts2": ROOT / "samples" / "gold_v2" / "ts2",
    "ts3": ROOT / "samples" / "gold_v2" / "ts3",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="set_name", default="synthetic", choices=[*GOLD_DIRS, "all"])
    parser.add_argument("--model", default=None, help="override the LLM model to evaluate")
    parser.add_argument("--llm", action="store_true", help="enable the LLM pass (slow on CPU)")
    parser.add_argument(
        "--choices",
        action="store_true",
        help="also ask closed-answer-space questions (VAT, and a check on each generated value); "
        "costs roughly one extra prefill per chunk, so compare seconds as well as F1",
    )
    parser.add_argument("--out", type=Path, default=None, help="write the run as JSON")
    parser.add_argument("--compare", type=Path, default=None, help="diff against a previous run")
    parser.add_argument("--fail-under", type=float, default=None, help="minimum overall F1")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also score strictly per docs/GOLD_SPEC.md (canonical values, absent types, "
        "findings by text/correction/occurrences); every set is run before the exit status. "
        "The report quotes document values: keep --out for client sets under samples/",
    )
    args = parser.parse_args(argv)

    settings = Settings(
        env="dev",
        docling_artifacts_path=Path("./models/docling"),
        llm_model=args.model or Settings().llm_model,
        llm_fast_model=args.model or Settings().llm_fast_model,
        llm_choices=args.choices,
    )
    llm = OllamaClient(settings) if args.llm else None
    config = RunConfig(model=args.model, use_llm=args.llm, choices=args.choices, strict=args.strict)

    names = list(GOLD_DIRS) if args.set_name == "all" else [args.set_name]
    worst = 1.0
    failed = False
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
            # one file per set when several run, or each would overwrite the one before
            out = args.out if len(names) == 1 else args.out.with_stem(f"{args.out.stem}.{name}")
            print(f"wrote {write_report(report, out)}")
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
        if args.strict:
            if not report.score.clean:
                print(f"FAIL: {name}: rule findings did not match the gold set", file=sys.stderr)
                failed = True
            if not strict_findings_ok(report.score):
                print(f"FAIL: {name}: strict findings did not match the gold set", file=sys.stderr)
                failed = True
        elif not report.score.clean:
            print("FAIL: rule findings did not match the gold set", file=sys.stderr)
            return 1
    if failed:
        return 1

    if args.fail_under is not None and worst < args.fail_under:
        print(f"FAIL: overall F1 {worst:.2%} is below {args.fail_under:.2%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
