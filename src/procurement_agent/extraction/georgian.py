"""Georgian morphology the checks need, and nothing more: names in the right case, months spelled
right, numbers in words, words mixing alphabets, dates in a document's own style.

Every rule that compares an act with its contract, and every correction it proposes, goes
through here. Before this module a correction was copied from wherever the contract happened to
state the value - usually its preamble, in the genitive - so a Word comment on "გიორგი კალანია"
in a signature block said "should be: გიორგი კალანაძის", which is itself wrong in that place.

Georgian inflects a noun phrase on its last word only ("დირექტორის გიორგი კალანაძის სახით": the
first name stays in the nominative). The case endings used in these documents are few, so the
morphology is a table, not a model:

* consonant stems, nominative ``-ი``: ``ბერიშვილი`` -> ``ბერიშვილის``, ``ბერიშვილით``;
* the truncating ``-ძე`` surnames: ``კალანაძე`` -> ``კალანაძის`` but ``კალანაძეს``;
* vowel stems in ``-ა -ე -ო -უ`` (and first names in ``-ი`` such as ``გიორგი``): ``ლომია`` ->
  ``ლომიას``;
* postpositions on top: ``-ისთვის``, ``-ში``, ``-ზე``, ``-თან``, ``-იდან``, ``-ამდე``.

Pure and dependency-free, like ``normalize``.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import Literal, get_args

from procurement_agent.extraction.normalize import (
    LEGAL_FORMS,
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
#: Every case and postposition ``inflect`` writes.
CASES: tuple[Case, ...] = get_args(Case)


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


#: Surname endings in "-ა" that keep it in every case ("ტყებუჩავა", "ტყებუჩავას").
_KEPT_A = ("ავა", "ევა", "ოვა", "უვა")


def _stem_class(lemma: str) -> Literal["consonant", "bare", "truncating", "vowel"]:
    if lemma.endswith("ძე"):
        return "truncating"
    if (
        len(lemma) > 3
        and lemma[-1] in "აე"
        and lemma[-2] not in _VOWELS
        and not lemma.endswith(_KEPT_A)
    ):
        # a common noun's final vowel drops before "-ის" and "-ით": "ხეობა" -> "ხეობის"
        return "truncating"
    if lemma.endswith("ი") and lemma not in _I_VOWEL_NAMES and len(lemma) > 2:
        return "consonant"
    if lemma[-1] not in _VOWELS:
        return "bare"  # a loanword that ends in a consonant: "დეველოპმენტ", "ლოფთ"
    return "vowel"


def _double_name(word: str) -> tuple[str, str] | None:
    """A hyphenated double surname split before its last part, which alone declines:
    ("მელაძე-", "ჯაფარიძის"). None for anything else."""
    head, sep, last = word.rpartition("-")
    if not sep or len(last) < 3 or not is_georgian_word(last):
        return None
    if not all(is_georgian_word(p) for p in head.split("-")):
        return None
    return head + sep, last


def inflect_word(lemma: str, case: Case) -> str:
    """``lemma`` (nominative) in ``case``; a double surname in its last part."""
    if (double := _double_name(lemma)) is not None:
        return double[0] + inflect_word(double[1], case)
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
    vowel-final word, or a consonant-final loanword stem ("ლოფთ"). A double surname is read in
    its last part."""
    if (double := _double_name(word)) is not None:
        return case_of(double[1])
    if not is_georgian_word(word):
        return "nom"
    for ending, case in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return case
    if len(word) >= 4 and word[-2] in _ONE_LETTER_STEMS and word[-1] in _VOWEL_STEM_ENDINGS:
        # a vowel stem's one-letter endings; its genitive and dative are the same word
        return _VOWEL_STEM_ENDINGS[word[-1]]
    return "nom"


#: The cases a vowel stem marks with one letter: "ლომაიას", "ლომაიამ", "ფოტოდ".
_VOWEL_STEM_ENDINGS: dict[str, Case] = {"ს": "gen", "მ": "erg", "დ": "adv"}
#: The vowels a stem ends in before those one-letter endings. Not "უ": Georgian has next to no
#: nouns in "-უ" (its surnames end in "-უა"), while loanwords end in "-უს", "-უმ" and "-უდ" in
#: the nominative - "პლუს", "კამპუს", "პრემიუმ", "ფორუმ", "გრინვუდ" - and were cut to "პლუ".
_ONE_LETTER_STEMS = "აეოი"
#: A case ending or a postposition hyphenated to a stem that is no Georgian word - one ending
#: in a Latin letter, a digit or a closing bracket. The endings are the declension's own
#: (``_ENDINGS``, and the dative's and ergative's "-ს", "-მა", "-მ"); a nominative "-ი" is not
#: one of them.
_HYPHENATED_ENDING = re.compile(
    r"(?P<stem>.*[A-Za-z0-9)\]])-(?:"
    + "|".join(sorted({e for e, _c in _ENDINGS} | {"ს", "მ"}, key=len, reverse=True))
    + ")"
)


def lemma_word(word: str, *, cases: Collection[Case] | None = None) -> str:
    """The nominative of one inflected word: ``მერგანიძის`` -> ``მერგანიძე``,
    ``თაუერსისთვის`` -> ``თაუერსი``, ``ლომიას`` -> ``ლომია``, ``ბერიშვილით`` -> ``ბერიშვილი``.

    ``cases``, when the caller knows them, are the cases the word's place allows: a name in
    apposition after "პროექტის" is a nominative or at most a genitive, never an ergative, so
    "სიგმა" there is no ergative of "სიგი". The word stays as written when its ending reads
    only as a case its place does not allow.

    A Latin word, a figure or an abbreviation takes its ending after a hyphen ("Green
    Valley-სთვის", "2-ისთვის", "N3-ის"), and the nominative is the stem without it: only a whole
    ending of the closed list (``_HYPHENATED_ENDING``), so "ვაკე-ა" and "12/2026-ბ" keep theirs.
    """
    hyphenated = _HYPHENATED_ENDING.fullmatch(word)
    if hyphenated is not None:
        return hyphenated.group("stem")
    head, hyphen, tail = word.rpartition("-")
    if hyphen and is_georgian_word(head) and is_georgian_word(tail) and len(tail) >= 3:
        # a double surname, "ხურციძე-ლომაიას": only its last part inflects (red team R5)
        return f"{head}-{lemma_word(tail, cases=cases)}"
    if not is_georgian_word(word):
        return word
    case = case_of(word)
    # "-ას" is a vowel stem's genitive and a common noun's dative alike: "ლომიას", "ხეობას"
    readings: tuple[Case, ...] = (case, "dat") if case == "gen" and word.endswith("ს") else (case,)
    if cases is not None:
        readings = tuple(c for c in readings if c in cases)
        if not readings:
            return word
    for lemma_candidate in _lemma_candidates(word):
        if any(inflect_word(lemma_candidate, c) == word for c in readings):
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
        # consonant stem ("ბერიშვილ" + "ი"), truncating ("მერგანიძ" + "ე", "ხეობ" + "ა"), vowel
        # stem as is. "-ობის"/"-ების" is far more often "-ობა"/"-ება" ("ხეობა", "საზოგადოება")
        # than "-ობი"/"-ები"; a plural ("სახლების") is the price of that guess.
        if stem.endswith("ძ"):
            out.extend([stem + "ე", stem + "ი", stem])
        elif stem.endswith(("ობ", "ებ")):
            out.extend([stem + "ა", stem + "ი", stem])
        elif stem[-1] in _VOWELS:
            # the consonant stem's "-ი" never follows a vowel: "პანორამა" is no ergative of
            # "პანორაი"
            out.extend([stem, stem + "ა", stem + "ე"])
        else:
            out.extend([stem + "ი", stem, stem + "ა", stem + "ე"])
    if word[-2] in _ONE_LETTER_STEMS and _VOWEL_STEM_ENDINGS.get(word[-1]) == case:
        out.append(word[:-1])  # vowel stem + "-ს" / "-მ" / "-დ"
    return list(dict.fromkeys(out))


def _split_quotes(phrase: str) -> tuple[str, str, str]:
    """Leading quotes, the phrase, trailing quotes: „ლოფთ თაუერსისთვის“ -> („, ..., “)."""
    q = re.escape(QUOTE_CHARS)
    m = re.fullmatch(rf"([{q}\s]*)(.*?)([{q}\s]*)", phrase)
    if m is None:  # pragma: no cover - the pattern matches every string
        return "", phrase, ""
    return m.group(1), m.group(2), m.group(3)


def lemma(phrase: str, *, cases: Collection[Case] | None = None) -> str:
    """A name or a project name in the nominative: only its last word inflects. ``cases``, when
    given, are the cases the phrase's place allows (``lemma_word``)."""
    lead, core, trail = _split_quotes(normalize_spaces(phrase))
    parts = core.split()
    if not parts:
        return phrase
    parts[-1] = lemma_word(parts[-1], cases=cases)
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


def in_case_of(wrong: str, right: str, *, written: Collection[str] = ()) -> str:
    """``right`` (in any case) rewritten in the case ``wrong`` is written in.

    What a correction must say: the act's own grammar, the contract's name. ``wrong`` may be
    misspelled, which does not matter - only its ending is read. ``written`` are the forms the
    documents write of the name: a form among them in the case meant is the correction as
    written, so a name that drops a vowel before its ending ("წერეთლის" of "წერეთელი") keeps the
    form the contract writes rather than one built from the nominative ("წერეთელის").

    >>> in_case_of("გიორგი კალანია", "გიორგი კალანაძის")
    'გიორგი კალანაძე'
    """
    forms = list(written)
    right_lemma = lemma(right)
    if forms:
        nominatives = attested_nominatives([right, *forms])
        right_lemma = nominatives[0]
        mine = [
            (normalize_spaces(f), phrase_case(f))
            for f, n in zip(forms, nominatives[1:], strict=True)
            if n == right_lemma
        ]
        case = _case_meant(wrong, right_lemma, {phrase_case(right), *(c for _, c in mine)})
        same = [f for f, c in mine if c == case]
        return same[0] if same else inflect(right_lemma, case)
    return inflect(right_lemma, _case_meant(wrong, right_lemma, {phrase_case(right)}))


#: A word's last syllable with the vowel some nouns drop before their ending put back: "წერეთლი"
#: (read off "წერეთლის") against "წერეთელი", "ამილახვრი" against "ამილახვარი".
_SYNCOPE = re.compile(rf"(?P<stem>[{GEORGIAN}]*[^\W\d_აეიოუ])(?P<vowel>[აეო])(?P<c>[ნლრმ])ი")


def _restores(full: str, short: str) -> bool:
    """Whether the phrase ``short`` is ``full`` with the vowel before its last word's final
    consonant dropped - the nominative read off a syncopated genitive."""
    a, b = full.split(), short.split()
    if len(a) != len(b) or a[:-1] != b[:-1]:
        return False
    m = _SYNCOPE.fullmatch(a[-1])
    return m is not None and b[-1] == f"{m.group('stem')}{m.group('c')}ი"


def attested_nominatives(forms: Collection[str]) -> list[str]:
    """The nominative of each of a person's written forms, as another written form attests it.

    ``lemma`` reads "სოსო წერეთლის" as "სოსო წერეთლი": a noun that drops a vowel before its ending
    cannot be told from its genitive alone. When the documents also write the nominative ("სოსო
    წერეთელი"), that is the name, and every form of it is given it (red team R5, AOA-R5-05: a
    correction must never be a non-word). A form no other form attests keeps its ``lemma``."""
    lemmas = [lemma(f) for f in forms]
    return [next((o for o in lemmas if _restores(o, n)), n) for n in lemmas]


def _case_meant(wrong: str, right_lemma: str, written: Collection[Case] = ()) -> Case:
    """The case ``wrong`` was meant in, ``right_lemma`` being the name it misspells.

    Its ending says so, unless the ending is itself the slip: "ბარამიზე" ends like the
    postposition "-ზე", but it is "ბარამიძე" with Shift missed on "ძ" - one letter from that
    nominative, two from "ბარამიძეზე". When the form the ending reads is more than one edit
    away and a form of the name in one other case is one edit away, that case was meant.

    ``written`` are the cases the documents write the name in. A slip one edit from the case
    its ending reads and one edit from another case too is read in the other one when only that
    one is written: a letter put in for the last consonant reads like a postposition ("-ში"),
    and the documents that never write the name so are the evidence.
    """
    read = phrase_case(wrong)
    _, wrong_core, _ = _split_quotes(normalize_spaces(wrong))
    _, right_core, _ = _split_quotes(normalize_spaces(right_lemma))
    if not wrong_core.split() or not right_core.split():
        return read
    last, name = wrong_core.split()[-1], right_core.split()[-1]
    if not is_georgian_word(last):
        return read
    near = {inflect_word(name, c): c for c in CASES if damerau(last, inflect_word(name, c)) <= 1}
    if damerau(last, inflect_word(name, read), cap=2) <= 1:
        others = {c for c in near.values() if c != read and c in written}
        return others.pop() if read not in written and len(others) == 1 else read
    return next(iter(near.values())) if len(near) == 1 else read


#: Postpositions older or more formal texts write in full, beside the modern ``_ENDINGS``.
_FORMAL_ENDINGS = ("ისათვის", "სათვის", "ისადმი", "სადმი")


def _stems_of(word: str) -> set[str]:
    """What ``word`` is left with once a case ending, a postposition or the nominative's final
    vowel is taken off - every reading, since the ending alone cannot say which one is right."""
    out = {word}
    for ending in (*(e for e, _ in _ENDINGS), *_FORMAL_ENDINGS):
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            out.add(word[: -len(ending)])
    if len(word) >= 4 and word[-1] in f"{_VOWELS}ს":
        out.add(word[:-1])  # "ლაზური" -> "ლაზურ", "ლომიას" -> "ლომია", "მერგანიძე" -> "მერგანიძ"
    return out


def syncopated(form: str) -> str | None:
    """``form`` with the vowel before its stem's last consonant dropped, as some nouns do before
    an ending: "მოედანის" -> "მოედნის", "სექტემბერის" -> "სექტემბრის". None when no such
    vowel stands there."""
    m = re.search(rf"([{GEORGIAN}])([აეო])([ნლრმ])(ის|ით|ად|ისთვის|იდან|ამდე)$", form)
    if m is None or m.group(1) in _VOWELS:
        return None
    return form[: m.start(2)] + form[m.end(2) :]


def word_lemmas(word: str) -> frozenset[str]:
    """Every nominative ``word`` is a form of under the case model - each candidate lemma that
    inflects back to it exactly (or with the vowel some nouns drop, ``syncopated``) - with the
    word itself, and the full form of a bare attributive stem ("ლაზურ" of "ლაზური")."""
    if not is_georgian_word(word):
        return frozenset({word})
    word = _normal_postposition(word)
    out = {word}
    if word[-1] not in _VOWELS:
        out.add(word + "ი")
    case = case_of(word)
    cases: tuple[Case, ...] = (case, "dat") if case == "gen" and word.endswith("ს") else (case,)
    candidates = list(_lemma_candidates(word))
    # a dropped vowel put back: "მოედნის" is a form of "მოედანი"
    m = re.search(rf"([{GEORGIAN}])([ნლრმ])(ის|ით|ად|ისთვის|იდან|ამდე)$", word)
    if m is not None and m.group(1) not in _VOWELS:
        stem = word[: m.start(2)]
        candidates += [f"{stem}{v}{m.group(2)}ი" for v in "აეო"]
    if len(word) > 3 and word[-1] == "ს" and word[-2] not in _VOWELS:
        # a consonant stem's dative: "უფლებებს" of "უფლებები", "ბერიშვილს" of "ბერიშვილი"
        candidates.append(word[:-1] + "ი")
        cases = (*cases, "dat")
    for lemma_candidate in candidates:
        for c in cases:
            form = inflect_word(lemma_candidate, c)
            if word in (form, syncopated(form)):
                out.add(lemma_candidate)
    return frozenset(out)


def _normal_postposition(word: str) -> str:
    """The older full postposition as the modern one: "-ისათვის" is "-ისთვის"."""
    for old, new in (("ისათვის", "ისთვის"), ("სათვის", "სთვის")):
        if word.endswith(old) and len(word) - len(old) >= 3:
            return word[: -len(old)] + new
    return word


def same_word(a: str, b: str) -> bool:
    """Whether two words are one word in two grammatical forms: forms of a common nominative
    under the case model (``word_lemmas``), a postposition or the bare stem an attribute takes
    ("ლაზურ რეზიდენსში") included. The spelling must agree - "ლაზუი" is not "ლაზური", and
    "მთაწმინდიის" is no form of "მთაწმინდა" - which stripping endings ("ლაზუ…",
    "მთაწმინდი…") did not see."""
    if a == b:
        return True
    if not (is_georgian_word(a) and is_georgian_word(b)):
        return False
    if syncopated(a) == b or syncopated(b) == a:
        # one ending written with the vowel and without it: a noun drops it or keeps it, so
        # one of the two is a slip (the vowel dropped from a genitive that keeps it)
        return False
    return bool(word_lemmas(a) & word_lemmas(b))


def same_phrase(a: str, b: str) -> bool:
    """Whether two phrases name one thing in two grammatical forms: every word but the last the
    same, the last words forms of a common nominative (``same_word``). Comparing the ``lemma``
    of each forces a form two nominatives share onto one of them: "ტერასისთვის" is a form of
    "ტერასა" and of "ტერასი" alike, and its lemma, "ტერასი", is not "ტერასა"."""
    _, core_a, _ = _split_quotes(normalize_spaces(a))
    _, core_b, _ = _split_quotes(normalize_spaces(b))
    words_a, words_b = core_a.lower().split(), core_b.lower().split()
    if not words_a or len(words_a) != len(words_b) or words_a[:-1] != words_b[:-1]:
        return False
    return same_word(words_a[-1], words_b[-1])


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
    """Every correct form of one month and its case. The vowel stem's genitive and dative are
    one word ("აგვისტოს"): it is listed as the genitive here, and ``_MONTH_DATIVE`` lists it
    again as the dative."""
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
#: The dative of a vowel-stem month: the genitive's word ("2026 წლის 31 აგვისტოს" is the day an
#: act was signed as often as the day a period ends "31 აგვისტოს ჩათვლით").
_MONTH_DATIVE = {month: nom + _VOWEL_CASES["dat"] for month, nom, _ in _MONTHS if nom.endswith("ო")}
#: The plain form of each month in each case (no particle, the short genitive).
_MONTH_FORM_OF: dict[tuple[int, Case], str] = {
    **{
        (m, c): form
        for form, (m, c) in reversed(MONTH_FORMS.items())
        if not form.endswith(("ვე", "ც", "ისა"))
    },
    **{(m, "dat"): form for m, form in _MONTH_DATIVE.items()},
}
#: How far a word may be from a month form and still be that month misspelled. Two edits cover
#: a typo in the stem plus one in the ending ("აგვუისტას" for "აგვისტოს").
_MONTH_TYPO_DISTANCE = 2
_MONTH_MIN_LEN = 4
#: Ordinary words one edit from a month form: "მარტო" (alone), "მარტივი" (simple).
_NOT_MONTHS = frozenset({"მარტო", "მარტოდ", "მარტივ", "მარტივი", "მარტივად", "მარტივს"})


def month_form(month: int, case: Case) -> str:
    """The plain form of ``month`` in ``case`` (no particle, the short genitive)."""
    return _MONTH_FORM_OF.get((month, case), _MONTH_NOMINATIVE[month])


#: Words after a day and month that take its genitive: a postposition ("31 აგვისტოს ჩათვლით",
#: "... შემდეგ", "... მდგომარეობით") or a noun the date qualifies ("31 აგვისტოს აქტი", "...
#: ხელშეკრულება", "... თარიღით").
_GENITIVE_BEFORE_WORD = re.compile(
    r"^(?:ჩათვლით|შემდეგ|მდგომარეობ|თარიღ|აქტ|ხელშეკრულებ|წერილ|ოქმ|ანგარიშ|ინვოის|"
    r"ბრძანებ|გადაწყვეტილებ|№|N(?![ა-ჰ]))"
)


def month_case(word: str, *, following: str = "") -> Case:
    """The case a correct month form is written in. The vowel stem's "აგვისტოს" is the
    genitive or the dative, and ``following``, the text after the date, tells which: a
    postposition or a noun the date qualifies asks for the genitive; otherwise - a verb, the end
    of the sentence - the day is the dative ("აქტი გაფორმდა 2026 წლის 31 აგვისტოს")."""
    low = word.lower()
    _, case = MONTH_FORMS.get(low, (0, "nom"))
    plain = next((low[: -len(p)] for p in _PARTICLES if p and low.endswith(p)), low)
    ambiguous = case == "gen" and plain in _MONTH_DATIVE.values()
    if ambiguous and not _GENITIVE_BEFORE_WORD.match(following.lstrip(" \t\n,")):
        return "dat"
    return case


@dataclass(frozen=True, slots=True)
class MonthTypo:
    word: str
    month: int
    #: the correct form the word was meant to be, or None when two months are equally close
    #: ("ივმისი": June or July?) - a wrong correction is worse than none
    correction: str | None


#: Cases a month takes as a word, never in a date: "from a person" (-ისგან), "towards" (-ისკენ),
#: "with" (-თან), "on" (-ზე) and the ergative's subject. A slip tied between one of them and a
#: date's form was meant as the date's.
_NOT_WITH_DATES: frozenset[Case] = frozenset({"gen_gan", "gen_ken", "dat_tan", "dat_ze", "erg"})
#: Words after a date that take the genitive: "30 სექტემბრის ჩათვლით" (inclusive).
_GENITIVE_AFTER = ("ჩათვლით", "შემდეგ")


def month_typo(word: str, *, following: str = "") -> MonthTypo | None:
    """``word`` as a misspelled month, or None when it is a correct month form or no month.

    Unlike a prefix test this sees a typo after the stem - "სექტემბრდან" for "სექტემბრიდან",
    "ოქტომბერის" for "ოქტომბრის" - because every correct form is listed. Callers look only at
    words in a date, so "მარტო" (alone) is never read as a misspelled March. ``following`` is
    the word after it: "ჩათვლით" asks for the genitive, which settles a typo one edit from two
    forms ("სექტემბირს": "სექტემბრის" or "სექტემბერს").
    """
    wants_genitive = following.startswith(_GENITIVE_AFTER)
    low = word.lower()
    if (
        len(low) < _MONTH_MIN_LEN
        or low in MONTH_FORMS
        or low in _NOT_MONTHS
        or not is_georgian_word(low)
    ):
        return None
    best: dict[int, tuple[int, int, int, int, str]] = {}
    for form, (month, case) in MONTH_FORMS.items():
        if abs(len(form) - len(low)) > _MONTH_TYPO_DISTANCE:
            continue
        d = damerau(low, form, cap=_MONTH_TYPO_DISTANCE)
        if d > _MONTH_TYPO_DISTANCE:
            continue
        # At equal distance the form the next word asks for wins, then a form a date takes, then
        # the form ending like the word: "ოქტომბერის" is one edit from both "ოქტომბერი" and
        # "ოქტომბრის", and it was meant as the genitive; "სექტემბრისან" is one edit from
        # "სექტემბრიდან" and from "სექტემბრისგან", which no date takes.
        fits = 0 if not wants_genitive or case == "gen" else 1
        rare = 1 if case in _NOT_WITH_DATES else 0
        rank = (d, fits, rare, -_common_suffix(low, form))
        if month not in best or rank < best[month][:4]:
            best[month] = (*rank, form)
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda kv: kv[1][:4])
    month, (d, _, _, _, form) = ranked[0]
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

#: The word for "year" after a date's year, as the example writes it ("წ.", "წელს", "წლის").
_STYLE_YEAR_WORD = r"(?P<tail>\s*წ(?:\.|[ა-ჰ]*))?"
#: A numeric date: "05.09.2026", "5/9/2026", and a two-digit year before its year word,
#: "05.09.26 წ.".
_STYLE_NUMERIC = re.compile(
    rf"^\s*(?P<day>\d{{1,2}})(?P<sep>[./-])(?P<month>\d{{1,2}})(?P=sep)(?P<year>\d{{4}}|\d{{2}})"
    rf"(?!\d){_STYLE_YEAR_WORD}"
)
_STYLE_ISO = re.compile(r"^\s*\d{4}-\d{2}-\d{2}")
#: A day as a Georgian date writes it: "30", "05", in quotes ("„30“"), or as an ordinal ("1-ლი").
_STYLE_DAY = (
    r"(?P<open>„|,,|\"|“|«)?(?P<day>\d{1,2})(?P<close>“|”|\"|''|»)?(?P<ordinal>\s*-\s*(?:ლი|ე))?"
)
_STYLE_YMD = re.compile(
    rf"^\s*(?P<year>\d{{4}})(?P<sep>\s*(?:წლის|წ\.)\s*){_STYLE_DAY}(?P<gap>\s*)"
    rf"(?P<month>[{GEORGIAN}]+)"
)
_STYLE_DMY = re.compile(
    rf"^\s*{_STYLE_DAY}(?P<gap>\s*)(?P<month>[{GEORGIAN}]+)(?P<incl>\s+ჩათვლით)?(?P<sep>,?\s*)"
    rf"(?P<year>\d{{4}}){_STYLE_YEAR_WORD}"
)
#: An English date: "5 July 2026", "5th Jul. 2026", "July 5, 2026".
_EN_ORDINAL = r"(?P<ordinal>st|nd|rd|th)?"
_STYLE_EN_DMY = re.compile(
    rf"^\s*(?P<day>\d{{1,2}}){_EN_ORDINAL}(?P<gap>\s+)(?P<month>[A-Za-z]+)(?P<dot>\.?)"
    r"(?P<sep>,?\s+)(?P<year>\d{4})"
)
_STYLE_EN_MDY = re.compile(
    rf"^\s*(?P<month>[A-Za-z]+)(?P<dot>\.?)(?P<gap>\s+)(?P<day>\d{{1,2}}){_EN_ORDINAL}"
    r"(?P<sep>,?\s+)(?P<year>\d{4})"
)
_EN_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip


def _styled_day(d: date, m: re.Match[str]) -> str:
    """The day as the example writes its own: zero-padded when that one is ("05"), in the same
    quotes when it is quoted ("„05“"), and an ordinal first day as one ("1-ლი") - an ordinal is
    written only for the first, so another day is written plain."""
    day = f"{d.day:02d}" if m.group("day").startswith("0") else str(d.day)
    if m.group("ordinal") and d.day == 1:
        day += m.group("ordinal")
    if m.group("open") and m.group("close"):
        return f"{m.group('open')}{day}{m.group('close')}"
    return day


def _english_month(d: date, written: str) -> str | None:
    """``d``'s month named as ``written`` names its own: in full or abbreviated to three letters,
    in its capitals; None when ``written`` is no English month."""
    low = written.lower()
    full = next(
        (n for n in _EN_MONTH_NAMES if low == n or (len(low) >= 3 and n.startswith(low))), None
    )
    if full is None:
        return None
    name = _EN_MONTH_NAMES[d.month - 1]
    if low != full:
        name = name[:3]
    if written.isupper() and len(written) > 1:
        return name.upper()
    if written[:1].isupper():
        return name.capitalize()
    return name


def _english_ordinal(day: int) -> str:
    if 11 <= day % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _render_english(d: date, m: re.Match[str]) -> str | None:
    month = _english_month(d, m.group("month"))
    if month is None:
        return None
    day = f"{d.day:02d}" if m.group("day").startswith("0") else str(d.day)
    if m.group("ordinal"):
        day += _english_ordinal(d.day)
    month += m.group("dot")
    if m.re is _STYLE_EN_DMY:
        return f"{day}{m.group('gap')}{month}{m.group('sep')}{d.year}"
    return f"{month}{m.group('gap')}{day}{m.group('sep')}{d.year}"


def render_date_like(d: date, example: str, *, following: str = "") -> str:
    """``d`` written the way ``example`` writes a date, keeping its month's case.

    "2026 წლის 5 სექტემბერი" as the example gives "2026 წლის 31 აგვისტო"; "05.09.2026" gives
    "31.08.2026"; "„05“ ივლისი, 2026 წ." gives "„30“ ივნისი, 2026 წ."; "05.09.26 წ." gives
    "31.08.26 წ."; "5 September 2026" gives "31 August 2026". ``following`` is the word after the
    example in its document: it tells the case of a month form that can be either of two
    (``month_case``). Every form ``rules.find_dates`` reads has its style here; an example in
    none yields the ISO date.
    """
    ex = normalize_spaces(example)
    if m := _STYLE_NUMERIC.match(ex):
        day = f"{d.day:02d}" if len(m.group("day")) == 2 else str(d.day)
        month = f"{d.month:02d}" if len(m.group("month")) == 2 else str(d.month)
        year = f"{d.year % 100:02d}" if len(m.group("year")) == 2 else str(d.year)
        sep = m.group("sep")
        return f"{day}{sep}{month}{sep}{year}{m.group('tail') or ''}"
    if _STYLE_ISO.match(ex):
        return d.isoformat()
    if m := _STYLE_YMD.match(ex):
        written = month_form(d.month, month_case(m.group("month"), following=following))
        return f"{d.year}{m.group('sep')}{_styled_day(d, m)}{m.group('gap')}{written}"
    if m := _STYLE_DMY.match(ex):
        # "30 ნოემბრის ჩათვლით, 2026 წ.": the month's case is the one "ჩათვლით" asks for
        incl = m.group("incl") or ""
        case = month_case(m.group("month"), following=incl or following)
        written = month_form(d.month, case)
        tail = m.group("tail") or ""
        return f"{_styled_day(d, m)}{m.group('gap')}{written}{incl}{m.group('sep')}{d.year}{tail}"
    for pattern in (_STYLE_EN_DMY, _STYLE_EN_MDY):
        if (m := pattern.match(ex)) and (english := _render_english(d, m)) is not None:
            return english
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
    #: what it may have been: the stray key's Georgian letter put back, the stray letter dropped
    readings: tuple[str, ...] = ()
    #: the correction is a word the documents write, not only the stray letter dropped
    attested: bool = False


def mixed_script_words(text: str, *, vocabulary: frozenset[str] = frozenset()) -> list[MixedWord]:
    """Georgian words with a Latin or Cyrillic letter, or a digit, inside them: "sმომიჯნავე".

    A letter from another alphabet does not belong inside a Georgian word, and on a born-digital
    document it is a typing slip (on a scan it may be the OCR - the caller decides). The
    correction drops the stray characters, or puts back the Georgian letter on the stray one's
    key when ``vocabulary`` (both documents' words) attests that word.
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
        # the letter typed on the wrong keyboard layout, in place of the Georgian one on its key
        # ("უფლeბები" for "უფლებები"), or a stray key pressed as well ("sმომიჯნავე")
        keyed = "".join(ch if _GEO.match(ch) else _KA_KEYS.get(ch, "") for ch in w)
        # a stray letter standing where a Georgian one was: the words the documents write with
        # a Georgian letter in its place ("მომсახურების" for "მომსახურების")
        slot = re.compile("".join(re.escape(ch) if _GEO.match(ch) else f"[{GEORGIAN}]" for ch in w))
        in_place = sorted(v for v in vocabulary if slot.fullmatch(v))
        correction: str | None = stripped if len(stripped) >= 3 else None
        candidates = [c for c in dict.fromkeys((keyed, stripped)) if len(c) >= 3]
        attested = (
            [c for c in candidates if c in vocabulary]
            or (in_place if len(in_place) == 1 else [])
            or [c for c in candidates if any(same_word(c, v) for v in vocabulary)]
        )
        # the stray key's own letter put back, or the stray letter dropped, when that leaves a
        # word the documents write; failing that, a Georgian letter in its place - a guess with
        # no relation to the key, taken only when nothing likelier is a written word (a stray
        # letter inserted before a dative's "ს" is dropped, not read as the genitive's "ი",
        # mutation harness seed 229); then a written word in another form; otherwise the stray
        # letter alone goes: rewriting another letter towards a word the contract spells
        # differently ("ეგზემპლიარად" for the act's "ეგზემპლარად") is prose spelling, not the
        # removal of a stray letter
        if attested:
            correction = attested[0]
        out.append(
            MixedWord(
                word=w,
                start=m.start(),
                end=m.end(),
                correction=correction,
                readings=tuple(candidates),
                attested=bool(attested),
            )
        )
    return out


#: The Georgian keyboard layout: the Georgian letter on each Latin key. A letter typed with the
#: wrong layout active is this key's Latin letter.
_KA_KEYS = dict(
    zip(
        "abgdevzTiklmnopJrstufqRySCcZwWxjh",
        "აბგდევზთიკლმნოპჟრსტუფქღყშჩცძწჭხჯჰ",
        strict=True,
    )
)

#: The letter rows of that keyboard, by the Latin letter each key carries.
_KEY_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
#: Georgian's stops and affricates come in series - voiced, aspirated, ejective - whose letters
#: a writer confuses by ear: "კ" for "ქ", "პ" for "ფ".
_SOUND_SERIES = ("ბფპ", "დთტ", "გქკ", "ძცწ", "ჯჩჭ", "ქხყ")


def _key_neighbours() -> dict[str, frozenset[str]]:
    """Each letter's slips: the letters of the keys around it, and the other letter of its own
    key (shift held or missed). A row sits half a key right of the one above it."""
    where = {key: (r, c) for r, row in enumerate(_KEY_ROWS) for c, key in enumerate(row)}

    def letters(key: str) -> set[str]:
        return {_KA_KEYS[k] for k in (key, key.upper()) if k in _KA_KEYS}

    out: dict[str, frozenset[str]] = {}
    for key, (r, c) in where.items():
        around = [(r, c - 1), (r, c + 1), (r - 1, c), (r - 1, c + 1), (r + 1, c - 1), (r + 1, c)]
        near = {
            ch
            for rr, cc in around
            if 0 <= rr < len(_KEY_ROWS) and 0 <= cc < len(_KEY_ROWS[rr])
            for ch in letters(_KEY_ROWS[rr][cc])
        }
        for letter in letters(key):
            out[letter] = frozenset(near | letters(key) - {letter})
    return out


_SLIPS = _key_neighbours()


def typing_slip(typed: str, meant: str) -> bool:
    """Whether writing the letter ``typed`` where ``meant`` belongs is a slip: a key next to it,
    its own key with the shift held or missed, or a letter of the same sound series."""
    if typed in _SLIPS.get(meant, frozenset()):
        return True
    return any(typed in series and meant in series for series in _SOUND_SERIES)


# --- legal forms ---------------------------------------------------------------------------------

ContractorKind = Literal["company", "natural_person", "individual_entrepreneur", "foreign"]

#: The abbreviations of ``normalize.LEGAL_FORMS`` that make a company contractor.
_COMPANY_FORMS = frozenset({"შპს", "სს", "კს", "სპს", "ააიპ"})
#: Every spelling of those forms - written out, dotted, bracketed ("შ.პ.ს.", "ა(ა)იპ") - read
#: from the one legal-form table (red team R4, AOA-R4-02). A form is a word of its own, and a
#: dotted form's prefix inside a longer one ("ს.ს" of "ს.ს.ი.პ.") is no form.
_COMPANY_FORM = "|".join(f"(?:{p})" for p, short in LEGAL_FORMS if short in _COMPANY_FORMS)
_ENTREPRENEUR_FORM = "|".join(f"(?:{p})" for p, short in LEGAL_FORMS if short == "ი/მ")

#: How a party's legal form reads, and what kind of contractor it makes. A small business is a
#: tax status, not a legal form, so it cannot be read off the name: an individual entrepreneur
#: may or may not be one.
_LEGAL_FORMS: tuple[tuple[re.Pattern[str], ContractorKind], ...] = (
    (re.compile(rf"(?<!\w)(?:{_COMPANY_FORM})(?!\w)(?!\.\s?\w\.)"), "company"),
    (
        re.compile(rf"(?<!\w)(?:{_ENTREPRENEUR_FORM}|ი\.მ\.)"),
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
