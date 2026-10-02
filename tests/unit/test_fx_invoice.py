"""Client rule 9: the invoice in lari for an act written in a foreign currency. Synthetic text.

The client's example: an act for 11,000 USD dated 17 September, 1,000 USD of it paid in advance
on 10 September. The advance goes at the NBG rate of its payment day and the rest at the rate of
the act's date: 1,000 x 2.6104 + 10,000 x 2.6081 = 2,610.40 + 26,081.00 = 28,691.40 GEL. Their
example without an advance writes 2,610.04 for 1,000 x 2.6104, which is 2,610.40; the tests hold
the arithmetic, and the typo is a question for the client.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from procurement_agent.checks.aoa import AoaChecker, DocumentView, check_aoa
from procurement_agent.checks.contract_terms import amount_roles, read_advance
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.checks.fx_invoice import (
    AdvanceInput,
    InvoicePlan,
    NoInvoice,
    invoice_gel,
    parse_advance,
    plan_invoice,
    price_terms,
    to_money,
)
from procurement_agent.config import Settings
from procurement_agent.fx.provider import NullRateProvider, RateQuote, open_rate_provider
from tests.unit.test_aoa_check import ACT, CONTRACT

_SEP_10, _SEP_17 = date(2026, 9, 10), date(2026, 9, 17)

_FEE = "საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით."
_USD_FEE = "საფასური შეადგენს 11 000 (თერთმეტი ათასი) აშშ დოლარს."
_ADVANCE = " ავანსი 1 000 აშშ დოლარი გადახდილია 2026 წლის 10 სექტემბერს."
_PERIOD = "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით."
_SIGNED = "აქტი გაფორმდა 2026 წლის 30 აპრილი"


def _usd_act(fee: str = _USD_FEE, advance: str = _ADVANCE, day: str = "17 სექტემბერ") -> str:
    """The act in dollars: its fee, an advance line, and the work done and signed on ``day``."""
    return (
        ACT.replace(_FEE, fee + advance)
        .replace(_PERIOD, f"მომსახურება შესრულდა 2026 წლის {day}ს.")
        .replace(_SIGNED, f"აქტი გაფორმდა 2026 წლის {day}ი")
    )


#: the act in dollars, with the client's advance
USD_ACT = _usd_act()
#: its contract: the price in dollars, and work until the end of the year
USD_CONTRACT = CONTRACT.replace("100 (ასი) ლარს", "11 000 (თერთმეტი ათასი) აშშ დოლარს").replace(
    "2026 წლის 1 სექტემბრის ჩათვლით", "2026 წლის 31 დეკემბრის ჩათვლით"
)
#: the client's example without an advance: 1,000 USD, act dated 10 September
SMALL_ACT = _usd_act(
    fee="საფასური შეადგენს 1 000 (ათასი) აშშ დოლარს.", advance="", day="10 სექტემბერ"
)
SMALL_CONTRACT = USD_CONTRACT.replace("11 000 (თერთმეტი ათასი)", "1 000 (ათასი)")


class StaticRateProvider:
    """Rates from a table, as a connector would answer them; it notes what was asked."""

    def __init__(self, table: dict[tuple[str, date], str]) -> None:
        self.table = {key: Decimal(value) for key, value in table.items()}
        self.asked: list[tuple[str, date]] = []
        self.closed = False

    def rate(self, currency: str, on: date) -> RateQuote | None:
        self.asked.append((currency, on))
        value = self.table.get((currency, on))
        if value is None:
            return None
        return RateQuote(currency=currency, rate=value, valid_on=on, source="NBG")

    def coverage(self, currency: str) -> tuple[date, date] | None:
        days = sorted(d for c, d in self.table if c == currency)
        return (days[0], days[-1]) if days else None

    def close(self) -> None:
        self.closed = True


#: the client's two rates
NBG = {("USD", _SEP_10): "2.6104", ("USD", _SEP_17): "2.6081"}


def _check(act: str = USD_ACT, contract: str | None = USD_CONTRACT, **kw: object) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract, **kw)  # type: ignore[arg-type]


def _note(report: CheckReport) -> Finding:
    [note] = [f for f in report.findings if f.rule_id == "AOA.FX_INVOICE"]
    assert note.severity == "info"
    return note


def _skip(report: CheckReport, rule_id: str) -> str:
    [reason] = [s.reason_en for s in report.skipped if s.rule_id == rule_id]
    return reason


# --- the whole check -------------------------------------------------------------------------


def test_a_correct_act_in_lari_has_no_invoice_note_or_skip() -> None:
    report = check_aoa(aoa_text=ACT, contract_text=CONTRACT)
    assert report.findings == []
    assert not [s for s in report.skipped if s.rule_id.startswith("AOA.FX")]
    assert report.summary["currency"] == "GEL"
    assert "fx_total" not in report.summary


def test_a_correct_act_in_dollars_is_clean_but_for_its_invoice_note() -> None:
    """No error and no warning - the act is right - and one note telling how to invoice it."""
    report = _check()
    assert [(f.rule_id, f.severity) for f in report.findings] == [("AOA.FX_INVOICE", "info")]
    assert "AOA.FX_RATE" in [s.rule_id for s in report.skipped]


def test_the_clients_example_with_an_advance() -> None:
    note = _note(_check(rates=StaticRateProvider(NBG)))
    assert (
        "1,000.00 USD × NBG rate of 2026-09-10 (2.6104) = 2,610.40 GEL + 10,000.00 USD "
        "× NBG rate of 2026-09-17 (2.6081) = 26,081.00 GEL; in total 28,691.40 GEL"
    ) in note.message_en
    assert "სულ 28,691.40 GEL" in note.message_ka
    assert (note.expected, note.actual) == (None, None)  # a "should be" would read as a mistake


def test_the_clients_example_without_an_advance() -> None:
    report = _check(SMALL_ACT, SMALL_CONTRACT, rates=StaticRateProvider(NBG))
    note = _note(report)
    assert "1,000.00 USD × NBG rate of 2026-09-10 (2.6104) = 2,610.40 GEL" in note.message_en
    assert "2,610.04" not in note.message_en
    assert "in total" not in note.message_en  # one term is its own total
    assert report.summary["invoice_gel"] == "2610.40"
    assert not [s for s in report.skipped if s.rule_id == "AOA.FX_RATE"]


def test_without_rates_the_note_gives_the_formula_and_the_skip_names_both_days() -> None:
    report = _check()
    note = _note(report)
    assert note.message_en.startswith(
        "The act is in USD. The invoice must be: 1,000.00 USD × NBG rate of 2026-09-10 + "
        "10,000.00 USD × NBG rate of 2026-09-17 — verify the invoice."
    )
    assert "NBG-ის კურსი (2026-09-10)" in note.message_ka
    assert "GEL" not in note.message_en  # no total without rates
    reason = _skip(report, "AOA.FX_RATE")
    assert reason.startswith("no exchange-rate source is connected")
    assert reason.endswith("2026-09-10, 2026-09-17")


def test_the_note_is_marked_on_the_acts_total() -> None:
    note = _note(_check())
    assert note.quote == "11 000 (თერთმეტი ათასი) აშშ დოლარს"


def test_an_act_stating_its_fee_only_in_parts_is_marked_on_the_remainder() -> None:
    act = _usd_act(fee="დარჩენილი თანხა 10 000 აშშ დოლარი.")
    report = _check(act, rates=StaticRateProvider(NBG))
    note = _note(report)
    assert note.quote == "10 000 აშშ დოლარი"
    assert report.summary["fx_total"] == "11000.00"
    assert "in total 28,691.40 GEL" in note.message_en


def test_the_advance_is_read_from_the_act_with_its_payment_day() -> None:
    report = _check()
    assert "The advance is taken from the act: confirm it." in _note(report).message_en
    assert "ავანსი აღებულია აქტიდან" in _note(report).message_ka
    summary = report.summary
    assert (summary["advance_amount"], summary["advance_date"], summary["advance_source"]) == (
        "1000.00",
        "2026-09-10",
        "act",
    )
    assert (summary["currency"], summary["fx_total"]) == ("USD", "11000.00")


def test_the_acts_date_is_not_taken_from_an_advance_clause() -> None:
    """A deadline counted from the act's date, then the advance's payment day: the remainder
    still takes the act's own date, 17 September."""
    act = USD_ACT.replace(
        "4. გაფორმების თარიღი",
        "თანხა გადაიხდება აქტის გაფორმების თარიღიდან 5 სამუშაო დღის ვადაში; ავანსი 1 000 აშშ "
        "დოლარი გადახდილია 2026 წლის 10 სექტემბერს.\n4. გაფორმების თარიღი",
    )
    report = _check(act, rates=StaticRateProvider(NBG))
    assert report.summary["act_date"] == "2026-09-17"
    assert "in total 28,691.40 GEL" in _note(report).message_en


def test_an_entered_advance_overrides_the_acts() -> None:
    entered = AdvanceInput(amount=Decimal("2000.00"), paid_on=_SEP_10)
    report = _check(advance=entered, rates=StaticRateProvider(NBG))
    note = _note(report)
    assert "2,000.00 USD × NBG rate of 2026-09-10 (2.6104) = 5,220.80 GEL" in note.message_en
    assert "9,000.00 USD × NBG rate of 2026-09-17 (2.6081) = 23,472.90 GEL" in note.message_en
    assert "confirm" not in note.message_en  # nothing was read from the act
    assert report.summary["advance_source"] == "input"


def test_an_entered_amount_takes_the_acts_payment_day() -> None:
    report = _check(advance=AdvanceInput(amount=Decimal("2000.00")))
    assert "2,000.00 USD × NBG rate of 2026-09-10" in _note(report).message_en
    assert report.summary["advance_source"] == "act"  # the day is the act's, to be confirmed


def test_an_entered_day_takes_the_acts_advance_amount() -> None:
    act = _usd_act(advance=" ავანსი 1 000 აშშ დოლარი გადახდილია.")
    report = _check(act, advance=AdvanceInput(paid_on=date(2026, 9, 11)))
    assert "1,000.00 USD × NBG rate of 2026-09-11" in _note(report).message_en


def test_an_entered_zero_says_no_advance_was_paid() -> None:
    """The act's advance is not read either; and the summary keeps the 0, so the report's form
    offers it back instead of a blank that would read the act's advance again."""
    report = _check(advance=AdvanceInput(amount=Decimal(0)), rates=StaticRateProvider(NBG))
    note = _note(report)
    assert "11,000.00 USD × NBG rate of 2026-09-17 (2.6081) = 28,689.10 GEL" in note.message_en
    assert "+" not in note.message_en
    assert (report.summary["advance_amount"], report.summary["advance_source"]) == (
        "0.00",
        "input",
    )
    assert "advance_date" not in report.summary


def test_an_advance_of_nothing_is_no_advance() -> None:
    """ "ავანსი: 0" is the act saying none was paid: the whole fee goes at the act's rate, with
    no term of 0.00 at an unknown day's rate."""
    report = _check(_usd_act(advance=" ავანსი: 0 აშშ დოლარი."), rates=StaticRateProvider(NBG))
    note = _note(report)
    assert "11,000.00 USD × NBG rate of 2026-09-17 (2.6081) = 28,689.10 GEL" in note.message_en
    assert "+" not in note.message_en and "confirm" not in note.message_en
    assert "advance_source" not in report.summary


def test_a_contract_advance_is_never_taken_for_a_payment() -> None:
    """A contract plans an advance - as an amount or a share - and says nothing of whether, or
    when, it was paid."""
    for clause in ("4.2. ავანსი შეადგენს 1 000 აშშ დოლარს.", "4.2. ავანსი შეადგენს 30%-ს."):
        contract = USD_CONTRACT.replace("4.3.", f"{clause}\n4.3.")
        report = _check(_usd_act(advance=""), contract, rates=StaticRateProvider(NBG))
        assert "11,000.00 USD × NBG rate of 2026-09-17" in _note(report).message_en
        assert "advance_amount" not in report.summary


def test_the_second_tranche_is_the_remainder_not_an_advance() -> None:
    act = _usd_act(
        advance=_ADVANCE + " მეორე ტრანში 10 000 აშშ დოლარი გადაიხდება აქტის საფუძველზე."
    )
    report = _check(act, rates=StaticRateProvider(NBG))
    assert "in total 28,691.40 GEL" in _note(report).message_en


def test_an_advance_with_no_payment_day_is_named_for_what_it_is() -> None:
    report = _check(_usd_act(advance=" ავანსი 1 000 აშშ დოლარი გადახდილია."))
    note = _note(report)
    assert "1,000.00 USD × NBG rate of the advance payment date (not stated)" in note.message_en
    assert "ავანსის გადახდის დღე — მითითებული არ არის" in note.message_ka
    assert _skip(report, "AOA.FX_RATE").endswith(
        "rate of: the advance payment date (not stated), 2026-09-17"
    )


def test_an_act_with_no_date_of_its_own_is_named_for_what_it_is() -> None:
    act = USD_ACT.replace("4. გაფორმების თარიღი\nაქტი გაფორმდა 2026 წლის 17 სექტემბერი\n", "")
    note = _note(_check(act))
    assert "10,000.00 USD × NBG rate of the act's date (not found)" in note.message_en


def test_an_advance_the_act_mentions_without_its_amount_is_a_skip() -> None:
    """Converting the whole fee on the act's date would be a wrong invoice, not a short one."""
    report = _check(_usd_act(advance=" ავანსი გადახდილია 2026 წლის 10 სექტემბერს."))
    assert not [f for f in report.findings if f.rule_id == "AOA.FX_INVOICE"]
    assert _skip(report, "AOA.FX_INVOICE").startswith(
        "the act mentions an advance but not its amount in USD"
    )
    # entered, it is used
    entered = AdvanceInput(amount=Decimal(1000), paid_on=_SEP_10)
    act = _usd_act(advance=" ავანსი გადახდილია 2026 წლის 10 სექტემბერს.")
    note = _note(_check(act, advance=entered))
    assert "1,000.00 USD × NBG rate of 2026-09-10 + 10,000.00 USD" in note.message_en


def test_an_advance_above_the_total_is_a_skip_that_keeps_what_was_entered() -> None:
    report = _check(advance=AdvanceInput(amount=Decimal(20000)))
    assert not [f for f in report.findings if f.rule_id == "AOA.FX_INVOICE"]
    assert _skip(report, "AOA.FX_INVOICE") == (
        "the advance (20,000.00 USD) is more than the act's total (11,000.00 USD), so the "
        "invoice was not worked out"
    )
    # the form on the report offers the entered figure back, to be corrected
    assert (report.summary["advance_amount"], report.summary["advance_source"]) == (
        "20000.00",
        "input",
    )


def test_an_advance_equal_to_the_total_is_the_whole_invoice() -> None:
    entered = AdvanceInput(amount=Decimal(11000), paid_on=_SEP_10)
    note = _note(_check(advance=entered, rates=StaticRateProvider(NBG)))
    assert "11,000.00 USD × NBG rate of 2026-09-10 (2.6104) = 28,714.40 GEL" in note.message_en
    assert "2026-09-17" not in note.message_en


def test_an_act_priced_in_lari_with_a_dollar_equivalent_is_in_lari() -> None:
    """A dollar amount beside a lari price is an equivalent: the act is in lari, and its invoice
    needs no conversion."""
    equivalent = "100 (ასი) ლარს (ეკვივალენტი 37 აშშ დოლარი)"
    act = ACT.replace("100 (ასი) ლარს", equivalent)
    report = check_aoa(aoa_text=act, contract_text=CONTRACT.replace("100 (ასი) ლარს", equivalent))
    assert report.findings == []
    assert not [s for s in report.skipped if s.rule_id.startswith("AOA.FX")]
    assert report.summary["currency"] == "GEL"


def test_an_act_priced_in_dollars_with_a_lari_equivalent_is_in_dollars() -> None:
    act = _usd_act(fee=_USD_FEE.replace("დოლარს.", "დოლარს (ეკვივალენტი 28 691.40 ლარი)."))
    report = _check(act)
    assert "10,000.00 USD × NBG rate of 2026-09-17" in _note(report).message_en
    assert report.summary["currency"] == "USD"


def test_amounts_in_two_foreign_currencies_are_a_skip() -> None:
    act = _usd_act(advance=" დამატებით: 500 ევრო.")
    report = _check(act)
    assert not [f for f in report.findings if f.rule_id == "AOA.FX_INVOICE"]
    assert "more than one foreign currency (USD, EUR)" in _skip(report, "AOA.FX_INVOICE")


def test_an_act_with_no_amount_is_a_skip() -> None:
    act = ACT.replace(_FEE, "საფასური განისაზღვრება დანართით.")
    report = check_aoa(aoa_text=act)
    assert _skip(report, "AOA.FX_INVOICE").startswith("no amount with a currency was found")
    assert "currency" not in report.summary


@pytest.mark.parametrize("contractor_type", ["small_business", "foreign"])
def test_the_exempt_types_get_nothing(contractor_type: str) -> None:
    """The rule does not apply to them - not a note, not a skip; the summary says so."""
    report = _check(contractor_type=contractor_type)
    assert not [f for f in report.findings if f.rule_id.startswith("AOA.FX")]
    assert not [s for s in report.skipped if s.rule_id.startswith("AOA.FX")]
    assert report.summary["not_applicable"] == "AOA.FX_INVOICE"
    assert report.summary["currency"] == "USD"
    assert "fx_total" not in report.summary


def test_the_exempt_types_get_no_skip_either_when_nothing_could_be_worked_out() -> None:
    act = _usd_act(advance=" დამატებით: 500 ევრო.")
    report = _check(act, contractor_type="foreign")
    assert not [s for s in report.skipped if s.rule_id.startswith("AOA.FX")]


def test_an_unknown_contractor_type_is_calculated_and_says_who_needs_none() -> None:
    unset = _note(_check())
    assert "A small business or a foreign organisation needs no such invoice." in unset.message_en
    assert "მცირე მეწარმესა და უცხოურ ორგანიზაციას" in unset.message_ka
    for contractor_type in ("company", "natural_person", "individual_entrepreneur_vat"):
        note = _note(_check(contractor_type=contractor_type))
        assert "needs no such invoice" not in note.message_en


def test_without_a_rate_source_the_null_provider_is_used() -> None:
    reason = _skip(_check(), "AOA.FX_RATE")
    assert reason.startswith("no exchange-rate source is connected")
    assert isinstance(open_rate_provider(Settings(fx_rate_source="none")), NullRateProvider)


def test_a_source_missing_a_day_names_that_day_and_what_it_holds() -> None:
    rates = StaticRateProvider({("USD", _SEP_17): "2.6081"})
    report = _check(rates=rates)
    assert _skip(report, "AOA.FX_RATE") == (
        "the invoice total in lari was not calculated: no NBG USD rate was found for 2026-09-10 "
        "(the imported rates cover 2026-09-17 … 2026-09-17); enter the rate or import newer rates"
    )
    assert "in total" not in _note(report).message_en
    assert rates.asked == [("USD", _SEP_10), ("USD", _SEP_17)]


def test_running_the_check_twice_reports_the_note_once() -> None:
    checker = AoaChecker(
        aoa=DocumentView.build("a", USD_ACT), contract=DocumentView.build("c", USD_CONTRACT)
    )
    first, second = checker.run(), checker.run()
    assert first.findings == second.findings
    assert first.skipped == second.skipped
    assert first.summary == second.summary


# --- the arithmetic --------------------------------------------------------------------------


def _plan(act: str = USD_ACT, **kw: object) -> InvoicePlan | NoInvoice | None:
    roled = amount_roles(DocumentView.build("a", act).text)
    advance = read_advance(DocumentView.build("a", act).text, in_act=True)
    return plan_invoice(roled, advance, act_date=_SEP_17, **kw)  # type: ignore[arg-type]


def test_each_term_is_rounded_half_up_and_then_summed() -> None:
    """261.045 and 260.815 round to 261.05 and 260.82: 521.87. Rounding the sum instead would
    give 521.86."""
    plan = _plan(
        _usd_act(fee="საფასური შეადგენს 200 (ორასი) აშშ დოლარს.", advance=""),
        entered=AdvanceInput(amount=Decimal(100), paid_on=_SEP_10),
    )
    assert isinstance(plan, InvoicePlan)
    rates = StaticRateProvider({("USD", _SEP_10): "2.61045", ("USD", _SEP_17): "2.60815"})
    priced = price_terms(plan, rates)
    assert [p.gel for p in priced] == [Decimal("261.05"), Decimal("260.82")]
    assert invoice_gel(priced) == Decimal("521.87")


@pytest.mark.parametrize("bad", ["0", "-2.6081", "NaN"])
def test_a_rate_that_is_not_a_positive_number_is_no_rate(bad: str) -> None:
    plan = _plan()
    assert isinstance(plan, InvoicePlan)
    priced = price_terms(
        plan, StaticRateProvider({("USD", _SEP_10): "2.6104", ("USD", _SEP_17): bad})
    )
    assert [p.quote is None for p in priced] == [False, True]
    assert invoice_gel(priced) is None


def test_a_rate_for_another_currency_is_no_rate() -> None:
    class Wrong(StaticRateProvider):
        def rate(self, currency: str, on: date) -> RateQuote | None:
            return RateQuote(currency="EUR", rate=Decimal(3), valid_on=on, source="NBG")

    plan = _plan()
    assert isinstance(plan, InvoicePlan)
    assert invoice_gel(price_terms(plan, Wrong({}))) is None


def test_a_plan_for_an_act_in_lari_is_none() -> None:
    assert _plan(ACT) is None


def test_the_plan_splits_the_total_at_the_advance() -> None:
    plan = _plan()
    assert isinstance(plan, InvoicePlan)
    assert (plan.currency, plan.total, plan.advance_source) == ("USD", Decimal("11000.00"), "act")
    assert [(t.part, t.amount, t.on) for t in plan.terms] == [
        ("advance", Decimal("1000.00"), _SEP_10),
        ("rest", Decimal("10000.00"), _SEP_17),
    ]


def test_an_advance_the_act_restates_is_one_payment() -> None:
    """A clause repeated in an annex is the same payment, not a second one."""
    plan = _plan(_usd_act(advance=_ADVANCE + _ADVANCE))
    assert isinstance(plan, InvoicePlan)
    assert [t.amount for t in plan.terms] == [Decimal("1000.00"), Decimal("10000.00")]


def test_money_goes_through_its_decimal_form() -> None:
    """Decimal(2610.4) carries the float's binary tail; the invoice must not."""
    assert to_money(2610.4) == Decimal("2610.40")
    assert str(to_money(0.1 + 0.2)) == "0.30"


# --- what the user entered -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "day", "expected"),
    [
        ("1,000", None, AdvanceInput(amount=Decimal("1000.00"))),
        ("1 000,00", "2026-09-10", AdvanceInput(amount=Decimal("1000.00"), paid_on=_SEP_10)),
        ("1000.5", "", AdvanceInput(amount=Decimal("1000.50"))),
        ("0", None, AdvanceInput(amount=Decimal("0.00"))),
        ("", "2026-09-10", AdvanceInput(paid_on=_SEP_10)),
        # what does not read is left out, the rest kept
        ("abc", "2026-09-10", AdvanceInput(paid_on=_SEP_10)),
        ("1000", "10.09.2026", AdvanceInput(amount=Decimal("1000.00"))),
        ("1000", "1800-01-01", AdvanceInput(amount=Decimal("1000.00"))),
    ],
)
def test_the_entered_advance_is_read_as_people_write_it(
    amount: str, day: str | None, expected: AdvanceInput
) -> None:
    assert parse_advance(amount, day) == expected


@pytest.mark.parametrize(
    ("amount", "day"),
    [
        (None, None),
        ("", " "),
        ("abc", "yesterday"),
        ("-5", None),  # a negative advance
        ("0.125", None),  # no money has three decimals
        ("1" * 40, None),  # nor forty digits
        ("1e20", None),
    ],
)
def test_an_entered_advance_that_does_not_read_is_none(amount: str | None, day: str | None) -> None:
    assert parse_advance(amount, day) is None
