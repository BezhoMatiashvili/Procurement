"""What scanning does to text, for the checks that read a scanned document.

Tesseract misreads a letter as another that looks like it, drops a letter, doubles one, and reads
punctuation that is not there. A check that compares a scanned act with its contract letter by
letter reports each such misreading as a wrong name or a wrong address - the correct scanned act
of the client's set got three errors that way. Two things here keep that apart from a real
mistake:

* ``repair_codes`` puts back the fixed shape of a code: an IBAN is "GE", two digits, a bank's
  two letters and sixteen digits, so "CL29N80000000101904917" can only be
  "GE29NB0000000101904917". A character is replaced only by one it is commonly misread as, and
  only where the code's shape demands a letter or a digit; the text keeps its length, so every
  offset into it still points at the same place.
* ``misread_explains`` says whether two values differ by no more than what scanning does - a
  few letter slips for the value's length, a dropped or doubled letter, punctuation. A digit
  read as another digit counts only for shapes that are alike ("8" and "3"); two digits
  swapped is a typist's slip, never the scanner's.

A difference ``misread_explains`` accepts is still reported, as a possible misreading to check
against the paper original, never passed over: the scanner cannot tell a real typo from its
own, and neither can the check.
"""

from __future__ import annotations

import re
import unicodedata

from procurement_agent.extraction.georgian import damerau
from procurement_agent.extraction.normalize import QUOTE_CHARS, normalize_spaces

__all__ = ["BANK_CODES", "misread_explains", "repair_codes"]

#: The two-letter bank codes of Georgian IBANs (NBG's register): the letters after "GE" and the
#: check digits.
BANK_CODES = frozenset(
    {"BG", "TB", "LB", "PC", "BS", "CD", "HS", "KS", "CR", "VT", "IS", "ZB", "PB", "NB", "TR"}
)

#: A digit read where a letter is due, and a letter read where a digit is due.
_AS_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}
_AS_DIGIT = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "G": "6",
             "B": "8", "T": "7"}  # fmt: skip
#: Letters the scanner mistakes for each other in Latin capitals.
_LATIN_ALIKE = {
    frozenset(p) for p in ("CG", "EL", "EF", "OD", "OQ", "IL", "IT", "BR", "MN", "UV", "PR", "KX")
}
#: What the first two characters of a Georgian IBAN may be read as.
_G_READS = frozenset("GC6OQ")
_E_READS = frozenset("ELF83B")
#: Digits whose shapes are alike enough to be read for each other.
_DIGITS_ALIKE = {frozenset(p) for p in ("38", "89", "56", "68", "08", "06", "09", "17")}
_PUNCT = frozenset(".,:;'\"`|!-_()[]{}/\\")

_IBAN_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{22}(?![A-Za-z0-9])")
_IBAN = re.compile(r"GE\d{2}[A-Z]{2}\d{16}")
_BIC_LABEL = re.compile(r"(?:ბანკის\s+კოდი|SWIFT|BIC)\s*:?\s*", re.IGNORECASE)
_BIC_TOKEN = re.compile(r"[A-Za-z0-9]{8}(?:[A-Za-z0-9]{3})?(?![A-Za-z0-9])")
_BIC = re.compile(r"[A-Z]{4}GE[A-Z0-9]{2}(?:[A-Z0-9]{3})?")
#: A day of the month read as a bar or a letter: "2026 წლის | აგვისტოდან". After "წლის" and
#: before a word, only a number fits.
_DAY_AS_BAR = re.compile(r"((?:წლის|წ\.)\s+)[|lI](\s+[ა-ჰ])")


def _alike_letters(a: str, b: str) -> bool:
    return a == b or frozenset((a, b)) in _LATIN_ALIKE


def _bank_code(two: str) -> str | None:
    """The known bank code two scanned characters stand for, if exactly one fits."""
    letters = "".join(_AS_LETTER.get(c, c) for c in two)
    if letters in BANK_CODES:
        return letters
    near = [
        code
        for code in BANK_CODES
        if sum(a != b for a, b in zip(code, letters, strict=True)) == 1
        and all(_alike_letters(a, b) for a, b in zip(code, letters, strict=True))
    ]
    return near[0] if len(near) == 1 else None


def _iban(token: str) -> str | None:
    """``token`` as the IBAN it was scanned from, or None when it cannot be one."""
    t = token.upper()
    if _IBAN.fullmatch(t):
        return t
    if t[0] not in _G_READS or t[1] not in _E_READS:
        return None
    digits = [_AS_DIGIT.get(c, c) for c in t[2:4] + t[6:]]
    if not all(d.isdigit() for d in digits):
        return None
    code = _bank_code(t[4:6])
    if code is None:
        return None
    return "GE" + "".join(digits[:2]) + code + "".join(digits[2:])


def _bic(token: str) -> str | None:
    t = token.upper()
    if _BIC.fullmatch(t):
        return t
    head = "".join(_AS_LETTER.get(c, c) for c in t[:4])
    country = ("G" if t[4] in _G_READS else t[4]) + ("E" if t[5] in _E_READS else t[5])
    fixed = head + country + t[6:]
    return fixed if _BIC.fullmatch(fixed) else None


def repair_codes(text: str) -> str:
    """``text`` with its scanned IBANs, the bank codes after a bank-code label and a date's day
    read as a bar put back in shape. Only for text that came from OCR; the length never
    changes."""

    def iban(m: re.Match[str]) -> str:
        return _iban(m.group(0)) or m.group(0)

    text = _IBAN_TOKEN.sub(iban, text)
    text = _DAY_AS_BAR.sub(r"\g<1>1\g<2>", text)
    out = list(text)
    for label in _BIC_LABEL.finditer(text):
        m = _BIC_TOKEN.match(text, label.end())
        if m and (fixed := _bic(m.group(0))) is not None:
            out[m.start() : m.end()] = fixed
    return "".join(out)


# --- a difference scanning explains ------------------------------------------------------------


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    s = "".join(" " if ch in QUOTE_CHARS else ch for ch in s)
    return normalize_spaces(s).upper()


def _alignment(a: str, b: str) -> list[tuple[str, str]]:
    """Levenshtein alignment of ``a`` onto ``b``: (a's character or "", b's character or "") per
    edit - a transposition is two substitutions, which is what makes it a typist's slip here."""
    rows, cols = len(a) + 1, len(b) + 1
    dist = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        dist[i][0] = i
    for j in range(cols):
        dist[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            dist[i][j] = min(
                dist[i - 1][j] + 1,
                dist[i][j - 1] + 1,
                dist[i - 1][j - 1] + (a[i - 1] != b[j - 1]),
            )
    edits: list[tuple[str, str]] = []
    i, j = len(a), len(b)
    while i or j:
        if i and j and dist[i][j] == dist[i - 1][j - 1] + (a[i - 1] != b[j - 1]):
            if a[i - 1] != b[j - 1]:
                edits.append((a[i - 1], b[j - 1]))
            i, j = i - 1, j - 1
        elif i and dist[i][j] == dist[i - 1][j] + 1:
            edits.append((a[i - 1], ""))
            i -= 1
        else:
            edits.append(("", b[j - 1]))
            j -= 1
    return edits


#: Longer than this, a value is not compared letter by letter: its difference is not a misread.
_MAX_LENGTH = 120


def _budget(length: int) -> int:
    """How many letter slips a value of this length may carry and still be a misreading."""
    return 1 if length < 10 else 2 if length < 30 else 3


def _free(x: str, y: str) -> bool:
    """Punctuation or a space read, dropped or read as other punctuation: no slip at all."""
    return all(c in _PUNCT or c in " " for c in (x, y) if c)


def misread_explains(read: str, meant: str) -> bool:
    """Whether scanning alone can turn ``meant`` into ``read``: a few slips for the length,
    punctuation and spaces free, and never a digit swapped for an unlike digit."""
    a, b = _fold(meant), _fold(read)
    if a == b:
        return True
    if not a or not b or max(len(a), len(b)) > _MAX_LENGTH:
        return False
    edits = _alignment(a, b)
    # two neighbours swapped ("…17" for "…71") is a typist's slip, whatever the characters: a
    # swap is one edit to Damerau and two to Levenshtein
    if damerau(a, b, cap=len(edits) + 1) < len(edits):
        return False
    slips = 0
    for x, y in edits:
        if _free(x, y):
            continue
        if x.isdigit() and y.isdigit() and frozenset((x, y)) not in _DIGITS_ALIKE:
            return False
        slips += 1
    return slips <= _budget(len(a))
