"""Ask the model about the parts of an act the rules could not decide (client rules 10.3, 10.6).

Stage 1 (``aoa_scope``) compares the act's subject and services with the contract's word by
word. What it cannot match is a note for a person to read (``Finding.needs_review``): a
paraphrase and another service look alike to a rule. This module is stage 2 - the local model
reads those notes - and stage 3, ``decide``: plain code that combines the two answers.

The model can only ever mark a note, and it is held to the same limits as the comparison's
model pass (``compare_llm``):

* **It is asked only what the rules left open.** A part that matches is never sent; neither is
  a number that differs, which is the rules' own evidence and final, nor a number the act merely
  adds to the contract's own words - that is a question about the number, which the rules
  already compared.
* **It never clears a note, and never makes an error.** "Same" leaves the note where it is,
  marked with the model's reading; "different" raises it to a warning at most. So text inside a
  document - which is untrusted, and wrapped as data - can move nothing further than that.
  Nor does it speak for a number: "same" about a part where the act states a number the
  contract's text does not is not applied, since that may be the same work with another count.
* **Every answer is checked before it counts.** The id must be one the model was given, both
  quotes must be in the text it was shown for that item, both sentences must be there and be its
  own - a reason copied word for word from another item's is how this model answers an item it
  did not read - and an answer the model was less than half sure of counts as no answer (one
  whose confidence is above 1, a scale it was not asked for, is refused).
* **A stored run is tied to what it answered.** Item ids are positions ("s2" is the act's second
  listed service), so the run records both documents and a digest of the items, the prompt's
  version and the model; if any of that no longer matches, the run is set aside.
* **One call for the whole batch**, for the reason ``compare_llm`` gives.

Failure is reported, never swallowed: an unreachable or slow model, or a prompt too long for the
context window, leaves an ``AOA.REVIEW`` note on the report, and the rules' notes stand.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from procurement_agent.checks.aoa_scope import (
    MATCH_SHARE,
    Alignment,
    ScopeReading,
    Verdict,
    share,
)
from procurement_agent.checks.compare_llm import _quote_problem
from procurement_agent.checks.findings import Severity, SkipNote
from procurement_agent.extraction.normalize import normalize_spaces
from procurement_agent.extraction.prompts import (
    AOA_REVIEW_PROMPT_VERSION,
    SYSTEM_AOA_REVIEW,
    aoa_review_user_prompt,
    escape_untrusted,
)
from procurement_agent.llm.base import LLMClient, LLMError
from procurement_agent.security.audit import log
from procurement_agent.security.safe_errors import safe_exc

ModelVerdict = Literal["same", "different", "unclear"]

#: The id of the act's subject; a listed service is "s<n>" (``service_id``).
SUBJECT_ID = "subject"
#: The contract's services shown beside one of the act's, the most alike first. The client's
#: template lists four, so all of them; a longer list shows the five the rules find closest.
_CANDIDATES = 5
#: An answer the model is less sure of than this counts as no answer.
_MIN_CONFIDENCE = 0.5
#: Tokens one answer takes - two quotes of about twelve words and a sentence in each language,
#: Georgian costing about twice English - with room to spare, and the JSON around them.
_TOKENS_PER_ANSWER = 320
_TOKENS_BASE = 64
_MAX_OUTPUT_TOKENS = 8192
#: How much of a rationale a finding quotes.
_RATIONALE_CHARS = 300


def service_id(n: int) -> str:
    """The id of the act's ``n``-th listed service, counting from 1."""
    return f"s{n}"


@dataclass(frozen=True)
class ScopeItem:
    """One part of the act the rules could not decide, as the model is shown it."""

    id: str
    rule_id: str
    #: the act's words: its subject clause, or one service it lists
    act: str
    #: what they are compared with: the contract's subject clause, or its services
    contract: tuple[str, ...]
    #: the act states a number its closest contract text does not (``Alignment.differ``), so
    #: the model's "same" cannot stand for it: only the rules compare numbers
    numbers_differ: bool = False

    @property
    def kind(self) -> str:
        return "subject" if self.id == SUBJECT_ID else "service"

    def contract_text(self) -> str:
        """The contract side as the prompt shows it: the clause, or one service per line."""
        if self.kind == "subject":
            return "\n".join(self.contract)
        return "\n".join(f"- {c}" for c in self.contract)


def escalated(found: Alignment) -> bool:
    """Whether the rules leave this part to the model: its words are not all the contract's.
    A number that replaces the contract's is the rules' answer already; a number only the act
    adds, in the contract's own words, is a question about a number (see the module docstring).
    """
    return found.verdict == "review" and found.share < MATCH_SHARE


def review_items(reading: ScopeReading) -> list[ScopeItem]:
    """What the model review is asked about, in the act's order: the subject, then each listed
    service, where the rules left it open (``escalated``)."""
    items: list[ScopeItem] = []
    found = reading.subject
    if found is not None and reading.contract_subject is not None and escalated(found):
        items.append(
            ScopeItem(
                SUBJECT_ID,
                "AOA.SUBJECT",
                found.item.text,
                (reading.contract_subject.text,),
                bool(found.differ),
            )
        )
    for n, found in enumerate(reading.services, 1):
        if not escalated(found):
            continue
        # most alike first; a stable sort keeps the contract's own order among equals
        ranked = sorted(reading.contract_services, key=lambda c: share(found.item, c), reverse=True)
        items.append(
            ScopeItem(
                service_id(n),
                "AOA.SERVICES",
                found.item.text,
                tuple(c.text for c in ranked[:_CANDIDATES]),
                bool(found.differ),
            )
        )
    return items


def review_digest(items: Sequence[ScopeItem], *, model: str | None) -> str:
    """A fingerprint of what a review asks: each item's id, rule and both texts, the prompt's
    version and the model. Two runs with the same digest asked the same model the same thing,
    so a stored answer can be reused - and one with another digest must not be applied."""
    rows = [(i.id, i.rule_id, i.act, list(i.contract)) for i in items]
    payload = json.dumps(
        [AOA_REVIEW_PROMPT_VERSION, model, rows], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LLMScopeAnswer(BaseModel):
    """One answer. The field order is the order the model writes them in (checked on its raw
    output: the grammar follows the schema), so it quotes and explains first and gives its
    verdict last. Asked for the verdict first, it once committed to one and then explained the
    opposite: a service the contract never lists came back "same", with a reason describing
    other work. After the verdict moved last, the verdicts agreed with their reasons.
    """

    #: ``ser_json_inf_nan``: a confidence the model wrote as 1e999 reads as infinity, which JSON
    #: would store as null - and a stored review that no longer loads is dropped without a word.
    model_config = ConfigDict(extra="forbid", ser_json_inf_nan="constants")

    item_id: str = Field(description="the item id this answer is about")
    quote_act: str = Field(description="exact quote from the item's act text")
    quote_contract: str = Field(description="exact quote from the item's contract text")
    rationale_en: str = Field(description="one short sentence in English")
    rationale_ka: str = Field(description="the same sentence in Georgian")
    verdict: ModelVerdict
    #: No bounds in the schema: a value out of range fails one answer here, not the whole call
    #: at validation. Below ``_MIN_CONFIDENCE`` (or not a number) it counts as no answer; above
    #: 1 the answer is refused (``resolve_review``).
    confidence: float = Field(description="from 0 to 1: how sure the verdict is")


class LLMScopeReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[LLMScopeAnswer] = Field(default_factory=list)


class ScopeReviewSet(BaseModel):
    """What one review run produced, as it is stored beside the act.

    Only the model's answers are kept. The rules are recomputed on every read, so a stored run
    can never pin a report to an older version of them - and every answer is checked again
    against the items as they are now when it is applied.
    """

    model: str | None = None
    items: list[LLMScopeAnswer] = Field(default_factory=list)
    notes: list[SkipNote] = Field(default_factory=list)
    #: what the run was asked about
    aoa_document_id: str
    contract_document_id: str
    digest: str
    #: the ids the model was shown; the rest did not fit in one review (see ``notes``)
    asked: list[str] = Field(default_factory=list)
    #: the model's failure, when it gave no answer at all (its note is in ``notes``)
    error: str | None = None


def _output_budget(n: int) -> int:
    return min(_MAX_OUTPUT_TOKENS, _TOKENS_BASE + _TOKENS_PER_ANSWER * n)


def _row(item: ScopeItem) -> tuple[str, str, str, str]:
    return (item.id, item.kind, item.act, item.contract_text())


def request_review(
    items: Sequence[ScopeItem],
    llm: LLMClient,
    *,
    aoa_document_id: str,
    contract_document_id: str,
    model: str | None = None,
    max_items: int = 8,
    max_chars: int = 6000,
) -> ScopeReviewSet:
    """Ask the model about ``items`` in one call, and return what it said for storing.

    As many items as fit go: at most ``max_items``, in a prompt of at most ``max_chars``
    characters of item text and markup. Never raises for a model problem - an unreachable or
    slow model comes back as a note, so a caller always has something to store.
    """
    identity = {
        "aoa_document_id": aoa_document_id,
        "contract_document_id": contract_document_id,
        "digest": review_digest(items, model=model),
    }
    notes: list[SkipNote] = []
    rows: list[tuple[str, str, str, str]] = []
    for item in items:
        if len(rows) < max_items and len(aoa_review_user_prompt([*rows, _row(item)])) <= max_chars:
            rows.append(_row(item))
    left = len(items) - len(rows)
    if left:
        notes.append(
            SkipNote(
                rule_id="AOA.REVIEW",
                reason_ka=f"კიდევ {left} შენიშვნა მოდელს არ გადაეცა — ერთ გადამოწმებაში "
                f"მაქსიმუმ {max_items} შენიშვნა და ტექსტის {max_chars} სიმბოლო ეტევა",
                reason_en=f"{left} further note(s) were not sent to the model — one review "
                f"holds at most {max_items} notes and {max_chars} characters of text",
            )
        )
    asked = [row[0] for row in rows]
    if not rows:
        return ScopeReviewSet(model=model, notes=notes, **identity)

    started = time.perf_counter()
    try:
        answer = llm.generate_structured(
            system=SYSTEM_AOA_REVIEW,
            user=aoa_review_user_prompt(rows),
            schema=LLMScopeReview,
            model=model,
            max_output_tokens=_output_budget(len(rows)),
        )
    except LLMError as e:
        log.warning("aoa_review_failed", error=safe_exc(e), items=len(rows))
        notes.append(
            SkipNote(
                rule_id="AOA.REVIEW",
                reason_ka=f"მოდელმა ვერ უპასუხა: {type(e).__name__}",
                reason_en=f"the model did not answer: {type(e).__name__}",
            )
        )
        return ScopeReviewSet(
            model=model, notes=notes, asked=asked, error=type(e).__name__, **identity
        )

    log.info(
        "aoa_review_done",
        asked=len(rows),
        returned=len(answer.items),
        seconds=round(time.perf_counter() - started, 1),
    )
    return ScopeReviewSet(model=model, items=answer.items, notes=notes, asked=asked, **identity)


# --- stage 3: the orchestrator -------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """What becomes of one part of the act: the finding's severity (None: no finding), whether
    it stays marked for a person to read, and the model's reading it carries, if any."""

    severity: Severity | None
    needs_review: bool = False
    model: Literal["same", "different"] | None = None


def decide(rules: Verdict, model: ModelVerdict | None) -> Decision:
    """Combine the rules' verdict on a part of the act with the model's, if it was asked.

    Plain code, not a model, so the same stored answer always gives the same report and every
    case below can be tested:

    ======  ==================================  ============================================
    rules   model                               result
    ======  ==================================  ============================================
    match   (never asked)                       no finding
    number  (never asked)                       error, whatever the model said
    review  different                           warning, needs review, "likely different"
    review  same                                info, needs review, "likely the same"
    review  unclear, refused, no answer, or     info, needs review; an AOA.REVIEW note says
            "same" where a number differs       why (``resolve_review``)
    review  not asked                           info, needs review
    ======  ==================================  ============================================

    So the model never clears a note and never makes an error: text inside a document can move
    a note one step, to a warning, and no further.
    """
    if rules == "match":
        return Decision(None)
    if rules == "number":
        return Decision("error")
    if model == "different":
        return Decision("warning", needs_review=True, model="different")
    if model == "same":
        return Decision("info", needs_review=True, model="same")
    return Decision("info", needs_review=True)


@dataclass(frozen=True)
class ReviewOutcome:
    """A stored review checked against this check's items (``resolve_review``)."""

    #: the model's verdict on each item it was asked about: "same" or "different" for an answer
    #: that passed every check, "unclear" for everything else it was asked
    verdicts: dict[str, ModelVerdict]
    #: the answers behind "same" and "different", for what they say
    answers: dict[str, LLMScopeAnswer]
    notes: tuple[SkipNote, ...]
    #: True when the review answered other documents or other notes and was set aside whole
    stale: bool = False


#: Why an answer was refused, in both languages: (English, Georgian).
_REJECTED: dict[str, tuple[str, str]] = {
    "unknown": (
        "the id was not one of the notes it was asked about",
        "იდენტიფიკატორი არ ეკუთვნის გადაცემულ შენიშვნებს",
    ),
    "missing": (
        "the quote was not found in the text it claimed to come from",
        "ციტატა ტექსტში ვერ მოიძებნა",
    ),
    "short": (
        "the quote was too short to show what the answer rests on",
        "ციტატა ძალიან მოკლეა, რომ პასუხი დაადასტუროს",
    ),
    "empty": ("the explanation was empty", "განმარტება ცარიელია"),
    "repeated": (
        "the explanation repeated another note's word for word",
        "განმარტება სიტყვასიტყვით იმეორებს სხვა შენიშვნისას",
    ),
    "confidence": (
        "its confidence was not a number from 0 to 1",
        "დარწმუნებულობის მაჩვენებელი 0-დან 1-მდე რიცხვი არ იყო",
    ),
}


def _note(ka: str, en: str) -> SkipNote:
    return SkipNote(rule_id="AOA.REVIEW", reason_ka=ka, reason_en=en)


def _stale(
    items: Sequence[ScopeItem],
    stored: ScopeReviewSet,
    aoa_document_id: str,
    contract_document_id: str,
) -> SkipNote | None:
    """A note saying why ``stored`` does not belong to this check, or None when it does."""
    if (stored.aoa_document_id, stored.contract_document_id) != (
        aoa_document_id,
        contract_document_id,
    ):
        return _note(
            "მოდელის პასუხები სხვა დოკუმენტების გადამოწმებას ეკუთვნის და არ გამოიყენება",
            "the model's answers belong to a review of other documents and were not applied",
        )
    if stored.digest != review_digest(items, model=stored.model):
        return _note(
            "შენიშვნები შეიცვალა მას შემდეგ, რაც მოდელს ჰკითხეს; პასუხები არ გამოიყენება — "
            "გაუშვით გადამოწმება თავიდან",
            "the notes have changed since the model was asked, so its answers were not "
            "applied; run the review again",
        )
    return None


def _answer_problem(item: ScopeItem, answer: LLMScopeAnswer) -> str | None:
    """Why ``answer`` does not stand, or None when it does. Quotes are checked against what the
    model was shown - the escaped text - with at least eight characters, or a whole line."""
    act = escape_untrusted(item.act)
    contract = escape_untrusted(item.contract_text())
    whole_act = {normalize_spaces(act)}
    whole_lines = {normalize_spaces(line) for line in contract.split("\n")}
    whole_lines |= {normalize_spaces(escape_untrusted(c)) for c in item.contract}
    problem = _quote_problem(act, answer.quote_act, whole_act) or _quote_problem(
        contract, answer.quote_contract, whole_lines
    )
    if problem is None and (not answer.rationale_en.strip() or not answer.rationale_ka.strip()):
        problem = "empty"
    if problem is None and not answer.confidence <= 1.0:
        problem = "confidence"  # a scale it was not asked for; infinity too
    return problem


def resolve_review(
    items: Sequence[ScopeItem],
    stored: ScopeReviewSet,
    *,
    aoa_document_id: str,
    contract_document_id: str,
) -> ReviewOutcome:
    """Check a stored review against this check's items, answer by answer.

    Done here rather than where the answers were generated, so a run read back from disk is held
    to exactly the same standard as one that has just arrived. The first answer about an item is
    the one that counts. What the model left open - an "unclear", a doubtful or refused answer,
    a "same" about a part whose numbers are not the contract's, or none at all - keeps the
    rules' note as it is, and a note on the report says so.
    """
    stale = _stale(items, stored, aoa_document_id, contract_document_id)
    if stale is not None:
        return ReviewOutcome({}, {}, (stale,), stale=True)
    notes: list[SkipNote] = []
    for note in stored.notes:
        if note not in notes:
            notes.append(note)

    by_id = {i.id: i for i in items}
    asked = [i for i in stored.asked if i in by_id]
    verdicts: dict[str, ModelVerdict] = dict.fromkeys(asked, "unclear")
    answers: dict[str, LLMScopeAnswer] = {}
    seen: set[str] = set()
    reasons: set[str] = set()
    rejected: dict[str, int] = {}
    unsure = numbers = 0
    for answer in stored.items:
        item = by_id.get(answer.item_id)
        if item is None or answer.item_id not in verdicts:
            rejected["unknown"] = rejected.get("unknown", 0) + 1
            continue
        if answer.item_id in seen:
            continue  # a second answer about the same item; the first counts
        seen.add(answer.item_id)
        if answer.verdict == "unclear" or not answer.confidence >= _MIN_CONFIDENCE:
            unsure += 1
            continue
        problem = _answer_problem(item, answer)
        reason = normalize_spaces(answer.rationale_en).casefold()
        if problem is None and reason in reasons:
            problem = "repeated"  # the first item's reason stands; the copy is not an answer
        reasons.add(reason)
        if problem is not None:
            rejected[problem] = rejected.get(problem, 0) + 1
            continue
        if answer.verdict == "same" and item.numbers_differ:
            numbers += 1  # the same work with another count, perhaps: the rules' question
            continue
        verdicts[answer.item_id] = answer.verdict
        answers[answer.item_id] = answer

    for reason, n in rejected.items():
        en, ka = _REJECTED[reason]
        notes.append(
            _note(
                f"მოდელის {n} პასუხი უარყოფილია — {ka}",
                f"{n} of the model's answers were rejected — {en}",
            )
        )
    if unsure:
        notes.append(
            _note(
                f"მოდელმა {unsure} შენიშვნაზე ვერ გადაწყვიტა — უპასუხა, რომ გაურკვეველია, ან "
                "ნახევარზე ნაკლებად იყო დარწმუნებული; ისინი გადასამოწმებელი რჩება",
                f"the model could not decide {unsure} note(s) — it answered unclear, or was "
                "less than half sure; they stay for a person to read",
            )
        )
    if numbers:
        notes.append(
            _note(
                f"მოდელის {numbers} პასუხი არ იქნა გამოყენებული — მოდელმა ის ხელშეკრულებით "
                "გათვალისწინებულ სამუშაოდ მიიჩნია, მაგრამ აქტში აქ ისეთი რიცხვია, რომელიც "
                "ხელშეკრულების ტექსტში არ წერია, რიცხვებს კი მხოლოდ წესები ადარებს; შენიშვნა "
                "გადასამოწმებელი რჩება",
                f"{numbers} of the model's answers were not applied — it read the text as work "
                "the contract names, but the act states a number there that the contract's text "
                "does not, and only the rules compare numbers; the note stays for a person to "
                "read",
            )
        )
    silent = len([i for i in asked if i not in seen])
    if silent and stored.error is None:  # a model that failed outright has its own note
        notes.append(
            _note(
                f"მოდელმა {silent} გადაცემულ შენიშვნაზე პასუხი არ გასცა; ისინი გადასამოწმებელი "
                "რჩება",
                f"the model gave no answer about {silent} note(s) it was asked about; they stay "
                "for a person to read",
            )
        )
    return ReviewOutcome(verdicts, answers, tuple(notes))


def _clip(text: str) -> str:
    text = normalize_spaces(text)
    if len(text) <= _RATIONALE_CHARS:
        return text
    return text[: _RATIONALE_CHARS - 1].rstrip() + "…"


def model_reading(rule_id: str, answer: LLMScopeAnswer) -> tuple[str, str]:
    """What a finding adds when the model's answer is applied to it: its reading and its reason,
    (Georgian, English)."""
    ka, en = _clip(answer.rationale_ka), _clip(answer.rationale_en)
    if rule_id == "AOA.SUBJECT":
        if answer.verdict == "different":
            return (
                f"მოდელის შეფასებით, აქტის საგანი, სავარაუდოდ, ხელშეკრულების საგნისგან "
                f"განსხვავდება: {ka}",
                f"the model reads the act's subject as likely other work than the contract's: {en}",
            )
        return (
            f"მოდელის შეფასებით, სავარაუდოდ, იგივე საგანია, სხვა სიტყვებით: {ka}",
            f"the model reads it as likely the same subject in other words: {en}",
        )
    if answer.verdict == "different":
        return (
            f"მოდელის შეფასებით, ეს მომსახურება, სავარაუდოდ, ხელშეკრულების ჩამონათვალში არ "
            f"არის: {ka}",
            f"the model reads this service as likely not one the contract lists: {en}",
        )
    return (
        f"მოდელის შეფასებით, სავარაუდოდ, ეს ხელშეკრულებაში ჩამოთვლილი მომსახურებაა, სხვა "
        f"სიტყვებით: {ka}",
        f"the model reads it as likely a service the contract lists, in other words: {en}",
    )
