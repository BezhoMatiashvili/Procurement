"""Ask the model to explain the changes the rules could not name a value for.

This pass is small on purpose and can only ever narrow the picture.

* **It never finds a difference.** ``checks/compare.py`` decides what changed; the model is
  handed a closed list of already-located passages and asked what the new wording means
  differently. It cannot add a hunk, and an answer about an id it was not given is dropped.
* **It never promotes.** A hunk the rules called substantive stays substantive. The model may
  mark one immaterial — a reordering, a synonym — and that only moves it down, into the same
  place a whitespace change sits. ``docs/ACCURACY.md`` measures this model as a precision
  regression on this corpus when it is allowed to assert; withholding is what it is good for.
* **It cannot talk down a change in substance.** A rewrite that inserts or deletes a figure or
  a negation ("არ", "not", "without") keeps its materiality whatever the model says about it -
  those are exactly the rewrites a small model calls "the same meaning".
* **Every quote is checked against the passage it claims to come from**, the same gate
  ``llm_extractor.quote_in_chunk`` applies to extraction, and it has to be long enough to prove
  something: eight characters, or the whole of a word that changed. An item whose quotes fail
  is dropped and counted, under the reason it failed.
* **A stored run is tied to the comparison it answered.** Hunk ids are positions ("h1", "h2"),
  so the run records both documents and a digest of the hunks; if either no longer matches the
  recomputed comparison, the run is set aside rather than attached to the wrong changes.
* **One call for the whole batch.** Not one per hunk: the prefix reuse that would make serial
  calls cheap does not materialise at these sizes (docs/ACCURACY.md).

Failure is reported, never swallowed. An unreachable model, a timeout or a cap leaves a
``SkipNote`` on the report and the deterministic half untouched.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from procurement_agent.checks.compare import ComparisonReport, Counts, Hunk, _short
from procurement_agent.checks.findings import Finding, SkipNote
from procurement_agent.extraction.normalize import normalize_spaces, parse_georgian_number_words
from procurement_agent.extraction.prompts import SYSTEM_COMPARE, compare_user_prompt
from procurement_agent.llm.base import LLMClient, LLMError
from procurement_agent.security.audit import log

ExplanationKind = Literal["rewritten", "expanded", "shortened", "clarified"]


class LLMChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="the change id this item describes")
    kind: ExplanationKind
    material: bool = Field(description="false when the wording means the same thing")
    summary_en: str = Field(description="one short sentence in English")
    summary_ka: str = Field(description="the same sentence in Georgian")
    quote_left: str = Field(description="exact quote from the old text")
    quote_right: str = Field(description="exact quote from the new text")


class LLMComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[LLMChange] = Field(default_factory=list)


class ExplanationSet(BaseModel):
    """What one explain run produced, as it is stored beside the document.

    Only the model's answers and the notes are kept. The deterministic comparison is cheap and
    is recomputed on every read, so a stored run can never pin the report to an older version of
    the rules - and every quote is re-checked against the passage when it is merged back in.
    """

    model: str | None = None
    items: list[LLMChange] = Field(default_factory=list)
    notes: list[SkipNote] = Field(default_factory=list)
    #: What the run was asked about. Optional so a run stored before these existed still loads;
    #: such a run is re-validated quote by quote like any other, just without this check.
    left_document_id: str | None = None
    right_document_id: str | None = None
    hunks_digest: str | None = None


def hunks_digest(report: ComparisonReport) -> str:
    """A fingerprint of a comparison's hunks: their ids, kinds and texts, in order.

    Two comparisons with the same digest assign the same id to the same change. Explanations
    and materiality are left out on purpose, so applying a run does not change the digest.
    """
    rows = [
        (
            h.id,
            h.kind,
            h.left.path if h.left else None,
            h.left.text if h.left else None,
            h.right.path if h.right else None,
            h.right.text if h.right else None,
        )
        for h in report.hunks
    ]
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: A quote shorter than this proves nothing on its own ("აქ" occurs in half the passages)...
_MIN_QUOTE = 8
#: ...unless it is the whole of a word that changed.

#: Words that turn a statement into its opposite. A rewrite adding or removing one is never
#: "the same meaning", whatever the model says.
_NEGATIONS = frozenset({"არ", "ვერ", "არა", "არც", "ნუ", "გარეშე", "not", "no", "without", "never"})
_DIGIT = re.compile(r"\d")
_WORD = re.compile(r"\w+")


def _contains(haystack: str, quote: str) -> bool:
    """Whitespace-insensitive substring check — the ``quote_in_chunk`` gate, for a passage."""
    needle = normalize_spaces(quote)
    return bool(needle) and needle in normalize_spaces(haystack)


def _changed_parts(hunk: Hunk) -> tuple[set[str], set[str]]:
    """What the word diff removed from the old text and added to the new, per side."""
    old: set[str] = set()
    new: set[str] = set()
    for op in hunk.inline:
        if op.tag in ("delete", "replace") and op.left.strip():
            old.add(normalize_spaces(op.left))
        if op.tag in ("insert", "replace") and op.right.strip():
            new.add(normalize_spaces(op.right))
    return old, new


def _quote_problem(passage: str, quote: str, changed: set[str]) -> str | None:
    """Why a quote does not support an answer, or None when it does."""
    needle = normalize_spaces(quote)
    if not needle or needle not in normalize_spaces(passage):
        return "missing"
    if len(needle) < _MIN_QUOTE and needle not in changed:
        return "short"
    return None


def _changes_substance(hunk: Hunk) -> bool:
    """True when the edit inserts or deletes a figure or a negation.

    The rules leave such a hunk as a plain "text changed" when no typed value could be read on
    either side, but a model is not allowed to call it immaterial.
    """
    for op in hunk.inline:
        if op.tag == "equal":
            continue
        for part in (op.left, op.right):
            if _DIGIT.search(part):
                return True
            if any(w.lower() in _NEGATIONS for w in _WORD.findall(part)):
                return True
            # a number written in words is a figure too: "ხუთი" → "ოცდაათი" days
            if any(parse_georgian_number_words(w) is not None for w in _WORD.findall(part)):
                return True
    return False


def _texts(hunk: Hunk) -> tuple[str, str]:
    return (hunk.left.text if hunk.left else "", hunk.right.text if hunk.right else "")


def request_explanations(
    report: ComparisonReport,
    llm: LLMClient,
    *,
    model: str | None = None,
    max_hunks: int = 8,
) -> ExplanationSet:
    """Ask the model about the changes the rules could not name a value for.

    Never raises for a model problem: an unreachable or slow model comes back as a note, so a
    caller always has something to store and the deterministic half is never lost.
    """
    candidates = report.unexplained[:max_hunks]
    notes: list[SkipNote] = []
    if not candidates:
        return ExplanationSet(model=model, notes=notes, **_identity(report))

    over = len(report.unexplained) - len(candidates)
    if over > 0:
        notes.append(
            SkipNote(
                rule_id="CMP.EXPLAIN",
                reason_ka=f"კიდევ {over} ცვლილება მოდელს არ გადაეცა — ერთ გაშვებაზე "
                f"მაქსიმუმ {max_hunks} ცვლილებაა",
                reason_en=f"{over} further changes were not sent to the model — one run covers "
                f"at most {max_hunks}",
            )
        )

    changes = [(h.id, *_texts(h)) for h in candidates]
    started = time.perf_counter()
    try:
        answer = llm.generate_structured(
            system=SYSTEM_COMPARE,
            user=compare_user_prompt(changes),
            schema=LLMComparison,
            model=model,
        )
    except LLMError as e:
        log.warning("compare_explain_failed", error=repr(e), hunks=len(candidates))
        notes.append(
            SkipNote(
                rule_id="CMP.EXPLAIN",
                reason_ka=f"მოდელმა ვერ უპასუხა: {type(e).__name__}",
                reason_en=f"the model did not answer: {type(e).__name__}",
            )
        )
        return ExplanationSet(model=model, notes=notes, **_identity(report))

    log.info(
        "compare_explain_done",
        asked=len(candidates),
        returned=len(answer.items),
        seconds=round(time.perf_counter() - started, 1),
    )
    return ExplanationSet(model=model, items=answer.items, notes=notes, **_identity(report))


def _identity(report: ComparisonReport) -> dict[str, str]:
    return {
        "left_document_id": report.left_document_id,
        "right_document_id": report.right_document_id,
        "hunks_digest": hunks_digest(report),
    }


#: Why an answer was refused, in both languages: (English, Georgian).
_REJECTED: dict[str, tuple[str, str]] = {
    "unknown": (
        "the id was not one of the changes it was asked about",
        "იდენტიფიკატორი არ ეკუთვნის გადაცემულ ცვლილებებს",
    ),
    "missing": (
        "the quote was not found in the passage it claimed to come from",
        "ციტატა ტექსტში ვერ მოიძებნა",
    ),
    "short": (
        "the quote was too short to show where the change is",
        "ციტატა ძალიან მოკლეა, რომ ცვლილება დაადასტუროს",
    ),
    "empty": ("the summary was empty", "აღწერა ცარიელია"),
}


def _add_note(skipped: list[SkipNote], note: SkipNote) -> None:
    if note not in skipped:
        skipped.append(note)


def _stale(report: ComparisonReport, stored: ExplanationSet) -> SkipNote | None:
    """A note saying why ``stored`` does not belong to ``report``, or None when it does."""
    if (
        stored.left_document_id is not None and stored.left_document_id != report.left_document_id
    ) or (
        stored.right_document_id is not None
        and stored.right_document_id != report.right_document_id
    ):
        return SkipNote(
            rule_id="CMP.EXPLAIN",
            reason_ka="მოდელის პასუხები სხვა დოკუმენტების შედარებას ეკუთვნის და არ გამოიყენება",
            reason_en="the model's answers belong to a comparison of different documents "
            "and were not applied",
        )
    if stored.hunks_digest is not None and stored.hunks_digest != hunks_digest(report):
        return SkipNote(
            rule_id="CMP.EXPLAIN",
            reason_ka="შედარება შეიცვალა მას შემდეგ, რაც მოდელს ჰკითხეს; პასუხები არ "
            "გამოიყენება — გაუშვით ახსნა თავიდან",
            reason_en="the comparison has changed since the model was asked, so its answers "
            "were not applied; run the explanation again",
        )
    return None


def apply_explanations(report: ComparisonReport, stored: ExplanationSet) -> ComparisonReport:
    """Merge a stored explain run into a freshly computed comparison.

    Every item is re-validated here rather than where it was generated, so a result read back
    from disk is held to exactly the same standard as one that has just arrived: the run must
    belong to this comparison, the id must be a hunk that still needs explaining, and both
    quotes must appear in that hunk's own text. Applying the same run twice changes nothing
    the second time.
    """
    skipped = list(report.skipped)
    stale = _stale(report, stored)
    if stale is not None:
        _add_note(skipped, stale)
        return report.model_copy(update={"skipped": skipped})
    for note in stored.notes:
        _add_note(skipped, note)

    by_id = {h.id: h for h in report.hunks if h.needs_explaining}
    accepted: dict[str, LLMChange] = {}
    rejected: dict[str, int] = {}
    for item in stored.items:
        hunk = by_id.get(item.id)
        if hunk is None:
            # an id the model was not given, or a hunk the rules now explain
            rejected["unknown"] = rejected.get("unknown", 0) + 1
            continue
        if hunk.explained_by is not None or item.id in accepted:
            continue  # already applied; not a rejection
        left, right = _texts(hunk)
        old_parts, new_parts = _changed_parts(hunk)
        problem = _quote_problem(left, item.quote_left, old_parts) or _quote_problem(
            right, item.quote_right, new_parts
        )
        if problem is None and (not item.summary_en.strip() or not item.summary_ka.strip()):
            problem = "empty"
        if problem is not None:
            rejected[problem] = rejected.get(problem, 0) + 1
            continue
        accepted[item.id] = item

    for reason, n in rejected.items():
        en, ka = _REJECTED[reason]
        _add_note(
            skipped,
            SkipNote(
                rule_id="CMP.EXPLAIN",
                reason_ka=f"მოდელის {n} პასუხი უარყოფილია — {ka}",
                reason_en=f"{n} of the model's answers were rejected — {en}",
            ),
        )
    if not accepted:
        return report.model_copy(
            update={"skipped": skipped, "llm_model": stored.model or report.llm_model}
        )

    hunks: list[Hunk] = []
    counts = report.counts.model_copy()
    demoted: list[Hunk] = []
    for hunk in report.hunks:
        explained = accepted.get(hunk.id)
        if explained is None:
            hunks.append(hunk)
            continue
        # The model may only move a change down (see the module docstring), and never one
        # whose edit touches a figure or a negation.
        demote = (
            not explained.material
            and hunk.materiality == "substantive"
            and not _changes_substance(hunk)
        )
        if demote:
            _move(counts, "substantive", "cosmetic")
            demoted.append(hunk)
        hunks.append(
            hunk.model_copy(
                update={
                    "explanation_en": explained.summary_en.strip(),
                    "explanation_ka": explained.summary_ka.strip(),
                    "explained_by": "llm",
                    "materiality": "cosmetic" if demote else hunk.materiality,
                }
            )
        )

    return report.model_copy(
        update={
            "hunks": hunks,
            "counts": counts,
            "skipped": skipped,
            "llm_explained": sum(1 for h in hunks if h.explained_by == "llm"),
            "llm_model": stored.model,
            # a change moved down to cosmetic keeps no "text changed" finding of its own: the
            # list, the workbook and the Word comments must say what the diff says
            "findings": [f for f in report.findings if not _is_finding_of(f, demoted)],
        }
    )


def _is_finding_of(finding: Finding, hunks: list[Hunk]) -> bool:
    if finding.rule_id != "CMP.TEXT_CHANGED":
        return False
    for h in hunks:
        left, right = _texts(h)
        if (finding.expected, finding.actual) == (_short(left or None), _short(right or None)):
            return True
    return False


def _move(counts: Counts, src: str, dst: str) -> None:
    setattr(counts, src, getattr(counts, src) - 1)
    setattr(counts, dst, getattr(counts, dst) + 1)


def explain(
    report: ComparisonReport,
    llm: LLMClient,
    *,
    model: str | None = None,
    max_hunks: int = 8,
) -> ComparisonReport:
    """Ask, then merge. The two halves are separate so a background job can store the middle."""
    return apply_explanations(
        report, request_explanations(report, llm, model=model, max_hunks=max_hunks)
    )
