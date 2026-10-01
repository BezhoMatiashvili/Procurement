"""Facts a document states as a whole, read by the readers the AOA check uses.

The extraction pipeline reads a fact where it finds one: a date, the keyword nearest to it,
a type. That is right for a price in a table and wrong for what a document says in a clause:
its term, when the work was done, how and when it is paid, whether the price includes VAT, what
it is about. Read chunk by chunk, a service period came out as two unrelated dates, the end of a
contract's performance window as a second "contract date", "5 working days to pay" as an
unclassified duration, and VAT and the subject not at all - five fields at 0% while
``contract_terms`` already read every one of them for the acceptance check.

Here the whole document is read once, by those same readers, and each fact is stated in the
canonical form ``docs/GOLD_SPEC.md`` fixes: ranges as ``start/end``, payment deadlines as
``P5D working``, VAT status as ``incl``/``excl``/``net``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# the period's words, re-exported where the comparison reads them (``compare``): one definition,
# the period reader's own (``AoaChecker._period_hits``)
from procurement_agent.checks.aoa import _MONTH_PERIOD as _MONTH_PERIOD
from procurement_agent.checks.aoa import _SERVICE_WORDS as _SERVICE_WORDS
from procurement_agent.checks.aoa import (
    AoaChecker,
    DocumentView,
    contract_named_by,
)
from procurement_agent.checks.aoa_scope import _SUBJECT_MAX, _SUBJECT_START, subject_clause
from procurement_agent.checks.contract_terms import (
    _FROM_SIGNING,
    _NOT_A_TERM,
    _PAYMENT,
    RoledAmount,
    TaxPhrase,
    fee_scope,
    performance_clause,
    tax_phrases,
)
from procurement_agent.extraction.normalize import normalize_spaces
from procurement_agent.extraction.rules import (
    find_dates,
    find_percentages,
    is_advance_deadline,
    is_hypothetical,
    is_net_measure,
    is_penalty_rate,
    payment_terms_value,
    penalty_period,
    percent_of,
    restating_bracket,
)
from procurement_agent.extraction.schemas import FactType

#: A document is an act when its title says so near the top.
_ACT_TITLE = re.compile(r"მიღება\s*-?\s*ჩაბარების\s+აქტ|acceptance\s+(?:act|certificate)", re.I)
_TITLE_WINDOW = 300
#: ... or below a letterhead - the issuer's name, addresses, telephone, bank - on a line of its
#: own among the first lines, before the text begins: a numbered clause, or a contract's title.
_TITLE_LINES = 15
_TITLE_LINE_CHARS = 150
_TEXT_BEGINS = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.\s|მუხლი\s+\d|article\s+\d)"
    r"|ხელშეკრულება(?![ა-ჰ])|(?<![a-z])(?:agreement|contract)(?![a-z])",
    re.I,
)
#: A first day a performance clause writes and the date reader could not read: a number, then a
#: word ending in "from" ("1-ლი ივნისიდან" before the reader knew ordinals).
_UNREAD_START = re.compile(
    r"(?<![\d.])\d{1,2}(?:[./-]\d{1,2}[./-]\d{2,4})?\S*\s*[ა-ჰ]*დან(?![ა-ჰ])"
)
_UNIT_CODES = {"day": "D", "week": "W", "month": "M", "year": "Y"}
#: How a range reads: an en dash with a space either side, built from its code point.
_DASH = f" {chr(0x2013)} "


@dataclass(frozen=True, slots=True)
class DocumentFact:
    type: FactType
    #: as the extraction shows it
    value: str
    #: the canonical form (docs/GOLD_SPEC.md section 2)
    normalized: str
    #: the document's own words, to be found again in a chunk for the citation
    raw_text: str
    confidence: float = 0.9
    unit: str | None = None


def is_act(text: str) -> bool:
    """Whether the document is an acceptance act: its title near the top, or on a line of its
    own below a letterhead, before the text begins."""
    if _ACT_TITLE.search(text[:_TITLE_WINDOW]) is not None:
        return True
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    for line in lines[:_TITLE_LINES]:
        if len(line) <= _TITLE_LINE_CHARS and _ACT_TITLE.search(line):
            return True
        if _TEXT_BEGINS.search(line):
            return False
    return False


def document_facts(text: str) -> list[DocumentFact]:
    """Every whole-document fact the readers can state, in canonical form."""
    view = DocumentView.build("document", text)
    folded = view.text
    out: list[DocumentFact] = []
    act = is_act(folded) or is_act(text)
    checker = AoaChecker(aoa=view, contract=None)

    if act:
        act_date = checker._act_date()
        if act_date is not None:
            d, hit = act_date
            out.append(_date_fact(FactType.act_date, d, hit.raw_text))
        # the contract the act closes, by its date: stated here, it takes the place of every
        # date the chunk rules guessed a contract date from a keyword nearby - the act's own,
        # the period's, the advance's
        for h in find_dates(folded):
            if contract_named_by(folded, h) is not None:
                signed = date.fromisoformat(str(h.normalized))
                out.append(_date_fact(FactType.contract_date, signed, h.raw_text))
                break
        period = _service_period(checker)
        if period is not None:
            out.append(_range_fact(FactType.execution_period, *period))
    else:
        term = view.term()
        if term.signed is not None:
            raw = next(
                (h.raw_text for h in find_dates(folded) if h.normalized == term.signed.isoformat()),
                term.signed.isoformat(),
            )
            out.append(_date_fact(FactType.contract_date, term.signed, raw))
        if term.performance is not None and (term.performance.start or term.performance.end):
            start = term.performance.start
            if start is None and not _start_unread(view):
                start = term.signed
            out.append(_range_fact(FactType.execution_period, start, term.performance.end, None))
        if term.validity is not None and (term.validity.start or term.validity.end or term.signed):
            start = term.validity.start or term.signed
            out.append(_range_fact(FactType.validity_period, start, term.validity.end, None))

    payment = _payment_terms(view)
    if payment is not None:
        out.append(payment)

    vat = _vat_status(view)
    if vat is not None:
        out.append(vat)

    subject = _subject(folded, view.sentences())
    if subject:
        what = _trim_subject(subject)
        out.append(
            DocumentFact(
                type=FactType.subject,
                value=what,
                normalized=what,
                raw_text=what,
                confidence=0.85,
            )
        )

    out.extend(_penalties(folded))
    return out


def _service_period(checker: AoaChecker) -> tuple[date, date, str] | None:
    """An act's service period: its first day, its last and the words stating it, read where
    the acceptance check reads it (``AoaChecker._period_hits``) - after the words that open it,
    or failing them by its form - so the two can never state different periods (red team R4)."""
    hits = checker._period_hits()
    if not hits:
        return None
    text = checker.aoa.text
    first = min(h.start for _, h in hits)
    last = max(h.end for _, h in hits)
    dates = [d for d, _ in sorted(hits, key=lambda p: p[1].start)]
    # two dates are the period's start and end as written, even when a year typed wrong puts
    # the end first: sorted, the period stated was one neither document states
    if len(dates) == 2:
        return dates[0], dates[1], text[first:last]
    return min(dates), max(dates), text[first:last]


def _start_unread(view: DocumentView) -> bool:
    """Whether a performance clause writes a first day the date reader could not read: the
    window then has no known start, and the signing date must not stand in for it."""
    text = view.text
    dates = [(h.start, h.end) for h in find_dates(text)]
    for a, b in view.sentences():
        if not performance_clause(text, a, b) or _FROM_SIGNING.search(text, a, b):
            continue
        if _NOT_A_TERM.search(text, a, b) or _PAYMENT.search(text, a, b):
            continue
        for m in _UNREAD_START.finditer(text, a, b):
            if not any(s <= m.start() < e for s, e in dates):
                return True
    return False


#: What a subject clause says before the service itself: the clause number, and the project
#: it is for up to its cadastral code - "1.1. ... „ნიმუში“ პროექტისთვის (ს/კ: 01.02.03.456)",
#: "... პროექტის - „ნიმუში“ (საკადასტრო კოდი: 01.02.03.456) ფარგლებში".
_BEFORE_THE_SERVICE = re.compile(
    r"^.*?\(\s*(?:ს\s*/\s*კ|საკადასტრო\s+კოდი)[^)]*\)\s*(?:ფარგლებში\s*)?", re.DOTALL
)
_CLAUSE_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s*")


def _subject(text: str, sentences: list[tuple[int, int]]) -> str | None:
    """The subject clause as written: up to the term it defines ("(შემდგომში „მომსახურება“)",
    ``subject_clause``), else up to the end of its sentence - "ხელშეკრულების საგანია ...
    სამუშაოების შესრულება." is a subject clause as much as one that defines a term. After a
    heading ("1. ხელშეკრულების საგანი") the clause is the sentence under it."""
    found = subject_clause(text)
    if found is not None and found.text:
        return found.text
    for m in _SUBJECT_START.finditer(text):
        at = next((i for i, (a, b) in enumerate(sentences) if a <= m.start() < b), None)
        if at is None:
            continue
        rest = text[m.end() : sentences[at][1]].strip(" :")
        if len(rest.split()) < 2 and at + 1 < len(sentences):
            rest = text[sentences[at + 1][0] : sentences[at + 1][1]]
        rest = normalize_spaces(rest[:_SUBJECT_MAX])
        if len(rest.split()) >= 2:
            return rest
    return None


def _trim_subject(text: str) -> str:
    """The service a subject clause names, without the clause number and the project reference
    in front of it; the clause as it is when that would leave too little."""
    trimmed = _BEFORE_THE_SERVICE.sub("", text, count=1)
    if len(trimmed.split()) < 2:
        trimmed = text
    return _CLAUSE_NUMBER.sub("", trimmed).strip(" .,;:")


def _date_fact(ft: FactType, d: date, raw: str) -> DocumentFact:
    return DocumentFact(type=ft, value=d.isoformat(), normalized=d.isoformat(), raw_text=raw)


def _range_fact(
    ft: FactType, start: date | None, end: date | None, raw: str | None
) -> DocumentFact:
    """A period as one fact: ``2026-08-01/2026-08-31``, or ``2026-08-01/..`` with no end."""
    a = start.isoformat() if start else ".."
    b = end.isoformat() if end else ".."
    shown = f"{a}{_DASH}{b}" if end else f"{a}{_DASH.rstrip()}"
    return DocumentFact(
        type=ft, value=shown, normalized=f"{a}/{b}", raw_text=raw or shown, confidence=0.9
    )


def _vat_status(view: DocumentView) -> DocumentFact | None:
    """Whether the fee includes VAT, excludes it, or is net: the tax wording around the fee,
    read the way the tax-wording rule reads it.

    Only wording about this fee counts: not the tax wording inside a bracket that restates the
    price on another basis ("(დღგ-ს გარეშე 1 000 ლარი)", ``rules.restating_bracket``), which is
    that figure's; not "net" measuring days or a weight, nor inside "net of VAT" (an exclusion);
    and wording under a condition ("თუ ... არ არის დღგ-ს გადამხდელი") only when nothing else
    states the status."""
    text = view.text
    sentences = view.sentences()
    phrases = tax_phrases(text)
    worded = [p.span for p in phrases if p.kind in ("inclusion", "exclusion")]
    fees = [r for r in view.amounts() if r.role in (None, "price")]
    restated = [b for r in fees if (b := restating_bracket(text, r.money.span[0])) is not None]
    fees = [r for r in fees if not _within(r.money.span, restated)]
    phrases = [
        p
        for p in phrases
        if not _within(p.span, restated)
        and not (p.kind == "net" and (_within(p.span, worded) or is_net_measure(text, p.span[1])))
    ]
    stated = [p for p in phrases if not is_hypothetical(text, p.span[0])]
    return _status_of(stated, fees, sentences) or _status_of(phrases, fees, sentences)


def _within(span: tuple[int, int], others: list[tuple[int, int]]) -> bool:
    return any(a <= span[0] and span[1] <= b for a, b in others)


def _status_of(
    phrases: list[TaxPhrase], fees: list[RoledAmount], sentences: list[tuple[int, int]]
) -> DocumentFact | None:
    """The VAT status the wording around the fees states, by kind: net, exempt, exclusion,
    inclusion; the wording anywhere when none is near a fee."""
    near: list[TaxPhrase] = []
    for fee in fees:
        a, b = fee_scope(sentences, fee.money.span)
        near.extend(p for p in phrases if a <= p.span[0] < b)
    chosen = near or [p for p in phrases if p.kind in ("inclusion", "exclusion", "net", "exempt")]
    for kind, status in (
        ("net", "net"),
        ("exempt", "none"),
        ("exclusion", "excl"),
        ("inclusion", "incl"),
    ):
        found = next((p for p in chosen if p.kind == kind), None)
        if found is not None:
            return DocumentFact(
                type=FactType.vat_status,
                value=found.text,
                normalized=status,
                raw_text=found.text,
                confidence=0.85,
            )
    return None


def _payment_terms(view: DocumentView) -> DocumentFact | None:
    """How the fee is paid, canonically (``rules.payment_terms_value``): the deadline, then the
    advance's share - "P10D working advance 30%". A staged payment states the balance's deadline,
    not the advance's; the share is the one the chunk rule reads (``find_percentages``)."""
    text = view.text
    deadlines = [d for d in view.payment_terms().deadlines if d.unit in _UNIT_CODES]
    deadline = next(
        (d for d in deadlines if not is_advance_deadline(text, *d.span)),
        deadlines[0] if deadlines else None,
    )
    share = next((h for h in find_percentages(text) if h.type is FactType.payment_terms), None)
    canonical = None
    if deadline is not None:
        canonical = f"P{deadline.count}{_UNIT_CODES[deadline.unit]}"
        if deadline.qualifier:
            canonical += f" {deadline.qualifier}"
    value = payment_terms_value(canonical, percent_of(share) if share is not None else None)
    if value is None:
        return None
    raw = deadline.raw if deadline is not None else share.raw_text if share is not None else value
    return DocumentFact(
        type=FactType.payment_terms,
        value=raw,
        normalized=value,
        raw_text=raw,
        unit=deadline.unit if deadline is not None else "%",
    )


def _penalties(text: str) -> list[DocumentFact]:
    """Percentages a penalty clause sets, with the period it counts per: "0.5% per day"."""
    out: list[DocumentFact] = []
    for hit in find_percentages(text):
        # a penalty word in the rate's own sentence, no cap, no VAT rate, no guarantee's share
        if not is_penalty_rate(text, hit.start, hit.end):
            continue
        value = percent_of(hit)
        if value is None:
            continue
        shown = f"{value:g}%"
        period = penalty_period(text, hit.start, hit.end)
        if period is not None:
            shown += f" per {period}"
        out.append(
            DocumentFact(
                type=FactType.penalty,
                value=shown,
                normalized=shown,
                raw_text=hit.raw_text,
                confidence=0.85,
            )
        )
    return out
