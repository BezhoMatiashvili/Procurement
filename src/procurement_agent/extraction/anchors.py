"""Label-anchored extraction: read the fields these documents put behind fixed labels.

Georgian procurement contracts and acceptance acts are rigidly templated. A company name is
always inside „…“ after a legal form; an identification code always follows
"საიდენტიფიკაციო კოდი:"; an address always follows "იურიდიული მისამართი:"; a signatory always
follows "დირექტორი". Reading those directly is exact, instant and explainable, where asking a
4B model on CPU costs minutes per document and mislabels fields.

This layer therefore runs *before* the LLM, and the LLM is only asked for what it leaves empty.
Anchors that do not fire simply yield nothing — they never block the fallback — and the
evaluation harness reports anchor hit-rate per field so that a new document family that does
not match this template shows up as a measurable drop rather than as silent wrong answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from procurement_agent.extraction.normalize import (
    ABBREVIATED_LEGAL_FORMS,
    ENTREPRENEUR,
    FUNCTION_WORDS,
    IBAN_CURRENCY_SUFFIXES,
    ID_CODE_RE,
    ID_CODE_WRITTEN_RE,
    ID_LABEL,
    LEGAL_FORM,
    LEGAL_FORMS,
    QUOTE_CHARS,
    bic_has_country,
    iban_is_valid,
    iban_length,
    id_code_digits,
    legal_form_abbreviation,
    name_stem,
    names_match,
    normalize_confusables,
    normalize_name,
    normalize_spaces,
    strip_quotes,
    unlabelled_bic_plausible,
)
from procurement_agent.extraction.rules import CADASTRAL_RE
from procurement_agent.extraction.schemas import FactType

_Q = re.escape(QUOTE_CHARS)
_QUOTED = rf"[{_Q}]([^{_Q}]{{2,90}}?)[{_Q}]"

# Georgian legal forms. "ი/მ" and "ინდ. მეწარმე" matter because the client's tax-wording rules
# branch on whether the contractor is a company, an individual entrepreneur or a person.
# A form is also written out in full, in any case ("შეზღუდული პასუხისმგებლობის საზოგადოება
# „…“" is "შპს „…“", red team R3, AOA-R3-04), or abbreviated with dots or a bracketed letter
# ("შ.პ.ს.", "ა(ა)იპ", red team R4, AOA-R4-02), and a party name carries the abbreviation
# (``_legal_form``). ``normalize.LEGAL_FORMS`` is the one list every reader is built from.
_ENTREPRENEUR = ENTREPRENEUR
_LEGAL_FORM = LEGAL_FORM

_PARTY_NAME = re.compile(rf"(?<![ა-ჰ])({_LEGAL_FORM})\s*{_QUOTED}")


def _legal_form(written: str) -> str:
    """The legal form as a party name carries it: abbreviated, "ი / მ" as "ი/მ"."""
    return legal_form_abbreviation(written) or re.sub(r"\s*/\s*", "/", written)


#: An individual entrepreneur or a natural person is named, not quoted: "ი/მ ირაკლი სულაძე",
#: "ფ/პ ანა ბერიძე" - a first name and a surname after the abbreviation.
_PERSON_PARTY = re.compile(
    rf"(?<![ა-ჰ])(ი\s*/\s*მ|ფ\s*/\s*პ|{_ENTREPRENEUR}ე)\s+"
    r"([ა-ჰ]{2,20}\s+[ა-ჰ]{2,25})(?![ა-ჰ])"
)
# The code behind any of its labels (``normalize.ID_LABEL``: "ს/კ", "ს.კ.", "საიდ. კოდი", "ID",
# ...), compact or a company's nine digits printed in threes ("406 444 789"); its value is the
# digits alone. The rules and the act check read codes with the same pattern. A party's code
# is read as written, a slip's wrong length included, for the act check to compare
# (``ID_CODE_WRITTEN_RE``, red team R3, AOA-R3-01); a code fact is 9 or 11 digits.
_ID_CODE = ID_CODE_RE
_PARTY_CODE = ID_CODE_WRITTEN_RE
_code_digits = id_code_digits


#: each legal form before a quoted name, and its abbreviation
_FORM_BEFORE_NAME = tuple(
    (re.compile(rf"(?<![ა-ჰ])(?:{pattern})(?=\s*[{_Q}])"), short) for pattern, short in LEGAL_FORMS
)
_HONORIFIC_WORD = re.compile(
    r"(?<![ა-ჰ])(?:(?:ქალ)?ბატონ(?:ი|ის|მა)?|[ბქ]\s?-\s?ნ(?:ი|ის|მა)?\.?)\s+(?=[ა-ჰ])"
)


def plain_party_wording(text: str) -> str:
    """``text`` with each legal form before a quoted name abbreviated and each courtesy title
    before a name dropped: "შეზღუდული პასუხისმგებლობის საზოგადოება „ა“", "შ.პ.ს. „ა“" and "შპს
    „ა“", "ქალბატონ ნინო" and "ნინო" name one party and one person. For a comparison that must
    not report the other spelling as a change."""
    for pattern, short in _FORM_BEFORE_NAME:
        text = pattern.sub(short, text)
    return _HONORIFIC_WORD.sub("", text)


ROLE_WORDS = frozenset(
    {
        "შემკვეთი",
        "დამკვეთი",
        "შემსრულებელი",
        "მიმწოდებელი",
        "მყიდველი",
        "გამყიდველი",
        "მხარე",
        "მხარეები",
    }
)
#: The role words that put a party on a side - the one who pays, the one paid. "მხარე" (a party)
#: names neither.
_SIDE_WORDS = ROLE_WORDS - {"მხარე", "მხარეები"}
# the same words in the genitive, which is how they appear before a noun
_ROLE_WORDS_INFLECTED = frozenset(
    {w for r in ROLE_WORDS for w in (r, r[:-1] + "ის", r + "ს", r[:-1] + "მა")}
)

# An address ends at ";", "(" or ")", at the end of the text, or where the next field starts.
# The next field is recognised four ways, because the address has to stop in text that still
# has its line breaks (the pipeline) and in text that has been flattened to one line (the AOA
# checker hands the anchors normalised text):
#   * a line break followed by a label ("ტელ: ...", "ელ-ფოსტა: ..."), a numbered clause
#     ("4.1. ...") or a new party ("შპს „...“") - a plain line break is a wrapped address line;
#   * an inline contact label, "ტელ:" / "ელ. ფოსტა:" / "e-mail:", written on the same line;
#   * another requisite's label - the code, the account, the bank - on the same line or the
#     next, with its colon or without: "..., ს/კ: 206...", "\nა/ა GE...";
#   * a bare account or code on the next line.
# "ტელ" needs its colon or full stop: "ქ. ტელავი" is a city, not a telephone number.
_CONTACT_LABEL = r"(?:ტელ(?:ეფონი)?|ელ\.?\s?-?\s?ფოსტა|მობ(?:ილური)?|ფაქსი|e-?mail|tel|phone|fax)"
_REQUISITE_LABEL = (
    rf"(?:{ID_LABEL}|"
    r"ა\s*/\s*ა|ანგარიშ(?:ი|ის\s*ნომერი)(?![ა-ჰ])|ბანკი(?![ა-ჰ])|ბანკის\s*კოდი|"
    r"IBAN|BIC|SWIFT)(?![A-Za-z])"
)
_NEXT_LINE_FIELD = (
    # a clause number needs its full stop ("4.2. "): a bare "12 " is a wrapped house number
    r"\n\s*(?:\d+(?:\.\d+)*\.\s|(?:[^\s:;]{1,25}\s?){1,3}[:\uff1a]|"
    rf"(?:{_LEGAL_FORM})\s*[{_Q}]|[A-Z]{{2}}\d{{2}}[A-Z0-9]{{4}}|(?:\d{{9}}|\d{{11}})(?!\d))"
)
#: The end of a table cell as Docling writes a table row by row: "<left cell>, <column header> =
#: <right cell>". Whatever follows is another column's - another party's address, say.
_CELL_BOUNDARY = r",\s*[^,=\n]{1,40}?\s=\s"
#: Another party starts on the same line: its side ("„შემსრულებელი“") or its name ("შპს „...“").
#: A table written column by column puts the next column's header right after an address.
_NEXT_PARTY = (
    rf"[\s,]+(?:[{_Q}](?:{'|'.join(sorted(_SIDE_WORDS))})[{_Q}]|(?:{_LEGAL_FORM})\s*[{_Q}])"
)
#: An address's label: "მისამართი:", "იურიდიული მისამართი:". Another one after an address ends
#: it - the next column's address where a requisites table's cells are joined into one line
#: (red team R4, COMPARE-R4-08).
_ADDRESS_LABEL = r"(?:იურიდიული\s*)?მისამართი\s*[:\uff1a]"
#: Who represents the party, after its address: "..., წარმოდგენილი მისი დირექტორის X-ის სახით",
#: "..., რომელსაც წარმოადგენს დირექტორი X", "..., დირექტორი: X" (red team R4, COMPARE-R4-04).
#: A head's title ends an address only with a name after it, so a street named after an office
#: ("პრეზიდენტის გამზ. N5") stays in it.
_REPRESENTED_BY = (
    r",\s*(?:(?:წარმოდგენილ(?:ი|ია)|წარმომადგენელ(?:ი|ია)|რომელ(?:საც|იც)\s+წარმოადგენს"
    r"|რომლის\s+სახელით)(?![ა-ჰ])"
    r"|(?:მისი\s+)?(?:(?:გენერალური|აღმასრულებელი)\s+)?"
    r"(?:დირექტორ|რექტორ|პრეზიდენტ|ხელმძღვანელ|მმართველ)(?:ი|ის|მა)?(?![ა-ჰ])"
    r"\s*[:,]?\s*(?:[ა-ჰ]\.\s?)?[ა-ჰ]{2,20}\s+[ა-ჰ]{2,25}(?![ა-ჰ.]))"
)
_ADDRESS = re.compile(
    rf"{_ADDRESS_LABEL}\s*([^;()]{{4,140}}?)\s*"
    rf"(?=[;()]|{_NEXT_LINE_FIELD}|[\s,]+{_CONTACT_LABEL}\s*[.:\uff1a]|"
    rf"[\s,]+{_REQUISITE_LABEL}|{_CELL_BOUNDARY}|{_NEXT_PARTY}|{_REPRESENTED_BY}|"
    rf"[\s,]+{_ADDRESS_LABEL}|\s*$)",
    re.IGNORECASE,
)
#: "ბატონი", "ქალბატონ", "ბ-ნ", "ქ-ნი": a courtesy title before a name, not part of it
_HONORIFIC = r"(?:(?:ქალ)?ბატონ(?:ი|ის|მა)?\s+|[ბქ]\s?-\s?ნ(?:ი|ის|მა)?\.?\s*)?"
#: a first name and a surname (and words after them the reader may cut: a patronymic between
#: the two, see ``PATRONYMIC``), or an initial and a surname
_PERSON = r"[ა-ჰ]\.\s?[ა-ჰ]{2,25}(?![ა-ჰ])|[ა-ჰ]{2,20}(?:\s+[ა-ჰ]{2,25}){1,3}"
#: A patronymic between a first name and a surname: the father's name in the genitive and "ძე"
#: (son) or "ასული" (daughter) - "ნოდარ გიორგის ძე ბარამიძე". No part of the name compared:
#: ``party_director`` is the first name and the surname (red team R3, AOA-R3-06).
PATRONYMIC = re.compile(r"[ა-ჰ]{2,20}ს\s+(?:ძე|ძის|ძემ|ასული|ასულის|ასულმა)")
# "დირექტორი ნინო კაპანაძე", "დირექტორი: დავით მელაძე", "დირექტორი: დ. მელაძე",
# "წარმოდგენილი გენერალური დირექტორის, დავით მელაძის სახით"
# The label must end its word ("დირექტორთან შეთანხმებით" is "in agreement with the director",
# no name), and a deputy or an acting director is not the director.
#
# A party's head signs under other titles too - a public body's chairman or head, a university's
# rector (red team R2, AOA-R2-06): "წარმოდგენილი თავმჯდომარის, მარინე ნიმუშაძის სახით",
# "ხელმძღვანელი: ...", "რექტორი ...", "პრეზიდენტი", "მმართველი" (a managing partner). These titles
# are words of their own ("პრორექტორი", "ვიცე-პრეზიდენტი" are not the head) and are read only
# where no other body's name stands before them in the genitive (``_TITLE_OF_OTHER``):
# "პროექტის ხელმძღვანელი" is the project's manager, not the party's head.
_OTHER_HEAD = (
    r"(?<![ა-ჰ\-])(?:(?:რექტორ|პრეზიდენტ|ხელმძღვანელ|მმართველ)(?:ი|ის|მა)?"
    r"|თავმჯდომარ(?:ე|ის|ემ))"
)
#: Between a signatory's title and the name: a colon or a comma, a signature's blank line of
#: underscores or dots ("დირექტორი ______ /ნინო კაპანაძე/"), and a slash or a bracket the name
#: is written in (red team R4, AOA-R4-04). The name's place says it is a name when the mark
#: closes after it (``_read_signatory``).
_TITLE_TO_NAME = r"\s*(?P<sep>[:,]?)\s*(?:(?:_{2,}|\.{3,}|…+)\s*)?(?P<mark>[/(]\s*)?"
_DIRECTOR = re.compile(
    rf"(?:(?:გენერალური|აღმასრულებელი)\s+)?(?:დირექტორ(?:ი|ის|მა)?|{_OTHER_HEAD})(?![ა-ჰ])"
    rf"{_TITLE_TO_NAME}"
    rf"{_HONORIFIC}(?!მოადგილ|მოვალეობ|შემსრულებ)(?P<name>{_PERSON})"
)
#: A head's title in the nominative, as a signature writes it after the name.
_HEAD_NOMINATIVE = (
    r"(?:(?:გენერალური|აღმასრულებელი)\s+)?"
    r"(?:დირექტორი|რექტორი|პრეზიდენტი|ხელმძღვანელი|მმართველი|თავმჯდომარე)"
)
#: A signatory written before the head's title (red team R4, AOA-R4-04): "ნინო კაპანაძე -
#: დირექტორი", "ნინო კაპანაძე (დირექტორი)". The place says it is a name, and is required whole:
#: the name opens its line or cell (after a line break, a ";", a ":" or a quote - a party's name
#: or side right above it), and the title closes it (a line break, the end, a ";", a quote, a
#: signature's blank or slash). A first name and a surname, a patronymic between them, or an
#: initial and a surname. A deputy ("- დირექტორის მოადგილე") or a narrowed title ("-
#: ფინანსური დირექტორი") is no head's title here: the title must follow the dash or bracket.
_NAME_BEFORE_TITLE = re.compile(
    rf"(?:^|(?<=[\n;:{_Q}]))[ \t]*{_HONORIFIC}"
    r"(?P<name>[ა-ჰ]\.\s?[ა-ჰ]{2,25}|[ა-ჰ]{2,20}(?:[ \t]+[ა-ჰ]{2,20}ს[ \t]+(?:ძე|ასული))?"
    r"[ \t]+[ა-ჰ]{2,25})"
    rf"[ \t]*(?:[-\u2013\u2014][ \t]*{_HEAD_NOMINATIVE}|\([ \t]*{_HEAD_NOMINATIVE}[ \t]*\))"
    rf"(?=[ \t]*(?:$|\n|[;{_Q}_/]))",
    re.MULTILINE,
)
#: A head's title narrowed by a function is not the party's head (red team R3, AOA-R3-05): a
#: financial, commercial, technical or operations director ("ფინანსური დირექტორი", "საოპერაციო
#: დირექტორი") co-signs beside the head. The adjectives are named, not guessed from an ending: a
#: surname ends in "-ური" too ("ჩიტაური"), and in an act flattened to one line a signatory's
#: surname stands right before the next head's title; so does one in "სა-...-ო" ("სანტორო"),
#: which is why the "სა-...-ო" adjectives are named as well. "გენერალური" and "აღმასრულებელი"
#: name the head (``_DIRECTOR``).
_FUNCTION_BEFORE_TITLE = re.compile(
    r"(?<![ა-ჰ])(?:(?:ფინანსურ|კომერციულ|ტექნიკურ|იურიდიულ|ადმინისტრაციულ|ოპერაციულ|რეგიონულ"
    r"|სტრატეგიულ)(?:ი|მა)?|სა(?:ოპერაციო|ფინანსო|ინჟინრო|მშენებლო|წარმოო|მეცნიერო|სწავლო"
    r"|მედიცინო|ინვესტიციო|რეკლამო|ინფორმაციო|ვაჭრო|მარკეტინგო|ლოგისტიკო))\s+$"
)
#: The head's title in the genitive, as a deputy's title names it: "დირექტორის მოადგილე".
_HEAD_GENITIVE = (
    r"(?:გენერალური\s+|აღმასრულებელი\s+)?(?:დირექტორის|რექტორის|პრეზიდენტის|"
    r"ხელმძღვანელის|მმართველის|თავმჯდომარის)"
)
# "დირექტორის მოადგილე: ...", "თავმჯდომარის პირველი მოადგილის, ... სახით", "დირექტორის
# მოვალეობის შემსრულებელი ...": a deputy or an acting head signs in the head's place. Not the
# head: the act check compares a deputy only with the deputy the contract names.
_DEPUTY = re.compile(
    rf"(?<![ა-ჰ\-])(?:{_HEAD_GENITIVE}\s+)?(?:პირველი\s+)?"
    r"(?:მოადგილ(?:ე|ის|ემ)|მოვალეობის\s+შემსრულებ(?:ელი|ლის|ელმა))(?![ა-ჰ])"
    rf"{_TITLE_TO_NAME}{_HONORIFIC}(?P<name>{_PERSON})"
)
#: A genitive before a title that makes it another body's officer, unless it is a party's own
#: board or a side ("სამეთვალყურეო საბჭოს თავმჯდომარე", "შემსრულებლის ხელმძღვანელი").
_TITLE_OF_OTHER = re.compile(r"(?<![ა-ჰ])([ა-ჰ]{2,}(?:ის|ს))\s+$")
_OWN_BODY_GENITIVES = frozenset(
    {
        "საბჭოს",
        "გამგეობის",
        "შემკვეთის",
        "დამკვეთის",
        "შემსრულებლის",
        "მიმწოდებლის",
        "მყიდველის",
        "გამყიდველის",
    }
)
# "წარმოდგენილი მინდობილობით მოქმედი პირის, ლევან გელაშვილის სახით", "მინდობილობით: ლევან
# გელაშვილი": a signatory acting under a power of attorney, not the party's director. A
# signature block writes the attorney's title as "მინდობილი პირი: ..." or "წარმომადგენელი
# (მინდობილობით) ..." (red team R4, AOA-R4-12): read with the same guards on the name's place.
_PROXY = re.compile(
    r"(?:მინდობილობით(?:\s+მოქმედი\s+პირ(?:ი|ის|მა)?)?|რწმუნებულ(?:ი|ის|მა)"
    r"|მინდობილ(?:ი|მა)\s+პირ(?:ი|ის|მა)?"
    r"|წარმომადგენ(?:ელი|ლის|ელმა)\s*\(\s*მინდობილობით\s*\))(?![ა-ჰ])"
    rf"{_TITLE_TO_NAME}{_HONORIFIC}(?P<name>{_PERSON})"
)
_PROJECT_WORD = r"პრო[ეა]{1,3}ქტ[ა-ჰ]*"
# "პროექტ „ზღვის რეზიდენსი“", after a dash or a colon: "პროექტის - „ზღვის რეზიდენსი“", and
# after a label noun: "პროექტის დასახელება: „ზღვის რეზიდენსი“"
_PROJECT = re.compile(
    rf"{_PROJECT_WORD}(?:\s+(?:დასახელება|სახელწოდება|სახელი)[ა-ჰ]{{0,2}})?"
    r"\s*(?:[-\u2013\u2014:]\s*)?"
    rf"{_QUOTED}"
)
# "„ზღვის რეზიდენსი“ პროექტი", and with the case ending written after the quote:
# "„ზღვის რეზიდენსი“-ს პროექტის" - the quote then holds the nominative
_PROJECT_REV = re.compile(rf"{_QUOTED}(?:-?[ა-ჰ]{{1,4}}(?![ა-ჰ]))?\s*{_PROJECT_WORD}")
# An IBAN: a country, two check digits and the account, compact ("GE29NB00...") or printed in
# groups of four ("GE29 NB00 0000 ..."), and now and then cut by a line break. The first piece
# starts it; the pieces after it are read up to the country's length - see ``_ibans``.
_IBAN_START = re.compile(r"\b([A-Z]{2})\d{2}[A-Z0-9]*\b")
_IBAN_PIECE = re.compile(r"[ \n]([A-Z0-9]+)\b")
#: the shortest IBAN any country issues
_IBAN_MIN = 15
#: "4." or "4)" - the next clause's number, which may follow an account at the end of a line
_CLAUSE_NUMBER = re.compile(r"\d{1,2}[.)]")
# The grouped form of a country whose length is unknown can run on into a following capitalised
# word, so it is accepted only at a length whose checksum validates - see ``_iban_by_checksum``.
_IBAN_GROUPED = re.compile(r"\b[A-Z]{2}\d{2}(?: [A-Z0-9]{4}){2,7}(?: [A-Z0-9]{1,3})?\b")
# Unlabelled BIC. The shape alone fits any 8- or 11-letter capitalised word ("SUPPLIER",
# "DESCRIPTION"), so a match is kept only when ``unlabelled_bic_plausible`` says so.
_SWIFT = re.compile(r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")
# A bank code read from its own label, so a malformed one is still captured. The pattern
# above only matches a well-formed BIC, which means a wrong code like "AAAGE22" (seven
# characters, TBC Bank instead of Bank of Georgia) produced no value at all and the
# comparison against the contract had nothing to compare.
# The label in any case, the code as written: a code is capitals, so "Payment by SWIFT transfer"
# names no code - read case-blind it gave the bank code "TRANSFER".
_SWIFT_LABELLED = re.compile(
    r"(?i:ბანკის\s*კოდი|ბანკის\s*BIC|SWIFT(?:\s*/\s*BIC)?|BIC)\s*[:\uff1a]?\s*"
    r"([A-Z0-9]{6,11})\b"
)
# "(შემდგომში „შემკვეთი“)", "(შემდგომში - ,,შემსრულებელი”)"
_ROLE = re.compile(rf"შემდგომში[\s\-\u2013\u2014]*(?:{_QUOTED}|[,\s]*([ა-ჰ]{{4,20}}))")
_ROLE_BARE = re.compile(rf"{_QUOTED}")

# words that look like a name after "დირექტორი" but are role nouns, not people
_NOT_A_NAME = {
    "სახით",
    "წარმოდგენილი",
    "და",
    "შემდგომში",
    "მიერ",
    "ხელმოწერა",
    "ხელმოწერები",
    "მხარე",
    "მხარეები",
    "მხარები",
    "კომპანია",
    "საზოგადოება",
    "შესაბამისად",
    # a predicate after a title: "მინდობილი პირი ვალდებულია წარმოადგინოს" (red team R4)
    "ვალდებულია",
    "უფლებამოსილია",
    "პასუხისმგებელია",
}


@dataclass(frozen=True, slots=True)
class AnchorHit:
    type: FactType
    value: str
    raw_text: str
    start: int
    end: int
    normalized: str | float | None = None
    unit: str | None = None
    note: str | None = None


#: Where a party's role was read. "defined": the document's own definition of its side,
#: "(შემდგომში „შემკვეთი“)". "signature": the bare role word above the name in a signature
#: block, which a side-by-side layout can pair with the other party's name. A role read from
#: any other definition - "(შემდგომში „მომსახურება“)" - has no source.
RoleSource = Literal["defined", "signature"]
#: Under which title a signatory signs: the party's head, an attorney, or the head's deputy.
SignatoryKind = Literal["director", "proxy", "deputy"]


@dataclass(slots=True)
class Party:
    """One contracting party, assembled from the labels around its name."""

    name: str | None = None
    id_code: str | None = None
    address: str | None = None
    directors: list[str] = field(default_factory=list)
    #: who signs for the party under a power of attorney, in its place of a director
    proxies: list[str] = field(default_factory=list)
    #: who signs for the party as its head's deputy or as acting head ("დირექტორის მოადგილე")
    deputies: list[str] = field(default_factory=list)
    role: str | None = None
    is_bank: bool = False
    #: IBANs stated inside this party's block of requisites, in document order
    accounts: list[str] = field(default_factory=list)
    role_source: RoleSource | None = None
    #: Where the name, the identification code and the defined role were read, as offsets into
    #: the text once ``find_parties`` has normalised it line by line. The AOA checker's text is
    #: normalised already, so for it they index the text itself. None on a record built by hand.
    span: tuple[int, int] | None = None
    id_span: tuple[int, int] | None = None
    role_span: tuple[int, int] | None = None

    @property
    def key(self) -> str:
        return self.id_code or normalize_name(self.name or "")


def bic_is_well_formed(code: str) -> bool:
    """ISO 9362: four letters (bank), two letters (country), two alphanumerics, and an
    optional three-character branch - so a BIC is 8 or 11 characters. A seven-character code
    such as "AAAGE22" is always wrong, and saying so distinguishes a typo from a genuinely
    different bank."""
    code = code.strip().upper()
    if len(code) not in (8, 11):
        return False
    return code[:4].isalpha() and code[4:6].isalpha() and code[6:].isalnum()


def _clean(s: str) -> str:
    return normalize_spaces(s).strip(" ,;:.-")


def _prepare(text: str) -> str:
    """Normalise each line on its own, so that line breaks survive.

    ``normalize_confusables`` folds every whitespace run, newlines included, into one space.
    Applied to the whole text, it erased the line break that ends an address, and the address
    swallowed the telephone and e-mail lines under it. Text that arrives already flattened
    passes through unchanged.
    """
    return "\n".join(normalize_confusables(line) for line in text.split("\n"))


def _iban_by_length(text: str, m: re.Match[str], length: int) -> tuple[str, int, int] | None:
    """The IBAN starting at ``m`` for a country whose IBANs are ``length`` characters long.

    Pieces after the first - groups of four, or the rest of an IBAN a line break or a soft break
    cut off ("GE07XY0000000123 456789") - are taken while the whole still fits the length, and
    the IBAN is whole when it has exactly that length. Its checksum is not asked: an anonymised
    or mistyped account fails it and is still the account the document states. A clause number
    after a short IBAN ("... 4. აქტი") is never a piece of it.

    A currency sub-account's code written straight after the account ("GE29...17GEL", "... 17GEL"
    in groups) is no part of it: the IBAN is the account's first ``length`` characters, and the
    span still runs over the code, so a finding quotes the account as the document prints it.
    """
    first = m.group(0)
    if len(first) > length and first[length:] in IBAN_CURRENCY_SUFFIXES:
        return first[:length], m.start(), m.end()
    pieces, end = [first], m.end()
    total = len(first)
    ran_out = True
    while total < length:
        pm = _IBAN_PIECE.match(text, end)
        if pm is None:
            break
        piece = pm.group(1)
        if total + len(piece) > length and piece[length - total :] in IBAN_CURRENCY_SUFFIXES:
            pieces.append(piece[: length - total])
            total, end = length, pm.end()
            break
        if total + len(piece) > length or _CLAUSE_NUMBER.match(text, pm.start(1)):
            ran_out = False
            break
        pieces.append(piece)
        total += len(piece)
        end = pm.end()
    if total == length:
        return "".join(pieces), m.start(), end
    grouped = len(pieces) > 1 and all(len(p) == 4 for p in pieces[:-1])
    if grouped and ran_out and total >= _IBAN_MIN:
        # a grouped account a digit short or long is still the account written: a mistake to report
        return "".join(pieces), m.start(), end
    if len(m.group(0)) >= _IBAN_MIN:
        return m.group(0), m.start(), m.end()
    return None


def _iban_by_checksum(m: re.Match[str]) -> tuple[str, int, int] | None:
    """A grouped IBAN of a country of unknown length: drop trailing groups until the checksum
    holds, so "XX29 NB00 ... 17 BANK" does not take the next word with it."""
    groups = m.group(0).split(" ")
    while len(groups) > 3:
        candidate = "".join(groups)
        if iban_is_valid(candidate):
            return candidate, m.start(), m.start() + len(" ".join(groups))
        groups.pop()
    return None


def _ibans(text: str) -> list[tuple[str, int, int]]:
    """(IBAN, start, end) for compact, grouped and broken IBANs, each read once.

    Where an IBAN ends is decided by its country's fixed length (``iban_length``) wherever that
    is known; the checksum decides only for a grouped IBAN of another country.
    """
    out: list[tuple[str, int, int]] = []
    for m in _IBAN_START.finditer(text):
        if any(s <= m.start() < e for _, s, e in out):
            continue
        length = iban_length(m.group(1))
        if length is not None:
            found = _iban_by_length(text, m, length)
        elif len(m.group(0)) >= _IBAN_MIN:
            found = (m.group(0), m.start(), m.end())
        else:
            grouped = _IBAN_GROUPED.match(text, m.start())
            found = _iban_by_checksum(grouped) if grouped else None
        if found is not None:
            out.append(found)
    return out


def _same_party(a: Party, b: Party, *, split_conflicts: bool = False) -> bool:
    if a.id_code and b.id_code:
        if a.id_code != b.id_code:
            return False
        # One code under two names. Merged, the second name's block vanishes into the first:
        # a contractor written with the customer's code reads as the customer.
        return not split_conflicts or not (a.name and b.name) or names_match(a.name, b.name)
    return bool(a.name and b.name and names_match(a.name, b.name))


# --- signatories -----------------------------------------------------------------------------

#: What a Georgian surname ends in: -შვილი, -ძე, -ია, -უა, -ავა, -ანი, -ელი, -ონი, in any case.
#: A word after "დირექტორი" in a sentence ("პასუხს აგებს", "უფლებამოსილების") has none of these;
#: a name that has none is still read where its place says it is a name.
_SURNAME_NOMINATIVE = ("შვილი", "ძე", "ია", "უა", "ავა", "ანი", "ელი", "ონი")
#: the stem a consonant-final surname keeps before a case ending: "ბერიძ-ის", "მაჭავარიან-ის"
_SURNAME_STEM = ("შვილ", "ძ", "ან", "ელ", "ონ")
_CASE_ENDINGS = ("ისა", "ის", "ით", "მა", "ს", "მ")
#: The name's own line or cell ends after it: the line, the text, a quote, a signature line.
_NAME_ENDS = re.compile(rf"[ \t]*(?:$|\n|[{_Q}_/])")
#: The name is the preamble's "..., X Y-ის სახით" ("in the person of X Y").
_IN_THE_PERSON_OF = re.compile(r"\s*სახით(?![ა-ჰ])")
_NAME_WORD = re.compile(r"[ა-ჰ]\.|[ა-ჰ]+")


def _surname_shaped(word: str) -> bool:
    """Whether ``word``, in any case, ends as a Georgian surname does ("ჩხეტიას", "ბერიძის")."""
    if word.endswith(_SURNAME_NOMINATIVE):
        return len(word) >= 5  # "ხელი" ("hand") ends as "-ელი" does, and is no surname
    stems = [word[: -len(e)] for e in _CASE_ENDINGS if word.endswith(e) and len(word) - len(e) >= 4]
    return any(s.endswith((*_SURNAME_NOMINATIVE, *_SURNAME_STEM)) for s in stems)


@dataclass(slots=True)
class _Signatory:
    """A name read after a head's, a deputy's or an attorney's title, not yet given to a party."""

    owner: Party
    name: str
    start: int
    #: its place says it is a name: after a colon, before "სახით", alone on its line, or with a
    #: surname's ending. Otherwise it is kept only when the document names the person again.
    confirmed: bool
    kind: SignatoryKind


def _signatories_of(party: Party, kind: SignatoryKind) -> list[str]:
    """The list of ``party`` that holds its signatories of ``kind``."""
    if kind == "proxy":
        return party.proxies
    return party.deputies if kind == "deputy" else party.directors


def _another_body_s(window: str, m: re.Match[str]) -> bool:
    """Whether the title read at ``m`` is another body's officer's: a genitive stands right before
    it that is not the party's own board or side ("პროექტის ხელმძღვანელი", the project's
    manager). A director's title keeps the reading it always had."""
    if m.re is _DIRECTOR and "დირექტორ" in window[m.start() : m.start("sep")]:
        return False
    gm = _TITLE_OF_OTHER.search(window[max(0, m.start() - 40) : m.start()])
    return gm is not None and gm.group(1) not in _OWN_BODY_GENITIVES


#: The longest first name a signatory is given: "კონსტანტინე" has eleven letters, and a longer
#: word after the label is a predicate ("უფლებამოსილია", "is authorised"), not a name.
_FIRST_NAME_MAX = 12


def _name_shaped(first: str, last: str) -> bool:
    """Whether two words can be a first name and a surname. Not when the first is longer than
    any first name, nor when it is a genitive the second word, no surname, depends on - a noun
    phrase, "ხელმოწერის უფლება" ("the right of signature"): in a person's name only the
    surname takes the case ending."""
    if first.endswith(".") and len(first) == 2:
        return True  # an initial
    if len(first) > _FIRST_NAME_MAX:
        return False
    return not (first.endswith("ის") and len(first) > 4 and not _surname_shaped(last))


#: Titles that are also adjectives: "ხელმძღვანელი პირი" (a person in charge), "მმართველი ორგანო"
#: (a governing body). A name after them needs its place to say so - a colon, "სახით", the end
#: of its line - or the document naming the person again; a surname's ending is not enough. Nor
#: is a vowel stem's genitive "-ს" a first name there: "მმართველი ორგანოს გადაწყვეტილებით" ("by
#: the governing body's decision") names nobody. After any other title a first name in "-ს" is
#: read, as a foreign one is ("დირექტორი: თომას ბრაუნი").
_ADJECTIVAL_TITLE = re.compile(r"ხელმძღვანელ|მმართველ")


def _read_signatory(window: str, m: re.Match[str]) -> tuple[str, int, bool] | None:
    """(name, end of the name in ``window``, confirmed) for a director's or an attorney's label,
    or None when the words after it are no name - "ან მის მიერ" ("or by him")."""
    words: list[re.Match[str]] = []
    for w in _NAME_WORD.finditer(m.group("name")):
        if w.group(0) in _NOT_A_NAME or len(words) == 4:
            break
        words.append(w)
    if len(words) < 2:
        return None
    between = m.group("name")[words[1].start() : words[2].end()] if len(words) > 3 else ""
    if len(words) > 3 and PATRONYMIC.fullmatch(between):
        del words[1:3]  # the patronymic: the name is its first name and its surname
    first, last = words[0].group(0), words[1].group(0)
    if {first, last} & (FUNCTION_WORDS | _NOT_A_NAME) or last.endswith("."):
        return None
    if not _name_shaped(first, last):
        return None
    end = m.start("name") + words[1].end()
    adjectival = _ADJECTIVAL_TITLE.search(window[m.start() : m.start("sep")]) is not None
    if adjectival and first.endswith("ს") and len(first) > 4 and not _surname_shaped(last):
        return None
    mark = (m.group("mark") or "").strip()
    confirmed = bool(
        m.group("sep") == ":"
        or _IN_THE_PERSON_OF.match(window, end)
        or _NAME_ENDS.match(window, end)
        # written in slashes or brackets: "/ნინო კაპანაძე/", "(ნინო კაპანაძე)"
        or (mark and re.match(rf"[ \t]*{re.escape(_CLOSING_MARK[mark])}", window[end:]))
        or (_surname_shaped(last) and not adjectival)
    )
    return f"{first} {last}", end, confirmed


#: The mark that closes a name written in slashes or brackets
_CLOSING_MARK = {"/": "/", "(": ")"}


def _name_before_title(m: re.Match[str]) -> str | None:
    """The signatory ``_NAME_BEFORE_TITLE`` read at ``m``: its first name and surname, without
    a patronymic; None when the words are no name ("ხელშეკრულების პირობები - დირექტორი")."""
    words = _NAME_WORD.findall(m.group("name"))
    if len(words) == 4:
        del words[1:3]  # the patronymic
    first, last = words[0], words[-1]
    if {first, last} & (FUNCTION_WORDS | _NOT_A_NAME | ROLE_WORDS) or last.endswith("."):
        return None
    return f"{first} {last}" if _name_shaped(first, last) else None


# --- a signature table serialised row by row ------------------------------------------------

# Docling writes a table cell by cell as "<first cell>, <column header> = <cell>." per row, and
# drops the header row's first cell. A two-column signature table - the customer left, the
# contractor right - thus reads "შპს „ა“, „შემსრულებელი“ = შპს „ბ“. დირექტორი: X, „შემსრულებელი“ =
# დირექტორი: Y." The left column's director X then stands inside the right party's text.
#: after a name: the next cell stands under the column header ``h``
_GRID_NEXT = re.compile(r"\s*,\s*(?P<h>[^,=\n]{2,80}?)\s=\s")
#: before a party's name: the name is a cell under the column header ``h``
_GRID_BEFORE = re.compile(r",\s*(?P<h>[^,=\n]{2,80}?)\s=\s*$")


def _column(header: str) -> tuple[str, str] | None:
    """A column header that says whose column it is: a side ("შემსრულებელი") or a party's name."""
    word = _clean(strip_quotes(header))
    if word in _SIDE_WORDS:
        return "role", word
    if pm := _PARTY_NAME.fullmatch(_clean(header)):
        return "name", _clean(f"{_legal_form(pm.group(1))} {pm.group(2)}")
    return None


def party_column(cell: str) -> tuple[str, str] | None:
    """Whose column a table cell opens, read off its first line: a side ("„შემკვეთი“"), as
    ("role", word), or a party's name ("შპს „ა“", "ი/მ ანა ბერიძე"), as ("name", name). None for
    any other cell. The chunker asks it to tell a table laid out one party per column."""
    first = next((line for line in cell.split("\n") if line.strip()), "")
    first = _clean(normalize_confusables(first))
    if (column := _column(first)) is not None:
        return column
    for pattern in (_PARTY_NAME, _PERSON_PARTY):
        if (pm := pattern.match(first)) is not None:
            return "name", _clean(f"{_legal_form(pm.group(1))} {pm.group(2)}")
    return None


def _in_column(p: Party, column: tuple[str, str], headed: dict[int, tuple[str, str]]) -> bool:
    kind, value = column
    if headed.get(id(p)) == column:
        return True
    if kind == "role":
        return p.role == value
    return bool(p.name and names_match(p.name, value))


def _grid_owner(
    text: str,
    end: int,
    party: Party,
    earlier: list[Party],
    headed: dict[int, tuple[str, str]],
) -> Party:
    """Whose signatory the name ending at ``end`` is.

    The party whose text it stands in, unless the name is a table's first cell with a column
    header after it that names that party (or the party is a bank's name in the payment lines):
    the name is then in another column, the one of the nearest party before it that the header
    does not name.
    """
    # the whole text: the header is often the next party's name, where this party's text ends
    gm = _GRID_NEXT.match(text, end)
    column = _column(gm.group("h")) if gm else None
    if column is None:
        return party
    for q in reversed([*earlier, party]):
        if not q.is_bank and not _in_column(q, column, headed):
            return q
    return party


# --- parties ----------------------------------------------------------------------------------

# A company with its legal form after its quoted name, "„ნიმუშის ხიდი“ შპს" (red team R2,
# AOA-R2-04): the quotes bound the name as they do before the form. The form is a word of its
# own, is not followed by another quoted name ("„შემსრულებელი“ შპს „...“" is a role above a
# party), and the quoted words are no role. A name without quotes is not read: nothing marks
# where it ends, and the act check says so in a skip note.
#: The abbreviated company forms, in every spelling (``normalize.ABBREVIATED_LEGAL_FORMS``);
#: a person ("ი/მ") is named after the form, never before it.
_COMPANY_FORM_ABBREVIATED = "|".join(
    f"(?:{pattern})" for pattern, short in ABBREVIATED_LEGAL_FORMS if short != "ი/მ"
)
_FORM_AFTER_NAME = re.compile(
    rf"{_QUOTED}[ \t]*({_COMPANY_FORM_ABBREVIATED})(?![ა-ჰ\-])(?![ \t]*[{_Q}])"
    rf"(?![ \t]*(?:{_LEGAL_FORM}))"
)


@dataclass(frozen=True, slots=True)
class _Mention:
    """A party named with its legal form after the name, shaped as a match of ``_PARTY_NAME``
    is - group 1 the legal form, group 2 the name - so every mention is read alike."""

    begin: int
    finish: int
    form: str
    name: str
    text: str

    def start(self) -> int:
        return self.begin

    def end(self) -> int:
        return self.finish

    def span(self) -> tuple[int, int]:
        return self.begin, self.finish

    def group(self, i: int = 0) -> str:
        return (self.text, self.form, self.name)[i]


def _form_after_name(text: str) -> list[_Mention]:
    """The companies ``text`` names as "„...“ შპს"."""
    return [
        _Mention(m.start(), m.end(), m.group(2), m.group(1), m.group(0))
        for m in _FORM_AFTER_NAME.finditer(text)
        if _clean(m.group(1)) not in _ROLE_WORDS_INFLECTED
    ]


#: a side's role word written directly above or before a name: "„შემკვეთი“ სს „...ბანკი“"
_SIDE_ABOVE = re.compile(rf"[{_Q}](?:{'|'.join(sorted(_SIDE_WORDS))})[{_Q}]\s*[:\-]?\s*$")


def _is_a_named_party(p: Party, parties: list[Party], before: str) -> bool:
    """A bank's name that is a contracting party: its side is defined or written above it, or a
    party with an identification code carries the same name. A bank the payment lines name as a
    bank ("ბანკი: სს „...“") stays a bank."""
    if "ბანკ" in before[-25:]:
        return False
    return (
        p.role_source == "defined"
        or _SIDE_ABOVE.search(before) is not None
        or any(
            q is not p
            and not q.is_bank
            and q.id_code
            and q.name
            and names_match(p.name or "", q.name)
            for q in parties
        )
    )


def _merge_into(existing: Party, p: Party) -> None:
    existing.name = existing.name or p.name
    if not existing.id_code:
        existing.id_code, existing.id_span = p.id_code, p.id_span
    existing.address = existing.address or p.address
    if not existing.role or (p.role_source == "defined" and existing.role_source != "defined"):
        existing.role, existing.role_source = p.role, p.role_source
        existing.role_span = p.role_span
    existing.accounts.extend(a for a in p.accounts if a not in existing.accounts)
    _add_names(existing.directors, p.directors)
    _add_names(existing.proxies, p.proxies)
    _add_names(existing.deputies, p.deputies)


def _add_names(names: list[str], new: list[str]) -> None:
    for d in new:
        if not any(names_match(d, e) for e in names):
            names.append(d)


def find_parties(text: str, *, split_conflicts: bool = False) -> list[Party]:
    """Find every contracting party and the labelled fields that belong to it.

    A party's fields are read from the window that starts at its name and ends at the next
    party name, which is exactly how these documents lay them out. Parties are then merged by
    identification code (or by name when there is none), so that the requisites block at the
    top and the signature block at the bottom contribute to one record. A role the document
    defines ("შემდგომში „…“") wins over a signature block's bare role word.

    A bank named in the payment lines is no party: what its window holds - the account, and a
    signatory listed after the bank lines - belongs to the party before it. A signatory is a
    name where the label's place says so (``_read_signatory``), or one the document names again;
    and one in the left column of a signature table goes to the left column's party
    (``_grid_owner``).

    ``split_conflicts`` keeps two records with one code but different names apart instead of
    merging them. The AOA checker asks for it, to see a code given to the wrong party; the
    extraction pipeline does not, so what it reads is unchanged.
    """
    text = _prepare(text)
    quoted = list(_PARTY_NAME.finditer(text))
    people = [
        m
        for m in _PERSON_PARTY.finditer(text)
        if not any(w in _NOT_A_NAME for w in m.group(2).split())
        and not any(q.start() <= m.start() < q.end() for q in quoted)
    ]
    named = [*quoted, *people]
    bare = [
        m
        for m in _form_after_name(text)
        if not any(q.start() < m.end() and m.start() < q.end() for q in named)
    ]
    matches: list[re.Match[str] | _Mention] = sorted([*named, *bare], key=lambda m: m.start())
    if not matches:
        return []

    parties: list[Party] = []
    befores: list[str] = []
    headed: dict[int, tuple[str, str]] = {}
    signatories: list[_Signatory] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        window = text[start:end]
        before = text[max(0, start - 40) : start]

        party = Party(name=_clean(f"{_legal_form(m.group(1))} {m.group(2)}"), span=m.span())
        # "ბანკი: სს „საქართველოს ბანკი“" and "...გადარიცხვით: სს „საქართველოს ბანკი“,
        # ბანკის კოდი:" are payment details, not contracting parties. A real party always
        # brings an identification code with it, so require one before believing the name.
        after = window[len(m.group(0)) : len(m.group(0)) + 60]
        looks_like_bank = "ბანკ" in before[-25:] or "ბანკ" in m.group(2) or "ბანკის კოდი" in after
        party.is_bank = looks_like_bank and not _PARTY_CODE.search(window[: len(m.group(0)) + 80])
        gm = _GRID_BEFORE.search(text[max(0, start - 100) : start])
        if gm and (column := _column(gm.group("h"))):
            headed[id(party)] = column

        if mm := _PARTY_CODE.search(window):
            party.id_code = _code_digits(mm.group(1))
            party.id_span = (start + mm.start(1), start + mm.end(1))
        if mm := _ADDRESS.search(window):
            party.address = _clean(mm.group(1))
        if rm := _ROLE.search(window):
            party.role = _clean(rm.group(1) or rm.group(2) or "")
            # "(შემდგომში „მომსახურება“)" after a name in a clause defines the service, not the
            # party: only a side is a definition that outranks a signature block's role
            if party.role in _SIDE_WORDS:
                party.role_source = "defined"
                party.role_span = (start + rm.start(), start + rm.end())
        elif rm := _ROLE_BARE.search(before):
            # signature blocks put the bare role above the name: “შემსრულებელი” / შპს „…“
            cand = _clean(rm.group(1))
            if cand in ROLE_WORDS:
                party.role = cand
                party.role_source = "signature"
        kinds: tuple[tuple[re.Pattern[str], SignatoryKind], ...] = (
            (_DIRECTOR, "director"),
            (_PROXY, "proxy"),
            (_DEPUTY, "deputy"),
        )
        for label, kind in kinds:
            at = 0
            # the next label is looked for right after the name read, never after the words the
            # pattern took beyond it: they may be the next signatory's title
            while (dm := label.search(window, at)) is not None:
                at = dm.end()
                if label is not _PROXY and _another_body_s(window, dm):
                    continue
                if label is _DIRECTOR and _FUNCTION_BEFORE_TITLE.search(window, 0, dm.start()):
                    continue
                if (read := _read_signatory(window, dm)) is None:
                    continue
                name, name_end, confirmed = read
                at = name_end
                owner = _grid_owner(text, start + name_end, party, parties, headed)
                signatories.append(
                    _Signatory(owner, name, start + dm.start("name"), confirmed, kind)
                )
        # "ნინო კაპანაძე - დირექტორი": the head's title after the name; its place confirms it
        for bm in _NAME_BEFORE_TITLE.finditer(window):
            if (head := _name_before_title(bm)) is None:
                continue
            owner = _grid_owner(text, start + bm.end(), party, parties, headed)
            signatories.append(_Signatory(owner, head, start + bm.start("name"), True, "director"))
        party.accounts = list(dict.fromkeys(iban for iban, _, _ in _ibans(window)))
        parties.append(party)
        befores.append(before)

    # a name no label's place confirms is kept when the document names the person again
    confirmed_names = [s.name for s in signatories if s.confirmed]
    for s in sorted(signatories, key=lambda s: s.start):
        if s.confirmed or any(names_match(s.name, c) for c in confirmed_names):
            _add_names(_signatories_of(s.owner, s.kind), [s.name])

    for p, before in zip(parties, befores, strict=True):
        if p.is_bank and _is_a_named_party(p, parties, before):
            p.is_bank = False

    merged: list[Party] = []
    last: Party | None = None  # the record the previous real party went into
    for p in parties:
        if p.is_bank:
            # "ბანკი: სს „…ბანკი“, ა/ა GE…" sits inside the preceding party's requisites: the
            # account after the bank's name, and a signatory listed after the bank lines, are
            # that party's
            if last is not None:
                last.accounts.extend(a for a in p.accounts if a not in last.accounts)
                _add_names(last.directors, p.directors)
                _add_names(last.proxies, p.proxies)
                _add_names(last.deputies, p.deputies)
                last.address = last.address or p.address
            continue
        existing = next(
            (q for q in merged if _same_party(p, q, split_conflicts=split_conflicts)), None
        )
        if existing is None:
            merged.append(p)
            last = p
            continue
        _merge_into(existing, p)
        last = existing
    return merged


def defined_sides(text: str) -> list[str]:
    """The sides ``text`` defines for its parties - "(შემდგომში „შემკვეთი“)" - in order, each
    once. Only a side: "(შემდგომში „მომსახურება“)" defines the service. The act check compares
    them with the parties read, to say which side's party it could not read (red team R4,
    AOA-R4-02) instead of passing it over."""
    out: list[str] = []
    for m in _ROLE.finditer(_prepare(text)):
        role = _clean(m.group(1) or m.group(2) or "")
        if role in _SIDE_WORDS and role not in out:
            out.append(role)
    return out


def first_party_start(text: str) -> int | None:
    """Where the first party's name starts, or None when no party is named.

    A contract's preamble - its title, the place and the date it was signed - is everything
    before it, which is how a reader tells the signing date from every other date. A party is
    named as ``find_parties`` names one: a legal form and a quoted name, or a person or an
    individual entrepreneur by first name and surname ("ფ/პ ანა ბერიძე").

    Parties the title names ("ხელშეკრულება N 7 შპს „…“-სა და შპს „…“-ს შორის") are part of the
    title, and the preamble goes on past them (``_title_end``).
    """
    after = _title_end(text)
    quoted = _PARTY_NAME.search(text, after)
    person = next(
        (
            m
            for m in _PERSON_PARTY.finditer(text, after)
            if not any(w in _NOT_A_NAME for w in m.group(2).split())
        ),
        None,
    )
    starts = [m.start() for m in (quoted, person) if m is not None]
    return min(starts, default=None)


#: A title that names the parties: the contract word and its number right before the first
#: party ("ხელშეკრულება N 7 შპს „…“-სა და ...") ...
_TITLE_WORD_BEFORE = re.compile(r"ხელშეკრულება(?![ა-ჰ])[ \t]*(?:(?:N|№)[ \t]*\S+[ \t]*)?$")
#: ... the parties joined by "-სა და ... -ს შორის" (between), on one line, no clause between ...
_TITLE_TEXT = r"(?:(?![.;:]\s)[^\n(])*?"
_BETWEEN = re.compile(rf"{_TITLE_TEXT}(?<![ა-ჰ])და(?![ა-ჰ]){_TITLE_TEXT}(?<![ა-ჰ])შორის(?![ა-ჰ])")
#: ... or the contract word after the parties: "... შორის გაფორმებული (მომსახურების)
#: ხელშეკრულება".
_TITLE_WORD_AFTER = re.compile(
    r"[ \t]+(?:გაფორმებული|დადებული)[ \t]+(?:[ა-ჰ]+[ \t]+)?ხელშეკრულება(?![ა-ჰ])"
)
_TITLE_REACH = 200


def _title_end(text: str) -> int:
    """Where a title that names the parties ends - after its "შორის" - or 0 when the first
    party is not named in a title. A party block written the same way ("... დაიდო შპს „…“-სა
    (შემდგომში „შემკვეთი“) და ... შორის") defines the parties' roles in brackets, and the
    bracket keeps it out; so does a document word other than the contract's number before it."""
    first = _PARTY_NAME.search(text)
    if first is None:
        return 0
    line_start = text.rfind("\n", 0, first.start()) + 1
    limit = min(len(text), first.end() + _TITLE_REACH)
    between = _BETWEEN.match(text, first.end(), limit)
    if between is None:
        return 0
    if _TITLE_WORD_BEFORE.search(text, max(line_start, first.start() - 60), first.start()):
        return between.end()
    opens_line = not text[line_start : first.start()].strip()
    if opens_line and _TITLE_WORD_AFTER.match(text, between.end()):
        return between.end()
    return 0


#: A document's first numbered clause: "1.", "1.1." or "მუხლი 1" opening a line or a sentence,
#: or following the year of a heading's date in text flattened to one line ("... 2026 1. ...")
#: or its day, the date written ISO ("... 2026-03-03 1. ...").
_FIRST_CLAUSE = re.compile(
    r"(?:^|(?<=\n)|(?<=[.;:!?)“”\"]\s)|(?<=\d{4}\s)|(?<=\d{4}-\d\d-\d\d\s))[ \t]*"
    r"(?:1\.(?:1\.?)?[ \t]*(?=[^\W\d_])|(?:მუხლი|article)[ \t]+1(?!\d))",
    re.IGNORECASE,
)


def preamble_end(text: str) -> int | None:
    """Where a document's preamble ends: at its first party (``first_party_start``) or at its
    first numbered clause, whichever comes first; None when it names neither. A date before it
    is the document's own; a date after it belongs to a clause."""
    clause = _FIRST_CLAUSE.search(text)
    ends = [
        e for e in (first_party_start(text), clause.start() if clause else None) if e is not None
    ]
    return min(ends, default=None)


def find_anchors(text: str) -> list[AnchorHit]:
    """Anchor hits as flat facts, for the extraction pipeline."""
    text = _prepare(text)
    hits: list[AnchorHit] = []

    def emit(
        ft: FactType,
        value: str,
        raw_text: str,
        start: int,
        end: int,
        *,
        normalized: str | float | None = None,
        unit: str | None = None,
        note: str | None = None,
    ) -> None:
        value = _clean(value)
        if value:
            hits.append(
                AnchorHit(
                    type=ft,
                    value=value,
                    raw_text=raw_text,
                    start=start,
                    end=end,
                    normalized=normalized,
                    unit=unit,
                    note=note,
                )
            )

    def add(
        ft: FactType,
        value: str,
        m: re.Match[str],
        *,
        normalized: str | float | None = None,
        unit: str | None = None,
        note: str | None = None,
    ) -> None:
        emit(ft, value, m.group(0), m.start(), m.end(), normalized=normalized, unit=unit, note=note)

    for m in _ID_CODE.finditer(text):
        code = _code_digits(m.group(1))
        add(FactType.party_id_code, code, m, normalized=code)
    for m in _ADDRESS.finditer(text):
        add(FactType.party_address, m.group(1), m)
    forward_spans: list[tuple[int, int]] = []
    for m in _PROJECT.finditer(text):
        forward_spans.append(m.span())
        add(FactType.project_name, m.group(1), m)
    for m in _PROJECT_REV.finditer(text):
        if any(a <= m.end() <= b for a, b in forward_spans):
            continue
        if _clean(m.group(1)) in _ROLE_WORDS_INFLECTED:
            continue
        add(FactType.project_name, m.group(1), m)
    for m in CADASTRAL_RE.finditer(text):
        add(FactType.cadastral_code, m.group(0), m, normalized=m.group(0))
    for iban, start, end in _ibans(text):
        emit(
            FactType.bank_account,
            iban,
            text[start:end],
            start,
            end,
            unit="IBAN",
            normalized=iban,
            # kept, not dropped: a mistyped account is what the acceptance check must report
            note=None if iban_is_valid(iban) else "IBAN checksum does not validate",
        )
    for m in _SWIFT.finditer(text):
        if not unlabelled_bic_plausible(text, m.start(), m.end()):
            continue
        add(FactType.bank_code, m.group(0), m, unit="SWIFT")
    for m in _SWIFT_LABELLED.finditer(text):
        code = m.group(1)
        # a code, even a mistyped one, has a digit or a real country code: "ADDRESS" has neither
        if not any(ch.isdigit() for ch in code) and not (
            len(code) in (8, 11) and bic_has_country(code)
        ):
            continue
        note = None if bic_is_well_formed(code) else "bank code is not a well-formed BIC"
        emit(FactType.bank_code, code, m.group(0), m.start(), m.end(), unit="SWIFT", note=note)

    # Directors come from the party parser rather than a bare regex: it knows which company a
    # signatory belongs to, cuts the trailing role noun off a genitive construction, and merges
    # the requisites block with the signature block. Without this the strongest field the
    # anchors produce never reaches the pipeline, and the LLM is asked for it instead.
    for party in find_parties(text):
        # names come from the party parser, which is the only place that knows a bank named in
        # the payment clause is not a contracting party
        if party.name:
            index = text.find(party.name)
            # a legal form written out is not the name's text: point at where the name stands
            start, end = (
                (index, index + len(party.name)) if index >= 0 or party.span is None else party.span
            )
            emit(
                FactType.party_name,
                party.name,
                party.name if index >= 0 else text[start:end],
                max(start, 0),
                max(start, 0) + (end - start),
                note=f"role: {party.role}" if party.role else None,
            )
        if party.address:
            index = text.find(party.address)
            emit(
                FactType.party_address,
                party.address,
                party.address,
                max(index, 0),
                max(index, 0) + len(party.address),
            )
        # the signatories: the party's directors, and whoever signs under a power of attorney
        for director, how in [(d, "signatory") for d in party.directors] + [
            (p, "signatory under a power of attorney") for p in party.proxies
        ]:
            start, end = _name_written(text, director)
            emit(
                FactType.party_director,
                director,
                text[start:end] or director,
                start,
                end,
                note=f"{how} for {party.name}" if party.name else None,
            )

    # Names are deduplicated by stem: "ნინო კაპანაძის" in the preamble and
    # "ნინო კაპანაძე" in the signature block are one person, and emitting both would
    # report a false extra signatory on every correctly drafted document.
    name_types = {FactType.party_name, FactType.party_director, FactType.project_name}
    seen: set[tuple[FactType, str, str | None]] = set()
    out: list[AnchorHit] = []
    for h in sorted(hits, key=lambda h: h.start):
        key = name_stem(h.value) if h.type in name_types else normalize_name(h.value)
        k = (h.type, key, h.unit)
        if k not in seen:
            seen.add(k)
            out.append(h)
    return out


def _name_written(text: str, name: str) -> tuple[int, int]:
    """Where ``text`` writes the person ``name``: as it is, or with a patronymic between its
    first name and its surname ("ნოდარ გიორგის ძე ბარამიძე" for "ნოდარ ბარამიძე"); the start of
    the text, as before, when neither is found."""
    index = text.find(name)
    if index >= 0:
        return index, index + len(name)
    first, _, last = name.partition(" ")
    if last:
        m = re.search(
            rf"(?<![ა-ჰ]){re.escape(first)}\s+{PATRONYMIC.pattern}\s+{re.escape(last)}(?![ა-ჰ])",
            text,
        )
        if m is not None:
            return m.span()
    return 0, len(name)


def split_patronymic(name: str) -> tuple[str, str | None]:
    """``name`` as its first name and its surname, and the patronymic written between them, or
    None: "დავით გიორგის ძე მელაძე" is ("დავით მელაძე", "გიორგის ძე"). The act check compares
    the two words and writes a correction with the patronymic kept (red team R3, AOA-R3-06)."""
    words = name.split()
    if len(words) == 4 and PATRONYMIC.fullmatch(f"{words[1]} {words[2]}"):
        return f"{words[0]} {words[3]}", f"{words[1]} {words[2]}"
    return name, None


def with_patronymic(name: str, patronymic: str | None) -> str:
    """``name`` (a first name and a surname) with ``patronymic`` put back between the two."""
    first, _, rest = name.partition(" ")
    return f"{first} {patronymic} {rest}" if patronymic and rest else name


def directors_of(text: str) -> list[str]:
    return [d for p in find_parties(text) for d in p.directors]
