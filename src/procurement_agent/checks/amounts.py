"""Reading amounts out of text, shared by the AOA check and the document comparison.

Both checks need the same three things - the number in a value however it is written, the
monetary amounts a document states, and the amounts it spells out in words - and each used to
carry its own copy. Two copies of a parser drift: one learns a currency format, the other does
not, and the two checks start disagreeing about what the same document says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from procurement_agent.extraction.georgian import damerau, number_to_words, parse_number_words
from procurement_agent.extraction.normalize import parse_georgian_number_words, parse_number
from procurement_agent.extraction.rules import (
    _TABLE_CELL,
    _TOTAL_ROW,
    _norm_currency,
    find_amounts,
)
from procurement_agent.extraction.schemas import FactType

__all__ = [
    "CURRENCY",
    "FIGURE",
    "FIGURE_TETRI",
    "NUM_IN_TEXT",
    "DigitsAndWords",
    "Money",
    "digits_and_words",
    "figure_value",
    "header_money",
    "looks_like_numeral",
    "money_amounts",
    "money_hits",
    "number_of",
    "read_words",
    "render_figure_like",
    "word_amounts",
    "words_for",
    "words_like",
]

#: The first run of digits (with grouping) in a string.
NUM_IN_TEXT = re.compile("\\d[\\d\\s.,\u00a0]*\\d|\\d")

_MONEY_TYPES = frozenset({FactType.price, FactType.total_amount, FactType.amount})

#: A figure as documents write it: digits grouped in threes by a space, a dot or a comma, and
#: an optional decimal part - "2 400", "2.400,50", "100". A group is exactly three digits after
#: one separator, so an item number in front ("საფასური: 1. 100 (ასი)") is not read into it,
#: which a looser pattern did: it read 1100 there and reported the words as wrong.
FIGURE = r"\d{1,3}(?:[ \u00a0.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
#: A figure with its tetri written after a hyphen, the bookkeeping style (red team R3, AOA-R3-03):
#: "3 450-00" is 3450.00. Exactly two digits, straight after the hyphen; whether they are tetri
#: or the end of a range ("10-15 ლარი") is ``figure_value``'s to say.
FIGURE_TETRI = rf"(?:{FIGURE})(?:-\d{{2}}(?!\d))?"
#: Where a figure may start: not inside another number, and not as the two tetri digits after a
#: figure's hyphen ("00" of "3 450-00").
_FIGURE_START = r"(?<![\d.,])(?!(?<=\d-)\d{2}(?!\d))"
_HYPHEN_TETRI = re.compile(rf"({FIGURE})-(\d{{2}})")
_HAS_FRACTION = re.compile(r"[.,]\d{1,2}$")
#: "100 (ასი) ლარს" - a figure, then the same number written out. Digits may appear inside the
#: brackets only for tetri ("ლარი და 50 თეთრი").
_DIGITS_THEN_WORDS = re.compile(
    rf"{_FIGURE_START}({FIGURE_TETRI})(?!\d)\s*\(\s*([ა-ჰ][ა-ჰ0-9 ,.]{{1,80}}?)\s*"
    # a scan reads the closing bracket as ":" or "|" now and then; before a currency word
    # nothing else can be meant
    r"(?:\)|[:;|\]}](?=\s*(?:აშშ\s+)?(?:ლარ|დოლარ|ევრო)))"
)
#: A currency as it stands beside a figure: a word in any case ("ლარს", "აშშ დოლარი"), a code
#: or a sign.
CURRENCY = r"(?:(?:აშშ\s+)?(?:ლარ|დოლარ|ევრო)[ა-ჰ]*|GEL|USD|EUR|₾|\$|€)"
#: "100 ლარს (ასი)", "100 ₾ (ასი)": the currency between the figure and its words. Only a bracket
#: that reads as a numeral makes such a pair (``digits_and_words``): "100 ლარი (ერთი თვის)" is
#: no amount in words.
_DIGITS_CURRENCY_WORDS = re.compile(
    rf"{_FIGURE_START}({FIGURE_TETRI})(?!\d)\s*{CURRENCY}\s*\(\s*([ა-ჰ][ა-ჰ0-9 ,.]{{1,80}}?)\s*\)"
)
#: Currency words inside the brackets: "(ასი ლარი)", "(ათასი აშშ დოლარი)".
_CURRENCY_WORD = re.compile(r"(?:აშშ\s+)?(?:ლარ|დოლარ|ევრო|ფუნტ|რუბლ)[ა-ჰ]*")
#: The tetri word that ends a bracket: "… და 50 თეთრი", "… ორმოცდაათი თეთრს".
_TETRI_WORD = re.compile(r"\s*(?:თეთრ|ცენტ)[ა-ჰ]*\s*$")
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


def _tetri_value(whole: str, tetri: str) -> float | None:
    """A figure and the two digits after its hyphen as lari and tetri: "3 450" and "00" are
    3450.0. None when the two digits read as the end of a range instead - more than the figure
    before them ("10-15", "90-95") - or when the figure has a fraction of its own."""
    if _HAS_FRACTION.search(whole):
        return None
    value = parse_number(whole)
    cents = int(tetri)
    if value is None or cents > value:
        return None
    return round(value + cents / 100, 2)


def figure_value(figure: str) -> float | None:
    """The number a figure (``FIGURE_TETRI``) states: "2 400" is 2400.0, and "3 450-00", its
    tetri after a hyphen, 3450.0. None for a hyphen that joins a range ("10-15")."""
    m = _HYPHEN_TETRI.fullmatch(figure.strip())
    if m is not None:
        return _tetri_value(m.group(1), m.group(2))
    return parse_number(figure)


#: The figure a hyphen joins to the tetri an amount starts with: "3 450-" of "3 450-00 ლარს".
_WHOLE_BEFORE = re.compile(rf"(?<![\d.,/-])({FIGURE})-$")
#: Tetri after the hyphen that ends an amount written currency first: "-00" of "GEL 3 450-00".
_TETRI_AFTER_HYPHEN = re.compile(r"-(\d{2})(?![\d.,]?\d)")


def _hyphen_tetri(text: str, h: Money) -> Money:
    """``h`` with the rest of a "3 450-00" figure the rule hit left out: the lari before the
    hyphen when the hit starts at the tetri ("00 ლარს"), or the tetri after it when the hit ends
    at the lari ("GEL 3 450"). ``h`` as it is when there is none, or it reads as a range."""
    start, end = h.span
    tetri = re.match(r"(\d{2})(?![\d.,]?\d)", h.raw)
    if tetri is not None and h.value == int(tetri.group(1)):
        whole = _WHOLE_BEFORE.search(text, max(0, start - 30), start)
        if whole is not None:
            value = _tetri_value(whole.group(1), tetri.group(1))
            if value is not None:
                a = whole.start(1)
                return Money(value=value, currency=h.currency, span=(a, end), raw=text[a:end])
    after = _TETRI_AFTER_HYPHEN.match(text, end)
    lari = re.search(rf"({FIGURE})$", h.raw)
    if after is not None and lari is not None and float(h.value).is_integer():
        value = _tetri_value(lari.group(1), after.group(1))
        if value is not None:
            b = after.end()
            return Money(value=value, currency=h.currency, span=(start, b), raw=text[start:b])
    return h


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
    penalty instead of losing it. A figure with its tetri after a hyphen ("3 450-00 ლარს") is one
    amount, 3450.00, never its tetri "00" (red team R3, AOA-R3-03).
    """
    out: list[Money] = []
    for h in sorted(find_amounts(text), key=lambda h: (h.start, h.end)):
        if not h.currency or (h.type not in _MONEY_TYPES and not penalties):
            continue
        n = number_of(h.value, h.normalized if isinstance(h.normalized, float) else None)
        if n is not None:
            money = Money(value=n, currency=h.currency, span=(h.start, h.end), raw=h.raw_text)
            out.append(_hyphen_tetri(text, money))
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
    #: the words as written, currency words and tetri left out: "ასი" of "(ასი ლარი)"
    words: str
    #: the words as a number (tetri included), or None when they do not read as one - strictly:
    #: "ათასი ათასი" is not 2000
    spelled: float | None
    raw: str
    span: tuple[int, int]
    #: where the figure and the words are, for a finding that marks only the wrong half
    digits_span: tuple[int, int] = (0, 0)
    words_span: tuple[int, int] = (0, 0)
    #: whether the bracket holds a numeral at all: "დანართი 1 (ტექნიკური დავალება)" does not
    numeral: bool = True

    @property
    def digits_raw(self) -> str:
        return self.raw[: self.digits_span[1] - self.span[0]].strip()


#: Every word a numeral up to a thousand is written with, and the scale words: the vocabulary a
#: bracket must mostly be made of to be an amount in words, typos allowed.
_NUMERAL_WORDS = frozenset(
    {w for n in range(1, 1000) for w in number_to_words(n).split()}
    | {w for n in (1000, 2000, 10**6, 2 * 10**6, 10**9) for w in number_to_words(n).split()}
    | {"ათას", "მილიონ", "მილიარდ", "ნული"}
)


#: Two-letter words a bracket may hold that are words in their own right, not a numeral with a
#: letter dropped: "(და სხვა)", "(ან …)".
_SHORT_WORDS = frozenset(
    {"და", "ან", "თუ", "ის", "ეს", "ამ", "იმ", "არ", "ნუ", "მე", "კი", "რა", "ოჰ", "ჰო", "აქ"}
)


def _is_numeral_word(word: str) -> bool:
    """A numeral word, or one garbled: one edit away, or two that keep its first two letters
    ("ხუთთთ" for "ხუთი") - enough to say "a number was meant here" without reading "ოფისი"
    (office) as one."""
    if word in _NUMERAL_WORDS or parse_georgian_number_words(word) is not None:
        return True
    if len(word) == 2:  # "აი" for "ასი": a letter dropped from a short numeral
        return word not in _SHORT_WORDS and any(
            len(w) == 3 and damerau(word, w, cap=1) == 1 for w in _NUMERAL_WORDS
        )
    if len(word) < 3:
        return False
    for w in _NUMERAL_WORDS:
        if abs(len(w) - len(word)) > 2:
            continue
        d = damerau(word, w, cap=2)
        if d <= 1 or (d == 2 and len(word) >= 4 and word[:2] == w[:2]):
            return True
    return False


def looks_like_numeral(words: str) -> bool:
    """Whether a bracket is a number written out, allowing for a typo in it."""
    toks = [t for t in words.split() if t != "და"]
    hits = sum(1 for t in toks if _is_numeral_word(t))
    return bool(toks) and hits * 2 >= len(toks)


def _split_tetri(inner: str) -> tuple[str, str] | None:
    """A bracket's lari words and its tetri, apart: "ასი ლარი და ორმოცდაათი თეთრი" is ("ასი
    ლარი", "ორმოცდაათი"). The tetri are what follows the currency word, or, with none, the
    last "და"; None when the bracket names no tetri."""
    m = _TETRI_WORD.search(inner)
    if m is None:
        return None
    head = inner[: m.start()]
    currencies = list(_CURRENCY_WORD.finditer(head))
    if currencies:
        cut = currencies[-1].end()
    elif (conj := head.rfind(" და ")) >= 0:
        cut = conj
    else:
        cut = 0
    tetri = re.sub(r"^[\s,]*(?:და\s+)?", "", head[cut:]).strip()
    return head[:cut], tetri


def read_words(inner: str) -> tuple[str, float | None]:
    """The numeral part of a bracket and its value, tetri included: "ასი ლარი და 50 თეთრი" and
    "ასი ლარი და ორმოცდაათი თეთრი" are ("ასი", 100.5). A value that does not parse strictly is
    None, and so are tetri that are not 0-99."""
    tetri = 0.0
    if (split := _split_tetri(inner)) is not None:
        inner, part = split
        cents = int(part) if part.isdigit() else parse_number_words(part)
        words = " ".join(_CURRENCY_WORD.sub(" ", inner).replace(",", " ").split())
        if cents is None or not 0 <= cents < 100:  # a lari has 100 tetri
            return words, None
        tetri = cents / 100
    words = " ".join(_CURRENCY_WORD.sub(" ", inner).replace(",", " ").split())
    words = re.sub(r"\s+და$", "", words)
    if not words and split is not None:
        return words, tetri  # tetri alone: "0.50 (ორმოცდაათი თეთრი)"
    value = parse_number_words(words)
    return words, None if value is None else round(value + tetri, 2)


def digits_and_words(text: str) -> list[DigitsAndWords]:
    """Every figure immediately restated in words, or with only its currency between the two
    ("100 ლარს (ასი)"). Pairs whose digits do not parse are left out."""
    out: list[DigitsAndWords] = []
    direct = list(_DIGITS_THEN_WORDS.finditer(text))
    across = [
        m
        for m in _DIGITS_CURRENCY_WORDS.finditer(text)
        if looks_like_numeral(read_words(m.group(2).strip())[0])
    ]
    for m in sorted([*direct, *across], key=lambda m: m.start()):
        digits_span = m.span(1)
        digits = figure_value(m.group(1))
        if digits is None and (joined := _HYPHEN_TETRI.fullmatch(m.group(1))) is not None:
            # a range, "10-15 (თხუთმეტი)": the figure before the words is the one they restate
            digits_span = (m.start(1) + joined.start(2), m.end(1))
            digits = parse_number(joined.group(2))
        if digits is None:
            continue
        inner = m.group(2).strip()
        words, spelled = read_words(inner)
        at = text.find(words, m.start(2), m.end(2) + 1) if words else -1
        words_span = (at, at + len(words)) if at >= 0 else m.span(2)
        out.append(
            DigitsAndWords(
                digits=digits,
                words=words,
                spelled=spelled,
                raw=text[digits_span[0] : m.end()].strip(),
                span=(digits_span[0], m.end()),
                digits_span=digits_span,
                words_span=words_span,
                numeral=looks_like_numeral(words),
            )
        )
    return out


#: A currency a table's column header names: "ღირებულება (ლარი)", "თანხა, ლარი", "ღირებულება
#: ლარში", "Amount (USD)".
_HEADER_CURRENCY = re.compile(rf"(?<![ა-ჰA-Za-z])(?:{CURRENCY})(?![A-Za-z])")
#: A column of rates, not of amounts: "დოლარის კურსი", "Rate (GEL)".
_RATE_COLUMN = re.compile(r"კურს|(?<![a-z])rate(?![a-z])", re.IGNORECASE)
#: A table cell's value that is a figure alone: "3 540", "3 450.00", "3 450-00", ended by the
#: cell's stop or a space.
_CELL_FIGURE = re.compile(rf"\s*({FIGURE_TETRI})(?!\d)(?=\.?(?:\s|$))")
#: A currency right after a figure, which makes it the money reader's.
_CURRENCY_AFTER = re.compile(rf"\s*{CURRENCY}")
#: A total row that is not the document's total: the total before tax.
_NOT_THE_TOTAL = re.compile(
    r"გარეშე|გამოკლებით|(?<![a-z])(?:excl|excluding|without|before|net)(?![a-z])", re.IGNORECASE
)


def header_money(text: str) -> tuple[list[Money], list[Money]]:
    """The figures of a price table whose column header names the currency ("ღირებულება (ლარი) =
    3 540"), which the money reader leaves alone: nothing beside the figure says it is money
    (red team R3, AOA-R3-09). Docling writes a table cell as "<row key>, <column header> =
    <value>" (``rules._TABLE_CELL``); a row is the cells from one key on until the key changes
    or a column comes round again. The text may be flattened to one line, so a cell's value is
    read as the figure it starts with.

    Returns the document's stated total - the figure in the last row a total word names ("სულ",
    "ჯამი") that is not a total before tax - and, when no row is one, every such figure: a line
    of a list is no total, and whoever reads it must say it was not compared. A figure with its
    currency written beside it is the money reader's, and not read here."""
    cells = list(_TABLE_CELL.finditer(text))
    rows: list[list[tuple[str, str, str, re.Match[str] | None]]] = []
    for c, nxt in zip(cells, [*cells[1:], None], strict=False):
        key, header = c.group(1).strip(), c.group(2).strip()
        end = nxt.start() if nxt is not None else len(text)
        figure = _CELL_FIGURE.match(text, c.end(), end)
        if figure is not None and _CURRENCY_AFTER.match(text, figure.end(1), end):
            figure = None  # "3 540 ლარი": the money reader's
        # the last cell of a flattened table runs on into the text after it
        value = text[c.end() : end].strip().rstrip(".").strip()
        if figure is not None and nxt is None:
            value = figure.group(1)
        cell = (key, header, value, figure)
        row = rows[-1] if rows else None
        if row is None or row[0][0] != key or any(h == header for _, h, _, _ in row):
            rows.append([cell])
        else:
            row.append(cell)
    totals: list[Money] = []
    lines: list[Money] = []
    for row in rows:
        total = any(
            _TOTAL_ROW.fullmatch(x) for x in (row[0][0], *(v for _, _, v, _ in row))
        ) and not any(_NOT_THE_TOTAL.search(f"{k} {v}") for k, _, v, _ in row)
        for _, header, _, figure in row:
            named = _HEADER_CURRENCY.search(header)
            if figure is None or named is None or _RATE_COLUMN.search(header):
                continue
            amount = figure_value(figure.group(1))
            currency = _norm_currency(named.group(0))
            if amount is None or currency is None:
                continue
            money = Money(value=amount, currency=currency, span=figure.span(1), raw=figure.group(1))
            (totals if total else lines).append(money)
    return (totals[-1:], []) if totals else ([], lines)


def words_for(value: float) -> str:
    """How an amount is written out in the brackets after its figure: "ასი", or with tetri
    "ასი ლარი და 50 თეთრი"."""
    lari = int(value)
    cents = round((value - lari) * 100)
    base = number_to_words(lari)
    return f"{base} ლარი და {cents} თეთრი" if cents else base


def words_like(value: float, example: str) -> str:
    """``value`` written out the way the bracket ``example`` writes its amount: the number words
    anew, the currency word and the tetri as the example has them - "შვიდასი ლარი და 00 თეთრი"
    with 100 gives "ასი ლარი და 00 თეთრი". Tetri written in digits keep their width, tetri in
    words are written in words. With no currency word in the example, ``words_for``."""
    lari = int(value)
    cents = round((value - lari) * 100)
    split = _split_tetri(example)
    head = split[0] if split is not None else example
    currencies = list(_CURRENCY_WORD.finditer(head))
    if not currencies:
        return words_for(value)
    out = f"{number_to_words(lari)} {currencies[-1].group(0)}"
    tail = _TETRI_WORD.search(example)
    if split is not None and tail is not None:
        written = split[1]
        if written.isdigit():
            tetri = f"{cents:0{len(written)}d}"
        else:
            tetri = number_to_words(cents) if cents else "ნული"
        return f"{out} და {tetri} {tail.group(0).strip()}"
    return f"{out} და {cents} თეთრი" if cents else out


def render_figure_like(value: float, example: str) -> str:
    """``value`` written the way ``example`` writes a figure: "2 400", "2,400.00", "2400", and
    with its tetri after a hyphen, "3 450-00"."""
    ex = example.strip()
    if (hyphen := _HYPHEN_TETRI.fullmatch(ex)) is not None:
        lari, cents = divmod(round(value * 100), 100)
        return f"{render_figure_like(lari, hyphen.group(1))}-{cents:02d}"
    decimals = re.search(r"[.,](\d{1,2})$", ex)
    grouped = re.search(r"\d[ \u00a0.,]\d{3}", ex)
    sep = grouped.group(0)[1] if grouped else ""
    dec_sep = decimals.group(0)[0] if decimals else ""
    places = len(decimals.group(1)) if decimals else 0
    whole = f"{value:,.{places}f}" if places else f"{round(value):,}"
    int_part, _, frac = whole.partition(".")
    int_part = int_part.replace(",", sep)
    return f"{int_part}{dec_sep}{frac}" if places else int_part
