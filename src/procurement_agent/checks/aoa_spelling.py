"""Prose spelling (მართლწერა) in the act: a word the dictionary does not know, one letter from a
word it does (AOA.SPELLING, beside the month, number-word and stray-letter rules in ``aoa``).

The client asks for the act's spelling to be checked. Two earlier attempts failed on the client's
correct act (docs/ACCURACY.md): comparing the act's words with the contract's reads every other
case ending as a typo, and the hunspell word list read without its affix rules rejects a third of
a correct document. This reads the same hunspell ka_GE dictionary through spylls, a full affix
engine, and reports a word only when every gate below holds. The correct act must stay clean, so
each gate is there because a correct word got through without it:

* the word is Georgian, four letters or more, and nobody else's: not a name (in quotes, after a
  legal form or a signatory's title, a word of any party, director, address or project the
  anchors read, or a surname by its ending), not in a date or an amount's words, and not already
  reported by the month or script rules;
* the contract does not write it - the documents' own vocabulary, in the dictionary or not;
* the dictionary does not know it in any form;
* it is not a form the dictionary merely lacks: a known word less its final "ი" (the attributive
  "ყოველ ვადაგადაცილებულ დღეზე"), or a known word ending in a vowel plus a whole case ending,
  postposition or particle ("შეწყვეტამდე", "მოლაპარაკებათა");
* one known word is the best correction: one letter away (dropped, added, replaced, or two
  swapped - the edits hunspell's suggester tries first), with the same first letter, and among
  those the one the documents write, then the one keeping the word's ending. A tie reports
  nothing, and so does a participle one vowel from a known one: -ულ-/-ილ- is a usage question
  ("შედგენულია" beside "შედგენილია"), not a slip.

Nothing is fetched at runtime: the dictionary is two files on disk (``Settings``), and without
them the act check says the prose was not checked.
"""

from __future__ import annotations

import bisect
import re
import warnings
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from spylls.hunspell import Dictionary
from spylls.hunspell.algo import permutations
from spylls.hunspell.algo.lookup import AffixForm, Lookup
from spylls.hunspell.algo.trie import Trie

from procurement_agent.checks.amounts import digits_and_words
from procurement_agent.extraction.georgian import damerau, is_georgian_word, words
from procurement_agent.extraction.normalize import QUOTE_CHARS
from procurement_agent.extraction.rules import find_dates
from procurement_agent.extraction.schemas import FactType
from procurement_agent.security.audit import log

if TYPE_CHECKING:
    from procurement_agent.checks.aoa import DocumentView
    from procurement_agent.config import Settings

#: The dictionary's two files, in ``Settings.spelling_dictionary_path``.
DICTIONARY_FILES = ("ka_GE.aff", "ka_GE.dic")
_BASENAME = "ka_GE"
#: Shorter words are left alone: at three letters too many real words sit one letter apart.
MIN_LETTERS = 4
_VOWELS = "აეიოუ"


@dataclass(frozen=True, slots=True)
class SpellingUnavailable:
    """Why the prose was not spell-checked, for the report's skip note."""

    reason_ka: str
    reason_en: str


#: The act checked with no dictionary at all: a caller that did not pass one.
NOT_SUPPLIED = SpellingUnavailable(
    "მართლწერის ლექსიკონი არ არის მიწოდებული; ტექსტის მართლწერა არ შემოწმებულა "
    "(თვეები, თანხები სიტყვიერად და სხვა ანბანის ასოები შემოწმდა)",
    "no spelling dictionary was supplied, so the act's prose was not spell-checked (months, "
    "amounts in words and letters of other alphabets were)",
)


class Lexicon(Protocol):
    """What the prose rule asks of a dictionary."""

    def known(self, word: str) -> bool: ...

    def neighbours(self, word: str) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class ProseTypo:
    """A word of the act, where it is, and the one word it most likely meant."""

    word: str
    start: int
    end: int
    correction: str


#: Distinct words one document may send to the correction search, the costly step (20-200 ms a
#: word). The client's acts need 8 each, their contracts read with no vocabulary at all 20; the
#: cap keeps a long document that is no act from holding a request for minutes.
MAX_SEARCHES = 40


@dataclass(frozen=True, slots=True)
class ProseReading:
    """The act's prose typos, and how many words were left unread past ``MAX_SEARCHES``."""

    typos: list[ProseTypo]
    unsearched: int = 0

    def shortfall(self) -> SpellingUnavailable | None:
        """Why part of the prose went unchecked, for a skip note; None when none did."""
        if not self.unsearched:
            return None
        return SpellingUnavailable(
            f"ტექსტის მართლწერა ნაწილობრივ შემოწმდა: {self.unsearched} სიტყვა აღარ შემოწმებულა "
            f"(ერთ დოკუმენტში მაქსიმუმ {MAX_SEARCHES})",
            f"the act's prose was spell-checked in part: {self.unsearched} more words the "
            f"dictionary does not know were not checked (at most {MAX_SEARCHES} per document)",
        )


# --- the dictionary -----------------------------------------------------------------------------

#: Words remembered per question before a cache starts over: a server checks acts for months,
#: and a document's words are asked about again within it and in its next revision.
_CACHE_SIZE = 50_000


class Speller:
    """The ka_GE dictionary through spylls, fast enough to try every one-letter edit of a word.

    On its own spylls spends 5-50 ms deciding that a string is not a word, because the Georgian
    affix table has over a thousand suffixes and most of them chain; one word's edits (600-1100 of
    them) then take seconds. Two things make it fast without changing an answer, both held to
    plain spylls by ``agrees_with_spylls`` in the tests:

    * ``_Stems``: a string is a word only if some split of it by the affix rules leaves a stem
      the dictionary lists. That is checked first with string operations, flags ignored - more
      splits than spylls allows, so a string it rejects spylls rejects too;
    * ``_IndexedLookup``: spylls' second-suffix scan reads only the suffixes that carry the flag
      it needs, instead of every suffix the word ends with.
    """

    def __init__(self, dictionary: Any) -> None:
        aff, dic = dictionary.aff, dictionary.dic
        self._plain = dictionary
        self._dic = dic
        self._lookup = _IndexedLookup(aff, dic)
        self._stems = _Stems(aff)
        self._letters: str = aff.TRY
        self._forbidden: str | None = aff.FORBIDDENWORD
        # _Stems models plain affix splits; anything else a dictionary can declare turns it off
        self._prefilter = not (
            aff.COMPOUNDFLAG
            or aff.COMPOUNDBEGIN
            or aff.COMPOUNDMIDDLE
            or aff.COMPOUNDEND
            or aff.COMPOUNDRULE
            or aff.ICONV
            or aff.IGNORE
            or aff.COMPLEXPREFIXES
        )
        self._known: dict[str, bool] = {}
        self._near: dict[str, tuple[str, ...]] = {}

    @classmethod
    def from_directory(cls, directory: Path) -> Speller:
        return cls(Dictionary.from_files(str(directory / _BASENAME)))

    def known(self, word: str) -> bool:
        """Whether the dictionary knows ``word`` in any of its forms (spylls' lookup)."""
        hit = self._known.get(word)
        if hit is None:
            hit = self._could_be(word) and bool(self._lookup(word))
            _remember(self._known, word, hit)
        return hit

    def neighbours(self, word: str) -> tuple[str, ...]:
        """The known words one edit from ``word``: two neighbouring letters swapped, a letter
        dropped, or one of the dictionary's letters added or put in place of one - the edits
        hunspell's suggester tries first - each accepted as its suggester accepts one."""
        hit = self._near.get(word)
        if hit is None:
            hit = tuple(c for c in dict.fromkeys(self._edits(word)) if self._suggestable(c))
            _remember(self._near, word, hit)
        return hit

    def agrees_with_spylls(self, candidates: Iterable[str]) -> list[str]:
        """The strings on which this fast path and plain spylls disagree, as a word or as a
        suggestion - none, when the shortcuts are sound."""
        plain = self._plain.lookuper
        out = []
        for w in candidates:
            as_word = bool(self._plain.lookup(w))
            forms = plain.good_forms(
                w, capitalization=False, allow_nosuggest=False, compound_forms=False
            )
            as_suggestion = not self._is_forbidden(w) and any(forms)
            if (self.known(w), self._suggestable(w)) != (as_word, as_suggestion):
                out.append(w)
        return out

    def _edits(self, word: str) -> Iterator[str]:
        edits = chain(
            permutations.swapchar(word),
            permutations.extrachar(word),
            permutations.forgotchar(word, self._letters),
            permutations.badchar(word, self._letters),
        )
        # swapchar also swaps twice in a short word, which is two edits
        return (e for e in edits if e != word and damerau(e, word, cap=1) == 1)

    def _suggestable(self, word: str) -> bool:
        """spylls' test for an edit it could suggest (``Suggest.suggestions``): a word in some
        form, not marked never to be suggested, not a forbidden one."""
        return (
            self._could_be(word)
            and not self._is_forbidden(word)
            and any(
                self._lookup.good_forms(
                    word, capitalization=False, allow_nosuggest=False, compound_forms=False
                )
            )
        )

    def _is_forbidden(self, word: str) -> bool:
        return bool(self._forbidden) and bool(self._dic.has_flag(word, self._forbidden))

    def _could_be(self, word: str) -> bool:
        return not self._prefilter or any(s in self._dic.index for s in self._stems.of(word))


def _remember(cache: dict[str, Any], key: str, value: Any) -> None:
    if len(cache) >= _CACHE_SIZE:
        cache.clear()
    cache[key] = value


class _Stems:
    """Every stem an affix split of a string leaves, flags aside: a prefix, then up to two
    suffixes, the way spylls' ``Lookup.produce_affix_forms`` splits one. A stem may come out more
    than once; a string none of whose stems the dictionary lists is no word."""

    def __init__(self, aff: Any) -> None:
        suffixes: dict[str, dict[tuple[str, str], re.Pattern[str]]] = defaultdict(dict)
        for s in chain.from_iterable(aff.SFX.values()):
            suffixes[s.add][s.strip, s.condition] = s.cond_regexp
        self._suffixes = {
            add: tuple((strip, cond) for (strip, _), cond in rules.items())
            for add, rules in suffixes.items()
        }
        self._longest = max(map(len, self._suffixes), default=0)
        prefixes: dict[tuple[str, str, str], re.Pattern[str]] = {}
        for p in chain.from_iterable(aff.PFX.values()):
            prefixes[p.add, p.strip, p.condition] = p.cond_regexp
        self._prefixes = tuple((add, strip, cond) for (add, strip, _), cond in prefixes.items())

    def of(self, word: str) -> Iterator[str]:
        expanded: set[str] = set()
        yield word
        yield from self._desuffix(word, expanded)
        for add, strip, cond in self._prefixes:
            if word.startswith(add):
                stem = strip + word[len(add) :]
                if cond.search(stem):
                    yield stem
                    yield from self._desuffix(stem, expanded)

    def _desuffix(self, word: str, expanded: set[str]) -> Iterator[str]:
        """One suffix off, then a second: hunspell strips at most two. A stem's second level is
        the same wherever it was reached from, so it is gone through once."""
        for stem in self._strip(word):
            yield stem
            if stem not in expanded:
                expanded.add(stem)
                yield from self._strip(stem)

    def _strip(self, word: str) -> Iterator[str]:
        n = len(word)
        for k in range(min(self._longest, n) + 1):
            for strip, cond in self._suffixes.get(word[n - k :], ()):
                stem = word[: n - k] + strip
                if cond.search(stem):
                    yield stem


class _IndexedLookup(Lookup):  # type: ignore[misc]  # spylls ships no type information
    """spylls' ``Lookup``, its second-suffix scan indexed by flag.

    A second suffix must carry the first one's flag, and spylls finds one by reading every
    suffix the stem ends with and filtering on that flag: most of the lookup's time on this
    dictionary. Here the suffixes are indexed by the flags they carry, built in spylls' own order,
    so the same forms come out in the same order.
    """

    def __init__(self, aff: Any, dic: Any) -> None:
        super().__init__(aff, dic)
        by_flag: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
        for suffix in chain.from_iterable(aff.SFX.values()):
            for flag in suffix.flags:
                by_flag[flag][suffix.add[::-1]].append(suffix)
        self._carrying = {flag: Trie(index) for flag, index in by_flag.items()}

    def desuffix(
        self,
        word: str,
        required_flags: list[str],
        forbidden_flags: list[str],
        nested: bool = False,
        crossproduct: bool = False,
    ) -> Iterator[Any]:
        if nested and required_flags:
            return self._second_suffix(word, required_flags, forbidden_flags, crossproduct)
        forms: Iterator[Any] = super().desuffix(
            word, required_flags, forbidden_flags, nested, crossproduct
        )
        return forms

    def _second_suffix(
        self, word: str, required: list[str], forbidden: list[str], crossproduct: bool
    ) -> Iterator[Any]:
        trie = self._carrying.get(required[0])
        if trie is None:
            return
        for suffix in trie.lookup(word[::-1]):
            if crossproduct and not suffix.crossproduct:
                continue
            if not all(f in suffix.flags for f in required):
                continue
            if any(f in suffix.flags for f in forbidden):
                continue
            stem = suffix.replace_regexp.sub(suffix.strip, word)
            if suffix.cond_regexp.search(stem):
                yield AffixForm(word, stem, suffix=suffix)


def load_speller(directory: Path) -> Speller | SpellingUnavailable:
    """The dictionary in ``directory``, loaded once per process; never raises. Missing or
    unreadable files are named in the reason, so the report says what to put where."""
    missing = [name for name in DICTIONARY_FILES if not (directory / name).is_file()]
    if missing:
        paths = ", ".join(str(directory / name) for name in missing)
        return SpellingUnavailable(
            f"მართლწერის ლექსიკონი ვერ მოიძებნა ({paths}); ტექსტის მართლწერა არ შემოწმებულა",
            f"the spelling dictionary is missing ({paths}; scripts/fetch_spelling_dictionary.py "
            "fetches it), so the act's prose was not spell-checked",
        )
    with warnings.catch_warnings():
        # spylls leaves a file open when it fails part-way through reading it; the file closes
        # as the exception goes, and the warning that it was left open says nothing to act on
        warnings.simplefilter("ignore", ResourceWarning)
        try:
            return _load(directory.resolve())
        except Exception as e:  # a broken dictionary must not fail the act check
            error = repr(e)
    log.warning("spelling_dictionary_unreadable", path=str(directory), error=error)
    return SpellingUnavailable(
        f"მართლწერის ლექსიკონი ვერ წაიკითხა ({directory}); ტექსტის მართლწერა არ შემოწმებულა",
        f"the spelling dictionary in {directory} could not be read, so the act's prose was not "
        "spell-checked",
    )


@lru_cache(maxsize=4)
def _load(directory: Path) -> Speller:
    return Speller.from_directory(directory)


def open_speller(settings: Settings) -> Speller | SpellingUnavailable:
    """The dictionary the settings name, or why the prose check will not run."""
    if not settings.prose_spelling_enabled:
        return SpellingUnavailable(
            "ტექსტის მართლწერის შემოწმება გამორთულია (PA_PROSE_SPELLING_ENABLED)",
            "prose spelling is turned off (PA_PROSE_SPELLING_ENABLED=false)",
        )
    return load_speller(settings.spelling_dictionary_path)


# --- the rule -----------------------------------------------------------------------------------


def prose_typos(
    aoa: DocumentView,
    contract: DocumentView | None,
    lexicon: Lexicon,
    *,
    taken: Iterable[tuple[int, int]] = (),
    limit: int = MAX_SEARCHES,
) -> ProseReading:
    """The act's prose typos, by the gates in the module docstring, cheapest first: the
    dictionary is asked only about words nothing else accounts for. ``taken`` are the spans
    other rules have already reported; ``limit`` caps the distinct words searched for a
    correction, in the order the act writes them."""
    text = aoa.text
    contract_words = (
        frozenset(w for w, _, _ in words(contract.text)) if contract is not None else frozenset()
    )
    # a correction the documents write is the likelier one
    written = contract_words | {w for w, _, _ in words(text)}
    names = _name_words(aoa)
    fenced = _Spans([*_protected_spans(aoa), *taken])
    corrections: dict[str, str | None] = {}
    unsearched: set[str] = set()
    out: list[ProseTypo] = []
    for word, start, end in words(text):
        if len(word) < MIN_LETTERS or not is_georgian_word(word):
            continue
        if word in contract_words or word in names or _SURNAME.search(word):
            continue
        if fenced.touches(start, end):
            continue
        if lexicon.known(word) or _lacked_form(word, lexicon):
            continue
        if word not in corrections:
            if len(corrections) >= limit:
                unsearched.add(word)
                continue
            corrections[word] = _correction(word, lexicon, written)
        correction = corrections[word]
        if correction is not None:
            out.append(ProseTypo(word, start, end, correction))
    return ProseReading(out, len(unsearched))


def _lacked_form(word: str, lexicon: Lexicon) -> bool:
    """A form of a known word the dictionary does not list: the attributive, which drops a
    final "ი" before its noun ("ყოველ ვადაგადაცილებულ დღეზე"), or a vowel stem with an ending
    that attaches to it unchanged ("შეწყვეტა" + "მდე", "მოლაპარაკება" + "თა").

    A final "ი" is no vowel stem: it is the nominative ending of a consonant stem, which the
    other cases replace ("შემსრულებელი" -> "შემსრულებლის", "შემსრულებელს"), so "შემსრულებელი"
    + "ს" is the misspelling itself. Only the particles follow it as they do any whole word.
    The one-letter cases (-ს, -მ, -დ) are not tried: the dictionary spells those out for every
    vowel stem it has, so one of them it lacks is a letter added in error ("შესრულდად")."""
    if lexicon.known(word + "ი"):
        return True
    for ending in _WHOLE_ENDINGS:
        base = word[: -len(ending)]
        stems = _VOWELS if ending in _PARTICLES else _VOWEL_STEMS
        if word.endswith(ending) and len(base) >= 3 and base[-1] in stems and lexicon.known(base):
            return True
    return False


_VOWEL_STEMS = "აეოუ"
#: "-ც" (also) and "-ვე" (the same), which follow a whole word, whatever its ending.
_PARTICLES = ("ც", "ვე")
#: Endings a vowel stem takes as they are: the postpositions on the dative or the genitive, the
#: instrumental, the old plural, and the particles. Longest first.
_WHOLE_ENDINGS = (
    "სათვის", "სადმი", "სთვის", "სთანაც", "სთან", "სგან", "სკენ", "დანაც", "დან", "მდე", "ში",
    "ზე", "თი", "თა", "ნი", *_PARTICLES,
)  # fmt: skip


def _correction(word: str, lexicon: Lexicon, written: frozenset[str] | set[str]) -> str | None:
    """The one known word ``word`` most likely meant, or None when there is none or two are as
    likely: a wrong correction is worse than none."""
    near = lexicon.neighbours(word)
    if any(_participle_vowel(word, other) for other in near):
        return None
    # the first letter is rarely the mistyped one; a word that differs there is another word
    near = tuple(c for c in near if c[:1] == word[:1])
    if not near:
        return None
    ranked = sorted(((c in written, _shared_ending(word, c)), c) for c in near)
    best = ranked[-1][0]
    top = [c for rank, c in ranked if rank == best]
    return top[0] if len(top) == 1 else None


def _participle_vowel(word: str, other: str) -> bool:
    """-ულ- against -ილ-, the one letter the two differ in: "შედგენულია" / "შედგენილია"."""
    if len(word) != len(other):
        return False
    diff = [i for i, (a, b) in enumerate(zip(word, other, strict=True)) if a != b]
    return (
        len(diff) == 1
        and {word[diff[0]], other[diff[0]]} == {"უ", "ი"}
        and word[diff[0] + 1 : diff[0] + 2] == "ლ"
    )


def _shared_ending(a: str, b: str) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


# --- what is not prose --------------------------------------------------------------------------

#: A Georgian surname by its ending - "-შვილი", "-ძე" and their case forms - which the dictionary
#: mostly lacks. Not needed on the client's acts, where every name sits in a labelled field; it
#: keeps a name written outside one from being read as a misspelling.
_SURNAME = re.compile(r"(?:შვილ(?:ი|ის|მა|ს|ით|თან|ისთვის)|ძე(?:ს|მ|სთან)?|ძის|ძით|ძისთვის)$")
_LEGAL_FORM = (
    r"შპს|სს|კს|სპს|ააიპ|სსიპ|შ\.პ\.ს\.?|ს\.ს\.?|ი\s*/\s*მ|ი\.მ\.|ფ\s*/\s*პ|ფ\.პ\.|"
    r"ინდ(?:\.|ივიდუალური)\s*მეწარმე|ფიზიკური\s+პირი"
)
#: The titles and labels a person's name follows in a signature or requisites block.
_SIGNATORY = r"დირექტორ(?:ი|ის|მა)?|წარმომადგენ(?:ელი|ლის|ელმა)|ხელმოწერა"
_Q = re.escape(QUOTE_CHARS)
#: The name after a legal form, quoted or not - up to three words - and after a signatory's
#: title: a first name and a surname, and no further ("დირექტორის ... სახით" is prose again).
_NAME_AFTER = (
    re.compile(
        rf"(?<![ა-ჰ])(?:{_LEGAL_FORM})(?![ა-ჰ])[\s:,-]*[{_Q}]?\s*((?:[ა-ჰ]+\.?\s+){{0,2}}[ა-ჰ]+)"
    ),
    re.compile(rf"(?<![ა-ჰ])(?:{_SIGNATORY})(?![ა-ჰ])[\s:,-]*((?:[ა-ჰ]+\.?\s+)?[ა-ჰ]+)"),
)
_NAME_TYPES = frozenset(
    {FactType.party_name, FactType.party_director, FactType.party_address, FactType.project_name}
)


def _name_words(aoa: DocumentView) -> frozenset[str]:
    """Every word of a party's name, director or address, or of a project, wherever it is
    written. The anchors read a name once, at one place; its other mentions are the same name."""
    values = [h.value for h in aoa.hits() if h.type in _NAME_TYPES]
    for p in aoa.parties:
        values += [p.name or "", p.address or "", *p.directors]
    return frozenset(w for v in values for w, _, _ in words(v))


def _protected_spans(aoa: DocumentView) -> list[tuple[int, int]]:
    """Where the text is not prose: quotes, names after a legal form or a title, the anchored
    fields, dates, and a figure's words ("100 (ასი)") - each read by a rule of its own."""
    text = aoa.text
    spans = quoted_spans(text)
    spans += [m.span(1) for pattern in _NAME_AFTER for m in pattern.finditer(text)]
    spans += [(h.start, h.end) for h in aoa.hits()]
    spans += [(h.start, h.end) for h in find_dates(text)]
    spans += [d.words_span for d in digits_and_words(text)]
    return spans


class _Spans:
    """Spans to stay out of, merged, for overlap tests by bisection."""

    def __init__(self, spans: Iterable[tuple[int, int]]) -> None:
        merged: list[list[int]] = []
        for a, b in sorted(s for s in spans if s[0] < s[1]):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        self._starts = [a for a, _ in merged]
        self._ends = [b for _, b in merged]

    def touches(self, start: int, end: int) -> bool:
        i = bisect.bisect_right(self._starts, end - 1) - 1
        return i >= 0 and self._ends[i] > start


#: Marks that only open a quotation (the low double quote and the left guillemets), marks that
#: only close one (the right guillemets), and the curly and straight quotes, which open or close
#: by where they stand. Written as code points: several look like other punctuation.
_OPENS = "".join(map(chr, (0x201E, 0x00AB, 0x2039)))
_CLOSES = "".join(map(chr, (0x00BB, 0x203A)))
_EITHER = "".join(map(chr, (0x201C, 0x201D, 0x0022, 0x2018, 0x2019)))
#: How far a closing mark may be from its opening one: a name or a defined term, not a clause.
_QUOTE_REACH = 90


def quoted_spans(text: str) -> list[tuple[int, int]]:
    """What ``text`` puts in quotes, each pair matched where it stands: an opening mark before a
    word, a closing one after it, at most 90 characters apart. A mark with no partner encloses
    nothing - pairing marks by count would put everything after one stray quote inside quotes.
    """
    out: list[tuple[int, int]] = []
    opened: int | None = None
    i, n = 0, len(text)
    while i < n:
        ch, width = text[i], 1
        before = text[i - 1] if i else " "
        after = text[i + 1] if i + 1 < n else " "
        if text.startswith(",,", i) and text[i + 2 : i + 3].isalnum():
            kind, width = "open", 2
        elif ch in _OPENS:
            kind = "open"
        elif ch in _CLOSES:
            kind = "close"
        elif ch in _EITHER:
            # an opening mark stands before a word, a closing one after it
            opens = (before.isspace() or before in "([{") and after.isalnum()
            closes = (before.isalnum() or before in ".!?)") and not after.isalnum()
            if opens != closes:
                kind = "open" if opens else "close"
            else:  # spaced out, as in „ მხარე “: it closes whatever is open
                kind = "close" if opened is not None else "open"
        else:
            i += 1
            continue
        if kind == "open":
            opened = i + width
        elif opened is not None:
            if i - opened <= _QUOTE_REACH:
                out.append((opened, i))
            opened = None
        i += width
    return out
