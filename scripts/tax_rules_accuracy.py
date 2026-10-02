"""Measure client rules 5-8 end to end: the tax wording an act must carry, by contractor type.

    uv run --offline python scripts/tax_rules_accuracy.py --per-class 200 --seed 7

Each case writes an act with an invented contractor of one type - a natural person ("ფ/პ", a
personal number), an individual entrepreneur paying VAT or a small business ("ი/მ"), a foreign
organisation - and, where the class says so, a company. The type is given to the check, or left
for it to read from the act. The fee is worded right for the type, wrongly, or not at all, in one
of several phrasings, sometimes with an advance line worded too. A case passes when the
AOA.TAX_WORDING findings are exactly the expected severities - and, where the type is left to the
act, when the check reads the type it should, or none where the act does not say it.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from procurement_agent.checks.aoa import check_aoa

FIRST = ("გიორგი", "ნინო", "დავით", "ანა", "ლევან", "მარიამ", "ნიკა", "თამარ")
LAST = ("ბერიძე", "მელაძე", "გელაშვილი", "ლომიძე", "ხარაბაძე", "კაპანაძე")
#: wording a type needs, wording that contradicts it, beside the fee
RIGHT = {
    "natural_person": (
        "NET",
        "NET (საშემოსავლო და საპენსიო გადასახადების გარეშე)",
        "საშემოსავლო და საპენსიო გადასახადების გარეშე",
    ),
    "individual_entrepreneur_vat": (
        "დღგ-ს ჩათვლით",
        "მათ შორის დღგ",
        "დღგ-ს გათვალისწინებით",
        "(including VAT)",
        "დამატებული ღირებულების გადასახადის ჩათვლით",
    ),
    "small_business": (
        "გადასახადების ჩათვლით",
        "ყველა გადასახადის ჩათვლით",
        "და ანგარიშსწორება ითვალისწინებს გადასახადებს (მათ შორის დღგ-ს)",
    ),
    "foreign": ("NET", "(NET amount)"),
}
WRONG = {
    "natural_person": ("გადასახადების ჩათვლით", "დღგ-ს ჩათვლით", "(gross, taxes included)"),
    "individual_entrepreneur_vat": ("დღგ-ს გარეშე", "გადასახადების გარეშე"),
    "small_business": ("გადასახადების გარეშე", "დღგ-ს გარეშე"),
    "foreign": (
        "დღგ-ს ჩათვლით",
        "გადასახადების ჩათვლით",
        "(VAT included)",
        "(including all taxes)",
    ),
}
CLASSES = (
    "right",
    "wrong",
    "missing",
    "advance_wrong",
    "small_vat_only",
    "vat_payer_says_not",
    "infer_right",
    "infer_wrong",
    "infer_company",
    "abstain_ie",
    "abstain_both",
)
TYPES = tuple(RIGHT)


@dataclass(frozen=True)
class Case:
    cls: str
    act: str
    given: str | None
    expected: frozenset[str]  # the severities of AOA.TAX_WORDING findings
    inferred: str | None  # the type the check must read from the act, when not given
    skip: bool  # the rule must say it could not run


def _contractor(rng: random.Random, kind: str, status: str = "") -> str:
    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    number = f"01{rng.randrange(10**8, 10**9)}"
    if kind == "natural_person":
        return f"ფ/პ {name}, პირადი ნომერი: {number}"
    if kind in ("individual_entrepreneur_vat", "small_business", "ie"):
        return f"ი/მ {name}{status}, საიდენტიფიკაციო კოდი: {number}"
    if kind == "company":
        return f"შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 4{rng.randrange(10**7, 10**8)}"
    return "„Nordwind Solutions“ GmbH, Germany, registration number HRB 12345"


def _act(contractor: str, wording: str, advance: str = "") -> str:
    fee = f"საფასური შეადგენს 1 000 (ათასი) ლარს {wording}".rstrip() + "."
    return f"""მიღება-ჩაბარების აქტი
შპს „ალფა ჯგუფი“, საიდენტიფიკაციო კოდი: 123456789 (შემდგომში „დამკვეთი“) და
{contractor} (შემდგომში „შემსრულებელი“)
1. „შემსრულებელმა“ გასწია საკონსულტაციო მომსახურება.
2. მომსახურება შესრულდა 2026 წლის 15 სექტემბერს.
3. {fee}{advance}
4. აქტი გაფორმდა 2026 წლის 15 სექტემბერი
"""


def make_case(rng: random.Random, cls: str) -> Case:
    kind = rng.choice(TYPES)
    if cls == "right":
        act = _act(_contractor(rng, kind), rng.choice(RIGHT[kind]))
        return Case(f"{cls}/{kind}", act, kind, frozenset(), None, False)
    if cls == "wrong":
        act = _act(_contractor(rng, kind), rng.choice(WRONG[kind]))
        return Case(f"{cls}/{kind}", act, kind, frozenset({"error"}), None, False)
    if cls == "missing":
        act = _act(_contractor(rng, kind), "")
        return Case(f"{cls}/{kind}", act, kind, frozenset({"error"}), None, False)
    if cls == "advance_wrong":
        kind = rng.choice(("natural_person", "individual_entrepreneur_vat", "small_business"))
        bad = rng.choice(WRONG[kind])
        advance = f" ავანსი 300 (სამასი) ლარი {bad} გადახდილია 2026 წლის 1 სექტემბერს."
        act = _act(_contractor(rng, kind), rng.choice(RIGHT[kind]), advance)
        return Case(f"{cls}/{kind}", act, kind, frozenset({"error"}), None, False)
    if cls == "small_vat_only":
        act = _act(_contractor(rng, "small_business"), "დღგ-ს ჩათვლით")
        return Case(cls, act, "small_business", frozenset({"warning"}), None, False)
    if cls == "vat_payer_says_not":
        contractor = _contractor(rng, "individual_entrepreneur_vat", " (არ არის დღგ-ს გადამხდელი)")
        act = _act(contractor, "დღგ-ს ჩათვლით")
        return Case(cls, act, "individual_entrepreneur_vat", frozenset({"warning"}), None, False)
    if cls in ("infer_right", "infer_wrong"):
        kind = rng.choice(("natural_person", "individual_entrepreneur_vat", "small_business"))
        status = {
            "individual_entrepreneur_vat": " (დღგ-ს გადამხდელი)",
            "small_business": " (მცირე მეწარმე)",
        }.get(kind, "")
        wording = rng.choice(RIGHT[kind] if cls == "infer_right" else WRONG[kind])
        act = _act(_contractor(rng, kind, status), wording)
        expected = frozenset() if cls == "infer_right" else frozenset({"error"})
        return Case(f"{cls}/{kind}", act, None, expected, kind, False)
    if cls == "infer_company":
        act = _act(_contractor(rng, "company"), rng.choice(("დღგ-ს ჩათვლით", "")))
        return Case(cls, act, None, frozenset(), "company", False)
    if cls == "abstain_ie":
        act = _act(_contractor(rng, "ie"), rng.choice(("დღგ-ს ჩათვლით", "გადასახადების ჩათვლით")))
        return Case(cls, act, None, frozenset(), None, True)
    # an entrepreneur said to be both a small business and a VAT payer: no guess
    contractor = _contractor(rng, "ie", " (მცირე მეწარმე, დღგ-ს გადამხდელი)")
    return Case(cls, _act(contractor, "დღგ-ს ჩათვლით"), None, frozenset(), None, True)


def score(case: Case) -> str | None:
    report = check_aoa(aoa_text=case.act, contractor_type=case.given)  # type: ignore[arg-type]
    got = frozenset(f.severity for f in report.findings if f.rule_id == "AOA.TAX_WORDING")
    if got != case.expected:
        return f"got {sorted(got)}"
    if case.given is None:
        read = (
            report.summary.get("contractor_type")
            if "contractor_type_inferred" in report.summary
            else None
        )
        if read != case.inferred:
            return f"read the type as {read}, expected {case.inferred}"
    skipped = any(s.rule_id == "AOA.TAX_WORDING" for s in report.skipped)
    if skipped != case.skip:
        return f"skip {skipped}, expected {case.skip}"
    return None


def run(per_class: int, seed: int) -> tuple[Counter[str], list[tuple[Case, str]]]:
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    passed: Counter[str] = Counter()
    failures: list[tuple[Case, str]] = []
    for cls in CLASSES:
        for _ in range(per_class):
            case = make_case(rng, cls)
            why = score(case)
            if why is None:
                passed[cls] += 1
            else:
                failures.append((case, why))
    return passed, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rules 5-8 accuracy on random acts")
    parser.add_argument("--per-class", type=int, default=50)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--show", type=int, default=3)
    args = parser.parse_args(argv)
    passed, failures = run(args.per_class, args.seed)
    total = args.per_class * len(CLASSES)
    print(f"rules 5-8: {sum(passed.values())} of {total} cases right")
    for cls in CLASSES:
        print(f"  {cls:20s} {passed[cls]:5d} / {args.per_class}")
    reasons = Counter(f"{c.cls}: {w}" for c, w in failures)
    for reason, n in reasons.most_common(15):
        print(f"  {n:4d}  {reason}")
    for case, why in failures[: args.show]:
        print(f"--- {case.cls} (given {case.given}): {why}\n{case.act}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
