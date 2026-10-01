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
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from procurement_agent.checks.amounts import (
    FIGURE,
    NUM_IN_TEXT,
    looks_like_numeral,
    number_of,
    read_words,
)
from procurement_agent.checks.findings import Finding, Severity, SkipNote
from procurement_agent.extraction.anchors import (
    bic_is_well_formed,
    find_anchors,
    find_parties,
    plain_party_wording,
)
from procurement_agent.extraction.georgian import (
    MixedWord,
    is_georgian_word,
    lemma,
    mixed_script_words,
    month_typo,
    number_words_typo,
    parse_number_words,
    same_phrase,
)
from procurement_agent.extraction.georgian import words as georgian_words
from procurement_agent.extraction.labels import LABELS
from procurement_agent.extraction.normalize import (
    amounts_equal,
    confusable_chars,
    iban_is_valid,
    name_stem,
    names_match,
    normalize_confusables,
    normalize_name,
    normalize_spaces,
    parse_number,
    strip_quotes,
)
from procurement_agent.extraction.rules import (
    _KA_MONTHS,
    VAT_RATE_NOTE,
    RuleHit,
    date_role,
    run_rules,
)
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
        FactType.act_date,
        FactType.contract_number,
        FactType.party_name,
        FactType.party_id_code,
        FactType.party_director,
        FactType.party_address,
        FactType.bank_account,
        FactType.bank_code,
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
_DATES = frozenset({FactType.contract_date, FactType.act_date, FactType.date})
_NAMES = frozenset({FactType.party_name, FactType.party_director, FactType.project_name})
_BANK = frozenset({FactType.bank_account, FactType.bank_code})
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
    FactType.act_date: "CMP.DATE",
    FactType.date: "CMP.DATE",
    FactType.bank_account: "CMP.BANK",
    FactType.bank_code: "CMP.BANK",
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
    """A bank detail as it is compared: letters and digits only, and a BIC's branch form
    "XXX" as its eight-letter code, the key the act check compares bank codes by (ISO 9362). A
    code restated with its "XXX" was a critical bank change the act check calls no change."""
    # imported here: checks.aoa imports compare_llm, which imports this module
    from procurement_agent.checks.aoa import _bank_key as branch_folded

    return branch_folded(re.sub(r"[^A-Z0-9]", "", normalize_confusables(value).upper()))


def _value_key(ft: FactType, value: str, normalized: str | float | None) -> str:
    if ft is FactType.quantity and isinstance(normalized, str):
        return f"z:{normalized}"  # "4 item" is not "4 set"
    if ft in _NUMERIC:
        n = parse_number(value)
        if n is not None:
            return f"n:{n:.4f}"
    if ft in _BANK:
        return f"b:{_bank_key(value)}"
    if normalized is not None:
        return f"z:{normalized}"
    return f"s:{normalize_name(value)}"


def hits_of(text: str, *, reference: str = "") -> list[_Hit]:
    """Every typed value in ``text``, anchors first, deduplicated by type and value.

    Anchors before rules is the ordering ``merger.merge`` already uses: a value read from behind
    its own label is the best evidence available, so it wins the slot.

    The text is folded first, exactly as ``AoaChecker``'s ``DocumentView.build`` folds it. Both
    passes must agree about what the document says or typography starts inventing differences:
    a revision that replaced every hyphen with an en dash reported the contract number changing
    from "PR-2026/017" to "PR", because the number pattern stopped matching at the dash while
    the line itself was correctly called formatting-only. For the same reason a stray letter of
    another alphabet inside a Georgian word is read past (``_as_meant``); ``reference``, the
    text this one is compared with, says which word it was meant to be.
    """
    text = _as_meant(normalize_confusables(text), reference)
    out: list[_Hit] = []
    seen: set[tuple[FactType, str]] = set()

    def keep(hit: _Hit) -> None:
        k = (hit.type, _value_key(hit.type, hit.value, hit.normalized))
        if k not in seen:
            seen.add(k)
            out.append(hit)

    for a in find_anchors(text):
        keep(_canonical(_Hit(a.type, a.value, a.normalized, a.unit, None, "anchor")))
    for hit in _rule_hits(text):
        keep(_canonical(hit))
    for _start, _end, span in _month_periods(text):
        keep(_Hit(FactType.execution_period, span, span, None, None, "rules"))
    for hit in _vat_hits(text):
        keep(hit)
    for hit in _quantity_hits(text):
        keep(hit)
    return out


#: What may follow a month named as a period, and belongs to it: "თვეში", "თვის განმავლობაში".
_MONTH_TAIL = re.compile(r"\s*(?:თვეში|თვის\s+განმავლობაში|თვის|თვე)(?![ა-ჰ])")


def _month_periods(text: str) -> list[tuple[int, int, str]]:
    """The service periods ``text`` states as a whole month - "შესრულდა 2026 წლის მაისის
    თვეში" - as (start, end, "first/last day"), read as ``checks.document_facts`` reads them for
    the whole document: that month's first to last day, in a sentence about the work and not
    about an advance.

    The line reader did not know the form. An act's period restated "in May" was an execution
    period removed on its line while the whole-document reading saw the same period on both
    sides, and moved to June it was a period change and a "text changed" besides.
    """
    # imported here: document_facts reads through checks.aoa, which imports this module
    from procurement_agent.checks.aoa import _ADVANCE_WORDS
    from procurement_agent.checks.document_facts import _MONTH_PERIOD, _SERVICE_WORDS
    from procurement_agent.extraction.rules import _month_ka

    out: list[tuple[int, int, str]] = []
    for m in _MONTH_PERIOD.finditer(text):
        month = _month_ka(m.group("m"))
        if month is None:
            continue
        starts = [c.end() for c in _CLAUSE_START.finditer(text, 0, m.start())]
        stop = _CLAUSE_START.search(text, m.end())
        sentence = text[starts[-1] if starts else 0 : stop.start() if stop else len(text)]
        if not _SERVICE_WORDS.search(sentence) or _ADVANCE_WORDS.search(sentence):
            continue
        year = int(m.group("y"))
        first = date(year, month, 1)
        last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
        tail = _MONTH_TAIL.match(text, m.start("m") + len(m.group("m")))
        end = tail.end() if tail else m.end()
        out.append((m.start(), end, f"{first.isoformat()}/{last.isoformat()}"))
    return out


#: The case endings of a unit noun, and nothing longer: "ცალი", "ცალს", "ტონამდე" are units,
#: "ცალკე" (separately), "ცალმხრივი" (unilateral), "ტონერი" (toner) and "ლიტრიანი" (a bottle's
#: size) are other words.
_CASE = r"(?:ი|ა|ს|ის|ას|ით|ად|ამდე|ზე|ში|ებ(?:ი|ის|ს|ით|ად|ამდე)?)?(?![ა-ჰ])"
#: The units a procured quantity is counted in, and the name ``docs/GOLD_SPEC.md`` gives each.
#: A closed list: copies of the document ("2 ეგზემპლარად"), pages, trips and months are counts
#: or periods, not quantities, and stay what the other rules make of them.
_UNITS: tuple[tuple[str, str], ...] = (
    # "ც." abbreviates ცალი, and "მ2" / "მ3" write the metre with a plain digit
    (
        rf"ცალ{_CASE}|(?<![ა-ჰ])ც(?:\.|(?![ა-ჰ]))|(?<![a-z])(?:pcs|pieces?|items?)(?![a-z])",
        "item",
    ),
    (rf"კომპლექტ{_CASE}|(?<![a-z])sets?(?![a-z])", "set"),
    (
        rf"კვ\.?\s*მ(?:ეტრ{_CASE})?(?![ა-ჰ])|(?<![ა-ჰ])მ[2²](?!\d)"
        r"|(?<![a-z])(?:m²|m2|sq\.?\s*m)(?![a-z0-9])",
        "m2",
    ),
    (
        rf"კუბ\.?\s*მ(?:ეტრ{_CASE})?(?![ა-ჰ])|(?<![ა-ჰ])მ[3³](?!\d)"
        r"|(?<![a-z])(?:m³|m3|cu\.?\s*m)(?![a-z0-9])",
        "m3",
    ),
    (rf"კგ(?![ა-ჰ])|კილოგრამ{_CASE}|(?<![a-z])kg(?![a-z])", "kg"),
    (rf"ტონ{_CASE}|(?<![a-z])(?:tons?|tonnes?)(?![a-z])", "t"),
    (rf"ლიტრ{_CASE}|(?<![a-z])(?:litres?|liters?)(?![a-z])", "l"),
)
_QUANTITY = re.compile(
    r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(?:\(\s*[ა-ჰa-z\s-]+\s*\)\s*)?(?:"
    + "|".join(f"(?P<u{i}>{pattern})" for i, (pattern, _unit) in enumerate(_UNITS))
    + ")",
    re.IGNORECASE,
)


def _quantity_hits(text: str) -> list[_Hit]:
    """Quantities a text states: a number, its words in brackets if written, and a unit -
    "4 (ოთხი) ცალი", "12 კომპლექტი", "3 pcs" - as "4 item", the form of ``docs/GOLD_SPEC.md``.

    The extraction reads quantities with the model only, so a count of what is procured that
    changed from four to five, or from pieces to sets, was "text changed", or a warning about
    a number in words.
    """
    out: list[_Hit] = []
    for m in _QUANTITY.finditer(text):
        number = parse_number(m.group(1))
        unit = next((u for i, (_p, u) in enumerate(_UNITS) if m.group(f"u{i}") is not None), None)
        if number is None or unit is None:
            continue
        canonical = f"{number:g} {unit}"
        out.append(_Hit(FactType.quantity, m.group(0), canonical, unit, None, "rules"))
    return out


#: Which days a term counts, as ``docs/GOLD_SPEC.md`` writes them: business and banking days
#: are working days (``contract_terms`` reads them the same way).
_DAY_BASIS = {"working": "working", "business": "working", "banking": "working"}
_DAY_BASIS["calendar"] = "calendar"


def _canonical(hit: _Hit) -> _Hit:
    """A payment term in the form ``docs/GOLD_SPEC.md`` fixes: its length and the days it
    counts, "P5D working". Both terms of "7 working days" → "7 calendar days" read "P7D", and
    the one thing that changed was compared away."""
    if hit.type is not FactType.payment_terms or not isinstance(hit.normalized, str):
        return hit
    if not _ISO_COUNT.match(hit.normalized) or " " in hit.normalized:
        return hit
    basis = _basis(hit)
    if basis is None:
        return hit
    return _Hit(
        hit.type, hit.value, f"{hit.normalized} {basis}", hit.unit, hit.currency, hit.source
    )


def _basis(hit: _Hit) -> str | None:
    """Which days a duration counts, "working" or "calendar", when it says: its value reads
    "7 working days" or "7 days"."""
    words = hit.value.lower().split()
    return _DAY_BASIS.get(words[1]) if len(words) == 3 else None


_VAT_STATUS = {"inclusion": "incl", "exclusion": "excl", "net": "net", "exempt": "none"}


def _vat_hits(text: str) -> list[_Hit]:
    """Whether the price includes VAT, read by the reader the tax-wording rule and the
    whole-document pass use: "დღგ-ს ჩათვლით" is ``incl``, "გადასახადების გარეშე" ``excl``.

    Without it a price that stopped including VAT was a line of "text changed", and a contract
    and its act that disagreed about taxes were not told apart at all.
    """
    # imported here: contract_terms imports compare_llm, which imports this module
    from procurement_agent.checks.contract_terms import tax_phrases

    out: list[_Hit] = []
    for phrase in tax_phrases(text):
        status = _VAT_STATUS.get(phrase.kind)
        if status is not None:
            out.append(_Hit(FactType.vat_status, status, status, None, None, "rules"))
    return out


#: A Latin or Cyrillic letter: inside a Georgian word, a slip of the keyboard layout.
_FOREIGN_LETTER = re.compile(r"[A-Za-zЀ-ӿ]")
#: A slip is a letter or two. A word with more is a name written in two alphabets, "ABCჯგუფი",
#: and is read as written.
_MAX_SLIP = 2


def _slips(text: str, vocabulary: frozenset[str] = frozenset()) -> list[MixedWord]:
    """The Georgian words of ``text`` with a Latin or Cyrillic letter or two typed into them."""
    return [
        w
        for w in mixed_script_words(text, vocabulary=vocabulary)
        if 0 < len(_FOREIGN_LETTER.findall(w.word)) <= _MAX_SLIP
    ]


def _as_meant(text: str, reference: str = "") -> str:
    """``text`` with every Georgian word that has a Latin or Cyrillic letter typed into it read
    as the word it was meant to be.

    The slip is reported once, by ``spelling_deltas``, as the misspelling it is. Read literally,
    the same one letter was also a second, false change: a stray letter in "სამუშაო" (working)
    turned "five working days" into a payment term removed, and one in "დირექტორის" (director)
    removed the director, whose label no longer read as one. The meant word is the one
    ``reference`` writes one letter away, else the word without the stray letters. A word with
    a digit inside it and no foreign letter is left as it is.
    """
    if not _slips(text):
        return text
    vocabulary = frozenset(
        w for w, _, _ in georgian_words(normalize_confusables(reference)) if is_georgian_word(w)
    )
    out: list[str] = []
    pos = 0
    for word in _slips(text, vocabulary):
        out.append(text[pos : word.start])
        out.append(word.correction or _FOREIGN_LETTER.sub("", word.word))
        pos = word.end
    out.append(text[pos:])
    return "".join(out)


#: A period as ``docs/GOLD_SPEC.md`` writes it, "start/end"; ".." for an end not stated.
_ISO_RANGE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\.\.)/(?:\d{4}-\d{2}-\d{2}|\.\.)$")
#: What stands between the two dates of one period: "1 მარტიდან 31 მარტის ჩათვლით" (the
#: first date in the "from" form), "01.03.2026-დან 30.04.2026-მდე", "01.03.2026 - 30.04.2026",
#: "1 March 2026 to 31 March 2026". Two dates joined by "და" (and) are two dates.
_RANGE_GAP = re.compile(r"-?დან[\s,]*|\s*-\s*|\s+(?:to|until|till|through)\s+", re.IGNORECASE)
_RANGE_AFTER_FROM = re.compile(r"[\s,]*")


def _rule_hits(text: str) -> list[_Hit]:
    """``run_rules``'s values, with a period written from one date to another read as one value.

    A period is one entity: moved a year, it is one change. Read as two dates it was two
    findings ("start 2026-03-01 → 2027-03-01", "end 2026-03-31 → 2027-03-31"), and a reader
    could not tell whether one period moved or two unrelated dates did. The range is written
    "start/end", the form the gold and the extraction use (docs/GOLD_SPEC.md).
    """
    rules = run_rules(text)
    out: list[_Hit] = []
    i = 0
    while i < len(rules):
        r = rules[i]
        end = _range_end(text, rules, i)
        if end is None:
            ft = _payment_if_said(text, r, _total_if_said(text, r))
            unit = _VAT_RATE if r.note == VAT_RATE_NOTE else r.unit
            normalized = _advance_if_said(text, r, ft)
            out.append(_Hit(ft, r.value, normalized, unit, r.currency, "rules"))
            i += 1
            continue
        j, ft = end
        span = f"{r.normalized}/{rules[j].normalized}"
        out.append(_Hit(ft, span, span, None, None, "rules"))
        i = j + 1
    return out


#: A price the words before it call the whole: "the total price", "სულ ღირებულება".
_TOTAL_WORDS = re.compile(r"(?<![a-z])total(?![a-z])|(?<![ა-ჰ])(?:სულ|ჯამურ|საერთო)", re.I)
#: ... unless they call it the price of one unit.
_UNIT_WORDS = re.compile(r"(?<![a-z])unit\s+(?:price|rate|cost)|ერთეულის\s+ფას", re.I)
#: Where the sentence a value is in begins: a full stop, semicolon or line break.
_CLAUSE_START = re.compile(r"[;\n]|\.(?!\d)")


def _total_if_said(text: str, hit: RuleHit) -> FactType:
    """A price the sentence calls the total, as the total amount.

    The nearest keyword types an amount, and in "The total price of the services is GEL
    9,600.00" that is "price": the contract's whole fee was labelled a unit price.
    """
    if hit.type is not FactType.price:
        return hit.type
    starts = [m.end() for m in _CLAUSE_START.finditer(text, 0, hit.start)]
    before = text[starts[-1] if starts else 0 : hit.start]
    if _TOTAL_WORDS.search(before) and not _UNIT_WORDS.search(before):
        return FactType.total_amount
    return hit.type


#: A sentence about paying: "საფასური გადაიხდება", "ანგარიშსწორება მოხდება", "payable within".
_PAYMENT_WORDS = re.compile(
    r"გადაიხდ|გადახდ|ანგარიშსწორ|(?<![a-z])(?:paid|payable|payments?|pay)(?![a-z])", re.I
)


def _payment_if_said(text: str, hit: RuleHit, ft: FactType) -> FactType:
    """A length of time the sentence it is in pays within, as a payment term.

    The keyword nearest a duration types it, and "საფასური გადაიხდება აქტის გაფორმებიდან 7
    სამუშაო დღის ვადაში" - the way an act says it - has none the rules know ("გადაიხდება" is
    not "გადახდა"): the act's payment term was an unclassified duration, a change of it
    "Duration (unclassified)", and against the contract a payment term "removed".
    """
    if ft is not FactType.duration:
        return ft
    return FactType.payment_terms if _PAYMENT_WORDS.search(_sentence_of(text, hit)) else ft


def _sentence_of(text: str, hit: RuleHit) -> str:
    """The sentence ``hit`` stands in: from the last full stop, semicolon or line break before
    it to the first after it."""
    starts = [m.end() for m in _CLAUSE_START.finditer(text, 0, hit.start)]
    end = _CLAUSE_START.search(text, hit.end)
    return text[starts[-1] if starts else 0 : end.start() if end else len(text)]


def _advance_if_said(text: str, hit: RuleHit, ft: FactType) -> str | float | None:
    """A share of the price paid in advance in the form ``docs/GOLD_SPEC.md`` gives payment
    terms, "advance 20%": reported as a bare "20%" -> "30%", the change matched no gold entry
    and did not say what the percentage was of. The words stay in the finding's note; a
    percentage of a payment sentence that names no advance stays as it is written."""
    if ft is not FactType.payment_terms or hit.unit != "%":
        return hit.normalized
    number = as_number(hit.value, hit.normalized)
    if number is None:
        return hit.normalized
    # imported here: contract_terms imports compare_llm, which imports this module
    from procurement_agent.checks.contract_terms import _ADVANCE

    if not _ADVANCE.search(_sentence_of(text, hit)):
        return hit.normalized
    return f"advance {number:g}%"


def _range_end(text: str, rules: list[RuleHit], i: int) -> tuple[int, FactType] | None:
    """The hit that closes a period opened by ``rules[i]``, and the period's type, if any."""
    first = rules[i]
    if not _ISO_DATE.match(str(first.normalized)):
        return None
    j = i + 1
    while j < len(rules) and rules[j].start < first.end:  # a hit read inside the first date
        j += 1
    if j == len(rules):
        return None
    second = rules[j]
    if not _ISO_DATE.match(str(second.normalized)):
        return None  # a range written with its end first is still that range: its syntax says so
    if first.type == second.type or second.type is FactType.date:
        ft = first.type
    elif first.type is FactType.date:
        ft = second.type
    else:
        return None
    gap = text[first.end : second.start]
    marked = _RANGE_GAP.fullmatch(gap) is not None or (
        text[first.start : first.end].rstrip().endswith("დან") and _RANGE_AFTER_FROM.fullmatch(gap)
    )
    if marked and ft not in _PERIOD_TYPES:
        # two dates written as a range are a period whatever word names it ("გაწეულია
        # 01.11.2026 - 30.11.2026 პერიოდში"): the time the work is done, or the contract's
        # validity where the words before say so. Never a point in time: a window "from 1 April
        # to 30 September" near the word "contract" was a contract date holding a range, and
        # one under delivery words a delivery deadline - each a type that holds one date
        # (docs/GOLD_SPEC.md section 2)
        before = text[max(0, first.start - _VALIDITY_WINDOW) : first.start]
        valid = _VALIDITY_WORDS.search(before) is not None
        ft = FactType.validity_period if valid else FactType.execution_period
    return (j, ft) if marked else None


#: The types that hold a period from one date to another.
_PERIOD_TYPES = frozenset({FactType.execution_period, FactType.validity_period})


#: Words that make a range the contract's validity rather than the time the work is done.
_VALIDITY_WORDS = re.compile(r"მოქმედ|ძალაში|(?<![a-z])(?:valid|in\s+force)", re.IGNORECASE)
_VALIDITY_WINDOW = 80


def _is_range(hit: _Hit) -> bool:
    return isinstance(hit.normalized, str) and _ISO_RANGE.match(hit.normalized) is not None


def _endpoints(hits: list[_Hit]) -> list[_Hit]:
    """The hits with every period read back as the dates it states."""
    out: list[_Hit] = []
    for hit in hits:
        if not _is_range(hit):
            out.append(hit)
            continue
        for day in str(hit.normalized).split("/"):
            if day != "..":
                out.append(_Hit(hit.type, day, day, None, None, hit.source))
    return out


#: The name this module has always used; the parser itself lives in ``checks.amounts`` so the
#: AOA check reads numbers exactly the same way.
as_number = number_of


def _same_value(a: _Hit, b: _Hit) -> bool:
    ft = a.type
    if ft is FactType.quantity and a.unit != b.unit:
        return False  # four pieces are not four sets
    if ft in _NUMERIC:
        x, y = as_number(a.value, a.normalized), as_number(b.value, b.normalized)
        if x is not None and y is not None:
            return amounts_equal(x, y)
    if ft in _NAMES and (names_match(a.value, b.value) or _same_lemma(a.value, b.value)):
        return True
    if ft in _BANK:
        return _bank_key(a.value) == _bank_key(b.value)
    if ft in _DURATIONS and None not in (_basis(a), _basis(b)) and _basis(a) != _basis(b):
        return False  # 30 working days are not 30 calendar days
    if a.normalized is not None and b.normalized is not None:
        return str(a.normalized) == str(b.normalized)
    return normalize_name(a.value) == normalize_name(b.value)


def _same_lemma(a: str, b: str) -> bool:
    """Two forms of one name: "ლაზური რეზიდენსის" and "ლაზური რეზიდენსისთვის", and "მთის
    ვერანდისთვის" and "მთის ვერანდა" - a form two nominatives share (``same_phrase``)."""
    return normalize_name(lemma(a)) == normalize_name(lemma(b)) or same_phrase(a, b)


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


#: The unit of a percentage that is the VAT's rate (``rules.VAT_RATE_NOTE``).
_VAT_RATE = "% VAT"


def _delta(a: _Hit | None, b: _Hit | None) -> ValueDelta | None:
    """One difference between two typed values, or None when they say the same thing."""
    hit = a or b
    if hit is None:  # pragma: no cover - never called with two Nones
        return None
    if (a is None or b is None) and hit.unit == _VAT_RATE:
        # "დღგ-ს ჩათვლით" restated "დღგ-ს (18%) ჩათვლით" says no more: the rate is part of the
        # tax wording, whose change CMP.VAT reports. A rate changed (18% -> 20%) is reported.
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
        if _is_range(a) and _is_range(b):
            note = _range_note(str(a.normalized), str(b.normalized))
        if ft in _BANK:
            note = _bank_note(a.value, b.value)
        if ft in _NAMES and note is None:
            diff = classify_difference(b.value, a.value)
            if diff is not None and diff.looks_like_typo:
                note = ("looks like a misspelling", "ჰგავს მართლწერის შეცდომას")
        if note is None:
            note = _as_written_note(a, b)
        left, right = _as_reported(a), _as_reported(b)
        if left == right and ft in _DURATIONS:  # only the days they count changed
            left, right = f"{left} {_basis(a)}", f"{right} {_basis(b)}"
        return ValueDelta(
            type=ft,
            kind="changed",
            rule_id=rule,
            severity=severity,
            label_en=en,
            label_ka=ka,
            left=left,
            right=right,
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
        left=_as_reported(a) if a is not None else None,
        right=_as_reported(b) if b is not None else None,
        note_en=note_one[0] if (note_one := _as_written_note(a, b)) else None,
        note_ka=note_one[1] if note_one else None,
    )


#: Names a document inflects, and a finding states in the nominative (docs/GOLD_SPEC.md).
_NOMINATIVE = frozenset({FactType.party_director, FactType.project_name})


#: Values a finding states in their canonical form (docs/GOLD_SPEC.md section 2): a payment
#: term "P5D working", a warranty "P24M", a delivery deadline "P30D" or its date, a quantity
#: "4 item".
_CANONICAL = _DURATIONS | {FactType.payment_terms, FactType.quantity}


def _as_reported(hit: _Hit) -> str:
    """A value the way a finding states it: a director or a project by name, in the nominative,
    and a term in its canonical form.

    The preamble names the director in the genitive ("დირექტორის ნინო კაპანაძის სახით"), the
    signature block in the nominative; a project is "„ქარვის კოშკისთვის“" in one clause and
    "„ქარვის კოშკის“" in the next. Taken as written, one misspelled name was one finding per case
    form, each in a grammar the reader then had to undo; the name itself is what changed. A
    payment term stated as "7 working days" could never be matched against "P7D working", the
    form the gold and the extraction write; the words stay in the finding's note.
    """
    if hit.type in _NOMINATIVE:
        return lemma(hit.value)
    if hit.type in _CANONICAL and isinstance(hit.normalized, str) and hit.normalized:
        return hit.normalized
    return hit.value


def _as_written_note(a: _Hit | None, b: _Hit | None) -> tuple[str, str] | None:
    """The values as the documents write them, when the finding states them otherwise."""
    shown = [(h, _as_reported(h)) for h in (a, b) if h is not None and h.type in _CANONICAL]
    if not shown or all(h.value == value for h, value in shown):
        return None
    words = " → ".join(h.value for h, _ in shown)
    return (f"as written: {words}", f"როგორც წერია: {words}")


def _range_note(left: str, right: str) -> tuple[str, str] | None:
    """How far each end of a period moved: "start +365 days, end +365 days"."""
    ends = [
        (_iso_to_date(a), _iso_to_date(b))
        for a, b in zip(left.split("/"), right.split("/"), strict=True)
    ]
    parts_en: list[str] = []
    parts_ka: list[str] = []
    for (d1, d2), en, ka in zip(ends, ("start", "end"), ("დასაწყისი", "დასასრული"), strict=True):
        if d1 is None or d2 is None or d1 == d2:
            continue
        parts_en.append(f"{en} {(d2 - d1).days:+d} days")
        parts_ka.append(f"{ka} {(d2 - d1).days:+d} დღე")
    return (", ".join(parts_en), ", ".join(parts_ka)) if parts_en else None


def deltas_between(
    left_text: str, right_text: str, *, types: frozenset[FactType] | None = None
) -> list[ValueDelta]:
    """Typed differences between two pieces of text, paired per fact type."""
    left_hits = hits_of(left_text, reference=right_text) if left_text else []
    right_hits = hits_of(right_text, reference=left_text) if right_text else []
    return _deltas(left_hits, right_hits, types=types)


#: What a document states as a whole rather than on a line: a contract's term and its
#: performance window, an act's service period, a penalty and the period it counts per, and
#: whether its price includes VAT - read from the tax wording around the fee, not from every
#: tax word the document holds.
_WHOLE_DOCUMENT_TYPES = frozenset(
    {FactType.execution_period, FactType.validity_period, FactType.penalty, FactType.vat_status}
)
#: A document's own dates, known by where they stand rather than by a word beside them: the
#: contract's in its preamble ("ქ. თბილისი, 2026 წლის 12 თებერვალი"), the act's where it is signed.
_DOCUMENT_DATES = frozenset({FactType.contract_date, FactType.act_date})


def document_deltas(left_text: str, right_text: str) -> tuple[list[ValueDelta], list[SkipNote]]:
    """The document-wide field differences: every specific value, compared wherever it is.

    The facts a document states as a whole are read the way ``checks.document_facts`` reads
    them for the extraction. Date by date, a contract's performance window "from signing until
    2026-12-31" against its act's service period "2026-03-01 to 2026-03-31" came out as "an
    execution period of 2026-03-31 added", its term "in force from signing" not at all, and its
    "0.1% per day" penalty as a bare "0.1%".

    When those readers fail on either document, both are compared line by line and the note
    says so: a comparison is never lost to one reader, and the failure is not hidden either.
    """
    left_hits = hits_of(left_text, reference=right_text)
    right_hits = hits_of(right_text, reference=left_text)
    try:
        left_facts, right_facts = _document_facts(left_text), _document_facts(right_text)
    except Exception as e:  # one reader failing must not cost the comparison; it is reported
        note = SkipNote(
            rule_id="CMP.FIELDS",
            reason_ka="ვადების, პერიოდებისა და პირგასამტეხლოს მთლიანი დოკუმენტიდან წაკითხვა "
            f"ვერ მოხერხდა ({type(e).__name__}); ისინი სტრიქონ-სტრიქონ შედარდა",
            reason_en="the whole-document reading of the term, the periods and the penalties "
            f"failed ({type(e).__name__}); they were compared line by line",
        )
        return _deltas(left_hits, right_hits, types=FIELD_TYPES), [note]
    whole = [
        _in_place_of(hits, [f for f in facts if f.type in _WHOLE_DOCUMENT_TYPES])
        for hits, facts in ((left_hits, left_facts), (right_hits, right_facts))
    ]
    return _deltas(*whole, types=FIELD_TYPES) + _dates_changed(left_facts, right_facts), []


def _dates_changed(left: list[_Hit], right: list[_Hit]) -> list[ValueDelta]:
    """A document's own date changed between the versions: one each side, and different. The
    line it stands on may name it by no word ("Date (unclassified)"); this names it."""
    out: list[ValueDelta] = []
    for ft in sorted(_DOCUMENT_DATES, key=lambda t: t.value):
        ls = [h for h in left if h.type is ft]
        rs = [h for h in right if h.type is ft]
        if len(ls) == 1 and len(rs) == 1 and (d := _delta(ls[0], rs[0])) is not None:
            out.append(d)
    return out


def _document_facts(text: str) -> list[_Hit]:
    """The whole-document facts ``text`` states, as values."""
    # imported here: document_facts reads through checks.aoa, which imports compare_llm and so
    # this module
    from procurement_agent.checks.document_facts import document_facts

    if not text:
        return []
    return [
        _Hit(f.type, f.normalized, f.normalized, None, None, "document")
        for f in document_facts(text)
        if f.type in _WHOLE_DOCUMENT_TYPES | _DOCUMENT_DATES
    ]


def _in_place_of(hits: list[_Hit], facts: list[_Hit]) -> list[_Hit]:
    """``hits`` with ``facts`` in place of the line-level hits of the same types."""
    stated = {f.type for f in facts}
    return [h for h in hits if h.type not in stated] + facts


def _deltas(
    left_hits: list[_Hit], right_hits: list[_Hit], *, types: frozenset[FactType] | None
) -> list[ValueDelta]:
    if types is not None:
        left_hits = [h for h in left_hits if h.type in types]
        right_hits = [h for h in right_hits if h.type in types]
    out: list[ValueDelta] = []
    present = {h.type for h in left_hits} | {h.type for h in right_hits}
    for ft in sorted(present, key=lambda t: t.value):
        ls = [h for h in left_hits if h.type == ft]
        rs = [h for h in right_hits if h.type == ft]
        if ls and rs and any(map(_is_range, ls)) != any(map(_is_range, rs)):
            # one side writes the period from one date to the other, the other names its dates
            # apart: compare the dates, as a range against a single date means nothing
            ls, rs = _endpoints(ls), _endpoints(rs)
        for a, b in _pair_hits(ls, rs):
            d = _delta(a, b)
            if d is not None:
                out.append(d)
    return _without_relabels(out)


#: Types a value keeps when only the word naming it changes: "ფასი" and "ღირებულება" read the
#: same 100 as a unit price and as a total.
_RELABEL_FAMILIES: tuple[frozenset[FactType], ...] = (
    frozenset({FactType.price, FactType.total_amount, FactType.amount, FactType.penalty}),
    frozenset(
        {
            FactType.contract_date,
            FactType.act_date,
            FactType.date,
            FactType.delivery_deadline,
            FactType.execution_period,
            FactType.validity_period,
        }
    ),
    frozenset(
        {
            FactType.duration,
            FactType.payment_terms,
            FactType.warranty_period,
            FactType.delivery_deadline,
            FactType.execution_period,
            FactType.validity_period,
        }
    ),
)


def _without_relabels(deltas: list[ValueDelta]) -> list[ValueDelta]:
    """Drop a removal and an addition that are one value relabelled.

    Values are paired within a type, and the type comes from the nearest keyword: reworded
    from "ფასი შეადგენს 100 ლარს" to "ღირებულება შეადგენს 100 ლარს", the same 100 was a unit
    price removed and a total added - two critical changes for one changed word.
    """
    removed = [d for d in deltas if d.kind == "removed"]
    added = [d for d in deltas if d.kind == "added"]
    drop: set[int] = set()
    for r in removed:
        for a in added:
            if id(a) in drop or id(r) in drop:
                continue
            if not any(r.type in fam and a.type in fam for fam in _RELABEL_FAMILIES):
                continue
            if _display_value(r.type, r.left) == _display_value(a.type, a.right):
                drop.update({id(r), id(a)})
                break
    return [d for d in deltas if id(d) not in drop]


def _display_value(ft: FactType, value: str | None) -> str | None:
    if value is None:
        return None
    if ft is FactType.quantity:
        return normalize_name(value)  # "4 item": the unit is part of it
    if ft in _NUMERIC or ft in (FactType.penalty,):
        n = as_number(value)
        if n is not None:
            return _number_key(n, value)
    if ft in _NAMES:
        return _name_key(value)
    return normalize_name(value)


def _number_key(number: float, value: str) -> str:
    """A figure's key with its unit: "10%" is not "10 GEL", as a penalty of a tenth of the
    price is not a ten-lari fee - keyed by the number alone, a new 10% penalty counted as
    written in a document that charged a 10 GEL bank fee, and was never reported."""
    return f"n:{number:.4f}{'%' if '%' in value else ''}"


def _name_key(value: str) -> str:
    """One key for every case form of a name: "ქარვის კოშკისთვის", "ქარვის კოშკის" and
    "ქარვის კოშკი" are one project, "ნინო კაპანაძის" and "ნინო კაპანაძე" one person."""
    return name_stem(lemma(value))


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

    A Cyrillic letter typed into a Georgian word is not a lookalike of anything around it: it is
    a stray letter a reader can see, and ``spelling_deltas`` names the word it is in. Counted
    here as well, the one slip was two findings, one of them quoting the whole paragraph.
    """
    out: list[str] = []
    if confusable_chars(_outside_georgian_words(left)) != confusable_chars(
        _outside_georgian_words(right)
    ):
        out.append("CMP.HOMOGLYPH")
    # counted, not collected: a second zero-width space where there was one is still a change
    if Counter(_INVISIBLE.findall(left)) != Counter(_INVISIBLE.findall(right)):
        out.append("CMP.INVISIBLE_CHARS")
    return out


def _outside_georgian_words(text: str) -> str:
    """``text`` without the Georgian words that have a stray letter of another alphabet in them."""
    out: list[str] = []
    pos = 0
    for word in _slips(text):
        out.append(text[pos : word.start])
        pos = word.end
    out.append(text[pos:])
    return " ".join(out)


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


#: What a text says once its layout is set aside: its words, its numbers with the separators
#: inside them, and the signs that carry meaning (the multiplication sign as its code point).
_MULTIPLY = chr(0x00D7)
_LAYOUT_TOKEN = re.compile(rf"\d+(?:[.,/:-]\d+)*|[^\W\d_]+|[%№#$€£₾§&+=<>*/{_MULTIPLY}]")


def _layout_key(text: str) -> str:
    """``text`` without its layout: spacing, quotes, dashes, sentence and list punctuation, and
    where one paragraph or table cell ends and the next begins.

    A table of numbered sections rewritten as numbered paragraphs ("1 | მომსახურება" →
    "1. მომსახურება"), a paragraph broken before its currency word, a full stop added: the same
    words and numbers in the same order, and nothing a reader needs to check. A separator
    between digits is kept - "12.5" is not "125" - and so are %, №, # and currency signs.
    """
    return " ".join(_LAYOUT_TOKEN.findall(normalize_confusables(text).lower()))


#: The year a period's dates take from after them: ", 2026 წელს".
_YEAR_OF_DATES = re.compile(r",?\s*(?<!\d)(\d{4})\s*(?:წელს|წელი|წ\.)(?![ა-ჰ])")


def _same_values_rewritten(left: str, right: str) -> bool:
    """True when the only difference is how the same values are written.

    "2026 წლის 31 მარტი", "31 მარტი, 2026 წ." and "31.03.2026" are one date; "2026 წლის 31
    დეკემბრამდე" and "31.12.2026-მდე" one date with one postposition; a period from the first
    of May to its last day and "2026 წლის მაისის თვეში" one period; "0.1%" and "0.1
    პროცენტის" one rate; "ლარს" and "ლ." one currency; "12 600 ლარი და 50 თეთრი" and "12
    600.50 ლარი" one amount. Each value is compared as the value it is and has not changed, and
    the words around them are the same: the line has nothing else to report
    (``_masked_values``).
    """
    left_masked, left_values = _masked_values(left, keyed=True)
    right_masked, right_values = _masked_values(right, keyed=True)
    return bool(left_values or right_values) and _layout_key(left_masked) == _layout_key(
        right_masked
    )


#: The stems of the Georgian month names, as the date reader knows them.
_KA_MONTH_STEMS = tuple(_KA_MONTHS)


@dataclass(frozen=True, slots=True)
class _Span:
    """Where a value stands in a text, and what it says: its canonical key."""

    start: int
    end: int
    key: str


#: What follows a date and belongs to it: the word for its year ("2026 წ."), a postposition
#: fused on with or without a hyphen ("31.12.2026-მდე", "01.05.2026-დან", "30.06.2026-ის") and
#: "ჩათვლით" (inclusive).
_DATE_TAIL = re.compile(
    r"(?:\s*(?:წლის|წელი|წ\.?)(?![ა-ჰ]))?"
    r"(?:-?(?P<post>იდან|დან|ამდე|მდე|ის|ს|ში|ზე|ით)(?![ა-ჰ]))?"
    r"(?P<incl>\s*\(?\s*ჩათვლით\s*\)?)?"
)


def _postposition(written: str, tail: re.Match[str] | None) -> str:
    """What a date's postposition says of it: "from" (-დან), "until" (-მდე, ჩათვლით) or nothing
    (a case ending). "2026 წლის 31 დეკემბრამდე" is fused into the month's name, "31.12.2026-მდე"
    written after the figures; the two are one date until one day. A year written after the
    date is its own and says nothing of the role ("30 ნოემბრის ჩათვლით, 2026 წ.", ``date_role``)."""
    role = date_role(written)
    if role is not None:
        return role
    post = (tail.group("post") or "") if tail else ""
    for word in re.findall(r"[ა-ჰ]+", written):
        if any(word.startswith(stem) for stem in _KA_MONTH_STEMS):
            post = word
    if post.endswith("დან"):
        return "from"
    if post.endswith("მდე") or (tail is not None and tail.group("incl")):
        return "until"
    return ""


def _span_key(text: str, hit: RuleHit) -> str:
    """A value's key for the masked text: everything a finding compares it by - its number or
    code, its unit, currency and the days it counts - and nothing of how it is written."""
    one = _canonical(_Hit(hit.type, hit.value, hit.normalized, hit.unit, hit.currency, "rules"))
    if hit.type in _BANK:
        return f"bank {_bank_key(hit.value)}"
    number = as_number(hit.value, hit.normalized) if hit.type in _NUMERIC else None
    value = f"{number:g}" if number is not None else str(one.normalized or one.value)
    return f"{value} {hit.unit or ''} {hit.currency or ''} {_basis(one) or ''}"


def _value_spans(text: str) -> list[_Span]:
    """Every value ``text`` states, where it stands, read by the readers ``hits_of`` uses: the
    rules' values with a period's two dates as one, a month named as a period, a quantity, the
    tax wording, and a penalty's "per day". Each date carries the year word, postposition and
    "ჩათვლით" written after it."""
    # imported here: contract_terms imports compare_llm, which imports this module
    from procurement_agent.checks.contract_terms import tax_phrases
    from procurement_agent.extraction.rules import _PER_PERIOD

    spans: list[_Span] = []
    rules = run_rules(text)
    i = 0
    while i < len(rules):
        r = rules[i]
        closing = _range_end(text, rules, i)
        if closing is not None:
            second = rules[closing[0]]
            tail = _DATE_TAIL.match(text, second.end)
            end = tail.end() if tail else second.end
            spans.append(_Span(r.start, end, f"period {r.normalized}/{second.normalized}"))
            i = closing[0] + 1
            continue
        if _ISO_DATE.match(str(r.normalized)):
            tail = _DATE_TAIL.match(text, r.end)
            post = _postposition(text[r.start : r.end], tail)
            end = tail.end() if tail else r.end
            spans.append(_Span(r.start, end, f"date {r.normalized} {post}"))
        else:
            spans.append(_Span(r.start, r.end, _span_key(text, r)))
            if r.type is FactType.penalty:
                stop = _CLAUSE_START.search(text, r.end)
                per = _PER_PERIOD.search(text, r.end, stop.start() if stop else len(text))
                if per is not None:
                    spans.append(_Span(per.start(), per.end(), "per"))
        i += 1
    spans.extend(_Span(a, b, f"period {span}") for a, b, span in _month_periods(text))
    for m in _QUANTITY.finditer(text):
        (hit,) = _quantity_hits(m.group(0)) or (None,)
        if hit is not None:
            spans.append(_Span(m.start(), m.end(), f"quantity {hit.normalized}"))
    spans.extend(_Span(*p.span, f"vat {p.kind}") for p in tax_phrases(text))
    out: list[_Span] = []
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if out and span.start < out[-1].end:
            continue  # inside a value already masked
        out.append(span)
    return out


def _masked_values(text: str, *, keyed: bool) -> tuple[str, list[_Span]]:
    """``text`` folded as ``hits_of`` folds it, with every value it states cut out: replaced by
    its key when ``keyed`` (two texts then read the same only where their values are the same),
    by a bare mark otherwise (what is left is the words around the values)."""
    folded = normalize_confusables(text)
    spans = _value_spans(folded)
    out: list[str] = []
    pos = 0
    for span in spans:
        out.append(folded[pos : span.start])
        out.append(f" §{span.key}§ " if keyed else " § ")
        pos = span.end
    out.append(folded[pos:])
    years = {m.group(0) for s in spans for m in re.finditer(r"\d{4}(?=-\d\d-\d\d)", s.key)}
    # the year a period's dates take from after it, "1 მაისიდან 31 მაისის ჩათვლით, 2026 წელს",
    # is theirs
    rest = _YEAR_OF_DATES.sub(lambda m: "" if m.group(1) in years else m.group(0), "".join(out))
    return rest, spans


_WORD_NUMBER_LABELS: dict[str, tuple[str, str]] = {
    "amount": ("Amount in words", "თანხა სიტყვიერად"),
    "duration": ("Period in words", "ვადა სიტყვიერად"),
    "count": ("Number in words", "რიცხვი სიტყვიერად"),
}
_SPELLING_LABEL = ("Spelling", "მართლწერა")
_PARENTHESIS = re.compile(r"\(([^()]{2,80})\)")
_CURRENCY_WORDS = re.compile(r"ლარ|თეთრ|დოლარ|ევრო|gel|usd|eur|\$|€", re.IGNORECASE)
_PERIOD_WORDS = re.compile(
    r"^(?:სამუშაო|კალენდარულ[ა-ჰ]*|საბანკო)?\s*(?:დღ|თვ|წ[ელ]|კვირ|საათ|day|month|year|week)",
    re.IGNORECASE,
)
_AFTER = 40


#: A letter: words still to come in the segment after a number in words.
_LETTER = re.compile(r"[^\W\d_]")


@dataclass(frozen=True, slots=True)
class _Context:
    """What a number ending its segment counts may be said beside the segment: in the header of
    its table column ("ღირებულება (ლარი)"), or at the start of the paragraph after it ("ლარს").

    A cell of a quantity column ("რაოდენობა") or a unit column ("ერთეული") also knows its row's
    other half: ``quantity`` and ``unit`` are the texts of the row's quantity and unit cells."""

    header: str = ""
    following: str = ""
    quantity: str = ""
    unit: str = ""


_NO_CONTEXT = _Context()

#: The header of a table column of quantities, and of the column naming their unit - not
#: "ერთეულის ფასი" (unit price), which is money.
_QUANTITY_HEADER = re.compile(r"რაოდენობ|(?<![a-z])(?:quantity|qty)(?![a-z])", re.IGNORECASE)
_UNIT_HEADER = re.compile(
    r"(?<![ა-ჰ])ერთეული(?![ა-ჰ])|(?<![ა-ჰ])ერთ\.|ზომის\s+ერთეულ"
    r"|(?<![a-z])(?:units?|uom)(?![a-z])(?!\s*(?:price|rate|cost))",
    re.IGNORECASE,
)


def _contexts(segments: Sequence[Segment]) -> dict[int, _Context]:
    """Each segment's context (``_Context``), keyed by ``id``: a table cell's column header and
    its row's quantity and unit, another segment's next paragraph."""
    rows: dict[tuple[int | None, int | None], dict[str, str]] = {}
    for segment in segments:
        header = segment.column or ""
        for role, pattern in (("quantity", _QUANTITY_HEADER), ("unit", _UNIT_HEADER)):
            if segment.table_row is not None and pattern.search(header):
                rows.setdefault((segment.table, segment.table_row), {}).setdefault(
                    role, segment.text
                )
    out: dict[int, _Context] = {}
    for k, segment in enumerate(segments):
        if segment.column is not None:
            row = rows.get((segment.table, segment.table_row), {})
            out[id(segment)] = _Context(
                header=segment.column,
                quantity=row.get("quantity", ""),
                unit=row.get("unit", ""),
            )
        elif k + 1 < len(segments) and segments[k + 1].table is None:
            out[id(segment)] = _Context(following=segments[k + 1].text[:_AFTER])
    return out


def _cell_quantity(text: str, context: _Context) -> _Hit | None:
    """The quantity a cell of a quantity or unit column states with the rest of its row: "20"
    under "რაოდენობა" beside "ცალი" under "ერთეული" is "20 item", read from either cell.

    Only a row whose unit cell is nothing but a unit of the closed list (``_UNITS``) and whose
    quantity cell is nothing but a figure: a quantity column beside a unit it cannot name is
    left to the figure's own reading.
    """
    if _QUANTITY_HEADER.search(context.header):
        figure, unit = text, context.unit
    elif _UNIT_HEADER.search(context.header):
        figure, unit = context.quantity, text
    else:
        return None
    if parse_number(_figure_alone(figure).strip()) is None:
        return None
    # the figure as written, with its words: "6 (ექვსი)" and a unit are read as the prose reads
    # "6 (ექვსი) ცალი", and the words that changed with the figure are that change's own
    written = f"{normalize_spaces(figure)} {normalize_spaces(unit)}"
    hits = _quantity_hits(written)
    if len(hits) != 1 or hits[0].value != written:
        return None
    return hits[0]


def _numeral_kind(inner: str, after: str, header: str = "") -> str:
    """What a number in words counts, from the words inside and right after it: money, a
    period of time, or anything else (copies, pages). A number that ends its table cell counts
    what its column's header says: "800 (რვაასი)" under "ღირებულება (ლარი)" is lari."""
    if _CURRENCY_WORDS.search(inner) or _CURRENCY_WORDS.match(after.strip()):
        return "amount"
    if _PERIOD_WORDS.match(after.strip()):
        return "duration"
    if header and _CURRENCY_WORDS.search(header) and not _LETTER.search(after):
        return "amount"
    return "count"


def _spelled_numbers(text: str, context: _Context = _NO_CONTEXT) -> list[tuple[str, str, str]]:
    """Every bracketed number in words, with what follows it and its column's header:
    [("ასი", "ლარს ...", ""), ...]. A bracket that ends its segment is followed by the start
    of the next paragraph: "800 (რვაასი)" on its own line before "ლარს" is lari.

    The bracket right after a figure holds that figure written out, so it is read as a number in
    words even misspelled out of recognition - "100 (აი)" - as long as it is a misspelling of
    the figure's own words: "დანართი 1 (ტექნიკური დავალება)" is not. Missed, such a typo was
    read as the words removed, and as the amount is still written beside it, not reported at all.
    """
    out: list[tuple[str, str, str]] = []
    for m in _PARENTHESIS.finditer(text):
        inner = m.group(1).strip()
        if (
            looks_like_numeral(inner)
            or _spelled_value(inner) is not None  # "ასი ლარი და 00 თეთრი": currency and tetri
            or _misspells_figure(text[: m.start()], inner)
        ):
            after = text[m.end() : m.end() + _AFTER]
            if context.following and not _LETTER.search(after):
                after = f"{after} {context.following}"
            out.append((inner, after, context.header))
    return out


_FIGURE_BEFORE = re.compile(rf"(?<![\d.,])({FIGURE})\s*$")


def _misspells_figure(before: str, words: str) -> bool:
    """Whether ``words`` are the figure that ends ``before``, written out with a typo."""
    m = _FIGURE_BEFORE.search(before[-60:])
    figure = parse_number(m.group(1)) if m else None
    if figure is None or figure < 1 or figure != int(figure):
        return False
    return number_words_typo(words, int(figure)) is not None


def _spelled_display(words: str, value: float) -> str:
    """A number in words as a finding shows it: "ორი ათას ოთხასი (2400)"."""
    shown = str(int(value)) if float(value).is_integer() else f"{value:.2f}"
    return f"{words} ({shown})"


def _spelled_value(inner: str) -> float | None:
    """What a bracket's words say, currency words and tetri read with them: "ასი ლარი და 00
    თეთრი" is 100, like "ასი" - restating an amount with its tetri changes nothing."""
    return read_words(inner)[1]


def word_amount_deltas(
    left: str,
    right: str,
    left_context: _Context = _NO_CONTEXT,
    right_context: _Context = _NO_CONTEXT,
) -> list[ValueDelta]:
    """Differences between the numbers the two texts spell out in brackets.

    Paired by content, not by position: a revision that misspelled "(ასი)" as "(ასოი)" used to
    pair every later bracket with the one before it and report "ასი (100) → ხუთი (5)" - two
    unrelated numbers. Aligned, the typo is one changed bracket, and it is reported as the
    misspelling it is. Each number is labelled by what it counts: a fee, a period of days, or a
    plain count - "5 (ხუთი) სამუშაო დღე" is no amount.
    """
    return [d for d, _read in _word_number_changes(left, right, left_context, right_context)]


#: A number in words that changed, and what it read as before and after when both sides read.
_WordChange = tuple[ValueDelta, tuple[float, float] | None]


def _word_number_changes(
    left: str,
    right: str,
    left_context: _Context = _NO_CONTEXT,
    right_context: _Context = _NO_CONTEXT,
) -> list[_WordChange]:
    lp, rp = _spelled_numbers(left, left_context), _spelled_numbers(right, right_context)
    if [x for x, *_ in lp] == [x for x, *_ in rp]:
        return []
    sm = difflib.SequenceMatcher(None, [x for x, *_ in lp], [x for x, *_ in rp], autojunk=False)
    out: list[_WordChange] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        ls, rs = lp[i1:i2], rp[j1:j2]
        for k in range(max(len(ls), len(rs))):
            d = _spelled_delta(ls[k] if k < len(ls) else None, rs[k] if k < len(rs) else None)
            if d is not None:
                out.append(d)
    return out


def _spelled_delta(
    a: tuple[str, str, str] | None, b: tuple[str, str, str] | None
) -> _WordChange | None:
    va = _spelled_value(a[0]) if a else None
    vb = _spelled_value(b[0]) if b else None
    kind = _numeral_kind(*(b or a))  # type: ignore[misc]
    en, ka = _WORD_NUMBER_LABELS[kind]
    severity: Severity = "error" if kind in ("amount", "duration") else "warning"
    if a is not None and b is not None:
        if va is not None and vb is not None:
            if va == vb:
                return None
            changed = ValueDelta(
                type=FactType.total_amount if kind == "amount" else FactType.duration,
                kind="changed",
                rule_id="CMP.AMOUNT_WORDS",
                severity=severity,
                label_en=en,
                label_ka=ka,
                left=_spelled_display(a[0], va),
                right=_spelled_display(b[0], vb),
                delta=float(vb - va),
                delta_pct=round((vb - va) / abs(va) * 100, 2) if va else None,
            )
            return changed, (va, vb)
        # one side does not read as a number: the same number misspelled, or a changed one
        known, other = (va, b[0]) if va is not None else (vb, a[0])
        if (
            known is not None
            and float(known).is_integer()
            and number_words_typo(other, int(known)) is not None
        ):
            if kind == "amount":
                # an amount's words are its operative statement: misspelled, they are wrong as
                # the amount is wrong - an error, as the act check reports it (GOLD_SPEC 3-4)
                misspelled = ValueDelta(
                    type=FactType.total_amount,
                    kind="changed",
                    rule_id="CMP.AMOUNT_WORDS",
                    severity="error",
                    label_en=en,
                    label_ka=ka,
                    left=a[0],
                    right=b[0],
                    note_en="the amount in words is misspelled",
                    note_ka="თანხა სიტყვიერად შეცდომითაა დაწერილი",
                )
                return misspelled, None
            typo = _spelling(
                a[0],
                b[0],
                note=("a number written out with a typo", "რიცხვი სიტყვიერად შეცდომით წერია"),
            )
            return typo, None
        unreadable = ValueDelta(
            type=FactType.total_amount if kind == "amount" else FactType.duration,
            kind="changed",
            rule_id="CMP.AMOUNT_WORDS",
            severity=severity,
            label_en=en,
            label_ka=ka,
            left=a[0] if va is None else _spelled_display(a[0], va),
            right=b[0] if vb is None else _spelled_display(b[0], vb),
        )
        return unreadable, None
    side = a or b
    value = va if a is not None else vb
    if side is None or value is None:
        return None
    gone_or_new = ValueDelta(
        type=FactType.total_amount if kind == "amount" else FactType.duration,
        kind="removed" if a is not None else "added",
        rule_id="CMP.AMOUNT_WORDS",
        severity=severity,
        label_en=en,
        label_ka=ka,
        left=_spelled_display(side[0], value) if a is not None else None,
        right=_spelled_display(side[0], value) if b is not None else None,
    )
    return gone_or_new, None


def _restated(read: tuple[float, float] | None, values: list[ValueDelta]) -> bool:
    """Whether a number in words that changed is its figure's change told a second time.

    "1 200 (ათას ორასი) ლარს" rewritten as "1 700 (ათას შვიდასი) ლარს" is one new fee, and
    the amount already says so; the words changed with the digits and still agree with them. A
    change of the words alone, or of a count no rule reads ("2 (ორი) ეგზემპლარად"), has no
    such twin and is reported on its own.
    """
    if read is None:
        return False
    before, after = read
    for d in values:
        if d.kind != "changed" or d.left is None or d.right is None:
            continue
        x, y = as_number(d.left), as_number(d.right)
        if x is not None and y is not None and amounts_equal(x, before) and amounts_equal(y, after):
            return True
    return False


def _spelling(left: str, right: str, *, note: tuple[str, str] | None = None) -> ValueDelta:
    en, ka = _SPELLING_LABEL
    return ValueDelta(
        type=FactType.other,
        kind="changed",
        rule_id="CMP.SPELLING",
        severity="warning",
        label_en=en,
        label_ka=ka,
        left=left,
        right=right,
        note_en=note[0] if note else None,
        note_ka=note[1] if note else None,
    )


def spelling_deltas(left: str, right: str) -> list[ValueDelta]:
    """Words that changed into a misspelling of themselves, or back: a letter of another
    alphabet inside a Georgian word ("მომიჯნავე" → "sმომიჯნავე"), a month name misspelled
    ("აგვისტოდან" → "აგისტოდან"). Without this such a line reads only as "text changed", and
    the reader has to find the one letter themselves.

    Only these two kinds: a word that became another word one letter apart may just be the same
    word in another case ("შემკვეთი" / "შემკვეთს"), which is not a spelling mistake.
    """
    lw = [w for w, _, _ in georgian_words(left)]
    rw = [w for w, _, _ in georgian_words(right)]
    out: list[ValueDelta] = []
    sm = difflib.SequenceMatcher(None, lw, rw, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace" or i2 - i1 != j2 - j1:
            continue
        for a, b in zip(lw[i1:i2], rw[j1:j2], strict=True):
            if a == b:
                continue
            if bool(mixed_script_words(a)) != bool(mixed_script_words(b)):
                out.append(
                    _spelling(
                        a,
                        b,
                        note=(
                            "a letter of another alphabet inside a word",
                            "სიტყვაში სხვა ანბანის ასოა",
                        ),
                    )
                )
                continue
            ta, tb = month_typo(a), month_typo(b)
            if (ta is None) != (tb is None) and (ta or tb) is not None:
                out.append(
                    _spelling(a, b, note=("a month name misspelled", "თვის დასახელება შეცდომითაა"))
                )
    return out


def _numeral_run(words: list[tuple[str, int, int]], i1: int, i2: int) -> tuple[int, int]:
    """Widen ``[i1, i2)`` to the whole numeral it is part of: "ოთხასი" in "ორი ათას ოთხასი"."""
    while i1 > 0 and parse_number_words(words[i1 - 1][0]) is not None:
        i1 -= 1
    while i2 < len(words) and parse_number_words(words[i2][0]) is not None:
        i2 += 1
    return i1, i2


def _in_brackets(text: str, pos: int) -> bool:
    """Whether ``pos`` sits inside a "( ... )" - those numbers are ``word_amount_deltas``'s."""
    opened = text.rfind("(", 0, pos)
    return opened >= 0 and text.rfind(")", opened, pos) < 0


def numeral_word_deltas(left: str, right: str) -> list[ValueDelta]:
    """A number written only in words that changed: "ხუთი სამუშაო დღის" → "ოცდაათი სამუშაო
    დღის". No digit moved, so the value rules see nothing, and a model asked about the line
    may call it immaterial - a payment term that went from five days to thirty.

    The whole numeral is compared, not the one word that changed in it, and a numeral inside
    brackets is left to ``word_amount_deltas``.
    """
    lw = georgian_words(left)
    rw = georgian_words(right)
    sm = difflib.SequenceMatcher(None, [w for w, _, _ in lw], [w for w, _, _ in rw], autojunk=False)
    out: list[ValueDelta] = []
    seen: set[tuple[int, int, int, int]] = set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace":
            continue
        i1, i2 = _numeral_run(lw, i1, i2)
        j1, j2 = _numeral_run(rw, j1, j2)
        if (i1, i2, j1, j2) in seen or i1 >= i2 or j1 >= j2:
            continue
        seen.add((i1, i2, j1, j2))
        if _in_brackets(left, lw[i1][1]) or _in_brackets(right, rw[j1][1]):
            continue
        a_words = " ".join(w for w, _, _ in lw[i1:i2])
        b_words = " ".join(w for w, _, _ in rw[j1:j2])
        va, vb = parse_number_words(a_words), parse_number_words(b_words)
        if va is None or vb is None or va == vb:
            continue
        a_end, b_end = lw[i2 - 1][2], rw[j2 - 1][2]
        kind = _numeral_kind(b_words, right[b_end : b_end + _AFTER])
        if kind == "count":
            kind = _numeral_kind(a_words, left[a_end : a_end + _AFTER])
        en, ka = _WORD_NUMBER_LABELS[kind]
        out.append(
            ValueDelta(
                type=FactType.total_amount if kind == "amount" else FactType.duration,
                kind="changed",
                rule_id="CMP.AMOUNT_WORDS",
                severity="error" if kind in ("amount", "duration") else "warning",
                label_en=en,
                label_ka=ka,
                left=f"{a_words} ({va})",
                right=f"{b_words} ({vb})",
                delta=float(vb - va),
                delta_pct=round((vb - va) / abs(va) * 100, 2) if va else None,
            )
        )
    return out


_BARE_NUMBER_LABEL = ("Number", "რიცხვი")
#: A figure and, in brackets after it, the figure written out: "800 (რვაასი)".
_FIGURE_WITH_WORDS = re.compile(rf"^\s*({FIGURE})\s*\(([^()]{{2,80}})\)\s*$")


def _figure_alone(text: str) -> str:
    """``text`` without the words its one figure is written out in, if that is all it is."""
    m = _FIGURE_WITH_WORDS.match(text)
    if m is None or not _spelled_numbers(text):
        return text
    return m.group(1)


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

    A figure written with its words, "800 (რვაასი)", is that figure: the cell a price is
    written in that way changed from 800 to 900 with its words left behind, and was only
    "text changed".
    """
    left, right = _figure_alone(left), _figure_alone(right)
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
    A name is one change in whichever case each line writes it (``_name_key``), a penalty
    whether its line says "0.1%" or its clause "0.1% per day", and a period whether a line
    reads it as two dates or the document-wide pass as the contract's performance window.
    """

    def side(value: str | None) -> str | None:
        if value is None:
            return None
        if delta.type is FactType.quantity:
            return normalize_name(value)
        if delta.type in _NUMERIC or delta.type is FactType.penalty:
            number = as_number(value)
            return _number_key(number, value) if number is not None else normalize_name(value)
        if delta.type in _NAMES:
            return _name_key(value)
        return normalize_name(value)

    return (_kind_of_change(delta), side(delta.left), side(delta.right))


def _kind_of_change(delta: ValueDelta) -> str:
    """A delta's rule, except that a period is a period whichever rule typed it: a line reads
    a contract's performance window as a range of dates, the document-wide pass as the
    execution period."""
    ends = [v for v in (delta.left, delta.right) if v is not None]
    return "period" if ends and all(_ISO_RANGE.match(v) for v in ends) else delta.rule_id


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


#: A clause's own number in front of it: "5.3.", "12)", "ა)".
_CLAUSE_NUMBER = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|\d+\)|[ა-ჰa-z]\))\s+", re.IGNORECASE)


def _unnumbered(text: str) -> str:
    """A line's key without its own clause number: "5.3. ..." and "6.3. ..." are one clause."""
    return key_of(_CLAUSE_NUMBER.sub("", text, count=1))


def _renumbered(left: Sequence[Segment], right: Sequence[Segment]) -> list[tuple[int, int]]:
    """The lines of a replace block that differ only in their clause number, as (i, j) in
    order: each the one line of its side with that text after the number, and long enough to
    say something."""
    lk = [_unnumbered(a.text) for a in left]
    rk = [_unnumbered(b.text) for b in right]
    count_l, count_r = Counter(lk), Counter(rk)
    where_r = {k: j for j, k in enumerate(rk) if count_r[k] == 1}
    pairs = [
        (i, where_r[k])
        for i, k in enumerate(lk)
        if count_l[k] == 1
        and k in where_r
        and len(k) >= _MOVE_MIN_CHARS
        and key_of(left[i].text) != key_of(right[where_r[k]].text)
    ]
    return _longest_increasing(pairs)


def _pair_gap(left: Sequence[Segment], right: Sequence[Segment]) -> list[_Pair]:
    if not left:
        return [(None, b, None) for b in right]
    if not right:
        return [(a, None, None) for a in left]
    return _pair_replace(left, right)


def _pair_replace(left: Sequence[Segment], right: Sequence[Segment]) -> list[_Pair]:
    """Re-pair a replace block so a 3-for-2 swap is not rendered as one undifferentiated blob.

    Lines that differ only in their clause number are paired first, in order, and the rest
    between them: an article inserted near the top renumbers every clause after it, and a
    contract of seventy clauses made one block too big to pair - reported whole as removed
    and added, the two versions looked unrelated and nothing between them was reported.
    """
    if len(left) == 1 and len(right) == 1:
        return [(left[0], right[0], None)]
    anchors = _renumbered(left, right)
    if anchors:
        paired: list[_Pair] = []
        i0 = j0 = 0
        for i, j in anchors:
            paired.extend(_pair_gap(left[i0:i], right[j0:j]))
            paired.append((left[i], right[j], None))
            i0, j0 = i + 1, j + 1
        paired.extend(_pair_gap(left[i0:], right[j0:]))
        return paired
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
    if any(s.table_row is not None for s in (*left, *right)):
        return _align_with_rows(left, right)
    pairs, ops = _align_segments(left, right)
    return pairs, _similarity_of(ops, pairs, len(left) + len(right))


def _align_segments(
    left: Sequence[Segment], right: Sequence[Segment]
) -> tuple[list[_Pair], list[_Opcode]]:
    """Segments aligned line by line: the pairs, and the opcodes they came from."""
    lk = [key_of(s.text) for s in left]
    rk = [key_of(s.text) for s in right]
    ops = _opcodes(lk, rk)
    # the lines each side has that the other side does not have where they stand: a line of one
    # side written, unchanged, among them on the other has moved
    unmatched_left = {
        lk[i] for tag, i1, i2, _j1, _j2 in ops if tag != "equal" for i in range(i1, i2)
    }
    unmatched_right = {
        rk[j] for tag, _i1, _i2, j1, j2 in ops if tag != "equal" for j in range(j1, j2)
    }
    pairs: list[_Pair] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            pairs.extend((a, b, None) for a, b in zip(left[i1:i2], right[j1:j2], strict=True))
        elif tag == "delete":
            pairs.extend((a, None, None) for a in left[i1:i2])
        elif tag == "insert":
            pairs.extend((None, b, None) for b in right[j1:j2])
        elif i2 - i1 + j2 - j1 > 2 and _laid_out_anew(left[i1:i2], right[j1:j2]):
            group = f"{_LAYOUT}{i1}"
            ls, rs = left[i1:i2], right[j1:j2]
            pairs.extend(
                (ls[k] if k < len(ls) else None, rs[k] if k < len(rs) else None, group)
                for k in range(max(len(ls), len(rs)))
            )
        else:
            pairs.extend(
                _pair_replace_with_moves(left[i1:i2], right[j1:j2], unmatched_left, unmatched_right)
            )
    return pairs, ops


#: The group of a table row added or removed whole: one change, not one per cell.
_ROW = "row:"

#: A unit of alignment: one segment, or the cells of one table row.
_Unit = tuple[Segment, ...]


def _units(segments: list[Segment]) -> list[_Unit]:
    """``segments`` with the cells of each table row taken together."""
    out: list[_Unit] = []
    for s in segments:
        last = out[-1][-1] if out else None
        if (
            s.table_row is not None
            and last is not None
            and last.table_row is not None
            and (last.table, last.table_row) == (s.table, s.table_row)
        ):
            out[-1] = (*out[-1], s)
        else:
            out.append((s,))
    return out


def _ordinal_columns(units: list[_Unit]) -> set[tuple[int | None, int | None]]:
    """(table, column) of each column that numbers its table's rows 1, 2, 3 ... - the "№",
    under a header or without one."""
    columns: dict[tuple[int | None, int | None], list[str]] = {}
    for unit in units:
        if unit[0].table_row is None:
            continue
        for cell in unit:
            # "1", "1." and "1)" alike
            number = cell.text.strip().rstrip(".)").strip()
            columns.setdefault((cell.table, cell.table_col), []).append(number)

    def counts(values: list[str]) -> bool:
        return len(values) >= 2 and values == [str(n) for n in range(1, len(values) + 1)]

    return {at for at, values in columns.items() if counts(values) or counts(values[1:])}


def _unit_key(unit: _Unit, ordinals: set[tuple[int | None, int | None]]) -> str:
    """A unit's alignment key. A row's is its words - not its figures, which a revision
    changes, nor the number it is in its table, which an inserted row changes for every row
    after it (the same reasoning as ``_row_key`` for a spreadsheet)."""
    if len(unit) == 1 and unit[0].table_row is None:
        return key_of(unit[0].text)
    words = [
        c.text
        for c in unit
        if (c.table, c.table_col) not in ordinals and parse_number(_figure_alone(c.text)) is None
    ]
    if words:
        return "\x00row:" + normalize_name(" ".join(words))
    return "\x00row#" + ",".join(str(c.table_col) for c in unit)


def _pair_rows(a: _Unit, b: _Unit, ordinals: set[tuple[int | None, int | None]]) -> list[_Pair]:
    """Two rows cell by cell, by column. A row number that moved because a row before it was
    added or removed is not a figure that changed: it is laid out anew."""
    if len(a) == 1 and len(b) == 1 and a[0].table_row is None and b[0].table_row is None:
        return [(a[0], b[0], None)]
    by_a: dict[int | None, list[Segment]] = {}
    by_b: dict[int | None, list[Segment]] = {}
    for c in a:
        by_a.setdefault(c.table_col, []).append(c)
    for c in b:
        by_b.setdefault(c.table_col, []).append(c)
    out: list[_Pair] = []
    for col in sorted(set(by_a) | set(by_b), key=lambda c: (c is None, c or 0)):
        xs, ys = by_a.get(col, []), by_b.get(col, [])
        if len(xs) != 1 or len(ys) != 1:
            # a cell of several paragraphs, or one the parser gave whole and as its paragraphs
            out.extend(_align_segments(xs, ys)[0])
            continue
        x, y = xs[0], ys[0]
        renumbered = (x.table, col) in ordinals and x.text.strip() != y.text.strip()
        out.append((x, y, f"{_LAYOUT}ord{x.index}" if renumbered else None))
    return out


def _without_cell_words(deltas: list[ValueDelta], cells: list[Segment]) -> list[ValueDelta]:
    """A row's deltas without the words a cell writes its own figure out in, "960 (ცხრაას
    სამოცი)": the row is what was added or removed, and the figure beside the words, a bare
    number, is no value found on its own either."""
    figures = {parse_number(alone) for c in cells if (alone := _figure_alone(c.text)) != c.text}
    return [
        d
        for d in deltas
        if not (
            d.rule_id == "CMP.AMOUNT_WORDS"
            and d.kind != "changed"
            and (shown := _SHOWN_FIGURE.search(d.left or d.right or "")) is not None
            and float(shown.group(1)) in figures
        )
    ]


def _one_side(unit: _Unit, *, left: bool) -> list[_Pair]:
    """A unit only one side has: a row whole, as one change."""
    if len(unit) == 1 or unit[0].table_row is None:
        return [(s, None, None) if left else (None, s, None) for s in unit]
    group = f"{_ROW}{'l' if left else 'r'}{unit[0].index}"
    return [(s, None, group) if left else (None, s, group) for s in unit]


def _align_with_rows(left: list[Segment], right: list[Segment]) -> tuple[list[_Pair], float]:
    """``_align_linear`` for documents with Word or PDF tables: a table row is aligned as a
    row, keyed by its words.

    Cells aligned one by one, a row inserted into a priced table was one "text added" per cell,
    and each row number after it - "4" paired with the "5" the next row became - a critical
    change of amount. Aligned as rows, the new row is one change and the renumbered rows are
    only laid out anew.
    """
    lu, ru = _units(left), _units(right)
    ordinals = _ordinal_columns(lu) & _ordinal_columns(ru)
    lk = [_unit_key(u, ordinals) for u in lu]
    rk = [_unit_key(u, ordinals) for u in ru]
    ops = _opcodes(lk, rk)
    unmatched_left = {
        key_of(s.text)
        for tag, i1, i2, _j1, _j2 in ops
        if tag != "equal"
        for u in lu[i1:i2]
        for s in u
    }
    unmatched_right = {
        key_of(s.text)
        for tag, _i1, _i2, j1, j2 in ops
        if tag != "equal"
        for u in ru[j1:j2]
        for s in u
    }
    moved = _moved_rows(lu, ru, lk, rk, ops)
    moved_to = {j: i for i, j in moved.items()}

    def from_row(i: int) -> list[_Pair]:
        return [(c, None, _moved_group(lu[i], ordinals)) for c in lu[i]]

    def to_row(j: int) -> list[_Pair]:
        return [(None, c, _moved_group(lu[moved_to[j]], ordinals)) for c in ru[j]]

    pairs: list[_Pair] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            for a, b in zip(lu[i1:i2], ru[j1:j2], strict=True):
                pairs.extend(_pair_rows(a, b, ordinals))
            continue
        if tag == "delete":
            for i in range(i1, i2):
                pairs.extend(from_row(i) if i in moved else _one_side(lu[i], left=True))
            continue
        if tag == "insert":
            for j in range(j1, j2):
                pairs.extend(to_row(j) if j in moved_to else _one_side(ru[j], left=False))
            continue
        # a row that moved is taken out first, as ``_pair_replace_with_moves`` takes a line out
        for i in range(i1, i2):
            if i in moved:
                pairs.extend(from_row(i))
        la = [lu[i] for i in range(i1, i2) if i not in moved]
        rb = [ru[j] for j in range(j1, j2) if j not in moved_to]
        pairs.extend(_replaced_rows(la, rb, ordinals, unmatched_left, unmatched_right))
        pairs.extend(p for j in range(j1, j2) if j in moved_to for p in to_row(j))
    total = len(left) + len(right)
    same = sum(
        1
        for a, b, group in pairs
        if a is not None and b is not None and group is None and key_of(a.text) == key_of(b.text)
    )
    # a row that only moved is the same text in both documents, cell for cell
    same += sum(
        1
        for i, j in moved.items()
        for a, b in zip(lu[i], ru[j], strict=False)
        if key_of(a.text) == key_of(b.text)
    )
    return pairs, _similarity_of([("equal", 0, same, 0, same)], pairs, total)


def _replaced_rows(
    la: list[_Unit],
    rb: list[_Unit],
    ordinals: set[tuple[int | None, int | None]],
    unmatched_left: set[str],
    unmatched_right: set[str],
) -> list[_Pair]:
    """The pairs of a block of units one document replaced with another's."""
    if not la or not rb:
        return [
            *(p for u in la for p in _one_side(u, left=True)),
            *(p for u in rb for p in _one_side(u, left=False)),
        ]
    if (
        len(la) == len(rb)
        and all(
            (x[0].table_row is None) == (y[0].table_row is None)
            for x, y in zip(la, rb, strict=True)
        )
        and all(x[0].table_row is not None for x in la)
    ):
        # the same rows with a word changed in each: still the rows they were
        return [p for a, b in zip(la, rb, strict=True) for p in _pair_rows(a, b, ordinals)]
    ls = [s for u in la for s in u]
    rs = [s for u in rb for s in u]
    if len(ls) + len(rs) > 2 and _laid_out_anew(ls, rs):
        group = f"{_LAYOUT}{ls[0].index}"
        return [
            (ls[k] if k < len(ls) else None, rs[k] if k < len(rs) else None, group)
            for k in range(max(len(ls), len(rs)))
        ]
    return _pair_replace_with_moves(ls, rs, unmatched_left, unmatched_right)


#: The group of a table row that moved: its cells where it stood, then where it stands now.
_MOVED_ROW = "moved-row:"


def _moved_group(unit: _Unit, ordinals: set[tuple[int | None, int | None]]) -> str:
    """The group both places of a moved row carry: the row, and which of its columns number
    the table's rows (a row number that moved with its row is laid out anew, not changed)."""
    columns = ",".join(str(c.table_col) for c in unit if (c.table, c.table_col) in ordinals)
    return f"{_MOVED_ROW}{unit[0].index}:{columns}"


def _moved_rows(
    lu: list[_Unit], ru: list[_Unit], lk: list[str], rk: list[str], ops: list[_Opcode]
) -> dict[int, int]:
    """Table rows that stand elsewhere in the other document, left unit -> right unit.

    Two rows swapped, or one moved to the end of its table, are a row removed where it stood
    and added where it stands: the same row, reported as two warnings, and if its price changed
    as well, as the row's text and never as the price. A row is found where it moved by its
    words (``_unit_key``) - only a key that one row alone carries in each document, and only
    a row with words: rows of nothing but numbers share a key and would be paired at random.
    """
    left_rows = [i for tag, i1, i2, _j1, _j2 in ops if tag != "equal" for i in range(i1, i2)]
    right_rows = [j for tag, _i1, _i2, j1, j2 in ops if tag != "equal" for j in range(j1, j2)]
    left_count, right_count = Counter(lk), Counter(rk)
    where = {
        rk[j]: j for j in right_rows if ru[j][0].table_row is not None and right_count[rk[j]] == 1
    }
    return {
        i: where[lk[i]]
        for i in left_rows
        if lu[i][0].table_row is not None
        and lk[i].startswith(f"\x00{_ROW}")
        and left_count[lk[i]] == 1
        and lk[i] in where
    }


def _pair_replace_with_moves(
    left: Sequence[Segment],
    right: Sequence[Segment],
    unmatched_left: set[str],
    unmatched_right: set[str],
) -> list[_Pair]:
    """``_pair_replace``, with the lines that moved taken out first.

    A list reordered - the same five items in another order - leaves a block where one item
    stands in for another. Paired as a rewrite, an item 0.3 alike to the one it replaced was
    reported "changed" into it, and as it was then no longer a line removed, the move it was
    part of was not seen: one item added, one rewritten, one removed, for a list that lost and
    gained nothing. A line whose text the other side has, unchanged, among its own unmatched
    lines is left unpaired here, for ``_detect_moves`` to pair.
    """
    moved_out = [a for a in left if _movable(a) and key_of(a.text) in unmatched_right]
    moved_in = [b for b in right if _movable(b) and key_of(b.text) in unmatched_left]
    if not moved_out and not moved_in:
        return _pair_replace(left, right)
    rest_left = [a for a in left if not any(a is m for m in moved_out)]
    rest_right = [b for b in right if not any(b is m for m in moved_in)]
    paired = _pair_replace(rest_left, rest_right) if rest_left and rest_right else []
    if not rest_right:
        paired = [(a, None, None) for a in rest_left]
    elif not rest_left:
        paired = [(None, b, None) for b in rest_right]
    return [*((a, None, None) for a in moved_out), *paired, *((None, b, None) for b in moved_in)]


#: The group of a block of lines that says the same as before, laid out anew.
_LAYOUT = "layout:"


def _laid_out_anew(left: Sequence[Segment], right: Sequence[Segment]) -> bool:
    """Whether a rewritten block is the same text in another layout.

    Docling reads a table of numbered sections as one line per cell, "1" and "მომსახურება";
    written as paragraphs the same rows are "1. მომსახურება". A paragraph broken in two before
    its currency word is two lines where there was one. Paired line by line, the first came out
    as twelve removed and rewritten lines, the second as a fee's words "added"; taken as a
    block, the words and numbers are the same in the same order (``_layout_key``). A character
    substitution is never layout, so hidden characters are checked first.
    """
    before = "\n".join(s.text for s in left)
    after = "\n".join(s.text for s in right)
    return not hidden_character_rules(before, after) and _layout_key(before) == _layout_key(after)


def _similarity_of(ops: list[_Opcode], pairs: list[_Pair], total: int) -> float:
    """How far the two documents are versions of one another, 0..1.

    Identical lines count whole, and a line paired with its rewritten counterpart counts by how
    alike the two are. Counting identical lines alone called a one-line document with one edit
    "unrelated" - and two unrelated documents get no line-to-line findings at all.
    """
    if not total:
        return 1.0
    same = sum(i2 - i1 for tag, i1, i2, _j1, _j2 in ops if tag == "equal")
    partial = 0.0
    for a, b, group in pairs:
        if group is not None and group.startswith(_LAYOUT):
            # the same text laid out anew counts whole, line for line on each side
            partial += 0.5 * ((a is not None) + (b is not None))
            continue
        if a is not None and b is not None and key_of(a.text) != key_of(b.text):
            partial += difflib.SequenceMatcher(
                None, key_of(a.text), key_of(b.text), autojunk=False
            ).ratio()
    return min(1.0, 2.0 * (same + partial) / total)


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


def _joined(segments: list[Segment]) -> SegmentRef | None:
    """One side of a block of lines, as the one reference its hunk carries."""
    if not segments:
        return None
    first = segments[0]
    return SegmentRef(
        index=first.index,
        text="\n".join(s.text for s in segments),
        kind=first.kind,
        path=first.path,
        heading=first.heading,
    )


def _pair_equal(a: Segment, b: Segment) -> bool:
    if a.kind == "sheet_cell" and b.kind == "sheet_cell":
        return _cells_equal(a, b)
    return key_of(a.text) == key_of(b.text) and a.formula == b.formula


def _rules_of(deltas: list[ValueDelta], *, base: str) -> list[str]:
    return [base, *sorted({d.rule_id for d in deltas})] if deltas else [base]


def _classify(
    a: Segment | None,
    b: Segment | None,
    *,
    ocr: bool,
    contexts: dict[int, _Context] | None = None,
) -> tuple[ChangeKind, Materiality, list[ValueDelta], list[str], list[InlineOp]]:
    contexts = contexts or {}
    ca = contexts.get(id(a), _NO_CONTEXT) if a is not None else _NO_CONTEXT
    cb = contexts.get(id(b), _NO_CONTEXT) if b is not None else _NO_CONTEXT
    if a is None:
        if b is None:
            raise ValueError("a diff pair has neither side")
        deltas = deltas_between("", b.text) + word_amount_deltas("", b.text, right_context=cb)
        mat: Materiality = "critical" if any(d.is_critical for d in deltas) else "substantive"
        return "added", mat, deltas, _rules_of(deltas, base="CMP.TEXT_ADDED"), []
    if b is None:
        deltas = deltas_between(a.text, "") + word_amount_deltas(a.text, "", left_context=ca)
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
    values = deltas_between(a.text, b.text)
    counted = _cell_quantity(a.text, ca), _cell_quantity(b.text, cb)
    quantity = _delta(*counted) if None not in counted else None
    # a quantity column's figure, or its row's unit, is the row's quantity - not a bare number
    bare = (
        [quantity]
        if quantity is not None
        else bare_number_delta(
            a.text,
            b.text,
            critical=a.kind != "sheet_cell",
            left_number=a.number,
            right_number=b.number,
        )
    )
    words = [
        d
        for d, read in _word_number_changes(a.text, b.text, ca, cb)
        if not _restated(read, values + bare)
    ]
    deltas = (
        values
        + words
        + numeral_word_deltas(a.text, b.text)
        + spelling_deltas(a.text, b.text)
        + bare
    )
    if deltas:
        mat = "critical" if any(d.is_critical for d in deltas) else "substantive"
        return "changed", mat, deltas, sorted({d.rule_id for d in deltas} | set(hidden)), inline
    if not hidden and (
        _numeric_reformat(a.text, b.text)
        or _layout_key(a.text) == _layout_key(b.text)
        or _layout_key(plain_party_wording(a.text)) == _layout_key(plain_party_wording(b.text))
        or _same_values_rewritten(a.text, b.text)
    ):
        return "changed", "cosmetic", [], ["CMP.FORMATTING"], inline
    return "changed", "substantive", [], sorted({*hidden, "CMP.TEXT_CHANGED"}), inline


def _build(
    pairs: list[_Pair],
    moves: dict[int, int],
    *,
    ocr: bool,
    contexts: dict[int, _Context] | None = None,
) -> tuple[list[DiffRow], list[Hunk], Counts]:
    partner: dict[int, int] = {}
    for i, j in moves.items():
        partner[i] = j
        partner[j] = i

    rows: list[DiffRow] = []
    hunks: list[Hunk] = []
    counts = Counts()
    hunk_of: dict[int, str] = {}
    # a moved row's hunk number, and the hunk of each of its cells that changed as it moved
    moved_rows: dict[str, int] = {}
    cell_hunk: dict[int, str] = {}
    made = 0
    i = 0
    while i < len(pairs):
        a, b, group = pairs[i]

        if group is not None and group.startswith(_LAYOUT):
            end = i
            while end < len(pairs) and pairs[end][2] == group:
                end += 1
            run = pairs[i:end]
            made += 1
            hid = f"h{made}"
            hunks.append(
                Hunk(
                    id=hid,
                    kind="changed",
                    materiality="cosmetic",
                    rules=["CMP.FORMATTING"],
                    left=_joined([ra for ra, _rb, _g in run if ra is not None]),
                    right=_joined([rb for _ra, rb, _g in run if rb is not None]),
                )
            )
            counts.cosmetic += 1
            counts.changed += 1
            for ra, rb, _g in run:
                shape: Literal["replace", "delete", "insert"] = (
                    "insert" if ra is None else "delete" if rb is None else "replace"
                )
                rows.append(DiffRow(tag=shape, left=_ref(ra), right=_ref(rb), hunk_id=hid))
            i = end
            continue

        if group is not None and group.startswith(_MOVED_ROW):
            end = i
            while end < len(pairs) and pairs[end][2] == group:
                end += 1
            if group not in moved_rows:
                moved_rows[group] = made + 1
                made = _moved_row_hunks(
                    group, pairs, made, hunks, counts, cell_hunk, ocr=ocr, contexts=contexts
                )
            for ra, rb, _g in pairs[i:end]:
                cell = rb if rb is not None else ra
                rows.append(
                    DiffRow(
                        tag="delete" if ra is not None else "insert",
                        left=_ref(ra),
                        right=_ref(rb),
                        hunk_id=cell_hunk.get(id(cell), f"h{moved_rows[group]}"),
                    )
                )
            i = end
            continue

        if group is not None and group.startswith(_ROW):
            end = i
            while end < len(pairs) and pairs[end][2] == group:
                end += 1
            run = pairs[i:end]
            cells = [c for ra, rb, _g in run for c in (ra, rb) if c is not None]
            # a cell the parser gave whole and as its paragraphs is written once
            lines = {key_of(c.text) for c in cells if c.kind != "table_cell"}
            written = [
                c
                for c in cells
                if c.kind != "table_cell"
                or not all(key_of(line) in lines for line in c.text.splitlines())
            ]
            whole = replace(cells[0], text=" | ".join(c.text for c in written or cells))
            added = run[0][0] is None
            kind, materiality, deltas, rules, inline = _classify(
                None if added else whole, whole if added else None, ocr=ocr, contexts=contexts
            )
            deltas = _without_cell_words(deltas, cells)
            if not deltas:
                materiality, rules = "substantive", [rules[0]]
            made += 1
            hid = f"h{made}"
            hunks.append(
                Hunk(
                    id=hid,
                    kind=kind,
                    materiality=materiality,
                    rules=rules,
                    left=None if added else _ref(whole),
                    right=_ref(whole) if added else None,
                    inline=inline,
                    deltas=deltas,
                )
            )
            setattr(counts, materiality, getattr(counts, materiality) + 1)
            setattr(counts, kind, getattr(counts, kind) + 1)
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

        kind, materiality, deltas, rules, inline = _classify(a, b, ocr=ocr, contexts=contexts)
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


def _moved_row_hunks(
    group: str,
    pairs: list[_Pair],
    made: int,
    hunks: list[Hunk],
    counts: Counts,
    cell_hunk: dict[int, str],
    *,
    ocr: bool,
    contexts: dict[int, _Context] | None,
) -> int:
    """The hunks of a table row that moved: the move, and each cell it changed on the way.

    Both places the row stands in carry ``group``; its cells are paired column by column as an
    aligned row's are (``_pair_rows``), so a price changed in a row that also moved is the
    price's change, and a row number that moved with its row is laid out anew. Returns the
    number of the last hunk made; each changed cell's hunk is recorded in ``cell_hunk`` under
    the id of its new cell (its old one when the column is gone).
    """
    left = tuple(a for a, _b, g in pairs if g == group and a is not None)
    right = tuple(b for _a, b, g in pairs if g == group and b is not None)
    columns = group.rsplit(":", 1)[-1]
    ordinals: set[tuple[int | None, int | None]] = {
        (left[0].table, int(c)) for c in columns.split(",") if c
    }
    whole = [replace(unit[0], text=" | ".join(c.text for c in unit)) for unit in (left, right)]
    made += 1
    hunks.append(
        Hunk(
            id=f"h{made}",
            kind="moved",
            materiality="cosmetic",
            rules=["CMP.MOVED"],
            left=_ref(whole[0]),
            right=_ref(whole[1]),
            moved_from=_row_path(left[0]),
            moved_to=_row_path(right[0]),
        )
    )
    counts.moved += 1
    for a, b, g in _pair_rows(left, right, ordinals):
        if g is not None or (a is not None and b is not None and _pair_equal(a, b)):
            continue  # a cell that moved with its row as it was, or its row number
        kind, materiality, deltas, rules, inline = _classify(a, b, ocr=ocr, contexts=contexts)
        made += 1
        hunks.append(
            Hunk(
                id=f"h{made}",
                kind=kind,
                materiality=materiality,
                rules=rules,
                left=_ref(a),
                right=_ref(b),
                inline=inline,
                deltas=deltas,
            )
        )
        setattr(counts, materiality, getattr(counts, materiality) + 1)
        setattr(counts, kind, getattr(counts, kind) + 1)
        cell = b if b is not None else a
        cell_hunk[id(cell)] = f"h{made}"
    return made


def _row_path(cell: Segment) -> str:
    """Where a table row stands: its first cell's path without the column, "T1/r3"."""
    return re.sub(r"/c\d+$", "", cell.path)


# --- findings -------------------------------------------------------------------------------


#: What changed in a line: each run of words that changed, as (before, after).
_Change = tuple[tuple[str, str], ...]


def _changed_words(left: str, right: str) -> _Change:
    lt, rt = _TOKEN.findall(left), _TOKEN.findall(right)
    return tuple(
        (normalize_spaces("".join(lt[i1:i2])), normalize_spaces("".join(rt[j1:j2])))
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, lt, rt, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    )


def _nested(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """Whether one changed line is, on both sides, one of the other's own lines: a table cell
    and one of its paragraphs.

    A line quoted inside another is not enough. An annex's heading "დანართი №2" and the clause
    that cites "დანართი №2-ში", both renumbered, are two changes a reader must see; matched as
    substrings, the clause's was dropped.
    """

    def within(part: tuple[str, str], whole: tuple[str, str]) -> bool:
        return all(
            normalize_spaces(p) in {normalize_spaces(line) for line in w.splitlines()}
            for p, w in zip(part, whole, strict=True)
        )

    return within(a, b) or within(b, a)


#: Unchanged words quoted either side of a change in a "text changed" finding.
_CONTEXT_WORDS = 3


def changed_passage(left: str, right: str) -> tuple[str, str]:
    """The part of a changed line that changed, on each side, with a few unchanged words either
    side and "…" where the line goes on: "… საბანკო ანგარიშზე უნაღდო …". A finding that quoted
    the whole paragraph, cut at 160 characters, showed two identical texts when the change lay
    further on. The whole line when nothing but spacing changed."""
    lt, rt = _TOKEN.findall(left), _TOKEN.findall(right)
    changed = [
        (i1, i2, j1, j2)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, lt, rt, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    ]
    if not changed:
        return left, right
    (i1, _, j1, _), (_, i2, _, j2) = changed[0], changed[-1]
    return _around(lt, i1, i2), _around(rt, j1, j2)


def _around(tokens: list[str], start: int, end: int) -> str:
    """``tokens[start:end]`` with ``_CONTEXT_WORDS`` words either side."""
    words = 0
    while start > 0 and words < _CONTEXT_WORDS:
        start -= 1
        words += tokens[start][0].isalnum()
    words = 0
    while end < len(tokens) and words < _CONTEXT_WORDS:
        words += tokens[end][0].isalnum()
        end += 1
    text = normalize_spaces("".join(tokens[start:end]))
    return f"{'… ' if start > 0 else ''}{text}{' …' if end < len(tokens) else ''}"


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
    hunks: list[Hunk],
    field_deltas: list[ValueDelta],
    right_text: str,
    *,
    ocr: bool,
    left_text: str = "",
    unrelated: bool = False,
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
    everywhere = _value_keys(left_text), _value_keys(right_text)
    # an act against the contract it closes: two kinds of document, however alike their words
    from procurement_agent.checks.document_facts import is_act  # checks import compare

    # read from the text folded as ``document_facts`` reads it: an act titled with an en dash
    # between "მიღება" and "ჩაბარების" is an act to both passes, or none of its values is
    # graded right
    cross_kind = bool(left_text and right_text) and is_act(
        normalize_confusables(left_text)
    ) != is_act(normalize_confusables(right_text))

    def _one_sided(d: ValueDelta) -> Severity:
        """Across two documents of different kinds - a contract and its act - a value only one
        of them states is what each is for (the act's own date, the contract's penalties):
        worth a look, not an error. A value both state differently stays the error it is."""
        if (unrelated or cross_kind) and d.kind != "changed" and d.severity == "error":
            return "warning"
        return d.severity

    written = _Written(left_text), _Written(right_text)
    # Only a change that is reported can make another report of it superfluous. Two documents
    # that are not versions of one another report no line's change: their positional diff pairs
    # the contract's party lines with the act's, and its "director X -> Y" was dropping the
    # document-wide "X removed" and "Y added" - for both parties, nothing was listed.
    all_deltas = [d for h in hunks if not unrelated for d in h.deltas] + list(field_deltas)
    changed_sides = {
        (_kind_of_change(d), side)
        for d in all_deltas
        if d.kind == "changed"
        for side in (_display_value(d.type, d.left), _display_value(d.type, d.right))
    }

    def superfluous(d: ValueDelta) -> bool:
        """An addition or removal that is not one: the value is written in the other document
        too (a paragraph moved with an edit), or it is the new or old side of a change already
        reported ("director X → Y" makes "director Y added" a second report of one change).

        "Written" is read in the other document's words as well as in the values its rules
        found there: without their quotes, "შპს ნიმუში" and its director are no longer found as
        a party by the rules, and were reported removed while the document still names them.
        """
        if d.kind == "changed":
            return False
        value = d.right if d.kind == "added" else d.left
        other = 0 if d.kind == "added" else 1
        if _value_key_of(d.type, value) in everywhere[other]:
            return True
        if value is not None and written[other].states(d, value):
            return True
        if _retells(d, all_deltas):
            return True
        return (_kind_of_change(d), _display_value(d.type, value)) in changed_sides

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
        quote: str | None = None,
    ) -> None:
        """``full`` is the untruncated (old, new) text behind a shortened expected/actual.

        Identity and anchoring use the full text; only the display fields are shortened. Two
        long lines that share their first 160 characters are two changes, and a comment can
        only be anchored on words the document actually contains - not on "…". ``quote`` is
        the line a value finding was read on: a value stated in canonical form ("0.2% per day")
        is written nowhere as such, and anchored on nothing the finding pointed nowhere.
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
        span: tuple[int, int] | None = None
        if quote is not None and full is None:
            # a value read on a line: anchored on the value itself where the line writes it as
            # reported (an IBAN, a code), on the line where it does not ("0.2% per day")
            at = right_text.find(quote)
            inside = right_text.find(new, at, at + len(quote)) if at >= 0 and new else -1
            if inside >= 0:
                span, quote = (inside, inside + len(new or "")), None
            elif at >= 0:
                span = (at, at + len(quote))
        else:
            if quote is None and full is not None:
                quote = new or old
            at = right_text.find(new) if new else -1
            if at >= 0:
                span = (at, at + len(new or ""))
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

    # the text changes reported so far, each with the words it changed
    text_changes: list[tuple[_Change, tuple[str, str]]] = []

    for h in hunks if not unrelated else []:
        # Two documents that are not versions of one another have no line to line changes worth
        # a finding: their positional diff is noise, and a value both carry was reported
        # "removed" on one line and "added" on another. Only the field differences speak then.
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
            shown = [_as_period(d, field_deltas) for d in h.deltas if not superfluous(d)]
            line = h.right or h.left
            for d in shown:
                ka, en = _delta_message(d)
                add(
                    d.rule_id,
                    _one_sided(d),
                    ka,
                    en,
                    expected=d.left,
                    actual=d.right,
                    key=dedup_key(d),
                    quote=line.text if line is not None else None,
                )
            if shown and not _says_more(h, shown, whole=len(shown) == len(h.deltas)):
                continue
            if not shown and _rewrapped(h, written):
                continue
            # The line changed words its values do not account for - the party a penalty falls
            # on, what a price leaves out - or every value on it is written in the other
            # document too while its words are not: the text change itself is reported as well
            # (a paragraph removed whose amount is restated elsewhere is a paragraph removed).
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
        if rule == "CMP.TEXT_CHANGED" and _explained_elsewhere(h, all_deltas):
            continue
        ka, en = _TEXT_MESSAGES[rule]
        anchor = h.right or h.left
        where = anchor.path if anchor is not None else ""
        before, after = h.left.text if h.left else None, h.right.text if h.right else None
        if before is not None and after is not None:
            change = _changed_words(before, after)
            if any(
                change == other and _nested((before, after), lines) for other, lines in text_changes
            ):
                continue  # a table cell and its own paragraph, both emitted by the parser
            text_changes.append((change, (before, after)))
            before, after = changed_passage(before, after)
        add(
            rule,
            "warning",
            f"{ka} ({where})",
            f"{en} ({where})",
            expected=_short(before),
            actual=_short(after),
            full=texts(h),
        )

    for d in field_deltas:
        if superfluous(d):
            continue
        ka, en = _delta_message(d)
        add(d.rule_id, _one_sided(d), ka, en, expected=d.left, actual=d.right, key=dedup_key(d))
    return out, suppressed


_PERIODS = frozenset({FactType.execution_period, FactType.validity_period})


def _as_period(d: ValueDelta, field_deltas: list[ValueDelta]) -> ValueDelta:
    """A line's change of one end of a period, as the change of the period the document-wide
    pass states.

    "მოქმედებს 2026 წლის 31 დეკემბრამდე" on its own line reads as the validity's end date; the
    document-wide pass reads the whole term, from signing to that date. Moved a month, the one
    term was two findings, "2026-12-31 → 2026-11-30" and "2026-02-12/2026-12-31 →
    2026-02-12/2026-11-30". The period is what changed, and is what is reported.
    """
    if d.type not in _PERIODS or any(v is not None and "/" in v for v in (d.left, d.right)):
        return d

    def ends(value: str | None) -> set[str]:
        return set(value.split("/")) if value is not None else set()

    for f in field_deltas:
        if f.type is not d.type:
            continue
        before, after = ends(f.left), ends(f.right)
        if d.kind == "changed":
            same = d.left in before and d.right in after
        elif d.kind == "removed":  # the end this line stated is gone from the period
            same = d.left in before and d.left not in after
        else:
            same = d.right in after and d.right not in before
        if same:
            return f
    return d


#: The figure a number in words is shown with: "ოთხი ათას რვაასი (4800)".
_SHOWN_FIGURE = re.compile(r"\((\d+(?:\.\d+)?)\)\s*$")
#: The count of an ISO duration: "P7D working" counts 7.
_ISO_COUNT = re.compile(r"^P(\d+)[DWMY]\b")
#: Which reported changes a number in words can retell: an amount's words its amount, a
#: period's or a count's words a period, a payment term or a quantity.
_RETOLD_BY: dict[FactType, frozenset[str]] = {
    FactType.total_amount: frozenset({"CMP.AMOUNT"}),
    FactType.duration: frozenset({"CMP.DURATION", "CMP.PAYMENT", "CMP.QUANTITY"}),
}


def _figure(value: str | None) -> float | None:
    """The number a reported value states: an amount, a count, the length of a period."""
    if value is None or _ISO_RANGE.match(value):
        return None
    m = _ISO_COUNT.match(value)
    return float(m.group(1)) if m else as_number(value)


def _retells(d: ValueDelta, deltas: list[ValueDelta]) -> bool:
    """Whether a number in words removed or added is the old or the new side of a figure's
    change reported anyway - ``_restated`` for words the diff could not pair.

    A fee clause moved to another article and raised, digits and words together, is a line
    removed and a line added; the amount's change is reported from both, and its old words
    "removed" and its new words "added" were two more errors for the one new fee.
    """
    if d.rule_id != "CMP.AMOUNT_WORDS" or d.kind == "changed":
        return False
    shown = _SHOWN_FIGURE.search((d.left if d.kind == "removed" else d.right) or "")
    rules = _RETOLD_BY.get(d.type)
    if shown is None or rules is None:
        return False
    number = float(shown.group(1))
    for other in deltas:
        if other.kind != "changed" or other.rule_id not in rules:
            continue
        figure = _figure(other.left if d.kind == "removed" else other.right)
        if figure is not None and amounts_equal(figure, number):
            return True
    return False


def _explained_elsewhere(hunk: Hunk, deltas: list[ValueDelta]) -> bool:
    """Whether every word this line changed is part of a change reported for another line.

    A director's name typed wrong in the preamble and again in the signature table is one
    change: the preamble's line names it ("Director / signatory: X → Y"), and the table cell,
    where no label stands in front of the name, would otherwise add a bare "text changed".
    The name is matched in any case form, as the finding states it in the nominative.

    Only a change this line states whole explains it: the other change's old value written in
    this line's old text and its new value in the new text, word for word. A fragment is not
    enough - an annex number "2" → "3" is no part of a date that moved from the 12th of
    February to the 12th of March, and a court "in Tbilisi" is no part of an address that moved
    from Tbilisi; matched as substrings, both lines vanished from the findings.
    """
    if hunk.left is None or hunk.right is None:
        return False
    changed = [
        (_stem_words(op.left), _stem_words(op.right))
        for op in hunk.inline
        if op.tag != "equal" and (op.left.strip() or op.right.strip())
    ]
    if not changed:
        return False
    before, after = _stem_words(hunk.left.text), _stem_words(hunk.right.text)
    stated = [
        (left, right)
        for d in deltas
        if d.kind == "changed"
        for left, right in [(_stem_words(d.left or ""), _stem_words(d.right or ""))]
        if left and right and left <= before and right <= after
    ]
    return all(any(old <= left and new <= right for left, right in stated) for old, new in changed)


def _stem_words(text: str) -> frozenset[str]:
    """The words of ``text`` in any case form: "ჭიჭინაძის" and "ჭიჭინაძე" are one word."""
    return frozenset(name_stem(w) for w in _WORD.findall(normalize_name(text)))


_ROLES_LABEL = ("Party roles", "მხარეთა როლები")
#: The word that opens a party's definition of its side: "(შემდგომში „შემკვეთი“)".
_DEFINITION = "შემდგომში"

#: One party whose defined side changed: (name or code, side before, side after).
_Reassigned = tuple[str, str, str]


def _sides_reassigned(left_text: str, right_text: str) -> list[_Reassigned]:
    """The parties that now hold a side another party held: the customer defined as the
    contractor, and back.

    Each party is defined with its side - "(შემდგომში „შემკვეთი“)" - and a revision that swaps
    the two words changes who pays whom while every name, code and account stays where it was.
    Only a side another party held before counts: a side renamed with a synonym ("მიმწოდებელი"
    for "შემსრულებელი") is wording, and stays the text change it is.
    """
    before = {p.key: p for p in find_parties(left_text) if p.role_source == "defined" and p.role}
    after = {p.key: p for p in find_parties(right_text) if p.role_source == "defined" and p.role}
    out: list[_Reassigned] = []
    for key, p in before.items():
        q = after.get(key)
        if q is None or q.role == p.role:
            continue
        if any(o.role == q.role for other, o in before.items() if other != key):
            out.append((p.name or key, p.role or "", q.role or ""))
    return out


def _role_delta(roles: list[_Reassigned]) -> ValueDelta:
    """The reassignment as one critical change, naming the side each party now holds.

    Line by line a swap read as two "text changed" lines about a word each, and a reader had to
    work out from them that the customer and the contractor had traded places.
    """
    swapped = len(roles) > 1 and {old for _, old, _ in roles} == {new for _, _, new in roles}
    note = (
        ("the parties swapped sides", "მხარეებმა როლები გაცვალეს")
        if swapped
        else ("a party now holds another party's side", "მხარეს სხვა მხარის როლი მიენიჭა")
    )
    en, ka = _ROLES_LABEL
    return ValueDelta(
        type=FactType.party_name,
        kind="changed",
        rule_id="CMP.PARTY",
        severity="error",
        label_en=en,
        label_ka=ka,
        left="; ".join(f"{name} — „{old}“" for name, old, _ in roles),
        right="; ".join(f"{name} — „{new}“" for name, _, new in roles),
        note_en=note[0],
        note_ka=note[1],
    )


def _named_dates(hunks: list[Hunk], field_deltas: list[ValueDelta]) -> list[Hunk]:
    """A line's date that no word names, which the document-wide reading knows as the
    contract's or the act's own ("ქ. თბილისი, 2026 წლის 12 თებერვალი"), carries that name."""
    named = {dedup_key(d): d for d in field_deltas if d.type in _DOCUMENT_DATES}
    if not named:
        return hunks
    out: list[Hunk] = []
    for h in hunks:
        deltas = [named.get(dedup_key(d), d) if d.type is FactType.date else d for d in h.deltas]
        out.append(h.model_copy(update={"deltas": deltas}) if deltas != h.deltas else h)
    return out


def _carry_role_change(
    hunks: list[Hunk], counts: Counts, change: ValueDelta, roles: list[_Reassigned]
) -> list[Hunk]:
    """The definitions whose side word changed carry the role change as their value, so each
    line says what changed on it and the change is reported once."""
    words = {(normalize_name(old), normalize_name(new)) for _, old, new in roles}
    out: list[Hunk] = []
    for h in hunks:
        carries = (
            h.kind == "changed"
            and h.right is not None
            and _DEFINITION in h.right.text
            and any(
                op.tag != "equal" and (normalize_name(op.left), normalize_name(op.right)) in words
                for op in h.inline
            )
        )
        if not carries:
            out.append(h)
            continue
        setattr(counts, h.materiality, getattr(counts, h.materiality) - 1)
        counts.critical += 1
        rules = {r for r in h.rules if r != "CMP.TEXT_CHANGED"} | {change.rule_id}
        out.append(
            h.model_copy(
                update={
                    "deltas": [*h.deltas, change],
                    "rules": sorted(rules),
                    "materiality": "critical",
                }
            )
        )
    return out


def _value_key_of(ft: FactType, value: str | None) -> tuple[FactType, str] | None:
    if value is None:
        return None
    return (_family_of(ft), _display_value(ft, value) or "")


def _family_of(ft: FactType) -> FactType:
    """One representative per relabelling family, so a key survives a changed label."""
    for fam in _RELABEL_FAMILIES:
        if ft in fam:
            return min(fam, key=lambda t: t.value)
    return ft


#: Values that are words or codes, not quantities: a document states one when its text says it,
#: in any case form, with or without quotes. A number is never looked for this way - "5" is
#: written somewhere in every document.
_WRITTEN_TYPES = frozenset(
    {
        FactType.party_name,
        FactType.party_director,
        FactType.party_address,
        FactType.project_name,
        FactType.party_id_code,
        FactType.id_code,
        FactType.bank_account,
        FactType.bank_code,
        FactType.cadastral_code,
        FactType.contract_number,
    }
)
_WORD = re.compile(r"\w+")


class _Written:
    """What a document says in so many words, read only when a finding asks."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._stems: str | None = None
        self._spelled: set[str] | None = None
        self._words: set[str] | None = None

    def states(self, delta: ValueDelta, value: str) -> bool:
        if delta.type in _WRITTEN_TYPES:
            key = _stems(value)
            return bool(key.strip()) and key in self.stems
        if delta.rule_id == "CMP.AMOUNT_WORDS":
            return value in self.spelled
        return False

    @property
    def stems(self) -> str:
        if self._stems is None:
            self._stems = _stems(self._text)
        return self._stems

    @property
    def words(self) -> set[str]:
        if self._words is None:
            self._words = _words_of(self._text)
        return self._words

    @property
    def spelled(self) -> set[str]:
        if self._spelled is None:
            self._spelled = {
                _spelled_display(words, value)
                for words, *_ in _spelled_numbers(self._text)
                if (value := parse_number_words(words)) is not None
            }
        return self._spelled


def _words_of(text: str) -> set[str]:
    """The words and numbers of ``text`` as ``_layout_key`` reads them, a lone letter ("ს" of
    "ს/კ") left out."""
    return {w for w in _layout_key(text).split() if len(w) > 1 or w.isdigit()}


def _rewrapped(hunk: Hunk, written: tuple[_Written, _Written]) -> bool:
    """Whether a line whose values are all written in the other document says nothing new.

    Every word it lost is still written in the newer document and every word it gained was
    already in the older one: the text was re-flowed around a change reported on its own line
    ("(ს/კ: 01.02.03.456)" cut out of a line that then merged with the next), split, or read
    twice by the layout - a table cell whose two paragraphs come out once each and once as the
    cell. A line that lost words the newer document no longer has is reported as the text
    change it is.
    """
    lost: set[str] = set()
    gained: set[str] = set()
    if hunk.kind == "removed" and hunk.left is not None:
        lost = _words_of(hunk.left.text)
    elif hunk.kind == "added" and hunk.right is not None:
        gained = _words_of(hunk.right.text)
    else:
        for op in hunk.inline:
            if op.tag in ("delete", "replace"):
                lost |= _words_of(op.left)
            if op.tag in ("insert", "replace"):
                gained |= _words_of(op.right)
    return lost <= written[1].words and gained <= written[0].words


def _says_more(hunk: Hunk, shown: list[ValueDelta], *, whole: bool) -> bool:
    """Whether a line that carries a reported value change also changed words that change says
    nothing of.

    A line removed or added whole (one side only) is its values: a deleted penalty clause is the
    penalty removed, one finding. It says more only when a value on it went unreported
    (``whole`` false) because the other document restates it - the paragraph removed while its
    amount is written elsewhere is still a paragraph removed. A line changed in place is read
    word by word, below.

    "მიმწოდებელს ეკისრება პირგასამტეხლო ... 0.1%" rewritten "შემსყიდველს ... 0.2%" is a new
    rate, and a penalty that now falls on the buyer; a price raised and newly excluding
    transport, a warranty shortened and newly excluding a component, are two changes each. With
    the value reported, the line's own text change was dropped, and the party that bears a
    penalty could flip with no finding saying so.

    Each side is read with its values cut out (``_masked_values``: a figure with its words,
    currency and unit, a date with its postposition, a rate with its "per day", the tax
    wording). A word changed outside them counts unless it is one of the reported values' own
    words, in whatever case the line writes it - a director's name, an address, a misspelled
    word, or the words a canonical term was written in (its note). A figure and its words
    changed together, and nothing else, say nothing more.
    """
    if hunk.left is None or hunk.right is None:
        return not whole
    left = _masked_values(hunk.left.text, keyed=False)[0] if hunk.left else ""
    right = _masked_values(hunk.right.text, keyed=False)[0] if hunk.right else ""
    lt, rt = _layout_key(left).split(), _layout_key(right).split()
    told = {
        name_stem(w)
        for d in shown
        for text in (d.left, d.right, d.note_en)
        for w in _WORD.findall(normalize_name(text or ""))
    }
    stems = tuple(s for s in told if len(s) >= _TOLD_STEM)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, lt, rt, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for token in (*lt[i1:i2], *rt[j1:j2]):
            if not _is_word(token) or name_stem(token) in told:
                continue
            # a value's word in a case the stemmer does not undo: "კოშკისთვის" is the project
            # "კოშკი" reported in the nominative
            if any(token.startswith(stem) for stem in stems):
                continue
            return True
    return False


#: The shortest stem of a reported value's word that the line's word may extend by a case ending.
_TOLD_STEM = 4


def _is_word(token: str) -> bool:
    """A layout token that is a word of two letters or more - not a figure, a sign or the mark
    a value was cut out for."""
    return len(token) > 1 and token.isalpha()


def _stems(text: str) -> str:
    """``text`` as a line of word stems between spaces - quotes, punctuation and case endings
    gone - so a name is found in whichever form and quotes a document writes it."""
    return " " + " ".join(name_stem(w) for w in _WORD.findall(normalize_name(text))) + " "


def _value_keys(text: str) -> set[tuple[FactType, str] | None]:
    """Every typed value a document states anywhere, keyed like ``_value_key_of``: a period
    both as the range and as its two dates, which is how the other document may name it."""
    if not text:
        return set()
    hits = hits_of(text)
    return {
        _value_key_of(h.type, _as_reported(h) if h.type in _CANONICAL else h.value)
        for h in hits + _endpoints(hits)
    }


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
    contexts = {} if spreadsheets else {**_contexts(left), **_contexts(right)}
    rows, hunks, counts = _build(pairs, moves, ocr=ocr, contexts=contexts)

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
    elif l_sheet or r_sheet:
        # a workbook states no term, period or penalty clause to read as a whole
        field_deltas = deltas_between(left_text, right_text, types=FIELD_TYPES)
    else:
        field_deltas, notes = document_deltas(left_text, right_text)
        skipped.extend(notes)
        hunks = _named_dates(hunks, field_deltas)
        roles = _sides_reassigned(left_text, right_text)
        if roles:
            change = _role_delta(roles)
            hunks = _carry_role_change(hunks, counts, change, roles)
            field_deltas.append(change)

    findings, suppressed = _findings_of(
        hunks,
        field_deltas,
        right_text,
        ocr=ocr,
        left_text=left_text,
        unrelated=alignment == "unrelated",
    )
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
