"""Run a gold set end to end and score it.

This is the instrument every accuracy claim in the project rests on. It exists because the
acceptance criteria in ``docs/CLIENT_REQUEST_ka.md`` (>=90% on amounts and dates, >=80% on
textual terms) had no measuring device: the only benchmark in the repository timed the
pipeline without checking a single answer.

Parsing is cached by content hash - and by everything else that changes the parse, see
``_parsed`` - so that repeated runs measure extraction, not Docling.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from docling.datamodel.base_models import ConversionStatus
from docling_core.types.doc.document import DoclingDocument

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.aoa_spelling import open_speller
from procurement_agent.checks.compare import ComparisonReport
from procurement_agent.checks.compare import compare as compare_documents
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.config import Settings
from procurement_agent.eval.gold import ExpectedFinding, GoldDocument, GoldSet
from procurement_agent.eval.score import (
    DocumentScore,
    FindingsScore,
    SetScore,
    StrictFieldScore,
    count_marked_places,
    score_extraction,
    score_extraction_strict,
    score_findings,
    score_findings_strict,
    strict_totals,
)
from procurement_agent.extraction.pipeline import chunks_text, run_extraction
from procurement_agent.extraction.schemas import ExtractionResult, FactType
from procurement_agent.ingestion.chunker import Chunk, chunk_docling_document
from procurement_agent.ingestion.docling_service import (
    conversion_fingerprint,
    convert,
    load_document,
    save_document,
)
from procurement_agent.ingestion.excel_reader import read_formulas, read_workbook, sheet_chunks
from procurement_agent.ingestion.segmenter import (
    Segment,
    pdf_has_text_layer,
    segments_from_docling,
    segments_from_workbook,
)
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
    #: Whether the closed-answer-space pass ran. Recorded because it changes both the answers
    #: and the cost, so two runs that differ only in this must not compare as the same config.
    choices: bool = False
    #: Strict scoring (docs/GOLD_SPEC.md): canonical values, every target type, findings
    #: matched on their words and corrections. Reported next to the lenient numbers.
    strict: bool = False

    def label(self) -> str:
        strict = "+strict" if self.strict else ""
        if not self.use_llm:
            return f"rules-only{strict}"
        suffix = "+choices" if self.choices else ""
        return f"{self.model or 'default'}@{self.prompt_version or '?'}{suffix}{strict}"


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
                "choices": self.config.choices,
                "strict": self.config.strict,
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
                    "choice_calls": d.choice_calls,
                    "chunks_selected": d.chunks_selected,
                    "chunks_with_facts": d.chunks_with_facts,
                    "seconds": d.seconds,
                }
                for d in self.score.documents
            ],
            "fallback_fields": self.fallback_fields,
            "fragile_fields": [
                ft.value
                for ft, fs in by_field.items()
                if fs.expected and fs.f1 >= 0.9 and self.score.anchor_hit_rate(ft) == 0.0
            ],
            **({"strict": strict_json(self.score)} if self.config.strict else {}),
        }


def _ratio(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _field_json(fs: StrictFieldScore, *, details: bool) -> dict[str, object]:
    out: dict[str, object] = {
        "expected": fs.expected,
        "tp": fs.true_positives,
        "fp": fs.false_positives,
        "fn": fs.false_negatives,
        "precision": _ratio(fs.precision),
        "recall": _ratio(fs.recall),
    }
    if details:
        out["missing"] = fs.missing
        out["wrong"] = fs.wrong
    return out


def _findings_json(fs: FindingsScore) -> dict[str, object]:
    return {
        "ok": fs.ok,
        "unscorable": fs.unscorable,
        "matched": [{"expected": e, "reported": r} for e, r in fs.matched],
        "missing": fs.missing,
        "unexpected": fs.unexpected,
        "duplicates": fs.duplicates,
        "allowed": fs.allowed,
        "occurrence_errors": fs.occurrence_errors,
    }


def strict_json(score: SetScore) -> dict[str, object]:
    """The strict section of a run report: totals, then every document in full."""
    totals = strict_totals(score.documents)

    def bucket(fields: dict[FactType, StrictFieldScore]) -> dict[str, object]:
        return {
            ft.value: _field_json(fs, details=False)
            for ft, fs in sorted(fields.items(), key=lambda kv: kv[0].value)
        }

    documents: list[dict[str, object]] = []
    for d in score.documents:
        entry: dict[str, object] = {"name": d.name}
        if d.strict is not None:
            entry["fields"] = {
                ft.value: {
                    **_field_json(fs, details=True),
                    "provisional": ft in d.strict.provisional,
                }
                for ft, fs in d.strict.fields.items()
            }
            entry["ignored"] = [t.value for t in d.strict.ignored]
            entry["unclassified"] = {t.value: n for t, n in d.strict.unclassified.items()}
        if d.strict_findings is not None:
            entry["findings"] = _findings_json(d.strict_findings)
        documents.append(entry)
    return {
        "derived": bucket(totals.derived),
        "provisional": bucket(totals.provisional),
        "overall_derived": _field_json(_sum_fields(totals.derived), details=False),
        "overall_provisional": _field_json(_sum_fields(totals.provisional), details=False),
        "unclassified": {t.value: n for t, n in sorted(totals.unclassified.items())},
        "unclassified_total": sum(totals.unclassified.values()),
        "findings_ok": all(
            d.strict_findings.ok for d in score.documents if d.strict_findings is not None
        ),
        "documents": documents,
    }


def _sum_fields(fields: dict[FactType, StrictFieldScore]) -> StrictFieldScore:
    total = StrictFieldScore(fact_type=FactType.other)
    for fs in fields.values():
        total.add(fs)
    return total


def strict_findings_ok(score: SetScore) -> bool:
    return all(d.strict_findings.ok for d in score.documents if d.strict_findings is not None)


def _cache_key(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:32]


def _parsed(path: Path, settings: Settings, cache_dir: Path) -> DoclingDocument:
    """Docling's parse of ``path``, from the cache when an identical parse is already there.

    The cache directory is named by the file's hash *and* by ``conversion_fingerprint``: the
    Docling version, whether OCR could run, the OCR languages and the model artifacts. Keyed by
    the file alone, a parse made on a machine without Tesseract was served to every later run
    as though it were current, and the number a run reported depended on which run came first.
    ``save_document`` writes atomically, so an interrupted run cannot leave a truncated entry.
    """
    cached_dir = cache_dir / f"{_cache_key(path)}-{conversion_fingerprint(settings)}"
    # save_document() owns the file name inside the directory it is handed
    cached = cached_dir / "docling.json"
    if cached.exists():
        return load_document(cached)
    result = convert(path, settings)
    if result.status == ConversionStatus.SUCCESS:
        # A partial parse (a page that failed, a timeout) is used for this run but not kept:
        # cached, it would be served to every later run as though it were the document.
        save_document(result.document, cached_dir)
    return result.document


def load_chunks_for(path: Path, settings: Settings, cache_dir: Path) -> list[Chunk]:
    """Parse a document, caching Docling's output so a sweep measures extraction, not parsing."""
    doc_id = _cache_key(path)
    if path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        return sheet_chunks(read_workbook(path.read_bytes()), doc_id=doc_id)
    return chunk_docling_document(_parsed(path, settings, cache_dir), doc_id=doc_id)


def _segments_for(path: Path, settings: Settings, cache_dir: Path) -> list[Segment]:
    """Segments for a gold document, reusing the same Docling cache the chunks use."""
    if path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        data = path.read_bytes()
        return segments_from_workbook(read_workbook(data), read_formulas(data))
    return segments_from_docling(_parsed(path, settings, cache_dir))


def _compare_rule_ids(gold: GoldDocument, settings: Settings, cache_dir: Path) -> list[str]:
    """The CMP.* rules a comparison reports as errors, for scoring against the gold file.

    ``gold.reference`` is the earlier version and ``gold.document`` the revision, matching the
    direction the UI uses: the left-hand file is the original. No model is involved, so this
    column of a run means the same thing whatever ``--llm`` was set to.
    """
    return [f.rule_id for f in _compare_report(gold, settings, cache_dir).errors]


def _compare_report(gold: GoldDocument, settings: Settings, cache_dir: Path) -> ComparisonReport:
    if gold.reference is None:  # GoldDocument's validator guarantees this; say so if it moves
        msg = f"comparison gold file {gold.name!r} has no reference document"
        raise ValueError(msg)
    return compare_documents(
        _segments_for(gold.reference, settings, cache_dir),
        _segments_for(gold.document, settings, cache_dir),
        left_document_id=f"{gold.name}:before",
        right_document_id=gold.name,
        left_kind=gold.reference.suffix.lstrip("."),
        right_kind=gold.document.suffix.lstrip("."),
        min_similarity=settings.compare_min_similarity,
        max_segments=settings.compare_max_segments,
    )


_TRAILING_PLACE = re.compile(r"\s*\([^()]*\)\s*$")
_CHANGE_KIND = re.compile(r"\s+(?:added|removed)$")


def compare_label(finding: Finding, report: ComparisonReport | None = None) -> str | None:
    """The English label a comparison finding carries ("Bank code").

    Taken from the report's own value delta when one has the finding's rule and values - that
    is where the label lives - and from the message otherwise: the words before the first ':'
    once a trailing "(where)" is dropped, without "added"/"removed" ("Bank code added: X").
    Read tolerantly (``getattr``): ``compare.py`` is being changed while this is written.
    """
    if report is not None:
        deltas = [d for h in getattr(report, "hunks", []) for d in getattr(h, "deltas", [])]
        deltas += list(getattr(report, "field_deltas", []))
        for d in deltas:
            if (
                getattr(d, "rule_id", None) == finding.rule_id
                and getattr(d, "left", None) == finding.expected
                and getattr(d, "right", None) == finding.actual
            ):
                label = getattr(d, "label_en", None)
                if isinstance(label, str) and label:
                    return label
    message = _TRAILING_PLACE.sub("", finding.message_en)
    head = message.split(":", 1)[0] if ":" in message else message
    return _CHANGE_KIND.sub("", head.strip()) or None


def _occurrence_counter(
    source: Path,
) -> Callable[[ExpectedFinding, Finding], int | None]:
    def count(entry: ExpectedFinding, finding: Finding) -> int | None:
        return count_marked_places(source, [finding], entry.text)

    return count


def _scanned(path: Path) -> bool:
    """A PDF with no text layer of its own: what it says came from OCR."""
    return path.suffix.lower() == ".pdf" and not pdf_has_text_layer(path)


def _aoa_report(
    gold: GoldDocument, chunks: list[Chunk], settings: Settings, cache_dir: Path
) -> CheckReport:
    reference = (
        chunks_text(load_chunks_for(gold.reference, settings, cache_dir))
        if gold.reference
        else None
    )
    registry = open_snapshot(settings.registry_db) if settings.registry_db else None
    try:
        return check_aoa(
            aoa_text=chunks_text(chunks),
            aoa_id=gold.name,
            contract_text=reference,
            registry=registry,
            contractor_type=gold.contractor_type,
            aoa_ocr=_scanned(gold.document),
            contract_ocr=gold.reference is not None and _scanned(gold.reference),
            speller=open_speller(settings),
        )
    finally:
        if registry is not None:
            registry.close()


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
    # a comparison's gold describes the pair, not what the newer document states: its fields
    # are scored only when it lists some
    if config.strict and (gold.check != "compare" or gold.fields):
        score.strict = score_extraction_strict(
            result,
            gold.fields,
            gold.name,
            ignore=gold.ignore,
            provisional=gold.provisional,
        )

    legacy = bool(gold.expected_findings or gold.forbidden_findings)
    strict = config.strict and bool(gold.findings or gold.strict_findings)
    if strict and gold.reference is None:
        # No check runs without a reference (a contract's own gold has none). Expected findings
        # there are a gold defect, reported rather than raised at load time: one unfinished
        # gold file must not abort a whole set.
        if gold.findings:
            score.strict_findings = FindingsScore(
                unscorable="the gold file expects findings but names no reference document"
            )
        strict = False
    if strict and gold.check is None:
        score.strict_findings = FindingsScore(unscorable="the gold file names no check to run")
        strict = False
    if not (legacy or strict) or gold.check is None:
        return score, result

    # Each check runs once; the legacy lists read its errors, the strict entries every finding.
    comparison: ComparisonReport | None = None
    if gold.check == "compare":
        comparison = _compare_report(gold, settings, cache_dir)
        reported = list(comparison.findings)
        errors = list(comparison.errors)
    else:
        aoa = _aoa_report(gold, chunks, settings, cache_dir)
        reported = list(aoa.findings)
        errors = list(aoa.errors)

    if legacy:
        score.missing_findings, score.unexpected_findings = score_findings(
            [f.rule_id for f in errors],
            gold.expected_findings,
            gold.forbidden_findings,
            must_be_clean=gold.must_be_clean,
        )
    if strict:
        labels = (
            [compare_label(f, comparison) for f in reported]
            if comparison is not None
            else [None] * len(reported)
        )
        score.strict_findings = score_findings_strict(
            gold.findings,
            reported,
            labels=labels,
            strict=gold.strict_findings,
            allowed=gold.allowed_findings,
            count_occurrences=_occurrence_counter(gold.document),
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

    # A field with a high F1 and no anchor coverage is carried entirely by keyword proximity,
    # which is the layer most sensitive to a change of wording. It reads exactly like a robust
    # result in the table above, so say so explicitly.
    fragile = [
        ft
        for ft, fs in by_field.items()
        if fs.expected and fs.f1 >= 0.9 and report.score.anchor_hit_rate(ft) == 0.0
    ]
    if fragile:
        lines += [
            "",
            "fragile: scoring well with no anchor coverage, i.e. carried by keyword proximity",
            "         alone and liable to break on a change of wording:",
            *(f"  {ft.value}" for ft in sorted(fragile, key=lambda t: t.value)),
        ]

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
    if report.config.strict:
        lines += ["", *format_strict(report.score)]
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "      -" if value is None else f"{value:7.2%}"


def _strict_rows(title: str, fields: dict[FactType, StrictFieldScore]) -> list[str]:
    lines = [
        title,
        f"{'field':22s} {'n':>4s} {'tp':>4s} {'fp':>4s} {'fn':>4s} {'prec':>7s} {'rec':>7s}",
        "-" * 60,
    ]
    for ft, fs in sorted(fields.items(), key=lambda kv: kv[0].value):
        lines.append(
            f"{ft.value:22s} {fs.expected:4d} {fs.true_positives:4d} {fs.false_positives:4d} "
            f"{fs.false_negatives:4d} {_pct(fs.precision)} {_pct(fs.recall)}"
        )
    total = _sum_fields(fields)
    lines += [
        "-" * 60,
        f"{'OVERALL':22s} {total.expected:4d} {total.true_positives:4d} "
        f"{total.false_positives:4d} {total.false_negatives:4d} {_pct(total.precision)} "
        f"{_pct(total.recall)}",
    ]
    return lines


def format_strict(score: SetScore) -> list[str]:
    """The strict section: per-field totals (derived and provisional apart), the unclassified
    count, then every document's field misses and findings with their words."""
    totals = strict_totals(score.documents)
    lines: list[str] = [f"STRICT (docs/GOLD_SPEC.md) over {totals.documents} document(s)"]
    lines += _strict_rows("derived fields (exact gold):", totals.derived)
    if totals.provisional:
        lines += [
            "",
            *_strict_rows("provisional fields (labelled, unconfirmed):", totals.provisional),
        ]
    unclassified = ", ".join(f"{t.value}={n}" for t, n in sorted(totals.unclassified.items()))
    lines += [
        "",
        f"unclassified (generic, not scored): {sum(totals.unclassified.values())}"
        + (f"  ({unclassified})" if unclassified else ""),
    ]
    for d in score.documents:
        head = f"  {d.name}:"
        body: list[str] = []
        if d.strict is not None:
            for ft, fs in sorted(d.strict.fields.items(), key=lambda kv: kv[0].value):
                if fs.correct:
                    continue
                tag = " (provisional)" if ft in d.strict.provisional else ""
                body.append(
                    f"    {ft.value}{tag}: missing {fs.missing or '-'}  wrong {fs.wrong or '-'}"
                )
        fsc = d.strict_findings
        if fsc is not None:
            if fsc.unscorable:
                body.append(f"    findings UNSCORABLE: {fsc.unscorable}")
            body.append(
                f"    findings: matched {len(fsc.matched)}  missing {len(fsc.missing)}  "
                f"unexpected {len(fsc.unexpected)}  duplicates {len(fsc.duplicates)}  "
                f"occurrence errors {len(fsc.occurrence_errors)}"
            )
            body += [f"      MATCHED {e}  <=  {r}" for e, r in fsc.matched]
            body += [f"      MISSING {e}" for e in fsc.missing]
            body += [f"      UNEXPECTED {e}" for e in fsc.unexpected]
            body += [f"      DUPLICATE {e}" for e in fsc.duplicates]
            body += [f"      OCCURRENCES {e}" for e in fsc.occurrence_errors]
            body += [f"      allowed {e}" for e in fsc.allowed]
        if body:
            lines += ["", head, *body]
    return lines


def compare(baseline: dict[str, object], current: RunReport) -> str:
    """Field-level deltas against a previous run, so a change can be judged, not asserted."""
    old = baseline.get("fields", {})
    new = current.to_json()["fields"]
    if not isinstance(old, dict) or not isinstance(new, dict):
        msg = "a run report's 'fields' must be a mapping; is the baseline file a run report?"
        raise TypeError(msg)
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
