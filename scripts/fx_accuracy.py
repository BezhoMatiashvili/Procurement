"""Measure client rule 9 end to end: random foreign-currency acts, their invoices in lari.

    uv run --offline python scripts/fx_accuracy.py --cases 2000 --seed 7

Each case writes an act from scratch - invented parties, a fee in one of the currencies the
National Bank quotes, written as its code, its sign or its Georgian or English name, the amount in
one of the ways people group digits, with or without an advance paid on an earlier day and with or
without the remainder spelled out, the dates in one of the act's styles - imports a rate for every
day into a real rates table (``fx/rates_table.py``), runs the whole act check and compares the
invoice it works out with one computed here, independently, from the case's own numbers:
each term in Decimal, rounded half up to the tetri, then summed.

A case passes when the check reads the act's currency, works the invoice out and arrives at the
same lari to the tetri. Nothing is read from any client document; the rates are random.
"""

from __future__ import annotations

import argparse
import random
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.fx.rates_table import RateRow, TableRateProvider, import_rates

_TETRI = Decimal("0.01")
#: Currencies, the units the NBG quotes them in, and how an act may write them after an amount
#: (the dative a Georgian fee takes, or the code / English name), or before it (a code or sign).
CURRENCIES: dict[str, tuple[int, tuple[str, ...], tuple[str, ...]]] = {
    "USD": (1, ("აშშ დოლარს", "USD", "ამერიკულ დოლარს", "US dollars"), ("USD", "$")),
    "EUR": (1, ("ევროს", "EUR", "euros"), ("EUR", "€")),
    "GBP": (1, ("GBP", "ფუნტ სტერლინგს"), ("GBP", "£")),
    "CHF": (1, ("CHF", "შვეიცარიულ ფრანკს", "Swiss francs"), ("CHF",)),
    "JPY": (100, ("JPY", "იაპონურ იენს"), ("JPY",)),
    "RUB": (100, ("RUB", "რუსულ რუბლს", "₽"), ("RUB", "₽")),
    "TRY": (1, ("TRY", "თურქულ ლირას"), ("TRY", "₺")),
    "CNY": (10, ("CNY", "ჩინურ იუანს"), ("CNY",)),
    "PLN": (10, ("PLN", "პოლონურ ზლოტს"), ("PLN",)),
    "CAD": (1, ("CAD", "კანადურ დოლარს", "Canadian dollars"), ("CAD",)),
    "AZN": (1, ("AZN", "აზერბაიჯანულ მანათს"), ("AZN", "₼")),
    "UAH": (10, ("UAH", "უკრაინულ გრივნას"), ("UAH", "₴")),
    "KZT": (100, ("KZT", "ყაზახურ ტენგეს"), ("KZT",)),
    "AMD": (1000, ("AMD", "სომხურ დრამს"), ("AMD",)),
}
_MONTHS = (
    ("იანვარი", "იანვარს"),
    ("თებერვალი", "თებერვალს"),
    ("მარტი", "მარტს"),
    ("აპრილი", "აპრილს"),
    ("მაისი", "მაისს"),
    ("ივნისი", "ივნისს"),
    ("ივლისი", "ივლისს"),
    ("აგვისტო", "აგვისტოს"),
    ("სექტემბერი", "სექტემბერს"),
    ("ოქტომბერი", "ოქტომბერს"),
    ("ნოემბერი", "ნოემბერს"),
    ("დეკემბერი", "დეკემბერს"),
)


@dataclass(frozen=True)
class Case:
    """One act and what its invoice must come to."""

    text: str
    currency: str
    expected: Decimal
    rates: tuple[RateRow, ...]
    dims: dict[str, str]


def _amount(value: Decimal, style: str) -> str:
    whole, cents = f"{value:.2f}".split(".")
    groups = f"{int(whole):,}"
    if style == "space_comma":
        return f"{groups.replace(',', ' ')},{cents}"
    if style == "space_dot":
        return f"{groups.replace(',', ' ')}.{cents}"
    if style == "comma_dot":
        return f"{groups}.{cents}"
    return f"{whole}.{cents}"  # plain


def _day_written(day: date, style: str, *, dative: bool) -> str:
    nom, dat = _MONTHS[day.month - 1]
    if style == "numeric":
        return f"{day:%d.%m.%Y}"
    if style == "day_first":
        return f"{day.day} {dat if dative else nom}, {day.year} წ."
    return f"{day.year} წლის {day.day} {dat if dative else nom}"


def _rate(rng: random.Random, quantity: int) -> Decimal:
    per_unit = Decimal(rng.uniform(0.005, 4.0)).quantize(Decimal("0.000001"))
    return (per_unit * quantity).quantize(Decimal("0.0001"))


def make_case(rng: random.Random) -> Case:
    code = rng.choice(sorted(CURRENCIES))
    quantity, after_forms, before_forms = CURRENCIES[code]
    act_day = date(2026, 1, 1) + timedelta(days=rng.randrange(360))
    total = Decimal(rng.randrange(1_000_00, 5_000_000_00)) / 100
    if rng.random() < 0.3:
        total = total.quantize(Decimal(1))
    amount_style = rng.choice(("space_comma", "space_dot", "comma_dot", "plain"))
    date_style = rng.choice(("long", "numeric", "day_first"))
    before = rng.random() < 0.25

    def money(value: Decimal) -> str:
        if before:
            return f"{rng.choice(before_forms)} {_amount(value, amount_style)}"
        return f"{_amount(value, amount_style)} {rng.choice(after_forms)}"

    dims = {
        "currency": code,
        "amount": amount_style,
        "dates": date_style,
        "currency_before": str(before),
    }
    rates = [RateRow(code, act_day, _rate(rng, quantity), quantity, "NBG")]
    advance_sentence = ""
    advance: tuple[Decimal, date] | None = None
    phrasing = "none"
    if rng.random() < 0.55:
        share = Decimal(rng.randrange(10, 61)) / 100
        paid = (total * share).quantize(_TETRI, rounding=ROUND_HALF_UP)
        paid_day = act_day - timedelta(days=rng.randrange(1, 45))
        advance = (paid, paid_day)
        rates.append(RateRow(code, paid_day, _rate(rng, quantity), quantity, "NBG"))
        when = _day_written(paid_day, date_style, dative=True)
        phrasing = rng.choice(("paid", "transferred", "as_advance"))
        if phrasing == "paid":
            advance_sentence = f" ავანსი {money(paid)} გადახდილია {when}."
        elif phrasing == "transferred":
            advance_sentence = f" {when} წინასწარ გადარიცხულია {money(paid)}."
        else:
            advance_sentence = f" ავანსის სახით {when} ჩარიცხულია {money(paid)}."
        if rng.random() < 0.5:
            advance_sentence += (
                f" დარჩენილი თანხა {money(total - paid)} გადაიხდება აქტის საფუძველზე."
            )
            phrasing += "+rest"
    dims["advance"] = phrasing
    signed = _day_written(act_day, date_style, dative=False)
    done = _day_written(act_day, date_style, dative=True)
    text = f"""მიღება-ჩაბარების აქტი
შპს „ნიმუში ტრეიდი“, საიდენტიფიკაციო კოდი: 404123456 (შემდგომში „დამკვეთი“) და
შპს „ტესტ სერვისი“, საიდენტიფიკაციო კოდი: 405987654 (შემდგომში „შემსრულებელი“)
1. „შემსრულებელმა“ გასწია საკონსულტაციო მომსახურება.
2. მომსახურება შესრულდა {done}.
3. საფასური შეადგენს {money(total)}.{advance_sentence}
4. აქტი გაფორმდა {signed}
"""
    expected = Decimal(0)
    rest = total
    by_day = {(r.currency, r.valid_on): r.per_unit for r in rates}
    if advance is not None:
        paid, paid_day = advance
        expected += (paid * by_day[code, paid_day]).quantize(_TETRI, rounding=ROUND_HALF_UP)
        rest = total - paid
    expected += (rest * by_day[code, act_day]).quantize(_TETRI, rounding=ROUND_HALF_UP)
    return Case(text, code, expected, tuple(rates), dims)


def run(cases: int, seed: int) -> tuple[int, list[tuple[Case, str]]]:
    """Run ``cases`` random acts; returns how many passed and the failures with their reason."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    failures: list[tuple[Case, str]] = []
    passed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(cases):
            case = make_case(rng)
            db = Path(tmp) / f"fx{i}.db"
            import_rates(case.rates, db)
            provider = TableRateProvider(db)
            try:
                summary = check_aoa(aoa_text=case.text, rates=provider).summary
            finally:
                provider.close()
            got = summary.get("invoice_gel")
            if summary.get("currency") != case.currency:
                failures.append((case, f"currency read as {summary.get('currency')}"))
            elif got is None:
                failures.append((case, "no invoice worked out"))
            elif Decimal(got) != case.expected:
                failures.append((case, f"invoice {got} GEL, expected {case.expected}"))
            else:
                passed += 1
    return passed, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rule 9 accuracy on random acts")
    parser.add_argument("--cases", type=int, default=500)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--show", type=int, default=3, help="failures to print in full")
    args = parser.parse_args(argv)
    passed, failures = run(args.cases, args.seed)
    share = passed / args.cases if args.cases else 0.0
    print(f"rule 9: {passed} of {args.cases} invoices exact to the tetri ({share:.2%})")
    by_dim: Counter[str] = Counter()
    for case, why in failures:
        by_dim[why.split(" ")[0]] += 1
        for k, v in case.dims.items():
            by_dim[f"{k}={v}"] += 1
    for key, n in by_dim.most_common(20):
        print(f"  {n:5d}  {key}")
    for case, why in failures[: args.show]:
        print(f"--- {why} {case.dims}\n{case.text}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
