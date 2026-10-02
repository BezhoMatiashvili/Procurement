"""Measure client rule 1 end to end: random parties, their registry extracts, the act's mistakes.

    uv run --offline python scripts/registry_accuracy.py --per-class 200 --seed 7

Each case invents two companies - name, legal form, identification code, legal address, one or two
authorised persons - writes each one's registry extract as the agency does (the 2011 layout or the
2025 one, chosen at random), reads it back with the application's own reader
(``registry.extract``), and writes an act for the two parties in an act's style: the legal form
abbreviated, the name quoted, the address without the country or the district, the director in
the genitive in the preamble and in the nominative under the signature. Then one class of case:

* clean, or harmless: the act exactly as the registry has it ("clean"); the signature written
  surname first ("surname_first"); the registry's second authorised person signing
  ("second_person") - no registry finding allowed;
* a mistake planted in one party: a letter of the name changed ("name_slip"), another legal form
  ("legal_form"), the house number changed ("addr_number"), a letter of the street changed
  ("addr_street"), the flat the registry lists left out ("addr_short", a warning), a letter of the
  director's surname changed wherever it is written ("director_slip"), the registry recording a
  liquidation ("status", a warning).

A case passes when the registry findings are exactly the expected (rule id, severity) pairs - for
the party the mistake is in, nothing else - and an error's correction names what the registry
writes. Every name is invented; nothing is read from any client document.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.registry.extract import parse_extract
from procurement_agent.registry.snapshot import RegistryEntry
from procurement_agent.registry.source import RegistrySource

FIRST = {  # nominative -> genitive
    "გიორგი": "გიორგის",
    "ნინო": "ნინოს",
    "დავით": "დავითის",
    "ანა": "ანას",
    "ლევან": "ლევანის",
    "მარიამ": "მარიამის",
    "ნიკა": "ნიკას",
    "თამარ": "თამარის",
    "ზურაბ": "ზურაბის",
    "ეკა": "ეკას",
    "ირაკლი": "ირაკლის",
    "სოფო": "სოფოს",
}
SURNAME_STEMS = ("ბერი", "მელა", "კაპანა", "ლომი", "ჯაფარი", "გელა", "ხარაბა", "ქავთარა")
SURNAME_ENDINGS = (("ძე", "ძის"), ("შვილი", "შვილის"))
NAME_WORDS = (
    "ალფა",
    "ბეტა",
    "გამა",
    "დელტა",
    "ომეგა",
    "სიგმა",
    "ზენიტი",
    "ორბიტა",
    "ვექტორი",
    "ფორტუნა",
    "ნავიგატორი",
    "პროგრესი",
    "ტრიუმფი",
    "ჰორიზონტი",
)
NAME_SECOND = (
    "ჯგუფი",
    "სერვისი",
    "ტრეიდი",
    "კონსალტინგი",
    "ინჟინერინგი",
    "დეველოპმენტი",
    "ლოჯისტიკა",
    "ჰოლდინგი",
)
CITIES = ("თბილისი", "ბათუმი", "ქუთაისი", "რუსთავი", "ზუგდიდი", "თელავი")
STREETS = (
    "რუსთაველის",
    "ჭავჭავაძის",
    "პეკინის",
    "ყაზბეგის",
    "ბარათაშვილის",
    "მარჯანიშვილის",
    "გამსახურდიას",
    "წერეთლის",
)
DISTRICTS = ("ვაკის", "საბურთალოს", "მთაწმინდის", "ნაძალადევის", "გლდანის", "ისნის")
FORMS = {"შპს": "შეზღუდული პასუხისმგებლობის საზოგადოება", "სს": "სააქციო საზოგადოება"}
CLASSES = (
    "clean",
    "surname_first",
    "second_person",
    "name_slip",
    "legal_form",
    "addr_number",
    "addr_street",
    "addr_short",
    "director_slip",
    "status",
)
EXPECTED: dict[str, set[tuple[str, str]]] = {
    "clean": set(),
    "surname_first": set(),
    "second_person": set(),
    "name_slip": {("AOA.REGISTRY_NAME", "error")},
    "legal_form": {("AOA.REGISTRY_NAME", "error")},
    "addr_number": {("AOA.REGISTRY_ADDRESS", "error")},
    "addr_street": {("AOA.REGISTRY_ADDRESS", "error")},
    "addr_short": {("AOA.REGISTRY_ADDRESS", "warning")},
    "director_slip": {("AOA.REGISTRY_DIRECTOR", "error")},
    "status": {("AOA.REGISTRY_STATUS", "warning")},
}
_GEORGIAN = "აბგდევზთიკლმნოპჟრსტუფქღყშჩცძწჭხჯჰ"


@dataclass
class Person:
    first: str
    stem: str
    ending: tuple[str, str]

    @property
    def nominative(self) -> str:
        return f"{self.first} {self.stem}{self.ending[0]}"

    @property
    def genitive(self) -> str:
        return f"{FIRST[self.first]} {self.stem}{self.ending[1]}"


@dataclass
class Company:
    form: str
    name: str
    code: str
    city: str
    street: str
    number: int
    district: str
    flat: int | None
    persons: list[Person]
    status: str | None = None
    #: what the act writes, where it differs from the registry
    act: dict[str, str] = field(default_factory=dict)


#: a noun's nominative ending and the one-letter endings that only decline it (dative -ს,
#: vocative -ო): "სერვისი" written "სერვისს" is the same name in another case, not a slip
_DECLINED = {"ი": "სო"}


def _slip(word: str, rng: random.Random) -> str:
    """One letter of ``word`` changed - never the first, never into the same letter, and never
    the last into a letter that only declines the word."""
    i = rng.randrange(1, len(word))
    banned = _DECLINED.get(word[i], "") if i == len(word) - 1 else ""
    letter = rng.choice([c for c in _GEORGIAN if c != word[i] and c not in banned])
    return word[:i] + letter + word[i + 1 :]


def _person(rng: random.Random) -> Person:
    return Person(rng.choice(sorted(FIRST)), rng.choice(SURNAME_STEMS), rng.choice(SURNAME_ENDINGS))


def _company(rng: random.Random) -> Company:
    persons = [_person(rng)]
    if rng.random() < 0.3:
        persons.append(_person(rng))
    return Company(
        form=rng.choice(sorted(FORMS)),
        name=f"{rng.choice(NAME_WORDS)} {rng.choice(NAME_SECOND)}",
        code=f"{rng.randrange(2, 5)}{rng.randrange(10**7, 10**8)}",
        city=rng.choice(CITIES),
        street=rng.choice(STREETS),
        number=rng.randrange(1, 200),
        district=rng.choice(DISTRICTS),
        flat=rng.randrange(1, 90) if rng.random() < 0.4 else None,
        persons=persons,
    )


def _registry_address(c: Company, layout: int) -> str:
    flat = f", ბინა {c.flat}" if c.flat is not None else ""
    if layout == 2011:
        return f"საქართველო, ქ. {c.city}ს {c.district} რაიონში, {c.street} ქ. №{c.number}{flat}"
    return f"საქართველო, ქალაქი {c.city}, {c.street} ქუჩა, N{c.number}{flat}"


def _extract_text(c: Company, layout: int, rng: random.Random) -> str:
    title = "ამონაწერი მეწარმეთა და არასამეწარმეო\n(არაკომერციული) იურიდიული პირების\nრეესტრიდან\n"
    pos = ["დირექტორი", "წარმომადგენელი"]
    if layout == 2011:
        people = "\n".join(
            f"- 01{rng.randrange(10**8, 10**9)}, {p.nominative},\n{pos[i]}, ,"
            for i, p in enumerate(c.persons)
        )
        return (
            "*B90000001 *\n"
            + title
            + f"""განაცხადის ნომერი: 123456
ამონაწერის მომზადების თარიღი: 01/09/2026 10:00:00
სუბიექტი
საფირმო სახელწოდება: {c.form} {c.name}
იურიდიული მისამართი: {_registry_address(c, 2011)}
საიდენტიფიკაციო კოდი: {c.code}
სამართლებრივი ფორმა: {FORMS[c.form]}
მარეგისტრირებელი ორგანო: საჯარო რეესტრის ეროვნული სააგენტო
ხელმძღვანელობაზე/წარმომადგენლობაზე უფლებამოსილი პირები
{people}
დამფუძნებლები
- 01000000033, ლევან ქავთარაძე,
ყადაღა/აკრძალვა:
რეგისტრირებული არ არის
"""
        )
    people = "\n".join(
        f"{pos[i]}\n{p.nominative}, 61{rng.randrange(10**8, 10**9)} ,ერთპიროვნული"
        for i, p in enumerate(c.persons)
    )
    status = c.status or "რეგისტრირებული არ არის"
    return (
        "*B90000002 *\nსაქართველოს იუსტიციის სამინისტრო\n"
        + title
        + f"""განაცხადის რეგისტრაციის ნომერი, მომზადების თარიღი: B90000002,
01/09/2026 16:07:35
სუბიექტი
საფირმო სახელწოდება: {c.form} {c.name}
სამართლებრივი
ფორმა: {FORMS[c.form]}
საიდენტიფიკაციო
ნომერი: {c.code}
იურიდიული მისამართი: {_registry_address(c, 2025)}
ინფორმაცია ლიკვიდაციის/ რეორგანიზაციის/ გადახდისუუნარობის
პროცესის მიმდინარეობის შესახებ
{status}
მმართველობის ორგანო
საერთო კრება
ხელმძღვანელობა/წარმომადგენლობა
{people}
პარტნიორები
მესაკუთრე რაოდენობა წილი
გიორგი ჭავჭავაძე,
61000000055 100 100%
"""
    )


def _block(c: Company) -> tuple[str, str, str, str, str]:
    """(legal form, name, address, director in the genitive, signature) as the act writes them."""
    form = c.act.get("form", c.form)
    name = c.act.get("name", c.name)
    address = f"ქ. {c.city}, {c.act.get('street', c.street)} ქ. {c.act.get('number', c.number)}"
    if c.flat is not None and "short" not in c.act:
        address += f", ბინა {c.flat}"
    signer = c.persons[int(c.act.get("signer", "0"))]
    nominative, genitive = signer.nominative, signer.genitive
    if (stem := c.act.get("stem")) is not None:
        nominative = f"{signer.first} {stem}{signer.ending[0]}"
        genitive = f"{FIRST[signer.first]} {stem}{signer.ending[1]}"
    signed = nominative
    if c.act.get("order") == "surname_first":
        first, last = nominative.split(" ", 1)
        signed = f"{last} {first}"
    return form, name, address, genitive, signed


def _act(customer: Company, contractor: Company) -> str:
    cf, cn, ca, cg, cs = _block(customer)
    kf, kn, ka, kg, ks = _block(contractor)
    customer_line = (
        f"{cf} „{cn}“, საიდენტიფიკაციო კოდი: {customer.code}; იურიდიული მისამართი: {ca} "
        f"(შემდგომში „დამკვეთი“) წარმოდგენილი დირექტორის {cg} სახით და"
    )
    contractor_line = (
        f"{kf} „{kn}“, საიდენტიფიკაციო კოდი: {contractor.code}, იურიდიული მისამართი: {ka} "
        f"(შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის {kg} სახით"
    )
    return f"""მიღება-ჩაბარების აქტი
{customer_line}
{contractor_line}
1. „შემსრულებელმა“ გასწია საკონსულტაციო მომსახურება.
2. მომსახურება შესრულდა 2026 წლის 15 სექტემბერს.
3. საფასური შეადგენს 1 000 (ათასი) ლარს გადასახადების ჩათვლით.
4. აქტი გაფორმდა 2026 წლის 15 სექტემბერი
„დამკვეთი“
{cf} „{cn}“
დირექტორი {cs}
„შემსრულებელი“
{kf} „{kn}“
დირექტორი: {ks}
"""


@dataclass(frozen=True)
class Case:
    cls: str
    act: str
    entries: tuple[RegistryEntry, ...]
    correction: str | None  # a value the error's correction must contain


def make_case(rng: random.Random, cls: str) -> Case:
    customer = _company(rng)
    contractor = _company(rng)
    while contractor.name == customer.name or contractor.code == customer.code:
        contractor = _company(rng)
    target = rng.choice((customer, contractor))
    correction: str | None = None
    if cls == "surname_first":
        target.act["order"] = "surname_first"
    elif cls == "second_person":
        if len(target.persons) == 1:
            target.persons.append(_person(rng))
        target.act["signer"] = "1"
    elif cls == "name_slip":
        words = target.name.split()
        k = rng.randrange(len(words))
        words[k] = _slip(words[k], rng)
        target.act["name"] = " ".join(words)
        correction = target.name
    elif cls == "legal_form":
        target.act["form"] = next(f for f in FORMS if f != target.form)
        correction = target.form
    elif cls == "addr_number":
        target.act["number"] = str(target.number + rng.randrange(1, 9))
        correction = str(target.number)
    elif cls == "addr_street":
        target.act["street"] = _slip(target.street, rng)
        correction = target.street
    elif cls == "addr_short":
        if target.flat is None:
            target.flat = rng.randrange(1, 90)
        target.act["short"] = "1"
    elif cls == "director_slip":
        signer = target.persons[0]
        target.act["stem"] = _slip(signer.stem, rng)
        correction = signer.stem
    elif cls == "status":
        target.status = "ლიკვიდაციის პროცესი დაწყებულია 01/08/2026"
    entries = []
    for c in (customer, contractor):
        layout = 2025 if (c.status or rng.random() < 0.5) else 2011
        entries.append(parse_extract(_extract_text(c, layout, rng))[0])
    return Case(cls, _act(customer, contractor), tuple(entries), correction)


def score(case: Case) -> str | None:
    """None when the case passes, else why not."""
    report = check_aoa(aoa_text=case.act, registry=RegistrySource.of(None, case.entries))
    got = [f for f in report.findings if f.rule_id.startswith("AOA.REGISTRY")]
    pairs = {(f.rule_id, f.severity) for f in got}
    if pairs != EXPECTED[case.cls]:
        return f"got {sorted(pairs)}"
    if case.correction is not None:
        errors = [f for f in got if f.severity == "error"]
        if not any(case.correction in (f.expected or "") for f in errors):
            return f"correction {[f.expected for f in errors]} lacks {case.correction!r}"
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
    parser = argparse.ArgumentParser(description="rule 1 accuracy on random parties")
    parser.add_argument("--per-class", type=int, default=50)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--show", type=int, default=3)
    args = parser.parse_args(argv)
    passed, failures = run(args.per_class, args.seed)
    total = args.per_class * len(CLASSES)
    print(f"rule 1: {sum(passed.values())} of {total} cases right")
    for cls in CLASSES:
        print(f"  {cls:15s} {passed[cls]:5d} / {args.per_class}")
    for case, why in failures[: args.show]:
        print(f"--- {case.cls}: {why}\n{case.act}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
