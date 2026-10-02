"""A party's name and legal address against the registry's: what they say, not how they are spelt.

The registry and an act write the same party differently and both correctly. The registry writes a
company's legal form out in full beside the name or in a field of its own ("შეზღუდული
პასუხისმგებლობის საზოგადოება"), an act abbreviates it and quotes the name ("შპს „…“"). An address
in the registry carries the country, the district and every kind of place ("საქართველო, ქ.
თბილისის ვაკე-საბურთალოს რაიონში, ტესტის ქ. №42"); an act writes "ქ. თბილისი, ტესტის ქ. 42".
Comparing the strings made each of those an error on a correct act. So a name is compared as its
legal form (folded to its abbreviation) and its words, and an address as its place names - each
in any case - and its numbers, in order; the words that only say what kind of place a name is,
the country and the district are not compared.

An address that keeps every name and number of the registry's, in order, but leaves some out - a
flat, a floor - is not the same address either: the act "omits" them.
"""

from __future__ import annotations

import re
from typing import Literal

from procurement_agent.extraction.normalize import (
    LEGAL_FORM,
    legal_form_abbreviation,
    normalize_spaces,
)

__all__ = ["address_parts", "compare_addresses", "same_place_word", "split_legal_form"]

_QUOTES = re.compile(r"[„“”\"«»'`]")
_FORM_FIRST = re.compile(rf"^\s*({LEGAL_FORM})(?=[\s„“”\"«'(]|$)")
_FORM_LAST = re.compile(rf"[\s,]({LEGAL_FORM})\s*$")


def split_legal_form(name: str) -> tuple[str | None, str]:
    """(the abbreviation of the legal form the name is written with, the name without it).

    "შპს „ტესტი“", "შეზღუდული პასუხისმგებლობის საზოგადოება „ტესტი“" and "„ტესტი“ შპს" are all
    ("შპს", "ტესტი"); a name with no legal form is (None, the name). Quotes are dropped.
    """
    text = normalize_spaces(name or "")
    form: str | None = None
    m = _FORM_FIRST.match(text) or _FORM_LAST.search(text)
    if m is not None:
        form = legal_form_abbreviation(m.group(1))
        text = f"{text[: m.start(1)]} {text[m.end(1) :]}"
    return form, normalize_spaces(_QUOTES.sub(" ", text)).strip(" ,.")


#: Words saying what kind of place a name is - not compared: abbreviated, exactly ("ქ" is the
#: city or the street, never the start of "ქუთაისი"), or written out in any case.
_PLACE_KIND = re.compile(
    r"ქ|ბ|შეს|დას|სართ|კორპ|სოფ|მოედ|გამზ|მ/რ|ოთ"
    r"|(?:ქალაქ|ქუჩ|გამზირ|შესახვევ|ჩიხ|მოედან|ხეივან|დასახლებ|მიკრორაიონ|კორპუს|ბინ|სართულ"
    r"|სადარბაზო|ოთახ|სოფელ|დაბა|ტერიტორი)[ა-ჰ]*"
    r"|n|no|№|#|street|st|avenue|ave|city|apt|flat|floor",
    re.IGNORECASE,
)
_COUNTRY = re.compile(r"საქართველო[ა-ჰ]*|georgia", re.IGNORECASE)
#: A district or municipality, which one address names and another leaves out: the word and the
#: name before it ("ვაკე-საბურთალოს რაიონში", "მთაწმინდის რ-ნი") are not compared.
_DISTRICT = re.compile(r"რაიონ[ა-ჰ]*|რ-ნ[ა-ჰ]*|მუნიციპალიტეტ[ა-ჰ]*|district", re.IGNORECASE)
_TOKEN = re.compile(r"[^\s,;.()\[\]:]+")
_NUMBER = re.compile(r"^[N№]?(\d[\d/-]*[ა-ჰa-z]?)$", re.IGNORECASE)

Part = tuple[Literal["number", "word"], frozenset[str]]


def address_parts(address: str) -> list[Part]:
    """The parts of an address that are compared, in order: its numbers and its place names. A
    number is marked "№", "N" or "#" ("#8"), or not at all."""
    marked = address.replace("№", " № ").replace("#", " # ")
    tokens = [_QUOTES.sub("", t) for t in _TOKEN.findall(marked)]
    tokens = [t for t in tokens if t]
    parts: list[Part] = []
    for i, token in enumerate(tokens):
        if _DISTRICT.fullmatch(token):
            if parts and parts[-1][0] == "word":
                parts.pop()  # the district's name, written before the word
            continue
        if _PLACE_KIND.fullmatch(token) or _COUNTRY.fullmatch(token):
            continue
        number = _NUMBER.match(token)
        if number is not None:
            parts.append(("number", frozenset({number.group(1).lower()})))
            continue
        if i + 1 < len(tokens) and _DISTRICT.fullmatch(tokens[i + 1]):
            parts.append(("word", frozenset({token.lower()})))  # popped at the district word
            continue
        parts.append(("word", frozenset({token.lower()})))
    return parts


def same_place_word(a: str, b: str) -> bool:
    """One place name in two of the forms an address writes it in: the same, or one the
    genitive or dative of the other ("თბილისი", "თბილისის"; "ფშაველა", "ფშაველას"), a name in
    -ე with its genitive in -ის ("ჭავჭავაძე", "ჭავჭავაძის"), a name in -ი with its locative in -ში
    ("თბილისი", "თბილისში"). Nothing looser: a vowel changed is another name, or a slip
    ("რუსთაველას" is not "რუსთაველის") - a stem match would hide every slip in a last syllable."""
    if a == b:
        return True
    for x, y in ((a, b), (b, a)):
        if y == f"{x}ს":
            return True
        if x.endswith("ე") and y == f"{x[:-1]}ის":
            return True
        if x.endswith("ი") and y == f"{x[:-1]}ში":
            return True
    return False


def _same(a: Part, b: Part) -> bool:
    if a[0] != b[0]:
        return False
    if a[0] == "number":
        return a[1] == b[1]
    x, y = next(iter(a[1])), next(iter(b[1]))
    # an initial for a street's namesake: "ა. წერეთლის" is "აკაკი წერეთლის" (red team R6, R6-11)
    if len(x) == 1 or len(y) == 1:
        return len(x) != len(y) and (y.startswith(x) if len(x) == 1 else x.startswith(y))
    return same_place_word(x, y)


def compare_addresses(act: str, registry: str) -> Literal["same", "omits", "different"]:
    """Whether the act's address is the registry's: the same names and numbers in order
    ("same"); every one of the act's among the registry's, in order, with some left out
    ("omits"); or not ("different")."""
    a, r = address_parts(act), address_parts(registry)
    if not a or not r:
        return "different"
    if len(a) == len(r) and all(_same(x, y) for x, y in zip(a, r, strict=True)):
        return "same"
    # the act's parts as a subsequence of the registry's: an address shortened, not changed
    j = 0
    for part in r:
        if j < len(a) and _same(a[j], part):
            j += 1
    return "omits" if j == len(a) else "different"
