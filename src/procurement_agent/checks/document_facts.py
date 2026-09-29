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
from datetime import date, timedelta

from procurement_agent.checks.aoa import (
    _ACT_DATED_BEFORE,
    _ADVANCE_WORDS,
    _CONTRACT_REFERENCE,
    AoaChecker,
    DocumentView,
)
from procurement_agent.checks.aoa_scope import subject_clause
from procurement_agent.checks.contract_terms import (
    _FROM_SIGNING,
    _NOT_A_TERM,
    _PAYMENT,
    _PERFORMANCE,
    _VALIDITY,
    TaxPhrase,
    _range_in,
    fee_scope,
    tax_phrases,
)
from procurement_agent.extraction.rules import RuleHit, _month_ka, find_dates, find_percentages
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
#: What an act says a service period of: the work done, rendered, carried out ("შესრულდა",
#: "შესრულებულია", "გაწეულ იქნა", "განხორციელდა", "ჩატარდა"), or a period named as one.
_SERVICE_WORDS = re.compile(
    r"შესრულ|გაწე|გაიწ|განხორციელ|ჩატარ|პერიოდ|მომსახურებ|სამუშაო|მიწოდ|"
    r"(?<![a-z])(?:perform|render|provid|period)",
    re.I,
)
#: A range's end written without its year, the start's year understood: "2026 წლის 1 ივნისიდან
#: 30 ივნისის ჩათვლით", "... 30 ივნისამდე".
_YEARLESS_END = re.compile(
    r"\s*[-\u2013\u2014]?\s*(?P<d>\d{1,2})\s*(?P<m>[ა-ჰ]{3,14})(?P<mark>\s*\(?\s*ჩათვლით)?"
)
#: A period stated as a month: "2026 წლის ივნისის თვეში", "2026 წ. ივნისის თვის განმავლობაში".
_MONTH_PERIOD = re.compile(r"(?<!\d)(?P<y>\d{4})\s*წ(?:ლის|\.)?\s*(?P<m>[ა-ჰ]{3,14})\s+თვ[ეი]")
#: A first day a performance clause writes and the date reader could not read: a number, then a
#: word ending in "from" ("1-ლი ივნისიდან" before the reader knew ordinals).
_UNREAD_START = re.compile(
    r"(?<![\d.])\d{1,2}(?:[./-]\d{1,2}[./-]\d{2,4})?\S*\s*[ა-ჰ]*დან(?![ა-ჰ])"
)
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
            if _CONTRACT_SIGNED_AFTER.match(folded, h.end) or _CONTRACT_DATED_BEFORE.search(
                folded, max(0, h.start - 60), h.start
            ):
                signed = date.fromisoformat(str(h.normalized))
                out.append(_date_fact(FactType.contract_date, signed, h.raw_text))
                break
        period = _service_period(view, checker)
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


def _service_period(view: DocumentView, checker: AoaChecker) -> tuple[date, date, str] | None:
    """An act's service period: its first day, its last and the words stating it.

    Read first where the acceptance check reads it (``AoaChecker._period_hits``, after
    "შესრულდა" and the other words that open it). When that finds no range, the period is
    known by its form instead of a verb: two dates of one sentence written as a range - "... 1
    ივნისიდან ... 30 ივნისის ჩათვლით", "01.06.2026 - 30.06.2026" - that date neither the act
    nor an advance and name no contract; failing that, a month named as one ("2026 წლის ივნისის
    თვეში")."""
    text = view.text
    hits = checker._period_hits()
    if len({d for d, _ in hits}) >= 2:
        first = min(h.start for _, h in hits)
        last = max(h.end for _, h in hits)
        return min(d for d, _ in hits), max(d for d, _ in hits), text[first:last]
    ranges = _ranges(view, checker)
    if hits:
        # one date read after the words that open the period: the range it starts, if any
        at = hits[0][1].start
        ranges = [r for r in ranges if r[3] <= at < r[4]]
        if not ranges:
            d, h = hits[0]
            return d, d, h.raw_text
    served = [r for r in ranges if _SERVICE_WORDS.search(r[2])]
    chosen = served[:1] or (ranges if len(ranges) == 1 else [])
    if chosen:
        a, b, _, lo, hi = chosen[0]
        return a, b, text[lo:hi]
    return _month_period(view)


def _ranges(view: DocumentView, checker: AoaChecker) -> list[tuple[date, date, str, int, int]]:
    """Every range an act's sentences write, in document order: (first day, last day, the
    sentence, where the range starts, where it ends)."""
    text = view.text
    act = checker._act_date()
    hits = [
        h
        for h in find_dates(text)
        if (act is None or h.start != act[1].start)
        and not _ACT_DATED_BEFORE.search(text, max(0, h.start - 30), h.start)
        and not _CONTRACT_REFERENCE.match(text, h.end)
    ]
    out: list[tuple[date, date, str, int, int]] = []
    for a, b in view.sentences():
        sentence = text[a:b]
        if _ADVANCE_WORDS.search(sentence) or _VALIDITY.search(sentence):
            continue
        inside = [h for h in hits if a <= h.start < b]
        if not inside:
            continue
        found = _range_in(text, (a, b), inside, takes_effect=False)
        start, end, stop = found.start, found.end, inside[-1].end
        if start is not None and end is None:
            end, stop = _yearless_end(text, inside, start, b)
        if start is not None and end is not None and start <= end:
            out.append((start, end, sentence, inside[0].start, stop))
    return out


def _yearless_end(
    text: str, inside: list[RuleHit], start: date, limit: int
) -> tuple[date | None, int]:
    """The last day of a range whose end is written without a year, right after its first day,
    and where it ends."""
    first = next(h for h in inside if h.normalized == start.isoformat())
    m = _YEARLESS_END.match(text, first.end, limit)
    if m is None:
        return None, first.end
    word = m.group("m")
    month = _month_ka(word)
    if month is None or not (m.group("mark") or word.endswith("მდე")):
        return None, first.end
    for year in (start.year, start.year + 1):
        try:
            end = date(year, month, int(m.group("d")))
        except ValueError:
            return None, first.end
        if end >= start:
            return end, m.end()
    return None, first.end


def _month_period(view: DocumentView) -> tuple[date, date, str] | None:
    """A service period stated as a whole month: its first day to its last."""
    text = view.text
    for m in _MONTH_PERIOD.finditer(text):
        month = _month_ka(m.group("m"))
        if month is None:
            continue
        span = next(((a, b) for a, b in view.sentences() if a <= m.start() < b), None)
        sentence = text[span[0] : span[1]] if span else ""
        if not _SERVICE_WORDS.search(sentence) or _ADVANCE_WORDS.search(sentence):
            continue
        year = int(m.group("y"))
        first = date(year, month, 1)
        last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
        return first, last, m.group(0)
    return None


def _start_unread(view: DocumentView) -> bool:
    """Whether a performance clause writes a first day the date reader could not read: the
    window then has no known start, and the signing date must not stand in for it."""
    text = view.text
    dates = [(h.start, h.end) for h in find_dates(text)]
    for a, b in view.sentences():
        if not _PERFORMANCE.search(text, a, b) or _FROM_SIGNING.search(text, a, b):
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
