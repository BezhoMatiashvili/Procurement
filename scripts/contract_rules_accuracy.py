"""Measure client rule 10 end to end: an act against its contract, one planted difference a case.

    uv run --offline python scripts/contract_rules_accuracy.py --per-class 100 --seed 7

Each case writes a contract and its act from scratch - two invented companies with their codes,
addresses and directors, a project, its cadastral code, a subject and a list of services with
numbers in them, a term, a fee in digits and words, an advance, a payment deadline and method, a
bank code and an IBAN - and plants one class of difference in the act (or none): each of the
client's bullets - the contractor's and the customer's code, name and director, a code given to
the wrong party, the subject, the cadastral code, an act outside the contract's term, a service,
the fee, the payment deadline and method, the IBAN and the bank code, a party's code or the
account left out - and two harmless restyles. A case passes when the act's errors and warnings
are exactly the expected rule ids (a few named secondary ones allowed) and each finding's
correction is the contract's value in the act's own form (a director's name in the case the act
writes it in). Every name, code and account is invented.
"""

# the templates are a contract's and an act's own lines
# ruff: noqa: E501

from __future__ import annotations

import argparse
import random
import string
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from procurement_agent.checks.aoa import check_aoa

FIRST = {
    "გიორგი": "გიორგის",
    "ნინო": "ნინოს",
    "დავით": "დავითის",
    "ანა": "ანას",
    "ლევან": "ლევანის",
    "ნიკა": "ნიკას",
}
STEMS = ("ბერი", "მელა", "კაპანა", "ლომი", "ხარაბა", "ქავთარა")
ENDINGS = (("ძე", "ძის"), ("შვილი", "შვილის"))
NAMES = (
    "ალფა ჯგუფი",
    "ბეტა სერვისი",
    "დელტა ტრეიდი",
    "ომეგა ჰოლდინგი",
    "სიგმა კონსალტინგი",
    "ზენიტი ინჟინერინგი",
)
CITIES = ("თბილისი", "ბათუმი", "ქუთაისი", "რუსთავი")
STREETS = ("რუსთაველის", "ჭავჭავაძის", "პეკინის", "ყაზბეგის", "წერეთლის")
FEES = (
    ("1 000", "ათასი"),
    ("2 500", "ორი ათას ხუთასი"),
    ("3 200", "სამი ათას ორასი"),
    ("5 000", "ხუთი ათასი"),
    ("7 500", "შვიდი ათას ხუთასი"),
    ("12 000", "თორმეტი ათასი"),
    ("15 000", "თხუთმეტი ათასი"),
)
DAYS = ((3, "სამი"), (5, "ხუთი"), (10, "ათი"), (15, "თხუთმეტი"))
SUBJECTS = (
    "{n} სართულიანი საცხოვრებელი სახლის ტექნიკური ზედამხედველობის მომსახურების გაწევა",
    "{n} ერთეული სერვერის ტექნიკური მომსახურების გაწევა",
    "{n} ობიექტის დაცვის მომსახურების გაწევა",
)
SERVICES = (
    (
        "მთავარი გვერდის დიზაინი GEO/ENG",
        "{k} სერვისის გვერდის აღწერა GEO/ENG",
        "საკონტაქტო ფორმის აწყობა",
    ),
    ("{k} ობიექტის დათვალიერება", "ყოველთვიური ანგარიშის მომზადება", "ტექნიკური დასკვნის შედგენა"),
)
#: each month in the nominative, the ablative ("from"), the genitive and the terminative ("until"):
#: the months that drop a vowel ("სექტემბრის") written as Georgian writes them
MONTHS = (
    ("იანვარი", "იანვრიდან", "იანვრის", "იანვრამდე"),
    ("თებერვალი", "თებერვლიდან", "თებერვლის", "თებერვლამდე"),
    ("მარტი", "მარტიდან", "მარტის", "მარტამდე"),
    ("აპრილი", "აპრილიდან", "აპრილის", "აპრილამდე"),
    ("მაისი", "მაისიდან", "მაისის", "მაისამდე"),
    ("ივნისი", "ივნისიდან", "ივნისის", "ივნისამდე"),
    ("ივლისი", "ივლისიდან", "ივლისის", "ივლისამდე"),
    ("აგვისტო", "აგვისტოდან", "აგვისტოს", "აგვისტომდე"),
    ("სექტემბერი", "სექტემბრიდან", "სექტემბრის", "სექტემბრამდე"),
    ("ოქტომბერი", "ოქტომბრიდან", "ოქტომბრის", "ოქტომბრამდე"),
    ("ნოემბერი", "ნოემბრიდან", "ნოემბრის", "ნოემბრამდე"),
    ("დეკემბერი", "დეკემბრიდან", "დეკემბრის", "დეკემბრამდე"),
)
LAST_DAY = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
CLASSES = (
    "clean",
    "contractor_code",
    "customer_code",
    "contractor_name",
    "customer_name",
    "contractor_director",
    "customer_director",
    "code_swap",
    "subject_number",
    "cadastral",
    "act_after_validity",
    "service_number",
    "fee",
    "payment_deadline",
    "payment_method",
    "iban",
    "bic",
    "omit_contractor_code",
    "omit_iban",
    "h_quotes",
    "h_amount_format",
)
EXPECTED: dict[str, set[str]] = {
    "contractor_code": {"AOA.ID_CODES"},
    "customer_code": {"AOA.ID_CODES"},
    "contractor_name": {"AOA.PARTY_NAME"},
    "customer_name": {"AOA.PARTY_NAME"},
    "contractor_director": {"AOA.DIRECTOR"},
    "customer_director": {"AOA.DIRECTOR"},
    "code_swap": {"AOA.ID_CODE_PARTY"},
    "subject_number": {"AOA.SUBJECT"},
    "cadastral": {"AOA.CADASTRAL"},
    "act_after_validity": {"AOA.ACT_DATE_IN_TERM"},
    "service_number": {"AOA.SERVICES"},
    "fee": {"AOA.FEE"},
    "payment_deadline": {"AOA.PAYMENT_DEADLINE"},
    "payment_method": {"AOA.PAYMENT_METHOD"},
    "iban": {"AOA.BANK"},
    "bic": {"AOA.BANK"},
    "omit_contractor_code": {"AOA.ID_CODES"},
    "omit_iban": {"AOA.BANK"},
}
#: secondary findings a class may raise besides its own
ALLOWED: dict[str, set[str]] = {
    "code_swap": {"AOA.ID_CODES"},
    "act_after_validity": {"AOA.PERIOD_IN_TERM"},
    "bic": {"AOA.BANK_CODE_FORMAT"},
}
_GEORGIAN = "აბგდევზთიკლმნოპჟრსტუფქღყშჩცძწჭხჯჰ"


#: a noun's nominative ending and the one-letter endings that only decline it (dative -ს,
#: vocative -ო): "ჯგუფი" written "ჯგუფს" is the same name in another case, not a slip
_DECLINED = {"ი": "სო"}


def _slip(word: str, rng: random.Random) -> str:
    i = rng.randrange(1, len(word))
    banned = _DECLINED.get(word[i], "") if i == len(word) - 1 else ""
    return (
        word[:i]
        + rng.choice([c for c in _GEORGIAN if c != word[i] and c not in banned])
        + (word[i + 1 :])
    )


def _iban(rng: random.Random) -> str:
    bank = rng.choice(("NB", "TB", "BG", "LB"))
    account = "".join(rng.choice(string.digits) for _ in range(16))
    rearranged = f"{bank}{account}GE00"
    digits = "".join(str(int(c, 36)) for c in rearranged)
    check = 98 - int(digits) % 97
    return f"GE{check:02d}{bank}{account}"


@dataclass
class Party:
    form: str
    name: str
    code: str
    city: str
    street: str
    number: int
    first: str
    stem: str
    ending: tuple[str, str]

    @property
    def director(self) -> str:
        return f"{self.first} {self.stem}{self.ending[0]}"

    @property
    def director_genitive(self) -> str:
        return f"{FIRST[self.first]} {self.stem}{self.ending[1]}"

    def block(self) -> str:
        return (
            f"{self.form} „{self.name}“, საიდენტიფიკაციო კოდი: {self.code}, იურიდიული მისამართი: "
            f"ქ. {self.city}, {self.street} {self.number}"
        )


def _party(rng: random.Random, name: str) -> Party:
    return Party(
        form=rng.choice(("შპს", "სს")),
        name=name,
        code=f"{rng.randrange(2, 5)}{rng.randrange(10**7, 10**8)}",
        city=rng.choice(CITIES),
        street=rng.choice(STREETS),
        number=rng.randrange(1, 150),
        first=rng.choice(sorted(FIRST)),
        stem=rng.choice(STEMS),
        ending=rng.choice(ENDINGS),
    )


@dataclass
class Pair:
    customer: Party
    contractor: Party
    project: str
    cadastral: str
    subject: str
    services: tuple[str, ...]
    fee: tuple[str, str]
    days: tuple[int, str]
    bic: str
    iban: str
    month: int
    validity_month: int
    act: dict[str, str] = field(default_factory=dict)


def _contract(p: Pair) -> str:
    c, k = p.customer, p.contractor
    services = "\n".join(f"- {s}" for s in p.services)
    return f"""ხელშეკრულება
საქართველო, თბილისი                    1 თებერვალი, 2026 წელი
{c.block()} (შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის {c.director_genitive} სახით და
{k.block()} (შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის {k.director_genitive} სახით
მუხლი I. ხელშეკრულების საგანი
1.1. „შემსრულებელი“ იღებს ვალდებულებას „{p.project}“ პროექტისთვის (ს/კ: {p.cadastral}) {p.subject} (შემდგომში „მომსახურება“).
1.2. „შემსრულებელმა“ საანგარიშო თვეში „შემკვეთს“ უნდა ჩააბაროს:
{services}
რის შესახებაც მხარეები აფორმებენ მიღება-ჩაბარების აქტს.
2.1. შეასრულოს მომსახურება 2026 წლის 1 თებერვლიდან - 2026 წლის 31 დეკემბრის ჩათვლით.
3.1. ხელშეკრულება ძალაში შედის მისი ხელმოწერის მომენტიდან და მოქმედებს 2026 წლის {LAST_DAY[p.validity_month - 1]} {MONTHS[p.validity_month - 1][3]}.
4.1. ღირებულება შეადგენს {p.fee[0]} ({p.fee[1]}) ლარს, რომელიც მოიცავს გადასახადებს მათ შორის დღგ-ს.
4.2. ავანსი შეადგენს ღირებულების 30%-ს.
4.3. დარჩენილი თანხა გადაიხდება მიღება-ჩაბარების აქტის გაფორმების თარიღიდან {p.days[0]} ({p.days[1]}) სამუშაო დღის ვადაში, „შემსრულებლის“ საბანკო ანგარიშზე გადარიცხვით.
4.4. ანგარიშსწორება: ბანკის კოდი: {p.bic}, ანგარიშის ნომერი: {p.iban}.
"""


def _act(p: Pair) -> str:
    c, k = p.customer, p.contractor
    a = p.act

    def name(x: Party, key: str) -> str:
        return a.get(key, x.name)

    c_code = a.get("customer_code", c.code)
    k_code = a.get("contractor_code", k.code)
    c_dir, c_gen = (
        a.get("customer_director", c.director),
        a.get("customer_director_gen", c.director_genitive),
    )
    k_dir = a.get("contractor_director", k.director)
    nominative, ablative, genitive, _ = MONTHS[p.month - 1]
    last = LAST_DAY[p.month - 1]
    subject = a.get("subject", p.subject)
    services = "\n".join(f"- {s}" for s in a.get("services", "\n".join(p.services)).split("\n"))
    fee = a.get("fee", f"{p.fee[0]} ({p.fee[1]})")
    days = a.get("days", f"{p.days[0]} ({p.days[1]})")
    method = a.get("method", "შემსრულებლის საბანკო ანგარიშზე გადარიცხვით")
    text = f"""მიღება-ჩაბარების აქტი
{c.form} „{name(c, "customer_name")}“, საიდენტიფიკაციო კოდი: {c_code}, იურიდიული მისამართი: ქ. {c.city}, {c.street} {c.number} (შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის {c_gen} სახით და
{k.form} „{name(k, "contractor_name")}“, საიდენტიფიკაციო კოდი: {k_code}, იურიდიული მისამართი: ქ. {k.city}, {k.street} {k.number} (შემდგომში „შემსრულებელი“)
„მხარეებმა“ გააფორმეს ხელშეკრულება, რომლის საგანსაც წარმოადგენს „შემკვეთის“ პროექტ „{p.project}“ (ს/კ: {a.get("cadastral", p.cadastral)}) {subject} (შემდგომში „მომსახურება“).
2. შესრულების თარიღი
მომსახურება შესრულდა 2026 წლის 1 {ablative} 2026 წლის {last} {genitive} ჩათვლით.
3. საფასური
საფასური შეადგენს {fee} ლარს გადასახადების ჩათვლით.
დარჩენილი თანხა გადაიხდება აქტის გაფორმების თარიღიდან {days} სამუშაო დღის ვადაში, {method}.
ბანკის კოდი: {a.get("bic", p.bic)}; ა/ა: {a.get("iban", p.iban)}
4. გაფორმების თარიღი
აქტი გაფორმდა 2026 წლის {last} {nominative}
დანართი #1
მომსახურების აღწერა:
1. „შემსრულებლის“ მიერ „{p.project}“ პროექტისთვის შესრულებული მომსახურება, კერძოდ:
{services}
„შემკვეთი“
{c.form} „{name(c, "customer_name")}“
დირექტორი {c_dir}
„შემსრულებელი“
{k.form} „{name(k, "contractor_name")}“
დირექტორი: {k_dir}
"""
    if "omit_code" in a:
        text = text.replace(f", საიდენტიფიკაციო კოდი: {k_code}", "", 1)
    if "omit_iban" in a:
        text = text.replace(f"; ა/ა: {a.get('iban', p.iban)}", "", 1)
    if "quotes" in a:
        text = text.replace("„", '"').replace("“", '"')
    return text


def make_pair(rng: random.Random) -> Pair:
    names = rng.sample(NAMES, 2)
    n, k = rng.randrange(2, 9), rng.randrange(2, 9)
    services = tuple(s.format(k=k) for s in rng.choice(SERVICES))
    month = rng.randrange(3, 10)
    return Pair(
        customer=_party(rng, names[0]),
        contractor=_party(rng, names[1]),
        project=rng.choice(("მზე", "ზღვის ხედი", "ახალი უბანი", "მთის ბინები")),
        cadastral=f"{rng.randrange(1, 99):02d}.{rng.randrange(1, 99):02d}.{rng.randrange(1, 99):02d}.{rng.randrange(1, 999):03d}",
        subject=rng.choice(SUBJECTS).format(n=n),
        services=services,
        fee=rng.choice(FEES),
        days=rng.choice(DAYS),
        bic=f"{''.join(rng.choice(string.ascii_uppercase) for _ in range(4))}GE22",
        iban=_iban(rng),
        month=month,
        validity_month=12,
    )


@dataclass(frozen=True)
class Want:
    """A correction a case must get: a finding whose ``actual`` is the act's wrong value (any,
    when None) and whose ``expected`` is the right one - or holds it, when not ``exact``."""

    actual: str | None
    right: str
    exact: bool = True


def plant(rng: random.Random, p: Pair, cls: str) -> list[Want]:
    """Plant ``cls`` in the act of ``p``; the corrections the findings must name."""
    c, k = p.customer, p.contractor

    def code_slip(code: str) -> str:
        i = rng.randrange(1, len(code))
        return code[:i] + str((int(code[i]) + rng.randrange(1, 9)) % 10) + code[i + 1 :]

    if cls == "contractor_code":
        p.act["contractor_code"] = code_slip(k.code)
        return [Want(p.act["contractor_code"], k.code)]
    if cls == "customer_code":
        p.act["customer_code"] = code_slip(c.code)
        return [Want(p.act["customer_code"], c.code)]
    if cls in ("contractor_name", "customer_name"):
        party = k if cls == "contractor_name" else c
        words = party.name.split()
        j = rng.randrange(len(words))
        words[j] = _slip(words[j], rng)
        p.act[cls] = " ".join(words)
        return [Want(f"{party.form} {p.act[cls]}", f"{party.form} {party.name}")]
    if cls in ("contractor_director", "customer_director"):
        party = k if cls == "contractor_director" else c
        stem = _slip(party.stem, rng)
        p.act[cls] = f"{party.first} {stem}{party.ending[0]}"
        p.act[f"{cls}_gen"] = f"{FIRST[party.first]} {stem}{party.ending[1]}"
        # the act names the customer's director in its preamble too, in the genitive
        wants = [Want(p.act[cls], party.director)]
        if party is c:
            wants.append(Want(p.act[f"{cls}_gen"], party.director_genitive))
        return wants
    if cls == "code_swap":
        p.act["contractor_code"] = c.code
        return [Want(c.code, k.code)]
    if cls == "subject_number":
        old = next(ch for ch in p.subject if ch.isdigit())
        new = str(int(old) % 8 + 2 if int(old) % 8 + 2 != int(old) else int(old) + 1)
        p.act["subject"] = p.subject.replace(old, new, 1)
        return [Want(new, old)]
    if cls == "cadastral":
        groups = p.cadastral.split(".")
        groups[3] = f"{(int(groups[3]) + rng.randrange(1, 50)) % 1000:03d}"
        p.act["cadastral"] = ".".join(groups)
        return [Want(p.act["cadastral"], p.cadastral)]
    if cls == "act_after_validity":
        p.validity_month = p.month - 1
    elif cls == "service_number":
        items = list(p.services)
        j = next(i for i, s in enumerate(items) if any(ch.isdigit() for ch in s))
        old = next(ch for ch in items[j] if ch.isdigit())
        right = items[j]
        items[j] = items[j].replace(old, str(int(old) + 1), 1)
        p.act["services"] = "\n".join(items)
        return [Want(items[j], right)]
    elif cls == "fee":
        other = rng.choice([f for f in FEES if f != p.fee])
        p.act["fee"] = f"{other[0]} ({other[1]})"
        return [Want(None, f"{p.fee[0]} ({p.fee[1]})", exact=False)]
    elif cls == "payment_deadline":
        other = rng.choice([d for d in DAYS if d != p.days])
        p.act["days"] = f"{other[0]} ({other[1]})"
        return [Want(None, f"{p.days[0]} ({p.days[1]})", exact=False)]
    elif cls == "payment_method":
        p.act["method"] = "ნაღდი ანგარიშსწორებით"
    elif cls == "iban":
        i = rng.randrange(8, len(p.iban))
        p.act["iban"] = (
            p.iban[:i] + str((int(p.iban[i]) + rng.randrange(1, 9)) % 10) + p.iban[i + 1 :]
        )
        return [Want(p.act["iban"], p.iban)]
    elif cls == "bic":
        p.act["bic"] = f"{''.join(rng.choice(string.ascii_uppercase) for _ in range(4))}GE22"
        return [Want(p.act["bic"], p.bic)]
    elif cls == "omit_contractor_code":
        p.act["omit_code"] = "1"
        return [Want(None, k.code)]
    elif cls == "omit_iban":
        p.act["omit_iban"] = "1"
        return [Want(None, p.iban)]
    elif cls == "h_quotes":
        p.act["quotes"] = "1"
    elif cls == "h_amount_format":
        p.act["fee"] = f"{p.fee[0].replace(' ', '')}.00 ({p.fee[1]})"
    return []


@dataclass(frozen=True)
class Case:
    cls: str
    act: str
    contract: str
    wants: tuple[Want, ...] = ()


def make_case(rng: random.Random, cls: str) -> Case:
    p = make_pair(rng)
    wants = plant(rng, p, cls)
    return Case(cls, _act(p), _contract(p), tuple(wants))


def score(case: Case) -> str | None:
    report = check_aoa(aoa_text=case.act, contract_text=case.contract)
    found = [f for f in report.findings if f.severity in ("error", "warning")]
    got = {f.rule_id for f in found}
    want = EXPECTED.get(case.cls, set())
    if not want <= got or not got <= want | ALLOWED.get(case.cls, set()):
        return f"got {sorted(got)}"
    for w in case.wants:
        mine = [f for f in found if w.actual is None or f.actual == w.actual]
        values = [f.expected or "" for f in mine]
        if not any((v == w.right) if w.exact else (w.right in v) for v in values):
            return f"correction of {w.actual!r}: {values} is not {w.right!r}"
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
    parser = argparse.ArgumentParser(description="rule 10 accuracy on random act/contract pairs")
    parser.add_argument("--per-class", type=int, default=30)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--show", type=int, default=2)
    args = parser.parse_args(argv)
    passed, failures = run(args.per_class, args.seed)
    total = args.per_class * len(CLASSES)
    print(f"rule 10: {sum(passed.values())} of {total} cases right")
    for cls in CLASSES:
        print(f"  {cls:22s} {passed[cls]:5d} / {args.per_class}")
    reasons = Counter(f"{c.cls}: {w}" for c, w in failures)
    for reason, n in reasons.most_common(20):
        print(f"  {n:4d}  {reason}")
    for case, why in failures[: args.show]:
        print(f"--- {case.cls}: {why}\n{case.act}\n=== contract\n{case.contract}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
