"""Reading an act's and a contract's terms one sentence at a time.

The act date, the role an amount plays (price, advance, penalty), the term and the payment
conditions are each stated in one clause, and every check that reads them has to agree on
where that clause ends. A private splitter per check drifts: one learns that "ქ. თბილისი" is
an abbreviation, another does not, and two rules then read the same sentence differently.

The text the checks see is flattened (``DocumentView.text`` has no line breaks), so a numbered
clause is recognised by its number rather than by the line it starts.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Literal

from procurement_agent.checks.amounts import Money, money_hits
from procurement_agent.checks.compare_llm import _NEGATIONS
from procurement_agent.extraction.anchors import preamble_end
from procurement_agent.extraction.rules import (
    KEYWORDS,
    RuleHit,
    find_dates,
    find_durations,
    find_percentages,
    nearest_keyword_group,
    percent_of,
)
from procurement_agent.extraction.schemas import FactType

__all__ = [
    "AdvancePayment",
    "AdvanceShare",
    "AdvanceTerms",
    "AmountRole",
    "ContractTerm",
    "Deadline",
    "MethodHit",
    "PaymentMethod",
    "PaymentTerms",
    "RoledAmount",
    "TaxPhrase",
    "TermRange",
    "amount_roles",
    "fee_scope",
    "read_advance",
    "read_payment_terms",
    "read_term",
    "sentence_at",
    "sentence_spans",
    "tax_phrases",
]

#: ".", ";", "!" or "?" followed by whitespace, or at the very end. "0.5%" and "48 750,00" have
#: no space after the stop, so they are never split.
_SENTENCE_END = re.compile(r"[.;!?](?=\s|$)")
#: A clause number, "4.2.1." or "2.1.": one or two digits a level, two to four levels, and its
#: trailing stop. It opens a clause - also in a run-on heading such as "... შესრულება 2.1. „...“",
#: where the line break before it was flattened away - and its own stop closes nothing. A date
#: ("30.04.2026.") or a cadastral code ("01.02.03.456.") has a longer group, and a decimal
#: ("2.5 ლარი") has no trailing stop, so neither is taken for one.
_CLAUSE_NUMBER = re.compile(r"(?<!\S)\d{1,2}(?:\.\d{1,2}){1,3}\.(?=\s)")
#: A list item number, "1.", which keeps its own item when it follows a colon or a clause end.
_ITEM_NUMBER = re.compile(r"(?<!\S)\d{1,2}\.(?=\s)")
#: A letter standing alone before a stop abbreviates a word: "ქ. თბილისი" (city), "2026 წ."
#: (year), an initial. "დღგ-ს." is not one: the letter there is the end of a word.
_ABBREVIATION = re.compile(r"(?<!\S)[^\W\d_]\.(?=\s)")


def _opens_a_clause(text: str, start: int) -> bool:
    """Whether ``start`` begins the text or follows a clause end or a colon, spaces aside."""
    i = start
    while i > 0 and text[i - 1].isspace():
        i -= 1
    return i == 0 or text[i - 1] in ".;:!?"


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every sentence in ``text``, in order, without surrounding whitespace.

    A sentence ends at ".", ";", "!" or "?" followed by whitespace, except the stop that belongs
    to a clause number ("4.2.1."), to a list item number after a colon or a clause end ("1."),
    or to a one-letter abbreviation ("ქ."). A clause number also starts a sentence of its own.
    """
    starts: set[int] = {0, len(text)}
    kept_stops: set[int] = set()
    for m in _CLAUSE_NUMBER.finditer(text):
        starts.add(m.start())
        kept_stops.add(m.end() - 1)
    for m in _ITEM_NUMBER.finditer(text):
        if _opens_a_clause(text, m.start()):
            kept_stops.add(m.end() - 1)
    for m in _ABBREVIATION.finditer(text):
        kept_stops.add(m.end() - 1)
    for m in _SENTENCE_END.finditer(text):
        if m.start() not in kept_stops:
            starts.add(m.end())

    spans: list[tuple[int, int]] = []
    for a, b in pairwise(sorted(starts)):
        piece = text[a:b]
        lead = len(piece) - len(piece.lstrip())
        end = len(piece.rstrip())
        if end > lead:
            spans.append((a + lead, a + end))
    return spans


def _sentence_index(spans: list[tuple[int, int]], pos: int) -> int | None:
    """The index of the sentence holding ``pos``, or None when ``pos`` falls between two."""
    i = bisect_right(spans, pos, key=lambda span: span[0]) - 1
    return i if i >= 0 and pos < spans[i][1] else None


def sentence_at(spans: list[tuple[int, int]], pos: int) -> tuple[int, int] | None:
    """The sentence of ``spans`` holding ``pos``, or None when ``pos`` falls between two."""
    i = _sentence_index(spans, pos)
    return spans[i] if i is not None else None


def _hits_between(hits: list[RuleHit], starts: list[int], a: int, b: int) -> list[RuleHit]:
    """The hits starting in ``[a, b)``; ``starts`` is their start offsets, in order."""
    return hits[bisect_left(starts, a) : bisect_left(starts, b)]


# --- the role an amount plays -------------------------------------------------------------------

AmountRole = Literal[
    "price", "advance", "remainder", "penalty", "guarantee", "tax", "equivalent"
]  # fmt: skip

#: An amount restated in another currency beside the real one: "100 ლარი (ეკვივალენტი 37 აშშ
#: დოლარი)". It is the same fee, not a second one, and not a price to compare.
_EQUIVALENT = re.compile(r"ეკვივალენტ|ექვივალენტ|equivalent|equiv\.", re.IGNORECASE)

#: What the words around an amount call it. At the same distance the longer keyword wins, so
#: "მეორე ტრანშ" (the second tranche) is a remainder although "ტრანშ" alone is an advance; a
#: remaining tie goes to the role listed first. The price is last: its stems are the shortest
#: and the most common, and a sentence about an advance mentions the price it is a share of.
#: A security is named by the noun, "უზრუნველყოფა": the verb "უზრუნველყოფს" (ensures) shares
#: its stem, and "the client ensures payment of 100 lari" states the fee.
_ROLE_KEYWORDS: dict[AmountRole, tuple[str, ...]] = {
    "advance": ("ავანს", "წინასწარ", "ტრანშ", "advance", "prepayment", "prepaid", "tranche"),
    "remainder": (
        "დარჩენ",
        "ნაშთ",
        "მეორე ნაწილ",
        "მეორე ტრანშ",
        "remaining",
        "remainder",
        "balance",
    ),
    "penalty": KEYWORDS[FactType.penalty],
    "guarantee": (
        "საგარანტიო",
        "გარანტი",
        "უზრუნველყოფა",
        "უზრუნველყოფის",
        "უზრუნველყოფად",
        "guarantee",
        "security deposit",
    ),
    "price": (*KEYWORDS[FactType.total_amount], *KEYWORDS[FactType.price]),
}

#: The tax named right before a figure makes the figure the tax: "დღგ 18 ლარი", "დღგ-ს თანხა
#: (18%): 18 ლარი", "VAT amount: 18.00 GEL", "საიდანაც დღგ შეადგენს 18 ლარს". Named anywhere
#: else in the sentence it is not: the price clause says "... 100 ლარს, რომელიც მოიცავს
#: გადასახადებს მათ შორის დღგ-ს", and that 100 is the price. Matched against the end of the
#: text before the figure.
_TAX_BEFORE = re.compile(
    r"(?<![^\W\d_])(?:დღგ|vat)(?:-?ის|-?ს)?"
    r"(?:\s+(?:თანხა|ოდენობა|amount))?"
    r"(?:\s*\(?\s*\d{1,2}(?:[.,]\d{1,2})?\s*%\s*\)?)?"
    r"(?P<verb>\s+(?:შეადგენს|არის|is|amounts\s+to))?"
    r"\s*[:=-]?\s*$",
    re.IGNORECASE,
)
#: A tax stated with a verb is the figure's only when the tax opens its clause: "..., საიდანაც
#: დღგ შეადგენს 18 ლარს", "; VAT is 18 GEL". After any other word the verb states the price the
#: tax goes with: "The fee plus VAT is 100 GEL", "საფასური, რომელსაც ემატება დღგ შეადგენს 100
#: ლარს". Matched against the end of the sentence before the tax.
_CLAUSE_OPENS = re.compile(
    r"(?:^|[,;:(]|საიდანაც|აქედან|მათ\s+შორის|of\s+which|from\s+which)\s*$", re.IGNORECASE
)
#: ... unless the tax is how the price is qualified: "Total incl. VAT 118 GEL", "Total with VAT:
#: 118 GEL", "Price before VAT: 118 GEL" and "Price including 18% VAT: 118 GEL" are prices.
_QUALIFIED_PRICE = re.compile(
    r"(?<![a-z])(?:incl\.?|including|inclusive\s+of|excl\.?|excluding|exclusive\s+of|without"
    r"|with|before|after|plus|ex\.?|net\s+of|\+)\s*(?:\d{1,2}(?:[.,]\d{1,2})?\s*%\s*)?$",
    re.IGNORECASE,
)
#: How far back from a figure its tax label may start, and from the label its qualifier.
_TAX_LABEL_REACH = 40
_QUALIFIER_REACH = 20
#: The tax named right after a figure makes the figure the tax too, when the tax closes the
#: clause: "მათ შორის 18 ლარი დღგ.", "18.00 GEL VAT,", "18 ლარი (დღგ)", "18 ლარი დღგ-ს სახით".
#: Words that go on after the tax say what a price includes, leaves out or adds - "100 ლარს
#: დღგ-ს ჩათვლით", "დღგ-ს დარიცხვით", "100 GEL VAT to be added" - or label the figure after it
#: ("100 ლარი დღგ: 18 ლარი"). No list of them is ever complete, and a price read as the tax
#: leaves the act with no fee at all, so only a tax that closes the clause counts. A comma or a
#: "+" between the figure and the tax adds the tax to a price, so neither is allowed there.
#: Matched at the end of the figure.
_TAX_AFTER = re.compile(
    r"\s*\(?\s*(?:დღგ|vat)(?:-?ის|-?ს)?(?![\w-])"
    r"(?:\s*\(\s*\d{1,2}(?:[.,]\d{1,2})?\s*%\s*\))?+"
    r"(?=\s*(?:[.,;)]|$|(?:სახით|ოდენობით)(?![ა-ჰ])))",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RoledAmount:
    """One amount and the role its sentence gives it; None when the sentence names none."""

    money: Money
    role: AmountRole | None


def _names_a_tax(text: str, sentence: tuple[int, int], span: tuple[int, int]) -> bool:
    """Whether a tax label stands right before the figure at ``span``, or right after it.

    Both are looked for inside the figure's sentence. The qualifier of a label before the
    figure ("incl.") is looked for across a sentence end too, because the splitter takes the
    stop of "incl." for one.
    """
    (sentence_start, sentence_end), (amount_start, amount_end) = sentence, span
    if _TAX_AFTER.match(text, amount_end, sentence_end):
        return True
    lead_start = max(sentence_start, amount_start - _TAX_LABEL_REACH)
    m = _TAX_BEFORE.search(text, lead_start, amount_start)
    if m is None:
        return False
    lead = text[sentence_start : m.start()][-_QUALIFIER_REACH:]
    if m.group("verb") and not _CLAUSE_OPENS.search(lead):
        return False
    return _QUALIFIED_PRICE.search(text, max(0, m.start() - _QUALIFIER_REACH), m.start()) is None


def amount_roles(text: str, *, sentences: list[tuple[int, int]] | None = None) -> list[RoledAmount]:
    """Every amount with a currency, in document order, with the role its sentence gives it.

    The role is the nearest keyword's (``nearest_keyword_group``, the fact classifier's own
    weighing), read inside the amount's sentence only, so a clause about the advance cannot
    lend its word to the price in the next one. A tax amount is the exception: only a tax named
    right before or right after the figure makes it one (``_TAX_BEFORE``, ``_TAX_AFTER``).
    Penalties are kept and named.
    """
    spans = sentence_spans(text) if sentences is None else sentences
    out: list[RoledAmount] = []
    for money in money_hits(text, penalties=True):
        start, end = money.span
        i = _sentence_index(spans, start)
        a, b = spans[i] if i is not None else (start, end)
        b = max(b, end)
        role: AmountRole | None
        opened = text.rfind("(", max(0, start - 40), start)
        if (
            opened >= 0
            and ")" not in text[opened:start]
            and _EQUIVALENT.search(text, opened, start)
        ):
            role = "equivalent"
        elif _names_a_tax(text, (a, b), (start, end)):
            role = "tax"
        else:
            role = nearest_keyword_group(text[a:b], start - a, end - a, _ROLE_KEYWORDS)
        out.append(RoledAmount(money=money, role=role))
    return out


#: How much text a fee's tax wording is looked for in, at most.
_FEE_SCOPE_CAP = 400


def fee_scope(
    sentences: list[tuple[int, int]], span: tuple[int, int], cap: int = _FEE_SCOPE_CAP
) -> tuple[int, int]:
    """Where the words qualifying a fee are written: its sentence and the next one.

    The next one because a table puts "NET" or "დღგ-ს ჩათვლით" in the cell after the figure, and
    a sentence break is all that is left of the cell border. At most ``cap`` characters, most of
    them after the figure, where the wording goes: a flattened table can be one long sentence.
    """
    start, end = span
    i = _sentence_index(sentences, start)
    if i is None:
        return start, end
    lo = sentences[i][0]
    hi = sentences[i + 1][1] if i + 1 < len(sentences) else sentences[i][1]
    hi = max(hi, end)
    if hi - lo > cap:
        lo = max(lo, min(start - cap // 4, hi - cap))
        hi = max(end, min(hi, lo + cap))
    return lo, hi


# --- tax wording --------------------------------------------------------------------------------

TaxKind = Literal["net", "inclusion", "exclusion", "mention"]

#: A Georgian tax word in any case: tax, tax-related, VAT (დღგ-ს, დღგ-ის, or spelled out),
#: income tax, pension (საპენსიო შენატანი). The tail is possessive: a shorter word must never
#: let a lookahead after it pass ("გადასახადებ|ის გარეშე" is still "without taxes").
_KA_TAX = (
    r"(?<![ა-ჰ])(?:დამატებული\s+ღირებულების\s+გადასახად|გადასახად|საგადასახად|დღგ"
    r"|საშემოსავლო|საპენსიო)[ა-ჰ-]*+"
)
_KA_WORD = r"[ა-ჰ][ა-ჰ-]*+"
#: "including" and "taking into account" - the instrumental only: "კანონმდებლობით
#: გათვალისწინებული გადასახადები" (the taxes the law provides for) includes nothing.
_KA_INCLUDING = r"(?:ჩათვლით|გათვალისწინებით)"
_KA_EXCLUDING = r"(?:გარეშე|გამოკლებით|ჩაუთვლელად)"
#: A tax word and up to three words after it, joined by spaces, a comma, "და" or "ან" - the
#: taxes a phrase names: "საშემოსავლო და საპენსიო გადასახადების", "საშემოსავლო გადასახადისა და
#: საპენსიო შენატანის". "და" (and) and "ან" (or) join the words and are not counted among them.
#: It starts at a tax word, so a date ("30 აპრილის ჩათვლით") never begins one, and it never runs
#: past "including" or "excluding": "გადასახადის გარეშე, სხვა ხარჯების ჩათვლით" excludes the
#: tax and includes the costs.
_KA_TAX_RUN = (
    rf"{_KA_TAX}(?:(?:,|\s+(?:და|ან))?\s+(?!{_KA_INCLUDING}|{_KA_EXCLUDING}){_KA_WORD}){{0,3}}?"
)
_EN_TAX = r"(?:income\s+tax(?:es)?|pension\s+contributions?|value\s+added\s+tax|taxes|tax|vat)"
#: "all applicable taxes", "income tax and pension contributions". Both runs are bounded: the
#: text is untrusted, and an unbounded one scans to the end of a page of "tax and tax and ..."
#: from every word of it, which is quadratic.
_EN_TAX_RUN = (
    rf"(?:(?:all|any|applicable|the)\s+){{0,3}}{_EN_TAX}"
    rf"(?:\s*(?:,|and|or|&)\s*(?:the\s+)?{_EN_TAX}){{0,4}}(?![a-z])"
)

#: A quoted name, as the documents write parties and projects: „…“, "…", «…».
_QUOTED_NAME = re.compile(r"[„“«\"][^„“”«»\"\n]{1,80}[“”»\"]")

#: NET as a word: "NET", "NET-ით", "ნეტო", "ნეტოს" - not "network", "Netherlands", "ნეტარი" or
#: the end of "ინტერნეტ", and not a part of a name or an address: "ASP.NET", "info@net.ge".
_NET = re.compile(
    r"(?<![\w.@/])(?:net(?:to)?(?![a-z])|ნეტ(?:ო[ა-ჰ]*)?(?![ა-ჰ]))(?!\.\w)", re.IGNORECASE
)
#: The words a price is called by when a tax is said to be in it: "თანხაში შედის დღგ", "ფასში",
#: "ღირებულებაში". Only these: "ძალაში შედის" (enters into force) includes nothing.
_KA_MONEY = r"(?:თანხ|ფას|ღირებულებ|საფასურ|ანაზღაურებ)[ა-ჰ]*"
#: The amount includes the tax: "გადასახადების ჩათვლით", "დღგ-ს გათვალისწინებით", "მოიცავს
#: გადასახადებს", "თანხა შეიცავს დღგ-ს", "მათ შორის დღგ-ს", "ითვალისწინებს გადასახადებს",
#: "თანხაში შედის დღგ", "დღგ შედის ღირებულებაში", ", ჩათვლით დღგ", "including all taxes", "VAT
#: included", "gross fee". Up to four words may stand between "მოიცავს" and the tax: "მოიცავს
#: საქართველოს კანონმდებლობით გათვალისწინებულ ყველა გადასახადს" is the contracts' boilerplate.
#: "ჩათვლით" before the tax counts only where no word stands before it: after one, it closes a
#: date ("30 აპრილის ჩათვლით დღგ-ს გადამხდელის ...").
_INCLUSION = re.compile(
    "|".join(
        (
            rf"{_KA_TAX_RUN}\s+{_KA_INCLUDING}",
            # not "ძალაში შედის" (enters into force)
            rf"(?<!არ )(?:მოიცავს|შეიცავს|ითვალისწინებს|(?<!ძალაში )შედის|მათ\s+შორის)"
            rf"(?:\s+{_KA_WORD}){{0,4}}?\s+{_KA_TAX}(?!\s+{_KA_EXCLUDING})",
            # "დღგ შედის ღირებულებაში", and the adjective: "დღგ-იანი ღირებულება"
            rf"{_KA_TAX}\s+შედის",
            r"(?<![ა-ჰ])(?:დღგ|გადასახად[ა-ჰ]*?)-?იან(?:ი|მა|ს|ის|ით|ად)?(?![ა-ჰ])",
            rf"(?<!not )(?<![a-z])(?:including|includes?|inclusive\s+of|incl\.?)\s+{_EN_TAX_RUN}",
            rf"(?<![a-z]){_EN_TAX_RUN}\s+(?:is\s+|are\s+)?(?:included|inclusive)(?![a-z])",
            # the act check's wordings: the price includes the tax, in either order
            rf"(?<![ა-ჰ])(?<!არ ){_KA_MONEY}\s+შედის\s+{_KA_TAX}",
            rf"{_KA_TAX}\s+შედის\s+{_KA_MONEY}",
            rf"(?<![ა-ჰ] )(?<![ა-ჰ]){_KA_INCLUDING}\s+{_KA_TAX}",
            r"(?<![a-z])gross\s+(?:amount|fee|remuneration|price|sum|value)(?![a-z])",
        )
    ),
    re.IGNORECASE,
)
#: The amount excludes the tax: "საშემოსავლო და საპენსიო გადასახადების გარეშე", "არ მოიცავს
#: დღგ-ს", "თანხაში არ შედის დღგ", "+ დღგ", "excluding VAT", "without taxes", "net of taxes",
#: "VAT not included".
_EXCLUSION = re.compile(
    "|".join(
        (
            rf"{_KA_TAX_RUN}\s+{_KA_EXCLUDING}",
            rf"არ\s+(?:მოიცავს|შეიცავს|ითვალისწინებს|შედის)(?:\s+{_KA_WORD}){{0,4}}?\s+{_KA_TAX}",
            rf"{_KA_TAX}\s+არ\s+შედის",
            # "+ დღგ", and with its rate: "+ 18% დღგ"
            r"\+\s*(?:\d{1,2}(?:[.,]\d{1,2})?\s*%\s*)?(?:დღგ|vat)(?![a-z])",
            rf"(?<![a-z])(?:excluding|excludes?|exclusive\s+of|excl\.?|without|net\s+of"
            rf"|not\s+including)\s+{_EN_TAX_RUN}",
            rf"(?<![a-z]){_EN_TAX_RUN}\s+(?:is\s+|are\s+)?(?:not\s+included|excluded|exclusive)"
            r"(?![a-z])",
            rf"(?<![ა-ჰ]){_KA_MONEY}\s+არ\s+შედის\s+{_KA_TAX}",
            rf"(?<![a-z])(?:does|do)\s+not\s+include\s+{_EN_TAX_RUN}",
        )
    ),
    re.IGNORECASE,
)
#: Any other word about taxes, which a foreign organisation's act must not contain.
_MENTION = re.compile(
    rf"{_KA_TAX}|(?<![a-z]){_EN_TAX}(?:ation)?(?![a-z])",
    re.IGNORECASE,
)
#: A tax number is a label, not a statement about taxes: "საგადასახადო ნომერი", "გადასახადის
#: გადამხდელის საიდენტიფიკაციო ნომერი", "დღგ-ს გადამხდელის ნომერი", "VAT No.", "VAT-ID",
#: "Tax ID", "TIN".
_TAX_ID_LABEL = re.compile(
    "|".join(
        (
            # not the Tax Code (საგადასახადო კოდექსი), which is a law, not a number
            r"საგადასახადო\s+(?:ნომ|კოდ(?!ექს)|იდენტიფიკ)[ა-ჰ]*",
            r"(?:გადასახადის|დღგ-?ს|დღგ-?ის)\s+გადამხდელის(?:\s+[ა-ჰ]+)?\s+(?:ნომ|კოდ)[ა-ჰ]*",
            r"(?<![a-z])(?:vat|tax)[\s-]*(?:reg(?:istration)?\.?[\s-]*)?"
            r"(?:no\b\.?|nr\b\.?|number|id\b|code\b|identification\s+(?:number|no\b\.?))",
            r"(?<![a-z])tin(?![a-z])",
        )
    ),
    re.IGNORECASE,
)
#: Which taxes a phrase names, for the rules that need a particular one.
_NAMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("income", re.compile(r"საშემოსავლო|income", re.IGNORECASE)),
    ("pension", re.compile(r"საპენსიო|pension", re.IGNORECASE)),
    ("vat", re.compile(r"დღგ|დამატებული\s+ღირებულების|vat|value\s+added", re.IGNORECASE)),
)


@dataclass(frozen=True, slots=True)
class TaxPhrase:
    """One piece of tax wording in a document, where it is, and which taxes it names."""

    kind: TaxKind
    span: tuple[int, int]
    text: str
    #: "income", "pension", "vat"; empty when the phrase speaks of taxes in general
    names: frozenset[str]


def _inside(span: tuple[int, int], others: list[tuple[int, int]]) -> bool:
    return any(a <= span[0] and span[1] <= b for a, b in others)


def tax_phrases(text: str) -> list[TaxPhrase]:
    """Every piece of tax wording in ``text``, in document order.

    NET, an inclusion, an exclusion, or - for any other tax word - a mention. A tax word inside
    an inclusion or an exclusion belongs to that phrase and is not also a mention, and a tax
    number ("VAT No.", "საგადასახადო ნომერი") is a label, not wording at all.
    """
    labels = [m.span() for m in _TAX_ID_LABEL.finditer(text)]
    # A company's name is quoted ("შპს „ნეტ ჯგუფი“"): a tax word in it is part of the name, and
    # a contractor called "Net" states no NET amount by being named.
    quoted = [m.span() for m in _QUOTED_NAME.finditer(text)]
    found: list[TaxPhrase] = []

    def add(kind: TaxKind, m: re.Match[str]) -> None:
        if _inside(m.span(), labels) or _inside(m.span(), quoted):
            return
        phrase = m.group(0)
        names = frozenset(name for name, pattern in _NAMES if pattern.search(phrase))
        found.append(TaxPhrase(kind=kind, span=m.span(), text=phrase, names=names))

    for m in _NET.finditer(text):
        add("net", m)
    for m in _INCLUSION.finditer(text):
        add("inclusion", m)
    for m in _EXCLUSION.finditer(text):
        add("exclusion", m)
    worded = [p.span for p in found if p.kind in ("inclusion", "exclusion")]
    for m in _MENTION.finditer(text):
        if not _inside(m.span(), worded):
            add("mention", m)
    return sorted(found, key=lambda p: (p.span, p.kind))


# --- the term -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TermRange:
    """The first and the last day a clause sets. Either is None when the clause leaves it open:
    "from signing until the obligations are fulfilled" has a first day and no last one."""

    start: date | None
    end: date | None


@dataclass(frozen=True, slots=True)
class ContractTerm:
    """When a contract was signed, how long it is in force, and when its work is to be done."""

    #: the date in the preamble, before the first party: "ქ. თბილისი, 1 მარტი, 2026 წელი"
    signed: date | None
    #: how long the contract is in force ("მოქმედებს ... -მდე"); None when no clause says
    validity: TermRange | None
    #: when the work is done ("შეასრულოს მომსახურება ... -დან ... ჩათვლით"); None when no
    #: clause says
    performance: TermRange | None


#: A clause on how long the contract is in force: the contract itself is named, then the verb -
#: "„ხელშეკრულება“ ძალაში შედის ... და მოქმედებს ...", "ხელშეკრულების მოქმედების ვადა", "The
#: Contract is valid until ...". A power of attorney, a price list or a duty is "in force until"
#: a day as well, and with the template's open-ended validity that day would become the
#: contract's last: "ფასები მოქმედებს 2026 წლის 31 მარტამდე" is no end to the contract.
_VALIDITY = re.compile(
    r"ხელშეკრულება(?![ა-ჰ])[^;]{0,60}?(?:მოქმედებ|ძალაშ)"
    r"|ხელშეკრულების\W{0,3}(?:მოქმედებ|ძალაშ)"
    r"|(?<![a-z])(?:contract|agreement)(?![a-z])[^;]{0,60}?(?<![a-z])(?:valid|in\s+force)",
    re.IGNORECASE,
)
#: A clause on when the work is to be done.
#: The clause that sets when the work is done: "შეასრულოს …", "გასწიოს მომსახურება …" (render),
#: "მომსახურება გაიწევა …" (will be rendered), "შესრულების / გაწევის ვადა".
_PERFORMANCE = re.compile(
    r"შეასრულოს|გასწიოს|გაიწევა|გაეწევა|(?:შესრულების|გაწევის)\s+(?:ვადა|პერიოდ)"
    r"|(?<![a-z])perform",
    re.IGNORECASE,
)
#: A guarantee's clause dates the guarantee, not the contract: "the bank guarantee is valid until
#: ...". A payment clause is left out the same way (``_PAYMENT``, below).
_NOT_A_TERM = re.compile(r"გარანტი|(?<![a-z])(?:guarantee|warrant)", re.IGNORECASE)
#: What makes a date a range's first day or its last. Georgian writes it in the date's own ending
#: - "1 მარტიდან" (from), "31 დეკემბრამდე" (until) - or after it: "01.03.2026-დან", "1
#: სექტემბრის ჩათვლით" (up to and including). English writes it before: "from", "until".
_FIRST_AFTER = re.compile(r"\s*-?\s*ი?დან(?![ა-ჰ])")
_LAST_AFTER = re.compile(r"\s*-?\s*ა?მდე(?![ა-ჰ])|\s*\(?\s*(?:ჩათვლით|inclusive)", re.IGNORECASE)
_FIRST_BEFORE = re.compile(
    r"(?<![a-z])(?:from|since|starting|commencing|beginning|effective)(?:\s+(?:on|from))?\s*$",
    re.IGNORECASE,
)
_LAST_BEFORE = re.compile(
    r"(?<![a-z])(?:to|until|till|through|by|up\s+to(?:\s+and\s+including)?|ending(?:\s+on)?)\s*$",
    re.IGNORECASE,
)
#: How far from a date the words that mark it may stand.
_MARK_REACH = 40
#: A validity clause whose one date is when the contract takes effect: "ძალაში შედის 2026 წლის 1
#: მარტს", "enters into force on 1 March 2026".
_TAKES_EFFECT = re.compile(r"ძალაში\s+შე(?:დის|ვა)|enters?\s+into\s+force|effective", re.IGNORECASE)
#: A lone date right after the word for a term is the term's last day: "მოქმედების ვადა:
#: 31.12.2026", "შესრულების ვადაა 2026 წლის 31 დეკემბერი", "expiry date: 31 December 2026".
_TERM_LABEL_BEFORE = re.compile(
    r"(?:ვადა[ა-ჰ]*|(?<![a-z])expir(?:y|ation)(?:\s+date)?)\s*[:\u2013\u2014-]?\s*$", re.IGNORECASE
)
#: ... and one that runs from the signing, with no date: "ხელმოწერის მომენტიდან", "from signing".
_FROM_SIGNING = re.compile(
    r"ხელმოწერ[ა-ჰ]*\s+(?:მომენტ|დღ|თარიღ)[ა-ჰ]*დან|ხელმოწერიდან|ხელმოწერისთანავე"
    r"|(?<![a-z])(?:from|upon|on)\s+(?:the\s+(?:date|day|moment)\s+of\s+)?(?:its\s+)?"
    r"sign(?:ing|ature)",
    re.IGNORECASE,
)


def _date_of(hit: RuleHit) -> date:
    return date.fromisoformat(str(hit.normalized))


def _range_in(
    text: str, sentence: tuple[int, int], hits: list[RuleHit], *, takes_effect: bool
) -> TermRange:
    """The range one clause's dates set, each date a first or a last day by how it is written.

    Two dates with no mark at all are a range in the order written ("01.03.2026 - 01.09.2026").
    A lone unmarked date is a first day when the clause says the contract takes effect then, and
    a last day when it follows the word for the term ("ვადა: 31.12.2026"). Otherwise it is no
    range at all: what else it might be is a guess, and a guessed end would call a correct act
    late - "გაფორმდა 2026 წლის 1 მარტს და მოქმედებს ..." is the signing, not the end.
    """
    a, b = sentence
    firsts: list[date] = []
    lasts: list[date] = []
    plain: list[RuleHit] = []
    for h in hits:
        before = text[max(a, h.start - _MARK_REACH) : h.start]
        after = text[h.end : min(b, h.end + _MARK_REACH)]
        if h.raw_text.endswith("დან") or _FIRST_AFTER.match(after) or _FIRST_BEFORE.search(before):
            firsts.append(_date_of(h))
        elif h.raw_text.endswith("მდე") or _LAST_AFTER.match(after) or _LAST_BEFORE.search(before):
            lasts.append(_date_of(h))
        else:
            plain.append(h)
    dates = [_date_of(h) for h in plain]
    if not firsts and not lasts and len(dates) >= 2 and dates[0] <= dates[-1]:
        return TermRange(start=dates[0], end=dates[-1])
    if takes_effect and dates and not firsts:
        firsts.append(dates[0])
    elif not firsts and not lasts and len(plain) == 1:
        lone = plain[0]
        if _TERM_LABEL_BEFORE.search(text, max(a, lone.start - _MARK_REACH), lone.start):
            lasts.append(dates[0])
    return TermRange(start=min(firsts, default=None), end=max(lasts, default=None))


def _merge(ranges: list[TermRange]) -> TermRange | None:
    """Clauses of one kind make one range, from the earliest first day to the latest last one."""
    if not ranges:
        return None
    return TermRange(
        start=min((r.start for r in ranges if r.start is not None), default=None),
        end=max((r.end for r in ranges if r.end is not None), default=None),
    )


def _signing_date(text: str, hits: list[RuleHit]) -> date | None:
    """The date the preamble states: the first one, if it comes before the first party or the
    first numbered clause (``anchors.preamble_end``).

    The fact classifier cannot tell it: with no keyword near it, ``rules._classify`` calls the
    preamble's date an execution period. Its place is what marks it. None when no date comes
    before the parties - never a guess, because a wrong signing date calls a correct act early.
    """
    end = preamble_end(text)
    if end is None or not hits or hits[0].end > end:
        return None
    return _date_of(hits[0])


def read_term(text: str, *, sentences: list[tuple[int, int]] | None = None) -> ContractTerm:
    """A contract's signing date, its validity and its performance window (client rule 10.5).

    Each window is read from the clauses that name it - "მოქმედებს" / "ძალაში" for validity,
    "შეასრულოს" / "შესრულების ვადა" for performance - from their dates alone, each marked a first
    or a last day by how it is written. A clause about paying or about a guarantee is never read:
    the day a fee is due is not the day the contract ends, and "თანხა მოიცავს დღგ-ს ჩათვლით" is
    no term at all. The client's template states its validity with no date - "ძალაში შედის მისი
    ხელმოწერის მომენტიდან და მოქმედებს ... შესრულებამდე" - which is a range from the signing
    with no end.
    """
    spans = sentence_spans(text) if sentences is None else sentences
    hits = find_dates(text)
    starts = [h.start for h in hits]
    signed = _signing_date(text, hits)
    validity: list[TermRange] = []
    performance: list[TermRange] = []
    for a, b in spans:
        performs = _PERFORMANCE.search(text, a, b)
        valid = _VALIDITY.search(text, a, b)
        if not (performs or valid):
            continue
        if _NOT_A_TERM.search(text, a, b) or _PAYMENT.search(text, a, b):
            continue
        inside = _hits_between(hits, starts, a, b)
        if performs:
            performance.append(_range_in(text, (a, b), inside, takes_effect=False))
            continue
        takes_effect = _TAKES_EFFECT.search(text, a, b) is not None
        found = _range_in(text, (a, b), inside, takes_effect=takes_effect)
        if found.start is None and _FROM_SIGNING.search(text, a, b):
            found = TermRange(start=signed, end=found.end)
        validity.append(found)
    return ContractTerm(signed=signed, validity=_merge(validity), performance=_merge(performance))


# --- payment terms ------------------------------------------------------------------------------

PaymentMethod = Literal["non_cash", "cash"]

#: A clause about paying the fee.
_PAYMENT = re.compile(
    r"გადაიხდ|გადახდ|ანგარიშსწორებ|ანაზღაურებ|(?<![a-z])(?:payments?|payable|paid|pay)(?![a-z])",
    re.IGNORECASE,
)
#: ... unless it is about a notice, a termination or a penalty. The template gives five days'
#: notice of termination as well as five days to pay: "შეწყვეტამდე 5 (ხუთი) კალენდარული დღით
#: ადრე შეტყობინების საფუძველზე" is no payment deadline.
_NOT_PAYMENT = re.compile(
    r"შეწყვეტ|შეტყობინებ|ჯარიმ|პირგასამტეხლო"
    r"|(?<![a-z])(?:terminat|notice|notif|penalt|liquidated\s+damages)",
    re.IGNORECASE,
)
#: A duration is a deadline when it is written as one: "5 (ხუთი) სამუშაო დღის ვადაში", "10 დღის
#: განმავლობაში", "10 დღეში", "არაუგვიანეს 5 დღისა", "within 30 days", "30 days after".
_DEADLINE_AFTER = re.compile(
    r"\s*(?:ვადა|(?P<during>განმავლობა))[ა-ჰ]*|\s+(?:after|(?P<of>of)|from|following)(?![a-z])",
    re.IGNORECASE,
)
_DEADLINE_BEFORE = re.compile(
    r"(?:არაუგვიანეს|(?<![a-z])(?:within|(?:no|not)\s+later\s+than))\s*$", re.IGNORECASE
)
#: "განმავლობაში" (during) and "of" also say how long the work ran - "(1 თვის განმავლობაში)
#: გაწეული მომსახურება", "6 months of service" - so a span longer than these is a time limit only
#: when it is counted from something: "ხელშეკრულების გაფორმებიდან 1 თვის განმავლობაში".
_SHORT_UNITS = frozenset({"day", "week"})
_COUNTED_FROM = re.compile(r"(?:[ა-ჰ]დან|(?<![a-z])(?:from|after|following))\s*$", re.IGNORECASE)
#: Which days a deadline counts. Business and banking days are working days, whichever word a
#: document uses for them.
_QUALIFIERS = {
    "working": "working",
    "business": "working",
    "banking": "working",
    "calendar": "calendar",
}
#: How the fee is paid: by a transfer to an account, or in cash. "უნაღდო" (non-cash) holds the
#: letters of "ნაღდ" (cash), hence the letter before it.
_NON_CASH = re.compile(
    r"უნაღდო|გადარიცხ|საბანკო\s+ანგარიშ"
    r"|(?<![a-z])(?:bank\s+(?:transfer|account)|wire\s+transfer|non-cash|cashless|transferred)",
    re.IGNORECASE,
)
_CASH = re.compile(r"(?<![ა-ჰ])ნაღდ|(?<![\w-])cash(?![\w-])", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Deadline:
    """A time limit for paying, as a document states it."""

    count: int
    #: "day", "week", "month" or "year"
    unit: str
    #: "working" or "calendar"; None when the document does not say which days count
    qualifier: str | None
    #: in English, for the messages: "5 working days"
    value: str
    #: the document's own words: "5 (ხუთი) სამუშაო დღის"
    raw: str
    span: tuple[int, int]


@dataclass(frozen=True, slots=True)
class MethodHit:
    """How a document says the fee is paid, and its words for it."""

    method: PaymentMethod
    span: tuple[int, int]
    text: str


@dataclass(frozen=True, slots=True)
class PaymentTerms:
    """The deadline and the method a document states for paying (client rule 10.7). The advance
    is read on its own (``read_advance``): the invoice check needs it as well."""

    #: every time limit for paying, in document order
    deadlines: tuple[Deadline, ...]
    #: None when the document names no method, or names both
    method: MethodHit | None

    @property
    def stated(self) -> bool:
        return bool(self.deadlines) or self.method is not None


def _whole_words(text: str, span: tuple[int, int]) -> tuple[int, int]:
    """``span`` widened to the end of its last word: "გადარიცხ" is written "გადარიცხვით"."""
    start, end = span
    while end < len(text) and text[end].isalpha():
        end += 1
    return start, end


def _paying_sentences(text: str, spans: list[tuple[int, int]]) -> list[int]:
    """The indexes of the sentences about paying."""
    return [
        i
        for i, (a, b) in enumerate(spans)
        if _PAYMENT.search(text, a, b) and not _NOT_PAYMENT.search(text, a, b)
    ]


def _deadline(hit: RuleHit) -> Deadline:
    """A deadline out of a duration hit: its ``value`` reads "5 working days" or "5 days"."""
    words = hit.value.split()
    qualifier = _QUALIFIERS.get(words[1].lower()) if len(words) == 3 else None
    return Deadline(
        count=int(words[0]),
        unit=hit.unit or "",
        qualifier=qualifier,
        value=hit.value,
        raw=hit.raw_text,
        span=(hit.start, hit.end),
    )


def _is_deadline(text: str, hit: RuleHit) -> bool:
    lead = max(0, hit.start - _MARK_REACH)
    if hit.raw_text.endswith("ში") or _DEADLINE_BEFORE.search(text, lead, hit.start):
        return True
    after = _DEADLINE_AFTER.match(text, hit.end)
    if after is None:
        return False
    if (after.group("during") or after.group("of")) and hit.unit not in _SHORT_UNITS:
        return _COUNTED_FROM.search(text, lead, hit.start) is not None
    return True


def _method_hit(method: PaymentMethod, m: re.Match[str], text: str) -> MethodHit:
    span = _whole_words(text, m.span())
    return MethodHit(method=method, span=span, text=text[span[0] : span[1]])


def read_payment_terms(
    text: str, *, sentences: list[tuple[int, int]] | None = None
) -> PaymentTerms:
    """The payment deadline and the payment method a document states (client rule 10.7).

    Read only from the sentences about paying, and never from a notice, a termination or a
    penalty clause, however alike they read (``_NOT_PAYMENT``). A duration counts as a deadline
    when it is written as one - "... დღის ვადაში", "within ..." - so "paid monthly over 6 months"
    is not taken for one. The method is non-cash (a transfer to an account) or cash; a document
    that names both has said neither.
    """
    spans = sentence_spans(text) if sentences is None else sentences
    paying = _paying_sentences(text, spans)
    in_paying = set(paying)
    deadlines = tuple(
        _deadline(h)
        for h in sorted(find_durations(text), key=lambda h: h.start)
        if _sentence_index(spans, h.start) in in_paying and _is_deadline(text, h)
    )
    non_cash = [m for i in paying for m in _NON_CASH.finditer(text, *spans[i])]
    cash = [m for i in paying for m in _CASH.finditer(text, *spans[i])]
    method = None
    if non_cash and not cash:
        method = _method_hit("non_cash", non_cash[0], text)
    elif cash and not non_cash:
        method = _method_hit("cash", cash[0], text)
    return PaymentTerms(deadlines=deadlines, method=method)


# --- the advance --------------------------------------------------------------------------------

#: An advance, a prepayment, a tranche. "წინასწარ" (beforehand) only as a payment: "წინასწარი
#: შეტყობინება" is a prior notice. And "in advance" says when, not what.
_ADVANCE = re.compile(
    r"ავანს|ტრანშ|წინასწარი?\s+(?:გადახდ|გადაიხდ|ანგარიშსწორებ|ჩარიცხ|თანხ)"
    r"|(?<![a-z])(?<!in )(?:advance|prepayment|pre-payment|prepaid|tranche)(?![a-z])",
    re.IGNORECASE,
)
#: An advance ruled out in so many words: "ავანსის გარეშე", "without an advance", "no advance";
#: or a label with nothing after it but a no: "ავანსი: არა", "ავანსი - არ არის", "Advance: none",
#: "ავანსი: 0". Only at the clause's end: "ავანსი: არა უმეტეს 30%-ისა" says how much.
_NO_ADVANCE = re.compile(
    r"(?:ავანს|ტრანშ)[ა-ჰ]*\s+გარეშე"
    r"|(?<![a-z])(?:without|no)\s+(?:an?\s+|any\s+)?(?:advance|prepayment)"
    r"|(?:ავანს[ა-ჰ]*|(?<![a-z])(?:advance(?:\s+payment)?|prepayment))\s*[:\u2013\u2014-]\s*"
    r"(?:არა|არ\s+არის|none|no|nil|0(?:[.,]0{1,2})?)\s*(?:[.!]\s*)?$",
    re.IGNORECASE,
)
#: ... or denied as paid, given or provided for: "ავანსი არ გაიცემა", "ავანსის გადახდა
#: გათვალისწინებული არ არის", "ავანსი არ მიუღია". A denial of anything else leaves the advance
#: standing: "ავანსის დაბრუნება არ მოხდება" (the advance is not returned) has one. "მიღება"
#: (receipt) is not among them: the act's own name, "მიღება-ჩაბარების", holds it.
_GIVEN = re.compile(
    r"გაიცემ|გაცემ|გადაიხდ|გადახდ|გათვალისწინ|ხორციელ|მიუღ|მიიღო|ყოფილ|გადაცემ|ჩაირიცხ|ჩარიცხ"
    r"|(?<![a-z])(?:provided|paid|payable|made|given|foreseen|received)(?![a-z])",
    re.IGNORECASE,
)
_WORD = re.compile(r"\w+")
_DIGIT = re.compile(r"\d")
#: A clause inside a sentence: "ავანსი, რომელიც არ აღემატება 30%-ს" denies nothing of the advance.
_CLAUSE_BREAK = re.compile(r"[,;]")
#: What a percentage in an advance's sentence may be the share of. The price is not among them:
#: "ავანსი შეადგენს ღირებულების 30%-ს" is the advance's share of the price.
_SHARE_GROUPS: dict[str, tuple[str, ...]] = {
    "advance": _ROLE_KEYWORDS["advance"],
    "remainder": _ROLE_KEYWORDS["remainder"],
    "penalty": _ROLE_KEYWORDS["penalty"],
    "tax": ("დღგ", "vat", "გადასახად", "tax"),
}


@dataclass(frozen=True, slots=True)
class AdvancePayment:
    """An advance amount a document states, and - in an act - the day it was paid."""

    money: Money
    #: the date written beside it; always None in a contract, which plans a payment and never
    #: records one
    paid_on: date | None


@dataclass(frozen=True, slots=True)
class AdvanceShare:
    """An advance stated as a share of the price: "ავანსი 30%"."""

    percent: float
    span: tuple[int, int]
    raw: str


@dataclass(frozen=True, slots=True)
class AdvanceTerms:
    """What a document says about an advance: nothing, that there is none, or how much."""

    #: where it first speaks of an advance; None when it never does
    span: tuple[int, int] | None
    #: it rules an advance out: "ავანსი არ გაიცემა", "no advance"
    excluded: bool
    payments: tuple[AdvancePayment, ...]
    shares: tuple[AdvanceShare, ...]

    @property
    def mentioned(self) -> bool:
        return self.span is not None

    @property
    def stated(self) -> bool:
        """It says an advance is paid, or is to be: an amount, a share or just the word -
        unless all it says of one is that there is none."""
        return bool(self.payments or self.shares) or (self.mentioned and not self.excluded)

    @property
    def anchor(self) -> tuple[int, int] | None:
        """Where a finding about it is marked: its amount, else its share, else the word."""
        if self.payments:
            return self.payments[0].money.span
        if self.shares:
            return self.shares[0].span
        return self.span


def _rules_out(clause: str) -> bool:
    """Whether a clause about the advance says there is none. A clause with a figure in it is
    about how much ("ავანსი 30%-ზე მეტი არ გაიცემა"), not whether; its own number ("4.2.2.")
    is no figure."""
    if _NO_ADVANCE.search(clause):
        return True
    negated = any(w.lower() in _NEGATIONS for w in _WORD.findall(clause))
    figures = _DIGIT.search(_ITEM_NUMBER.sub("", _CLAUSE_NUMBER.sub("", clause)))
    return negated and _GIVEN.search(clause) is not None and figures is None


def _paid_on(
    money: Money, spans: list[tuple[int, int]], dates: list[RuleHit], starts: list[int]
) -> date | None:
    """The date nearest the amount inside its sentence: the day an act says it was paid."""
    sentence = sentence_at(spans, money.span[0])
    if sentence is None:
        return None
    start, end = money.span
    near = _hits_between(dates, starts, *sentence)
    if not near:
        return None
    return _date_of(min(near, key=lambda h: max(h.start - end, start - h.end)))


def read_advance(
    text: str,
    *,
    in_act: bool,
    sentences: list[tuple[int, int]] | None = None,
    amounts: list[RoledAmount] | None = None,
) -> AdvanceTerms:
    """What a document says about an advance (client rules 9 and 10.7): the invoice check and
    the payment-terms check read it here, so the two cannot disagree about it.

    Its amounts are the ones ``amount_roles`` calls an advance; its share is a percentage whose
    nearest keyword is the advance's. In an act each amount is dated by the nearest date in its
    sentence, because an act records a payment: "ავანსი 1 000 აშშ დოლარი გადახდილია 2026 წლის 10
    სექტემბერს". A contract only plans one, and the date in its advance clause is a deadline
    ("... ხელმოწერიდან 5 დღის ვადაში"), so with ``in_act`` false nothing is dated.
    """
    spans = sentence_spans(text) if sentences is None else sentences
    roled = amount_roles(text, sentences=spans) if amounts is None else amounts
    advances = [r.money for r in roled if r.role == "advance"]
    keyword = _ADVANCE.search(text)
    mentions = ([keyword.span()] if keyword else []) + [m.span for m in advances[:1]]
    if not mentions:
        return AdvanceTerms(span=None, excluded=False, payments=(), shares=())
    dates = find_dates(text) if in_act else []
    date_starts = [h.start for h in dates]
    # an advance of nothing - "ავანსი: 0 ლარი" - is the document saying there was none
    paid = [m for m in advances if m.value > 0]
    payments = tuple(
        AdvancePayment(money=m, paid_on=_paid_on(m, spans, dates, date_starts)) for m in paid
    )
    shares: list[AdvanceShare] = []
    excluded = bool(advances) and not paid
    for a, b in spans:
        sentence = text[a:b]
        if not _ADVANCE.search(sentence):
            continue
        for p in find_percentages(sentence):
            if nearest_keyword_group(sentence, p.start, p.end, _SHARE_GROUPS) == "advance":
                shares.append(
                    AdvanceShare(
                        percent=percent_of(p) or 0.0,
                        span=(a + p.start, a + p.end),
                        raw=p.raw_text,
                    )
                )
        clauses = _CLAUSE_BREAK.split(sentence)
        excluded |= any(_rules_out(c) for c in clauses if _ADVANCE.search(c))
    return AdvanceTerms(
        span=min(mentions), excluded=excluded, payments=payments, shares=tuple(shares)
    )
