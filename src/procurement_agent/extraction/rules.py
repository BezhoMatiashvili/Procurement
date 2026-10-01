"""Deterministic extraction (no LLM): amounts, dates, ID codes, percentages, durations,
contract numbers — in Georgian and English.

Each hit carries the character span inside the input text so the caller can attach
provenance. Classification into procurement-specific types uses nearby keywords; anything
ambiguous stays generic (``amount``/``date``/...) and is left for the LLM to refine.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

from procurement_agent.extraction.georgian import month_typo, parse_number_words
from procurement_agent.extraction.normalize import (
    FIGURE,
    FIGURE_SPACE,
    IBAN_CURRENCY_SUFFIXES,
    ID_CODE_RE,
    amounts_equal,
    edit_distance,
    iban_is_valid,
    id_code_digits,
    unlabelled_bic_plausible,
)
from procurement_agent.extraction.normalize import parse_number as _parse_number
from procurement_agent.extraction.schemas import FactType

# --- helpers -----------------------------------------------------------------------------------

_KA_MONTHS = {
    "იანვ": 1, "თებერ": 2, "მარტ": 3, "აპრილ": 4, "მაის": 5, "ივნის": 6,
    "ივლის": 7, "აგვისტ": 8, "სექტემბ": 9, "ოქტომბ": 10, "ნოემბ": 11, "დეკემბ": 12,
}  # fmt: skip
_EN_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_KA_MONTH_RE = "|".join(_KA_MONTHS)
# A month name, possibly misspelled. Resolved by _month_ka, which rejects non-months, so
# the surrounding day+year structure plus that rejection is what keeps this from over-matching.
_KA_MONTH_FUZZY = r"[ა-ჰ]{3,14}"
_EN_MONTH_RE = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)

#: The endings a currency's name takes in the case the sentence needs: nominative, genitive (and
#: its long form), dative, instrumental, adverbial. One set for every consonant-stem currency:
#: the dollar had "-ით" and "-ად" while the lari did not, so "3 180 ლარით" was no amount.
_CUR_CASES = r"(?:ი|ის|ისა|ს|ით|ად)?"
#: A currency as the documents write it after an amount: the code or sign, or the name in the
#: case the sentence needs - "აშშ დოლარს" (dative) is how a Georgian act states a dollar fee.
#: Sterling only as "pound sterling": "100 pounds" is a weight as often as it is money. The lari
#: may be "ქართული ლარი", "Lari", or abbreviated "ლ." - which is also a litre, so an amount in
#: "ლ." is kept only where a word about money types it (``find_amounts``).
_CUR_RE = (
    r"₾|\$|€|£|GEL|USD|EUR|GBP"
    rf"|(?:ქართულ(?:ი|მა)?\s+)?ლარ{_CUR_CASES}"
    rf"|(?:აშშ\s*|ა\.შ\.შ\.\s*)?დოლარ{_CUR_CASES}"
    r"|ევრო(?:ს|თი|დ)?"
    rf"|(?:გირვანქა|ფუნტი?)\s*სტერლინგ{_CUR_CASES}"
    r"|(?:US\s*|U\.S\.\s*)?dollars?|euros?|pounds?\s+sterling|lari"
    r"|ლ\.(?![ა-ჰA-Za-z])"
)
#: The lari abbreviated, which is also a litre: an amount in it needs a money word to be one.
_LARI_ABBREVIATION = "ლ."
#: A currency written *before* an amount: a code or a sign only. A name there is the end of a
#: sentence about something else - "... აშშ დოლარს 2026 წლის" read as 2,026 lari, because the
#: name pattern found "ლარს" inside "დოლარს" - never "ლარი 100".
_CUR_BEFORE_RE = r"(?<![A-Za-z])(?:GEL|USD|EUR|GBP)|₾|\$|€|£"
#: What each currency is called, tested in this order: dollars before lari, because "დოლარ"
#: (dollar) contains "ლარ" (lari).
_CURRENCY_NAMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("USD", re.compile(r"\$|usd|დოლარ|dollar", re.IGNORECASE)),
    ("EUR", re.compile(r"€|eur|ევრო", re.IGNORECASE)),
    ("GBP", re.compile(r"£|gbp|სტერლინგ|sterling", re.IGNORECASE)),
    ("GEL", re.compile(r"₾|gel|ლარ|lari|ლ\.", re.IGNORECASE)),
)
#: a figure, read by the one pattern every reader shares (``normalize.FIGURE``)
_NUM_RE = FIGURE

# context windows (chars) used to classify generic hits
_CTX = 80

KEYWORDS: dict[FactType, tuple[str, ...]] = {
    FactType.warranty_period: ("გარანტი", "საგარანტიო", "warrant", "guarantee period"),
    # the verb as well as the noun: "ვალდებულია მიაწოდოს „საქონელი“ ... -მდე", "შეასრულოს
    # „მომსახურება“ ... დღის ვადაში" state the deadline as plainly as "მიწოდების ვადა"
    FactType.delivery_deadline: ("მიწოდებ", "მოწოდებ", "მიაწოდ", "მოაწოდ", "deliver", "delivery"),
    FactType.execution_period: (
        "შესრულებ",
        "შესრულდა",
        "შეასრულ",
        "დასრულებ",
        "execution",
        "completion",
        "perform",
    ),
    # "prepayment" whole: a Latin keyword starts a word, so "payment" is not found inside it
    FactType.payment_terms: (
        "გადახდ",
        "ანგარიშსწორებ",
        "payment",
        "invoice",
        "ავანს",
        "advance",
        "prepayment",
        "pre-payment",
    ),
    FactType.validity_period: ("ძალაშ", "მოქმედებ", "valid", "validity", "expire"),
    FactType.penalty: ("პირგასამტეხლო", "ჯარიმ", "საურავ", "penalt", "liquidated damages"),
    FactType.total_amount: (
        "სულ",
        "ჯამ",
        "საერთო ღირებულებ",
        "საფასურ",
        "ღირებულებ",
        "ანაზღაურებ",
        "total",
        "grand total",
        "sum",
        "fee",
    ),
    FactType.price: ("ერთეულის ფას", "ფას", "price", "unit price", "cost", "rate"),
    # The act's own signing: "„აქტი“ გაფორმდა 2026 წლის 31 აგვისტო". These end where the bare
    # "გაფორმ" of contract_date ends, and the longer keyword wins a tie, so the date an act was
    # signed stops being reported as the contract's date. Every quote style is spelled out
    # because Georgian stems match as plain substrings.
    FactType.act_date: (
        "აქტი გაფორმ",
        "აქტი“ გაფორმ",
        "აქტი” გაფორმ",
        'აქტი" გაფორმ',
        "აქტი» გაფორმ",
        "აქტის გაფორმ",
        "აქტის თარიღ",
        "act was signed",
        "act date",
    ),
    FactType.contract_date: (
        "ხელშეკრულებ",
        "შეთანხმებ",
        "გაფორმ",
        "contract",
        "agreement",
        "dated",
        "თარიღ",
    ),
    FactType.vat_status: ("დღგ", "vat", "დამატებული ღირებულების"),
}


@dataclass(frozen=True, slots=True)
class RuleHit:
    type: FactType
    value: str
    raw_text: str
    start: int
    end: int
    normalized: str | float | None = None
    unit: str | None = None
    currency: str | None = None
    confidence: float = 0.6
    note: str | None = None


# Newlines are NOT boundaries: Docling emits inline runs ("შეადგენს\n100\n(ასი) ლარს") and
# table cells on separate lines; the nearest-keyword rule keeps neighbouring rows apart. The one
# exception is a label line (``label_line_spans``).
_SENTENCE_BOUNDARY = re.compile(r"[.;!?]")
_KW_CACHE: dict[str, re.Pattern[str]] = {}

#: A "label: value" line: a short label of words (no figure, quote or comma), a colon, and a
#: value on the same line, opened by a bullet or not - "მომსახურების შესრულების ვადა:
#: 10.03.2026 - 31.07.2026", "- საგარანტიო ვადა: 12 თვე".
_LABEL_LINE = re.compile(
    r"^[ \t]*(?:[-\u2013\u2014•][ \t]*)?[ა-ჰA-Za-z][ა-ჰA-Za-z /()\-]{0,58}?[ \t]*:[ \t]*"
    r"(?P<value>\S[^\n]*?)[ \t]*$",
    re.MULTILINE,
)
#: A value that goes on in the next line: it ends with a comma, a dash, an open bracket or
#: "და" / "and".
_VALUE_GOES_ON = re.compile(r"(?:[,(\-\u2013\u2014]|(?<![ა-ჰA-Za-z])(?:და|and))$", re.IGNORECASE)


def label_line_spans(text: str) -> list[tuple[int, int]]:
    """Every "label: value" line with no full stop of its own, as (start, end): a sentence by
    itself (docs/GOLD_SPEC.md section 2, red team R4). Terms written as such lines - one Word
    paragraph each - were read as one sentence, so a payment deadline on one line took the
    warranty's keyword on the next. A line whose value runs on (``_VALUE_GOES_ON``) is none,
    nor a line ending with its own stop, nor an inline run ("შეადგენს\\n100\\n(ასი) ლარს")."""
    return [
        (m.start(), m.end())
        for m in _LABEL_LINE.finditer(text)
        if not _VALUE_GOES_ON.search(m.group("value")) and m.group("value")[-1] not in ".;!?"
    ]


def close_label_lines(text: str) -> str:
    """``text`` with a full stop after each label line's value (``label_line_spans``), for a
    reader that splits sentences at stops only."""
    out: list[str] = []
    pos = 0
    for _, end in label_line_spans(text):
        out += [text[pos:end], "."]
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _label_line_bounds(text: str, a: int, b: int) -> list[int]:
    """The line breaks in ``text[a:b]`` that end or begin a label line."""
    bounds: list[int] = []
    for p in (i for i in range(a, b) if text[i] == "\n"):
        line_start = text.rfind("\n", 0, p) + 1
        nxt = text.find("\n", p + 1)
        before = text[line_start:p]
        after = text[p + 1 : len(text) if nxt < 0 else nxt]
        if label_line_spans(before) or label_line_spans(after):
            bounds.append(p)
    return bounds


_GEORGIAN_LETTER = re.compile(r"[\u10a0-\u10ff]")


def keyword_pattern(kw: str) -> re.Pattern[str]:
    """Latin keywords must start at a word boundary ("sum" must not match inside "ipsum").

    Georgian stems are matched anywhere: the language builds words with prefixes, so "ფას"
    must match inside "საფასური" (fee) and "ღირებულებ" inside "მთლიანი ღირებულების".
    """
    pat = _KW_CACHE.get(kw)
    if pat is None:
        prefix = "" if _GEORGIAN_LETTER.search(kw) else r"(?<!\w)"
        pat = _KW_CACHE[kw] = re.compile(prefix + re.escape(kw))
    return pat


def keyword_in(text: str, kw: str) -> bool:
    return keyword_pattern(kw).search(text) is not None


_AFTER_PENALTY = 20  # keywords that follow a value are weaker evidence than those preceding it


def nearest_keyword_group[K: Hashable](
    text: str, start: int, end: int, groups: Mapping[K, Iterable[str]]
) -> K | None:
    """The group whose keyword sits closest to ``text[start:end]`` *within the same sentence*.

    At the same distance the longer keyword wins, because Georgian builds words with prefixes
    and stems therefore nest: "ფას" (price) matches two characters inside "საფასურ" (fee), and
    without this the fee in an acceptance act is labelled a unit price. Remaining ties are
    resolved by group order, so callers list the more specific group first.

    Shared by the fact classifier below and by the checks that give an amount its role in a
    contract (price, advance, penalty ...), so both weigh the same evidence the same way.
    """
    lo, hi = max(0, start - _CTX), min(len(text), end + _CTX)
    pre = text[lo:start].lower()
    post = text[end:hi].lower()
    # keep only the sentence fragment adjacent to the hit; a label line is a sentence of its own
    cut = list(_SENTENCE_BOUNDARY.finditer(pre))
    if cut:
        pre = pre[cut[-1].end() :]
    m = _SENTENCE_BOUNDARY.search(post)
    if m:
        post = post[: m.start()]
    if "\n" in pre or "\n" in post:
        lines_before = _label_line_bounds(text, start - len(pre), start)
        if lines_before:
            pre = pre[lines_before[-1] + 1 - (start - len(pre)) :]
        lines_after = _label_line_bounds(text, end, end + len(post))
        if lines_after:
            post = post[: lines_after[0] - end]

    best: tuple[int, int, K] | None = None
    for group, keywords in groups.items():
        for kw in keywords:
            pat = keyword_pattern(kw)
            pre_matches = list(pat.finditer(pre))
            if pre_matches:
                dist = len(pre) - pre_matches[-1].end()
                # sort key: nearer first, then longer keyword
                if best is None or (dist, -len(kw)) < (best[0], -best[1]):
                    best = (dist, len(kw), group)
            m_post = pat.search(post)
            if m_post:
                dist = m_post.start() + _AFTER_PENALTY
                if best is None or (dist, -len(kw)) < (best[0], -best[1]):
                    best = (dist, len(kw), group)
    return best[2] if best else None


def _classify(text: str, start: int, end: int, candidates: tuple[FactType, ...]) -> FactType | None:
    """Pick the candidate type whose keyword sits closest to the hit (``nearest_keyword_group``
    over ``KEYWORDS``). Callers list the more specific type first: it wins a tie."""
    return nearest_keyword_group(text, start, end, {ft: KEYWORDS.get(ft, ()) for ft in candidates})


def parse_number(s: str) -> float | None:
    """Kept as a name for callers of this module; the one implementation is in ``normalize``.

    This used to be a copy that had drifted: it did not know a minus sign, nor any space
    character other than the no-break space, so the rules and the scorer could read the same
    string as two different numbers.
    """
    return _parse_number(s)


def _norm_currency(raw: str) -> str | None:
    """The ISO code of a currency as written: "აშშ დოლარს" is USD, "ლარს" GEL."""
    return next((code for code, name in _CURRENCY_NAMES if name.search(raw)), None)


# --- amounts -----------------------------------------------------------------------------------

# "48 750,00 (ორმოცდარვა ათას ...) ლარს": contracts spell the number out between digits and currency
_PAREN = (
    r"(?:\([^()]{0,160}?"
    # a scan reads the closing bracket as ":" or "|" now and then; before a currency word
    # nothing else can be meant
    rf"(?:\)|[:;|\]}}](?=\s*(?:{_CUR_RE})))\s*)?"
)
#: A magnitude written as a word after the figure: "48 ათას ლარს", "1.2 მლნ ლარს", "1,2 მილიონ
#: ლარს", "2.5 million GEL". Only these words: "ათასობით" (thousands of) is no magnitude, and a
#: bare "m" is as often metres.
_MAGNITUDES: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"ათას|ათ\.|thousand", re.IGNORECASE), 1e3),
    (re.compile(r"მილიონ|მლნ|million|mln", re.IGNORECASE), 1e6),
    (re.compile(r"მილიარდ|მლრდ|billion|bn", re.IGNORECASE), 1e9),
)
_MAGNITUDE_RE = (
    r"(?:(?:ათას|მილიონ|მილიარდ)(?:ი|ს|ის|ით|ად)?(?![ა-ჰ])|ათ\.|მლნ\.?|მლრდ\.?"
    r"|(?:thousand|million|billion|mln|bn)(?![A-Za-z]))"
)
#: The figure's words in brackets between the figure and its magnitude: "36 (ოცდათექვსმეტი) ათას
#: ლარს", "1,2 (ერთი მთელი ორი) მილიონ ლარს". Letters and spaces only, and only where a magnitude
#: word follows: a bracket before the currency is ``_PAREN``'s.
_WORDS_BEFORE_MAGNITUDE = rf"(?:\s*\(\s*[ა-ჰA-Za-z][ა-ჰA-Za-z\s-]{{0,80}}\)(?=\s*{_MAGNITUDE_RE}))?"
_AMOUNT_AFTER = re.compile(
    rf"(?<![\d.,])({_NUM_RE}){_WORDS_BEFORE_MAGNITUDE}(?:\s*({_MAGNITUDE_RE}))?\s*{_PAREN}"
    rf"({_CUR_RE})(?![A-Za-zა-ჰ])",
    re.IGNORECASE,
)
_AMOUNT_BEFORE = re.compile(
    rf"({_CUR_BEFORE_RE})\s*({_NUM_RE})(?![\d]){_WORDS_BEFORE_MAGNITUDE}(?:\s*({_MAGNITUDE_RE}))?",
    re.IGNORECASE,
)


def _magnitude(word: str | None) -> float:
    """What a magnitude word multiplies its figure by; 1 with none."""
    if not word:
        return 1.0
    return next((factor for pat, factor in _MAGNITUDES if pat.match(word)), 1.0)


#: "2 400.00 (ორი ათას ოთხასი ლარი და 00 თეთრი)": the currency is written inside the words,
#: with tetri, and nothing after the bracket names it. The bracket must open with words.
_AMOUNT_CUR_INSIDE = re.compile(
    rf"(?<![\d.,])({_NUM_RE})\s*\(\s*([ა-ჰ][ა-ჰ ,]*?(?<![ა-ჰ])(?:{_CUR_RE})(?![A-Za-zა-ჰ])"
    r"[^()]{0,60})\)",
    re.IGNORECASE,
)

# A number whose only separators are spaces and which has no decimal part: "5 100". Written
# after a quantity keyword it is far more likely a count followed by a price ("რაოდენობა 5
# 100 ლარი") than one amount of 5 100, so only the group next to the currency is the amount.
# With a decimal part ("1 234 567,89") the grouping is unambiguous and is kept whole.
_SPACE_GROUPED = re.compile(rf"\d{{1,3}}(?:{FIGURE_SPACE}\d{{3}})+")
_GROUP_SPACE = re.compile(FIGURE_SPACE)
_QUANTITY_BEFORE = re.compile(r"(?:რაოდენობ[ა-ჰ]*|quantity|qty)\.?\s*$", re.IGNORECASE)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Half-open interval overlap, including one span wholly containing the other."""
    return a[0] < b[1] and b[0] < a[1]


#: Tetri in figures after a lari amount: "4 150 ლარი და 35 თეთრი", "4,150 lari and 35 tetri".
_TETRI_AFTER = re.compile(
    r"\s*,?\s*(?:და\s+|and\s+)?(\d{1,2})\s*(?:თეთრ[ა-ჰ]*|tetri)(?![A-Za-zა-ჰ])",
    re.IGNORECASE,
)
_HAS_FRACTION = re.compile(r"[.,]\d{1,2}$")


def _with_tetri(text: str, number: str, end: int, value: float) -> tuple[float, int]:
    """A lari amount with the tetri written after it in figures, as one amount: the value and the
    end of its span. A figure with a fraction of its own takes no more tetri."""
    m = _TETRI_AFTER.match(text, end)
    if m is None or _HAS_FRACTION.search(number):
        return value, end
    return round(value + int(m.group(1)) / 100, 2), m.end()


#: A cell of a serialised table row: "1, ერთეულის ფასი = 30.00 ლარი. 1, ჯამი = 3 000.00 ლარი" -
#: the row's key, then the column's name, then the value; each cell ends in ". ". A key holds no
#: full stop, except the item number it may be ("1.1").
_TABLE_CELL = re.compile(
    r"(?:^|(?<=\.\s))(\d{1,3}(?:\.\d{1,3})+|[^,=.\n]{0,60}),\s([^=\n]{1,80}?)\s=\s", re.MULTILINE
)
#: A column that makes a row a priced line: its unit price, quantity or unit.
_LINE_COLUMN = re.compile(
    r"ფას|რაოდენობ|ერთეულ|price|rate|quantity|qty|unit",
    re.IGNORECASE,
)
#: A row a total word names, as its key or as a cell's whole value: "სულ", "ჯამი", "Total".
_TOTAL_ROW = re.compile(
    r"\s*(?:სულ|ჯამ[ა-ჰ]*|საერთო|(?:grand\s+|sub-?)?total)(?![A-Za-zა-ჰ]).*",
    re.IGNORECASE,
)


#: A row keyed by an item number: "1", "2.", "1.1", "3)".
_ITEM_NUMBER = re.compile(r"\d{1,3}(?:\.\d{1,3})*[.)]?")


def _is_line_total(text: str, start: int) -> bool:
    """Whether the amount at ``start`` is a line amount of a price table: a cell of a column a
    total word names ("ჯამი"), in a row that no total word names and that is a line - it has a
    unit price or a quantity, it is keyed by an item number, or the table has a total row of
    its own. A lump-sum table ("N | დასახელება | ჯამი") has neither price nor quantity, and its
    lines are still lines. The document's total is the row "სულ" or a figure in the prose."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    line = text[line_start : len(text) if line_end < 0 else line_end]
    at = start - line_start
    cells = list(_TABLE_CELL.finditer(line))
    own = next((c for c in reversed(cells) if c.end() <= at), None)
    column = own.group(2).lower() if own is not None else ""
    if own is None or not any(keyword_in(column, kw) for kw in KEYWORDS[FactType.total_amount]):
        return False
    row = own.group(1).strip()
    siblings = [c for c in cells if c.group(1).strip() == row]
    values = {
        c: line[c.end() : nxt.start() if nxt else len(line)].strip(" .")
        for c, nxt in zip(cells, [*cells[1:], None], strict=True)
    }

    def names_a_total(key: str) -> bool:
        return _TOTAL_ROW.fullmatch(key) is not None or any(
            _TOTAL_ROW.fullmatch(v) for c, v in values.items() if c.group(1).strip() == key
        )

    if names_a_total(row):
        return False
    if any(_LINE_COLUMN.search(c.group(2)) for c in siblings if c is not own):
        return True
    if _ITEM_NUMBER.fullmatch(row):
        return True
    return any(names_a_total(key) for key in {c.group(1).strip() for c in cells} - {row})


#: A figure that restates a share of the value written right before it: "ღირებულების 5%-ის,
#: 100 ლარის ოდენობით", "დღგ 18% - 305.08 ლარი", "5% of the Contract price, GEL 100.00", "30%,
#: ანუ 3 000 ლარი"; or a tax the price holds: "მათ შორის დღგ - 305.08 ლარი", "of which VAT: 18
#: GEL". Only right before it: "ღირებულება (18% დღგ-ს ჩათვლით) შეადგენს 1 000 ლარს" is the
#: price, and so is "Price including VAT: 118 GEL".
_SHARE_BEFORE = re.compile(
    r"(?:\d{1,3}(?:[.,]\d{1,2})?\s*(?:%|პროცენტ[ა-ჰ]*|per\s?cent)(?:-?[ა-ჰ]+)?"
    r"(?:\s+of\s+(?:the\s+)?(?:total\s+)?(?:contract\s+)?(?:value|price|amount|sum|fee))?"
    r"|(?<![ა-ჰa-z])(?:მათ\s+შორის|საიდანაც|აქედან|of\s+which)\s+(?:დღგ|vat)(?:-?ის|-?ს)?"
    r"(?:\s+(?:თანხა|ოდენობა|amount))?)"
    r"[\s,:;=\u2013\u2014-]*"
    r"(?:(?:ანუ|რაც\s+შეადგენს|i\.\s*e\.|that\s+is|namely|which\s+is)[\s,:]*)?"
    r"(?:(?:GEL|USD|EUR|GBP|₾|\$|€|£)\s*)?$",
    re.IGNORECASE,
)
_SHARE_REACH = 80


def _is_share(text: str, start: int) -> bool:
    """Whether the amount at ``start`` restates a share of the value (a guarantee, an advance,
    the VAT inside the price) rather than stating the value. Its nearest keyword is the value's
    ("ღირებულების 5%-ის, 100 ლარის ოდენობით"), which names the base of the share, not the amount."""
    return _SHARE_BEFORE.search(
        text, max(0, start - _SHARE_REACH), start
    ) is not None or is_tax_amount(text, start)


#: A tax named inside a bracket, before the figure there: "(დღგ-ს გარეშე 1 000 ლარი)", "(excl.
#: VAT GEL 1,000)".
_TAX_WORD = re.compile(r"დ\.?ღ\.?გ|გადასახად|(?<![a-z])(?:vat|tax(?:es)?)(?![a-z])", re.IGNORECASE)
#: How far a bracket may open before its figure, and a money figure before the bracket.
_BRACKET_REACH = 60
_RESTATED_REACH = 60


def _restating_bracket(text: str, start: int) -> tuple[int, int] | None:
    """The bracket around the figure at ``start`` when it restates the money figure written right
    before it on another tax basis: "1 180 ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 1 000 ლარი)", "GEL
    1,180 incl. VAT (excl. VAT GEL 1,000)". Its span, "(" to ")"; None otherwise.

    The bracket names a tax before its figure, and a money figure stands before the bracket in
    the same sentence: the figure is that price again, not a second one, and the tax wording in
    the bracket is about it, not about the price (``document_facts`` reads the VAT status so).
    A bracket with no price before it ("ფასი (დღგ-ს ჩათვლით 1 180 ლარი)") is the price itself.
    """
    opened = text.rfind("(", max(0, start - _BRACKET_REACH), start)
    if opened < 0 or ")" in text[opened:start]:
        return None
    closed = text.find(")", start, start + _BRACKET_REACH * 2)
    if closed < 0 or not _TAX_WORD.search(text, opened, start):
        return None
    before = text[max(0, opened - _RESTATED_REACH) : opened]
    stops = list(_SENTENCE_END.finditer(before))
    before = before[stops[-1].end() :] if stops else before
    if not (_AMOUNT_AFTER.search(before) or _AMOUNT_BEFORE.search(before)):
        return None
    return opened, closed + 1


#: Where a sentence turns to another amount: ", საიდანაც ავანსი ...", ", ხოლო საგარანტიო თანხა
#: ...", ", მათ შორის ...", ", of which the advance ...", ", whereas ...".
_AMOUNT_TURN = re.compile(
    r"[,;\u2013\u2014]\s*(?:საიდანაც|აქედან|ხოლო|მათ\s+შორის|of\s+which|whereas|while)(?![ა-ჰa-z])",
    re.IGNORECASE,
)
#: What an amount is when its own clause names it so: an advance, a guarantee, a retention, a
#: deposit - a part of the price or a security, never the document's total.
_PART_OF_PRICE = re.compile(
    r"ავანს|წინასწარ|საგარანტიო|გარანტი|უზრუნველყოფ(?:ა|ის|ად)(?![ა-ჰ])|დაკავ|"
    r"(?<![a-z])(?:advance|prepayment|guarantee|retention|security|deposit)(?![a-z])",
    re.IGNORECASE,
)
#: A price per period ("ყოველთვიური ღირებულება", "monthly fee") and the total a sentence names
#: beside it ("საერთო ღირებულება", "total value").
_PERIODIC = re.compile(
    r"ყოველთვიურ|თვიურ|ყოველწლიურ|კვარტალურ|(?<![a-z])(?:monthly|annual|quarterly|per\s+month)"
    r"(?![a-z])",
    re.IGNORECASE,
)
_TOTAL_NAMED = re.compile(
    r"საერთო|ჯამურ|სრულ|მთლიან|(?<![a-z])(?:total|overall|aggregate)(?![a-z])", re.IGNORECASE
)
_CLAUSE_REACH = 160


def _clauses_of(text: str, start: int) -> tuple[str, list[str]]:
    """The clause the figure at ``start`` stands in, up to it, and every other clause of its
    sentence: a sentence is cut at its turns (``_AMOUNT_TURN``)."""
    lo = max(0, start - _CLAUSE_REACH)
    before = text[lo:start]
    stops = list(_SENTENCE_END.finditer(before))
    a = lo + (stops[-1].end() if stops else 0)
    stop = _SENTENCE_END.search(text, start, start + _CLAUSE_REACH)
    b = stop.start() if stop else min(len(text), start + _CLAUSE_REACH)
    turns = [m.start() for m in _AMOUNT_TURN.finditer(text, a, b)]
    cuts = [a, *turns, b]
    own = sum(1 for t in turns if t <= start)
    clauses = [text[cuts[i] : cuts[i + 1]] for i in range(len(cuts) - 1)]
    return text[cuts[own] : start], [c for i, c in enumerate(clauses) if i != own]


def _named_in_own_clause(text: str, start: int) -> bool:
    """Whether the figure at ``start`` is a part of the price or a periodic price that its own
    clause names so, in a sentence another clause of which states the price: "... 36 000 ლარს,
    საიდანაც ავანსი შეადგენს 10 800 ლარს", "ყოველთვიური ღირებულება 3 000 ლარი, ხოლო საერთო
    ღირებულება 36 000 ლარი". Such a figure is an ``amount``: the total is the other one.

    Only past a turn of the sentence, or beside a clause naming the total: "ღირებულება, ავანსის
    ჩათვლით, შეადგენს 36 000 ლარს" has no turn, and 36 000 stays the price."""
    own, others = _clauses_of(text, start)
    if not others:
        return False
    if _AMOUNT_TURN.match(own) and _PART_OF_PRICE.search(own):
        return True
    return _PERIODIC.search(own) is not None and any(
        _TOTAL_NAMED.search(c) and (_AMOUNT_AFTER.search(c) or _AMOUNT_BEFORE.search(c))
        for c in others
    )


# --- a price restated on another tax basis -------------------------------------------------------

#: A figure's tax basis: with the tax ("დღგ-ს ჩათვლით", "გადასახადების ჩათვლით", "including
#: VAT") or without it ("დღგ-ს გარეშე", "excl. VAT", "net of VAT").
_TAX_INCLUDED = re.compile(
    r"(?:დ\.?ღ\.?გ\.?|გადასახად[ა-ჰ]*)(?:-?ის|-?ს)?\s+(?:\(\s*\d{1,2}(?:[.,]\d+)?\s*%\s*\)\s*)?"
    r"ჩათვლით"
    r"|(?<![a-z])(?:including|inclusive\s+of|incl\.?)\s+(?:\d{1,2}\s*%\s+)?(?:vat|tax(?:es)?)"
    r"(?![a-z])|(?<![a-z])vat\s+(?:is\s+)?included(?![a-z])",
    re.IGNORECASE,
)
_TAX_EXCLUDED = re.compile(
    r"(?:დ\.?ღ\.?გ\.?|გადასახად[ა-ჰ]*)(?:-?ის|-?ს)?\s+(?:გარეშე|გამოკლებით)"
    r"|(?<![a-z])(?:excluding|exclusive\s+of|excl\.?|net\s+of|before|without)\s+"
    r"(?:vat|tax(?:es)?)(?![a-z])",
    re.IGNORECASE,
)
#: The tax named as a figure's own label, right before it: "დღგ - 915.25 ლარი", "დღგ (18%):
#: 915.25 ლარი", "დღგ-ს თანხა: ...", "VAT: GEL 180". Not the price's basis: "Price including
#: VAT: 118 GEL" and "ფასი + დღგ: 1 180 ლარი" label the price (``_TAX_QUALIFIED``).
_TAX_AMOUNT_LABEL = re.compile(
    r"(?<![ა-ჰa-z])(?:დ\.?ღ\.?გ\.?|vat|value\s+added\s+tax)"
    r"(?:(?:-?ის|-?ს)\s+(?:თანხა|ოდენობა)|\s+(?:თანხა|ოდენობა|amount))?"
    r"(?:\s*\(\s*\d{1,2}(?:[.,]\d{1,2})?\s*%\s*\))?"
    r"\s*[:=\u2013\u2014-]?\s*(?:(?:GEL|USD|EUR|GBP|₾|\$|€|£)\s*)?$",
    re.IGNORECASE,
)
_TAX_QUALIFIED = re.compile(
    r"(?<![a-z])(?:incl\.?|including|inclusive\s+of|excl\.?|excluding|exclusive\s+of|with|"
    r"without|plus|net\s+of)\s*$|\+\s*$",
    re.IGNORECASE,
)
_TAX_LABEL_REACH = 60


def is_tax_amount(text: str, start: int) -> bool:
    """Whether the money figure at ``start`` is the tax itself, labelled by the tax's name right
    before it: "დღგ - 915.25 ლარი", "დღგ (18%): 915.25 ლარი" (docs/GOLD_SPEC.md section 2: the tax
    the price holds is an ``amount``, never ``total_amount`` or ``price``)."""
    before = text[max(0, start - _TAX_LABEL_REACH) : start]
    label = _TAX_AMOUNT_LABEL.search(before)
    return label is not None and _TAX_QUALIFIED.search(before, 0, label.start()) is None


def _money_at(text: str, start: int) -> re.Match[str] | None:
    return _AMOUNT_AFTER.match(text, start) or _AMOUNT_BEFORE.match(text, start)


def _next_money(text: str, pos: int, stop: int) -> re.Match[str] | None:
    found = [m for p in (_AMOUNT_AFTER, _AMOUNT_BEFORE) if (m := p.search(text, pos, stop))]
    return min(found, key=lambda m: m.start()) if found else None


def _has_money(text: str, a: int, b: int) -> bool:
    return _next_money(text, a, b) is not None


def _sentence_start(text: str, pos: int) -> int:
    lo = max(0, pos - _CLAUSE_REACH)
    stops = list(_SENTENCE_END.finditer(text, lo, pos))
    return stops[-1].end() if stops else lo


def _basis(text: str, a: int, b: int) -> tuple[str, re.Match[str]] | None:
    """The last tax basis stated in ``text[a:b]``: ("incl" or "excl", its match)."""
    found = [
        (kind, m)
        for kind, pat in (("incl", _TAX_INCLUDED), ("excl", _TAX_EXCLUDED))
        for m in pat.finditer(text, a, b)
    ]
    return max(found, key=lambda f: f[1].start()) if found else None


#: A total word naming the figure after it: "სულ 6 000 ლარი", "სულ დღგ-ს ჩათვლით: 6 000 ლარი",
#: "ჯამი: ...", "Total: ...".
_TOTAL_LABEL = re.compile(
    r"(?<![ა-ჰa-z])(?:სულ|ჯამ[ა-ჰ]*|(?:grand\s+)?total)(?![ა-ჰa-z])[^.;\n\d]{0,40}$", re.IGNORECASE
)
#: A clause number opening a sentence: a new clause, never the price's own.
_OPENS_CLAUSE = re.compile(r"\s*\d{1,2}(?:\.\d{1,2})+\.?\s")
_BREAKDOWN_REACH = 200


def _breakdown_part(text: str, start: int, end: int) -> bool:
    """Whether the figure at ``text[start:end]`` opens a breakdown of the price: the VAT
    labelled as such follows it, then the figure a total word names - "5 084.75 ლარი, დღგ -
    915.25 ლარი, სულ 6 000 ლარი", also on lines of their own. The total is the one the total
    word names; the figure before the tax is the price without it."""
    tax = _next_money(text, end, end + _BREAKDOWN_REACH)
    if tax is None or not is_tax_amount(text, tax.start()):
        return False
    total = _next_money(text, tax.end(), tax.end() + _BREAKDOWN_REACH)
    return total is not None and _TOTAL_LABEL.search(text, tax.end(), total.start()) is not None


def restatement(text: str, start: int) -> tuple[int, int] | None:
    """The words that make the money figure at ``start`` the price restated on another tax
    basis, not a second price; their span, or None. The figure is then an ``amount``, and the
    tax wording inside the span is about it, not about the price (``document_facts`` reads the
    VAT status so, and the money typing reads the figure so: the two cannot disagree).

    * a bracket after the price: "1 180 ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 1 000 ლარი)";
    * a clause past a turn of the price's sentence that names a tax basis: "36 000 ლარს დღგ-ს
      ჩათვლით, საიდანაც ღირებულება დღგ-ს გარეშე შეადგენს 30 508.47 ლარს", ", მათ შორის: დღგ-ს
      გარეშე - 30 508.47 ლარი";
    * the next sentence, stating the other basis of the price before it: "... 36 000 ლარს
      დღგ-ს ჩათვლით. ღირებულება დღგ-ს გარეშე შეადგენს 30 508.47 ლარს.";
    * the figure before the tax in a breakdown that a total closes (``_breakdown_part``).

    A net figure stated alone ("ღირებულება დღგ-ს გარეშე შეადგენს 30 000 ლარს") is the price, and
    so is a total its own clause names past a turn ("ავანსი 5 000 ლარი, ხოლო საერთო ღირებულება
    დღგ-ს ჩათვლით 36 000 ლარი")."""
    bracket = _restating_bracket(text, start)
    if bracket is not None:
        return bracket
    money = _money_at(text, start)
    if money is None:
        return None
    end = money.end()
    sentence = _sentence_start(text, start)
    # a turn may be the semicolon that ends the price's sentence: "6 000 ლარს; მათ შორის: ..."
    turn = next(reversed(list(_AMOUNT_TURN.finditer(text, max(0, sentence - 1), start))), None)
    if turn is not None:
        own = text[turn.start() : start]
        basis = _basis(text, turn.start(), start)
        priced = _sentence_start(text, turn.start())
        if (
            basis is not None
            and not _TOTAL_NAMED.search(own)
            and not _PERIODIC.search(own)
            and _has_money(text, priced, turn.start())
            and not _PART_OF_PRICE.search(text, priced, turn.start())
        ):
            return basis[1].start(), end
    basis = _basis(text, sentence, start)
    if (
        basis is not None
        and turn is None
        and sentence > 0
        and not _OPENS_CLAUSE.match(text, sentence)
        and not _has_money(text, sentence, start)
    ):
        previous = _sentence_start(text, sentence - 1)
        before = _basis(text, previous, sentence)
        if (
            before is not None
            and before[0] != basis[0]
            and _has_money(text, previous, sentence)
            and not _TOTAL_NAMED.search(text, sentence, start)
        ):
            return basis[1].start(), end
    if _breakdown_part(text, start, end):
        return (basis[1].start() if basis is not None else start), end
    return None


#: The name ``document_facts`` reads the restatement by; the bracket was its first form.
restating_bracket = restatement


def _money_type(text: str, span: tuple[int, int]) -> FactType | None:
    """What a money figure at ``span`` is: the total, a price, a penalty, or None (an
    ``amount``). The nearest keyword says, then the table and the sentence around it: a line of
    a price table, the contract's price named as its whole value, a share of the value, the
    price restated on another tax basis, a part of the price its own clause names."""
    ft = _classify(text, *span, (FactType.total_amount, FactType.penalty, FactType.price))
    if ft == FactType.total_amount and _is_line_total(text, span[0]):
        ft = None
    if ft == FactType.price and is_contract_price(text, *span):
        ft = FactType.total_amount  # "ხელშეკრულების ფასი", "the total price"
    if ft in (FactType.total_amount, FactType.price) and (
        _is_share(text, span[0])
        or restatement(text, span[0]) is not None
        or _named_in_own_clause(text, span[0])
    ):
        # a guarantee, an advance or the VAT, as a share of the value; the price again on
        # another tax basis; an advance or a monthly fee its clause names beside the total
        ft = None
    return ft


#: A currency a table's column header names, as a word of its own: "ღირებულება (ლარი)", "ჯამი,
#: ლარი", "თანხა ლარში", "Amount (USD)". Not "ლარ" inside another word ("ფორმულარი").
_HEADER_CURRENCY = re.compile(
    r"(?<![ა-ჰA-Za-z])(?:ლარ(?:ი|ის|ში)?|(?:აშშ\s+)?დოლარ[ა-ჰ]*|ევრო|GEL|USD|EUR|GBP|lari"
    r"|dollars?|euros?)(?![ა-ჰA-Za-z])|₾|\$|€|£",
    re.IGNORECASE,
)
#: A column of exchange rates, not of money: "დოლარის კურსი", "Rate (GEL)".
_RATE_COLUMN = re.compile(r"კურს|(?<![a-z])rate(?![a-z])", re.IGNORECASE)
#: A cell's value that is a figure alone, ended by the cell's stop, a space or the line's end.
_CELL_FIGURE = re.compile(rf"\s*({_NUM_RE})(?![\d.,]\d)(?=\.?(?:\s|$))")


def find_header_amounts(text: str) -> list[RuleHit]:
    """The money of a price table whose column header names the currency and whose cells hold
    the figures alone: "1, ერთეულის ფასი (ლარი) = 4 000. ... , დასახელება = სულ. , ჯამი (ლარი) =
    6 000" (Docling's "<row>, <column> = <cell>"). ``find_amounts`` needs a currency beside the
    figure, so such a table had no money at all; its cells are typed as a table's are: the row
    a total word names is the total, a line's total an ``amount``, a unit price a ``price``.
    A currency word before a figure in prose stays no currency ("დოლარს 2026"): only a cell
    under its column's header is read so. The act check reads these cells by its own reader
    (``checks.amounts.header_money``), which ``find_amounts`` does not feed."""
    hits: list[RuleHit] = []
    for cell in _TABLE_CELL.finditer(text):
        header = cell.group(2)
        named = _HEADER_CURRENCY.search(header)
        if named is None or _RATE_COLUMN.search(header):
            continue
        figure = _CELL_FIGURE.match(text, cell.end())
        if figure is None:
            continue
        value = parse_number(figure.group(1))
        cur = _norm_currency(named.group(0))
        if value is None or cur is None:
            continue
        span = figure.span(1)
        ft = _money_type(text, span)
        hits.append(
            RuleHit(
                type=ft or FactType.amount,
                value=f"{value:,.2f} {cur}",
                raw_text=figure.group(1),
                start=span[0],
                end=span[1],
                normalized=value,
                currency=cur,
                confidence=0.7 if ft else 0.5,
            )
        )
    return hits


# --- the contract's price ------------------------------------------------------------------------

#: The word for a price, its last letters any case ending: "ფასი", "ფასს", "price".
_PRICE_WORD = re.compile(r"(?<![ა-ჰ])ფას(?!ურ)[ა-ჰ]*|(?<![a-z])price(?![a-z])", re.IGNORECASE)
#: What names a price the contract's whole value: a total word ("საერთო ფასი", "the total
#: price"), or the contract or what it buys whose price it is ("ხელშეკრულების ფასი", "the
#: Contract price", "the price of the Goods").
_WHOLE_PRICE = re.compile(
    r"საერთო|ჯამურ|სრულ|მთლიან|ხელშეკრულებ|შეთანხმებ|მომსახურებ|საქონ|სამუშაო|შესყიდვ"
    r"|(?<![a-z])(?:total|overall|aggregate|contract|agreement|goods|services|works)(?![a-z])",
    re.IGNORECASE,
)
#: A price per unit: "ერთეულის ფასი", "1 ცალის ფასი", "the unit price", "GEL 450 per chair",
#: "თითო ცალზე".
_PER_UNIT = re.compile(
    r"ერთეულ|თითო|ცალ(?:ის|ზე)|(?:1|ერთი)\s+(?:ც|მ|კგ|ტ|ლ)|"
    r"(?<![a-z])(?:unit|per|each|apiece|a\s+piece)(?![a-z])|/\s*(?:ც|მ|კგ|ტ|ლ|pcs?|m|kg|t)(?![a-zა-ჰ])",
    re.IGNORECASE,
)
_PRICE_LABEL_WORDS = 3
_PER_UNIT_AFTER = 30


def is_contract_price(text: str, start: int, end: int) -> bool:
    """Whether the money figure at ``text[start:end]``, labelled a price, is the contract's whole
    value: a ``total_amount`` (docs/GOLD_SPEC.md section 2, red team R4). The price word before it
    in its sentence is named the whole price - by a total word, or as the contract's or the
    goods' price - and nothing says it is a price per unit: "ხელშეკრულების საერთო ფასი შეადგენს
    36 000 ლარს", "The total Contract price is GEL 36,000" are the total; "ერთეულის ფასი",
    "the unit price is GEL 450 per chair" stay a price, and so does a cell of a price table."""
    if _table_cell_at(text, start):
        return False
    sentence = _sentence_start(text, start)
    words = list(_PRICE_WORD.finditer(text, sentence, start))
    if not words:
        return False
    word = words[-1]
    between = text[word.end() : start]
    if _next_money(text, word.end(), start) is not None:
        return False  # another figure is that price's
    lead = text[sentence : word.start()].split()[-_PRICE_LABEL_WORDS:]
    label = " ".join(lead) + " " + text[word.start() : start]
    after = text[end : end + _PER_UNIT_AFTER]
    stop = _SENTENCE_END.search(after)
    after = after[: stop.start()] if stop else after
    if _PER_UNIT.search(label) or _PER_UNIT.search(after):
        return False
    named = " ".join(lead)
    return _WHOLE_PRICE.search(named) is not None or _WHOLE_PRICE.search(between) is not None


def _table_cell_at(text: str, start: int) -> bool:
    """Whether the figure at ``start`` is a cell of a serialised table row ("<row>, <column> =
    <cell>")."""
    line_start = text.rfind("\n", 0, start) + 1
    return any(c.end() <= start for c in _TABLE_CELL.finditer(text[line_start : start + 1]))


def find_amounts(text: str) -> list[RuleHit]:
    hits: list[RuleHit] = []
    seen: set[tuple[int, int]] = set()
    patterns = (
        (_AMOUNT_AFTER, 1, 3, 2),
        (_AMOUNT_CUR_INSIDE, 1, 2, None),
        (_AMOUNT_BEFORE, 2, 1, 3),
    )
    for pat, num_group, cur_group, magnitude_group in patterns:
        for m in pat.finditer(text):
            span = (m.start(), m.end())
            if any(_overlaps(span, other) for other in seen):
                continue
            number = m.group(num_group)
            if (
                pat is _AMOUNT_AFTER
                and _SPACE_GROUPED.fullmatch(number)
                and _QUANTITY_BEFORE.search(text[max(0, span[0] - 30) : span[0]])
            ):
                cut = max(g.end() for g in _GROUP_SPACE.finditer(number))
                number = number[cut:]
                span = (m.start(num_group) + cut, span[1])
            value = parse_number(number)
            if value is None:
                continue
            magnitude = _magnitude(m.group(magnitude_group) if magnitude_group else None)
            if magnitude != 1.0:
                value = round(value * magnitude, 2)
            cur = _norm_currency(m.group(cur_group))
            if pat is _AMOUNT_AFTER and cur == "GEL":
                value, tetri_end = _with_tetri(text, number, span[1], value)
                span = (span[0], tetri_end)
            ft = _money_type(text, span)
            if ft is None and m.group(cur_group) == _LARI_ABBREVIATION:
                continue  # "20 ლ." with no word about money: litres as likely as lari
            seen.add(span)
            hits.append(
                RuleHit(
                    type=ft or FactType.amount,
                    value=f"{value:,.2f} {cur}" if cur else f"{value:,.2f}",
                    raw_text=text[span[0] : span[1]],
                    start=span[0],
                    end=span[1],
                    normalized=value,
                    currency=cur,
                    confidence=0.7 if ft else 0.5,
                )
            )
    return hits


# --- dates -------------------------------------------------------------------------------------

#: The word for "year" after a date's year, always taken whole: "წ.", the abbreviation without its
#: stop ("წ" before a non-letter), or "წელი" / "წელს" / "წლის" (and "-ვე"). The bare letter alone
#: was taken as the abbreviation inside "წელს", so the date ended mid-word ("2026 წ") and its
#: correction dropped the rest ("2026 ").
_YEAR_WORD = r"წ(?:\.|(?:ელი|ელს|ლის|ლისა)(?:ვე)?(?![ა-ჰ])|(?![ა-ჰ]))"
#: ... and the same without the bare letter, which alone is too little to make "26" a year.
_YEAR_WORD_SPELLED = r"წ(?:\.|(?:ელი|ელს|ლის|ლისა)(?:ვე)?(?![ა-ჰ]))"
#: A numeric date, with the word for year when one follows it ("05.07.2026 წ.", "05.07.2026
#: წელს"): the word is part of the date, so the act's date is quoted whole and its correction
#: keeps the word, as for a two-digit year.
_DATE_NUMERIC = re.compile(
    rf"(?<!\d)(\d{{1,2}})[./-](\d{{1,2}})[./-](\d{{4}})(?!\d)(?:\s*{_YEAR_WORD})?"
)
#: A two-digit year, only where the word for "year" says it is one: "12.05.26 წ.". The word is
#: part of the date, so a correction written like it keeps it ("30.06.26 წ.").
_DATE_NUMERIC_SHORT_YEAR = re.compile(
    rf"(?<![\d./-])(\d{{1,2}})[./-](\d{{1,2}})[./-](\d{{2}})(?![\d./-])\s*{_YEAR_WORD_SPELLED}"
)
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
#: The day of a Georgian date: "30", in quotes as a contract heads its date ("„30“ ივნისი",
#: ",,30'' ივნისი" - the closing quote required, so a quoted name before a date is not taken
#: in), or as an ordinal ("1-ლი მაისი", the first).
_KA_DAY = (
    r"(?P<q>„|,,|\"|“|«)?(?P<d>\d{1,2})(?(q)(?:“|”|\"|''|»))"
    r"(?:\s*-\s*(?:ლი|ე)(?![ა-ჰ]))?"
)
_DATE_KA_YMD = re.compile(
    rf"(?<!\d)(?P<y>\d{{4}})[.,\s]*წ(?:ლის|\.)?\s*{_KA_DAY}\.?\s*(?P<m>{_KA_MONTH_FUZZY})",
    re.IGNORECASE,
)
#: A day and a month, then the year: "30 ივნისი, 2026 წ.", also with the end's "up to and
#: including" between them ("30 ნოემბრის ჩათვლით, 2026 წ."), which is part of the date.
_DATE_KA_DMY = re.compile(
    rf"(?<!\d){_KA_DAY}\.?\s*(?P<m>{_KA_MONTH_FUZZY})(?P<incl>\s+ჩათვლით)?,?\s*"
    rf"(?P<y>\d{{4}})(?!\d)(?:\s*{_YEAR_WORD})?",
    re.IGNORECASE,
)
#: A year written after a date and a postposition, and the word that makes it a year:
#: "30 ნოემბრამდე, 2026 წ." ends on 30 November 2026.
_TRAILING_YEAR = re.compile(rf"[\s,]*\d{{4}}(?:\s*{_YEAR_WORD})?\s*$")
_UNTIL_END = re.compile(r"(?:მდე|ჩათვლით)$")
_DATE_EN_DMY = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+({_EN_MONTH_RE})\.?,?\s+(\d{{4}})", re.IGNORECASE
)
_DATE_EN_MDY = re.compile(
    rf"\b({_EN_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", re.IGNORECASE
)


#: A period whose end, or whose start's month and year, the other end states: "2026 წლის 1
#: მაისიდან 31 მაისის ჩათვლით", "2026 წლის 1-დან 31 მაისის ჩათვლით", "2026 წლის 1-31 მაისის
#: პერიოდში", "1 მაისიდან 31 მაისის ჩათვლით, 2026 წელს".
_KA_DAY_MONTH_AFTER = re.compile(
    rf"\s*(\d{{1,2}})\s+({_KA_MONTH_FUZZY})(?![ა-ჰ])(?!\s*,?\s*\d{{4}})"
)
_KA_DAYS_OF_MONTH = re.compile(
    rf"(?<!\d)(?P<y>\d{{4}})[.,\s]*წ(?:ლის|\.)?\s*(?P<a>\d{{1,2}})\s*(?P<sep>-\s*დან|დან|[-\u2013])"
    rf"\s*(?P<b>\d{{1,2}})\s+(?P<m>{_KA_MONTH_FUZZY})"
)
_KA_RANGE_YEAR_AFTER = re.compile(
    rf"(?<![\d.])(\d{{1,2}})\s+({_KA_MONTH_FUZZY}დან)\s+(\d{{1,2}})\s+({_KA_MONTH_FUZZY})"
    rf"(?:\s+ჩათვლით)?\s*,?\s*(\d{{4}})\s*წ"
)

#: A date of a range: where it stands, its day, its words, and where the date that closes the
#: range stands (None for a range's last day, or a date that closes nothing).
_RangeDate = tuple[tuple[int, int], date, str, tuple[int, int] | None]


def _range_ends_without_year(
    text: str, found: dict[tuple[int, int], tuple[date, str, str | None]]
) -> list[_RangeDate]:
    """The dates of a Georgian period that take their year - or month and year - from the other
    end or from the year written after the period. Unread, "2026 წლის 1 მაისიდან 31 მაისის
    ჩათვლით" was a period of one day, and the other forms no period at all."""
    out: list[_RangeDate] = []

    def month_day(y: int, mo_word: str, d: int) -> date | None:
        mo = _month_ka(mo_word)
        return _safe_date(y, mo, d) if mo else None

    for (_start, end), (first, raw, _note) in list(found.items()):
        if not raw.endswith("დან"):
            continue
        m = _KA_DAY_MONTH_AFTER.match(text, end)
        if m is None:
            continue
        dt = month_day(first.year, m.group(2), int(m.group(1)))
        if dt is not None and dt < first:  # "20 დეკემბრიდან 10 იანვრამდე": the next year's
            dt = month_day(first.year + 1, m.group(2), int(m.group(1)))
        if dt is not None:
            out.append(((m.start(1), m.end()), dt, m.group(0).strip(), None))
    for m in _KA_DAYS_OF_MONTH.finditer(text):
        a = month_day(int(m.group("y")), m.group("m"), int(m.group("a")))
        b = month_day(int(m.group("y")), m.group("m"), int(m.group("b")))
        if a is not None and b is not None:
            # "2026 წლის 1-დან": the first day keeps its "from"; "2026 წლის 1-31 მაისი": the
            # dash stands between the two days, as it does between two dates
            stop = m.end("sep") if m.group("sep").endswith("დან") else m.end("a")
            last = (m.start("b"), m.end())
            out.append(((m.start(), stop), a, text[m.start() : stop].strip(), last))
            out.append((last, b, text[last[0] : last[1]], None))
    for m in _KA_RANGE_YEAR_AFTER.finditer(text):
        a = month_day(int(m.group(5)), m.group(2), int(m.group(1)))
        b = month_day(int(m.group(5)), m.group(4), int(m.group(3)))
        if a is not None and b is not None:
            out.append(((m.start(1), m.end(2)), a, text[m.start(1) : m.end(2)], None))
            out.append(((m.start(3), m.end(4)), b, text[m.start(3) : m.end(4)], None))
    return out


#: A day and a dash right before a date that opens with its own day: "1-15 ივნისი, 2026 წ.",
#: "01-15.06.2026", "1 - 15 June 2026". Nothing that makes the number something else stands
#: before it: a digit, a stop, a slash, a dash or a letter ("1.5-15", "N12-15").
_DAY_RANGE_FIRST = re.compile(r"(?<![\w./\-\u2013])(\d{1,2})\s?[-\u2013]\s?$")
#: A day and a month and a dash right before a date that opens with its own day, the year
#: written once for both: "01.06-30.06.2026", "1 ივნისი - 30 ივნისი, 2026 წ.". The month in
#: figures has two digits, as the date after it writes its own ("1.2-3.04.2026" reads as a
#: clause or a version number), and nothing stands before the day that makes it part of another
#: number: a digit (also one word before), a stop, a slash, a dash or a letter.
_DAY_MONTH_RANGE_FIRST = re.compile(
    r"(?<![\w./\-\u2013])(?<!\d\s)(?P<d>\d{1,2})(?:(?P<sep>[./])(?P<mo>\d{2})|\s+(?P<m>[ა-ჰ]{3,14}))"
    r"\s*[-\u2013]\s*$"
)
#: A numeric date whose month has two digits: "30.06.2026", "30/06/2026 წ.".
_TWO_DIGIT_MONTH = re.compile(r"\d{1,2}(?P<sep>[./])\d{2}(?P=sep)\d{4}")
#: A number marker before the day makes it a number: "N 3-15 ივნისი".
_NUMBER_BEFORE = re.compile(r"(?:№|#|(?<![A-Za-zა-ჰ])N)\s*$")
_LEADING_DAY = re.compile(r"\d{1,2}(?!\d)")
_REACH_BEFORE = 24


def _range_firsts(
    text: str, found: dict[tuple[int, int], tuple[date, str, str | None]]
) -> list[_RangeDate]:
    """The first day of a range written before a date that states the month and the year for
    both ends, in that year: two days before one month and year - "1-15 ივნისი, 2026 წ.",
    "01-15.06.2026" - in that date's month too, and a day and a month before it -
    "01.06-30.06.2026", "1 ივნისი - 30 ივნისი, 2026 წ." - in their own month. Unread, the
    period was its last day alone. The first day's span ends before the dash, which stands
    between the two dates as it does in "01.06.2026 - 30.06.2026": every reader of a range reads
    both forms as one."""
    out: list[_RangeDate] = []
    for (start, end), (last, raw, _note) in list(found.items()):
        lead = _LEADING_DAY.match(raw)
        if lead is None or int(lead.group(0)) != last.day:
            continue  # the date does not open with its own day (ISO, "June 15, 2026", „15“)
        window = max(0, start - _REACH_BEFORE)
        m = _DAY_RANGE_FIRST.search(text, max(0, start - 8), start)
        if m is not None:
            if _NUMBER_BEFORE.search(text, max(0, m.start() - 4), m.start(1)):
                continue
            first_day = int(m.group(1))
            if 1 <= first_day < last.day:
                span = (m.start(1), m.end(1))
                out.append((span, last.replace(day=first_day), m.group(1), (start, end)))
            continue
        m = _DAY_MONTH_RANGE_FIRST.search(text, window, start)
        if m is None or _NUMBER_BEFORE.search(text, max(0, m.start() - 4), m.start("d")):
            continue
        if m.group("mo") is not None:
            numeric = _TWO_DIGIT_MONTH.match(raw)
            if numeric is None or numeric.group("sep") != m.group("sep"):
                continue
            month: int | None = int(m.group("mo"))
        else:
            if not _GEORGIAN_LETTER.search(raw):
                continue  # a Georgian day and month before an English or numeric date
            month = _month_ka(m.group("m"))
        first = _safe_date(last.year, month, int(m.group("d"))) if month else None
        if first is None or first >= last:
            continue
        span = (m.start("d"), m.end("mo") if m.group("mo") is not None else m.end("m"))
        out.append((span, first, text[span[0] : span[1]], (start, end)))
    return out


def _safe_date(y: int, mo: int, d: int) -> date | None:
    try:
        dt = date(y, mo, d)
    except ValueError:
        return None
    return dt if 1990 <= y <= 2100 else None


def _month_ka(s: str) -> int | None:
    """Resolve a Georgian month name, tolerating the typos these documents actually contain.

    Exact prefix first (the common case, and free). Otherwise the word is compared against
    each stem at three prefix lengths, because a typo may add or drop a letter:
    "აგისტოდან" (dropped ვ), "აგვუისტას" (inserted უ), "აგივსტოდან" (transposed ვი).
    A word that is not within the distance budget of any stem is not a month at all, which
    is what stops the widened regex from matching arbitrary Georgian words.
    """
    s = s.lower()
    exact = next((v for k, v in _KA_MONTHS.items() if s.startswith(k)), None)
    if exact is not None:
        return exact
    best: tuple[int, int] | None = None
    for stem, value in _KA_MONTHS.items():
        budget = max(1, len(stem) // 3)
        for n in (len(stem) - 1, len(stem), len(stem) + 1):
            if n < 3 or n > len(s):
                continue
            d = edit_distance(s[:n], stem, cap=budget)
            if d <= budget and (best is None or d < best[0]):
                best = (d, value)
    return best[1] if best else None


def month_ka_is_misspelled(s: str) -> bool:
    """True when the word is a month misspelled: not one of the month's correct forms, but
    within two edits of one (``georgian.month_typo``). A prefix test missed every typo after the
    stem - "სექტემბრდან" starts with "სექტემბ" - and those are typos the client plants."""
    return month_typo(s) is not None


def month_ka_correction(word: str, *, following: str = "") -> str | None:
    """How a misspelled Georgian month should be written: its correct form nearest to ``word``,
    in the case the act meant ("აგვუისტას" becomes "აგვისტოს", "სექტემბრდან" "სექტემბრიდან");
    ``following`` is the word after it, which may ask for a case ("ჩათვლით": the genitive).

    None when the word is no month, when it is already correct, and when two months are equally
    close ("ივმისი"): a wrong correction is worse than none.
    """
    found = month_typo(word, following=following)
    return found.correction if found else None


def _month_en(s: str) -> int | None:
    return _EN_MONTHS.get(s.lower()[:3])


def _opens_a_date(text: str, at: int) -> bool:
    """Whether a Georgian date written year first starts at ``at``: "2027 წლის 1 იანვარი"."""
    m = _DATE_KA_YMD.match(text, at)
    return m is not None and _month_ka(m.group("m")) is not None


DateRole = Literal["from", "until"]


def date_role(raw: str) -> DateRole | None:
    """Whether a date as written is a range's first day ("from") or its last ("until"), by the
    postposition on its own words: "1 მარტიდან", "30 ნოემბრამდე", "30 ნოემბრის ჩათვლით". A year
    written after it belongs to the date and says nothing of its role: "30 ნოემბრამდე, 2026 წ."
    is an end, as "2026 წლის 30 ნოემბრამდე" is. None when the date's words carry no mark (it
    may stand after them: "01.03.2026-დან")."""
    head = _TRAILING_YEAR.sub("", raw.strip())
    if _UNTIL_END.search(head):
        return "until"
    if head.endswith("დან"):
        return "from"
    return None


def find_dates(text: str) -> list[RuleHit]:
    found: dict[tuple[int, int], tuple[date, str, str | None]] = {}

    def add(m: re.Match[str], dt: date | None, month_word: str | None = None) -> None:
        if dt is None:
            return
        note = None
        if month_word and month_ka_is_misspelled(month_word):
            note = f"month name appears misspelled: {month_word!r}"
        found[(m.start(), m.end())] = (dt, m.group(0), note)

    for pattern in (_DATE_KA_YMD, _DATE_KA_DMY):
        for m in pattern.finditer(text):
            if pattern is _DATE_KA_DMY and m.group("incl") and _opens_a_date(text, m.start("y")):
                continue  # "30 ნოემბრის ჩათვლით, 2027 წლის 1 იანვრიდან": the year is the next's
            mo = _month_ka(m.group("m"))
            dt = _safe_date(int(m.group("y")), mo, int(m.group("d"))) if mo else None
            add(m, dt, m.group("m"))
    for m in _DATE_EN_DMY.finditer(text):
        mo = _month_en(m.group(2))
        add(m, _safe_date(int(m.group(3)), mo, int(m.group(1))) if mo else None)
    for m in _DATE_EN_MDY.finditer(text):
        mo = _month_en(m.group(1))
        add(m, _safe_date(int(m.group(3)), mo, int(m.group(2))) if mo else None)
    for m in _DATE_ISO.finditer(text):
        add(m, _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in _DATE_NUMERIC.finditer(text):
        add(m, _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for m in _DATE_NUMERIC_SHORT_YEAR.finditer(text):
        add(m, _safe_date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1))))
    closes: dict[tuple[int, int], tuple[int, int]] = {}
    for reader in (_range_ends_without_year, _range_firsts):
        for span, dt, raw, last in reader(text, found):
            if span not in found:
                found[span] = (dt, raw, None)
                if last is not None:
                    closes[span] = last

    hits: list[RuleHit] = []
    last_end = -1
    for (start, end), (dt, raw, note) in sorted(
        found.items(), key=lambda kv: (kv[0][0], -kv[0][1])
    ):
        # drop hits contained in or overlapping an earlier, longer one
        # (e.g. "1 აგვისტოდან 2026" straddling "2026 წლის 1 აგვისტოდან" and the next date)
        if start < last_end:
            continue
        last_end = end
        ft = _classify(
            text,
            start,
            end,
            (
                FactType.execution_period,
                FactType.delivery_deadline,
                FactType.validity_period,
                FactType.act_date,
                FactType.contract_date,
            ),
        )
        hits.append(
            RuleHit(
                type=ft or FactType.date,
                value=dt.isoformat(),
                raw_text=raw,
                start=start,
                end=end,
                normalized=dt.isoformat(),
                confidence=0.7 if ft else 0.5,
                note=note,
            )
        )
    return _staged_deliveries(text, _until_fulfilment(text, _one_type_per_range(hits, closes)))


#: An until-phrase whose noun is the work's: "... მოქმედებს ვალდებულებების სრულ შესრულებამდე",
#: "... remain in force until the full performance of ...". Matched against the text before an
#: English keyword.
_UNTIL_THE_WORK_EN = re.compile(
    r"(?<![a-z])until\s+(?:the\s+)?(?:(?:full|complete|due|proper)\s+)?$", re.IGNORECASE
)
_WORD_TAIL = re.compile(r"[ა-ჰa-z]*")


def _until_fulfilment(text: str, hits: list[RuleHit]) -> list[RuleHit]:
    """A date the validity clause sets is no performance date because the clause says the
    contract lasts until the work is done (red team R5, EXTRACTION-R5-02). In "ხელშეკრულება
    ძალაში შედის ხელმოწერისთანავე და მოქმედებს ვალდებულებების სრულ შესრულებამდე, მაგრამ
    არაუგვიანეს 2026 წლის 31 დეკემბრამდე" the nearest keyword is "შესრულებ", inside the
    until-phrase; the date is the validity's. Only where every performance keyword of the
    sentence stands in such a phrase and the sentence is a validity clause
    (``contract_terms._VALIDITY``): "შეასრულოს სამუშაოები ... 31 ივლისამდე, ხელშეკრულების
    მოქმედების ვადაში" is still the work's."""
    if not any(h.type is FactType.execution_period for h in hits):
        return hits
    # imported here: the terms reader imports this module
    from procurement_agent.checks.contract_terms import _VALIDITY, sentence_at, sentence_spans

    spans = sentence_spans(text)
    out: list[RuleHit] = []
    for h in hits:
        span = sentence_at(spans, h.start) if h.type is FactType.execution_period else None
        if (
            span is not None
            and _VALIDITY.search(text, *span) is not None
            and _only_until_the_work(text, *span)
        ):
            h = replace(h, type=FactType.validity_period)
        out.append(h)
    return out


def _only_until_the_work(text: str, a: int, b: int) -> bool:
    """Whether every performance keyword in ``text[a:b]`` is the noun of an until-phrase: a word
    in "-მდე" ("შესრულებამდე"), or "until the full performance"."""
    sentence = text[a:b].lower()
    found = False
    for kw in KEYWORDS[FactType.execution_period]:
        for m in keyword_pattern(kw).finditer(sentence):
            tail = _WORD_TAIL.match(sentence, m.end())
            word_end = tail.end() if tail is not None else m.end()
            if sentence[m.start() : word_end].endswith("მდე"):
                found = True
            elif _UNTIL_THE_WORK_EN.search(sentence, max(0, m.start() - 40), m.start()):
                found = True
            else:
                return False
    return found


#: An item of a staged delivery naming its batch or stage: "პირველი პარტია - არაუგვიანეს ...",
#: "II ეტაპი: ...", "second batch: ...".
_BATCH = re.compile(
    r"(?<![ა-ჰ])(?:პარტი|ეტაპ)[ა-ჰ]*|(?<![a-z])(?:batch|stage|lot|consignment)(?:es|s)?(?![a-z])",
    re.IGNORECASE,
)
#: How far before its date an item's batch word may stand.
_BATCH_REACH = 80
_SENTENCE_STOP = re.compile(r"[.!?](?=\s|$)")


def _batch_item(text: str, start: int) -> bool:
    """Whether the list item holding the date at ``start`` names a batch or a stage: the words
    since the item began (a comma, a semicolon, a colon or a line break)."""
    lo = max(text.rfind(c, 0, start) for c in ",;:\n") + 1
    return _BATCH.search(text, max(lo, start - _BATCH_REACH), start) is not None


def _staged_deliveries(text: str, hits: list[RuleHit]) -> list[RuleHit]:
    """A staged delivery's batches each have a delivery deadline (policy, red team R5:
    COMPARE-R5-11): "მიწოდება განხორციელდება ეტაპობრივად: პირველი პარტია - არაუგვიანეს 2026 წლის
    15 მაისისა, მეორე პარტია - არაუგვიანეს 2026 წლის 15 ივნისისა". The delivery word types the
    first batch's date; a later batch's, past the keyword's reach, is typed as the item before it
    when both items name a batch in one sentence. A date in an item that names none - an
    invoice's, a payment's - keeps its own type."""
    out: list[RuleHit] = []
    for h in hits:
        before = out[-1] if out else None
        if (
            h.type is FactType.date
            and before is not None
            and before.type is FactType.delivery_deadline
            and _SENTENCE_STOP.search(text, before.end, h.start) is None
            and _batch_item(text, h.start)
            and _batch_item(text, before.start)
        ):
            h = replace(h, type=FactType.delivery_deadline, confidence=before.confidence)
        out.append(h)
    return out


def _one_type_per_range(
    hits: list[RuleHit], closes: dict[tuple[int, int], tuple[int, int]]
) -> list[RuleHit]:
    """A range's first day typed as the date that closes it. Each is typed by the keyword
    nearest to it, and a first day typed apart from its last ("01" a delivery deadline,
    "30.06.2026" an execution period) was no range to the readers that join two dates of one
    type."""
    at = {(h.start, h.end): h for h in hits}
    out: list[RuleHit] = []
    for h in hits:
        last = at.get(closes.get((h.start, h.end), (-1, -1)))
        if last is not None and last.type is not h.type:
            h = replace(h, type=last.type, confidence=last.confidence)
        out.append(h)
    return out


# --- identification codes ----------------------------------------------------------------------

#: one label pattern for every reader (``normalize.ID_CODE_RE``)
_ID_CODE = ID_CODE_RE


def find_id_codes(text: str) -> list[RuleHit]:
    return [
        RuleHit(
            type=FactType.party_id_code,
            value=id_code_digits(m.group(1)),
            raw_text=m.group(0),
            start=m.start(),
            end=m.end(),
            normalized=id_code_digits(m.group(1)),
            confidence=0.85,
        )
        for m in _ID_CODE.finditer(text)
    ]


# --- percentages -------------------------------------------------------------------------------


def _bracketed_or_bare(figure: str, lookbehind: str) -> str:
    """A figure as contracts write it before its unit: bare ("10 (ათი) დღე", "0.3 პროცენტი"), or
    in brackets after its words ("ათი (10) დღე", "ნული მთელი სამი (0.3) პროცენტი"). One helper
    for every unit, so the words-first order is read wherever the figure-first one is. Group 1
    is the bracketed figure, group 2 the bare one."""
    return rf"{lookbehind}(?:\(\s*({figure})\s*\)|({figure}))"


#: A percentage: the sign, or the word - "0.3 პროცენტის", "0.3 (ნული მთელი სამი) პროცენტი",
#: "ნული მთელი სამი (0.3) პროცენტი", "0.2 percent", "0.2 per cent"; the figure's words may
#: stand before the sign as before the word ("5 (ხუთი)%-ის", "0.1 (ნული მთელი ერთი) %-ის").
#: Not the adjective "პროცენტული" ("10 პროცენტული პუნქტი"), and not a bracket holding a
#: figure ("5 (3.1)%").
_PCT = re.compile(
    # "(18%)" is a rate in brackets: the bare figure may follow "(", and the bracketed one
    # wins where it can ("(0.2) პროცენტის")
    _bracketed_or_bare(r"\d{1,3}(?:[.,]\d{1,2})?", r"(?<![\d.,])")
    + r"\s*(?:\([^()\d]{1,60}\)\s*)?(?:%|პროცენტ(?!ულ)[ა-ჰ]*|per\s?cent(?![a-z]))",
    re.IGNORECASE,
)
_PCT_NUMBER = re.compile(r"\d{1,3}(?:[.,]\d{1,2})?")


def percent_of(hit: RuleHit) -> float | None:
    """The rate a percentage hit states. A penalty's ``normalized`` is its canonical text once
    the period is known ("0.5% per day"), and so is an advance's ("advance 30%"), so a reader
    that needs the number takes it here. The words-first form opens with its bracket."""
    if isinstance(hit.normalized, float):
        return hit.normalized
    m = _PCT_NUMBER.search(hit.raw_text)
    return parse_number(m.group(0)) if m else None


#: A limit on a penalty's total, not a rate: "მაგრამ არაუმეტეს ხელშეკრულების ღირებულების 10%-ისა",
#: "არ უნდა აღემატებოდეს ღირებულების 10%-ს", "not exceeding 10% of the Contract value", "up to a
#: maximum of 10%". The cap word stands before the rate with at most four words between and no
#: figure: "არაუმეტეს 5 დღისა, პირგასამტეხლო 0.1%" caps the days, and 0.1% is the rate.
_CAP_BEFORE = re.compile(
    r"(?:(?<![ა-ჰ])(?:არა\s*უმეტეს|მაქსიმუმ|მაქსიმალურ[ა-ჰ]*|ზღვრულ[ა-ჰ]*)"
    r"|(?<![ა-ჰ])არ\s+(?:უნდა\s+)?(?:აღემატებოდეს|აღემატება|გადააჭარბოს|აჭარბებდეს)"
    r"|(?<![a-z])(?:not\s+(?:exceeding|to\s+exceed|more\s+than)|no\s+more\s+than|at\s+most"
    r"|up\s+to|capped\s+at|maximum(?:\s+of)?|limited\s+to|(?:shall|must|may|will)\s+not\s+exceed))"
    r"(?:\s+[^\s\d%.;]+){0,4}\s*$",
    re.IGNORECASE,
)
#: ... or after it: "10%-ს არ უნდა აღემატებოდეს", "10%-ზე მეტი არ უნდა იყოს", "ჯამში 10%-მდე",
#: "10% at most", "10% in aggregate".
_CAP_AFTER = re.compile(
    r"-?მდე(?![ა-ჰ])"
    r"|(?:-?[ა-ჰ]+)?\s+(?:მეტი\s+)?არ\s+(?:უნდა\s+)?(?:აღემატებოდეს|აღემატება|იყოს|გადააჭარბოს)"
    r"|\s+(?:at\s+most|maximum|in\s+(?:the\s+)?aggregate)(?![a-z])",
    re.IGNORECASE,
)
_CAP_REACH = 80


def is_rate_cap(text: str, start: int, end: int) -> bool:
    """Whether the percentage at ``text[start:end]`` caps a penalty's total rather than setting
    its rate. A cap is no penalty, and the period of the rate beside it is not its period: read
    as one, "0.1% per day, not exceeding 10%" stated a second penalty of "10% per day"."""
    if _OWN_PERIOD.match(text, end):
        return False  # "up to 0.5% per day", "არაუმეტეს 0.1%-ისა ყოველ დღეზე": a rate all the same
    before = text[max(0, start - _CAP_REACH) : start]
    cut = list(_SENTENCE_END.finditer(before))
    before = before[cut[-1].end() :] if cut else before
    return _CAP_BEFORE.search(before) is not None or _CAP_AFTER.match(text, end) is not None


#: The words for an advance: "ავანსი", "წინასწარი გადახდა" (not "წინასწარი შეტყობინება", a prior
#: notice), "advance", "prepayment" - not "in advance", which says when, not what.
_ADVANCE_WORD = re.compile(
    r"ავანს|წინასწარი?\s+(?:გადახდ|გადაიხდ|ანგარიშსწორებ|ჩარიცხ|თანხ)"
    r"|(?<![a-z])(?<!in )(?:advance|prepayment|pre-payment|prepaid)(?![a-z])",
    re.IGNORECASE,
)
#: The share paid after the advance: "დარჩენილი 70%", "the remaining 70%", "the balance".
_REMAINDER_WORD = re.compile(
    r"დარჩენ|ნაშთ|მეორე\s+ნაწილ|(?<![a-z])(?:remaining|remainder|balance)(?![a-z])",
    re.IGNORECASE,
)
#: An advance named right after its share: "30% ავანსი", "30%-იანი ავანსი", "30% advance".
_ADVANCE_AFTER = re.compile(
    r"(?:-?[ა-ჰ]+)?\s*(?:ავანს|(?:an?\s+)?(?:advance|prepayment)(?![a-z]))", re.IGNORECASE
)
#: ... or after the verb that pays it: "30% გადაიხდება ავანსის სახით", "30% ჩაირიცხება
#: ავანსად", "30% shall be paid as an advance". Only as the advance ("სახით", "ად", "as"):
#: "75% გადაიხდება ავანსის გამოქვითვით" is the balance, paid less the advance.
_ADVANCE_AFTER_VERB = re.compile(
    r"(?:-?[ა-ჰ]+)?\s+(?P<verb>[ა-ჰ]+)\s+(?:ავანსის\s+სახით|ავანსად)(?![ა-ჰ])"
    r"|(?:\s+of\s+the\s+[a-z]+\s+(?:value|price|amount))?\s+(?:shall|will|is|are)\s+(?:be\s+)?"
    r"(?P<en>paid|payable|transferred)\s+as\s+(?:an?\s+)?(?:advance|prepayment)(?![a-z])",
    re.IGNORECASE,
)
#: A rate that ends right where a word begins: that word is the rate's ("30% advance, 70% ...").
_RATE_ENDS = re.compile(r"(?:%|პროცენტ[ა-ჰ]*|per\s?cent)(?:-?[ა-ჰ]+)?\s*$", re.IGNORECASE)


def is_advance_share(text: str, start: int, end: int) -> bool:
    """Whether the percentage at ``text[start:end]`` is the advance's share of the price.

    Named right after it ("30% ავანსი"), or before it in its sentence ("ავანსის სახით
    ღირებულების 30%", "an advance payment of 30%") with no remainder word and no other rate
    between: in "ავანსი 30%, დარჩენილი 70%" and "30% advance, 70% on delivery" the 70% is the
    balance. The nearest keyword cannot tell: "payment" is nearer than "advance" in "advance
    payment of 30%"."""
    if _ADVANCE_AFTER.match(text, end):
        return True
    after_verb = _ADVANCE_AFTER_VERB.match(text, end)
    if after_verb is not None and (
        after_verb.group("en") or PAYMENT_WORDS.search(after_verb.group("verb"))
    ):
        return True
    before = text[max(0, start - _CTX) : start]
    cut = list(_SENTENCE_BOUNDARY.finditer(before))
    before = before[cut[-1].end() :] if cut else before
    word = next(reversed(list(_ADVANCE_WORD.finditer(before))), None)
    if word is None:
        return False
    between = before[word.end() :]
    if _REMAINDER_WORD.search(between) or _PCT.search(between):
        return False
    return _RATE_ENDS.search(before, 0, word.start()) is None


#: A word about paying the fee, built from the verb roots rather than from finished word forms,
#: so every preverb, version vowel and person reads: pay - "გადაიხდის", "გადაიხდება",
#: "გადაუხდის", "გადაუხადოს", "გადახდა", "იხდის", "უხდის"; transfer the money - "გადარიცხავს",
#: "გადაურიცხავს", "ჩაირიცხება", "ჩარიცხვა"; reimburse - "ანაზღაურება", "აანაზღაურებს",
#: "აუნაზღაურებს"; settle - "ანგარიშსწორება"; and English "pay", "pays", "paid", "paying",
#: "payable", "payment(s)". Not "გადამხდელი" (a payer, as in "დღგ-ს გადამხდელი") and not English
#: "transfer", which moves goods and rights as often as money.
PAYMENT_WORDS = re.compile(
    r"გადა[იუ]?ხა?დ|(?<![ა-ჰ])[იუ]ხდ"
    r"|(?:გადა|ჩა)[იუ]?რიცხ"
    r"|(?<![ა-ჰ])(?:ა|აა|აუ)ნაზღაურ"
    r"|ანგარიშსწორებ"
    r"|(?<![a-z])(?:pay|pays|paid|paying|payable|payments?)(?![a-z])",
    re.IGNORECASE,
)
#: A clause that goes on with the balance after a paying clause: "...; the balance within 10
#: working days", "...; დარჩენილი თანხა - 10 სამუშაო დღეში". It pays with the verb before it.
_BALANCE_OPENS = re.compile(
    r"(?:ხოლო\s+|and\s+)?(?:the\s+)?(?:დარჩენ|ნაშთ|მეორე\s+ნაწილ"
    r"|(?:remaining|remainder|balance)(?![a-z]))",
    re.IGNORECASE,
)


#: The marker of a list item: "ბ)", "2)", "4.2.2.", "- ".
_LIST_MARKER = re.compile(r"(?:[ა-ჰa-z]\)|\d{1,2}\)|\d{1,2}(?:\.\d{1,2}){0,3}\.|[-\u2013•])\s*")
#: An advance opening a stage of a staged payment: "ავანსი - ...", "Advance: ...".
_ADVANCE_OPENS = re.compile(
    r"ავანს|წინასწარი?\s+(?:გადახდ|ანგარიშსწორებ)|(?:an?\s+)?(?:advance|prepayment)(?![a-z])",
    re.IGNORECASE,
)


def continues_payment(text: str, previous_end: int, start: int) -> bool:
    """Whether the sentence at ``start`` goes on with the payment of the paying sentence that
    ends at ``previous_end``: after a semicolon, when it opens with the balance ("; the balance
    within 10 working days", "; ბ) დარჩენილი თანხა - ...", "; 4.2.2. დარჩენილი 70% - ...");
    after a colon that leads in a staged payment, when it opens with the advance or the
    balance ("ეტაპობრივად: 4.2.1. ავანსი - ..."). A sibling clause ("; 4.3. შემსრულებელი
    ...", ": 4.1. მიწოდება ...") goes on with nothing."""
    stop = text[previous_end - 1 : previous_end]
    if stop not in (";", ":"):
        return False
    marker = _LIST_MARKER.match(text, start)
    at = marker.end() if marker else start
    if _BALANCE_OPENS.match(text, at):
        return True
    return stop == ":" and _ADVANCE_OPENS.match(text, at) is not None


#: Which payment a deadline is for: the advance's, or the balance's after it.
_PAYMENT_PARTS: dict[str, tuple[str, ...]] = {
    "advance": ("ავანს", "advance", "prepayment", "prepaid"),
    "remainder": ("დარჩენ", "ნაშთ", "remaining", "remainder", "balance"),
}


def is_advance_deadline(text: str, start: int, end: int) -> bool:
    """Whether the deadline at ``text[start:end]`` is the advance's ("ავანსი ... 5 დღეში"), not
    the balance's ("... ხოლო დარჩენილი 70% ... 10 დღის განმავლობაში"): the nearer word of its
    clause says."""
    marked = _CLAUSE_TURN.sub(";", text)  # ", ხოლო დარჩენილი ..." is the balance's clause
    return nearest_keyword_group(marked, start, end, _PAYMENT_PARTS) == "advance"


def payment_terms_value(deadline: str | None = None, advance: float | None = None) -> str | None:
    """A payment term in the canonical form ``docs/GOLD_SPEC.md`` fixes: the deadline ("P10D
    working"), then the advance's share when one is stated ("P10D working advance 30%"), or
    the share alone ("advance 30%"). None when neither is stated."""
    parts = [p for p in (deadline, f"advance {advance:g}%" if advance is not None else None) if p]
    return " ".join(parts) or None


#: What a payment term is (``docs/GOLD_SPEC.md``): a duration, the days it counts, and the
#: advance's share - each part optional, never a bare number.
PAYMENT_TERMS_FORM = re.compile(
    r"P\d+[DWMY](?: working| calendar)?(?: advance \d+(?:\.\d+)?%)?|advance \d+(?:\.\d+)?%"
)


def is_payment_terms_value(value: object) -> bool:
    """Whether ``value`` is a payment term in its canonical form."""
    return (
        isinstance(value, str) and bool(value) and PAYMENT_TERMS_FORM.fullmatch(value) is not None
    )


#: The note on a percentage that is the VAT's rate ("+ 18% დღგ", "დღგ-ს (18%) ჩათვლით"): a
#: percentage, not a vat_status, and part of the tax wording beside it - restated where the other
#: version names the tax without it, it changes nothing (``checks.compare``).
VAT_RATE_NOTE = "the VAT rate"
#: The tax a rate is written right beside, with nothing but a bracket, a dash, a case ending or
#: "at"/"rate of" between: "18% დღგ", "18%-იანი დღგ", "დღგ-ს (18%)", "დღგ 18%", "VAT at 18%",
#: "VAT (18%)". Not a rate further off: "დღგ-ს ჩათვლით, ფასდაკლება 5%" is a discount.
_VAT_WORD = r"(?:დღგ|დამატებული\s+ღირებულების\s+გადასახად|vat|value\s+added\s+tax)"
_VAT_BEFORE_RATE = re.compile(
    rf"(?<![ა-ჰa-z]){_VAT_WORD}(?:-?[ა-ჰ]{{1,4}})?\s*(?:(?:at|rate(?:\s+of)?|განაკვეთი)\s*)?"
    r"[(:\u2013\u2014-]?\s*$",
    re.IGNORECASE,
)
_VAT_AFTER_RATE = re.compile(rf"\s*\)?(?:-?[ა-ჰ]{{1,4}})?\s*{_VAT_WORD}(?![a-z])", re.IGNORECASE)


def is_vat_rate(text: str, start: int, end: int) -> bool:
    """Whether the percentage at ``text[start:end]`` is the rate of the VAT written beside it."""
    return (
        _VAT_BEFORE_RATE.search(text, max(0, start - 40), start) is not None
        or _VAT_AFTER_RATE.match(text, end) is not None
    )


#: "net" that measures something other than money: a payment convention ("net 30 days"), a
#: weight, a mass, an area, a profit ("net weight", "ნეტო წონის"). It says nothing about tax.
_NET_MEASURE = re.compile(
    r"\s*(?:-?[ა-ჰ]{1,3}\s+)?(?:(?P<figure>\d)|(?:weight|mass|area|volume|tonnage|profit|income|"
    r"margin|loss)(?![a-z])|წონ|მას[აი]|ფართ|მოცულობ|მოგებ|ზარალ)",
    re.IGNORECASE,
)


def is_net_measure(text: str, end: int) -> bool:
    """Whether the word "net" ending at ``end`` measures a count, a weight or a profit rather
    than stating a net amount: "net 30 days", "net weight", "ნეტო წონის მიხედვით". A figure
    after it is a count only when it is no money: "NET 900 ლარი" is the net amount itself."""
    m = _NET_MEASURE.match(text, end)
    if m is None:
        return False
    return m.group("figure") is None or _AMOUNT_AFTER.match(text, m.start("figure")) is None


#: A condition the tax wording after it stands under: "თუ „შემსრულებელი“ არ არის დღგ-ს
#: გადამხდელი, ...", "იმ შემთხვევაში, თუკი ...", "If the Contractor is not registered as a VAT
#: payer, ...". English only where it opens its clause: "the price, where applicable, ..." has a
#: comma before the wording.
_CONDITION = re.compile(
    r"(?<![ა-ჰ])(?:თუ|თუკი)(?![ა-ჰ])"
    r"|(?:^\s*|[,;:(]\s*|\d\.\s+)(?:if|in\s+case|in\s+the\s+event|where|unless|provided\s+that)"
    r"(?![a-z])",
    re.IGNORECASE,
)
_CONDITION_REACH = 120


def is_hypothetical(text: str, start: int) -> bool:
    """Whether the words at ``start`` stand in a conditional clause: a condition opens their
    clause with no comma, semicolon or sentence end between. Such wording states what applies
    in a case that may not arise - "თუ შემსრულებელი არ არის დღგ-ს გადამხდელი, ღირებულება არ
    იცვლება" says nothing about whether this price includes VAT."""
    lo = max(0, start - _CONDITION_REACH)
    before = text[lo:start]
    stops = [m.end() for m in _SENTENCE_END.finditer(before)]
    clause = before[max(stops, default=0) :]
    last = next(reversed(list(_CONDITION.finditer(clause))), None)
    return last is not None and not re.search(r"[,;]", clause[last.end() :])


def find_percentages(text: str) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for m in _PCT.finditer(text):
        v = parse_number(m.group(1) or m.group(2))
        if v is None or v > 100:
            continue
        ft = _classify(
            text,
            m.start(),
            m.end(),
            (FactType.penalty, FactType.vat_status, FactType.payment_terms),
        )
        shown = f"{v:g}%"
        normalized: str | float = v
        note = None
        if ft == FactType.vat_status:
            # the rate beside "დღგ" ("+ 18% დღგ") says nothing about whether the price includes
            # the tax: a vat_status is one of four words, never a number (docs/GOLD_SPEC.md)
            ft, note = None, VAT_RATE_NOTE if is_vat_rate(text, m.start(), m.end()) else None
        elif ft == FactType.payment_terms:
            # a share of the price is a payment term only as the advance's ("advance 30%"); the
            # balance's share, or a share paid on some event, is a percentage
            if is_advance_share(text, m.start(), m.end()):
                shown = normalized = payment_terms_value(advance=v) or shown
            else:
                ft = None
        elif ft == FactType.penalty and is_rate_cap(text, m.start(), m.end()):
            ft = None  # "not exceeding 10%" caps the penalty: no rate of its own
        # a penalty counted per period is stated with it, canonically (docs/GOLD_SPEC.md):
        # "0.5% per day" and "0.5% per week" are two penalties, not one
        period = penalty_period(text, m.start(), m.end()) if ft == FactType.penalty else None
        if period is not None:
            shown = normalized = f"{v:g}% per {period}"
        hits.append(
            RuleHit(
                type=ft or FactType.percentage,
                value=shown,
                raw_text=m.group(0),
                start=m.start(),
                end=m.end(),
                normalized=normalized,
                unit="%",
                confidence=0.65 if ft else 0.45,
                note=note,
            )
        )
    return hits


#: The period a penalty counts per, written after its rate or before it: "ყოველ ვადაგადაცილებულ
#: დღეზე", "ყოველი თვის დაგვიანებისთვის", "დღიური", "per calendar day", "for each week of
#: delay", "For every month of delay ... 1%". Never "a month": "within a month, a penalty of 5%"
#: is no monthly penalty.
_PERIOD_KA = r"(დღ(?!გ)|კვირ|თვ|წელ|წლ)"
_PER_PERIOD = re.compile(
    rf"ყოველ(?:ი|ს)?\s+(?:[ა-ჰ]+\s+){{0,2}}{_PERIOD_KA}"
    r"|(?:ყოველ)?(დღ|თვ|კვირ|წლ)(?:იურ|ეულ)[ა-ჰ]*"
    r"|(?<![a-z])(?:per|each|every|for\s+each|for\s+every)\s+"
    r"(?:calendar\s+|working\s+|business\s+|banking\s+)?(day|week|month|year)(?![a-z])",
    re.IGNORECASE,
)
_PERIOD_UNITS = {
    "დღ": "day", "კვირ": "week", "თვ": "month", "წელ": "year", "წლ": "year",
    "day": "day", "week": "week", "month": "month", "year": "year",
}  # fmt: skip
#: A rate's own period, right after it and within its clause: "0.5% per day", "0.1%-ისა ყოველ
#: ვადაგადაცილებულ დღეზე", "0.2% of the price for each day". A cap counts per no period.
_OWN_PERIOD = re.compile(r"[^,;.\n]{0,40}?(?:" + _PER_PERIOD.pattern + ")", re.IGNORECASE)
#: A sentence's end: a stop followed by a space. "0.5%" and "5.1." are not ends inside a clause.
_SENTENCE_END = re.compile(r"[.;!?](?=\s)")
_PERIOD_AFTER = 60
_PERIOD_BEFORE = 120


def penalty_period(text: str, start: int, end: int) -> str | None:
    """The period ("day", "week", "month", "year") a rate at ``text[start:end]`` counts per,
    or None. Read after the rate first, then before it, never across the sentence's end nor
    across another rate: in "0.1% ყოველ დღეზე, მაგრამ არაუმეტეს 10%-ისა" the day is 0.1%'s.
    Nor across a turn to another clause (", ხოლო", ", whereas"): in "0.2% ყოველ დღეზე, ხოლო ...
    ჯარიმა 10%" the day stands after the 0.2% and is its own, so the fine counts per nothing."""
    after = text[end : end + _PERIOD_AFTER]
    stop = _SENTENCE_END.search(after)
    after = after[: stop.start()] if stop else after
    turn = _CLAUSE_TURN.search(after)
    after = after[: turn.start()] if turn else after
    other = _PCT.search(after)
    after = after[: other.start()] if other else after
    before = text[max(0, start - _PERIOD_BEFORE) : start]
    cut = [*_SENTENCE_END.finditer(before), *_PCT.finditer(before), *_CLAUSE_TURN.finditer(before)]
    before = before[max(c.end() for c in cut) :] if cut else before
    found = _PER_PERIOD.search(after)
    if found is None:
        found = next(reversed(list(_PER_PERIOD.finditer(before))), None)
    if found is None:
        return None
    unit = next(g for g in found.groups() if g)
    return _PERIOD_UNITS[unit.lower()]


#: The words for a penalty; the English ones whole, so "fine" is no part of "defined".
_PENALTY_WORD = re.compile(
    r"პირგასამტეხლო|ჯარიმ|საურავ"
    r"|(?<![a-z])(?:penalt[a-z]*|liquidated\s+damages|fines?)(?![a-z])",
    re.IGNORECASE,
)
#: A rate another role owns, named nearer to it than the penalty: a guarantee's, a retention's
#: or an advance's share ("საგარანტიო თანხის სახით დაკავდება ღირებულების 5%"). Named before the
#: penalty word it owns nothing: "ავანსის დაბრუნების დაგვიანებისთვის პირგასამტეხლო 0.1%".
_RATE_OWNER = re.compile(
    r"საგარანტიო|გარანტი|უზრუნველყოფ(?:ა|ის|ად)(?![ა-ჰ])|დაკავ|ავანს"
    r"|(?<![a-z])(?:guarantee|retention|retained|security|deposit|advance|prepayment)(?![a-z])",
    re.IGNORECASE,
)
#: A sentence's end, but not the stop of a clause or heading number ("5. პირგასამტეხლო 5.1.
#: ..."): a heading names the penalty its clauses set.
_SENTENCE_END_IN_TEXT = re.compile(r"(?<!\d)[.;!?](?=\s)")
_PENALTY_BEFORE = 120
_PENALTY_AFTER = 40


def is_penalty_rate(text: str, start: int, end: int) -> bool:
    """Whether the percentage at ``text[start:end]`` is a penalty's rate: a penalty word stands
    in its own sentence, before it (or after it, when none does), with no other role's word
    nearer to it (``_RATE_OWNER``). Never the VAT's rate ("ფასი მოიცავს 18% დღგ-ს"), a cap on
    the penalty, or a rate in the next clause: "პირგასამტეხლო 0.2% ყოველ დღეზე. 5.2. ...
    გარანტიას ღირებულების 5%-ის ოდენობით" sets one penalty."""
    if is_rate_cap(text, start, end) or is_vat_rate(text, start, end):
        return False
    before = text[max(0, start - _PENALTY_BEFORE) : start]
    stops = [m.end() for m in _SENTENCE_END_IN_TEXT.finditer(before)]
    before = before[max(stops, default=0) :]
    after = text[end : end + _PENALTY_AFTER]
    stop = _SENTENCE_END_IN_TEXT.search(after)
    after = after[: stop.start()] if stop else after
    word = next(reversed(list(_PENALTY_WORD.finditer(before))), None)
    owner = next(reversed(list(_RATE_OWNER.finditer(before))), None)
    if word is None:
        return owner is None and _PENALTY_WORD.search(after) is not None
    return owner is None or owner.start() < word.start()


# --- durations ---------------------------------------------------------------------------------

# The count: its figure, or its figure in brackets after the words - "10 (ათი) დღე" and "ათი
# (10) დღე" are both how contracts write it, as are "24 (twenty-four) months" and "ten (10)
# days". Only a closed bracket: "1) დღის ..." is a list item, not one day.
_COUNT = _bracketed_or_bare(r"\d{1,4}", r"(?<![\d(])")
#: Which days a count counts, in every case the adjective takes ("კალენდარული", "კალენდარულ
#: დღეში") and abbreviated with its stop ("სამ. დღის", "კალ. დღის"). Without the stop "სამ" is
#: the numeral three ("სამ დღეში").
_QUALIFIER_KA = r"კალენდარულ[ა-ჰ]*|სამუშაო|საბანკო|სამ\.|კალ\."
#: The unit of time; "დღ(?!გ)": დღგ is VAT, and "18 დღგ-ს ჩათვლით" is not eighteen days.
_UNIT_STEM_KA = r"(დღ(?!გ)|კვირ|თვ|წელ|წლ)[ა-ჰ]*"
_DUR_KA = re.compile(
    # the count, then its unit as a word or as the adjective hyphenated to it: "20 დღე",
    # "20-დღიან ვადაში", "12-თვიანი"; never an ordinal ("20-ე დღეს", "მე-20 დღეს")
    rf"(?<!მე-){_COUNT}(?:-(?=დღ(?!გ)|კვირ|თვ|წლ)"
    rf"|\s*(?:\(\s*[ა-ჰ ]+\s*\)\s*)?(?:({_QUALIFIER_KA})\s*)?){_UNIT_STEM_KA}",
    re.IGNORECASE,
)
#: A unit of time right after a word, with the days it counts: where a count written only in
#: words ends ("თხუთმეტი სამუშაო დღის", "ათი სამ. დღის").
_UNIT_AFTER_WORDS = re.compile(
    rf"(?<=[ა-ჰ])\s+(?:({_QUALIFIER_KA})\s*)?{_UNIT_STEM_KA}", re.IGNORECASE
)
#: The words right before it, the count's among them: up to eight.
_WORDS_BEFORE_UNIT = re.compile(r"(?<![ა-ჰ])((?:[ა-ჰ]+\s+){0,7}[ა-ჰ]+)$")
_WORDS_REACH = 120
_DUR_EN = re.compile(
    rf"{_COUNT}\s*(?:\([a-z\- ]+\)\s*)?(calendar|working|business|banking)?\s*"
    r"(day|week|month|year)s?\b",
    re.IGNORECASE,
)
_UNIT_KA = {"დღ": "day", "კვირ": "week", "თვ": "month", "წელ": "year", "წლ": "year"}


def _qualifier_ka(word: str) -> str:
    """Which days a Georgian qualifier counts, from its stem: "კალენდარულ(ი)" and "კალ." are
    calendar days, "სამუშაო" and "სამ." working days, "საბანკო" banking days."""
    if word.startswith("კალ"):
        return "calendar"
    return "banking" if word == "საბანკო" else "working"


#: The event a count starts from, which is not what the count measures: "მიწოდებიდან 10 დღის
#: ვადაში" (within 10 days *from delivery*) is an installation's or a payment's deadline, not the
#: delivery's; "24 months after delivery" is a warranty. A Georgian ablative ("-დან") or the word
#: after an English "from"/"after".
_COUNT_STARTS_FROM = re.compile(
    r"(?<![ა-ჰ])[ა-ჰ]+დან(?![ა-ჰ])"
    r"|(?<![a-z])(?:from|after|following|upon)\s+(?:the\s+)?(?:date\s+of\s+(?:the\s+)?)?"
    r"(?P<event>[a-z]+)",
    re.IGNORECASE,
)
#: Where a second clause of a sentence turns to another subject: ", ხოლო მონტაჟი ..." (whereas
#: the installation ...), ", whereas". A keyword of the first clause names nothing in the second.
_CLAUSE_TURN = re.compile(r"[,\u2013\u2014-](?=\s*(?:ხოლო|whereas)(?![ა-ჰa-z]))", re.IGNORECASE)


#: The warranty as a frame another count is set in, not the count's own subject: "საგარანტიო
#: ვადის განმავლობაში გამოვლენილ ნაკლს აღმოფხვრის 10 დღის ვადაში" (a remedy within 10 days),
#: "საგარანტიო ვადის დასრულებიდან 15 დღეში", "during / after the warranty period"; and the
#: guarantee money, which is a security and no warranty ("საგარანტიო თანხა", "საგარანტიო
#: უზრუნველყოფა", "საგარანტიო წერილი", "საბანკო გარანტია"). "საგარანტიო ვადა შეადგენს 12 თვეს",
#: "საგარანტიო ვადა - 12 თვე" and "12-თვიანი გარანტია" name the warranty itself.
_WARRANTY_FRAME = re.compile(
    r"საგარანტიო\s+(?:ვადის|პერიოდის)\s+(?:განმავლობაში|მანძილზე|დასრულებ[ა-ჰ]*|გასვლ[ა-ჰ]*"
    r"|ამოწურვ[ა-ჰ]*|ბოლომდე|შემდეგ|ფარგლებში)"
    r"|საგარანტიო\s+(?:ვადაში|პერიოდში)"
    r"|საგარანტიო\s+(?:თანხ|უზრუნველყოფ|წერილ|დეპოზიტ)[ა-ჰ]*|საბანკო\s+გარანტი[ა-ჰ]*"
    r"|(?<![a-z])(?:during|within|after|throughout|upon\s+expiry\s+of|until\s+the\s+end\s+of"
    r"|following)\s+the\s+(?:warranty|guarantee)\s+period(?![a-z])"
    r"|(?<![a-z])(?:bank|performance|bid)\s+guarantee(?![a-z])",
    re.IGNORECASE,
)


def _duration_context(text: str) -> str:
    """``text`` as a duration's type is read from it: the events counts start from blanked out,
    the warranty named only as a frame or as guarantee money blanked out too, and a turn to
    another clause made a sentence boundary. Every offset stays where it was."""
    chars = list(text)
    for m in _WARRANTY_FRAME.finditer(text):
        chars[m.start() : m.end()] = " " * (m.end() - m.start())
    for m in _COUNT_STARTS_FROM.finditer(text):
        a, b = m.span("event") if m.group("event") else m.span()
        chars[a:b] = " " * (b - a)
    for m in _CLAUSE_TURN.finditer(text):
        chars[m.start()] = ";"
    return "".join(chars)


def _in_brackets(text: str, pos: int) -> bool:
    opened = text.rfind("(", 0, pos)
    return opened >= 0 and text.rfind(")", opened, pos) < 0


def _counts_in_words(text: str, figures: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    """Every count of time written only in words, as (start, count, where its unit begins):
    "თხუთმეტი სამუშაო დღის" is 15 working days (docs/GOLD_SPEC.md section 2: a count written
    in words is the same duration as its figure). The longest run of words right before the
    unit that is a whole number; not words in brackets, which are a figure's own, nor words a
    figure stands right before, nor a count its figure already reads (``figures``), nor an
    ordinal ("მეორე დღეს"), which is no number."""
    out: list[tuple[int, int, int]] = []
    for unit in _UNIT_AFTER_WORDS.finditer(text):
        run = _WORDS_BEFORE_UNIT.search(text, max(0, unit.start() - _WORDS_REACH), unit.start())
        if run is None:
            continue
        found: tuple[int, int] | None = None
        for word in re.finditer(r"[ა-ჰ]+", run.group(1)):  # the longest run that is a number
            value = parse_number_words(run.group(1)[word.start() :])
            if value is not None and value > 0:
                found = run.start(1) + word.start(), value
                break
        if found is None:
            continue
        start, value = found
        if (
            _in_brackets(text, start)
            or text[:start].rstrip().endswith(tuple("0123456789)"))
            or any(_overlaps((start, unit.end()), f) for f in figures)
        ):
            continue
        out.append((start, value, unit.start()))
    return out


def find_durations(text: str) -> list[RuleHit]:
    hits: list[RuleHit] = []
    context = _duration_context(text)
    # (match, Georgian, the count when written in words, where the count starts)
    found: list[tuple[re.Match[str], bool, int | None, int]] = [
        (m, is_ka, None, m.start())
        for pat, is_ka in ((_DUR_KA, True), (_DUR_EN, False))
        for m in pat.finditer(text)
    ]
    for words_start, words_count, at in _counts_in_words(text, [m.span() for m, *_ in found]):
        unit_match = _UNIT_AFTER_WORDS.match(text, at)
        if unit_match is not None:
            found.append((unit_match, True, words_count, words_start))
    for m, is_ka, count, start in found:
        if count is None:
            n = int(m.group(1) or m.group(2))
            qual = m.group(3).lower() if m.group(3) else None
            unit = m.group(4).lower()
        else:
            n, qual, unit = count, m.group(1), m.group(2).lower()
        end = m.end()
        if is_ka and unit.startswith("წ") and n >= 1900:
            continue  # "2026 წლის ..." is a date ("of the year 2026"), not a duration
        if is_ka:
            # the unit group is exactly one of the stems, so it is the key as it stands; slicing
            # it to three letters turned "კვირ" into "კვი" and weeks into "2 კვირs"
            unit = _UNIT_KA.get(unit, unit)
            qual = _qualifier_ka(qual) if qual else None
        ft = _classify(
            context,
            start,
            end,
            (
                FactType.warranty_period,
                FactType.delivery_deadline,
                FactType.execution_period,
                FactType.payment_terms,
                FactType.validity_period,
            ),
        )
        label = f"{n} {qual + ' ' if qual else ''}{unit}{'s' if n != 1 else ''}"
        hits.append(
            RuleHit(
                type=ft or FactType.duration,
                value=label,
                raw_text=text[start:end],
                start=start,
                end=end,
                normalized=f"P{n}{unit[0].upper()}" if unit != "week" else f"P{n}W",
                unit=unit,
                confidence=0.7 if ft else 0.45,
            )
        )
    return sorted(hits, key=lambda h: h.start)


# --- contract numbers --------------------------------------------------------------------------

# The document kinds that carry a reference number. "contract_number" is documented as the
# "contract/offer/tender reference number", but the pattern only knew about contracts, so an
# offer number was never found. A preceding keyword is still required: a bare "№ 12" in a
# table is a row number far more often than a reference.
_REFERENCED_DOC = (
    r"ხელშეკრულებ[ა-ჰ]*|შეთანხმებ[ა-ჰ]*|წინადადებ[ა-ჰ]*|შემოთავაზებ[ა-ჰ]*|განაცხად[ა-ჰ]*|"
    r"ტენდერ[ა-ჰ]*|შესყიდვ[ა-ჰ]*|ინვოის[ა-ჰ]*|აქტ[ა-ჰ]*|"
    r"contract|agreement|offer|proposal|quotation|quote|tender|invoice|order|act"
)
# A number sign, or an "N" that stands alone as one: "N 12", "N12", "No.", "Nº", "N:". The
# letter must not be the start of a word - the old "N[oº]?\.?" read the n of "number" as a
# marker and returned "umber" - and the reference itself must contain a digit.
_NUMBER_MARKER = r"(?:№|#|(?<![A-Za-z])N(?:[oº°]\.?|\.|:)?(?![A-Za-z]))"
# A reference: groups of letters and digits joined by "-", "/", "_" or ".", and by a slash with a
# space either side ("15 / 2026"), which the canonical form drops. Only around a slash: a space
# before "." or "-" is a sentence's end or a dash, and a bare space is where the reference ends.
#: A group may also be one Georgian letter standing alone between separators: "12/2026-ა" and
#: "12/2026-ბ" are two contracts, "ს-45/2026" one. Two letters or more after a hyphen are a case
#: ending ("15/2026-ის", "-ით", "-ში") and end the reference; so do "-ს" (dative) and "-ი"
#: (nominative) at its end, which are case endings too.
_KA_LETTER_GROUP = r"(?<![ა-ჰ])[ა-ჰ](?![ა-ჰ])"
_KA_LETTER_LAST = r"(?<![ა-ჰ])(?![სი])[ა-ჰ](?![ა-ჰ])"
_REFERENCE_GROUP = (
    rf"(?:{_KA_LETTER_GROUP}[\-/_.](?=[A-Za-z0-9]))?"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-/_.]{0,30}[A-Za-z0-9])?"
    rf"(?:[\-/_.]{_KA_LETTER_LAST})?"
)
_REFERENCE = rf"(?=[A-Za-z0-9ა-ჰ\-/_.]*\d){_REFERENCE_GROUP}(?:\s*/\s*{_REFERENCE_GROUP})*"
_SPACED_SLASH = re.compile(r"\s*/\s*")
# The document word must start a word: "act" inside "Contact" and "აქტ" inside "კონტაქტი" named
# a phone number a contract number.
_CONTRACT_NO = re.compile(
    rf"(?<![A-Za-zა-ჰ])(?:{_REFERENCED_DOC})\s*{_NUMBER_MARKER}\s*({_REFERENCE})",
    re.IGNORECASE,
)
#: The Georgian genitive puts the number first: "N 27/2026 ხელშეკრულების საფუძველზე",
#: "№12 შეთანხმებით". The marker stays required ("2026 წლის ხელშეკრულება" names no number), and
#: only the contract's own words follow it: "N 5 პუნქტის" is a clause, and an act's or an
#: invoice's number written this way is left unread rather than taken for the contract's.
_CONTRACT_WORD_AFTER = r"ხელშეკრულებ[ა-ჰ]*|შეთანხმებ[ა-ჰ]*|contract|agreement"
_CONTRACT_NO_BEFORE = re.compile(
    rf"{_NUMBER_MARKER}\s*({_REFERENCE})\s+(?:{_CONTRACT_WORD_AFTER})(?![A-Za-z])",
    re.IGNORECASE,
)


_LATIN_LOWER = re.compile(r"[a-z]+")


def _upper_latin(reference: str) -> str:
    """A reference in its canonical case: Latin letters upper case ("sp-2026/318" is
    "SP-2026/318"), Georgian letters as written - ``str.upper`` would turn them into Mtavruli
    ("12/2026-ა" is no "12/2026-Ა")."""
    return _LATIN_LOWER.sub(lambda m: m.group(0).upper(), reference)


def find_contract_numbers(text: str) -> list[RuleHit]:
    hits: list[RuleHit] = []
    matches = list(_CONTRACT_NO.finditer(text))
    matches += [
        m
        for m in _CONTRACT_NO_BEFORE.finditer(text)
        if not any(_overlaps(m.span(1), other.span(1)) for other in matches)
    ]
    for m in matches:
        number = _upper_latin(_SPACED_SLASH.sub("/", m.group(1)))
        if len(number) < 2 or not any(ch.isdigit() for ch in number):
            continue
        hits.append(
            RuleHit(
                type=FactType.contract_number,
                value=number,
                raw_text=m.group(0),
                start=m.start(),
                end=m.end(),
                normalized=number,
                confidence=0.8,
            )
        )
    return hits


# --- bank accounts & cadastral codes -----------------------------------------------------------

# Georgian IBAN, compact or printed in groups of four ("GE29 NB00 0000 ..."), and ending where a
# currency sub-account's code is written after it ("GE29...17GEL"): that code is no part of it.
_IBAN_GE = re.compile(
    r"\bGE\d{2}[ \u00a0]?[A-Z]{2}\d{2}(?:[ \u00a0]?\d{4}){3}[ \u00a0]?\d{2}"
    rf"(?:\b|(?=(?:{'|'.join(sorted(IBAN_CURRENCY_SUFFIXES))})\b))"
)
_SWIFT = re.compile(r"\b[A-Z]{4}GE[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")
_SPACE_CHARS = re.compile(r"[ \u00a0]")
#: A cadastral code: the parcel, "01.02.03.456", and a building or unit under it written with up
#: to three more groups ("01.19.21.004.024.01.513"). The full stop that ends a sentence may
#: follow it ("კოდი: 01.02.03.456."); more digits, or a dot and more digits, may not. Shared
#: with the anchors, so an act's code and a fact's code are read by one pattern.
CADASTRAL_RE = re.compile(
    r"(?<![\d.])\d{2}\.\d{2}\.\d{2}\.\d{3}(?:\.\d{3}(?:\.\d{2}(?:\.\d{3})?)?)?(?!\d|\.\d)"
)


def find_bank_accounts(text: str) -> list[RuleHit]:
    """Georgian IBANs and bank codes.

    An IBAN that fails its ISO 13616 checksum is kept, with a note and a lower confidence,
    rather than dropped: a mistyped account number is precisely what the acceptance check
    exists to report, and dropping it would turn "wrong account" into "no account". A bank code
    is taken only where ``unlabelled_bic_plausible`` accepts it, because the pattern alone also
    fits ordinary capitalised words.
    """
    hits: list[RuleHit] = []
    for m in _IBAN_GE.finditer(text):
        iban = _SPACE_CHARS.sub("", m.group(0))
        valid = iban_is_valid(iban)
        hits.append(
            RuleHit(
                type=FactType.bank_account,
                value=iban,
                raw_text=m.group(0),
                start=m.start(),
                end=m.end(),
                normalized=iban,
                unit="IBAN",
                confidence=0.9 if valid else 0.6,
                note=None if valid else "IBAN checksum does not validate (possible typo)",
            )
        )
    for m in _SWIFT.finditer(text):
        if not unlabelled_bic_plausible(text, m.start(), m.end()):
            continue
        hits.append(
            RuleHit(
                type=FactType.bank_code,
                value=m.group(0),
                raw_text=m.group(0),
                start=m.start(),
                end=m.end(),
                normalized=m.group(0),
                unit="SWIFT",
                confidence=0.8,
            )
        )
    return hits


def find_cadastral_codes(text: str) -> list[RuleHit]:
    return [
        RuleHit(
            type=FactType.cadastral_code,
            value=m.group(0),
            raw_text=m.group(0),
            start=m.start(),
            end=m.end(),
            normalized=m.group(0),
            confidence=0.85,
        )
        for m in CADASTRAL_RE.finditer(text)
    ]


# --- entry point -------------------------------------------------------------------------------


def run_rules(text: str) -> list[RuleHit]:
    """All deterministic extractors over one piece of text, sorted by position."""
    money = find_amounts(text)
    money += [
        h
        for h in find_header_amounts(text)
        if not any(_overlaps((h.start, h.end), (m.start, m.end)) for m in money)
    ]
    hits = [
        *money,
        *find_dates(text),
        *find_id_codes(text),
        *find_percentages(text),
        *find_durations(text),
        *find_contract_numbers(text),
        *find_bank_accounts(text),
        *find_cadastral_codes(text),
    ]
    return sorted(hits, key=lambda h: (h.start, h.end))


# --- normalising a value that did not come from these rules -------------------------------------

_MONEY_TYPES = frozenset({FactType.price, FactType.total_amount, FactType.amount})
_TIME_TYPES = frozenset(
    {
        FactType.contract_date,
        FactType.date,
        FactType.delivery_deadline,
        FactType.execution_period,
        FactType.validity_period,
        FactType.warranty_period,
        FactType.duration,
    }
)
_PERCENT_TYPES = frozenset({FactType.percentage, FactType.vat_status})
_MIXED_TYPES = frozenset({FactType.penalty, FactType.payment_terms})
_ID_DIGITS = re.compile(r"(?<!\d)(\d{9}|\d{11})(?!\d)")
_BIC_SHAPE = re.compile(r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?")
_IBAN_SHAPE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")
#: A reference from another source, read as ``find_contract_numbers`` reads one.
_REFERENCE_TOKEN = re.compile(_REFERENCE_GROUP)
_LEADING_MARKER = re.compile(r"^\s*(?:№|#|N[oº°]?\.?|N:)\s*", re.IGNORECASE)
#: A hit has to cover this share of the value to stand for it. "24 months" is a duration;
#: "30% advance, remainder within 10 days" is a summary that merely contains one.
_MIN_COVER = 0.5


def _only(hits: list[RuleHit], value: str) -> RuleHit | None:
    distinct = {str(h.normalized) for h in hits}
    if len(distinct) != 1:
        return None
    hit = hits[0]
    return hit if len(hit.raw_text.strip()) >= _MIN_COVER * len(value) else None


def normalize_fact_value(ft: FactType, value: str) -> str | float | None:
    """The canonical form the rules would have given ``value`` for a fact of type ``ft``.

    A fact from the LLM arrives as display text ("1000 GEL", "24 months"); the same fact from
    a rule arrives with ``normalized`` set (1000.0, "P24M"). The merger keys facts on the
    canonical form, so without this every LLM value was a different key from the matching
    rule value, and a document where both sources agreed was reported as a conflict. Running
    the value through the very functions that normalise rule hits is what guarantees the two
    keys are computed the same way. Returns None when the value is not a single recognisable
    entity - free text is then compared as text.
    """
    v = value.strip()
    if not v:
        return None
    if ft in (FactType.party_id_code, FactType.id_code):
        ids = {m.group(1) for m in _ID_DIGITS.finditer(v)}
        return ids.pop() if len(ids) == 1 else None
    if ft in (FactType.bank_account, FactType.bank_code):
        compact = re.sub(r"\s", "", v).upper()
        if _IBAN_SHAPE.fullmatch(compact) or _BIC_SHAPE.fullmatch(compact):
            return compact
        return None
    if ft == FactType.cadastral_code:
        m = CADASTRAL_RE.search(v)
        return m.group(0) if m else None
    if ft == FactType.contract_number:
        m = _REFERENCE_TOKEN.fullmatch(_SPACED_SLASH.sub("/", _LEADING_MARKER.sub("", v)))
        found = _upper_latin(m.group(0)) if m else ""
        return found if len(found) >= 2 and any(ch.isdigit() for ch in found) else None
    if ft in _MONEY_TYPES:
        hit = _only(find_amounts(v), v)
        if hit is not None:
            return hit.normalized
        numbers = {parse_number(n) for n in re.findall(_NUM_RE, v)}
        numbers.discard(None)
        if len(numbers) == 1 and len(re.sub(r"[^\d]", "", v)) * 2 >= len(re.sub(r"\s", "", v)):
            return numbers.pop()
        return None
    finders: list[Callable[[str], list[RuleHit]]] = []
    if ft in _TIME_TYPES:
        finders = [find_dates, find_durations]
    elif ft in _PERCENT_TYPES:
        finders = [find_percentages]
    elif ft in _MIXED_TYPES:
        finders = [find_percentages, find_amounts, find_durations]
    for find in finders:
        hit = _only(find(v), v)
        if hit is not None:
            return hit.normalized
    return None


def value_supported_by_quote(normalized: str | float, quote: str) -> bool:
    """Whether a canonical value can be read back out of the quote that supposedly states it.

    ``quote_in_chunk`` proves the quote is in the document; it says nothing about whether the
    value the model wrote next to it came from that quote. A model can copy a real sentence and
    attach a number it made up. This reads the quote with the same rules and asks whether any
    value found there is the one claimed.
    """
    if isinstance(normalized, float):
        for m in re.finditer(_NUM_RE, quote):
            n = parse_number(m.group(0))
            if n is not None and amounts_equal(n, normalized):
                return True
        return False
    target = str(normalized)
    compact_quote = re.sub(r"\s", "", quote).upper()
    if re.sub(r"\s", "", target).upper() in compact_quote:
        return True
    found = {str(h.normalized) for h in [*find_dates(quote), *find_durations(quote)]}
    if target in found:
        return True
    # "P30D" against "ოცდაათი (30) კალენდარული დღე": the duration pattern cannot read the
    # count through the parenthesis, but the count is plainly in the quote.
    duration = re.fullmatch(r"P(\d+)[DWMY]", target)
    if duration:
        return re.search(rf"(?<!\d){duration.group(1)}(?!\d)", quote) is not None
    return False
