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
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree  # type: ignore[import-untyped]

from procurement_agent.checks.findings import Finding
from procurement_agent.eval.gold import ExpectedFinding
from procurement_agent.extraction.normalize import (
    amounts_equal,
    names_match,
    normalize_name,
    parse_number,
)
from procurement_agent.extraction.schemas import (
    DEFAULT_TARGETS,
    ExtractedFact,
    ExtractionResult,
    FactType,
)
from procurement_agent.report.docx_annotate import UnsupportedDocumentError, annotate_docx

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
    #: The strict scores (``--strict``), kept apart so the lenient numbers above keep meaning
    #: what they meant in every earlier run report.
    strict: StrictExtractionScore | None = None
    strict_findings: FindingsScore | None = None

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


# --------------------------------------------------------------------------------------------
# Strict scoring (docs/GOLD_SPEC.md sections 1-4). Opt-in: ``scripts/evaluate.py --strict``.
#
# The lenient scorer above forgives inflection, substrings and display formats, which is what
# made its numbers look better than the product is. The strict one compares canonical values
# only, scores every target type for every document (a type the gold does not list is expected
# to be absent) and matches findings on their words and corrections, not just their rule ids.
# It deliberately depends on nothing in ``extraction/`` beyond the schema, so a change to the
# product can never move the yardstick it is measured with.
# --------------------------------------------------------------------------------------------

#: Every type strict scoring holds each document to.
STRICT_TARGETS: tuple[FactType, ...] = tuple(
    dict.fromkeys((*DEFAULT_TARGETS, FactType.act_date, FactType.bank_code))
)
#: Leftovers the rules could not classify: counted and reported, never scored.
GENERIC_TYPES = frozenset(
    {
        FactType.amount,
        FactType.date,
        FactType.percentage,
        FactType.duration,
        FactType.id_code,
        FactType.other,
    }
)
#: Types compared as numbers (money, and percentages such as a penalty rate).
_STRICT_NUMERIC = frozenset({FactType.price, FactType.total_amount, FactType.penalty})

_QUOTES = frozenset(
    map(chr, (0x201E, 0x201C, 0x201D, 0x22, 0x27, 0x2018, 0x2019, 0xAB, 0xBB, 0x201A))
)
_DASHES = str.maketrans(dict.fromkeys(map(chr, (0x2013, 0x2014, 0x2011, 0x2212)), "-"))
_SPACES = re.compile(r"\s+")
#: A canonical number, optionally a percentage, and whatever follows it ("0.5% per day").
_LEADING_NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)(%?)(.*)", re.DOTALL)
_NUMBER_TOLERANCE = 0.005
#: A subject matches when this share of the gold's words is in the fact...
_SUBJECT_GOLD_SHARE = 0.8
#: ...and this share of the fact's words is in the gold.
_SUBJECT_FACT_SHARE = 0.5
#: Georgian case endings, postpositions and plural forms, longest first. Only the subject rule
#: stems; every other type is compared exactly.
_GEORGIAN_SUFFIXES = tuple(
    sorted(
        (
            "ებისთვის", "ისათვის", "ისთვის", "ებისგან", "ისგან", "ებში", "ებზე", "ებით",
            "ების", "ებად", "ებმა", "ებს", "ები", "თვის", "ამდე", "მდე", "იდან", "დან",
            "თან", "კენ", "ში", "ზე", "ით", "ის", "ად", "მა", "ს", "ი", "ო", "ე", "ა",
        ),
        key=len,
        reverse=True,
    )
)  # fmt: skip
_MIN_STEM = 3


def _is_latin(ch: str) -> bool:
    return ch.isascii() or unicodedata.name(ch, "").startswith("LATIN")


def fold(s: str) -> str:
    """GOLD_SPEC section 1: NFC, quotes dropped, dashes unified, whitespace collapsed, Latin
    upper-cased. Georgian is left as it is: ``str.upper`` would turn Mkhedruli into Mtavruli."""
    s = unicodedata.normalize("NFC", s)
    s = "".join(ch for ch in s if ch not in _QUOTES).translate(_DASHES)
    s = _SPACES.sub(" ", s).strip()
    return "".join(ch.upper() if ch.isalpha() and _is_latin(ch) else ch for ch in s)


def canonical(fact: ExtractedFact) -> str:
    """The value strict scoring compares: the normalised form when there is one."""
    return str(fact.normalized) if fact.normalized is not None else fact.value


def _stem(word: str) -> str:
    if not any(0x10D0 <= ord(ch) <= 0x10FF for ch in word):
        return word
    for suffix in _GEORGIAN_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
            word = word[: -len(suffix)]
            break
    # "მომსახურება" and "მომსახურების" must meet: drop a plural-looking "-ებ" left behind
    if word.endswith("ებ") and len(word) - 2 >= _MIN_STEM:
        word = word[:-2]
    return word


def _stems(s: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"\w+", fold(s))]


def _subject_match(expected: str, actual: str) -> bool:
    gold, got = _stems(expected), _stems(actual)
    if not gold or not got:
        return False
    gold_set, got_set = set(gold), set(got)
    gold_share = sum(1 for w in gold if w in got_set) / len(gold)
    fact_share = sum(1 for w in got if w in gold_set) / len(got)
    return gold_share >= _SUBJECT_GOLD_SHARE and fact_share >= _SUBJECT_FACT_SHARE


def _numeric_match(expected: str, actual: str) -> bool | None:
    """Numbers compared as numbers, the rest of the value exactly; None when either side does
    not start with a canonical number (then the caller compares the folded strings)."""
    want, got = _LEADING_NUMBER.fullmatch(expected), _LEADING_NUMBER.fullmatch(actual)
    if want is None or got is None:
        return None
    return (
        abs(float(want.group(1)) - float(got.group(1))) <= _NUMBER_TOLERANCE
        and want.group(2) == got.group(2)
        and fold(want.group(3)) == fold(got.group(3))
    )


def strict_values_match(fact_type: FactType, expected: str, actual: str) -> bool:
    """Whether a canonical value is the gold's, per GOLD_SPEC section 2 (strict scoring)."""
    want, got = fold(expected), fold(actual)
    if fact_type == FactType.subject:
        return _subject_match(want, got)
    if fact_type in _STRICT_NUMERIC:
        numeric = _numeric_match(want, got)
        if numeric is not None:
            return numeric
    return want == got


@dataclass(slots=True)
class StrictFieldScore:
    fact_type: FactType
    expected: int = 0
    true_positives: int = 0
    false_positives: int = 0
    #: gold values nothing matched, and canonical values that matched no gold value
    missing: list[str] = field(default_factory=list)
    wrong: list[str] = field(default_factory=list)

    @property
    def false_negatives(self) -> int:
        return max(0, self.expected - self.true_positives)

    @property
    def precision(self) -> float | None:
        """None when nothing was returned: there is no precision to speak of."""
        returned = self.true_positives + self.false_positives
        return self.true_positives / returned if returned else None

    @property
    def recall(self) -> float | None:
        """None when the gold expects nothing of this type."""
        return self.true_positives / self.expected if self.expected else None

    @property
    def correct(self) -> bool:
        return self.false_positives == 0 and self.false_negatives == 0

    def add(self, other: StrictFieldScore) -> None:
        self.expected += other.expected
        self.true_positives += other.true_positives
        self.false_positives += other.false_positives


@dataclass(slots=True)
class StrictExtractionScore:
    name: str
    fields: dict[FactType, StrictFieldScore] = field(default_factory=dict)
    #: types the gold marks as labelled rather than derived
    provisional: set[FactType] = field(default_factory=set)
    ignored: list[FactType] = field(default_factory=list)
    #: generic facts (amount, date, ...) the extractor could not classify, per type
    unclassified: dict[FactType, int] = field(default_factory=dict)

    @property
    def unclassified_total(self) -> int:
        return sum(self.unclassified.values())


def score_extraction_strict(
    result: ExtractionResult,
    expected: dict[FactType, list[str]],
    name: str,
    *,
    ignore: Sequence[FactType] = (),
    provisional: Sequence[FactType] = (),
) -> StrictExtractionScore:
    """Score every target type: listed types against their values, unlisted ones as absent."""
    ignored = set(ignore)
    score = StrictExtractionScore(
        name=name,
        provisional=set(provisional) - ignored,
        ignored=sorted(ignored, key=lambda t: t.value),
    )
    by_type: dict[FactType, list[ExtractedFact]] = {}
    for fact in result.facts:
        if fact.type in GENERIC_TYPES:
            score.unclassified[fact.type] = score.unclassified.get(fact.type, 0) + 1
        else:
            by_type.setdefault(fact.type, []).append(fact)

    targets = [*STRICT_TARGETS, *(t for t in expected if t not in STRICT_TARGETS)]
    for fact_type in targets:
        if fact_type in ignored or fact_type in GENERIC_TYPES:
            continue
        wanted = expected.get(fact_type, [])
        unmatched = list(by_type.get(fact_type, []))
        fs = StrictFieldScore(fact_type=fact_type, expected=len(wanted))
        for want in wanted:
            hit = next(
                (f for f in unmatched if strict_values_match(fact_type, want, canonical(f))),
                None,
            )
            if hit is None:
                fs.missing.append(want)
            else:
                unmatched.remove(hit)
                fs.true_positives += 1
        fs.false_positives = len(unmatched)
        fs.wrong = [canonical(f) for f in unmatched]
        score.fields[fact_type] = fs
    return score


# -- findings ---------------------------------------------------------------------------------

#: How a finding is described in a report: rule, severity, the words it points at, its fix.
_DESCRIBE_LIMIT = 160


def _short(s: str | None) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= _DESCRIBE_LIMIT else s[: _DESCRIBE_LIMIT - 1] + "…"


def describe_finding(finding: Finding, label: str | None = None) -> str:
    where = finding.quote or finding.actual
    parts = [f"{finding.rule_id} [{finding.severity}]"]
    if label:
        parts.append(f"label={label!r}")
    if where is not None:
        parts.append(f"at {_short(where)!r}")
    if finding.expected is not None:
        parts.append(f"expected={_short(finding.expected)!r}")
    parts.append(f"({_short(finding.message_en)})")
    return " ".join(parts)


def describe_entry(entry: ExpectedFinding) -> str:
    parts = [entry.rule]
    for key in ("severity", "label", "text", "correction", "before", "after", "occurrences"):
        value = getattr(entry, key)
        if value is not None:
            parts.append(f"{key}={value!r}")
    return " ".join(parts)


def finding_matches_entry(entry: ExpectedFinding, finding: Finding, label: str | None) -> bool:
    """GOLD_SPEC sections 3-4: the rule id, and every property the entry states."""
    if finding.rule_id != entry.rule:
        return False
    if entry.severity is not None and finding.severity != entry.severity:
        return False
    if entry.text is not None and fold(entry.text) not in fold(
        finding.quote or finding.actual or ""
    ):
        return False
    if entry.correction is not None and (
        finding.expected is None or fold(finding.expected) != fold(entry.correction)
    ):
        return False
    if entry.label is not None and (label is None or fold(label) != fold(entry.label)):
        return False
    if entry.before is not None and not same_value(entry.before, finding.expected):
        return False
    return entry.after is None or same_value(entry.after, finding.actual)


#: A reading added after a value for the reader: "ორი ათას ოთხასი (2400)".
_GLOSS = re.compile(r"\s*\([^()]*\)\s*$")
#: A figure as a finding writes it, grouped with commas: "2,400.00 GEL".
_FIGURE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _figure(s: str) -> float | None:
    m = _FIGURE.search(s)
    return float(m.group(0).replace(",", "")) if m else None


def same_value(gold: str, reported: str | None) -> bool:
    """A compare finding's value against the gold's (GOLD_SPEC section 4): the same text after
    folding, the same text once the finding's gloss is set aside ("ორი ათას ოთხასი (2400)"),
    or - where the gold states a bare number - the same number ("2,400.00 GEL" is 2400)."""
    if reported is None:
        return False
    want, got = fold(gold), fold(reported)
    if want in (got, fold(_GLOSS.sub("", reported))):
        return True
    bare = _LEADING_NUMBER.fullmatch(want)
    if bare is None or bare.group(3).strip():
        return False
    number = _figure(got)
    if number is None or abs(number - float(bare.group(1))) > _NUMBER_TOLERANCE:
        return False
    return (bare.group(2) == "%") == ("%" in got)  # 50% is not 50 GEL


def _assign(candidates: list[list[int]], n_findings: int) -> list[int | None]:
    """A maximum one-to-one assignment of entries to findings (Kuhn's augmenting paths), so a
    broad entry can never take the only finding a more specific entry could have matched."""
    owner: list[int | None] = [None] * n_findings

    def augment(entry: int, seen: set[int]) -> bool:
        for f in candidates[entry]:
            if f in seen:
                continue
            seen.add(f)
            current = owner[f]
            if current is None or augment(current, seen):
                owner[f] = entry
                return True
        return False

    for entry in range(len(candidates)):
        augment(entry, set())
    assigned: list[int | None] = [None] * len(candidates)
    for finding_index, owning_entry in enumerate(owner):
        if owning_entry is not None:
            assigned[owning_entry] = finding_index
    return assigned


@dataclass(slots=True)
class FindingsScore:
    #: (expected entry, the finding that matched it)
    matched: list[tuple[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: reported, expected by nothing, not allowed (only with ``strict_findings``)
    unexpected: list[str] = field(default_factory=list)
    #: a second finding for an entry already matched: one mistake is one finding
    duplicates: list[str] = field(default_factory=list)
    #: reported under an ``allowed_findings`` rule and expected by nothing
    allowed: list[str] = field(default_factory=list)
    #: entries whose finding marked a different number of places in the Word file
    occurrence_errors: list[str] = field(default_factory=list)
    #: why the findings could not be scored at all (e.g. no reference document)
    unscorable: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.unscorable is None
            and not self.missing
            and not self.unexpected
            and not self.duplicates
            and not self.occurrence_errors
        )


#: Counts the Word comments a finding produces whose anchored text contains the entry's text;
#: None when that cannot be checked (not a .docx).
OccurrenceCounter = Callable[[ExpectedFinding, Finding], "int | None"]


def score_findings_strict(
    entries: Sequence[ExpectedFinding],
    reported: Sequence[Finding],
    *,
    labels: Sequence[str | None] | None = None,
    strict: bool = False,
    allowed: Sequence[str] = (),
    count_occurrences: OccurrenceCounter | None = None,
) -> FindingsScore:
    """Match expected entries to reported findings, one to one."""
    labels = list(labels) if labels is not None else [None] * len(reported)
    candidates = [
        [i for i, f in enumerate(reported) if finding_matches_entry(e, f, labels[i])]
        for e in entries
    ]
    assigned = _assign(candidates, len(reported))
    score = FindingsScore()
    used = {i for i in assigned if i is not None}
    for entry, index in zip(entries, assigned, strict=True):
        if index is None:
            score.missing.append(describe_entry(entry))
            continue
        finding = reported[index]
        score.matched.append((describe_entry(entry), describe_finding(finding, labels[index])))
        if entry.occurrences is not None and count_occurrences is not None:
            marked = count_occurrences(entry, finding)
            if marked is None:
                score.occurrence_errors.append(f"{describe_entry(entry)}: could not annotate")
            elif marked != entry.occurrences:
                score.occurrence_errors.append(
                    f"{describe_entry(entry)}: {marked} place(s) marked, "
                    f"expected {entry.occurrences}"
                )
    allowed_rules = set(allowed)
    for i, finding in enumerate(reported):
        if i in used:
            continue
        text = describe_finding(finding, labels[i])
        if any(i in c for c in candidates):
            if strict:
                score.duplicates.append(text)
        elif finding.rule_id in allowed_rules:
            score.allowed.append(text)
        elif strict:
            score.unexpected.append(text)
    return score


# -- Word comments ----------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def comment_anchors(docx: Path) -> dict[str, str]:
    """Each comment id in ``word/document.xml`` and the folded text its range covers."""
    with zipfile.ZipFile(docx) as z:
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        root = etree.fromstring(z.read("word/document.xml"), parser)
    open_ranges: dict[str, list[str]] = {}
    done: dict[str, str] = {}
    for event, el in etree.iterwalk(root, events=("start", "end")):
        tag = el.tag
        if event == "start":
            if tag == f"{_W}commentRangeStart":
                open_ranges[el.get(f"{_W}id", "")] = []
            elif tag == f"{_W}commentRangeEnd":
                cid = el.get(f"{_W}id", "")
                if cid in open_ranges:
                    done[cid] = fold("".join(open_ranges.pop(cid)))
            elif tag in (f"{_W}t", f"{_W}delText"):
                for parts in open_ranges.values():
                    parts.append(el.text or "")
            elif tag in (f"{_W}tab", f"{_W}br"):
                for parts in open_ranges.values():
                    parts.append(" ")
        elif tag == f"{_W}p":
            for parts in open_ranges.values():
                parts.append(" ")
    return done


def count_marked_places(source: Path, findings: Sequence[Finding], text: str | None) -> int | None:
    """Annotate ``source`` with ``findings`` exactly as the product does (no ``text=``, like
    ``api/checks.py``) into a temporary directory, and count the comments whose anchored text
    contains ``text`` (every comment when ``text`` is None). None when it cannot annotate."""
    if source.suffix.lower() != ".docx":
        return None
    with tempfile.TemporaryDirectory(prefix="eval-annotate-") as tmp:
        out = Path(tmp) / "annotated.docx"
        try:
            annotate_docx(source, findings, out)
        except UnsupportedDocumentError:
            return None
        anchors = comment_anchors(out)
    if text is None:
        return len(anchors)
    needle = fold(text)
    return sum(1 for anchored in anchors.values() if needle in anchored)


@dataclass(slots=True)
class StrictTotals:
    """Strict per-field totals over a set, derived and provisional kept apart."""

    derived: dict[FactType, StrictFieldScore] = field(default_factory=dict)
    provisional: dict[FactType, StrictFieldScore] = field(default_factory=dict)
    unclassified: dict[FactType, int] = field(default_factory=dict)
    documents: int = 0


def strict_totals(documents: Sequence[DocumentScore]) -> StrictTotals:
    totals = StrictTotals()
    for doc in documents:
        if doc.strict is None:
            continue
        totals.documents += 1
        for fact_type, fs in doc.strict.fields.items():
            bucket = totals.provisional if fact_type in doc.strict.provisional else totals.derived
            bucket.setdefault(fact_type, StrictFieldScore(fact_type=fact_type)).add(fs)
        for fact_type, n in doc.strict.unclassified.items():
            totals.unclassified[fact_type] = totals.unclassified.get(fact_type, 0) + n
    return totals
