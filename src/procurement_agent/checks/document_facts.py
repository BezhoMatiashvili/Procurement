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

from procurement_agent.checks.aoa import AoaChecker, DocumentView
from procurement_agent.checks.aoa_scope import subject_clause
from procurement_agent.checks.contract_terms import TaxPhrase, fee_scope, tax_phrases
from procurement_agent.extraction.rules import find_dates, find_percentages
from procurement_agent.extraction.schemas import FactType

#: A document is an act when its title says so near the top.
_ACT_TITLE = re.compile(r"მიღება\s*-?\s*ჩაბარების\s+აქტ|acceptance\s+(?:act|certificate)", re.I)
_TITLE_WINDOW = 300
#: "... ყოველ ვადაგადაცილებულ დღეზე", "per day of delay": a penalty counted per day.
_PER_DAY = re.compile(r"ყოველ\s+(?:[ა-ჰ]+\s+){0,2}დღ|per\s+(?:calendar\s+|working\s+)?day", re.I)
_PENALTY_WORDS = re.compile(r"პირგასამტეხლო|ჯარიმ|საურავ|penalt|liquidated damages|fine", re.I)
_UNIT_CODES = {"day": "D", "week": "W", "month": "M", "year": "Y"}
#: How a range reads: an en dash with a space either side, built from its code point.
_DASH = f" {chr(0x2013)} "
_Q = "\u201e\u201c\u201d\"'"
#: An act names the contract it closes by its date: "მხარეებმა 01.09.2026 გააფორმეს
#: ხელშეკრულება" - the date, then the contract signed - ...
_CONTRACT_SIGNED_AFTER = re.compile(
    rf"[\s,]*(?:გააფორმეს|გაფორმებული|დადებული|დადეს)?[\s{_Q}]*(?:N\s*\S+\s*)?ხელშეკრულებ"
)
#: ... or the contract, then its signing: "ხელშეკრულება გაფორმდა 2026 წლის 1 მარტს",
#: "ხელშეკრულების თარიღი: 01.03.2026".
_CONTRACT_DATED_BEFORE = re.compile(
    rf"ხელშეკრულებ[ა-ჰ]*[{_Q}]*\s+(?:გაფორმდა|დაიდო|(?:გაფორმების\s+)?თარიღი)[\s:,-]*$"
)


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
    return _ACT_TITLE.search(text[:_TITLE_WINDOW]) is not None


def document_facts(text: str) -> list[DocumentFact]:
    """Every whole-document fact the readers can state, in canonical form."""
    view = DocumentView.build("document", text)
    folded = view.text
    out: list[DocumentFact] = []
    act = is_act(folded)
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
            if _CONTRACT_SIGNED_AFTER.match(folded, h.end) or _CONTRACT_DATED_BEFORE.search(
                folded, max(0, h.start - 60), h.start
            ):
                signed = date.fromisoformat(str(h.normalized))
                out.append(_date_fact(FactType.contract_date, signed, h.raw_text))
                break
        period = checker._period_hits()
        if period:
            dates = [d for d, _ in period]
            first = min(h.start for _, h in period)
            last = max(h.end for _, h in period)
            out.append(
                _range_fact(FactType.execution_period, min(dates), max(dates), folded[first:last])
            )
    else:
        term = view.term()
        if term.signed is not None:
            raw = next(
                (h.raw_text for h in find_dates(folded) if h.normalized == term.signed.isoformat()),
                term.signed.isoformat(),
            )
            out.append(_date_fact(FactType.contract_date, term.signed, raw))
        if term.performance is not None and (term.performance.start or term.performance.end):
            start = term.performance.start or term.signed
            out.append(_range_fact(FactType.execution_period, start, term.performance.end, None))
        if term.validity is not None and (term.validity.start or term.validity.end or term.signed):
            start = term.validity.start or term.signed
            out.append(_range_fact(FactType.validity_period, start, term.validity.end, None))

    for deadline in view.payment_terms().deadlines[:1]:
        code = _UNIT_CODES.get(deadline.unit)
        if code is None:
            continue
        canonical = f"P{deadline.count}{code}"
        if deadline.qualifier:
            canonical += f" {deadline.qualifier}"
        out.append(
            DocumentFact(
                type=FactType.payment_terms,
                value=deadline.raw,
                normalized=canonical,
                raw_text=deadline.raw,
                unit=deadline.unit,
            )
        )

    vat = _vat_status(view)
    if vat is not None:
        out.append(vat)

    subject = subject_clause(folded)
    if subject is not None and subject.text:
        what = _trim_subject(subject.text)
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


#: What a subject clause says before the service itself: the clause number, and the project
#: it is for up to its cadastral code - "1.1. ... „ნიმუში“ პროექტისთვის (ს/კ: 01.02.03.456)",
#: "... პროექტის - „ნიმუში“ (საკადასტრო კოდი: 01.02.03.456) ფარგლებში".
_BEFORE_THE_SERVICE = re.compile(
    r"^.*?\(\s*(?:ს\s*/\s*კ|საკადასტრო\s+კოდი)[^)]*\)\s*(?:ფარგლებში\s*)?", re.DOTALL
)
_CLAUSE_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s*")


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
    read the way the tax-wording rule reads it."""
    sentences = view.sentences()
    phrases = tax_phrases(view.text)
    fees = [r for r in view.amounts() if r.role in (None, "price")]
    near: list[TaxPhrase] = []
    for fee in fees:
        a, b = fee_scope(sentences, fee.money.span)
        near.extend(p for p in phrases if a <= p.span[0] < b)
    chosen = near or [p for p in phrases if p.kind in ("inclusion", "exclusion", "net")]
    for kind, status in (("net", "net"), ("exclusion", "excl"), ("inclusion", "incl")):
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


def _penalties(text: str) -> list[DocumentFact]:
    """Percentages a penalty clause sets, with the period it counts per: "0.5% per day"."""
    out: list[DocumentFact] = []
    for hit in find_percentages(text):
        start = max(0, hit.start - 120)
        before = text[start : hit.start]
        if not _PENALTY_WORDS.search(before + text[hit.end : hit.end + 40]):
            continue
        value = float(hit.normalized) if isinstance(hit.normalized, float) else None
        if value is None:
            continue
        shown = f"{value:g}%"
        if _PER_DAY.search(text, hit.end, hit.end + 60):
            shown += " per day"
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
