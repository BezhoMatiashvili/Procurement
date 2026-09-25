"""Reading amounts out of text, shared by the AOA check and the document comparison.

Both checks need the same three things - the number in a value however it is written, the
monetary amounts a document states, and the amounts it spells out in words - and each used to
carry its own copy. Two copies of a parser drift: one learns a currency format, the other does
not, and the two checks start disagreeing about what the same document says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from procurement_agent.extraction.normalize import parse_georgian_number_words, parse_number
from procurement_agent.extraction.rules import find_amounts
from procurement_agent.extraction.schemas import FactType

__all__ = [
    "NUM_IN_TEXT",
    "DigitsAndWords",
    "Money",
    "digits_and_words",
    "money_amounts",
    "money_hits",
    "number_of",
    "word_amounts",
]

#: The first run of digits (with grouping) in a string.
NUM_IN_TEXT = re.compile("\\d[\\d\\s.,\u00a0]*\\d|\\d")

_MONEY_TYPES = frozenset({FactType.price, FactType.total_amount, FactType.amount})

#: "100 (ასი) ლარს" - digits, then the same number written out. Georgian letters only inside
#: the brackets: this is the act's own restatement of a figure, not any parenthetical.
_DIGITS_THEN_WORDS = re.compile(
    r"(?<![\d.,])(\d[\d\u00a0 .,]{0,14}\d|\d)\s*\(\s*([ა-ჰ ]{2,60})\s*\)"
)
#: Any short parenthetical. The comparison reads every one, because a revision that edits only
#: the words leaves the digits beside them untouched.
_PARENTHETICAL = re.compile(r"\(([^()]{2,80})\)")


def number_of(value: str, normalized: str | float | None = None) -> float | None:
    """The number in a value, however it was written.

    ``parse_number`` refuses "48 750,00 GEL" because of the currency, so the rule's own
    ``normalized`` reading wins where there is one and the first number in the text is the
    fallback. Without this the money column of a comparison is permanently blank.
    """
    if isinstance(normalized, int | float):
        return float(normalized)
    direct = parse_number(value)
    if direct is not None:
        return direct
    found = NUM_IN_TEXT.search(value)
    return parse_number(found.group()) if found else None


@dataclass(frozen=True, slots=True)
class Money:
    """One monetary amount as the document writes it: "100 (ასი) ლარს" is 100.0 GEL."""

    value: float
    currency: str
    #: where it is written, for a finding's anchor
    span: tuple[int, int]
    raw: str


def money_hits(text: str, *, penalties: bool = False) -> list[Money]:
    """Monetary amounts only, in document order, each with its currency and where it is written.

    A currency is required: without it the penalty clauses ("50%", "0.5% per day") come back as
    amounts and a fee comparison reports them as candidate contract values. An amount the rules
    classify as a penalty is left out for the same reason - unless ``penalties`` asks for it: a
    check that gives every amount its own role (``contract_terms.amount_roles``) names a
    penalty instead of losing it.
    """
    out: list[Money] = []
    for h in sorted(find_amounts(text), key=lambda h: (h.start, h.end)):
        if not h.currency or (h.type not in _MONEY_TYPES and not penalties):
            continue
        n = number_of(h.value, h.normalized if isinstance(h.normalized, float) else None)
        if n is not None:
            out.append(Money(value=n, currency=h.currency, span=(h.start, h.end), raw=h.raw_text))
    return out


def money_amounts(text: str) -> set[float]:
    """The values of ``money_hits``, which is what the document comparison compares."""
    return {m.value for m in money_hits(text)}


def word_amounts(text: str) -> list[tuple[int, str]]:
    """Every Georgian numeral written out in a parenthetical, as (value, as written).

    Georgian contracts always restate an amount in words - "100 (ასი) ლარს". A revision that
    edits only the words leaves every digit in the paragraph untouched, so the amount rules see
    no difference at all and the line reads as an unexplained rewrite.
    """
    out: list[tuple[int, str]] = []
    for m in _PARENTHETICAL.finditer(text):
        inner = m.group(1).strip()
        value = parse_georgian_number_words(inner)
        if value is not None:
            out.append((value, inner))
    return out


@dataclass(frozen=True, slots=True)
class DigitsAndWords:
    """One "digits (words)" pair, e.g. "100 (ასი)", and what each half reads as."""

    digits: float
    words: str
    #: the words as a number, or None when they do not read as one
    spelled: int | None
    raw: str
    span: tuple[int, int]


def digits_and_words(text: str) -> list[DigitsAndWords]:
    """Every figure immediately restated in words. Pairs whose digits do not parse are left out."""
    out: list[DigitsAndWords] = []
    for m in _DIGITS_THEN_WORDS.finditer(text):
        digits = parse_number(m.group(1))
        if digits is None:
            continue
        words = m.group(2).strip()
        out.append(
            DigitsAndWords(
                digits=digits,
                words=words,
                spelled=parse_georgian_number_words(words),
                raw=m.group(0).strip(),
                span=m.span(),
            )
        )
    return out
