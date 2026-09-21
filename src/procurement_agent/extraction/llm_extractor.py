"""LLM extraction over selected chunks, with verbatim-quote verification.

A fact is accepted only if its ``raw_text`` really occurs in the chunk (whitespace-insensitive).
This is the main defence against hallucinated values and against the model "answering" from
injected instructions rather than from the document.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from procurement_agent.extraction import weights
from procurement_agent.extraction.labels import LABELS
from procurement_agent.extraction.prompts import (
    SYSTEM_EXTRACT,
    SYSTEM_QUESTION,
    extraction_user_prompt,
    question_user_prompt,
)
from procurement_agent.extraction.schemas import (
    ExtractedFact,
    FactType,
    LLMAnswer,
    LLMExtraction,
)
from procurement_agent.ingestion.chunker import Chunk
from procurement_agent.llm.base import LLMClient, LLMError
from procurement_agent.security.audit import log

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip().lower()


def quote_in_chunk(quote: str, chunk: Chunk) -> bool:
    q = _norm(quote)
    if len(q) < 2:
        return False
    return q in _norm(chunk.body) or q in _norm(chunk.text)


@dataclass(slots=True)
class LLMPassResult:
    facts: list[ExtractedFact]
    answers: list[tuple[Chunk, LLMAnswer]]
    calls: int
    dropped_unverified: int
    errors: list[str]


ProgressFn = Callable[[int, int], None]


def run_llm_extraction(
    chunks: list[Chunk],
    *,
    llm: LLMClient,
    targets: list[FactType],
    question: str | None,
    model: str | None = None,
    progress: ProgressFn | None = None,
) -> LLMPassResult:
    facts: list[ExtractedFact] = []
    answers: list[tuple[Chunk, LLMAnswer]] = []
    calls = dropped = 0
    errors: list[str] = []

    for i, chunk in enumerate(chunks):
        if progress:
            progress(i, len(chunks))
        try:
            if question:
                ans = llm.generate_structured(
                    system=SYSTEM_QUESTION,
                    user=question_user_prompt(chunk.text, question),
                    schema=LLMAnswer,
                    model=model,
                )
                calls += 1
                if ans.found and ans.answer:
                    if quote_in_chunk(ans.raw_text, chunk):
                        answers.append((chunk, ans))
                    else:
                        dropped += 1
            else:
                out = llm.generate_structured(
                    system=SYSTEM_EXTRACT,
                    user=extraction_user_prompt(chunk.text, targets),
                    schema=LLMExtraction,
                    model=model,
                )
                calls += 1
                for f in out.facts:
                    if f.type not in targets:
                        continue
                    if not f.value.strip():
                        continue
                    if not quote_in_chunk(f.raw_text, chunk):
                        dropped += 1
                        log.info("llm_fact_unverified", chunk=chunk.id, type=f.type.value)
                        continue
                    facts.append(
                        ExtractedFact(
                            type=f.type,
                            label=LABELS[f.type][0],
                            value=f.value.strip(),
                            unit=f.unit,
                            currency=(f.currency or "").upper() or None,
                            raw_text=f.raw_text.strip(),
                            provenance=chunk.provenance,
                            source="llm",
                            confidence=weights.LLM,
                        )
                    )
        except LLMError as e:
            errors.append(f"{chunk.id}: {e}")
            log.warning("llm_chunk_failed", chunk=chunk.id, error=str(e))
    if progress:
        progress(len(chunks), len(chunks))
    return LLMPassResult(facts, answers, calls, dropped, errors)
