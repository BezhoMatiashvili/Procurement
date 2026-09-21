"""End-to-end extraction over one document: rules → selection → LLM → merge."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from procurement_agent.config import Settings
from procurement_agent.extraction import weights
from procurement_agent.extraction.anchors import AnchorHit, find_anchors
from procurement_agent.extraction.llm_extractor import run_llm_extraction
from procurement_agent.extraction.merger import anchor_hits_to_facts, merge, rule_hits_to_facts
from procurement_agent.extraction.prompts import PROMPT_VERSION
from procurement_agent.extraction.rules import RuleHit, run_rules
from procurement_agent.extraction.schemas import (
    DEFAULT_TARGETS,
    ExtractedFact,
    ExtractionResult,
    FactType,
)
from procurement_agent.extraction.selector import score_chunks, select_chunks
from procurement_agent.ingestion.chunker import Chunk, chunk_docling_document
from procurement_agent.ingestion.docling_service import load_document
from procurement_agent.ingestion.excel_reader import read_workbook, sheet_chunks
from procurement_agent.llm.base import LLMClient

ProgressFn = Callable[[int, str], None]


def upload_path(settings: Settings, doc_id: str, kind: str) -> Path:
    return settings.upload_dir / f"{doc_id}.{kind}"


def result_dir(settings: Settings, doc_id: str) -> Path:
    return settings.results_dir / doc_id


def load_chunks(settings: Settings, doc_id: str, kind: str) -> list[Chunk]:
    if kind == "xlsx":
        data = upload_path(settings, doc_id, kind).read_bytes()
        return sheet_chunks(read_workbook(data), doc_id=doc_id)
    doc = load_document(result_dir(settings, doc_id) / "docling.json")
    return chunk_docling_document(doc, doc_id=doc_id)


def run_extraction(
    *,
    document_id: str,
    chunks: list[Chunk],
    settings: Settings,
    llm: LLMClient | None,
    targets: list[FactType] | None = None,
    question: str | None = None,
    fast: bool = False,
    progress: ProgressFn | None = None,
) -> ExtractionResult:
    t0 = time.perf_counter()
    targets = list(targets or DEFAULT_TARGETS)
    warnings: list[str] = []

    def report(pct: int, msg: str) -> None:
        if progress:
            progress(pct, msg)

    # 1. deterministic passes over every chunk (cheap). Anchors read values from the labels
    #    the document puts in front of them; rules match patterns anywhere.
    report(5, "anchors")
    anchor_hits: dict[str, list[AnchorHit]] = {}
    for ch in chunks:
        hits_a = find_anchors(ch.text)
        if hits_a:
            anchor_hits[ch.id] = hits_a
    anchor_facts = anchor_hits_to_facts(anchor_hits, chunks)

    report(8, "rules")
    rule_hits: dict[str, list[RuleHit]] = {}
    for ch in chunks:
        # contextualised text includes section headings (e.g. 'Contract № ...'), body does not
        hits = run_rules(ch.text)
        if hits:
            rule_hits[ch.id] = hits
    rule_facts = rule_hits_to_facts(rule_hits, chunks)

    # 2. the LLM is only asked for targets the deterministic passes did not fill. On templated
    #    documents that is often nothing at all, which turns minutes of CPU into milliseconds.
    #    Only an anchor suppresses the LLM. A regex hit alone does not: confirmation from a
    #    second source is what raises its confidence, and that is worth the call.
    resolved = {f.type for f in anchor_facts}
    open_targets = [t for t in targets if t not in resolved]
    llm_targets = open_targets if (open_targets and not question) else targets

    report(15, "selecting")
    scored = score_chunks(chunks, targets=llm_targets, question=question, rule_hits=rule_hits)
    selected = select_chunks(scored, token_budget=settings.llm_chunk_token_budget)
    if not question and not open_targets:
        selected = []

    # 3. LLM pass
    llm_facts: list[ExtractedFact] = []
    calls = 0
    model_used: str | None = None
    if llm is not None and selected:
        model_used = settings.llm_fast_model if fast else settings.llm_model

        def llm_progress(i: int, n: int) -> None:
            report(15 + int(75 * i / max(n, 1)), f"llm {i}/{n}")

        out = run_llm_extraction(
            selected,
            llm=llm,
            targets=llm_targets,
            question=question,
            model=model_used,
            progress=llm_progress,
        )
        calls = out.calls
        llm_facts = out.facts
        if out.dropped_unverified:
            warnings.append(
                f"{out.dropped_unverified} model answer(s) dropped: quote not found in document"
            )
        warnings.extend(out.errors)
        for chunk, ans in out.answers:
            llm_facts.append(
                ExtractedFact(
                    type=FactType.other,
                    label="Answer",
                    value=ans.answer.strip(),
                    raw_text=ans.raw_text.strip(),
                    provenance=chunk.provenance,
                    source="llm",
                    confidence=weights.LLM_ANSWER,
                )
            )
    elif llm is None:
        warnings.append("LLM disabled: results contain rule-based facts only")

    # 4. merge
    report(95, "merging")
    facts = merge(
        rule_facts,
        llm_facts,
        targets if not question else [*targets, FactType.other],
        anchor_facts=anchor_facts,
    )
    report(100, "done")
    return ExtractionResult(
        document_id=document_id,
        facts=facts,
        targets=targets,
        question=question,
        warnings=warnings,
        model=model_used,
        llm_calls=calls,
        seconds=round(time.perf_counter() - t0, 2),
        prompt_version=PROMPT_VERSION,
        llm_options=dict(llm.options()) if llm is not None and hasattr(llm, "options") else {},
        llm_fallback_fields=open_targets,
    )
