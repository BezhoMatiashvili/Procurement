"""Scoring: how close an extraction or a check came to the ground truth.

Three numbers are reported per field, not one:

* **anchor hit rate** — did the deterministic label anchor fire at all,
* **precision** — of what we returned, how much was right,
* **recall** — of what was there, how much we found.

Anchor hit rate is separate on purpose. The anchors were designed by reading one family of
client documents, and a set of regexes tuned to one template can score well on it while being
useless on the next one. Tracking how often the anchors fire, apart from whether the answer was
right, is what makes that overfitting visible instead of silent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from procurement_agent.extraction.normalize import (
    amounts_equal,
    names_match,
    normalize_name,
    parse_number,
)
from procurement_agent.extraction.schemas import ExtractedFact, ExtractionResult, FactType

#: Types compared as money rather than as text.
_NUMERIC = frozenset({FactType.price, FactType.total_amount, FactType.amount, FactType.quantity})
#: Types where inflection must not count as a difference.
_NAMES = frozenset({FactType.party_name, FactType.party_director, FactType.project_name})


#: A value that is a date, and nothing else.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
#: Every number in a string, however it is grouped: "48 750,00", "0,1", "24".
_NUMBER = re.compile(r"\d{1,3}(?:[ \u00a0,.]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
#: A free-text value counts as another one it is contained in only if it is at least this share
#: of it. "სილქ თაუნი" inside "სილქ თაუნის პროექტი" is the same project; "თბილისი" inside a
#: full street address is not the same address.
_MIN_TEXT_RATIO = 0.5


def _bare_number(s: str) -> float | None:
    """The value of ``s`` if it is a single number or percentage and nothing else.

    The shape is checked before parsing: ``parse_number`` strips separators generously, and
    would read a cadastral code such as "01.02.03.456" as one large number.
    """
    core = s.strip().removesuffix("%").strip()
    return parse_number(core) if _NUMBER.fullmatch(core) else None


def values_match(fact_type: FactType, expected: str, actual: str) -> bool:
    """Whether an extracted value counts as the expected one.

    Deliberately the same notions of sameness the pipeline itself uses, so that the scorer and
    the deduplicator can never disagree about what "the same value" means.

    Numbers, percentages and dates are compared as values and never as substrings: a substring
    test credited "5%" for an expected "15%" and "2026-03-1" for "2026-03-15". Free text is
    still allowed to match inside a longer value, but only when it is a substantial part of it.
    """
    if fact_type in _NUMERIC:
        want, got = parse_number(expected), parse_number(actual)
        if want is not None and got is not None:
            return amounts_equal(want, got)
        if want is not None:
            # a display form such as "48 750,00 GEL" is not a number; ``fact_matches`` scores
            # the fact's canonical value next, which is where money is compared
            return False
    if _ISO_DATE.fullmatch(expected.strip()):
        return set(_ISO_DATE.findall(actual)) == {expected.strip()}
    want_number = _bare_number(expected)
    if want_number is not None:
        # "24" against "24 months" or "P24M": the one number the value states must be it
        found = {parse_number(m) for m in _NUMBER.findall(actual)}
        return len(found) == 1 and amounts_equal(want_number, found.pop())
    if fact_type in _NAMES and names_match(expected, actual):
        return True
    want_text, got_text = normalize_name(expected), normalize_name(actual)
    if not want_text or not got_text:
        return False
    if want_text == got_text:
        return True
    shorter, longer = sorted((want_text, got_text), key=len)
    if len(shorter) < _MIN_TEXT_RATIO * len(longer):
        return False
    # From a word start, and never stopping inside a number: "2026 03 1" must not be found in
    # "2026 03 15". A letter may follow, because Georgian inflects names by suffix -
    # "სილქ თაუნი" is the same project as "სილქ თაუნის პროექტი".
    return re.search(rf"(?<!\w){re.escape(shorter)}(?!\d)", longer) is not None


def fact_matches(fact_type: FactType, expected: str, fact: ExtractedFact) -> bool:
    """Compare against the fact's canonical form as well as its display form.

    A displayed amount reads "48 750,00 GEL"; the canonical 48750.0 is what the gold file
    states. Scoring only the display string marks correct extractions wrong and would have
    sent us optimising a number that was never broken.
    """
    if values_match(fact_type, expected, fact.value):
        return True
    if fact.normalized is not None:
        return values_match(fact_type, expected, str(fact.normalized))
    return False


@dataclass(slots=True)
class FieldScore:
    fact_type: FactType
    expected: int = 0
    true_positives: int = 0
    false_positives: int = 0
    anchored: bool = False

    @property
    def false_negatives(self) -> int:
        return max(0, self.expected - self.true_positives)

    @property
    def precision(self) -> float:
        returned = self.true_positives + self.false_positives
        return self.true_positives / returned if returned else 0.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.expected if self.expected else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


@dataclass(slots=True)
class DocumentScore:
    name: str
    fields: dict[FactType, FieldScore] = field(default_factory=dict)
    missing_findings: list[str] = field(default_factory=list)
    unexpected_findings: list[str] = field(default_factory=list)
    llm_calls: int = 0
    choice_calls: int = 0
    #: How many chunks the selector sent, and how many earned it. The gap is what a semantic
    #: gate would have to beat to be worth its own prefill per chunk.
    chunks_selected: int = 0
    chunks_with_facts: int = 0
    seconds: float = 0.0

    @property
    def findings_ok(self) -> bool:
        return not self.missing_findings and not self.unexpected_findings


def score_extraction(
    result: ExtractionResult, expected: dict[FactType, list[str]], name: str
) -> DocumentScore:
    score = DocumentScore(
        name=name,
        llm_calls=result.llm_calls,
        choice_calls=result.llm_choice_calls,
        chunks_selected=result.chunks_selected,
        chunks_with_facts=result.chunks_with_facts,
        seconds=result.seconds,
    )
    by_type: dict[FactType, list[ExtractedFact]] = {}
    for fact in result.facts:
        by_type.setdefault(fact.type, []).append(fact)

    for fact_type, wanted in expected.items():
        found = by_type.get(fact_type, [])
        fs = FieldScore(
            fact_type=fact_type,
            expected=len(wanted),
            anchored=any(f.source in ("anchor", "both") for f in found),
        )
        unmatched = list(found)
        for want in wanted:
            hit = next((f for f in unmatched if fact_matches(fact_type, want, f)), None)
            if hit is not None:
                unmatched.remove(hit)
                fs.true_positives += 1
        fs.false_positives = len(unmatched)
        score.fields[fact_type] = fs
    return score


def score_findings(
    reported: list[str], expected: list[str], forbidden: list[str], *, must_be_clean: bool
) -> tuple[list[str], list[str]]:
    """Returns (missing, unexpected) rule ids."""
    reported_set = set(reported)
    missing = sorted(set(expected) - reported_set)
    unexpected = sorted(reported_set) if must_be_clean else sorted(reported_set & set(forbidden))
    return missing, unexpected


@dataclass(slots=True)
class SetScore:
    name: str
    documents: list[DocumentScore] = field(default_factory=list)

    def by_field(self) -> dict[FactType, FieldScore]:
        totals: dict[FactType, FieldScore] = {}
        for doc in self.documents:
            for fact_type, fs in doc.fields.items():
                agg = totals.setdefault(fact_type, FieldScore(fact_type=fact_type))
                agg.expected += fs.expected
                agg.true_positives += fs.true_positives
                agg.false_positives += fs.false_positives
                agg.anchored = agg.anchored or fs.anchored
        return totals

    def anchor_hit_rate(self, fact_type: FactType) -> float:
        seen = [d.fields[fact_type] for d in self.documents if fact_type in d.fields]
        return sum(1 for f in seen if f.anchored) / len(seen) if seen else 0.0

    @property
    def clean(self) -> bool:
        return all(d.findings_ok for d in self.documents)
