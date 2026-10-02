"""Measure client rules 3 and 4 end to end on acts written from scratch.

    uv run --offline python scripts/act_rules_accuracy.py --per-class 200 --seed 7

Each case writes an act - invented parties and project, in one of three layouts (prose, "label:
value" lines, numbered sections), its dates in one of three styles, its work done with one of five
verbs - and plants one class of case:

* rule 3: the project named in quotes or after its label without them ("project_quoted",
  "project_labelled"), or not at all ("project_missing", a warning); the cadastral code dotted
  ("cadastral_ok"), written with dashes ("cadastral_dashed", a warning whose correction is the
  dotted code), with a digit group wrong ("cadastral_malformed", a warning), or left out
  ("cadastral_missing", a warning);
* rule 4: one completion date and the act dated that day ("single_ok") or another
  ("single_off", an error whose correction is that day); a whole month and the act dated its last
  day ("month_ok") or another ("month_off", corrected to the month's end); part of a month and the
  act dated the period's own last day ("partial_end_ok") or the month's ("partial_monthend_ok"),
  or another day ("partial_off", corrected to the month's end).

A case passes when the findings of AOA.PROJECT_PRESENT, AOA.CADASTRAL_PRESENT and AOA.ACT_DATE
are exactly the expected (rule id, severity) pairs, and a correction names the expected value
(a date read back with the application's own date reader). Every name is invented.
"""

from __future__ import annotations

import argparse
import calendar
import random
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.extraction.rules import find_dates

#: nominative, dative ("on"), ablative ("from"), genitive (before "ჩათვლით")
MONTHS = (
    ("იანვარი", "იანვარს", "იანვრიდან", "იანვრის"),
    ("თებერვალი", "თებერვალს", "თებერვლიდან", "თებერვლის"),
    ("მარტი", "მარტს", "მარტიდან", "მარტის"),
    ("აპრილი", "აპრილს", "აპრილიდან", "აპრილის"),
    ("მაისი", "მაისს", "მაისიდან", "მაისის"),
    ("ივნისი", "ივნისს", "ივნისიდან", "ივნისის"),
    ("ივლისი", "ივლისს", "ივლისიდან", "ივლისის"),
    ("აგვისტო", "აგვისტოს", "აგვისტოდან", "აგვისტოს"),
    ("სექტემბერი", "სექტემბერს", "სექტემბრიდან", "სექტემბრის"),
    ("ოქტომბერი", "ოქტომბერს", "ოქტომბრიდან", "ოქტომბრის"),
    ("ნოემბერი", "ნოემბერს", "ნოემბრიდან", "ნოემბრის"),
    ("დეკემბერი", "დეკემბერს", "დეკემბრიდან", "დეკემბრის"),
)
PROJECTS = (
    "მზის ქალაქი",
    "ზღვის ხედი",
    "მთის რეზიდენცია",
    "ახალი უბანი",
    "პარკის ბინები",
    "ვაზის ბაღი",
    "ცისფერი ტბა",
)
#: the subject and the verb a sentence about the work done takes
VERBS = (
    ("მომსახურება", "შესრულდა"),
    ("სამუშაოები", "დასრულდა"),
    ("მომსახურება", "გაიწია"),
    ("სამუშაოები", "ჩაბარდა"),
    ("მომსახურება", "გაწეულია"),
)
CLASSES = (
    "project_quoted",
    "project_labelled",
    "project_missing",
    "cadastral_ok",
    "cadastral_dashed",
    "cadastral_malformed",
    "cadastral_missing",
    "single_ok",
    "single_off",
    "month_ok",
    "month_off",
    "partial_end_ok",
    "partial_monthend_ok",
    "partial_off",
)
EXPECTED: dict[str, set[tuple[str, str]]] = {
    "project_missing": {("AOA.PROJECT_PRESENT", "warning")},
    "cadastral_dashed": {("AOA.CADASTRAL_PRESENT", "warning")},
    "cadastral_malformed": {("AOA.CADASTRAL_PRESENT", "warning")},
    "cadastral_missing": {("AOA.CADASTRAL_PRESENT", "warning")},
    "single_off": {("AOA.ACT_DATE", "error")},
    "month_off": {("AOA.ACT_DATE", "error")},
    "partial_off": {("AOA.ACT_DATE", "error")},
}
RULES = frozenset({"AOA.PROJECT_PRESENT", "AOA.CADASTRAL_PRESENT", "AOA.ACT_DATE"})


def _day(d: date, style: str, case: int) -> str:
    """A date in one of the act's styles; ``case`` indexes ``MONTHS`` (0 nominative, 1 dative,
    2 ablative, 3 genitive) and is ignored by the numeric style."""
    month = MONTHS[d.month - 1][case]
    if style == "numeric":
        return f"{d:%d.%m.%Y}"
    if style == "day_first":
        return f"{d.day} {month}, {d.year} წ."
    return f"{d.year} წლის {d.day} {month}"


def _last(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


@dataclass(frozen=True)
class Case:
    cls: str
    act: str
    correction: str | None  # an ISO date, or the dotted cadastral code


def _code(rng: random.Random) -> str:
    zone, sector, quarter = (rng.randrange(1, 99) for _ in range(3))
    return f"{zone:02d}.{sector:02d}.{quarter:02d}.{rng.randrange(1, 999):03d}"


def make_case(rng: random.Random, cls: str) -> Case:
    layout = rng.choice(("prose", "labels", "numbered"))
    style = rng.choice(("long", "numeric", "day_first"))
    subject, verb = rng.choice(VERBS)
    project = rng.choice(PROJECTS)
    code = _code(rng)
    month_start = date(2026, rng.randrange(1, 12), 1)
    correction: str | None = None
    period: tuple[date, date] | None = None
    if cls.startswith(("month", "partial")):
        if cls.startswith("month"):
            start, end = month_start, _last(month_start)
        else:
            start = month_start + timedelta(days=rng.randrange(0, 6))
            end = start + timedelta(days=rng.randrange(7, 20))
        period = (start, end)
        act_day = _last(end) if cls in ("month_ok", "partial_monthend_ok") else end
        if cls in ("month_off", "partial_off"):
            act_day = end - timedelta(days=rng.randrange(1, 6))
            correction = _last(end).isoformat()
        done = end
    else:
        done = month_start + timedelta(days=rng.randrange(0, 27))
        act_day = done
        if cls == "single_off":
            act_day = done + timedelta(days=rng.randrange(1, 9))
            correction = done.isoformat()
    if period is None:
        work = f"{subject} {verb} {_day(done, style, 1)}."
    elif style == "numeric":
        span = f"{_day(period[0], style, 0)} - {_day(period[1], style, 0)}"
        work = f"{subject} {verb} {span} პერიოდში."
    else:
        work = f"{subject} {verb} {_day(period[0], style, 2)} {_day(period[1], style, 3)} ჩათვლით."
    signed = _day(act_day, style, 0)
    cadastral = f"(ს/კ: {code})"
    if cls == "cadastral_dashed":
        cadastral = f"(ს/კ: {code.replace('.', '-')})"
        correction = code
    elif cls == "cadastral_malformed":
        groups = code.split(".")
        groups[2] = groups[2][1:]  # one digit short
        cadastral = f"(ს/კ: {'.'.join(groups)})"
    elif cls == "cadastral_missing":
        cadastral = ""
    project_text = f"„შემკვეთის“ პროექტ „{project}“ {cadastral} მომსახურების გაწევა."
    if cls == "project_labelled":
        project_text = f"პროექტის დასახელება: {project} {cadastral}"
    elif cls == "project_missing":
        project_text = f"მომსახურების გაწევა {cadastral}".strip()
    parties = (
        "შპს „ალფა ჯგუფი“, საიდენტიფიკაციო კოდი: 123456789 (შემდგომში „შემკვეთი“) და\n"
        "შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321 (შემდგომში „შემსრულებელი“)"
    )
    fee = "საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით."
    if layout == "labels":
        body = (
            f"მიღება-ჩაბარების აქტი\n{parties}\n{project_text}\nშესრულება: {work}\n"
            f"საფასური: 100 (ასი) ლარი, გადასახადების ჩათვლით.\nაქტის თარიღი: {signed}\n"
        )
    elif layout == "numbered":
        body = (
            f"მიღება-ჩაბარების აქტი\n{parties}\n1. {project_text}\n2. {work}\n3. {fee}\n"
            f"4. აქტი გაფორმდა {signed}\n"
        )
    else:
        body = (
            f"მიღება-ჩაბარების აქტი\n{parties}\n{project_text}\n{work} {fee}\n"
            f"აქტი გაფორმდა {signed}\n"
        )
    return Case(f"{cls}/{layout}/{style}", body, correction)


def score(case: Case) -> str | None:
    cls = case.cls.split("/")[0]
    report = check_aoa(aoa_text=case.act)
    got = [f for f in report.findings if f.rule_id in RULES]
    pairs = {(f.rule_id, f.severity) for f in got}
    if pairs != EXPECTED.get(cls, set()):
        skipped = [s.rule_id for s in report.skipped if s.rule_id in RULES]
        return f"got {sorted(pairs)} skipped {skipped}"
    if case.correction is not None:
        values = [f.expected or "" for f in got]
        if cls.startswith("cadastral"):
            ok = case.correction in values
        else:
            ok = any(case.correction in [h.normalized for h in find_dates(v)] for v in values)
        if not ok:
            return f"correction {values} is not {case.correction}"
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
    parser = argparse.ArgumentParser(description="rules 3 and 4 accuracy on random acts")
    parser.add_argument("--per-class", type=int, default=50)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--show", type=int, default=3)
    args = parser.parse_args(argv)
    passed, failures = run(args.per_class, args.seed)
    total = args.per_class * len(CLASSES)
    print(f"rules 3 and 4: {sum(passed.values())} of {total} cases right")
    for cls in CLASSES:
        print(f"  {cls:20s} {passed[cls]:5d} / {args.per_class}")
    reasons = Counter(f"{c.cls}: {w[:80]}" for c, w in failures)
    for reason, n in reasons.most_common(15):
        print(f"  {n:4d}  {reason}")
    for case, why in failures[: args.show]:
        print(f"--- {case.cls}: {why}\n{case.act}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
