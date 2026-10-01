"""End-to-end extraction over one document: rules → selection → LLM → merge."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from procurement_agent.checks.compare import ComparisonReport, compare
from procurement_agent.checks.findings import SkipNote
from procurement_agent.config import Settings
from procurement_agent.extraction import weights
from procurement_agent.extraction.anchors import AnchorHit, find_anchors
from procurement_agent.extraction.labels import LABELS
from procurement_agent.extraction.llm_extractor import run_llm_extraction
from procurement_agent.extraction.merger import anchor_hits_to_facts, merge, rule_hits_to_facts
from procurement_agent.extraction.normalize import normalize_confusables
from procurement_agent.extraction.prompts import PROMPT_VERSION
from procurement_agent.extraction.rules import RuleHit, close_label_lines, run_rules
from procurement_agent.extraction.schemas import (
    DEFAULT_TARGETS,
    ExtractedFact,
    ExtractionResult,
    FactType,
)
from procurement_agent.extraction.selector import score_chunks, select_chunks
from procurement_agent.ingestion.chunker import Chunk, chunk_docling_document
from procurement_agent.ingestion.docling_service import load_document
from procurement_agent.ingestion.excel_reader import read_formulas, read_workbook, sheet_chunks
from procurement_agent.ingestion.segmenter import (
    Segment,
    pdf_has_text_layer,
    segments_from_docling,
    segments_from_workbook,
)
from procurement_agent.llm.base import LLMClient

ProgressFn = Callable[[int, str], None]


def upload_path(settings: Settings, doc_id: str, kind: str) -> Path:
    return settings.upload_dir / f"{doc_id}.{kind}"


def result_dir(settings: Settings, doc_id: str) -> Path:
    return settings.results_dir / doc_id


def load_chunks(
    settings: Settings, doc_id: str, kind: str, *, notes: list[str] | None = None
) -> list[Chunk]:
    """The stored document cut into LLM-sized chunks.

    ``notes`` collects what the workbook reader had to leave out (rows, columns or sheets past
    its caps), so a caller can tell the user instead of extracting from a silently shortened
    file.
    """
    if kind == "xlsx":
        data = upload_path(settings, doc_id, kind).read_bytes()
        return sheet_chunks(read_workbook(data, notes=notes), doc_id=doc_id)
    doc = load_document(result_dir(settings, doc_id) / "docling.json")
    return chunk_docling_document(doc, doc_id=doc_id)


def chunks_text(chunks: Sequence[Chunk]) -> str:
    """The text a document check reads: every chunk's verbatim body, in document order."""
    return "\n".join(c.body or c.text for c in chunks)


def load_document_text(settings: Settings, doc_id: str, kind: str) -> str:
    """The stored document as the checks read it.

    Plain, with no HTTP in it, so a background job can read a document exactly as the API does;
    the evaluation joins its own cached chunks with ``chunks_text``, the same join. The caller
    checks that the document is ready (the API answers 409 when it is not).
    """
    return chunks_text(load_chunks(settings, doc_id, kind))


def load_segments(
    settings: Settings, doc_id: str, kind: str, *, notes: list[str] | None = None
) -> list[Segment]:
    """The same parsed document as ``load_chunks``, cut into diff-sized pieces instead.

    Chunks pack about 1100 real tokens and serialise a whole table onto one line, which is
    right for an LLM prompt and wrong for a line-by-line comparison. Both read the same stored
    artifact, so a fact and a diff hunk can never disagree about what the document says.
    """
    if kind == "xlsx":
        data = upload_path(settings, doc_id, kind).read_bytes()
        return segments_from_workbook(read_workbook(data, notes=notes), read_formulas(data))
    return segments_from_docling(load_document(result_dir(settings, doc_id) / "docling.json"))


def is_scanned_pdf(settings: Settings, doc_id: str, kind: str) -> bool:
    """True for a PDF whose text had to come from OCR (it has no text layer of its own)."""
    return kind == "pdf" and not pdf_has_text_layer(upload_path(settings, doc_id, kind))


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """What a comparison needs to know about one uploaded, parsed document."""

    id: str
    kind: str
    name: str


def compare_stored(
    settings: Settings, left: StoredDocument, right: StoredDocument
) -> ComparisonReport:
    """Compare two stored documents: the one entry point for the API, the UI and the worker.

    The API and the explain job used to build this call separately, and neither told
    ``compare`` whether the input was OCR'd, so every PDF was treated as a scan and a letter
    swapped for a lookalike in a born-digital PDF was reported as scanner noise. Workbook
    truncation is surfaced as a skip note for the same reason: two files compared on their
    first 20 000 rows must not read as "identical".
    """
    notes: list[str] = []
    report = compare(
        load_segments(settings, left.id, left.kind, notes=notes),
        load_segments(settings, right.id, right.kind, notes=notes),
        left_document_id=left.id,
        right_document_id=right.id,
        left_name=left.name,
        right_name=right.name,
        left_kind=left.kind,
        right_kind=right.kind,
        min_similarity=settings.compare_min_similarity,
        max_segments=settings.compare_max_segments,
        ocr=is_scanned_pdf(settings, left.id, left.kind)
        or is_scanned_pdf(settings, right.id, right.kind),
    )
    report.skipped.extend(
        SkipNote(
            rule_id="CMP.TRUNCATED",
            reason_ka=f"ფაილის ნაწილი ლიმიტის გამო არ შედარდა: {note}",
            reason_en=f"part of the file was not compared (size limit): {note}",
        )
        for note in notes
    )
    return report


def document_level_facts(chunks: Sequence[Chunk], warnings: list[str]) -> list[ExtractedFact]:
    """``checks.document_facts`` over the whole document, as facts cited to the chunk that holds
    each one's words."""
    from procurement_agent.checks.document_facts import document_facts  # checks import us

    if not chunks:
        return []
    try:
        # a "label: value" line is a sentence of its own (rules.label_line_spans): the readers
        # split sentences at their stops, and the document's text flattens its line breaks
        found = document_facts(close_label_lines(chunks_text(chunks)))
    except Exception as e:  # one reader failing must not cost the rest of the extraction
        warnings.append(f"document-level readers failed: {type(e).__name__}")
        return []
    out: list[ExtractedFact] = []
    for f in found:
        chunk = _chunk_holding(chunks, f.raw_text) or chunks[0]
        out.append(
            ExtractedFact(
                type=f.type,
                label=LABELS[f.type][0],
                value=f.value,
                normalized=f.normalized,
                unit=f.unit,
                raw_text=f.raw_text,
                provenance=chunk.provenance,
                source="rules",
                confidence=f.confidence,
            )
        )
    return out


def _chunk_holding(chunks: Sequence[Chunk], words: str) -> Chunk | None:
    """The chunk whose text holds ``words``; also without a stop ``close_label_lines`` added at
    the end of a label line."""
    needle = normalize_confusables(words)[:60]
    if not needle:
        return None
    for candidate in filter(None, dict.fromkeys((needle, needle.rstrip(".")))):
        found = next(
            (c for c in chunks if candidate in normalize_confusables(c.body or c.text)), None
        )
        if found is not None:
            return found
    return None


#: Unclassified leftovers a document-level fact accounts for: the "5 working days" duration is
#: the payment deadline once the payment reader has named it.
_GENERIC_OF = {FactType.payment_terms: FactType.duration}


def _superseded(fact: ExtractedFact, doc_facts: list[ExtractedFact]) -> bool:
    """Whether a chunk-level rule fact is replaced by a document-level one."""
    if fact.source != "rules":
        return False
    for d in doc_facts:
        if fact.type == d.type:
            return True
        generic = _GENERIC_OF.get(d.type)
        if (
            generic is not None
            and fact.type == generic
            and str(d.normalized).startswith(str(fact.normalized))
        ):
            return True
    return False


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

    # 2b. what the document states as a whole - its term, the service period, how it is paid,
    #     VAT, the subject, penalties - read once over the whole text by the readers the AOA
    #     check uses. Each replaces the chunk-by-chunk guesses of its type: those split a period
    #     into two dates and read a performance window's end as a second contract date.
    report(10, "document")
    doc_facts = document_level_facts(chunks, warnings)
    if doc_facts:
        rule_facts = [f for f in rule_facts if not _superseded(f, doc_facts)]
        rule_facts.extend(doc_facts)

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
    choice_calls = 0
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
            # Question mode has one free-form answer and nothing to verify against a fixed
            # option set, so the choice pass would only add latency there.
            choices=settings.llm_choices and not question,
        )
        calls = out.calls
        choice_calls = out.choice_calls
        llm_facts = out.facts
        if out.dropped_unverified:
            warnings.append(
                f"{out.dropped_unverified} model answer(s) dropped: quote not found in the "
                "document, or the value is not in its quote"
            )
        warnings.extend(out.choice_notes)
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
        llm_choice_calls=choice_calls,
        chunks_selected=len(selected),
        chunks_with_facts=len({f.provenance.chunk_id for f in llm_facts if f.provenance.chunk_id}),
        seconds=round(time.perf_counter() - t0, 2),
        prompt_version=PROMPT_VERSION,
        llm_options=dict(llm.options()) if llm is not None and hasattr(llm, "options") else {},
        llm_fallback_fields=open_targets,
    )
