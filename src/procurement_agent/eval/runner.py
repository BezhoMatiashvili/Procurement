"""Run a gold set end to end and score it.

This is the instrument every accuracy claim in the project rests on. It exists because the
acceptance criteria in ``docs/CLIENT_REQUEST_ka.md`` (>=90% on amounts and dates, >=80% on
textual terms) had no measuring device: the only benchmark in the repository timed the
pipeline without checking a single answer.

Parsing is cached by content hash so that repeated runs measure extraction, not Docling.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.config import Settings
from procurement_agent.eval.gold import GoldDocument, GoldSet
from procurement_agent.eval.score import DocumentScore, SetScore, score_extraction, score_findings
from procurement_agent.extraction.pipeline import run_extraction
from procurement_agent.extraction.schemas import ExtractionResult
from procurement_agent.ingestion.chunker import Chunk, chunk_docling_document
from procurement_agent.ingestion.docling_service import convert, load_document, save_document
from procurement_agent.ingestion.excel_reader import read_workbook, sheet_chunks
from procurement_agent.llm.base import LLMClient
from procurement_agent.registry.snapshot import open_snapshot


@dataclass(slots=True)
class RunConfig:
    """Everything that can change an answer, recorded with the result."""

    model: str | None = None
    use_llm: bool = False
    fast: bool = True
    prompt_version: str | None = None
    llm_options: dict[str, object] = field(default_factory=dict)

    def label(self) -> str:
        if not self.use_llm:
            return "rules-only"
        return f"{self.model or 'default'}@{self.prompt_version or '?'}"


@dataclass(slots=True)
class RunReport:
    set_name: str
    config: RunConfig
    score: SetScore
    started_at: str
    seconds: float
    #: (document, field) pairs no anchor or rule produced — the corpus for comparing models,
    #: because once the deterministic layer does its job there is little left for one to decide.
    fallback_fields: dict[str, list[str]] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        by_field = self.score.by_field()
        return {
            "set": self.set_name,
            "config": {
                "model": self.config.model,
                "use_llm": self.config.use_llm,
                "prompt_version": self.config.prompt_version,
                "llm_options": self.config.llm_options,
            },
            "started_at": self.started_at,
            "seconds": round(self.seconds, 2),
            "clean": self.score.clean,
            "fields": {
                ft.value: {
                    "expected": fs.expected,
                    "precision": round(fs.precision, 4),
                    "recall": round(fs.recall, 4),
                    "f1": round(fs.f1, 4),
                    "anchor_hit_rate": round(self.score.anchor_hit_rate(ft), 4),
                }
                for ft, fs in sorted(by_field.items(), key=lambda kv: kv[0].value)
            },
            "documents": [
                {
                    "name": d.name,
                    "missing_findings": d.missing_findings,
                    "unexpected_findings": d.unexpected_findings,
                    "llm_calls": d.llm_calls,
                    "seconds": d.seconds,
                }
                for d in self.score.documents
            ],
            "fallback_fields": self.fallback_fields,
        }


def _cache_key(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:32]


def load_chunks_for(path: Path, settings: Settings, cache_dir: Path) -> list[Chunk]:
    """Parse a document, caching Docling's output so a sweep measures extraction, not parsing."""
    doc_id = _cache_key(path)
    if path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        return sheet_chunks(read_workbook(path.read_bytes()), doc_id=doc_id)
    # save_document() owns the file name inside the directory it is handed
    cached_dir = cache_dir / doc_id
    cached = cached_dir / "docling.json"
    if cached.exists():
        document = load_document(cached)
    else:
        document = convert(path, settings).document
        save_document(document, cached_dir)
    return chunk_docling_document(document, doc_id=doc_id)


def _text_of(chunks: list[Chunk]) -> str:
    return "\n".join(c.body or c.text for c in chunks)


def run_document(
    gold: GoldDocument,
    *,
    settings: Settings,
    llm: LLMClient | None,
    config: RunConfig,
    cache_dir: Path,
) -> tuple[DocumentScore, ExtractionResult]:
    chunks = load_chunks_for(gold.document, settings, cache_dir)
    result = run_extraction(
        document_id=gold.name,
        chunks=chunks,
        settings=settings,
        llm=llm if config.use_llm else None,
        fast=config.fast,
    )
    score = score_extraction(result, gold.fields, gold.name)

    if gold.expected_findings or gold.forbidden_findings:
        reference = (
            _text_of(load_chunks_for(gold.reference, settings, cache_dir))
            if gold.reference
            else None
        )
        registry = open_snapshot(settings.registry_db) if settings.registry_db else None
        try:
            report = check_aoa(
                aoa_text=_text_of(chunks),
                aoa_id=gold.name,
                contract_text=reference,
                registry=registry,
                contractor_type=gold.contractor_type,
            )
        finally:
            if registry is not None:
                registry.close()
        reported = [f.rule_id for f in report.errors]
        score.missing_findings, score.unexpected_findings = score_findings(
            reported,
            gold.expected_findings,
            gold.forbidden_findings,
            must_be_clean=gold.must_be_clean,
        )
    return score, result


def run_set(
    gold_set: GoldSet,
    *,
    settings: Settings,
    llm: LLMClient | None = None,
    config: RunConfig | None = None,
    cache_dir: Path = Path(".eval_cache"),
) -> RunReport:
    config = config or RunConfig()
    started = datetime.now(UTC).isoformat()
    t0 = time.perf_counter()
    score = SetScore(name=gold_set.name)
    fallback: dict[str, list[str]] = {}

    for gold in gold_set:
        doc_score, result = run_document(
            gold, settings=settings, llm=llm, config=config, cache_dir=cache_dir
        )
        score.documents.append(doc_score)
        if result.llm_fallback_fields:
            fallback[gold.name] = [t.value for t in result.llm_fallback_fields]
        config.prompt_version = config.prompt_version or result.prompt_version
        if result.llm_options and not config.llm_options:
            config.llm_options = dict(result.llm_options)

    return RunReport(
        set_name=gold_set.name,
        config=config,
        score=score,
        started_at=started,
        seconds=time.perf_counter() - t0,
        fallback_fields=fallback,
    )


def format_table(report: RunReport) -> str:
    lines = [
        f"set={report.set_name}  config={report.config.label()}  "
        f"{report.seconds:.1f}s  clean={report.score.clean}",
        f"{'field':22s} {'n':>4s} {'prec':>7s} {'rec':>7s} {'F1':>7s} {'anchor':>7s}",
        "-" * 60,
    ]
    by_field = report.score.by_field()
    for ft, fs in sorted(by_field.items(), key=lambda kv: kv[0].value):
        lines.append(
            f"{ft.value:22s} {fs.expected:4d} {fs.precision:7.2%} {fs.recall:7.2%} "
            f"{fs.f1:7.2%} {report.score.anchor_hit_rate(ft):7.2%}"
        )
    if by_field:
        total = sum(fs.expected for fs in by_field.values())
        tp = sum(fs.true_positives for fs in by_field.values())
        fp = sum(fs.false_positives for fs in by_field.values())
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / total if total else 0.0
        lines += ["-" * 60, f"{'OVERALL':22s} {total:4d} {prec:7.2%} {rec:7.2%}"]

    problems = [d for d in report.score.documents if not d.findings_ok]
    lines.append("")
    if problems:
        lines.append("rule findings that did not match the gold set:")
        for d in problems:
            if d.missing_findings:
                lines.append(f"  {d.name}: MISSING {', '.join(d.missing_findings)}")
            if d.unexpected_findings:
                lines.append(f"  {d.name}: UNEXPECTED {', '.join(d.unexpected_findings)}")
    else:
        lines.append("rule findings: every document matched the gold set")
    return "\n".join(lines)


def compare(baseline: dict[str, object], current: RunReport) -> str:
    """Field-level deltas against a previous run, so a change can be judged, not asserted."""
    old = baseline.get("fields", {})
    assert isinstance(old, dict)
    new = current.to_json()["fields"]
    assert isinstance(new, dict)
    lines = [f"{'field':22s} {'F1 before':>10s} {'F1 after':>10s} {'delta':>8s}", "-" * 54]
    for name in sorted(set(old) | set(new)):
        before = float(old.get(name, {}).get("f1", 0.0)) if name in old else 0.0
        after = float(new.get(name, {}).get("f1", 0.0)) if name in new else 0.0
        flag = "" if abs(after - before) < 1e-9 else ("  +" if after > before else "  -")
        lines.append(f"{name:22s} {before:10.2%} {after:10.2%} {after - before:+8.2%}{flag}")
    return "\n".join(lines)


def write_report(report: RunReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path
