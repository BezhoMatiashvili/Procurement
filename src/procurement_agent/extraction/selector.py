"""Chunk selection: decide which chunks are worth an (expensive, CPU-bound) LLM call.

Scoring is purely lexical — keyword lexicons per fact type, rule hits already found in the
chunk, and overlap with a free-form question. Georgian inflection is handled by matching on
word prefixes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from procurement_agent.extraction.rules import KEYWORDS, RuleHit, keyword_in
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion.chunker import Chunk

_EXTRAKEYWORDS: dict[FactType, tuple[str, ...]] = {
    FactType.party_name: (
        "შპს", "სს ", "ი/მ", "llc", "ltd", "jsc", "კომპანი", "company",
        "მიმწოდებელ", "supplier", "შემსყიდველ", "buyer",
    ),
    FactType.party_director: (
        "დირექტორ", "director", "წარმომადგენ", "represented", "ხელმომწერ", "signatory", "სახელით",
    ),
    FactType.party_id_code: ("ს/კ", "საიდენტიფიკაციო", "id code", "identification", "tax id"),
    FactType.contract_number: ("№", "no.", "#", "ნომერ", "number", "ref"),
    FactType.quantity: ("რაოდენობ", "ცალი", "კომპლ", "quantity", "qty", "pcs", "units"),
}  # fmt: skip

_WORD = re.compile("[\\w\u10a0-\u10ff]{3,}", re.UNICODE)


def _keywords_for(targets: list[FactType]) -> set[str]:
    kws: set[str] = set()
    for t in targets:
        kws.update(KEYWORDS.get(t, ()))
        kws.update(_EXTRAKEYWORDS.get(t, ()))
    return kws


def _question_stems(question: str) -> set[str]:
    stems: set[str] = set()
    for w in _WORD.findall(question.lower()):
        stems.add(w[:5] if len(w) > 5 else w)  # crude stemming works for Georgian suffixes
    return stems


@dataclass(slots=True)
class ScoredChunk:
    chunk: Chunk
    score: float


def score_chunks(
    chunks: list[Chunk],
    *,
    targets: list[FactType],
    question: str | None,
    rule_hits: dict[str, list[RuleHit]],
) -> list[ScoredChunk]:
    kws = _keywords_for(targets)
    stems = _question_stems(question) if question else set()
    scored: list[ScoredChunk] = []
    for idx, ch in enumerate(chunks):
        low = ch.text.lower()
        s = 0.0
        s += sum(1.0 for k in kws if keyword_in(low, k))
        s += min(len(rule_hits.get(ch.id, [])), 6) * 0.5
        if stems:
            s += sum(2.0 for st in stems if st in low)
        if idx == 0:
            s += 1.5  # the first chunk usually names the parties, number and date
        if ch.headings and any(keyword_in(" ".join(ch.headings).lower(), k) for k in kws):
            s += 1.0
        scored.append(ScoredChunk(ch, s))
    return sorted(scored, key=lambda x: x.score, reverse=True)


def select_chunks(
    scored: list[ScoredChunk], *, token_budget: int, min_score: float = 1.0, max_chunks: int = 12
) -> list[Chunk]:
    chosen: list[Chunk] = []
    used = 0
    for sc in scored:
        if len(chosen) >= max_chunks:
            break
        if sc.score < min_score and chosen:
            break
        if used + sc.chunk.token_estimate > token_budget and chosen:
            continue
        chosen.append(sc.chunk)
        used += sc.chunk.token_estimate
    # keep document order for the LLM; it reads more naturally and merges better
    order = {id(c): i for i, c in enumerate(chosen)}
    return sorted(chosen, key=lambda c: (c.provenance.page or 0, _natural_key(c.id), order[id(c)]))


_DIGIT_RUN = re.compile(r"(\d+)")


def _natural_key(chunk_id: str) -> tuple[tuple[int, int | str], ...]:
    """Sort key that orders "doc:2" before "doc:10".

    Chunk ids end in a running index, and a plain string sort put the tenth chunk of a page
    between the first and the second, handing the model the document out of order.
    """
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in _DIGIT_RUN.split(chunk_id)
        if part
    )
