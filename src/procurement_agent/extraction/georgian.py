"""Georgian morphology the checks need, and nothing more: names in the right case, months spelled
right, numbers in words, words mixing alphabets, dates in a document's own style.

Every rule that compares an act with its contract, and every correction it proposes, goes
through here. Before this module a correction was copied from wherever the contract happened to
state the value - usually its preamble, in the genitive - so a Word comment on "გიორგი ჭიჭინია"
in a signature block said "should be: გიორგი ჭიჭინაძის", which is itself wrong in that place.

Georgian inflects a noun phrase on its last word only ("დირექტორის გიორგი ჭიჭინაძის სახით": the
first name stays in the nominative). The case endings used in these documents are few, so the
morphology is a table, not a model:

* consonant stems, nominative ``-ი``: ``ბერიშვილი`` -> ``ბერიშვილის``, ``ბერიშვილით``;
* the truncating ``-ძე`` surnames: ``ჭიჭინაძე`` -> ``ჭიჭინაძის`` but ``ჭიჭინაძეს``;
* vowel stems in ``-ა -ე -ო -უ`` (and first names in ``-ი`` such as ``გიორგი``): ``ლომია`` ->
  ``ლომიას``;
* postpositions on top: ``-ისთვის``, ``-ში``, ``-ზე``, ``-თან``, ``-იდან``, ``-ამდე``.

Pure and dependency-free, like ``normalize``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from procurement_agent.extraction.normalize import (
    QUOTE_CHARS,
    normalize_confusables,
    normalize_spaces,
    parse_georgian_number_words,
)

# --- alphabets ----------------------------------------------------------------------------------

#: Mkhedruli letters, the script every modern Georgian document is written in (U+10D0-U+10FA).
#: Built from code points: the characters themselves read like other scripts' letters.
GEORGIAN = f"{chr(0x10D0)}-{chr(0x10FA)}"
_GEO = re.compile(f"[{GEORGIAN}]")
_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LETTER = f"{GEORGIAN}A-Za-zЀ-ӿ"
#: A run of letters of any of the three scripts, with digits allowed between letters: the unit a
#: reader calls a word. Punctuation, quotes, slashes and hyphens end it, so "GEO/ENG", "ს/კ" and
#: "PDF-ის" are separate words and nothing to report.
_TOKEN = re.compile(rf"[{_LETTER}](?:[{_LETTER}0-9]*[{_LETTER}])?")

Case = Literal[
    "nom", "erg", "dat", "gen", "ins", "adv",
    "gen_tvis", "gen_gan", "gen_ken", "dat_shi", "dat_ze", "dat_tan", "ins_dan", "adv_mde",
]  # fmt: skip


def words(text: str) -> list[tuple[str, int, int]]:
    """Every word of ``text`` with its span."""
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]


def is_georgian_word(word: str) -> bool:
    return bool(word) and all(_GEO.match(ch) for ch in word)


# --- declension ---------------------------------------------------------------------------------

#: Endings that mark a case or a postposition on the last word of a phrase, longest first, with
#: the case they mark. Genitive-based postpositions attach to the genitive ("-ის" + "თვის"); the
#: dative-based ones replace a consonant stem's "-ს" ("სახლში") but follow a vowel stem's
#: ("ლომიასთან").
_ENDINGS: tuple[tuple[str, Case], ...] = (
    ("ისთვის", "gen_tvis"),
    ("სთვის", "gen_tvis"),
    ("ისგან", "gen_gan"),
    ("სგან", "gen_gan"),
    ("ისკენ", "gen_ken"),
    ("სკენ", "gen_ken"),
    ("იდან", "ins_dan"),
    ("დან", "ins_dan"),
    ("ამდე", "adv_mde"),
    ("მდე", "adv_mde"),
    ("სთან", "dat_tan"),
    ("თან", "dat_tan"),
    ("ში", "dat_shi"),
    ("ზე", "dat_ze"),
    ("ის", "gen"),
    ("ით", "ins"),
    ("თი", "ins"),
    ("ად", "adv"),
    ("მა", "erg"),
)

#: First names that end in "-ი" yet decline like vowel stems ("გიორგის", "გიორგიმ"). A name not
#: listed is declined as a consonant stem, which differs only in the ergative and instrumental.
_I_VOWEL_NAMES = frozenset(
    {
        "გიორგი", "ირაკლი", "გიგი", "ლევანი", "დავითი", "ნიკოლოზი", "თამარი", "მარიამი",
        "ანი", "ზურაბი", "ვახტანგი",
    }
)  # fmt: skip

_CONSONANT_CASES: dict[Case, str] = {
    "erg": "მა",
    "dat": "ს",
    "gen": "ის",
    "ins": "ით",
    "adv": "ად",
    "gen_tvis": "ისთვის",
    "gen_gan": "ისგან",
    "gen_ken": "ისკენ",
    "dat_shi": "ში",
    "dat_ze": "ზე",
    "dat_tan": "თან",
    "ins_dan": "იდან",
    "adv_mde": "ამდე",
}
_VOWEL_CASES: dict[Case, str] = {
    "erg": "მ",
    "dat": "ს",
    "gen": "ს",
    "ins": "თი",
    "adv": "დ",
    "gen_tvis": "სთვის",
    "gen_gan": "სგან",
    "gen_ken": "სკენ",
    "dat_shi": "ში",
    "dat_ze": "ზე",
    "dat_tan": "სთან",
    "ins_dan": "დან",
    "adv_mde": "მდე",
}
#: The truncating "-ძე" surnames drop their "-ე" before the endings that start with "-ი".
_TRUNCATED: frozenset[Case] = frozenset({"gen", "ins", "gen_tvis", "gen_gan", "gen_ken", "ins_dan"})


_VOWELS = "აეიოუ"


def _stem_class(lemma: str) -> Literal["consonant", "bare", "truncating", "vowel"]:
    if lemma.endswith("ძე"):
        return "truncating"
    if lemma.endswith("ი") and lemma not in _I_VOWEL_NAMES and len(lemma) > 2:
        return "consonant"
    if lemma[-1] not in _VOWELS:
        return "bare"  # a loanword that ends in a consonant: "დეველოპმენტ", "სილქ"
    return "vowel"


def inflect_word(lemma: str, case: Case) -> str:
    """``lemma`` (nominative) in ``case``."""
    if not lemma or case == "nom" or not is_georgian_word(lemma):
        return lemma
    cls = _stem_class(lemma)
    if cls == "consonant":
        return lemma[:-1] + _CONSONANT_CASES[case]
    if cls == "bare":
        return lemma + _CONSONANT_CASES[case]
    if cls == "truncating" and case in _TRUNCATED:
        return lemma[:-1] + _CONSONANT_CASES[case]
    return lemma + _VOWEL_CASES[case]


def case_of(word: str) -> Case:
    """The case a word is written in, read off its ending. Nominative when nothing matches: a
    vowel-final word, or a consonant-final loanword stem ("სილქ")."""
    if not is_georgian_word(word):
        return "nom"
    for ending, case in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return case
    if len(word) >= 4 and word[-1] == "ს" and word[-2] in "აეოუი":
        return "gen"  # a vowel stem's genitive or dative, which are the same word
    return "nom"


def lemma_word(word: str) -> str:
    """The nominative of one inflected word: ``ტოგონიძის`` -> ``ტოგონიძე``,
    ``თაუერსისთვის`` -> ``თაუერსი``, ``ლომიას`` -> ``ლომია``, ``ბერიშვილით`` -> ``ბერიშვილი``.
    """
    if not is_georgian_word(word):
        return word
    for lemma_candidate in _lemma_candidates(word):
        if inflect_word(lemma_candidate, case_of(word)) == word:
            return lemma_candidate
    return word


def _lemma_candidates(word: str) -> list[str]:
    """Nominatives ``word`` could be a form of, most likely first."""
    case = case_of(word)
    if case == "nom":
        return [word]
    out: list[str] = []
    for ending, c in _ENDINGS:
        if c != case or not word.endswith(ending) or len(word) - len(ending) < 3:
            continue
        stem = word[: len(word) - len(ending)]
        # consonant stem ("ბერიშვილ" + "ი"), truncating ("ტოგონიძ" + "ე"), vowel stem as is
        out.extend([stem + "ე", stem + "ი", stem] if stem.endswith("ძ") else [stem + "ი", stem])
    if case == "gen" and word.endswith("ს"):
        out.append(word[:-1])  # vowel stem + "-ს"
    return list(dict.fromkeys(out))


def _split_quotes(phrase: str) -> tuple[str, str, str]:
    """Leading quotes, the phrase, trailing quotes: „სილქ თაუერსისთვის“ -> („, ..., “)."""
    q = re.escape(QUOTE_CHARS)
    m = re.fullmatch(rf"([{q}\s]*)(.*?)([{q}\s]*)", phrase)
    if m is None:  # pragma: no cover - the pattern matches every string
        return "", phrase, ""
    return m.group(1), m.group(2), m.group(3)


def lemma(phrase: str) -> str:
    """A name or a project name in the nominative: only its last word inflects."""
    lead, core, trail = _split_quotes(normalize_spaces(phrase))
    parts = core.split()
    if not parts:
        return phrase
    parts[-1] = lemma_word(parts[-1])
    return lead + " ".join(parts) + trail


def inflect(phrase_lemma: str, case: Case) -> str:
    lead, core, trail = _split_quotes(normalize_spaces(phrase_lemma))
    parts = core.split()
    if not parts:
        return phrase_lemma
    parts[-1] = inflect_word(parts[-1], case)
    return lead + " ".join(parts) + trail


def phrase_case(phrase: str) -> Case:
    _, core, _ = _split_quotes(normalize_spaces(phrase))
    parts = core.split()
    return case_of(parts[-1]) if parts else "nom"


def in_case_of(wrong: str, right: str) -> str:
    """``right`` (in any case) rewritten in the case ``wrong`` is written in.

    What a correction must say: the act's own grammar, the contract's name. ``wrong`` may be
    misspelled, which does not matter - only its ending is read.

    >>> in_case_of("გიორგი ჭიჭინია", "გიორგი ჭიჭინაძის")
    'გიორგი ჭიჭინაძე'
    """
    return inflect(lemma(right), phrase_case(wrong))


def prefer_nominative(forms: list[str]) -> str | None:
    """Of several forms of one name, the one a document writes in the nominative, if any."""
    for f in forms:
        if phrase_case(f) == "nom":
            return f
    return forms[0] if forms else None


# --- months -------------------------------------------------------------------------------------

#: Nominative and the stem the oblique cases build on ("სექტემბრის", "სექტემბრიდან"); the
#: dative and the dative-based postpositions keep the nominative's stem ("სექტემბერს").
_MONTHS: tuple[tuple[int, str, str], ...] = (
    (1, "იანვარი", "იანვრ"),
    (2, "თებერვალი", "თებერვლ"),
    (3, "მარტი", "მარტ"),
    (4, "აპრილი", "აპრილ"),
    (5, "მაისი", "მაის"),
    (6, "ივნისი", "ივნის"),
    (7, "ივლისი", "ივლის"),
    (8, "აგვისტო", "აგვისტო"),
    (9, "სექტემბერი", "სექტემბრ"),
    (10, "ოქტომბერი", "ოქტომბრ"),
    (11, "ნოემბერი", "ნოემბრ"),
    (12, "დეკემბერი", "დეკემბრ"),
)
#: Particles a form may carry: "-ვე" (the same), "-ც" (also).
_PARTICLES = ("", "ვე", "ც")


def _month_forms(nominative: str, oblique: str) -> dict[str, Case]:
    if nominative.endswith("ო"):  # the one vowel stem, აგვისტო
        base: dict[str, Case] = {nominative: "nom"}
        base.update({nominative + e: c for c, e in _VOWEL_CASES.items() if c != "dat"})
    else:
        full = nominative[:-1]  # the stem the dative keeps: სექტემბერ
        base = {
            nominative: "nom",
            full + "მა": "erg",
            full + "ს": "dat",
            oblique + "ის": "gen",
            oblique + "ისა": "gen",
            oblique + "ით": "ins",
            oblique + "ად": "adv",
            oblique + "ისთვის": "gen_tvis",
            oblique + "ისგან": "gen_gan",
            oblique + "ისკენ": "gen_ken",
            full + "ში": "dat_shi",
            full + "ზე": "dat_ze",
            full + "თან": "dat_tan",
            oblique + "იდან": "ins_dan",
            oblique + "ამდე": "adv_mde",
        }
    return {form + p: case for form, case in base.items() for p in _PARTICLES}


#: Every correct form of every month, with the month and the case it is in.
MONTH_FORMS: dict[str, tuple[int, Case]] = {
    form: (month, case)
    for month, nom, obl in _MONTHS
    for form, case in _month_forms(nom, obl).items()
}
_MONTH_NOMINATIVE = {month: nom for month, nom, _ in _MONTHS}
#: How far a word may be from a month form and still be that month misspelled. Two edits cover
#: a typo in the stem plus one in the ending ("აგვუისტას" for "აგვისტოს").
_MONTH_TYPO_DISTANCE = 2
_MONTH_MIN_LEN = 4
#: Ordinary words one edit from a month form: "მარტო" (alone), "მარტივი" (simple).
_NOT_MONTHS = frozenset({"მარტო", "მარტოდ", "მარტივ", "მარტივი", "მარტივად", "მარტივს"})


def month_form(month: int, case: Case) -> str:
    """The plain form of ``month`` in ``case`` (no particle, the short genitive)."""
    for form, (m, c) in MONTH_FORMS.items():
        if m == month and c == case and not form.endswith(("ვე", "ც", "ისა")):
            return form
    return _MONTH_NOMINATIVE[month]


@dataclass(frozen=True, slots=True)
class MonthTypo:
    word: str
    month: int
    #: the correct form the word was meant to be, or None when two months are equally close
    #: ("ივმისი": June or July?) - a wrong correction is worse than none
    correction: str | None


def month_typo(word: str) -> MonthTypo | None:
    """``word`` as a misspelled month, or None when it is a correct month form or no month.

    Unlike a prefix test this sees a typo after the stem - "სექტემბრდან" for "სექტემბრიდან",
    "ოქტომბერის" for "ოქტომბრის" - because every correct form is listed. Callers look only at
    words in a date, so "მარტო" (alone) is never read as a misspelled March.
    """
    low = word.lower()
    if (
        len(low) < _MONTH_MIN_LEN
        or low in MONTH_FORMS
        or low in _NOT_MONTHS
        or not is_georgian_word(low)
    ):
        return None
    best: dict[int, tuple[int, int, str]] = {}
    for form, (month, _) in MONTH_FORMS.items():
        if abs(len(form) - len(low)) > _MONTH_TYPO_DISTANCE:
            continue
        d = damerau(low, form, cap=_MONTH_TYPO_DISTANCE)
        if d > _MONTH_TYPO_DISTANCE:
            continue
        # At equal distance the form ending like the word wins: "ოქტომბერის" is one edit from
        # both "ოქტომბერი" and "ოქტომბრის", and it was meant as the genitive.
        rank = (d, -_common_suffix(low, form))
        if month not in best or rank < best[month][:2]:
            best[month] = (*rank, form)
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda kv: kv[1][:2])
    month, (d, _, form) = ranked[0]
    # A short month is within two edits of ordinary words: one edit only, and the word keeps
    # the month's first two letters.
    if len(_MONTH_NOMINATIVE[month]) <= 6 and (d > 1 or low[:2] != form[:2]):
        return None
    if low[:1] != form[:1]:
        return None
    ambiguous = len(ranked) > 1 and ranked[1][1][0] == d
    return MonthTypo(word=word, month=month, correction=None if ambiguous else form)


def _common_suffix(a: str, b: str) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def damerau(a: str, b: str, *, cap: int = 3) -> int:
    """Optimal-string-alignment distance: Levenshtein plus the swap of two neighbours, which is
    how "აგივსტო" differs from "აგვისტო" in one keystroke rather than two."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


# --- dates in a document's own style ------------------------------------------------------------

_STYLE_NUMERIC = re.compile(r"^\s*(\d{1,2})([./-])(\d{1,2})\2(\d{4})")
_STYLE_ISO = re.compile(r"^\s*\d{4}-\d{2}-\d{2}")
_STYLE_YMD = re.compile(rf"^\s*(\d{{4}})(\s*(?:წლის|წ\.)\s*)(\d{{1,2}})(\s*)([{GEORGIAN}]+)")
_STYLE_DMY = re.compile(
    rf"^\s*(\d{{1,2}})(\s*)([{GEORGIAN}]+)(,?\s*)(\d{{4}})(\s*(?:წელი|წლის|წ\.)?)"
)


def render_date_like(d: date, example: str) -> str:
    """``d`` written the way ``example`` writes a date, keeping its month's case.

    "2026 წლის 5 სექტემბერი" as the example gives "2026 წლის 31 აგვისტო"; "05.09.2026" gives
    "31.08.2026". An example in no recognised style yields the ISO date.
    """
    ex = normalize_spaces(example)
    if m := _STYLE_NUMERIC.match(ex):
        day = f"{d.day:02d}" if len(m.group(1)) == 2 else str(d.day)
        month = f"{d.month:02d}" if len(m.group(3)) == 2 else str(d.month)
        return f"{day}{m.group(2)}{month}{m.group(2)}{d.year}"
    if _STYLE_ISO.match(ex):
        return d.isoformat()
    if m := _STYLE_YMD.match(ex):
        case = MONTH_FORMS.get(m.group(5).lower(), (0, "nom"))[1]
        return f"{d.year}{m.group(2)}{d.day}{m.group(4)}{month_form(d.month, case)}"
    if m := _STYLE_DMY.match(ex):
        case = MONTH_FORMS.get(m.group(3).lower(), (0, "nom"))[1]
        return f"{d.day}{m.group(2)}{month_form(d.month, case)}{m.group(4)}{d.year}{m.group(6)}"
    return d.isoformat()


# --- numbers in words ---------------------------------------------------------------------------

_UNITS = ("", "ერთი", "ორი", "სამი", "ოთხი", "ხუთი", "ექვსი", "შვიდი", "რვა", "ცხრა")
_TEENS = (
    "ათი", "თერთმეტი", "თორმეტი", "ცამეტი", "თოთხმეტი", "თხუთმეტი", "თექვსმეტი", "ჩვიდმეტი",
    "თვრამეტი", "ცხრამეტი",
)  # fmt: skip
_SCORES = ("", "ოც", "ორმოც", "სამოც", "ოთხმოც")  # 20, 40, 60, 80
_HUNDREDS = ("", "ას", "ორას", "სამას", "ოთხას", "ხუთას", "ექვსას", "შვიდას", "რვაას", "ცხრაას")
_SCALES = ((1_000_000_000, "მილიარდ"), (1_000_000, "მილიონ"), (1000, "ათას"))


def _below_hundred(n: int) -> str:
    if n < 10:
        return _UNITS[n]
    if n < 20:
        return _TEENS[n - 10]
    score, rest = divmod(n, 20)
    if rest == 0:
        return _SCORES[score] + "ი"
    return _SCORES[score] + "და" + _below_hundred(rest)


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        # a hundred keeps its "-ი" only at the end of the numeral: "ასი", but "ას ერთი"
        parts.append(_HUNDREDS[hundreds] + ("" if rest else "ი"))
    if rest:
        parts.append(_below_hundred(rest))
    return " ".join(parts)


def number_to_words(n: int) -> str:
    """A whole number written out the way Georgian contracts write it.

    >>> number_to_words(2400)
    'ორი ათას ოთხასი'
    >>> number_to_words(48750)
    'ორმოცდარვა ათას შვიდას ორმოცდაათი'
    """
    if n == 0:
        return "ნული"
    if n < 0:
        return "მინუს " + number_to_words(-n)
    parts: list[str] = []
    for value, word in _SCALES:
        count, n = divmod(n, value)
        if count:
            prefix = "" if (count == 1 and value == 1000) else _below_thousand(count)
            parts.append(f"{prefix} {word}".strip() + ("ი" if n == 0 else ""))
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def _numeral_key(text: str) -> str:
    """The words of a numeral, each without the final vowel it may or may not carry."""
    s = normalize_spaces(re.sub(r"[()\[\],.;:]", " ", normalize_confusables(text).lower()))
    return " ".join(w[:-1] if w[-1:] in ("ი", "ა") and len(w) > 2 else w for w in s.split())


def parse_number_words(text: str) -> int | None:
    """A numeral in words, or None - also None when the words parse but are not how the number
    is written: "ათასი ათასი" is not 2000 and "ასი ორასი" is not 300."""
    value = parse_georgian_number_words(text)
    if value is None:
        return None
    key = _numeral_key(text)
    written = {_numeral_key(number_to_words(value))}
    if 1000 <= value < 2000:
        written.add(_numeral_key("ერთი " + number_to_words(value)))  # also said for a thousand
    return value if key in written else None


def number_words_typo(text: str, value: int) -> str | None:
    """The correct words for ``value`` when ``text`` is those words misspelled ("ასოი" for 100),
    else None. Different words ("ორასი" for 100) are not a typo but a different number."""
    right = number_to_words(value)
    if _numeral_key(text) == _numeral_key(right):
        return None
    got = normalize_spaces(normalize_confusables(text).lower())
    budget = 1 if len(right) <= 6 else 2
    return right if damerau(got, right, cap=budget) <= budget else None


# --- words that mix alphabets --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MixedWord:
    word: str
    start: int
    end: int
    #: the word as it should read, or None when it cannot be told
    correction: str | None


def mixed_script_words(text: str, *, vocabulary: frozenset[str] = frozenset()) -> list[MixedWord]:
    """Georgian words with a Latin or Cyrillic letter, or a digit, inside them: "sმომიჯნავე".

    A letter from another alphabet does not belong inside a Georgian word, and on a born-digital
    document it is a typing slip (on a scan it may be the OCR - the caller decides). The
    correction drops the stray characters when that leaves a Georgian word, preferring a word
    ``vocabulary`` (the other document's words) actually contains.
    """
    out: list[MixedWord] = []
    for m in _TOKEN.finditer(text):
        w = m.group(0)
        geo = sum(1 for ch in w if _GEO.match(ch))
        if geo < 3:  # "ა/ა", "N1-ში": too little Georgian to call it a Georgian word
            continue
        foreign = any(_LATIN.match(ch) or _CYRILLIC.match(ch) for ch in w)
        inner_digit = re.search(rf"[{GEORGIAN}]\d+[{GEORGIAN}]", w) is not None
        if not foreign and not inner_digit:
            continue
        stripped = "".join(ch for ch in w if _GEO.match(ch))
        correction: str | None = stripped if len(stripped) >= 3 else None
        near = [v for v in vocabulary if abs(len(v) - len(stripped)) <= 1]
        if near:
            best = min(near, key=lambda v: damerau(v, stripped, cap=2))
            if damerau(best, stripped, cap=2) <= 1:
                correction = best
        out.append(MixedWord(word=w, start=m.start(), end=m.end(), correction=correction))
    return out


# --- legal forms ---------------------------------------------------------------------------------

ContractorKind = Literal["company", "natural_person", "individual_entrepreneur", "foreign"]

#: How a party's legal form reads, and what kind of contractor it makes. A small business is a
#: tax status, not a legal form, so it cannot be read off the name: an individual entrepreneur
#: may or may not be one.
_LEGAL_FORMS: tuple[tuple[re.Pattern[str], ContractorKind], ...] = (
    (re.compile(r"(?<!\w)(?:შპს|სს|კს|სპს|ააიპ|შ\.პ\.ს\.?|ს\.ს\.?)(?!\w)"), "company"),
    (
        re.compile(r"(?<!\w)(?:ი/მ|ი\.მ\.|ინდ\.?\s*მეწარმე|ინდივიდუალური\s+მეწარმე)"),
        "individual_entrepreneur",
    ),
    (re.compile(r"(?<!\w)(?:ფ/პ|ფ\.პ\.|ფიზიკური\s+პირი)"), "natural_person"),
    (
        re.compile(r"(?<!\w)(?:LLC|Ltd\.?|Limited|GmbH|Inc\.?|S\.A\.|B\.V\.|Corp\.?)(?!\w)"),
        "foreign",
    ),
)


def legal_form_kind(name: str) -> ContractorKind | None:
    for pattern, kind in _LEGAL_FORMS:
        if pattern.search(name):
            return kind
    return None
