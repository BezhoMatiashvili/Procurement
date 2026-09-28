"""Spelling checks scoped to the fields we can check authoritatively.

The client asks for მართლწერა (spelling) checking. Two general approaches were tried against
the client's own documents and both were rejected on measurements, recorded here so the choice
is not silently revisited:

1. *Compare the act's vocabulary against the contract's.* Georgian is heavily inflected, so
   "დირექტორი"/"დირექტორის" and "შემკვეთის"/"შემკვეთს" are one edit apart while being perfectly
   correct. On the client's correct act this produced 17 findings — on the one document that
   must produce none. Stemming reduced it to about three, not zero.
2. *Use the hunspell ka_GE dictionary offline.* Its 143k entries are stored with affix flags,
   so a plain wordlist lookup rejects 39% of the words in a correct document; it needs a full
   affix engine, and its coverage of legal/administrative Georgian is unmeasured.

What is reliable is checking the fields where we have an authoritative comparison target: a
name, an address or a project that the contract (or the registry) also states. Every typo the
client planted lives in such a field — a director's name, a month inside a date, a number in
words — and each is caught with no false positives. Free-text spell checking of the remaining
prose is deliberately out of scope; `docs/` records what it would take.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from procurement_agent.extraction.georgian import same_word
from procurement_agent.extraction.normalize import edit_distance, normalize_name

# Below this, two values are different things rather than one thing spelled two ways.
_TYPO_MAX_DISTANCE = 2
_TYPO_MIN_SIMILARITY = 0.6


@dataclass(frozen=True, slots=True)
class Difference:
    """How an observed value differs from the value it should equal."""

    actual: str
    expected: str
    kind: Literal["typo", "different"]
    #: the differing words, as (actual_word, expected_word) pairs
    words: tuple[tuple[str, str], ...]

    @property
    def looks_like_typo(self) -> bool:
        return self.kind == "typo"


def _similarity(a: str, b: str) -> float:
    """Share of the longer word left unchanged. Prefix overlap is not enough: the typo in
    "გტორგი" for "გიორგი" is at the second character, which a prefix measure scores as 0.17."""
    if not a or not b:
        return 0.0
    longest = max(len(a), len(b))
    return 1.0 - edit_distance(a, b, cap=longest) / longest


def _same_words(a: str, b: str) -> bool:
    """The same words in the same order, each in any grammatical form (``same_word``): "ნინო
    კაპანაძის" is "ნინო კაპანაძე", "მთაწმინდიის" is not "მთაწმინდის". Stripping endings
    (``names_match``) took the second for an inflection too."""
    aw, bw = a.split(), b.split()
    return len(aw) == len(bw) and all(same_word(x, y) for x, y in zip(aw, bw, strict=True))


def classify_difference(actual: str, expected: str) -> Difference | None:
    """Compare two values that should be identical. Returns None when they are.

    A small edit distance over a shared prefix reads as a misspelling and the message can name
    the correction; anything larger is reported as a genuinely different value. Either way the
    client is told what the contract says, so the distinction only changes the wording.
    """
    a, b = normalize_name(actual), normalize_name(expected)
    if not a or not b or a == b or _same_words(a, b):
        return None

    aw, bw = a.split(), b.split()
    pairs: list[tuple[str, str]] = []
    if len(aw) == len(bw):
        pairs = [(x, y) for x, y in zip(aw, bw, strict=True) if x != y]
    kind: Literal["typo", "different"] = "different"
    if (
        pairs
        and all(
            edit_distance(x, y, cap=_TYPO_MAX_DISTANCE) <= _TYPO_MAX_DISTANCE
            and _similarity(x, y) >= _TYPO_MIN_SIMILARITY
            for x, y in pairs
        )
    ) or (not pairs and edit_distance(a, b, cap=_TYPO_MAX_DISTANCE) <= _TYPO_MAX_DISTANCE):
        kind = "typo"
    return Difference(actual=actual, expected=expected, kind=kind, words=tuple(pairs))


def best_match(value: str, candidates: list[str]) -> tuple[str, Difference | None] | None:
    """The candidate a value most likely refers to, plus how it differs from it.

    Used to decide whether an act's director is a misspelling of a contract director or a
    different person entirely, without assuming the lists are in the same order.
    """
    if not candidates:
        return None
    scored: list[tuple[int, str]] = []
    for c in candidates:
        d = edit_distance(normalize_name(value), normalize_name(c), cap=99)
        scored.append((d, c))
    scored.sort(key=lambda t: t[0])
    best = scored[0][1]
    return best, classify_difference(value, best)
