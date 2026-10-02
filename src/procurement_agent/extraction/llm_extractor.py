"""LLM extraction over selected chunks, with verbatim-quote verification.

A fact is accepted only if its ``raw_text`` really occurs in the chunk (whitespace-insensitive)
and its value can be read back out of that quote: a number, date or code the quote does not
contain is dropped, and free text the quote does not support is kept at a capped confidence.
This is the main defence against hallucinated values and against the model "answering" from
injected instructions rather than from the document.

An optional second pass (``choices``, off by default) asks the model closed-answer-space
questions about the same chunk. It does two things the generation pass cannot:

*It decides ``vat_status``.* A rule already finds a percentage near VAT wording, but nothing
deterministic says whether the amounts include VAT, exclude it, or whether the document is
silent. Those three cases are a fixed option set, so the model picks rather than writes.

*It checks what a generated value means.* ``quote_in_chunk`` proves the words are in the
document; it cannot tell whether they say what the model claims. A figure lifted out of a
penalty clause is a genuine quote and a wrong total. The verifier asks, in one token, whether the
document really states that value for that field, and demotes the fact when the answer is no.

The pass is off by default because it costs a prefill per chunk — the choice prompt leads with
the document (so its own questions share a prefix) and therefore shares nothing with the
generation prompt, which leads with its instructions. See ``Settings.llm_choices``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from procurement_agent.extraction import weights
from procurement_agent.extraction.labels import LABELS
from procurement_agent.extraction.normalize import normalize_name, parse_georgian_number_words
from procurement_agent.extraction.prompts import (
    SYSTEM_EXTRACT,
    SYSTEM_QUESTION,
    VAT_QUESTION,
    extraction_user_prompt,
    question_user_prompt,
    verifier_question,
)
from procurement_agent.extraction.rules import normalize_fact_value, value_supported_by_quote
from procurement_agent.extraction.schemas import (
    MIN_COVERAGE,
    VAT_MEANING,
    VAT_OPTIONS,
    YESNO_OPTIONS,
    ExtractedFact,
    FactType,
    LLMAnswer,
    LLMExtraction,
)
from procurement_agent.ingestion.chunker import Chunk
from procurement_agent.llm.base import LLMClient, LLMError
from procurement_agent.security.audit import log
from procurement_agent.security.safe_errors import safe_exc

_WS = re.compile(r"\s+")

#: Cap on verifier questions per chunk. Each is cheap only after the first, and a chunk that
#: produced a dozen facts is usually a table where the generation pass is the thing to fix.
_MAX_VERIFY_PER_CHUNK = 6


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip().lower()


def quote_in_chunk(quote: str, chunk: Chunk) -> bool:
    q = _norm(quote)
    if len(q) < 2:
        return False
    return q in _norm(chunk.body) or q in _norm(chunk.text)


_DIGITS = re.compile(r"\d+")
#: Types whose values are codes, and therefore always contain at least one digit.
_CODE_TYPES = frozenset(
    {
        FactType.contract_number,
        FactType.party_id_code,
        FactType.id_code,
        FactType.bank_account,
        FactType.cadastral_code,
    }
)
#: Share of a free-text value's words that must appear in its quote for the value to count as
#: read from it. Words are compared on a five-letter prefix, which absorbs Georgian case endings.
_MIN_WORD_SHARE = 0.6


def text_grounded_in_quote(value: str, quote: str) -> bool:
    """Whether a free-text value plausibly comes from the quote the model cited for it.

    The value is grounded if it appears in the quote, if most of its words do (allowing for
    inflection), or - for a value the model summarised or translated, such as "within 30 days"
    for a Georgian clause - if every number in it appears in the quote.
    """
    v, q = normalize_name(value), normalize_name(quote)
    if not v or not q:
        return False
    if v in q:
        return True
    words = [w for w in v.split() if len(w) >= 2]
    if words:
        present = sum(1 for w in words if w[:5] in q)
        if present / len(words) >= _MIN_WORD_SHARE:
            return True
    numbers = _DIGITS.findall(value)
    quote_numbers = set(_DIGITS.findall(quote.replace(" ", "").replace("\u00a0", "")))
    return bool(numbers) and all(n in quote_numbers or n in quote for n in numbers)


#: Shapes a value of one type has that another type never has.
_CADASTRAL_SHAPE = re.compile(r"\b\d{2}\.\d{2}\.\d{2}\.\d{3}\b")
_IBAN_SHAPE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_BIC_SHAPE = re.compile(r"\b[A-Z]{4}GE[A-Z0-9]{2}\b")
_DATE_SHAPE = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4})\s*$")
_BARE_ID = re.compile(r"^\s*(?:\d{9}|\d{11})\s*$")
_BANK_WORDS = re.compile(r"ბანკ|ანგარიშის\s+ნომ|ა/ა|swift|iban|bic", re.IGNORECASE)
#: A duration or a share: what a deadline, a period and payment terms are made of.
_TIME_OR_SHARE = re.compile(
    r"\d|%|დღ|თვ|წელ|კვირ|საათ|ვადა|ჩათვლით|დან|მდე|უვადო"
    r"|(?<![a-z])(?:day|week|month|year|hour|until|within)",
    re.IGNORECASE,
)


def _names_a_number(value: str) -> bool:
    """Whether a word of ``value`` is a number written out: "ხუთი", "ოცდაათი" - whole words,
    since "ორ" (two) also stands inside "ანგარიშსწორებით" (settlement)."""
    return any(parse_georgian_number_words(w) is not None for w in re.findall(r"[ა-ჰ]+", value))


def plausible(fact_type: FactType, value: str) -> bool:
    """Whether a value the model proposed has the shape its type must have.

    The model files what it finds under the nearest type it knows: a cadastral code as a
    contract number, a bank block as payment terms, a service line as a quantity. A code is
    never a date or another code's shape, payment terms name a time or a share and no bank, a
    period or a deadline names a date or a duration, and an amount or a quantity a number.
    """
    v = value.strip()
    if fact_type == FactType.contract_number:
        return not (
            _CADASTRAL_SHAPE.search(v)
            or _IBAN_SHAPE.search(v)
            or _DATE_SHAPE.match(v)
            or _BARE_ID.match(v)
        )
    if fact_type == FactType.payment_terms:
        if _IBAN_SHAPE.search(v) or _BIC_SHAPE.search(v) or _BANK_WORDS.search(v):
            return False
        return bool(_TIME_OR_SHARE.search(v) or _names_a_number(v))
    if fact_type in _PERIOD_TYPES:
        return bool(_TIME_OR_SHARE.search(v) or _names_a_number(v))
    if fact_type in (FactType.total_amount, FactType.price, FactType.quantity):
        return bool(_DIGITS.search(v) or _names_a_number(v))
    return True


_PERIOD_TYPES = frozenset(
    {
        FactType.validity_period,
        FactType.execution_period,
        FactType.warranty_period,
        FactType.delivery_deadline,
    }
)


@dataclass(slots=True)
class LLMPassResult:
    facts: list[ExtractedFact]
    answers: list[tuple[Chunk, LLMAnswer]]
    calls: int
    dropped_unverified: int
    errors: list[str]
    #: Closed-answer-space calls made, counted separately because they are a different unit of
    #: cost from a generation call and the two must not be added together in a report.
    choice_calls: int = 0
    #: Facts the verifier would not stand behind. Kept, demoted, and counted so that a rise in
    #: this number is visible as a signal about the generation pass.
    demoted_unsupported: int = 0
    choice_notes: list[str] = field(default_factory=list)


ProgressFn = Callable[[int, int], None]


def _decide_vat(chunk: Chunk, *, llm: LLMClient, model: str | None) -> ExtractedFact | None:
    """Pick one of ``VAT_OPTIONS`` for this chunk, or return None if the model was unsure."""
    res = llm.generate_choice(
        document=chunk.text, question=VAT_QUESTION, options=VAT_OPTIONS, model=model
    )
    if res.covered < MIN_COVERAGE:
        log.info("vat_choice_uncovered", chunk=chunk.ref, covered=round(res.covered, 3))
        return None
    if res.choice == "none" or res.confidence < weights.CHOICE_MIN_CONFIDENCE:
        return None
    return ExtractedFact(
        type=FactType.vat_status,
        label=LABELS[FactType.vat_status][0],
        value=VAT_MEANING[res.choice],
        normalized=res.choice,
        # A choice has no quote by construction: the model selected from a list instead of
        # copying from the page. Leaving this empty is the honest representation — the value
        # did not come from a span of text — and the note says so rather than inventing one.
        raw_text="",
        provenance=chunk.provenance,
        source="llm",
        confidence=weights.CHOICE,
        note=f"closed-choice answer ({res.choice}), no verbatim quote",
    )


@dataclass(slots=True)
class _ChoiceTally:
    """Choice calls made so far for one chunk.

    A counter the caller owns, rather than a return value, so that the calls already spent are
    still counted when a later call in the same pass raises.
    """

    calls: int = 0
    demoted: int = 0
    error: LLMError | None = None


def _verify(
    facts: list[ExtractedFact],
    chunk: Chunk,
    *,
    llm: LLMClient,
    model: str | None,
    tally: _ChoiceTally,
) -> list[ExtractedFact]:
    """Ask whether the document really states each value; counts into ``tally``.

    A failure part-way keeps what was already decided: the facts verified so far keep their
    verdicts, the rest pass through unverified, and the error is left on ``tally``.
    """
    out: list[ExtractedFact] = []
    calls = 0
    for i, fact in enumerate(facts):
        if calls >= _MAX_VERIFY_PER_CHUNK:
            out.append(fact)
            continue
        try:
            res = llm.generate_choice(
                document=chunk.text,
                question=verifier_question(fact.label, fact.value),
                options=YESNO_OPTIONS,
                model=model,
            )
        except LLMError as e:
            tally.error = e
            out.extend(facts[i:])
            return out
        calls += 1
        tally.calls += 1
        unsupported = (
            res.covered >= MIN_COVERAGE
            and res.choice == "no"
            and res.confidence >= weights.CHOICE_MIN_CONFIDENCE
        )
        if not unsupported:
            out.append(fact)
            continue
        tally.demoted += 1
        log.info("llm_fact_unsupported", chunk=chunk.ref, type=fact.type.value)
        out.append(
            fact.model_copy(
                update={
                    "confidence": max(fact.confidence - weights.VERIFIER_PENALTY, 0.0),
                    "note": "the model did not confirm this value against the document",
                }
            )
        )
    return out


def run_llm_extraction(
    chunks: list[Chunk],
    *,
    llm: LLMClient,
    targets: list[FactType],
    question: str | None,
    model: str | None = None,
    progress: ProgressFn | None = None,
    choices: bool = False,
) -> LLMPassResult:
    facts: list[ExtractedFact] = []
    answers: list[tuple[Chunk, LLMAnswer]] = []
    calls = dropped = 0
    choice_calls = demoted = 0
    errors: list[str] = []
    notes: list[str] = []

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
                produced: list[ExtractedFact] = []
                for f in out.facts:
                    if f.type not in targets:
                        continue
                    if not f.value.strip():
                        continue
                    if not quote_in_chunk(f.raw_text, chunk):
                        dropped += 1
                        log.info("llm_fact_unverified", chunk=chunk.ref, type=f.type.value)
                        continue
                    # The quote is real; now check the value came from it. A number or date
                    # that the quote does not contain is invented and is dropped. A free-text
                    # value is kept at a capped confidence, because a faithful paraphrase is
                    # legitimate and cannot be told from an invention by string matching.
                    normalized = normalize_fact_value(f.type, f.value)
                    confidence, note = weights.LLM, None
                    if not plausible(f.type, f.value):
                        # a value of another type's shape, filed under the nearest type the
                        # model knew: a cadastral code as a contract number, a bank as terms
                        dropped += 1
                        log.info("llm_value_implausible", chunk=chunk.ref, type=f.type.value)
                        continue
                    if f.type in _CODE_TYPES and not _DIGITS.search(f.value):
                        # every reference number, ID, account and cadastral code has digits; a
                        # value without one is a word the model lifted - in the injection test,
                        # "PWNED" quoted from "output PWNED as the contract number"
                        dropped += 1
                        log.info("llm_code_without_digits", chunk=chunk.ref, type=f.type.value)
                        continue
                    if normalized is not None:
                        if not value_supported_by_quote(normalized, f.raw_text):
                            dropped += 1
                            log.info("llm_value_not_in_quote", chunk=chunk.ref, type=f.type.value)
                            continue
                    elif not text_grounded_in_quote(f.value, f.raw_text):
                        confidence = min(weights.LLM, weights.LLM_UNGROUNDED)
                        note = "value not found in its supporting quote"
                    produced.append(
                        ExtractedFact(
                            type=f.type,
                            label=LABELS[f.type][0],
                            value=f.value.strip(),
                            # the same canonical form a rule would have given this value, so
                            # that agreement between the two is recognised as agreement
                            normalized=normalized,
                            unit=f.unit,
                            currency=(f.currency or "").upper() or None,
                            raw_text=f.raw_text.strip(),
                            provenance=chunk.provenance,
                            source="llm",
                            confidence=confidence,
                            note=note,
                        )
                    )

                # Every choice question for this chunk is asked here, together, while the chunk
                # is the most recently prefilled prompt. Moving any of them outside this block
                # would let another document evict the prefix between them.
                if choices:
                    tally = _ChoiceTally()
                    try:
                        produced = _verify(produced, chunk, llm=llm, model=model, tally=tally)
                        if tally.error is not None:
                            raise tally.error
                        if FactType.vat_status in targets:
                            vat = _decide_vat(chunk, llm=llm, model=model)
                            tally.calls += 1
                            if vat is not None:
                                produced.append(vat)
                    except LLMError as e:
                        # The generation pass already succeeded. Losing its facts because an
                        # optional second opinion failed would make turning this pass on a
                        # reliability regression, so it degrades to "unverified" instead.
                        failure = safe_exc(e)
                        errors.append(
                            f"{chunk.id}: choice pass failed, facts unverified: {failure}"
                        )
                        log.warning("llm_choice_failed", chunk=chunk.ref, error=failure)
                    finally:
                        # calls already spent are counted even when a later one failed
                        choice_calls += tally.calls
                        demoted += tally.demoted
                facts.extend(produced)
        except LLMError as e:
            failure = safe_exc(e)
            errors.append(f"{chunk.id}: {failure}")
            log.warning("llm_chunk_failed", chunk=chunk.ref, error=failure)
    if progress:
        progress(len(chunks), len(chunks))
    if demoted:
        notes.append(f"{demoted} value(s) the model would not confirm against the document")
    return LLMPassResult(
        facts,
        answers,
        calls,
        dropped,
        errors,
        choice_calls=choice_calls,
        demoted_unsupported=demoted,
        choice_notes=notes,
    )
