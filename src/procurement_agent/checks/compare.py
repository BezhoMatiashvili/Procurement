"""Compare two documents: where the text changed, and which of those changes matter.

Two comparisons run together, because neither is sufficient on its own.

**Positional.** Segments are aligned with ``difflib`` exactly as ``checks/boq.py`` aligns BOQ
lines, producing a git-style diff: added, removed, changed and moved, with a word-level
highlight inside each changed line. Measured on the client's acceptance acts, the correct act
against each planted-mistake revision aligns at 0.87-0.98 similarity and lands 1-7 hunks
straight on the planted errors.

**Semantic.** The same alignment is useless across unrelated documents: the acceptance act
against its contract draft aligns at 0.08, where a positional diff is noise. So every changed
region is also read with ``rules.run_rules`` and ``anchors.find_anchors`` on *each side* and the
typed values are paired and compared through ``extraction/normalize`` - and the whole document
is compared the same way, independently of alignment, which is what still answers "did the bank
details change?" when the two files are not versions of each other at all.

Classification is per **entity**, never per hunk. A prototype that judged whole hunks filed the
client's Mistake #3 - a bank code naming a different bank - as "text changed", because the
digits in that paragraph were untouched. Reading the entities out of both sides is what makes
that a critical banking difference instead of a line that went red.

No LLM is involved here. ``checks/compare_llm.py`` can explain the changes this module cannot
classify, and it may only downgrade and annotate what is decided here.
"""

from __future__ import annotations

import bisect
import difflib
import math
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from procurement_agent.checks.amounts import NUM_IN_TEXT, number_of, word_amounts
from procurement_agent.checks.findings import Finding, Severity, SkipNote
from procurement_agent.extraction.anchors import bic_is_well_formed, find_anchors
from procurement_agent.extraction.labels import LABELS
from procurement_agent.extraction.normalize import (
    amounts_equal,
    confusable_chars,
    iban_is_valid,
    names_match,
    normalize_confusables,
    normalize_name,
    normalize_spaces,
    parse_number,
    strip_quotes,
)
from procurement_agent.extraction.rules import run_rules
from procurement_agent.extraction.schemas import FactType
from procurement_agent.extraction.spellcheck import classify_difference
from procurement_agent.ingestion.segmenter import Segment, document_text, key_of

ChangeKind = Literal["added", "removed", "changed", "moved"]
Materiality = Literal["critical", "substantive", "cosmetic"]
Alignment = Literal["versions", "unrelated"]

# --- what counts as important ---------------------------------------------------------------

#: The specific procurement types, and the only ones the *document-wide* pass compares. Keeping
#: the generic hits out of that pass is what stops an unrelated pair - an act against its own
#: contract draft - from filling the summary panel with every stray number in either file.
FIELD_TYPES: frozenset[FactType] = frozenset(
    {
        FactType.price,
        FactType.total_amount,
        FactType.vat_status,
        FactType.quantity,
        FactType.delivery_deadline,
        FactType.execution_period,
        FactType.payment_terms,
        FactType.warranty_period,
        FactType.validity_period,
        FactType.penalty,
        FactType.contract_date,
        FactType.contract_number,
        FactType.party_name,
        FactType.party_id_code,
        FactType.party_director,
        FactType.party_address,
        FactType.bank_account,
        FactType.cadastral_code,
        FactType.project_name,
    }
)

#: A difference in one of these is an error rather than a warning. The generic hits are in here
#: even though they are not in FIELD_TYPES, and the distinction is the whole point: for
#: *extraction* an unclassified amount is a low-confidence guess at a field, but for a
#: *comparison* it is a number that changed between two versions, which is exactly what the
#: client asked to be shown - prices and deadlines, whether or not a label was found for them.
CRITICAL_TYPES: frozenset[FactType] = FIELD_TYPES | frozenset(
    {
        FactType.amount,
        FactType.date,
        FactType.duration,
        FactType.percentage,
    }
)

_NUMERIC = frozenset({FactType.price, FactType.total_amount, FactType.amount, FactType.quantity})
_DATES = frozenset({FactType.contract_date, FactType.date})
_NAMES = frozenset({FactType.party_name, FactType.party_director, FactType.project_name})
_BANK = frozenset({FactType.bank_account})
_DURATIONS = frozenset(
    {
        FactType.duration,
        FactType.delivery_deadline,
        FactType.execution_period,
        FactType.warranty_period,
        FactType.validity_period,
    }
)

_RULE_FOR_TYPE: dict[FactType, str] = {
    FactType.price: "CMP.AMOUNT",
    FactType.total_amount: "CMP.AMOUNT",
    FactType.amount: "CMP.AMOUNT",
    FactType.quantity: "CMP.QUANTITY",
    FactType.contract_date: "CMP.DATE",
    FactType.date: "CMP.DATE",
    FactType.bank_account: "CMP.BANK",
    FactType.party_name: "CMP.PARTY",
    FactType.party_director: "CMP.PARTY",
    FactType.party_address: "CMP.PARTY",
    FactType.party_id_code: "CMP.ID_CODE",
    FactType.id_code: "CMP.ID_CODE",
    FactType.cadastral_code: "CMP.CADASTRAL",
    FactType.contract_number: "CMP.CONTRACT_NO",
    FactType.project_name: "CMP.PROJECT",
    FactType.percentage: "CMP.PERCENT",
    FactType.penalty: "CMP.PERCENT",
    FactType.vat_status: "CMP.VAT",
    FactType.duration: "CMP.DURATION",
    FactType.delivery_deadline: "CMP.DURATION",
    FactType.execution_period: "CMP.DURATION",
    FactType.warranty_period: "CMP.DURATION",
    FactType.validity_period: "CMP.DURATION",
    FactType.payment_terms: "CMP.PAYMENT",
    FactType.subject: "CMP.SUBJECT",
}
_DEFAULT_RULE = "CMP.VALUE"

#: Zero-width and bidirectional control characters. Invisible on screen and in print, and a
#: classic way to make two documents that read identically differ where it counts.
#: Ranges rather than literals, so this file cannot itself contain what it looks for.
#: bandit's B613 flags a source file carrying bidirectional controls, and it is right to:
#: the first version of this line was itself unreadable in a diff.
_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD),  # soft hyphen
    (0x200B, 0x200F),  # zero-width space/joiners, LTR and RTL marks
    (0x202A, 0x202E),  # bidirectional embedding and override
    (0x2060, 0x2064),  # word joiner and invisible operators
    (0xFEFF, 0xFEFF),  # byte-order mark used as a zero-width no-break space
)
_INVISIBLE = re.compile(
    "[" + "".join(rf"\u{lo:04x}-\u{hi:04x}" for lo, hi in _INVISIBLE_RANGES) + "]"
)
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

#: Below this, the two files are not two versions of one document and the positional diff is
#: noise. Measured: version pairs align at 0.87-0.98, an act against its contract at 0.08.
DEFAULT_MIN_SIMILARITY = 0.35

#: A replace block is re-paired by similarity; below this two segments are unrelated and become
#: a removal plus an addition rather than a rewrite.
_PAIR_FLOOR = 0.5
#: Re-pairing is quadratic. Past this many candidate pairs, fall back to positional pairing.
_PAIR_BUDGET = 2500


# --- wire models ------------------------------------------------------------------------------


class InlineOp(BaseModel):
    """One word-level operation inside a changed line, for the highlight."""

    model_config = ConfigDict(frozen=True)

    tag: Literal["equal", "insert", "delete", "replace"]
    left: str = ""
    right: str = ""


class ValueDelta(BaseModel):
    """One typed value that differs between the two documents."""

    model_config = ConfigDict(frozen=True)

    type: FactType
    kind: ChangeKind
    rule_id: str
    severity: Severity
    label_en: str
    label_ka: str
    left: str | None = None
    right: str | None = None
    #: absolute and relative change, for money and quantities
    delta: float | None = None
    delta_pct: float | None = None
    #: calendar days between two dates
    days: int | None = None
    note_en: str | None = None
    note_ka: str | None = None

    @property
    def is_critical(self) -> bool:
        """Severity, not type: a bare number in a spreadsheet is deliberately not an error."""
        return self.severity == "error"


class SegmentRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    text: str
    kind: str
    path: str
    heading: str | None = None


class Hunk(BaseModel):
    """One changed unit: a rewritten line, an added one, a removed one, or a move."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: ChangeKind
    materiality: Materiality
    rules: list[str] = Field(default_factory=list)
    left: SegmentRef | None = None
    right: SegmentRef | None = None
    inline: list[InlineOp] = Field(default_factory=list)
    deltas: list[ValueDelta] = Field(default_factory=list)
    moved_to: str | None = None
    moved_from: str | None = None
    #: set only by the optional LLM pass, and always shown as the model's words, not ours
    explanation_en: str | None = None
    explanation_ka: str | None = None
    explained_by: Literal["llm"] | None = None

    @property
    def needs_explaining(self) -> bool:
        """A substantive rewrite the rules could not name a value for — the LLM's whole scope.

        Deliberately narrow. A character substitution or an invisible-character change is also
        substantive and also carries no value, and the model has nothing to add about either.
        """
        return self.kind == "changed" and not self.deltas and self.rules == ["CMP.TEXT_CHANGED"]


class DiffRow(BaseModel):
    """One row of the rendered diff, changed or not. The spine the template walks."""

    model_config = ConfigDict(frozen=True)

    tag: Literal["equal", "insert", "delete", "replace"]
    left: SegmentRef | None = None
    right: SegmentRef | None = None
    hunk_id: str | None = None


class Counts(BaseModel):
    critical: int = 0
    substantive: int = 0
    cosmetic: int = 0
    added: int = 0
    removed: int = 0
    changed: int = 0
    moved: int = 0
    unchanged: int = 0

    @property
    def total_changes(self) -> int:
        return self.added + self.removed + self.changed + self.moved


class ComparisonReport(BaseModel):
    left_document_id: str
    right_document_id: str
    left_name: str = ""
    right_name: str = ""
    left_kind: str = ""
    right_kind: str = ""
    #: "versions" when the two files align as revisions of one document; "unrelated" otherwise,
    #: in which case the field differences are the answer and the line diff is not.
    alignment: Alignment = "versions"
    similarity: float = 0.0
    left_segments: int = 0
    right_segments: int = 0
    counts: Counts = Field(default_factory=Counts)
    rows: list[DiffRow] = Field(default_factory=list)
    hunks: list[Hunk] = Field(default_factory=list)
    #: document-wide, keyed by fact type rather than by position
    field_deltas: list[ValueDelta] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    skipped: list[SkipNote] = Field(default_factory=list)
    llm_model: str | None = None
    llm_explained: int = 0
    seconds: float = 0.0

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def identical(self) -> bool:
        return not self.hunks and not self.field_deltas

    def hunk(self, hunk_id: str) -> Hunk | None:
        return next((h for h in self.hunks if h.id == hunk_id), None)

    @property
    def unexplained(self) -> list[Hunk]:
        return [h for h in self.hunks if h.needs_explaining and h.explained_by is None]


# --- typed hits -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Hit:
    type: FactType
    value: str
    normalized: str | float | None
    unit: str | None
    currency: str | None
    source: str


def _bank_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalize_confusables(value).upper())


def _value_key(ft: FactType, value: str, normalized: str | float | None) -> str:
    if ft in _NUMERIC:
        n = parse_number(value)
        if n is not None:
            return f"n:{n:.4f}"
    if ft in _BANK:
        return f"b:{_bank_key(value)}"
    if normalized is not None:
        return f"z:{normalized}"
    return f"s:{normalize_name(value)}"


def hits_of(text: str) -> list[_Hit]:
    """Every typed value in ``text``, anchors first, deduplicated by type and value.

    Anchors before rules is the ordering ``merger.merge`` already uses: a value read from behind
    its own label is the best evidence available, so it wins the slot.

    The text is folded first, exactly as ``AoaChecker``'s ``DocumentView.build`` folds it. Both
    passes must agree about what the document says or typography starts inventing differences:
    a revision that replaced every hyphen with an en dash reported the contract number changing
    from "PR-2026/017" to "PR", because the number pattern stopped matching at the dash while
    the line itself was correctly called formatting-only.
    """
    text = normalize_confusables(text)
    out: list[_Hit] = []
    seen: set[tuple[FactType, str]] = set()
    for a in find_anchors(text):
        k = (a.type, _value_key(a.type, a.value, a.normalized))
        if k in seen:
            continue
        seen.add(k)
        out.append(_Hit(a.type, a.value, a.normalized, a.unit, None, "anchor"))
    for r in run_rules(text):
        k = (r.type, _value_key(r.type, r.value, r.normalized))
        if k in seen:
            continue
        seen.add(k)
        out.append(_Hit(r.type, r.value, r.normalized, r.unit, r.currency, "rules"))
    return out


#: The name this module has always used; the parser itself lives in ``checks.amounts`` so the
#: AOA check reads numbers exactly the same way.
as_number = number_of


def _same_value(a: _Hit, b: _Hit) -> bool:
    ft = a.type
    if ft in _NUMERIC:
        x, y = as_number(a.value, a.normalized), as_number(b.value, b.normalized)
        if x is not None and y is not None:
            return amounts_equal(x, y)
    if ft in _NAMES and names_match(a.value, b.value):
        return True
    if ft in _BANK:
        return _bank_key(a.value) == _bank_key(b.value)
    if a.normalized is not None and b.normalized is not None:
        return str(a.normalized) == str(b.normalized)
    return normalize_name(a.value) == normalize_name(b.value)


#: Past this many left-by-right comparisons, equal values are found by key rather than by
#: ``_same_value``'s tolerant comparison, and leftovers are paired in order.
_HIT_PAIR_BUDGET = 10_000
#: Below this similarity two leftover values are unrelated: a removal and an addition, not a
#: change of one into the other.
_HIT_PAIR_FLOOR = 0.5


def _similarity(a: _Hit, b: _Hit) -> float:
    """How alike two values of one type are, 0..1, for pairing what did not match exactly."""
    if a.type in _NUMERIC:
        x, y = as_number(a.value, a.normalized), as_number(b.value, b.normalized)
        if x is not None and y is not None:
            scale = max(abs(x), abs(y))
            return 1.0 if scale == 0 else max(0.0, 1.0 - abs(x - y) / scale)
    sa = str(a.normalized) if a.normalized is not None else normalize_name(a.value)
    sb = str(b.normalized) if b.normalized is not None else normalize_name(b.value)
    return difflib.SequenceMatcher(None, sa, sb, autojunk=False).ratio()


def _pair_hits(left: list[_Hit], right: list[_Hit]) -> list[tuple[_Hit | None, _Hit | None]]:
    """Pair values of one type: equal values first, then the leftovers by similarity.

    Matching equal values first is what keeps a paragraph holding two amounts from reporting
    both as changed when only one of them moved. Leftovers are paired by how alike they are,
    not by position: a paragraph that lost one date and shifted another by a day paired the
    survivor with the lost one and reported an unrelated date as a change. A single leftover on
    each side is still paired whatever it says - "director X → Y" is the useful reading.
    """
    pairs: list[tuple[_Hit | None, _Hit | None]] = []
    free = dict.fromkeys(range(len(right)))  # insertion-ordered set
    leftovers: list[_Hit] = []
    if len(left) * len(right) <= _HIT_PAIR_BUDGET:
        for hit in left:
            match = next((j for j in free if _same_value(hit, right[j])), None)
            if match is None:
                leftovers.append(hit)
            else:
                del free[match]
                pairs.append((hit, right[match]))
    else:
        by_key: dict[str, list[int]] = {}
        for j, r in enumerate(right):
            by_key.setdefault(_value_key(r.type, r.value, r.normalized), []).append(j)
        for hit in left:
            waiting = by_key.get(_value_key(hit.type, hit.value, hit.normalized))
            if waiting:
                j = waiting.pop(0)
                del free[j]
                pairs.append((hit, right[j]))
            else:
                leftovers.append(hit)

    rest = list(free)
    if len(leftovers) == 1 and len(rest) == 1:
        return [*pairs, (leftovers[0], right[rest[0]])]
    if len(leftovers) * len(rest) > _HIT_PAIR_BUDGET:
        # too many to score; fall back to document order rather than stall
        for hit in leftovers:
            pairs.append((hit, right[rest.pop(0)]) if rest else (hit, None))
        pairs.extend((None, right[j]) for j in rest)
        return pairs

    scored = sorted(
        ((-_similarity(a, right[j]), i, j) for i, a in enumerate(leftovers) for j in rest),
    )
    matched: dict[int, int] = {}
    taken: set[int] = set()
    for neg, i, j in scored:
        if -neg < _HIT_PAIR_FLOOR:
            break
        if i in matched or j in taken:
            continue
        matched[i] = j
        taken.add(j)
    for i, hit in enumerate(leftovers):
        pairs.append((hit, right[matched[i]]) if i in matched else (hit, None))
    pairs.extend((None, right[j]) for j in rest if j not in taken)
    return pairs


def _iso_to_date(value: object) -> date | None:
    m = _ISO_DATE.match(str(value))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:  # pragma: no cover - a well-formed but impossible date
        return None


def _bank_note(left: str, right: str) -> tuple[str, str] | None:
    """What kind of banking difference this is: a malformed code, or a different account.

    A failing IBAN checksum is information, never an error - the client's samples carry
    anonymised accounts whose check digits do not validate (docs/ACCURACY.md). Its use is
    exactly here: to tell a likely OCR slip from a genuinely different account once the two
    documents already disagree.
    """
    lo, ro = _bank_key(left), _bank_key(right)
    if lo[:2].isalpha() and lo[2:4].isdigit() and ro[:2].isalpha() and ro[2:4].isdigit():
        lv, rv = iban_is_valid(left), iban_is_valid(right)
        if lv and not rv:
            return (
                "the new account number fails its checksum",
                "ახალი ანგარიში ჩეკსუმას ვერ გადის",
            )
        if rv and not lv:
            return (
                "the old account number fails its checksum",
                "ძველი ანგარიში ჩეკსუმას ვერ გადის",
            )
        return None
    lw, rw = bic_is_well_formed(left), bic_is_well_formed(right)
    if lw and not rw:
        return (
            "the new bank code is not a valid BIC",
            "ახალი ბანკის კოდი არასწორი ფორმატისაა",
        )
    if rw and not lw:
        return (
            "the old bank code was not a valid BIC",
            "ძველი ბანკის კოდი არასწორი ფორმატისა იყო",
        )
    return None


def _delta(a: _Hit | None, b: _Hit | None) -> ValueDelta | None:
    """One difference between two typed values, or None when they say the same thing."""
    hit = a or b
    if hit is None:  # pragma: no cover - never called with two Nones
        return None
    ft = hit.type
    en, ka = LABELS.get(ft, (ft.value, ft.value))
    rule = _RULE_FOR_TYPE.get(ft, _DEFAULT_RULE)
    severity: Severity = "error" if ft in CRITICAL_TYPES else "warning"

    if a is not None and b is not None:
        if _same_value(a, b):
            return None
        note: tuple[str, str] | None = None
        delta = pct = None
        days = None
        if ft in _NUMERIC:
            x, y = as_number(a.value, a.normalized), as_number(b.value, b.normalized)
            if x is not None and y is not None:
                delta = round(y - x, 4)
                pct = round((y - x) / abs(x) * 100, 2) if x else None
        if ft in _DATES:
            d1, d2 = _iso_to_date(a.normalized), _iso_to_date(b.normalized)
            if d1 and d2:
                days = (d2 - d1).days
        if ft in _BANK:
            note = _bank_note(a.value, b.value)
        if ft in _NAMES and note is None:
            diff = classify_difference(b.value, a.value)
            if diff is not None and diff.looks_like_typo:
                note = ("looks like a misspelling", "ჰგავს მართლწერის შეცდომას")
        return ValueDelta(
            type=ft,
            kind="changed",
            rule_id=rule,
            severity=severity,
            label_en=en,
            label_ka=ka,
            left=a.value,
            right=b.value,
            delta=delta,
            delta_pct=pct,
            days=days,
            note_en=note[0] if note else None,
            note_ka=note[1] if note else None,
        )

    return ValueDelta(
        type=ft,
        kind="removed" if a is not None else "added",
        rule_id=rule,
        severity=severity,
        label_en=en,
        label_ka=ka,
        left=a.value if a is not None else None,
        right=b.value if b is not None else None,
    )


def deltas_between(
    left_text: str, right_text: str, *, types: frozenset[FactType] | None = None
) -> list[ValueDelta]:
    """Typed differences between two pieces of text, paired per fact type."""
    left_hits = hits_of(left_text) if left_text else []
    right_hits = hits_of(right_text) if right_text else []
    if types is not None:
        left_hits = [h for h in left_hits if h.type in types]
        right_hits = [h for h in right_hits if h.type in types]
    out: list[ValueDelta] = []
    present = {h.type for h in left_hits} | {h.type for h in right_hits}
    for ft in sorted(present, key=lambda t: t.value):
        ls = [h for h in left_hits if h.type == ft]
        rs = [h for h in right_hits if h.type == ft]
        for a, b in _pair_hits(ls, rs):
            d = _delta(a, b)
            if d is not None:
                out.append(d)
    return out


# --- text-shape classification ------------------------------------------------------------------


def _strip_invisible(s: str) -> str:
    return _INVISIBLE.sub("", s)


def cosmetic_kind(left: str, right: str) -> str | None:
    """``None`` when the two strings differ in substance.

    Otherwise which kind of invisible-to-the-eye difference it is, most alarming first: control
    characters, then homoglyphs, then ordinary whitespace and typography.
    """
    if left == right:
        return "identical"
    if _strip_invisible(left) != _strip_invisible(right):
        pass
    elif _INVISIBLE.search(left) or _INVISIBLE.search(right):
        return "invisible"
    folded_l = strip_quotes(normalize_confusables(left))
    folded_r = strip_quotes(normalize_confusables(right))
    if folded_l != folded_r:
        return None
    if confusable_chars(left) != confusable_chars(right):
        return "homoglyph"
    return "formatting"


#: Rules about characters rather than about values. Kept apart from the rest because they are
#: the only ones that must be reported *in addition to* whatever else changed in the same line.
HIDDEN_CHARACTER_RULES = ("CMP.HOMOGLYPH", "CMP.INVISIBLE_CHARS")


def hidden_character_rules(left: str, right: str) -> list[str]:
    """Character substitutions between two strings, whatever else may also differ.

    ``cosmetic_kind`` only speaks when a change is *nothing but* invisible, and ``hits_of``
    folds confusables away before it compares values. Between them, a Cyrillic letter swapped
    into a bank code in the same paragraph as a plausible wording change was reported as
    neither a banking difference nor a substitution — only as "text changed". That is exactly
    the case where the swap would be deliberate, so it is checked on its own here.
    """
    out: list[str] = []
    if confusable_chars(left) != confusable_chars(right):
        out.append("CMP.HOMOGLYPH")
    # counted, not collected: a second zero-width space where there was one is still a change
    if Counter(_INVISIBLE.findall(left)) != Counter(_INVISIBLE.findall(right)):
        out.append("CMP.INVISIBLE_CHARS")
    return out


def inline_ops(left: str, right: str) -> list[InlineOp]:
    """Word-level operations, the ``git --word-diff`` view of one changed line."""
    lt = _TOKEN.findall(left)
    rt = _TOKEN.findall(right)
    sm = difflib.SequenceMatcher(None, lt, rt, autojunk=False)
    out: list[InlineOp] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        out.append(
            InlineOp(
                tag=tag,
                left="".join(lt[i1:i2]),
                right="".join(rt[j1:j2]),
            )
        )
    return out


def _numeric_reformat(left: str, right: str) -> bool:
    """True when the only difference is how the same numbers are written.

    ``1 000,00`` and ``1000.00`` are the same amount. Without this they align as a rewritten
    line that no rule can name a value for, and end up queued for the model to explain.
    """
    ln = [parse_number(m.group()) for m in NUM_IN_TEXT.finditer(left)]
    rn = [parse_number(m.group()) for m in NUM_IN_TEXT.finditer(right)]
    if not ln or len(ln) != len(rn) or any(v is None for v in ln + rn):
        return False
    if not all(amounts_equal(a, b) for a, b in zip(ln, rn, strict=True)):
        return False
    bare_left = normalize_name(NUM_IN_TEXT.sub(" ", left))
    bare_right = normalize_name(NUM_IN_TEXT.sub(" ", right))
    return bare_left == bare_right


_WORD_AMOUNT_LABEL = ("Amount in words", "თანხა სიტყვიერად")


def word_amount_deltas(left: str, right: str) -> list[ValueDelta]:
    """Differences between the amounts the two texts spell out in words."""
    lw, rw = word_amounts(left), word_amounts(right)
    if [v for v, _ in lw] == [v for v, _ in rw]:
        return []
    en, ka = _WORD_AMOUNT_LABEL
    out: list[ValueDelta] = []
    for i in range(max(len(lw), len(rw))):
        a = lw[i] if i < len(lw) else None
        b = rw[i] if i < len(rw) else None
        if a is not None and b is not None:
            if a[0] == b[0]:
                continue
            out.append(
                ValueDelta(
                    type=FactType.total_amount,
                    kind="changed",
                    rule_id="CMP.AMOUNT_WORDS",
                    severity="error",
                    label_en=en,
                    label_ka=ka,
                    left=f"{a[1]} ({a[0]})",
                    right=f"{b[1]} ({b[0]})",
                    delta=float(b[0] - a[0]),
                    delta_pct=round((b[0] - a[0]) / abs(a[0]) * 100, 2) if a[0] else None,
                )
            )
        elif a is not None or b is not None:
            out.append(
                ValueDelta(
                    type=FactType.total_amount,
                    kind="removed" if a is not None else "added",
                    rule_id="CMP.AMOUNT_WORDS",
                    severity="error",
                    label_en=en,
                    label_ka=ka,
                    left=f"{a[1]} ({a[0]})" if a is not None else None,
                    right=f"{b[1]} ({b[0]})" if b is not None else None,
                )
            )
    return out


_BARE_NUMBER_LABEL = ("Number", "რიცხვი")


def bare_number_delta(
    left: str,
    right: str,
    *,
    critical: bool,
    left_number: float | None = None,
    right_number: float | None = None,
) -> list[ValueDelta]:
    """A segment that is nothing but a number, against another that is nothing but a number.

    ``rules.find_amounts`` needs a currency beside the figure, and a table or spreadsheet keeps
    the currency in a different cell - so the one place a price is most likely to change is the
    one place the amount rules cannot see it. On the project's own Word contract this is the
    difference between "text changed" and "the total went up by 3 350".

    ``critical`` is false for spreadsheets. Two different bids disagree in hundreds of numeric
    cells and that is what a bid *is*; calling every one of them critical would empty the word
    of meaning. In prose a figure that changed is a fact that changed.

    ``left_number``/``right_number`` are a spreadsheet cell's own values. They win over reading
    the text back, which misreads "12.345" as twelve thousand (see ``_number_of``).
    """
    a = left_number if left_number is not None else parse_number(left.strip())
    b = right_number if right_number is not None else parse_number(right.strip())
    exact = left_number is not None and right_number is not None
    if a is None or b is None or _numbers_equal(a, b, exact=exact):
        return []
    en, ka = _BARE_NUMBER_LABEL
    return [
        ValueDelta(
            type=FactType.amount,
            kind="changed",
            rule_id="CMP.AMOUNT",
            severity="error" if critical else "warning",
            label_en=en,
            label_ka=ka,
            left=left.strip(),
            right=right.strip(),
            delta=round(b - a, 4),
            delta_pct=round((b - a) / abs(a) * 100, 2) if a else None,
        )
    ]


def dedup_key(delta: ValueDelta) -> tuple[str, str | None, str | None]:
    """Identity of a difference, so the same change found twice is reported once.

    The same money can arrive as "48 750,00" from the line it sits on and as "48,750.00 GEL"
    from the document-wide pass. Comparing the strings would list it twice; comparing the
    numbers only works for the numeric types, which is why the text types keep their own rule.
    """

    def side(value: str | None) -> str | None:
        if value is None:
            return None
        if delta.type in _NUMERIC:
            number = as_number(value)
            return f"n:{number:.4f}" if number is not None else normalize_name(value)
        return normalize_name(value)

    return (delta.rule_id, side(delta.left), side(delta.right))


# --- alignment ----------------------------------------------------------------------------------

#: (left, right, group). ``group`` ties consecutive pairs into one hunk, which is how a whole
#: added worksheet stays one finding instead of one per cell.
_Pair = tuple[Segment | None, Segment | None, str | None]


class DocumentTooLargeError(RuntimeError):
    """Raised when a document has more segments than the comparison is configured to align."""


_Opcode = tuple[str, int, int, int, int]

#: ``SequenceMatcher``'s inner loop visits, for every element of one side, every position of
#: the same key on the other: the sum over keys of count_a x count_b. Unique lines make that
#: linear; a sheet of numeric rows - one repeated key - makes it quadratic. Past this many
#: visits the middle is split on unique anchors first.
_SM_BUDGET = 5_000_000


def _opcodes(a: Sequence[str], b: Sequence[str]) -> list[_Opcode]:
    """``SequenceMatcher.get_opcodes`` for two key lists, without its quadratic worst case.

    The common prefix and suffix are matched first (what ``git diff`` does), and a middle too
    large to hand to ``SequenceMatcher`` is cut at keys that occur exactly once on each side -
    patience diff's anchors - and each gap aligned on its own. Measured on 10 000 numeric rows
    against a copy with one value changed: 7.5 s before, well under a second after. On inputs
    within the budget and with no common prefix or suffix the answer is ``SequenceMatcher``'s.
    """
    ops: list[_Opcode] = []
    _opcodes_into(ops, a, b, 0, len(a), 0, len(b), depth=0)
    return _merge_ops(ops)


def _opcodes_into(
    ops: list[_Opcode],
    a: Sequence[str],
    b: Sequence[str],
    alo: int,
    ahi: int,
    blo: int,
    bhi: int,
    *,
    depth: int,
) -> None:
    pre = 0
    while alo + pre < ahi and blo + pre < bhi and a[alo + pre] == b[blo + pre]:
        pre += 1
    suf = 0
    while ahi - suf > alo + pre and bhi - suf > blo + pre and a[ahi - suf - 1] == b[bhi - suf - 1]:
        suf += 1
    if pre:
        ops.append(("equal", alo, alo + pre, blo, blo + pre))
    ma, mb = alo + pre, blo + pre
    na, nb = ahi - suf, bhi - suf
    if ma < na or mb < nb:
        _middle(ops, a, b, ma, na, mb, nb, depth=depth)
    if suf:
        ops.append(("equal", na, ahi, nb, bhi))


def _middle(
    ops: list[_Opcode],
    a: Sequence[str],
    b: Sequence[str],
    alo: int,
    ahi: int,
    blo: int,
    bhi: int,
    *,
    depth: int,
) -> None:
    if alo == ahi:
        ops.append(("insert", alo, alo, blo, bhi))
        return
    if blo == bhi:
        ops.append(("delete", alo, ahi, blo, blo))
        return
    count_b = Counter(b[blo:bhi])
    cost = sum(n * count_b.get(k, 0) for k, n in Counter(a[alo:ahi]).items())
    if cost <= _SM_BUDGET:
        sm = difflib.SequenceMatcher(None, a[alo:ahi], b[blo:bhi], autojunk=False)
        ops.extend(
            (tag, i1 + alo, i2 + alo, j1 + blo, j2 + blo)
            for tag, i1, i2, j1, j2 in sm.get_opcodes()
        )
        return
    anchors = _unique_anchors(a, b, alo, ahi, blo, bhi) if depth < 8 else []
    if not anchors:
        # Nothing distinctive to align on, and too large to search: a wholesale replacement.
        ops.append(("replace", alo, ahi, blo, bhi))
        return
    i, j = alo, blo
    for ai, bj in anchors:
        _opcodes_into(ops, a, b, i, ai, j, bj, depth=depth + 1)
        ops.append(("equal", ai, ai + 1, bj, bj + 1))
        i, j = ai + 1, bj + 1
    _opcodes_into(ops, a, b, i, ahi, j, bhi, depth=depth + 1)


def _unique_anchors(
    a: Sequence[str], b: Sequence[str], alo: int, ahi: int, blo: int, bhi: int
) -> list[tuple[int, int]]:
    """Keys occurring once in each range, as (i, j), longest increasing run by ``j``."""
    count_a = Counter(a[alo:ahi])
    count_b = Counter(b[blo:bhi])
    where_b = {b[j]: j for j in range(blo, bhi) if count_b[b[j]] == 1}
    pairs = [(i, where_b[a[i]]) for i in range(alo, ahi) if count_a[a[i]] == 1 and a[i] in where_b]
    return _longest_increasing(pairs)


def _longest_increasing(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Longest subsequence of ``pairs`` (sorted by i) whose j also increases. O(n log n)."""
    tails: list[int] = []  # j of the smallest tail of an increasing run of each length
    tail_at: list[int] = []  # index into pairs of that tail
    prev: list[int] = [-1] * len(pairs)
    for k, (_i, j) in enumerate(pairs):
        pos = bisect.bisect_left(tails, j)
        if pos == len(tails):
            tails.append(j)
            tail_at.append(k)
        else:
            tails[pos] = j
            tail_at[pos] = k
        prev[k] = tail_at[pos - 1] if pos else -1
    out: list[tuple[int, int]] = []
    k = tail_at[-1] if tail_at else -1
    while k >= 0:
        out.append(pairs[k])
        k = prev[k]
    return out[::-1]


def _merge_ops(ops: list[_Opcode]) -> list[_Opcode]:
    """Join adjacent opcodes of the same tag, as ``SequenceMatcher`` itself reports them."""
    out: list[_Opcode] = []
    for op in ops:
        if op[1] == op[2] and op[3] == op[4]:
            continue
        if out and out[-1][0] == op[0] and out[-1][2] == op[1] and out[-1][4] == op[3]:
            last = out[-1]
            out[-1] = (last[0], last[1], op[2], last[3], op[4])
        else:
            out.append(op)
    return out


def _ratio(ops: list[_Opcode], total: int) -> float:
    """``SequenceMatcher.ratio`` from opcodes: twice the matched elements over all elements."""
    same = sum(i2 - i1 for tag, i1, i2, _j1, _j2 in ops if tag == "equal")
    return 2.0 * same / total if total else 1.0


def _pair_replace(left: Sequence[Segment], right: Sequence[Segment]) -> list[_Pair]:
    """Re-pair a replace block so a 3-for-2 swap is not rendered as one undifferentiated blob."""
    if len(left) == 1 and len(right) == 1:
        return [(left[0], right[0], None)]
    if len(left) * len(right) > _PAIR_BUDGET:
        # Too big to score pairwise, and a block this large is a wholesale replacement rather
        # than a set of line edits. Say that, instead of inventing line-to-line correspondences.
        return [(a, None, None) for a in left] + [(None, b, None) for b in right]

    scored: list[tuple[float, int, int]] = []
    for i, a in enumerate(left):
        ka = key_of(a.text)
        for j, b in enumerate(right):
            m = difflib.SequenceMatcher(None, ka, key_of(b.text), autojunk=False)
            if m.real_quick_ratio() < _PAIR_FLOOR or m.quick_ratio() < _PAIR_FLOOR:
                continue
            ratio = m.ratio()
            if ratio >= _PAIR_FLOOR:
                scored.append((ratio, i, j))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))

    matched: dict[int, int] = {}
    taken: set[int] = set()
    for _ratio, i, j in scored:
        if i in matched or j in taken:
            continue
        matched[i] = j
        taken.add(j)

    out: list[_Pair] = [
        (a, right[matched[i]] if i in matched else None, None) for i, a in enumerate(left)
    ]
    out.extend((None, b, None) for j, b in enumerate(right) if j not in taken)
    return out


def _align_linear(left: list[Segment], right: list[Segment]) -> tuple[list[_Pair], float]:
    lk = [key_of(s.text) for s in left]
    rk = [key_of(s.text) for s in right]
    ops = _opcodes(lk, rk)
    pairs: list[_Pair] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            pairs.extend((a, b, None) for a, b in zip(left[i1:i2], right[j1:j2], strict=True))
        elif tag == "delete":
            pairs.extend((a, None, None) for a in left[i1:i2])
        elif tag == "insert":
            pairs.extend((None, b, None) for b in right[j1:j2])
        else:
            pairs.extend(_pair_replace(left[i1:i2], right[j1:j2]))
    return pairs, _ratio(ops, len(lk) + len(rk))


def _by_sheet(segments: list[Segment]) -> dict[str, list[Segment]]:
    out: dict[str, list[Segment]] = {}
    for s in segments:
        out.setdefault(s.sheet or "", []).append(s)
    return out


def _numbers_equal(x: float, y: float, *, exact: bool) -> bool:
    """Two numbers the same? A cent of slack for figures read out of text, none for cells.

    ``amounts_equal``'s cent absorbs "1 000,00" against "1000" in prose. A spreadsheet cell's
    value is exact, and a quantity going from 12.345 to 12.35 is a change a cent would hide;
    only binary floating-point noise (0.1 + 0.2) is forgiven there.
    """
    if exact:
        return math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9)
    return amounts_equal(x, y)


def _number_of(segment: Segment) -> float | None:
    """A segment's number: the cell's own value when the reader kept one, else its text parsed.

    ``parse_number`` reads a three-digit group after a dot as thousands - right for the
    Georgian prose it was written for, wrong for a spreadsheet cell holding 12.345, which it
    would compare as 12 345. Only a text document's segment falls back to parsing.
    """
    if segment.number is not None:
        return segment.number
    return parse_number(segment.text)


def _row_key(cells: list[Segment]) -> str:
    """Identity of a spreadsheet row: its text cells, ignoring every number in it.

    The same reasoning as ``BoqLine.key`` - a bidder filling in a blank rate is a changed cell,
    not a deleted row plus an unrelated new one. A row of nothing but numbers falls back to its
    column signature, so rows of the same shape still align in order.
    """
    words = [c.text for c in cells if _number_of(c) is None]
    if words:
        return normalize_name(" ".join(words))[:80]
    return "#" + ",".join(str(c.col) for c in cells)


def _cells_equal(a: Segment, b: Segment) -> bool:
    x, y = _number_of(a), _number_of(b)
    if x is not None and y is not None:
        return _numbers_equal(x, y, exact=a.number is not None and b.number is not None) and (
            a.formula == b.formula
        )
    return normalize_spaces(a.text) == normalize_spaces(b.text) and a.formula == b.formula


def _align_row_pair(left: list[Segment], right: list[Segment]) -> list[_Pair]:
    lby: dict[int | None, Segment] = {c.col: c for c in left}
    rby: dict[int | None, Segment] = {c.col: c for c in right}
    out: list[_Pair] = []
    for col in sorted(set(lby) | set(rby), key=lambda c: (c is None, c)):
        a, b = lby.get(col), rby.get(col)
        out.append((a, b, None))
    return out


def _align_sheet_pair(left: list[Segment], right: list[Segment]) -> list[_Pair]:
    lrows: dict[int, list[Segment]] = {}
    rrows: dict[int, list[Segment]] = {}
    for cell in left:
        lrows.setdefault(cell.row or 0, []).append(cell)
    for cell in right:
        rrows.setdefault(cell.row or 0, []).append(cell)
    ln, rn = sorted(lrows), sorted(rrows)
    lk = [_row_key(lrows[r]) for r in ln]
    rk = [_row_key(rrows[r]) for r in rn]
    out: list[_Pair] = []
    for tag, i1, i2, j1, j2 in _opcodes(lk, rk):
        if tag == "equal":
            for a, b in zip(ln[i1:i2], rn[j1:j2], strict=True):
                out.extend(_align_row_pair(lrows[a], rrows[b]))
        elif tag == "delete":
            out.extend((c, None, None) for r in ln[i1:i2] for c in lrows[r])
        elif tag == "insert":
            out.extend((None, c, None) for r in rn[j1:j2] for c in rrows[r])
        else:
            la, rb = ln[i1:i2], rn[j1:j2]
            for k in range(max(len(la), len(rb))):
                if k < len(la) and k < len(rb):
                    out.extend(_align_row_pair(lrows[la[k]], rrows[rb[k]]))
                elif k < len(la):
                    out.extend((c, None, None) for c in lrows[la[k]])
                else:
                    out.extend((None, c, None) for c in rrows[rb[k]])
    return out


def _align_sheets(left: list[Segment], right: list[Segment]) -> tuple[list[_Pair], float]:
    lby, rby = _by_sheet(left), _by_sheet(right)
    ln, rn = list(lby), list(rby)
    pairs: list[_Pair] = []
    for tag, i1, i2, j1, j2 in _opcodes(ln, rn):
        if tag == "equal":
            for a, b in zip(ln[i1:i2], rn[j1:j2], strict=True):
                pairs.extend(_align_sheet_pair(lby[a], rby[b]))
        elif tag == "delete":
            for name in ln[i1:i2]:
                pairs.extend((c, None, f"sheet:{name}") for c in lby[name])
        elif tag == "insert":
            for name in rn[j1:j2]:
                pairs.extend((None, c, f"sheet:{name}") for c in rby[name])
        else:
            la, rb = ln[i1:i2], rn[j1:j2]
            for k in range(max(len(la), len(rb))):
                if k < len(la) and k < len(rb):
                    pairs.extend(_align_sheet_pair(lby[la[k]], rby[rb[k]]))
                elif k < len(la):
                    pairs.extend((c, None, f"sheet:{la[k]}") for c in lby[la[k]])
                else:
                    pairs.extend((None, c, f"sheet:{rb[k]}") for c in rby[rb[k]])
    same = sum(
        1
        for a, b, _ in pairs
        if a is not None and b is not None and key_of(a.text) == key_of(b.text)
    )
    total = len(left) + len(right)
    return pairs, (2.0 * same / total if total else 1.0)


#: Text short enough, or numeric, is not distinctive enough to call a move. A workbook is
#: full of cells reading "0.00"; pairing a deleted one with an added one somewhere else would
#: invent hundreds of moves out of arithmetic.
_MOVE_MIN_CHARS = 12


def _movable(segment: Segment) -> bool:
    flat = key_of(segment.text)
    return len(flat) >= _MOVE_MIN_CHARS and _number_of(segment) is None


def _detect_moves(pairs: list[_Pair]) -> dict[int, int]:
    """Removals and additions of identical text, matched up as moves.

    Exact key equality only. A looser rule invents moves out of boilerplate that legitimately
    appears twice, which is worse than reporting a genuine move as a delete plus an insert.

    Cells of a wholly added or removed worksheet (a grouped pair) are never part of a move: the
    sheet is already reported as one change, and pairing one of its cells with an insertion
    elsewhere counted the same text twice - once as the sheet, once as a move.
    """
    inserts: dict[str, list[int]] = {}
    for i, (a, b, g) in enumerate(pairs):
        if g is None and a is None and b is not None and _movable(b):
            inserts.setdefault(key_of(b.text), []).append(i)
    moves: dict[int, int] = {}
    for i, (a, b, g) in enumerate(pairs):
        if g is not None or a is None or b is not None or not _movable(a):
            continue
        waiting = inserts.get(key_of(a.text))
        if waiting:
            moves[i] = waiting.pop(0)
    return moves


# --- classification -------------------------------------------------------------------------


def _ref(segment: Segment | None) -> SegmentRef | None:
    if segment is None:
        return None
    return SegmentRef(
        index=segment.index,
        text=segment.text,
        kind=segment.kind,
        path=segment.path,
        heading=segment.heading,
    )


def _pair_equal(a: Segment, b: Segment) -> bool:
    if a.kind == "sheet_cell" and b.kind == "sheet_cell":
        return _cells_equal(a, b)
    return key_of(a.text) == key_of(b.text) and a.formula == b.formula


def _rules_of(deltas: list[ValueDelta], *, base: str) -> list[str]:
    return [base, *sorted({d.rule_id for d in deltas})] if deltas else [base]


def _classify(
    a: Segment | None, b: Segment | None, *, ocr: bool
) -> tuple[ChangeKind, Materiality, list[ValueDelta], list[str], list[InlineOp]]:
    if a is None:
        if b is None:
            raise ValueError("a diff pair has neither side")
        deltas = deltas_between("", b.text) + word_amount_deltas("", b.text)
        mat: Materiality = "critical" if any(d.is_critical for d in deltas) else "substantive"
        return "added", mat, deltas, _rules_of(deltas, base="CMP.TEXT_ADDED"), []
    if b is None:
        deltas = deltas_between(a.text, "") + word_amount_deltas(a.text, "")
        mat = "critical" if any(d.is_critical for d in deltas) else "substantive"
        return "removed", mat, deltas, _rules_of(deltas, base="CMP.TEXT_REMOVED"), []

    if a.formula != b.formula and normalize_spaces(a.text) == normalize_spaces(b.text):
        return (
            "changed",
            "substantive",
            [],
            ["CMP.FORMULA"],
            inline_ops(a.formula or "", b.formula or ""),
        )

    inline = inline_ops(a.text, b.text)
    shape: str | None = cosmetic_kind(a.text, b.text)
    if shape == "invisible":
        return "changed", "substantive", [], ["CMP.INVISIBLE_CHARS"], inline
    if shape == "homoglyph":
        # On OCR'd input this is Tesseract, not tampering: normalize.py documents it emitting
        # Cyrillic lookalikes inside Latin IBANs and SWIFT codes. Still reported, not as alarm.
        return "changed", ("cosmetic" if ocr else "substantive"), [], ["CMP.HOMOGLYPH"], inline
    if shape in ("formatting", "identical"):
        return "changed", "cosmetic", [], ["CMP.FORMATTING"], inline

    hidden = hidden_character_rules(a.text, b.text)
    deltas = (
        deltas_between(a.text, b.text)
        + word_amount_deltas(a.text, b.text)
        + bare_number_delta(
            a.text,
            b.text,
            critical=a.kind != "sheet_cell",
            left_number=a.number,
            right_number=b.number,
        )
    )
    if deltas:
        mat = "critical" if any(d.is_critical for d in deltas) else "substantive"
        return "changed", mat, deltas, sorted({d.rule_id for d in deltas} | set(hidden)), inline
    if _numeric_reformat(a.text, b.text) and not hidden:
        return "changed", "cosmetic", [], ["CMP.FORMATTING"], inline
    return "changed", "substantive", [], sorted({*hidden, "CMP.TEXT_CHANGED"}), inline


def _build(
    pairs: list[_Pair], moves: dict[int, int], *, ocr: bool
) -> tuple[list[DiffRow], list[Hunk], Counts]:
    partner: dict[int, int] = {}
    for i, j in moves.items():
        partner[i] = j
        partner[j] = i

    rows: list[DiffRow] = []
    hunks: list[Hunk] = []
    counts = Counts()
    hunk_of: dict[int, str] = {}
    made = 0
    i = 0
    while i < len(pairs):
        a, b, group = pairs[i]

        if group is not None:
            end = i
            while end < len(pairs) and pairs[end][2] == group:
                end += 1
            run = pairs[i:end]
            added = run[0][0] is None
            sample = run[0][1] if added else run[0][0]
            if sample is None:  # a grouped run is all additions or all removals
                raise RuntimeError(f"worksheet run {group!r} has an empty side")
            name = sample.sheet or group.split(":", 1)[-1]
            made += 1
            hid = f"h{made}"
            ref = SegmentRef(
                index=sample.index,
                text=f"{name} ({len(run)})",
                kind=sample.kind,
                path=name,
                heading=name,
            )
            hunks.append(
                Hunk(
                    id=hid,
                    kind="added" if added else "removed",
                    materiality="substantive",
                    rules=["CMP.SHEET_ADDED" if added else "CMP.SHEET_REMOVED"],
                    left=None if added else ref,
                    right=ref if added else None,
                )
            )
            counts.substantive += 1
            if added:
                counts.added += 1
            else:
                counts.removed += 1
            for ra, rb, _g in run:
                rows.append(
                    DiffRow(
                        tag="insert" if added else "delete",
                        left=_ref(ra),
                        right=_ref(rb),
                        hunk_id=hid,
                    )
                )
            i = end
            continue

        if a is not None and b is not None and _pair_equal(a, b):
            rows.append(DiffRow(tag="equal", left=_ref(a), right=_ref(b)))
            counts.unchanged += 1
            i += 1
            continue

        if i in partner:
            existing = hunk_of.get(i)
            if existing is None:
                other = partner[i]
                left_i, right_i = (i, other) if a is not None else (other, i)
                moved_from = pairs[left_i][0]
                moved_to = pairs[right_i][1]
                if moved_from is None or moved_to is None:  # _detect_moves pairs a - with a +
                    raise RuntimeError("a move is missing its removed or its added side")
                made += 1
                hid = f"h{made}"
                hunks.append(
                    Hunk(
                        id=hid,
                        kind="moved",
                        materiality="cosmetic",
                        rules=["CMP.MOVED"],
                        left=_ref(moved_from),
                        right=_ref(moved_to),
                        moved_from=moved_from.path,
                        moved_to=moved_to.path,
                    )
                )
                counts.moved += 1
                hunk_of[i] = hid
                hunk_of[other] = hid
                existing = hid
            rows.append(
                DiffRow(
                    tag="delete" if a is not None else "insert",
                    left=_ref(a),
                    right=_ref(b),
                    hunk_id=existing,
                )
            )
            i += 1
            continue

        kind, materiality, deltas, rules, inline = _classify(a, b, ocr=ocr)
        made += 1
        hid = f"h{made}"
        hunks.append(
            Hunk(
                id=hid,
                kind=kind,
                materiality=materiality,
                rules=rules,
                left=_ref(a),
                right=_ref(b),
                inline=inline,
                deltas=deltas,
            )
        )
        if a is not None and b is not None:
            tag: Literal["replace", "delete", "insert"] = "replace"
        else:
            tag = "delete" if a is not None else "insert"
        rows.append(DiffRow(tag=tag, left=_ref(a), right=_ref(b), hunk_id=hid))
        setattr(counts, materiality, getattr(counts, materiality) + 1)
        setattr(counts, kind, getattr(counts, kind) + 1)
        i += 1
    return rows, hunks, counts


# --- findings -------------------------------------------------------------------------------


def _short(text: str | None, limit: int = 160) -> str | None:
    if text is None:
        return None
    flat = normalize_spaces(text)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _delta_message(d: ValueDelta) -> tuple[str, str]:
    if d.kind == "added":
        return (f"{d.label_ka} დაემატა: {d.right}", f"{d.label_en} added: {d.right}")
    if d.kind == "removed":
        return (f"{d.label_ka} ამოღებულია: {d.left}", f"{d.label_en} removed: {d.left}")
    tail_ka: list[str] = []
    tail_en: list[str] = []
    if d.delta is not None:
        sign = "+" if d.delta > 0 else ""
        pct = f", {sign}{d.delta_pct:.1f}%" if d.delta_pct is not None else ""
        tail_ka.append(f"სხვაობა {sign}{d.delta:g}{pct}")
        tail_en.append(f"difference {sign}{d.delta:g}{pct}")
    if d.days is not None:
        tail_ka.append(f"{d.days:+d} დღე")
        tail_en.append(f"{d.days:+d} days")
    if d.note_ka and d.note_en:
        tail_ka.append(d.note_ka)
        tail_en.append(d.note_en)
    suffix_ka = f" ({'; '.join(tail_ka)})" if tail_ka else ""
    suffix_en = f" ({'; '.join(tail_en)})" if tail_en else ""
    return (
        f"{d.label_ka}: {d.left} → {d.right}{suffix_ka}",
        f"{d.label_en}: {d.left} → {d.right}{suffix_en}",
    )


#: A spreadsheet pair can differ in a thousand cells. Every **critical** difference is always
#: listed - those are the answer and truncating them would be a lie - but the warnings and notes
#: behind them are capped, and the diff carries the rest. Without this, two unrelated bids
#: produce hundreds of rows and the twenty-one that matter are invisible.
_MAX_MINOR_FINDINGS = 60

_TEXT_MESSAGES: dict[str, tuple[str, str]] = {
    "CMP.TEXT_ADDED": ("დაემატა ტექსტი", "text added"),
    "CMP.TEXT_REMOVED": ("ტექსტი ამოღებულია", "text removed"),
    "CMP.TEXT_CHANGED": ("ტექსტი შეიცვალა", "text changed"),
    "CMP.SHEET_ADDED": ("დაემატა ფურცელი", "worksheet added"),
    "CMP.SHEET_REMOVED": ("ფურცელი ამოღებულია", "worksheet removed"),
    "CMP.INVISIBLE_CHARS": (
        "უხილავი სიმბოლოები (zero-width / bidi)",
        "invisible characters (zero-width / bidi controls)",
    ),
}


def _findings_of(
    hunks: list[Hunk], field_deltas: list[ValueDelta], right_text: str, *, ocr: bool
) -> tuple[list[Finding], int]:
    """One finding per difference worth naming.

    Formatting-only hunks are counted and drawn, never listed: a document with a few hundred
    whitespace differences would otherwise bury the four that matter. Everything else — every
    typed value, every added, removed, moved or rewritten line, every character substitution —
    gets a row with a stable rule id.
    """
    out: list[Finding] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    minor = 0
    suppressed = 0

    def add(
        rule_id: str,
        severity: Severity,
        ka: str,
        en: str,
        *,
        expected: str | None = None,
        actual: str | None = None,
        key: tuple[str, str | None, str | None] | None = None,
        full: tuple[str | None, str | None] | None = None,
    ) -> None:
        """``full`` is the untruncated (old, new) text behind a shortened expected/actual.

        Identity and anchoring use the full text; only the display fields are shortened. Two
        long lines that share their first 160 characters are two changes, and a comment can
        only be anchored on words the document actually contains - not on "…".
        """
        nonlocal minor, suppressed
        old, new = full if full is not None else (expected, actual)
        identity = key or (rule_id, old, new)
        if identity in seen:
            return
        if severity != "error":
            minor += 1
            if minor > _MAX_MINOR_FINDINGS:
                suppressed += 1
                return
        seen.add(identity)
        quote = (new or old) if full is not None else None
        span: tuple[int, int] | None = None
        if new:
            at = right_text.find(new)
            if at >= 0:
                span = (at, at + len(new))
        out.append(
            Finding(
                rule_id=rule_id,
                severity=severity,
                message_ka=ka,
                message_en=en,
                expected=expected,
                actual=actual,
                span=span,
                quote=quote,
            )
        )

    def texts(h: Hunk) -> tuple[str | None, str | None]:
        return (h.left.text if h.left else None, h.right.text if h.right else None)

    for h in hunks:
        # Character substitutions are reported *as well as* whatever else changed on the line,
        # never instead of it — see hidden_character_rules.
        if "CMP.HOMOGLYPH" in h.rules:
            chars = confusable_chars(h.left.text if h.left else "") or confusable_chars(
                h.right.text if h.right else ""
            )
            add(
                "CMP.HOMOGLYPH",
                "info" if ocr else "warning",
                f"ვიზუალურად იდენტური, მაგრამ სხვა სიმბოლოები: {chars}"
                + (" (სკანირებული დოკუმენტი — სავარაუდოდ OCR)" if ocr else ""),
                f"characters that look identical but differ: {chars}"
                + (" (scanned input — most likely OCR)" if ocr else ""),
                expected=_short(h.left.text if h.left else None),
                actual=_short(h.right.text if h.right else None),
                full=texts(h),
            )
        if "CMP.INVISIBLE_CHARS" in h.rules:
            ka, en = _TEXT_MESSAGES["CMP.INVISIBLE_CHARS"]
            add(
                "CMP.INVISIBLE_CHARS",
                "warning",
                ka,
                en,
                expected=_short(h.left.text if h.left else None),
                actual=_short(h.right.text if h.right else None),
                full=texts(h),
            )
        if not [r for r in h.rules if r not in HIDDEN_CHARACTER_RULES]:
            continue

        if h.deltas:
            for d in h.deltas:
                ka, en = _delta_message(d)
                add(
                    d.rule_id,
                    d.severity,
                    ka,
                    en,
                    expected=d.left,
                    actual=d.right,
                    key=dedup_key(d),
                )
            continue
        if h.kind == "moved":
            add(
                "CMP.MOVED",
                "info",
                f"გადატანილია: {h.moved_from} → {h.moved_to}",
                f"moved: {h.moved_from} → {h.moved_to}",
                expected=h.moved_from,
                actual=h.moved_to,
            )
            continue
        if "CMP.FORMATTING" in h.rules:
            continue
        if "CMP.FORMULA" in h.rules:
            # The formulas themselves, not the cell's path. The hunk's word diff was taken over
            # the two formulas, so each side's pieces put back together are that formula.
            old_formula = "".join(op.left for op in h.inline) or None
            new_formula = "".join(op.right for op in h.inline) or None
            cell = h.right or h.left
            where = cell.path if cell is not None else ""
            add(
                "CMP.FORMULA",
                "warning",
                f"ფორმულა შეიცვალა, მნიშვნელობა — არა ({where})",
                f"the formula changed while the value did not ({where})",
                expected=_short(old_formula),
                actual=_short(new_formula),
                full=(old_formula, new_formula),
            )
            continue
        rule = next((r for r in h.rules if r in _TEXT_MESSAGES), "CMP.TEXT_CHANGED")
        ka, en = _TEXT_MESSAGES[rule]
        anchor = h.right or h.left
        where = anchor.path if anchor is not None else ""
        add(
            rule,
            "warning",
            f"{ka} ({where})",
            f"{en} ({where})",
            expected=_short(h.left.text if h.left else None),
            actual=_short(h.right.text if h.right else None),
            full=texts(h),
        )

    for d in field_deltas:
        ka, en = _delta_message(d)
        add(d.rule_id, d.severity, ka, en, expected=d.left, actual=d.right, key=dedup_key(d))
    return out, suppressed


# --- entry point ----------------------------------------------------------------------------


def compare(
    left: list[Segment],
    right: list[Segment],
    *,
    left_document_id: str,
    right_document_id: str,
    left_name: str = "",
    right_name: str = "",
    left_kind: str = "",
    right_kind: str = "",
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    max_segments: int = 20_000,
    ocr: bool | None = None,
) -> ComparisonReport:
    """Compare two segmented documents. Deterministic: no I/O and no model.

    ``ocr`` says whether either side's text came from OCR, which softens character
    substitutions to "most likely the scanner". Pass it: a born-digital PDF is not a scan, and
    a Cyrillic letter swapped into its IBAN is exactly what must not be softened
    (``segmenter.pdf_has_text_layer`` tells the two apart). ``None`` keeps the older reading,
    that any PDF may be a scan.
    """
    started = time.perf_counter()
    for label, segments in (("first", left), ("second", right)):
        if len(segments) > max_segments:
            msg = (
                f"the {label} document has {len(segments)} segments, more than the "
                f"{max_segments} this comparison is configured to align"
            )
            raise DocumentTooLargeError(msg)

    skipped: list[SkipNote] = []
    both = bool(left) and bool(right)
    l_sheet = both and left[0].kind == "sheet_cell"
    r_sheet = both and right[0].kind == "sheet_cell"
    spreadsheets = l_sheet and r_sheet
    mixed = both and l_sheet != r_sheet
    if mixed:
        skipped.append(
            SkipNote(
                rule_id="CMP.LINE_DIFF",
                reason_ka="ერთი ფაილი ცხრილია, მეორე — ტექსტური დოკუმენტი; "
                "სტრიქონობრივი შედარება ასეთ წყვილზე აზრს არ იძლევა",
                reason_en="one file is a spreadsheet and the other a text document; "
                "a line-by-line comparison of that pair means nothing",
            )
        )

    pairs, ratio = _align_sheets(left, right) if spreadsheets else _align_linear(left, right)
    moves = _detect_moves(pairs)
    if ocr is None:
        ocr = "pdf" in (left_kind, right_kind)
    rows, hunks, counts = _build(pairs, moves, ocr=ocr)

    alignment: Alignment = "versions" if ratio >= min_similarity else "unrelated"
    if alignment == "unrelated" and not mixed:
        skipped.append(
            SkipNote(
                rule_id="CMP.LINE_DIFF",
                reason_ka=f"დოკუმენტები ერთმანეთს მხოლოდ {ratio:.0%}-ით ემთხვევა — ეს ერთი "
                "დოკუმენტის ორი ვერსია არ არის, ამიტომ სტრიქონობრივი შედარება ხმაურია; "
                "იხილეთ მნიშვნელოვან ველთა სხვაობები",
                reason_en=f"the documents overlap only {ratio:.0%} — these are not two versions "
                "of one document, so the line-by-line diff is noise; read the field "
                "differences instead",
            )
        )

    left_text, right_text = document_text(left), document_text(right)
    if len(left_text) + len(right_text) > 400_000:
        field_deltas: list[ValueDelta] = []
        skipped.append(
            SkipNote(
                rule_id="CMP.FIELDS",
                reason_ka="დოკუმენტები ძალიან დიდია ველების დონეზე შესადარებლად",
                reason_en="the documents are too large for the document-wide field comparison",
            )
        )
    else:
        field_deltas = deltas_between(left_text, right_text, types=FIELD_TYPES)

    findings, suppressed = _findings_of(hunks, field_deltas, right_text, ocr=ocr)
    if suppressed:
        skipped.append(
            SkipNote(
                rule_id="CMP.MINOR",
                reason_ka=f"კიდევ {suppressed} ნაკლებმნიშვნელოვანი ცვლილება სიაში არ არის — "
                "ყველა მათგანი ქვემოთ, შედარების ხედშია. კრიტიკული ცვლილებები ყოველთვის "
                "სრულად ჩამოითვლება.",
                reason_en=f"{suppressed} further minor changes are not listed — every one of "
                "them is in the diff below. Critical changes are never truncated.",
            )
        )

    return ComparisonReport(
        left_document_id=left_document_id,
        right_document_id=right_document_id,
        left_name=left_name,
        right_name=right_name,
        left_kind=left_kind,
        right_kind=right_kind,
        alignment=alignment,
        similarity=round(ratio, 4),
        left_segments=len(left),
        right_segments=len(right),
        counts=counts,
        rows=rows,
        hunks=hunks,
        field_deltas=field_deltas,
        findings=findings,
        skipped=skipped,
        seconds=round(time.perf_counter() - started, 4),
    )
