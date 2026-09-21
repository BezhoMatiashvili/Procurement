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

from procurement_agent.extraction.normalize import (
    QUOTE_CHARS,
    name_stem,
    names_match,
    normalize_confusables,
    normalize_name,
    normalize_spaces,
)
from procurement_agent.extraction.schemas import FactType

_Q = re.escape(QUOTE_CHARS)
_QUOTED = rf"[{_Q}]([^{_Q}]{{2,90}}?)[{_Q}]"

# Georgian legal forms. "ი/მ" and "ინდ. მეწარმე" matter because the client's tax-wording rules
# branch on whether the contractor is a company, an individual entrepreneur or a person.
_LEGAL_FORM = r"შპს|სს|ააიპ|სსიპ|ი\s*/\s*მ|ინდ(?:\.|ივიდუალური)?\s*მეწარმ[ეის]*"

_PARTY_NAME = re.compile(rf"(?<![ა-ჰ])({_LEGAL_FORM})\s*{_QUOTED}")
_ID_CODE = re.compile(
    r"(?:ს\s*/\s*კ|საიდენტიფიკაციო\s*(?:კოდი|ნომერი)?|პირადი\s*ნომერი)\s*[:№#-]?\s*(\d{9}|\d{11})(?!\d)",
    re.IGNORECASE,
)
_ADDRESS = re.compile(
    r"(?:იურიდიული\s*)?მისამართი\s*[:\uff1a]\s*([^;()\n]{4,140}?)\s*(?=[;()\n]|$)", re.IGNORECASE
)
# "დირექტორი ნინო კაპანაძე", "დირექტორი: დავით მელაძე",
# "წარმოდგენილი გენერალური დირექტორის, დავით მელაძის სახით"
_DIRECTOR = re.compile(
    r"(?:გენერალური\s+)?დირექტორ(?:ი|ის|მა)?\s*[:,]?\s*"
    r"([ა-ჰ]{2,20}(?:\s+[ა-ჰ]{2,25}){1,2})"
)
_PROJECT_WORD = r"პრო[ეა]{1,3}ქტ[ა-ჰ]*"
_PROJECT = re.compile(rf"{_PROJECT_WORD}\s*{_QUOTED}")
_PROJECT_REV = re.compile(rf"{_QUOTED}\s*{_PROJECT_WORD}")
_CADASTRAL = re.compile(r"(?<![\d.])\d{2}\.\d{2}\.\d{2}\.\d{3}(?:\.\d{3})?(?![\d.])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_SWIFT = re.compile(r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")
# A bank code read from its own label, so a malformed one is still captured. The pattern
# above only matches a well-formed BIC, which means a wrong code like "AAAGE22" (seven
# characters, TBC Bank instead of Bank of Georgia) produced no value at all and the
# comparison against the contract had nothing to compare.
_SWIFT_LABELLED = re.compile(
    r"(?:ბანკის\s*კოდი|ბანკის\s*BIC|SWIFT(?:\s*/\s*BIC)?|BIC)\s*[:\uff1a]?\s*([A-Z0-9]{6,11})\b",
    re.IGNORECASE,
)
# "(შემდგომში „შემკვეთი“)", "(შემდგომში - ,,შემსრულებელი”)"
_ROLE = re.compile(rf"შემდგომში[\s\-\u2013\u2014]*(?:{_QUOTED}|[,\s]*([ა-ჰ]{{4,20}}))")
_ROLE_BARE = re.compile(rf"{_QUOTED}")

ROLE_WORDS = frozenset(
    {"შემკვეთი", "დამკვეთი", "შემსრულებელი", "მყიდველი", "გამყიდველი", "მხარე", "მხარეები"}
)
# the same words in the genitive, which is how they appear before a noun
_ROLE_WORDS_INFLECTED = frozenset(
    {w for r in ROLE_WORDS for w in (r, r[:-1] + "ის", r + "ს", r[:-1] + "მა")}
)

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


@dataclass(slots=True)
class Party:
    """One contracting party, assembled from the labels around its name."""

    name: str | None = None
    id_code: str | None = None
    address: str | None = None
    directors: list[str] = field(default_factory=list)
    role: str | None = None
    is_bank: bool = False

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


def _same_party(a: Party, b: Party) -> bool:
    if a.id_code and b.id_code:
        return a.id_code == b.id_code
    return bool(a.name and b.name and names_match(a.name, b.name))


def find_parties(text: str) -> list[Party]:
    """Find every contracting party and the labelled fields that belong to it.

    A party's fields are read from the window that starts at its name and ends at the next
    party name, which is exactly how these documents lay them out. Parties are then merged by
    identification code (or by name when there is none), so that the requisites block at the
    top and the signature block at the bottom contribute to one record.
    """
    text = normalize_confusables(text)
    matches = list(_PARTY_NAME.finditer(text))
    if not matches:
        return []

    parties: list[Party] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        window = text[start:end]
        before = text[max(0, start - 40) : start]

        party = Party(name=_clean(f"{m.group(1)} {m.group(2)}"))
        # "ბანკი: სს „საქართველოს ბანკი“" and "...გადარიცხვით: სს „საქართველოს ბანკი“,
        # ბანკის კოდი:" are payment details, not contracting parties. A real party always
        # brings an identification code with it, so require one before believing the name.
        after = window[len(m.group(0)) : len(m.group(0)) + 60]
        looks_like_bank = "ბანკ" in before[-25:] or "ბანკ" in m.group(2) or "ბანკის კოდი" in after
        party.is_bank = looks_like_bank and not _ID_CODE.search(window[: len(m.group(0)) + 80])

        if mm := _ID_CODE.search(window):
            party.id_code = mm.group(1)
        if mm := _ADDRESS.search(window):
            party.address = _clean(mm.group(1))
        for dm in _DIRECTOR.finditer(window):
            words: list[str] = []
            for w in _clean(dm.group(1)).split():
                if w in _NOT_A_NAME:
                    break
                words.append(w)
            if len(words) >= 2:
                cand = " ".join(words[:2])
                if not any(names_match(cand, d) for d in party.directors):
                    party.directors.append(cand)
        if rm := _ROLE.search(window):
            party.role = _clean(rm.group(1) or rm.group(2) or "")
        elif rm := _ROLE_BARE.search(before):
            # signature blocks put the bare role above the name: “შემსრულებელი” / შპს „…“
            cand = _clean(rm.group(1))
            if cand in ROLE_WORDS:
                party.role = cand
        parties.append(party)

    merged: list[Party] = []
    for p in parties:
        if p.is_bank:
            continue
        existing = next((q for q in merged if _same_party(p, q)), None)
        if existing is None:
            merged.append(p)
            continue
        existing.name = existing.name or p.name
        existing.id_code = existing.id_code or p.id_code
        existing.address = existing.address or p.address
        existing.role = existing.role or p.role
        for d in p.directors:
            if not any(names_match(d, e) for e in existing.directors):
                existing.directors.append(d)
    return merged


def find_anchors(text: str) -> list[AnchorHit]:
    """Anchor hits as flat facts, for the extraction pipeline."""
    text = normalize_confusables(text)
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
        add(FactType.party_id_code, m.group(1), m, normalized=m.group(1))
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
    for m in _CADASTRAL.finditer(text):
        add(FactType.cadastral_code, m.group(0), m, normalized=m.group(0))
    for m in _IBAN.finditer(text):
        add(FactType.bank_account, m.group(0), m, unit="IBAN", normalized=m.group(0))
    for m in _SWIFT.finditer(text):
        add(FactType.bank_account, m.group(0), m, unit="SWIFT")
    for m in _SWIFT_LABELLED.finditer(text):
        code = m.group(1).upper()
        note = None if bic_is_well_formed(code) else "bank code is not a well-formed BIC"
        emit(FactType.bank_account, code, m.group(0), m.start(), m.end(), unit="SWIFT", note=note)

    # Directors come from the party parser rather than a bare regex: it knows which company a
    # signatory belongs to, cuts the trailing role noun off a genitive construction, and merges
    # the requisites block with the signature block. Without this the strongest field the
    # anchors produce never reaches the pipeline, and the LLM is asked for it instead.
    for party in find_parties(text):
        # names come from the party parser, which is the only place that knows a bank named in
        # the payment clause is not a contracting party
        if party.name:
            index = text.find(party.name)
            emit(
                FactType.party_name,
                party.name,
                party.name,
                max(index, 0),
                max(index, 0) + len(party.name),
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
        for director in party.directors:
            index = text.find(director)
            if index < 0:
                index = 0
            emit(
                FactType.party_director,
                director,
                text[index : index + len(director)] or director,
                index,
                index + len(director),
                note=f"signatory for {party.name}" if party.name else None,
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


def directors_of(text: str) -> list[str]:
    return [d for p in find_parties(text) for d in p.directors]
