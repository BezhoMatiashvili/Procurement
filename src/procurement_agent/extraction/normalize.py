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
QUOTE_CHARS = "„“”“”„«»‹›\"'`‘’" + "‚‛"  # and the low and reversed single quotes ‚ ‛
_DASHES = re.compile("[‐‑‒–—―−]")
#: Two commas typed for the low opening quote: ",,შემსრულებელი”". Only directly before a letter
#: or a digit - two commas before a space are a typo, not a quote.
_COMMA_QUOTE = re.compile(r",,(?=[^\W_])")

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


def normalize_layout(s: str) -> str:
    """``normalize_confusables`` without the homoglyph fold: NFC, dashes and spaces, the letters
    as written. Folding a homoglyph replaces one character with one, so the two texts have the
    same length and an offset into one is the same place in the other - which is how a finding
    computed over the folded text quotes the word the document actually has."""
    s = unicodedata.normalize("NFC", s)
    s = _DASHES.sub("-", s)
    return normalize_spaces(s)


def fold_look_alikes(s: str) -> str:
    """Cyrillic letters that look like Latin ones, as the Latin ones; one character for one."""
    return s.translate(_CYRILLIC_TO_LATIN)


def fold_comma_quotes(s: str) -> str:
    """Two commas typed for the opening quote (",,შემსრულებელი”") as the quote they stand
    for, so every reader - the anchors, the name and project rules - sees one form of quote."""
    return _COMMA_QUOTE.sub("„", s)


def fold_comma_quotes_mapped(s: str) -> tuple[str, list[int]]:
    """``fold_comma_quotes`` and, for each offset into its result and for its end, the offset
    into ``s``: the fold is one character for two, and a finding computed over the folded text
    must still quote the two commas the document has."""
    parts: list[str] = []
    back: list[int] = []
    at = 0
    for m in _COMMA_QUOTE.finditer(s):
        parts += [s[at : m.start()], "„"]
        back += [*range(at, m.start()), m.start()]
        at = m.end()
    parts.append(s[at:])
    back += [*range(at, len(s)), len(s)]
    return "".join(parts), back


def normalize_confusables(s: str) -> str:
    """NFC + fold homoglyphs and typographic punctuation. Safe to apply to any document text."""
    return fold_look_alikes(fold_comma_quotes(normalize_layout(s)))


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
    """True when two names differ only by inflection, or when one writes the first name as its
    initial: "ნ. ბერიძე" is "ნიკა ბერიძის" - the initial must be the first letter of a full
    first name, and the rest must agree."""
    if not a or not b:
        return False
    sa, sb = name_stem(a), name_stem(b)
    if sa == sb:
        return True
    wa, wb = sa.split(), sb.split()
    return _initial_of(wa, wb) or _initial_of(wb, wa)


_INITIAL_FIRST = re.compile(r"[^\W\d_]\.\s*[^\W\d_]")


def keep_initial(written: str, name: str) -> str:
    """``name`` in the style ``written`` has: its first name as an initial when ``written`` gives
    one ("გ. ჩხეტიძე" corrected to "გ. ჩხეტია", not to the full "გურამ ჩხეტია")."""
    parts = name.split(maxsplit=1)
    if len(parts) < 2 or not _INITIAL_FIRST.match(written):
        return name
    return f"{parts[0][0]}. {parts[1]}"


def _initial_of(initialled: list[str], full: list[str]) -> bool:
    return (
        len(initialled) == len(full) >= 2
        and len(initialled[0]) == 1
        and len(full[0]) > 1
        and full[0].startswith(initialled[0])
        and initialled[1:] == full[1:]
    )


# --- content words ------------------------------------------------------------------------------

#: Words that say nothing about what a clause is about: conjunctions, pronouns, postpositions
#: written as words, and the connecting words of a legal sentence, Georgian and English.
_STOPWORDS = frozenset(
    {
        "და",
        "ან",
        "თუ",
        "რომ",
        "რაც",
        "რის",
        "ეს",
        "ის",
        "ამ",
        "იმ",
        "მის",
        "მისი",
        "მათ",
        "მათი",
        "თავის",
        "თავისი",
        "ყველა",
        "ყოველი",
        "ნებისმიერი",
        "მიერ",
        "შორის",
        "მიმართ",
        "შესახებ",
        "შესაბამისად",
        "შესაბამისი",
        "როგორც",
        "ასევე",
        "აგრეთვე",
        "კერძოდ",
        "არის",
        "იყოს",
        "უნდა",
        "არ",
        "ვერ",
        "სხვა",
        "მხოლოდ",
        "ხოლო",
        "მიხედვით",
        "ფარგლებში",
        "თანახმად",
        "საფუძველზე",
        "ჩათვლით",
        "წინამდებარე",
        "აღნიშნული",
        "შემდეგი",
        "რომელიც",
        "რომლის",
        "რომლებიც",
        "რომელსაც",
        "წარმოადგენს",
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "by",
        "with",
        "as",
        "per",
        "its",
        "their",
        "this",
        "that",
    }
)
#: The same words, for readers that must tell a name from the words of a sentence: no person is
#: called "ან მის" ("or his").
FUNCTION_WORDS = _STOPWORDS
#: What a Georgian word carries after its stem, cut from the end one at a time: postpositions
#: ("-ში", "-ზე", "-თვის", "-დან"), the case endings - with the long forms a list takes before
#: "და", "დიზაინისა და …" - the plural "-ებ-" and the nominative "-ი". Cut this far, two forms
#: of one word agree - "სისტემების" and "სისტემებით" both come to "სისტემ" - and a vowel-final
#: nominative ("სისტემა") keeps its vowel, which ``stems_match`` allows for.
_CONTENT_SUFFIXES = (
    "თვის",
    "დან",
    "მდე",
    "თან",
    "ისა",
    "ითა",
    "ში",
    "ზე",
    "ით",
    "ად",
    "ის",
    "ებ",
    "ს",
    "ი",
    "ე",
)
_WORD_RE = re.compile(r"[ა-ჰa-z]+")


def _content_stem(word: str) -> str:
    changed = True
    while changed:
        changed = False
        for suffix in _CONTENT_SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: -len(suffix)]
                changed = True
                break
    return word


def content_stems(text: str) -> tuple[str, ...]:
    """The words of ``text`` that say what it is about, each cut to its stem, in order and each
    once. Function words go; so do numbers, which a caller compares on their own.

    ``name_stem`` stops at the genitive, which is all a name ever carries. A description of work
    also takes postpositions, the plural and the other cases: "სერვისის გვერდების აღწერით" and
    "სერვისის გვერდის აღწერა" must come out as the same words.
    """
    out: dict[str, None] = {}
    for word in _WORD_RE.findall(normalize_confusables(text).lower()):
        if word in _STOPWORDS:
            continue
        stem = _content_stem(word)
        if stem not in _STOPWORDS:
            out.setdefault(stem)
    return tuple(out)


def stems_match(a: str, b: str) -> bool:
    """Whether two content stems (``content_stems``) are one word.

    Equal; or the shorter one, four letters or more, starts the longer - a vowel-final
    nominative against its cut stem, "სისტემა" / "სისტემ"; or, both six letters or more, one
    letter apart - a typo, or the vowel some forms drop, "რესტორან" / "რესტორნ".
    """
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 4 and long_.startswith(short):
        return True
    return len(short) >= 6 and edit_distance(a, b, cap=1) <= 1


# --- numbers ------------------------------------------------------------------------------------

#: Besides "," and ".", what separates a figure's thousands: every space Word, a locale or a scan
#: puts there - the no-break space, the thin space (U+2009), the narrow no-break space (U+202F),
#: the figure space (U+2007) ... - and the apostrophe, straight or typographic ("36'000",
#: "36’000"). One set for every reader of a figure (red team R3, EXTRACTION-R3-11): a reader that
#: knew only two of them read "36 000" with a thin space as its last group, a total of 0.
_GROUP_APOSTROPHES = "'\u2019"
#: a regex class of the spaces that group a figure's digits
FIGURE_SPACE = f"[ {_SPACES}]"
#: one separator between two groups of a figure, as a regex class
FIGURE_SEPARATOR = f"[ {_SPACES}{_GROUP_APOSTROPHES},.]"
#: the separators only these readers know: after a digit and one of them, a figure goes on
_NEW_SEPARATORS = _SPACES.replace("\u00a0", "") + _GROUP_APOSTROPHES
#: A figure as documents write it: digits grouped in threes by a space of any kind, an
#: apostrophe, a dot or a comma, and an optional decimal part - "2 400", "2.400,50", "36'000",
#: "100". A group is exactly three digits after one separator, so an item number in front
#: ("საფასური: 1. 100 (ასი)") is not read into it. A figure never starts right after a digit and
#: one of the separators the older readers did not know: "48'50" is no amount, never "50".
FIGURE = (
    rf"(?<!\d[{_NEW_SEPARATORS}])"
    rf"(?:\d{{1,3}}(?:{FIGURE_SEPARATOR}\d{{3}})+(?:[.,]\d{{1,2}})?|\d+(?:[.,]\d{{1,2}})?)"
)
_FIGURE_GROUPING = re.compile(f"[ ,.{_GROUP_APOSTROPHES}]")


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
    if m and (len(m.group(1)) > 3 or not _FIGURE_GROUPING.search(m.group(1))):
        integer, frac = m.group(1), m.group(3)
    elif long_tail:
        integer, frac = long_tail.group(1), long_tail.group(3)
    else:
        integer, frac = s, ""
    # every group separator, the apostrophe included ("36'000"); spaces are one by now
    integer = _FIGURE_GROUPING.sub("", integer)
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


# --- identification codes -----------------------------------------------------------------------

#: The labels a party's identification code is written behind, in every spelling the documents
#: use: "ს/კ", "ს.კ.", "ს/ნ", "პ/ნ"; "საიდენტიფიკაციო" alone or with "კოდი", "ნომერი" or "№",
#: in any case ("საიდენტიფიკაციო კოდით", "ნომრის"), and abbreviated "საიდ. კოდი", "საიდ. №";
#: "პირადი ნომერი"; English "ID", "ID code", "identification number", "tax ID". One pattern
#: for every reader - the party reader, the rules, the act check - so that a label one of them
#: reads is never a code another misses (red team R2, AOA-R2-01). An abbreviation stands as a
#: word of its own: "ს/კ" is not the end of a longer word.
ID_LABEL = (
    r"(?:(?<![ა-ჰ])(?:ს\s*[/.]\s*[კნ]|პ\s*/\s*ნ)(?![ა-ჰ])\.?"
    r"|(?<![ა-ჰ])საიდ(?:ენტიფიკაციო(?![ა-ჰ])|\.)\s*"
    r"(?:(?:კოდ|ნომ(?:ერ|რ))(?:ი|ის|ით|ად)?(?![ა-ჰ])|№)?"
    r"|(?<![ა-ჰ])პირადი\s*ნომ(?:ერ|რ)(?:ი|ის|ით|ად)?(?![ა-ჰ])"
    # English labels start at a word boundary: "paid 204567890" is not "ID 204567890"
    r"|(?<![A-Za-z])(?:ID(?![A-Za-z])\s*(?:code|no\.?|number|№)?"
    r"|identification\s*(?:code|number|no\.?)|tax\s*id(?![A-Za-z])))"
)
#: between the label and the code: a number sign ("ს/კ N 204567890", "№"), a colon or a dash
_ID_SEPARATOR = r"(?:\s*(?:N|№|#)(?![A-Za-z]))?\s*[:\-]?\s*"
#: the code: 9 digits (a company) or 11 (a person), compact or a company's printed in threes
_ID_VALUE = r"(\d{9}|\d{11}|\d{3} \d{3} \d{3})(?!\d)"
#: An identification code behind its label; group 1 is the code as written (``id_code_digits``).
ID_CODE_RE = re.compile(ID_LABEL + _ID_SEPARATOR + _ID_VALUE, re.IGNORECASE)


def id_code_digits(code: str) -> str:
    """A code as its digits alone: "406 444 789" is 406444789."""
    return re.sub(r"\D", "", code)


# --- IBAN ---------------------------------------------------------------------------------------

#: ISO 4217 codes a Georgian bank prints after an account to name its currency sub-account
#: ("GE00NB0000000000000000GEL"). The account is the IBAN; the code is no part of it.
IBAN_CURRENCY_SUFFIXES = frozenset(
    {"GEL", "USD", "EUR", "GBP", "CHF", "RUB", "TRY", "AZN", "AMD", "UAH", "CNY"}
)

_IBAN_CLEAN = re.compile(r"[^A-Z0-9]")

#: ISO 13616 registry: an IBAN's length is fixed by its country. Only the countries these
#: documents meet, or their neighbours and banks' correspondents, and only lengths that are
#: certain; for any other country the length is unknown and a reader falls back on the checksum.
_IBAN_LENGTHS: dict[str, int] = {
    "GE": 22,
    "AT": 20,
    "AZ": 28,
    "BE": 16,
    "CH": 21,
    "DE": 22,
    "ES": 24,
    "FR": 27,
    "GB": 22,
    "IT": 27,
    "NL": 18,
    "PL": 28,
    "TR": 26,
    "UA": 29,
}


def iban_length(country: str) -> int | None:
    """The fixed length of ``country``'s IBANs (GE: 22), or None when this table lacks it.

    Where a grouped IBAN or one broken over a line ends is a question of length, not of the
    checksum: the client's anonymised accounts fail the checksum, and so does a mistyped one -
    which is exactly the account the acceptance check must still read and report.
    """
    return _IBAN_LENGTHS.get(country.upper())


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
