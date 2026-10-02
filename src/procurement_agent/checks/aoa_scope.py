"""What the act says was done, against what the contract says is to be done (client rules 10.3
and 10.6): the subject clause, and the list of services.

The act restates both in its own words - "რომლის საგანსაც წარმოადგენს …" for the contract's
"ხელშეკრულების საგანი …", an annex for the contract's list of what is to be handed over. A rule
can compare the words, but it cannot tell a paraphrase from a different service, so it claims
little (stage 1 of the plan, rules only):

* A number that differs - a quantity, an amount - in an item that otherwise reads as the
  contract's is an error. That is evidence.
* An item whose words the contract does not clearly hold is a note for a person to read
  (``Finding.needs_review``). A template that words its subject its own way gets no error and
  no warning.

Pure functions; ``AoaChecker.rule_subject`` and ``AoaChecker.rule_services`` turn what they find
into findings.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from procurement_agent.extraction.normalize import (
    QUOTE_CHARS,
    content_stems,
    normalize_name,
    normalize_spaces,
    parse_georgian_number_words,
    parse_number,
    stems_match,
)
from procurement_agent.extraction.rules import _EN_MONTH_RE, _KA_MONTH_RE, CADASTRAL_RE, find_dates

#: How much of an act's item must be the contract's words for the two to be one item: all of
#: them, counted on the act's side, since a contract says more than an act repeats. The items of
#: one list share most of their words - "… სერვისის გვერდის აღწერა GEO/ENG" - and the word that
#: differs is the one that names the service, so most is not enough.
MATCH_SHARE = 1.0
#: How much of it must be for a number that differs to be the contract's item's number, changed:
#: three words in four.
NUMBER_SHARE = 0.75
#: How far past its opening words a subject clause may run before the term it defines.
_SUBJECT_MAX = 700

_Q = re.escape(QUOTE_CHARS)
#: Where a subject clause starts: a contract's heading "ხელშეკრულების საგანი", or an act's
#: "(რომლის) საგანს / საგანსაც წარმოადგენს". Case forms only: "საგანგებო" is not a subject.
_SUBJECT_START = re.compile(
    r"(?:ხელშეკრულების|რომლის|მისი)\s+საგან(?:ი|ია)(?![ა-ჰ])"
    r"|საგან(?:ს|საც|ი)\s+წარმოადგენს(?![ა-ჰ])"
)
#: Where it ends: the term it defines, "(შემდგომში „მომსახურება“)" or the works or the goods.
_SUBJECT_END = re.compile(
    rf"\(\s*შემდგომში[\s\-,{_Q}]*(?:მომსახურება|სამუშაო(?:ები)?|საქონელი)(?![ა-ჰ])"
)
#: A lead-in whose list is the services: "… უნდა ჩააბაროს:", "კერძოდ:", "მოიცავს:",
#: "მომსახურების აღწერა:". One that says what a party must do - "ვალდებულია:" - lists
#: obligations instead.
_LEAD_IN = re.compile(r"ჩააბაროს|კერძოდ|მოიცავს|მომსახურების\s+აღწერა")
_OBLIGATIONS = re.compile(r"ვალდებულია")
#: A list line: "- …", "• …", "ა) …", "1) …".
_BULLET = re.compile(r"(?:[-•*·]|[ა-ჰ]\)|\d{1,2}\))\s*(\S.*)")
#: A list written as a table, one row to a line ended with ";" as the chunker writes a table
#: (``ingestion/chunker._by_row``): "1: მთავარი გვერდის დიზაინი;", or a label and its value
#: ("მთავარი გვერდის დიზაინი: 1 ცალი;"). An unbulleted list's lines end the same way. The row's
#: item number is no part of its text.
_ROW = re.compile(r"(?:(?:\d{1,3}(?:\.\d{1,3})*|[ა-ჰ])[.)]?\s*:\s*)?(\S.*?)\s*;")
#: A table under a header row as Docling writes it, every row on one line - "1, მომსახურება =
#: მთავარი გვერდის დიზაინი. 1, რაოდენობა = 1. 2, მომსახურება = …" - or one row to a line,
#: keyed by its item number ("1, მომსახურება = …, რაოდენობა = 1;", ``chunker._keyed_rows``):
#: where each cell starts, with its row's label when it gives one, and its column's name.
_CELL = re.compile(
    r"(?:^|(?<=[.;,]\s))(?:(?P<row>[^=.,;]{1,80}?),\s)?(?P<column>[^=.,;]{1,60}?)\s=\s"
)
#: An item number as a row's label: no part of the item's text.
_ITEM_NUMBER = re.compile(r"\d{1,3}(?:\.\d{1,3})*[.)]?|[ა-ჰ][.)]?")
#: A money column of such a table: the fee rules read the value, the services rule compares
#: what was done and how much of it.
_MONEY_COLUMN = re.compile(
    r"ფას|ჯამ|ღირებულ|თანხ|ლარ|დოლარ|ევრო|სულ|[₾$€]"
    r"|(?<![a-z])(?:price|amount|total|sum|cost|gel|usd|eur)",
    re.IGNORECASE,
)

_QUOTED = re.compile(rf"[{_Q}][^{_Q}]{{0,90}}[{_Q}]")
_CADASTRAL_NOTE = re.compile(r"\(\s*ს\s*/\s*კ[^()]*\)")
_BRACKETED = re.compile(r"\(([^()]{1,80})\)")
_CLAUSE_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)*\.(?=\s)")
_GEORGIAN_LETTER = re.compile("[\u10d0-\u10fa]")
_FOREIGN_LETTER = re.compile("[A-Za-z\u0400-\u04ff]")
_NUMBER = re.compile(r"(?<![\w.,])(?:\d{1,3}(?: \d{3})+|\d+)(?:[.,]\d+)?(?!\w)")
_YEAR = r"(?:19|20)\d{2}(?!\d)"
#: A year named as one, which ``find_dates`` leaves, since it names no day: "2026 წელი", "2026
#: წლის", "2026 წ.", or a month beside it - "აგვისტო 2026", "2026 აგვისტო", "August 2026",
#: "08/2026". That is the period, not a quantity; a bare "2026" may be one ("2026 ლარი").
_PERIOD = re.compile(
    rf"(?<![\d.,]){_YEAR}(?=\s*წ(?:ელ|ლ|\.|(?![ა-ჰ])))"
    rf"|(?:(?:{_KA_MONTH_RE})[ა-ჰ]*|\b(?:{_EN_MONTH_RE})\b\.?),?\s*{_YEAR}"
    rf"|(?<![\d.,]){_YEAR}\s+(?:{_KA_MONTH_RE})"
    rf"|(?<![\w.,/])(?:0?[1-9]|1[0-2])\s*[./]\s*{_YEAR}",
    re.IGNORECASE,
)
#: Words every subject clause of the template shares - the contract, its parties, the project -
#: which the party and project-name rules compare on their own: stems these start, so that every
#: case form goes, and whole stems where a prefix would take a service with it ("მხარდაჭერა",
#: support; "პროექტირება", design).
_TEMPLATE_PREFIXES = ("ხელშეკრულ", "შემსრულებ", "შემკვეთ", "დამკვეთ")
_TEMPLATE_STEMS = frozenset({"მხარ", "მხარე", "პროექტ"})

Verdict = Literal["match", "number", "review"]


@dataclass(frozen=True)
class ScopePart:
    """A subject clause or one listed service: what the document writes, and what of it is
    compared - its content words (``content_stems``) and its numbers."""

    text: str
    stems: tuple[str, ...]
    numbers: tuple[str, ...]
    #: where ``text`` is, in the text it was read from; None for a list line
    span: tuple[int, int] | None = None


@dataclass(frozen=True)
class Alignment:
    """An act's item, the contract's item most like it, and what the rules make of the pair."""

    item: ScopePart
    best: ScopePart | None
    #: the share of the item's content words the contract's item holds
    share: float
    verdict: Verdict
    #: the item's numbers the contract's item does not state
    differ: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScopeReading:
    """Both documents' subject clauses and lists of services, and each part of the act aligned
    with the contract's. Read once per check: the rules report from it, and the model review
    (``aoa_llm.review_items``) is asked about exactly what it leaves open, so the two can never
    read the documents differently."""

    contract_subject: ScopePart | None
    act_subject: ScopePart | None
    #: the act's subject against the contract's; None when either has no subject clause, or
    #: the act's names nothing but the project and the plot
    subject: Alignment | None
    contract_services: tuple[ScopePart, ...]
    act_services: tuple[ScopePart, ...]
    #: one per service the act lists, in its order; empty when either document lists none
    services: tuple[Alignment, ...]


def _as_meant(word: re.Match[str]) -> str:
    """A Georgian word without the letters of another alphabet a slip put in it."""
    w = word.group(0)
    if _GEORGIAN_LETTER.search(w) and _FOREIGN_LETTER.search(w):
        return _FOREIGN_LETTER.sub("", w)
    return w


def _clean(text: str) -> str:
    """The words worth comparing. Quoted names - the project, the parties' defined terms - and
    the cadastral code go, since the project-name and cadastral rules compare those; so do a
    clause number and a number's words in brackets, "4 (ოთხი)", which only repeat it. A Latin or
    Cyrillic letter inside a Georgian word ("გkვერდის") is dropped: AOA.SPELLING reports it, and
    the line is still the contract's line."""
    text = re.sub(r"\w+", _as_meant, text)
    text = _CLAUSE_NUMBER.sub(" ", text)
    text = _CADASTRAL_NOTE.sub(" ", text)
    text = _QUOTED.sub(" ", text)
    text = _BRACKETED.sub(
        lambda m: " " if parse_georgian_number_words(m.group(1)) is not None else m.group(0), text
    )
    return normalize_spaces(CADASTRAL_RE.sub(" ", text))


def _numbers(text: str) -> tuple[str, ...]:
    """The numbers ``text`` states - a quantity, an amount - each once, as a value: "1 000" and
    "1000" are one number. A date is not one, nor a month and year; the date rules read those."""
    chars = list(text)
    for hit in find_dates(text):
        chars[hit.start : hit.end] = " " * (hit.end - hit.start)
    for m in _PERIOD.finditer(text):
        chars[m.start() : m.end()] = " " * (m.end() - m.start())
    out: dict[str, None] = {}
    for m in _NUMBER.finditer("".join(chars)):
        value = parse_number(m.group(0))
        key = f"{value:.2f}".rstrip("0").rstrip(".") if value is not None else m.group(0)
        out.setdefault(key)
    return tuple(out)


def scope_part(text: str, span: tuple[int, int] | None = None) -> ScopePart:
    """``text`` read for comparison: its content words, less the template's own, and its
    numbers."""
    cleaned = _clean(text)
    stems = tuple(
        s
        for s in content_stems(cleaned)
        if s not in _TEMPLATE_STEMS and not s.startswith(_TEMPLATE_PREFIXES)
    )
    return ScopePart(normalize_spaces(text), stems, _numbers(cleaned), span)


def subject_clause(text: str) -> ScopePart | None:
    """The document's subject clause: from "ხელშეკრულების საგანი" (a contract's heading) or "…
    საგანს წარმოადგენს" (an act restating it) to the service it defines, "(შემდგომში
    „მომსახურება“)". None when no such clause ends within 700 characters of its start."""
    for start in _SUBJECT_START.finditer(text):
        end = _SUBJECT_END.search(text, start.end(), start.end() + _SUBJECT_MAX)
        if end is None:
            continue
        raw = text[start.end() : end.start()]
        a = start.end() + len(raw) - len(raw.lstrip(" :"))  # "ხელშეკრულების საგანი: …"
        b = end.start() - (len(raw) - len(raw.rstrip()))
        if a < b:
            return scope_part(text[a:b], (a, b))
    return None


def _table_items(line: str) -> list[str] | None:
    """The items of a table written in cells (``_CELL``): each row's label - unless it is an
    item number - and the cells of every column but a money column, joined. None for a line
    that is no such table."""
    cells = list(_CELL.finditer(line))
    if not cells or cells[0].start() != 0 or cells[0].group("row") is None:
        return None
    rows: dict[str, list[str]] = {}
    label = ""
    for i, cell in enumerate(cells):
        end = cells[i + 1].start() if i + 1 < len(cells) else len(line)
        value = line[cell.end() : end].strip().rstrip(".,;").strip()
        label = cell.group("row") or label
        row = rows.setdefault(label, [] if _ITEM_NUMBER.fullmatch(label) else [label])
        if value and not _MONEY_COLUMN.search(cell.group("column")):
            row.append(value)
    return [" ".join(parts) for parts in rows.values() if parts]


def service_items(lines: Sequence[str]) -> list[ScopePart]:
    """The services a document lists, right under a lead-in that ends in ":" and says what
    follows - "… უნდა ჩააბაროს:", "კერძოდ:", "მოიცავს:", "მომსახურების აღწერა:": its "- " / "•"
    / "ა)" lines, the rows of a table (``_ROW``, ``_table_items``) or an unbulleted list's lines
    ended with ";". A lead-in that says what a party must do ("ვალდებულია:") introduces
    obligations, and its lines are not services. Each item once: the parser can emit a table's
    text twice.
    """
    items: dict[str, ScopePart] = {}
    listing = False
    for line in lines:
        stripped = line.strip()
        found: list[str] = []
        if listing:
            if m := _BULLET.fullmatch(stripped):
                found = [m.group(1)]
            elif (table := _table_items(stripped)) is not None:
                found = table
            elif m := _ROW.fullmatch(stripped):
                found = [m.group(1)]
        if found:
            for text in found:
                items.setdefault(normalize_name(text), scope_part(text))
            continue
        listing = (
            line.rstrip().endswith(":")
            and _LEAD_IN.search(line) is not None
            and _OBLIGATIONS.search(line) is None
        )
    return list(items.values())


def share(item: ScopePart, other: ScopePart) -> float:
    """The share of ``item``'s content words that ``other`` holds (``stems_match``)."""
    if not item.stems:
        return 0.0
    held = sum(1 for s in item.stems if any(stems_match(s, t) for t in other.stems))
    return held / len(item.stems)


def _replaced(item: ScopePart, other: ScopePart) -> bool:
    """Whether ``item`` states a number in place of one ``other`` states: each has a number the
    other does not. A number only one of them states - the period, a share done - replaces
    nothing."""
    return any(n not in item.numbers for n in other.numbers) and any(
        n not in other.numbers for n in item.numbers
    )


def align(item: ScopePart, candidates: Sequence[ScopePart]) -> Alignment:
    """``item`` against the contract's item most like it, and the verdict.

    The most like it holds most of its words; on a tie, the one whose numbers it does not
    contradict, then the one that states more of its numbers: a contract that lists "4 გვერდი"
    and "5 გვერდი" has the act's "5 გვერდი", not a changed "4". Then:

    * match - the words are all the contract's item's, or the text is its text, and so are the
      numbers, or the act states none (an act need not restate a quantity);
    * number - three words in four are the contract's item's, and a number it states is
      replaced by another;
    * review - anything else: the words are not all the contract's (another service, other
      wording, another language), or the act adds a number the contract's item does not state.
    """
    best: ScopePart | None = None
    best_key: tuple[float, bool, int] = (-1.0, False, -1)
    for c in candidates:
        if c.text.casefold() == item.text.casefold():  # also an item with no words to stem
            return Alignment(item, c, 1.0, "match")
        key = (share(item, c), not _replaced(item, c), sum(n in c.numbers for n in item.numbers))
        if key > best_key:
            best, best_key = c, key
    if best is None:
        return Alignment(item, None, 0.0, "review")
    held = best_key[0]
    differ = tuple(n for n in item.numbers if n not in best.numbers)
    verdict: Verdict
    if held >= NUMBER_SHARE and _replaced(item, best):
        verdict = "number"
    elif held >= MATCH_SHARE and not differ:
        verdict = "match"
    else:
        verdict = "review"
    return Alignment(item, best, held, verdict, differ)


def read_scope(
    act_text: str,
    act_lines: Sequence[str],
    contract_text: str,
    contract_lines: Sequence[str],
) -> ScopeReading:
    """Read and align both documents' subjects and services (see ``ScopeReading``)."""
    contract_subject = subject_clause(contract_text)
    act_subject = subject_clause(act_text)
    subject = (
        align(act_subject, [contract_subject])
        if contract_subject is not None and act_subject is not None and act_subject.stems
        else None
    )
    contract_services = tuple(service_items(contract_lines))
    act_services = tuple(service_items(act_lines))
    services = (
        tuple(align(item, contract_services) for item in act_services) if contract_services else ()
    )
    return ScopeReading(
        contract_subject, act_subject, subject, contract_services, act_services, services
    )
