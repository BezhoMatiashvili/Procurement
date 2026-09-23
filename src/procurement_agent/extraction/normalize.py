"""Normalisation primitives shared by the rules, the checks and the evaluation harness.

Everything here is pure, deterministic and dependency-free. It is the single place where the
project decides what "the same number", "the same name" or "a valid IBAN" means, so that the
extractor, the AOA checker, the BOQ comparison and the scorer cannot drift apart.
"""

from __future__ import annotations

import re
import unicodedata

# --- whitespace, quotes, confusables ------------------------------------------------------------

# Every space-like codepoint Word, Excel and Tesseract produce. Plain str.split() misses most.
_SPACES = "              　"
_SPACE_RE = re.compile(f"[{_SPACES}]")
_WS = re.compile(r"\s+")

# Georgian documents use „…“ and ”…”; OCR and Word autocorrect add several more.
QUOTE_CHARS = "„“”“”„«»‹›\"'`‘’"
_DASHES = re.compile("[‐‑‒–—―−]")

# Cyrillic letters that are visually identical to Latin ones. Tesseract emits these inside
# otherwise-Latin tokens such as IBANs and SWIFT codes, where they silently break validation.
_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "У": "Y",
        "Х": "X",
        "І": "I",
        "Ј": "J",
        "Ѕ": "S",
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "х": "x",
        "у": "y",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
    }
)


def normalize_spaces(s: str) -> str:
    """Collapse every kind of space into single ASCII spaces."""
    return _WS.sub(" ", _SPACE_RE.sub(" ", s)).strip()


def normalize_confusables(s: str) -> str:
    """NFC + fold homoglyphs and typographic punctuation. Safe to apply to any document text."""
    s = unicodedata.normalize("NFC", s)
    s = s.translate(_CYRILLIC_TO_LATIN)
    s = _DASHES.sub("-", s)
    return normalize_spaces(s)


def confusable_chars(s: str) -> str:
    """The homoglyph codepoints present in ``s``, in order, deduplicated.

    ``normalize_confusables`` folds Cyrillic lookalikes away, which is what makes comparison
    work — and which also makes a swapped character invisible. Two strings that fold equal but
    are not equal differ in exactly these characters, and in a procurement document that is
    worth saying out loud rather than filing under "whitespace".
    """
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", s):
        if ord(ch) in _CYRILLIC_TO_LATIN and ch not in out:
            out.append(ch)
    return "".join(out)


def strip_quotes(s: str) -> str:
    """Quotes act as separators, not characters: „შპს „ნიმუში““ == 'შპს ნიმუში'."""
    for ch in QUOTE_CHARS:
        s = s.replace(ch, " ")
    return normalize_spaces(s)


def normalize_name(s: str) -> str:
    """Canonical form for comparing company and person names across two documents."""
    s = strip_quotes(normalize_confusables(s)).lower()
    for ch in ",.;:":
        s = s.replace(ch, " ")
    return normalize_spaces(s)


# Georgian noun endings a name carries in the genitive. Stripping them lets
# "კაპანაძის სახით" (intro) and "დირექტორი ნინო კაპანაძე" (signature) compare equal.
_GENITIVE = ("ისა", "ის", "ს")


def name_stem(name: str) -> str:
    """Case-insensitive stem of a personal or company name, inflectional endings removed."""
    out = []
    for word in normalize_name(name).split():
        for end in _GENITIVE:
            if word.endswith(end) and len(word) - len(end) >= 4:
                word = word[: -len(end)]
                break
        # nominative -ე / -ი on Georgian surnames (კაპანაძე -> კაპანაძ)
        if len(word) >= 5 and word[-1] in "ეი":
            word = word[:-1]
        out.append(word)
    return " ".join(out)


def names_match(a: str, b: str) -> bool:
    """True when two names differ only by inflection."""
    return bool(a) and bool(b) and name_stem(a) == name_stem(b)


# --- numbers ------------------------------------------------------------------------------------


def parse_number(s: str) -> float | None:
    """Parse a human-written number, guessing the decimal separator.

    Handles both '1,250.50' and '1.250,50' -> 1250.5. A trailing [.,] plus one or two digits is
    a decimal separator only when the integer part is long enough, or has no separator of its
    own, to rule out a thousands group ('12,500' is twelve and a half thousand, not 12.5).

    A separator after a lone zero is always decimal: no one writes a thousands group after 0,
    so '0.125' is an eighth, not 125. Any other single separator followed by exactly three
    digits stays a thousands group — '1.125' reads as 1125 — because that is how Georgian and
    most European documents write thousands, and the text alone cannot tell the two apart.
    Callers that know better from context (a percentage, a unit price in a table) must not
    rely on this function to guess.

    This is the only implementation in the project; ``rules.parse_number`` delegates here, so
    a rule hit and the scorer can never disagree about what a number is.
    """
    s = normalize_spaces(str(s))
    if not s:
        return None
    neg = s.startswith("-")
    s = s.lstrip("+-").strip()
    if zero := re.fullmatch(r"0[.,](\d+)", s):
        out = float(f"0.{zero.group(1)}")
        return -out if neg else out
    m = re.fullmatch(r"(.*?)([.,])(\d{1,2})", s)
    # A thousands group is exactly three digits, so a separator followed by four or more can
    # only be the decimal point: '12.3450' and '1,234.5678' are not 123450 and 12345678.
    long_tail = re.fullmatch(r"(.*?)([.,])(\d{4,})", s)
    if m and (len(m.group(1)) > 3 or not re.search(r"[ ,.]", m.group(1))):
        integer, frac = m.group(1), m.group(3)
    elif long_tail:
        integer, frac = long_tail.group(1), long_tail.group(3)
    else:
        integer, frac = s, ""
    integer = re.sub(r"[ ,.]", "", integer)
    if not integer.isdigit():
        return None
    out = float(f"{integer}.{frac}" if frac else integer)
    return -out if neg else out


def amounts_equal(a: float | None, b: float | None, tol: float = 0.01) -> bool:
    """Money comparison with a cent of slack; exact == on floats is never right here."""
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


# --- Georgian number words ----------------------------------------------------------------------

# Georgian contracts always restate an amount in words: "100 (ასი) ლარს". Comparing the two is
# the cheapest possible check on both a drafting error and an OCR digit error.
#
# The language is vigesimal from 20 to 99: 40 is "ორმოცი" (two-twenties), 90 is "ოთხმოცდაათი"
# (four-twenties-and-ten). Hundreds are lexicalised ("ორასი" = 200), so plain addition works.
# Stems are listed without the nominative "-ი", which the scanner consumes as an optional suffix.
_NUMBER_STEMS: dict[str, int] = {
    "ნულ": 0,
    "ერთ": 1,
    "ორ": 2,
    "სამ": 3,
    "ოთხ": 4,
    "ხუთ": 5,
    "ექვს": 6,
    "შვიდ": 7,
    "რვა": 8,
    "ცხრა": 9,
    "ათ": 10,
    "თერთმეტ": 11,
    "თორმეტ": 12,
    "ცამეტ": 13,
    "თოთხმეტ": 14,
    "თხუთმეტ": 15,
    "თექვსმეტ": 16,
    "ჩვიდმეტ": 17,
    "თვრამეტ": 18,
    "ცხრამეტ": 19,
    "ოც": 20,
    "ორმოც": 40,
    "სამოც": 60,
    "ოთხმოც": 80,
    "ას": 100,
    "ორას": 200,
    "სამას": 300,
    "ოთხას": 400,
    "ხუთას": 500,
    "ექვსას": 600,
    "შვიდას": 700,
    "რვაას": 800,
    "ცხრაას": 900,
    "ათას": 1000,
    "მილიონ": 1_000_000,
    "მილიარდ": 1_000_000_000,
}

# Longest-first, so "ორას" wins over "ორ" and "ათას" over "ათ".
_STEM_RE = re.compile(
    "|".join(sorted((re.escape(k) for k in _NUMBER_STEMS), key=len, reverse=True))
)

# Case endings a Georgian numeral can carry, plus the "და" ("and") that joins vigesimal parts.
_ENDINGS = ("ისა", "ის", "ად", "ით", "მა", "ს", "ი", "ო", "ა", "ე")


def parse_georgian_number_words(text: str) -> int | None:
    """Parse a Georgian numeral written in words. Returns None unless the whole string parses.

    >>> parse_georgian_number_words("ასი")
    100
    >>> parse_georgian_number_words("ორასი")
    200
    >>> parse_georgian_number_words("ორმოცდარვა ათას შვიდას ორმოცდაათი")
    48750
    """
    s = normalize_confusables(text).lower()
    s = re.sub(r"[()\[\],.;:]", " ", s)
    s = normalize_spaces(s)
    if not s:
        return None

    total = 0
    current = 0
    seen = False
    i = 0
    while i < len(s):
        if s[i] == " ":
            i += 1
            continue
        # "და" joins the parts of a vigesimal numeral (ოც-და-ათი) and is not itself a value.
        if s.startswith("და", i) and not _STEM_RE.match(s, i):
            i += 2
            continue
        m = _STEM_RE.match(s, i)
        if m is None:
            return None
        value = _NUMBER_STEMS[m.group(0)]
        i = m.end()
        seen = True
        if value >= 1000:
            current = (current or 1) * value
            total += current
            current = 0
        else:
            current += value
        # consume an optional case ending, longest first
        for end in sorted(_ENDINGS, key=len, reverse=True):
            if s.startswith(end, i):
                # only when it really ends the word, otherwise "ორმოცდაათი" loses its "ათ"
                nxt = i + len(end)
                if nxt >= len(s) or s[nxt] == " ":
                    i = nxt
                break
    if not seen:
        return None
    return total + current


# --- IBAN ---------------------------------------------------------------------------------------

_IBAN_CLEAN = re.compile(r"[^A-Z0-9]")


def iban_is_valid(iban: str) -> bool:
    """ISO 13616 mod-97 check.

    Without this, an OCR digit error in an account number is indistinguishable from a genuinely
    different account number, and the AOA checker reports the wrong kind of problem.
    """
    s = _IBAN_CLEAN.sub("", normalize_confusables(iban).upper())
    if not (15 <= len(s) <= 34) or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    rearranged = s[4:] + s[:4]
    total = 0
    for ch in rearranged:
        if ch.isdigit():
            total = (total * 10 + int(ch)) % 97
        elif ch.isalpha():
            total = (total * 100 + (ord(ch) - 55)) % 97
        else:
            return False
    return total == 1


# --- BIC / SWIFT -----------------------------------------------------------------------------

#: ISO 3166-1 alpha-2 country codes, plus XK (Kosovo), which SWIFT assigns although ISO does not.
#: Positions 5-6 of a BIC are a country code, which is the one structural property that tells
#: a real bank code from an ordinary capitalised word of the same length.
_ISO_COUNTRY_TEXT = """
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ
BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM
DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS
GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN
KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ
MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM
PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV
SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI
VN VU WF WS YE YT ZA ZM ZW XK
"""
ISO_COUNTRY_CODES: frozenset[str] = frozenset(_ISO_COUNTRY_TEXT.split())

#: Words that put a bank code in context. "ანგარიშ" (account) also covers "ანგარიშსწორება"
#: (settlement), which in these documents is exactly the clause that carries bank details.
_BANK_CONTEXT = re.compile(r"ბანკ|ანგარიშ|swift|\bbic\b|iban|\bbank|account", re.IGNORECASE)


def bic_has_country(code: str) -> bool:
    """True when positions 5-6 of a candidate BIC are a real country code."""
    return len(code) >= 6 and code[4:6].upper() in ISO_COUNTRY_CODES


def has_bank_context(text: str, start: int, end: int, *, window: int = 120) -> bool:
    """Whether a bank word sits within ``window`` characters either side of ``text[start:end]``.

    An unlabelled 8- or 11-letter capital word is a bank code only in a payment clause. Without
    this, a heading such as "SUPPLIER" or "DELIVERY" was read as a BIC — and because the
    pipeline stops asking the LLM for a field an anchor has filled, it also blocked extraction
    of the real one.
    """
    return _BANK_CONTEXT.search(text[max(0, start - window) : end + window]) is not None


_BIC_LABEL = re.compile(r"swift|\bbic\b|ბანკის\s*კოდ", re.IGNORECASE)
_BIC_SHAPED = re.compile(r"\b[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")
#: How far before an unlabelled code a SWIFT/BIC label may sit and still name it.
_BIC_LABEL_WINDOW = 40


def unlabelled_bic_plausible(text: str, start: int, end: int) -> bool:
    """Whether a BIC-shaped token that no label introduced is really a bank code.

    Three things have to hold. Positions 5-6 must be a country code - but that alone passes
    "SUPPLIER" (LI), "DELIVERY" (VE) and "BENEFICIARY" (FI). A bank word must be nearby - but
    those words sit in exactly the payment blocks that carry bank words. So, third, the code
    must either have a digit in its location code (every Georgian bank's does: "...GE22"), or
    follow a SWIFT/BIC label closely with no other code-shaped token in between. An all-letter
    foreign code nobody labelled is missed; a capitalised heading is never taken for a bank.
    """
    code = text[start:end]
    if not bic_has_country(code) or not has_bank_context(text, start, end):
        return False
    if any(ch.isdigit() for ch in code[6:8]):
        return True
    before = text[max(0, start - _BIC_LABEL_WINDOW) : start]
    labels = list(_BIC_LABEL.finditer(before))
    return bool(labels) and _BIC_SHAPED.search(before[labels[-1].end() :]) is None


# --- approximate string matching ------------------------------------------------------------


def edit_distance(a: str, b: str, *, cap: int = 3) -> int:
    """Levenshtein distance, giving up (returning cap + 1) once it is clearly over the cap."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def close_enough(a: str, b: str, *, max_distance: int = 2) -> bool:
    """Typo-tolerant equality, scaled down for short words so 'ორი'/'სამი' stay distinct."""
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    allowed = min(max_distance, max(1, min(len(a), len(b)) // 3))
    return edit_distance(a, b, cap=allowed) <= allowed
