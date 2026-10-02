"""Client rule 9: the invoice in lari for an act written in a foreign currency.

The instruction: an act in US dollars (or another foreign currency) is invoiced in lari at the
National Bank's official rate. An advance paid before the act is converted at the rate of the day
it was paid, and the rest at the rate of the act's date:

    advance    1,000.00 USD x 2.6104 (paid 10 September)   =  2,610.40 GEL
    remainder 10,000.00 USD x 2.6081 (act of 17 September) = 26,081.00 GEL
                                                              28,691.40 GEL

This module is that arithmetic and nothing more - pure functions over what the act states, what
the user entered and the rates a ``fx.provider.RateProvider`` returns - so each part is tested on
its own; ``checks.aoa.AoaChecker.rule_fx_invoice`` turns the result into a finding.

Money here is ``Decimal``: an invoice is checked against an accountant's figure to the tetri, and
binary floats are not exact to the tetri. Each term is rounded half up to two places and the
rounded terms are summed, the way an invoice lists them. Whether the client rounds each term or
only the sum is still their question to answer; the two differ by at most a tetri a term.

A contract's advance is never taken for a payment. A contract plans an advance; only the act, or
the user, can say one was paid, and when.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

from procurement_agent.checks.contract_terms import AdvanceTerms, RoledAmount
from procurement_agent.extraction.normalize import parse_number
from procurement_agent.fx.provider import RateProvider, RateQuote

__all__ = [
    "LOCAL_CURRENCY",
    "MAX_TYPED_RATES",
    "AdvanceInput",
    "InvoicePlan",
    "InvoiceTerm",
    "NoInvoice",
    "PricedTerm",
    "fee_currencies",
    "foreign_currencies",
    "format_entered_rates",
    "invoice_gel",
    "parse_advance",
    "parse_entered_rates",
    "parse_typed_rate",
    "plan_invoice",
    "price_terms",
    "to_money",
]

#: The currency an invoice is written in.
LOCAL_CURRENCY = "GEL"
#: An invoice's smallest unit: one tetri.
_TETRI = Decimal("0.01")
#: The most an entered advance may be, and the longest text read as one: anything longer or
#: larger is a slip of the keyboard, not an advance.
_MAX_AMOUNT = Decimal("1000000000000")
_MAX_TEXT = 32
#: The days an advance may have been paid on. Outside them the date is a typo.
_EARLIEST, _LATEST = date(1990, 1, 1), date(2100, 12, 31)
#: Amounts that are neither the fee nor a part of it (``contract_terms.amount_roles``).
_NOT_THE_FEE = frozenset({"penalty", "guarantee", "tax", "equivalent"})


def to_money(value: float | Decimal) -> Decimal:
    """An amount as an invoice writes it, to the tetri. A float goes through its shortest
    decimal form, "2610.4", so its binary tail never reaches the arithmetic."""
    exact = value if isinstance(value, Decimal) else Decimal(str(value))
    return exact.quantize(_TETRI, rounding=ROUND_HALF_UP)


# --- what the user entered --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdvanceInput:
    """An advance the user entered: its amount, in the act's currency, and the day it was paid.

    Either part may be missing, and the act then supplies it. An amount of 0 says no advance was
    paid, so none is read from the act either.
    """

    amount: Decimal | None = None
    paid_on: date | None = None


def _amount(text: str | None) -> Decimal | None:
    raw = (text or "").strip()
    if not raw or len(raw) > _MAX_TEXT:
        return None
    number = parse_number(raw)
    if number is None:
        return None
    try:
        value = Decimal(str(number))
    except InvalidOperation:
        return None
    # more than two decimals is no amount of money; a negative one is no advance
    if not value.is_finite() or value < 0 or value > _MAX_AMOUNT or value != to_money(value):
        return None
    return to_money(value)


def _day(text: str | None) -> date | None:
    raw = (text or "").strip()
    if not raw or len(raw) > _MAX_TEXT:
        return None
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    return day if _EARLIEST <= day <= _LATEST else None


def parse_advance(amount_text: str | None, date_text: str | None) -> AdvanceInput | None:
    """The two advance fields as the user filled them in, or None when neither holds anything.

    Lenient, like the contractor type in ``ui.routes._aoa_inputs``: a value that does not read -
    "abc", a negative amount, a date that is not an ISO date - is left out rather than refused,
    because it arrives in a link and the report must still open. The API refuses the same values
    (``api.checks.AoaCheckRequest``), where the caller can correct them.

    The amount may be written as people write it: "1,000", "1 000,00", "1000.5".
    """
    entered = AdvanceInput(amount=_amount(amount_text), paid_on=_day(date_text))
    return None if entered == AdvanceInput() else entered


#: An exchange rate typed in on the report's form: lari for ONE unit, up to six decimals. No
#: currency the National Bank quotes is worth a thousand lari a unit.
_TYPED_RATE = re.compile(r"\d{1,3}(?:\.\d{1,6})?")
#: One typed rate as a link carries it: "USD:2026-09-10".
_RATE_DAY = re.compile(r"([A-Z]{3}):(\d{4}-\d{2}-\d{2})")
#: The most typed rates one check takes: an invoice has a few terms, not dozens.
MAX_TYPED_RATES = 20


def parse_typed_rate(text: str | None) -> Decimal | None:
    """A rate as a user types it - "2.6104", or with a decimal comma "2,6104" - or None.

    A rate never reaches a thousand lari a unit, so a comma in it is a decimal comma, not a
    thousands separator.
    """
    raw = (text or "").strip().replace(" ", "")
    if not raw or len(raw) > 12:
        return None
    if raw.count(",") == 1 and "." not in raw:
        raw = raw.replace(",", ".")
    if not _TYPED_RATE.fullmatch(raw):
        return None
    value = Decimal(raw)
    return value if value > 0 else None


def parse_entered_rates(
    compact: str | None, days: Sequence[str] = (), values: Sequence[str] = ()
) -> dict[tuple[str, date], Decimal]:
    """The exchange rates a user typed in, by (currency, day): from a link's ``rates`` value
    ("USD:2026-09-10:2.6104,USD:2026-09-17:2.6081") and from the report form's fields (a day
    "USD:2026-09-10" and its value, pair by pair), the form's winning.

    Lenient like ``parse_advance``: what does not read - a lari rate, a malformed day, an empty
    field - is left out, so a link always opens. At most ``MAX_TYPED_RATES``.
    """
    pairs: list[tuple[str, str]] = []
    for part in (compact or "").split(",")[: MAX_TYPED_RATES + 1]:
        key, _, value = part.strip().rpartition(":")
        pairs.append((key, value))
    pairs.extend(zip(days[:MAX_TYPED_RATES], values[:MAX_TYPED_RATES], strict=False))
    out: dict[tuple[str, date], Decimal] = {}
    for key, value in pairs:
        found = _RATE_DAY.fullmatch(key.strip())
        rate = parse_typed_rate(value)
        if found is None or rate is None or found.group(1) == LOCAL_CURRENCY:
            continue
        day = _day(found.group(2))
        if day is None:
            continue
        out[found.group(1), day] = rate
    return dict(list(out.items())[:MAX_TYPED_RATES])


def format_entered_rates(entered: dict[tuple[str, date], Decimal]) -> str:
    """The typed rates as a link carries them: the inverse of ``parse_entered_rates``."""
    return ",".join(f"{c}:{d.isoformat()}:{r}" for (c, d), r in sorted(entered.items()))


# --- the plan ---------------------------------------------------------------------------------

#: "advance": paid ahead, converted on its payment day. "rest": the remainder - or, with no
#: advance, the whole fee - converted on the act's date.
Part = Literal["advance", "rest"]


@dataclass(frozen=True, slots=True)
class InvoiceTerm:
    """One line of the invoice: an amount in the act's currency, at one day's rate."""

    amount: Decimal
    part: Part
    #: the day whose rate applies; None when neither the act nor the user says, and the formula
    #: then names the day to look up instead
    on: date | None


@dataclass(frozen=True, slots=True)
class InvoicePlan:
    """How the invoice is worked out: the act's total, in its currency, split into terms."""

    currency: str
    total: Decimal
    #: where the act writes its total - or its remainder, when it states the fee only in parts
    anchor: tuple[int, int]
    terms: tuple[InvoiceTerm, ...]
    #: where the advance came from: all of it entered by the user, or some of it read from the
    #: act (to be confirmed); None when no advance was paid
    advance_source: Literal["input", "act"] | None

    @property
    def advances(self) -> tuple[InvoiceTerm, ...]:
        return tuple(t for t in self.terms if t.part == "advance")


@dataclass(frozen=True, slots=True)
class NoInvoice:
    """Why no invoice could be worked out. The rule reports it as not run."""

    reason_ka: str
    reason_en: str


def fee_currencies(amounts: Sequence[RoledAmount]) -> list[str]:
    """The currencies the act's fee is written in - its price, its advance, its remainder, or an
    amount it calls nothing - its first price's first, then in document order. A penalty, a
    guarantee or a tax amount says nothing about which currency the act is in."""
    fee = [r for r in amounts if r.role not in _NOT_THE_FEE]
    first = [r for r in fee if r.role == "price"][:1]
    return list(dict.fromkeys(r.money.currency for r in first + fee))


def foreign_currencies(amounts: Sequence[RoledAmount]) -> list[str]:
    """The foreign currencies the act is written in; none when it states its price in lari. A
    dollar amount beside a lari price - "100 ლარი (ეკვივალენტი 37 აშშ დოლარი)" - is an equivalent
    or a rate, and the act is in lari all the same."""
    currencies = fee_currencies(amounts)
    if not currencies or currencies[0] == LOCAL_CURRENCY:
        return []
    return [c for c in currencies if c != LOCAL_CURRENCY]


def _total(
    mine: Sequence[RoledAmount], advances: Sequence[Decimal]
) -> tuple[Decimal, tuple[int, int]]:
    """The act's total, and where it is written. ``mine`` is every fee amount in the act's
    currency, and holds at least one.

    The largest price, or failing any, the largest amount the act calls nothing. An act that
    states its fee only in parts adds them up: its remainder and ``advances``, and the finding is
    marked on the remainder.
    """
    fees = [r.money for r in mine if r.role == "price"] or [r.money for r in mine if r.role is None]
    if fees:
        top = max(fees, key=lambda m: m.value)
        return to_money(top.value), top.span
    rests = [r.money for r in mine if r.role == "remainder"]
    anchor = (rests or [r.money for r in mine])[0].span
    rest = sum(dict.fromkeys(to_money(m.value) for m in rests), Decimal(0))
    return rest + sum(advances, Decimal(0)), anchor


def _money_text(value: Decimal, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def plan_invoice(
    amounts: Sequence[RoledAmount],
    advance: AdvanceTerms,
    *,
    act_date: date | None,
    entered: AdvanceInput | None = None,
) -> InvoicePlan | NoInvoice | None:
    """How to invoice the act in lari, why that cannot be worked out, or None for an act in lari.

    ``amounts`` and ``advance`` are the act's own (``contract_terms.amount_roles`` and
    ``read_advance(..., in_act=True)``). What the user entered comes first, field by field, and
    the act fills in the rest: an entered amount with no date takes the act's advance date, an
    entered date with no amount the act's amount. Each advance the act states is a term of its
    own, dated by its payment; an entered advance is one term. An advance equal to the total is
    the whole invoice; one above it means a figure is wrong somewhere, and nothing is worked out.
    """
    currencies = fee_currencies(amounts)
    if not currencies:
        return NoInvoice(
            "აქტში ვალუტიანი თანხა ვერ მოიძებნა, ამიტომ ვერ დადგინდა, სჭირდება თუ არა "
            "ანგარიშფაქტურის ლარში გადაანგარიშება",
            "no amount with a currency was found in the act, so whether its invoice needs "
            "converting to lari could not be decided",
        )
    foreign = foreign_currencies(amounts)
    if not foreign:
        return None
    if len(foreign) > 1:
        listed = ", ".join(foreign)
        return NoInvoice(
            f"აქტში თანხები რამდენიმე უცხოურ ვალუტაშია ({listed}), ამიტომ ანგარიშფაქტურა ლარში "
            "არ გადაანგარიშებულა",
            f"the act states amounts in more than one foreign currency ({listed}), so its invoice "
            "was not converted to lari",
        )
    currency = foreign[0]

    # the act's advances in its currency, each payment once however often the act restates it
    stated = list(
        dict.fromkeys(
            (to_money(p.money.value), p.paid_on)
            for p in advance.payments
            if p.money.currency == currency
        )
    )
    advances: list[tuple[Decimal, date | None]]
    source: Literal["input", "act"] | None
    if entered is not None and entered.amount is not None:
        act_day = next((d for _, d in stated if d is not None), None)
        paid_on = entered.paid_on or act_day
        advances = [(entered.amount, paid_on)] if entered.amount > 0 else []
        # the day comes from the act only when the user gave an amount and no day
        from_act = bool(advances) and entered.paid_on is None and act_day is not None
        source = "act" if from_act else "input"
    elif stated:
        if entered is not None and entered.paid_on is not None:
            # a day entered for the act's advance: one payment, made that day
            advances = [(sum((a for a, _ in stated), Decimal(0)), entered.paid_on)]
        else:
            advances = stated
        source = "act"
    elif advance.stated:
        # the act says an advance was paid and not how much: converting the whole fee on the
        # act's date would be a wrong invoice, not an incomplete one
        return NoInvoice(
            f"აქტში ავანსი ნახსენებია, მაგრამ მისი თანხა {currency}-ში მითითებული არ არის, "
            "ამიტომ ანგარიშფაქტურა არ გადაანგარიშებულა; მიუთითეთ ავანსი და შეამოწმეთ ხელახლა",
            f"the act mentions an advance but not its amount in {currency}, so the invoice was "
            "not worked out; enter the advance and run the check again",
        )
    else:
        advances, source = [], None

    mine = [r for r in amounts if r.money.currency == currency and r.role not in _NOT_THE_FEE]
    # an act that states its fee only in parts is added up from its own advance when it has one
    value, anchor = _total(mine, [a for a, _ in stated] or [a for a, _ in advances])
    paid = sum((a for a, _ in advances), Decimal(0))
    if paid > value:
        return NoInvoice(
            f"ავანსი ({_money_text(paid, currency)}) აღემატება აქტის თანხას "
            f"({_money_text(value, currency)}), ამიტომ ანგარიშფაქტურა არ გადაანგარიშებულა",
            f"the advance ({_money_text(paid, currency)}) is more than the act's total "
            f"({_money_text(value, currency)}), so the invoice was not worked out",
        )
    terms = [InvoiceTerm(amount=a, part="advance", on=d) for a, d in advances]
    if paid < value:
        terms.append(InvoiceTerm(amount=value - paid, part="rest", on=act_date))
    return InvoicePlan(
        currency=currency, total=value, anchor=anchor, terms=tuple(terms), advance_source=source
    )


# --- the rates --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PricedTerm:
    """A term and its value in lari, when its day has a rate."""

    term: InvoiceTerm
    #: None when the day is unknown or the source has no usable rate for it
    quote: RateQuote | None
    #: the term in lari, rounded half up to the tetri; None without a rate
    gel: Decimal | None


def price_terms(plan: InvoicePlan, rates: RateProvider) -> tuple[PricedTerm, ...]:
    """Each term at its day's rate. A rate that is not a positive number, or is for another
    currency, is no rate: a broken source must not produce an invoice total."""
    out: list[PricedTerm] = []
    for term in plan.terms:
        quote = rates.rate(plan.currency, term.on) if term.on is not None else None
        if quote is not None and (
            quote.currency != plan.currency or not quote.rate.is_finite() or quote.rate <= 0
        ):
            quote = None
        gel = None
        if quote is not None:
            gel = (term.amount * quote.rate).quantize(_TETRI, rounding=ROUND_HALF_UP)
        out.append(PricedTerm(term=term, quote=quote, gel=gel))
    return tuple(out)


def invoice_gel(priced: Sequence[PricedTerm]) -> Decimal | None:
    """The invoice total in lari: the rounded terms, summed. None unless every term has a rate."""
    values = [p.gel for p in priced if p.gel is not None]
    if not priced or len(values) != len(priced):
        return None
    return sum(values, Decimal(0))
