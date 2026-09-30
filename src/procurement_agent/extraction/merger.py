"""Merge rule hits and LLM facts; deduplicate; flag conflicts."""

from __future__ import annotations

import re

from procurement_agent.extraction import weights
from procurement_agent.extraction.anchors import AnchorHit
from procurement_agent.extraction.georgian import Case, lemma
from procurement_agent.extraction.labels import LABELS
from procurement_agent.extraction.normalize import QUOTE_CHARS, names_match, normalize_name
from procurement_agent.extraction.rules import RuleHit
from procurement_agent.extraction.schemas import DEFAULT_TARGETS, ExtractedFact, FactType
from procurement_agent.ingestion.chunker import Chunk

# Types that should have exactly one value per document; differing values are a conflict worth
# surfacing (this is the basis for the contract/AOA consistency checks in later phases).
SINGLE_VALUED: frozenset[FactType] = frozenset(
    {
        FactType.total_amount,
        FactType.contract_date,
        FactType.act_date,
        FactType.contract_number,
        FactType.warranty_period,
        FactType.validity_period,
        FactType.vat_status,
        FactType.cadastral_code,
    }
)

#: What a vat_status states (docs/GOLD_SPEC.md): the price includes VAT, excludes it, none
#: applies, or it is a net amount. Never a number.
VAT_STATUSES = frozenset({"incl", "excl", "none", "net"})

_WS = re.compile(r"\s+")
_ORDER = {t: i for i, t in enumerate(DEFAULT_TARGETS)}

# Unclassified rule hits are still worth showing when the user asked for that family of facts:
# an amount the rules could not label is better than a silently dropped amount.
_GENERIC_FAMILY: dict[FactType, frozenset[FactType]] = {
    FactType.amount: frozenset({FactType.price, FactType.total_amount, FactType.penalty}),
    FactType.date: frozenset(
        {
            FactType.contract_date,
            FactType.act_date,
            FactType.delivery_deadline,
            FactType.execution_period,
            FactType.validity_period,
        }
    ),
    FactType.duration: frozenset(
        {
            FactType.delivery_deadline,
            FactType.execution_period,
            FactType.warranty_period,
            FactType.validity_period,
            FactType.payment_terms,
        }
    ),
    FactType.percentage: frozenset({FactType.penalty, FactType.vat_status, FactType.payment_terms}),
    FactType.id_code: frozenset({FactType.party_id_code}),
}


#: Names stated in the nominative (``anchor_hits_to_facts``).
_NOMINATIVE_TYPES = frozenset({FactType.party_director, FactType.project_name})


def _key(f: ExtractedFact) -> str:
    if f.normalized is not None:
        return str(f.normalized)
    return _norm(f.value)


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip().lower()


#: Types whose values are names, where Georgian inflection is not a difference.
_NAME_TYPES: frozenset[FactType] = frozenset(
    {FactType.party_name, FactType.party_director, FactType.project_name}
)


#: Appended to every fact of a single-valued type that has more than one value.
CONFLICT_NOTE = "multiple different values found for this field"


def _same_fact(a: ExtractedFact, b: ExtractedFact) -> bool:
    """Two facts describe the same thing if their keys or shown values coincide.

    Overlapping quotes are supporting evidence only, never enough on their own. Merging on the
    quote alone collapsed two different parties named in one preamble into a single fact, and
    let a wrong total whose quote happened to contain the right one disappear into it - hiding
    the very conflict this module exists to report. So two facts with canonical values merge
    only when those values are equal, and two free-text facts with overlapping quotes merge only
    when one value contains the other ("თბილისი, რუსთაველის 1" and "ქ. თბილისი, რუსთაველის 1").
    """
    if a.type != b.type:
        return False
    if _key(a) == _key(b) or _norm(a.value) == _norm(b.value):
        return True
    if a.type in _NAME_TYPES and names_match(a.value, b.value):
        # "ნინო კაპანაძის" in the preamble and "ნინო კაპანაძე" in the signature
        # block are one signatory; two facts would be a false extra name on every document
        return True
    if a.normalized is not None and b.normalized is not None:
        return False  # both canonical and different: two values, perhaps a conflict
    qa, qb = _norm(a.raw_text), _norm(b.raw_text)
    if not (qa and qb and (qa in qb or qb in qa)):
        return False
    va, vb = normalize_name(a.value), normalize_name(b.value)
    return bool(va and vb) and (va in vb or vb in va)


def _add_note(fact: ExtractedFact, note: str) -> None:
    """Add a caveat without dropping the ones already there."""
    if not fact.note:
        fact.note = note
    elif note not in fact.note:
        fact.note = f"{fact.note}; {note}"


def rule_hits_to_facts(hits: dict[str, list[RuleHit]], chunks: list[Chunk]) -> list[ExtractedFact]:
    by_id = {c.id: c for c in chunks}
    header_id = chunks[0].id if chunks else None
    facts: list[ExtractedFact] = []
    for cid, hs in hits.items():
        chunk = by_id[cid]
        for h in hs:
            ftype, conf, note = h.type, h.confidence, h.note
            if ftype == FactType.vat_status and h.normalized not in VAT_STATUSES:
                # the rate next to "დღგ" ("+ 18% დღგ") says nothing about whether the price
                # includes the tax: as a fact it is a percentage
                ftype = FactType.percentage
            if ftype == FactType.date and cid == header_id:
                # An unlabeled date in the document header is almost always the document date
                # ("ქ. თბილისი, 2026 წლის 15 მარტი"); keep it, but say so.
                ftype, conf, note = (
                    FactType.contract_date,
                    weights.HEADER_DATE,
                    "date found in document header",
                )
            facts.append(
                ExtractedFact(
                    type=ftype,
                    label=LABELS[ftype][0],
                    value=h.value,
                    normalized=h.normalized,
                    unit=h.unit,
                    currency=h.currency,
                    raw_text=h.raw_text,
                    provenance=chunk.provenance,
                    source="rules",
                    confidence=conf,
                    note=note,
                )
            )
    return facts


def anchor_hits_to_facts(
    hits: dict[str, list[AnchorHit]], chunks: list[Chunk]
) -> list[ExtractedFact]:
    """Label-anchored values as facts. Highest confidence of any automatic source.

    A director or a project is stated in the nominative whatever case the label put it in:
    "დირექტორის ნინო კაპანაძის სახით" names "ნინო კაპანაძე", and "„მზის ქალაქისთვის“" the project
    "მზის ქალაქი". The quote keeps the document's own words.
    """
    by_id = {c.id: c for c in chunks}
    facts: list[ExtractedFact] = []
    for cid, hs in hits.items():
        chunk = by_id[cid]
        for h in hs:
            value, normalized = h.value, h.normalized
            if h.type in _NOMINATIVE_TYPES:
                cases = _project_cases(h.raw_text) if h.type == FactType.project_name else None
                value = normalized = lemma(h.value, cases=cases)
            facts.append(
                ExtractedFact(
                    type=h.type,
                    label=LABELS[h.type][0],
                    value=value,
                    normalized=normalized,
                    unit=h.unit,
                    raw_text=h.raw_text,
                    provenance=chunk.provenance,
                    source="anchor",
                    confidence=weights.ANCHOR,
                    note=h.note,
                )
            )
    return facts


#: The project word with a case ending of its own ("პროექტის", "პროექტი"), or bare ("პროექტ").
_PROJECT_HEAD = re.compile(r"\s*პრო[ეა]{1,3}ქტ([ა-ჰ]*)")
#: A name in apposition after an inflected head is a nominative ("პროექტის „ზღვის ხედი“
#: ფარგლებში"); some writers put it in the genitive or add a postposition, whose endings are
#: never a nominative's. An ergative, a dative or an adverbial is not written there, and reading
#: one cut "„მთის პანორამა“" to "მთის პანორაი" and "„ზღვის ექსპრეს“" to "ზღვის ექსპრე".
_APPOSITION_CASES: tuple[Case, ...] = (
    "gen", "gen_tvis", "gen_gan", "gen_ken", "dat_shi", "dat_ze", "dat_tan", "ins_dan", "adv_mde",
)  # fmt: skip
#: A quoted name before the project word is its attribute, a genitive: "„ზღვის ხედის“ პროექტი" -
#: or it carries the phrase's postposition itself ("„ზღვის ხედისთვის“ პროექტის ..."), which is
#: never a nominative's ending either. Never a dative: "„ზღვის ექსპრეს“" is a name.
_ATTRIBUTE_CASES: tuple[Case, ...] = _APPOSITION_CASES


def _project_cases(raw_text: str) -> tuple[Case, ...] | None:
    """The cases a project name can be in where the document writes it (``raw_text``, the words
    the anchor read). After the bare "პროექტ" the name carries the phrase's case itself
    ("პროექტ „ზღვის ხედისთვის“"), so any case; None when the place is not recognised."""
    stripped = raw_text.lstrip()
    if stripped[:1] and stripped[:1] in QUOTE_CHARS:
        return _ATTRIBUTE_CASES
    m = _PROJECT_HEAD.match(stripped)
    if m is None or not m.group(1):
        return None
    return _APPOSITION_CASES


#: Types whose values are codes a label names: one value is one type.
_CODE_TYPES = frozenset(
    {
        FactType.cadastral_code,
        FactType.bank_account,
        FactType.bank_code,
        FactType.party_id_code,
        FactType.contract_number,
    }
)


def _fold_code(value: str) -> str:
    return "".join(value.split()).upper()


def merge(
    rule_facts: list[ExtractedFact],
    llm_facts: list[ExtractedFact],
    targets: list[FactType],
    anchor_facts: list[ExtractedFact] | None = None,
) -> list[ExtractedFact]:
    wanted = set(targets)
    for generic, family in _GENERIC_FAMILY.items():
        if wanted & family:
            wanted.add(generic)
    merged: list[ExtractedFact] = []

    def add(f: ExtractedFact) -> None:
        if f.type not in wanted:
            return
        existing = next((m for m in merged if _same_fact(m, f)), None)
        if existing is None:
            merged.append(f)
            return
        # same fact from two sources (or repeated): raise confidence, keep first provenance
        if existing.source != f.source:
            existing.source = "both"
            existing.confidence = min(
                weights.CROSS_SOURCE_CAP,
                max(existing.confidence, f.confidence) + weights.CROSS_SOURCE_BONUS,
            )
        else:
            existing.confidence = min(
                weights.SAME_SOURCE_CAP, existing.confidence + weights.SAME_SOURCE_BONUS
            )
        if f.normalized is not None and existing.normalized is None:
            existing.normalized = f.normalized
        if f.note:
            # A note is a caveat about the value, not a property of the source that won. Losing
            # it here would silently discard warnings — "date found in document header", or a
            # value the verifier would not confirm — whenever some other source agreed.
            _add_note(existing, f.note)

    # anchors first: when two sources describe the same fact the first one keeps its value,
    # and a value read from its own label is the best evidence available
    for f in anchor_facts or []:
        add(f)
    for f in rule_facts:
        add(f)
    # a code the document's own labels name as one type is not the model's value for another:
    # the model has no type for a cadastral code in its list and files it as a contract number
    codes = {
        (_fold_code(f.value), f.type)
        for f in [*(anchor_facts or []), *rule_facts]
        if f.type in _CODE_TYPES
    }
    for f in llm_facts:
        if any(value == _fold_code(f.value) and t != f.type for value, t in codes):
            continue
        add(f)

    # conflicts among single-valued types
    by_type: dict[FactType, list[ExtractedFact]] = {}
    for f in merged:
        by_type.setdefault(f.type, []).append(f)
    for t, fs in by_type.items():
        if t in SINGLE_VALUED and len({_key(f) for f in fs}) > 1:
            for f in fs:
                f.conflict = True
                _add_note(f, CONFLICT_NOTE)

    return sorted(
        merged,
        key=lambda f: (_ORDER.get(f.type, 99), -f.confidence, f.provenance.page or 0),
    )
