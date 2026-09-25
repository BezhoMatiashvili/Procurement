"""The act against its contract's term and payment terms (client rules 10.5 and 10.7).

The pair here states everything those rules read: a signing date, a performance window, a
validity with a last day, a payment deadline, a payment method and an advance. The smaller pair
in ``test_aoa_check`` states none of the payment terms, so its clean test never runs these rules.
Each test plants one defect. Synthetic text only.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from tests.unit.test_aoa_check import ACT as SHORT_ACT
from tests.unit.test_aoa_check import CONTRACT as SHORT_CONTRACT

CONTRACT = """ხელშეკრულება
საქართველო, თბილისი                    1 მარტი, 2026 წელი
შპს „ალფა ჯგუფი“. საიდენტიფიკაციო კოდი: 123456789; იურიდიული მისამართი: ქ. თბილისი,
რუსთაველის 12 (შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის ნინო კაპანაძის სახით და
შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321, იურიდიული მისამართი: ქ. ბათუმი,
ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის დავით მელაძის სახით
1.1. „შემსრულებელი“ იღებს ვალდებულებას „მზე“ პროექტისთვის (ს/კ: 01.02.03.456) მომსახურების გაწევა.
2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან - 2026 წლის 1 სექტემბრის ჩათვლით.
3.1. ხელშეკრულება ძალაში შედის მისი ხელმოწერის მომენტიდან და მოქმედებს 2026 წლის 31 დეკემბრამდე.
3.2. „შემკვეთს“ უფლება აქვს შეწყვიტოს ხელშეკრულება შეწყვეტამდე 5 (ხუთი) კალენდარული დღით ადრე
შეტყობინების საფუძველზე.
4.1. ღირებულება შეადგენს 1 000 (ათასი) ლარს, რომელიც მოიცავს გადასახადებს მათ შორის დღგ-ს.
4.2. ავანსი შეადგენს ღირებულების 30%-ს.
4.3. დარჩენილი თანხა გადაიხდება მიღება-ჩაბარების აქტის გაფორმების თარიღიდან 5 (ხუთი) სამუშაო
დღის ვადაში, „შემსრულებლის“ საბანკო ანგარიშზე გადარიცხვით.
4.4. ანგარიშსწორება: სს „საქართველოს ბანკი“, ბანკის კოდი: AAAAGE22,
ანგარიშის ნომერი: GE29NB0000000101904917.
"""

ACT = """მიღება-ჩაბარების აქტი
შპს „ალფა ჯგუფი“. საიდენტიფიკაციო კოდი: 123456789; იურიდიული მისამართი: ქ. თბილისი,
რუსთაველის 12 (შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის ნინო კაპანაძის სახით და
შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321, იურიდიული მისამართი: ქ. ბათუმი,
ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“)
„შემკვეთის“ პროექტ „მზე“ (ს/კ: 01.02.03.456) მომსახურების გაწევა.
2. შესრულების თარიღი
მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით.
3. საფასური
საფასური შეადგენს 1 000 (ათასი) ლარს გადასახადების ჩათვლით. ავანსი, ღირებულების 30%,
გადახდილია 2026 წლის 10 მარტს.
დარჩენილი თანხა გადაიხდება აქტის გაფორმების თარიღიდან 5 (ხუთი) სამუშაო დღის ვადაში,
შემსრულებლის საბანკო ანგარიშზე გადარიცხვით.
ბანკი: სს „საქართველოს ბანკი“;
ბანკის კოდი: AAAAGE22; ა/ა: GE29NB0000000101904917
4. გაფორმების თარიღი
აქტი გაფორმდა 2026 წლის 30 აპრილი
„შემკვეთი“
შპს „ალფა ჯგუფი“
დირექტორი ნინო კაპანაძე
„შემსრულებელი“
შპს „ბეტა სერვისი“
დირექტორი: დავით მელაძე
"""

_SIGNED = "აქტი გაფორმდა 2026 წლის 30 აპრილი"
_PERIOD = "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით."
_PERFORMANCE = "2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან - 2026 წლის 1 სექტემბრის ჩათვლით.\n"
_VALIDITY = (
    "3.1. ხელშეკრულება ძალაში შედის მისი ხელმოწერის მომენტიდან და მოქმედებს 2026 წლის 31 "
    "დეკემბრამდე.\n"
)
_ADVANCE = "4.2. ავანსი შეადგენს ღირებულების 30%-ს.\n"
#: the act's deadline and method, and the contract's
_ACT_TERMS = "5 (ხუთი) სამუშაო დღის ვადაში,\nშემსრულებლის საბანკო ანგარიშზე გადარიცხვით"
_CONTRACT_TERMS = "5 (ხუთი) სამუშაო\nდღის ვადაში, „შემსრულებლის“ საბანკო ანგარიშზე გადარიცხვით"


def _act(*, dated: str | None = None, period: str | None = None, terms: str | None = None) -> str:
    """The act with its date, its service period or its payment terms rewritten."""
    act = ACT
    if dated is not None:
        act = act.replace(_SIGNED, f"აქტი გაფორმდა {dated}")
    if period is not None:
        act = act.replace(_PERIOD, f"მომსახურება შესრულდა {period}.")
    if terms is not None:
        act = act.replace(_ACT_TERMS, terms)
    return act


def _check(act: str = ACT, contract: str = CONTRACT) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract)


def _only(report: CheckReport, rule_id: str) -> Finding:
    [finding] = [f for f in report.findings if f.rule_id == rule_id]
    assert finding.message_ka and finding.message_en
    return finding


def _skip_reasons(report: CheckReport, rule_id: str) -> list[str]:
    return [s.reason_en for s in report.skipped if s.rule_id == rule_id]


def test_a_correct_act_under_a_full_contract_has_no_findings() -> None:
    """Every term and payment rule runs here, and none of them fires: no errors, no warnings,
    not even a note - and nothing is skipped but the two inputs this pair does not supply and
    the subject clause and list of services it does not state (``test_aoa_scope`` has a pair
    that states them)."""
    report = _check()
    assert report.findings == []
    assert [s.rule_id for s in report.skipped] == [
        "AOA.TAX_WORDING",
        "AOA.SUBJECT",
        "AOA.SERVICES",
        "AOA.REGISTRY",
    ]


# --- the act's date against the term (10.5) ------------------------------------------------------


class TestActDateInTerm:
    def test_an_act_dated_after_the_validity_ends_is_an_error(self) -> None:
        report = _check(_act(dated="2027 წლის 15 იანვარი"))
        finding = _only(report, "AOA.ACT_DATE_IN_TERM")
        assert (finding.severity, finding.actual) == ("error", "2027-01-15")
        assert finding.expected == "2026-03-01..2026-12-31"
        assert finding.quote == "2027 წლის 15 იანვარი"
        assert "after the contract term ended (2026-12-31)" in finding.message_en

    def test_an_act_dated_before_the_contract_was_signed_is_an_error(self) -> None:
        act = _act(
            dated="2026 წლის 28 თებერვალი",
            period="2026 წლის 1 თებერვლიდან 2026 წლის 28 თებერვლის ჩათვლით",
        )
        finding = _only(_check(act), "AOA.ACT_DATE_IN_TERM")
        assert (finding.severity, finding.actual) == ("error", "2026-02-28")
        assert "before the contract was signed (2026-03-01)" in finding.message_en

    def test_an_act_dated_after_the_performance_window_only_is_a_warning(self) -> None:
        """The contract is still in force, so the act may be late rather than wrong."""
        finding = _only(_check(_act(dated="2026 წლის 15 სექტემბერი")), "AOA.ACT_DATE_IN_TERM")
        assert (finding.severity, finding.actual) == ("warning", "2026-09-15")
        assert finding.expected == "2026-03-01..2026-09-01"
        assert "rule 4" not in finding.message_en

    def test_a_month_end_act_after_a_mid_month_window_names_the_rule_4_conflict(self) -> None:
        """The audit's case: work until 20 August, and rule 4 dates the act 31 August - after the
        window. Which rule gives way is the client's call, so it is a warning that says so."""
        contract = CONTRACT.replace(
            "2026 წლის 1 სექტემბრის ჩათვლით", "2026 წლის 20 აგვისტოს ჩათვლით"
        )
        act = _act(
            dated="2026 წლის 31 აგვისტო",
            period="2026 წლის 1 აგვისტოდან 2026 წლის 20 აგვისტოს ჩათვლით",
        )
        report = _check(act, contract)
        finding = _only(report, "AOA.ACT_DATE_IN_TERM")
        assert finding.severity == "warning"
        assert "rule 4 dates an act on the last day of the month" in finding.message_en
        assert "მე-4 წესით" in finding.message_ka
        # rule 4 itself is satisfied, and nothing else is wrong with the act
        assert [f.rule_id for f in report.findings] == ["AOA.ACT_DATE_IN_TERM"]

    @pytest.mark.parametrize(
        "clause",
        [
            "5.1. წარმომადგენლის მინდობილობა მოქმედებს 2026 წლის 31 მარტამდე.",
            "5.1. ფასები ფიქსირებულია და მოქმედებს 2026 წლის 31 მარტის ჩათვლით.",
            "5.1. კონფიდენციალურობის ვალდებულება ძალაშია 2026 წლის 31 მარტამდე.",
        ],
    )
    def test_what_else_is_in_force_until_a_day_does_not_end_the_contract(self, clause: str) -> None:
        """The client's template is in force until the obligations are fulfilled - no last day.
        A power of attorney, a price list or a duty "in force until" a day does not lend the
        contract that day: an act dated after it is not an act after the contract ended."""
        contract = CONTRACT.replace(
            "მოქმედებს 2026 წლის 31 დეკემბრამდე", "მოქმედებს ვალდებულებების სრულად შესრულებამდე"
        ).replace("4.4.", f"{clause}\n4.4.")
        assert _check(ACT, contract).findings == []

    def test_an_act_with_no_date_is_a_skip_not_a_second_error(self) -> None:
        act = ACT.replace(f"4. გაფორმების თარიღი\n{_SIGNED}\n", "")
        report = _check(act)
        assert [f.rule_id for f in report.findings] == ["AOA.ACT_DATE"]
        assert _skip_reasons(report, "AOA.ACT_DATE_IN_TERM") == [
            "the act's date could not be found, so it was not compared with the contract's term"
        ]

    def test_a_contract_that_dates_nothing_is_a_skip(self) -> None:
        contract = (
            CONTRACT.replace(_PERFORMANCE, "")
            .replace(_VALIDITY, "")
            .replace("                    1 მარტი, 2026 წელი", "")
        )
        report = _check(ACT, contract)
        assert report.findings == []
        assert _skip_reasons(report, "AOA.ACT_DATE_IN_TERM") == [
            "the contract states neither when it was signed nor when its term ends, so the act's "
            "date was not compared with it"
        ]


# --- the service period against the term (10.5) ---------------------------------------------------


class TestPeriodInTerm:
    def test_the_period_is_held_to_the_performance_window(self) -> None:
        act = _act(
            dated="2026 წლის 31 ოქტომბერი",
            period="2026 წლის 1 ოქტომბრიდან 2026 წლის 31 ოქტომბრის ჩათვლით",
        )
        finding = _only(_check(act), "AOA.PERIOD_IN_TERM")
        assert (finding.expected, finding.actual) == (
            "2026-03-01..2026-09-01",
            "2026-10-01..2026-10-31",
        )
        assert "the contract's performance period" in finding.message_en

    def test_without_a_performance_window_a_validity_with_an_end_bounds_the_period(self) -> None:
        act = _act(
            dated="2027 წლის 31 იანვარი",
            period="2027 წლის 1 იანვრიდან 2027 წლის 31 იანვრის ჩათვლით",
        )
        finding = _only(_check(act, CONTRACT.replace(_PERFORMANCE, "")), "AOA.PERIOD_IN_TERM")
        assert finding.expected == "2026-03-01..2026-12-31"
        assert "the contract term" in finding.message_en

    @pytest.mark.parametrize(
        "clause",
        [
            # the audit's case: "including VAT" and a final payment date widened the term to 2027
            "4.2. თანხა მოიცავს დღგ-ს ჩათვლით; საბოლოო ანგარიშსწორება 2027 წლის 15 მარტამდე.",
            "4.2. ღირებულება შეადგენს 100 ლარს დღგ-ს ჩათვლით, 2027 წლის 15 მარტის ჩათვლით.",
        ],
    )
    def test_a_payment_date_or_including_vat_does_not_widen_the_term(self, clause: str) -> None:
        contract = SHORT_CONTRACT.replace("4.3.", f"{clause}\n4.3.")
        act = SHORT_ACT.replace(
            "2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით",
            "2026 წლის 1 დეკემბრიდან 2026 წლის 31 დეკემბრის ჩათვლით",
        ).replace("აქტი გაფორმდა 2026 წლის 30 აპრილი", "აქტი გაფორმდა 2026 წლის 31 დეკემბერი")
        finding = _only(_check(act, contract), "AOA.PERIOD_IN_TERM")
        assert finding.expected == "2026-03-01..2026-09-01"

    @pytest.mark.parametrize(
        "tail",
        [
            ", წინასწარ შეთანხმებული გრაფიკის შესაბამისად",
            ", as agreed in advance",
            " (წინასწარი შეთანხმებით)",
            ", ავანსი კი გადახდილია 2026 წლის 10 მარტს",
        ],
    )
    def test_an_advance_in_the_period_sentence_leaves_the_period_alone(self, tail: str) -> None:
        """A date is the advance's when the advance is named before it: the period's own dates
        stay, and rule 4 and the term check with them. Dropping the whole sentence lost the
        period, and reported a correct act for having none."""
        act = ACT.replace("30 აპრილის ჩათვლით.", f"30 აპრილის ჩათვლით{tail}.")
        report = _check(act)
        assert report.findings == []
        assert report.summary["execution_period"] == "2026-04-01..2026-04-30"

    def test_an_advance_paid_on_a_day_written_before_it_is_not_the_period(self) -> None:
        act = ACT.replace(
            " ავანსი, ღირებულების 30%,\nგადახდილია 2026 წლის 10 მარტს.",
            " 2026 წლის 10 მარტს გადახდილი ავანსი შეადგენს ღირებულების 30%-ს.",
        )
        report = _check(act)
        assert report.findings == []
        assert report.summary["execution_period"] == "2026-04-01..2026-04-30"

    def test_a_contract_with_no_term_is_a_skip_not_a_pass(self) -> None:
        """It used to fall back to the contract's first and last dates, whatever they dated."""
        contract = CONTRACT.replace(_PERFORMANCE, "").replace(_VALIDITY, "")
        report = _check(_act(period="2030 წლის 1 აპრილიდან 2030 წლის 30 აპრილის ჩათვლით"), contract)
        assert "AOA.PERIOD_IN_TERM" not in [f.rule_id for f in report.findings]
        assert _skip_reasons(report, "AOA.PERIOD_IN_TERM") == [
            "the contract states neither a performance period nor an end to its validity, so the "
            "execution period was not compared with it"
        ]

    def test_an_open_ended_validity_bounds_nothing(self) -> None:
        """ "Until the obligations are fulfilled" has no last day to be late against."""
        contract = CONTRACT.replace(_PERFORMANCE, "").replace(
            "მოქმედებს 2026 წლის 31 დეკემბრამდე", "მოქმედებს ვალდებულებების სრულად შესრულებამდე"
        )
        report = _check(_act(dated="2027 წლის 31 იანვარი", period="2027 წლის 31 იანვარს"), contract)
        assert [f.rule_id for f in report.findings] == []
        assert _skip_reasons(report, "AOA.PERIOD_IN_TERM")


# --- the payment terms (10.7) ---------------------------------------------------------------------


class TestPaymentTerms:
    def test_a_changed_deadline_is_an_error(self) -> None:
        act = _act(terms=_ACT_TERMS.replace("5 (ხუთი)", "10 (ათი)"))
        finding = _only(_check(act), "AOA.PAYMENT_DEADLINE")
        assert finding.severity == "error"
        assert (finding.expected, finding.actual) == (
            "5 (ხუთი) სამუშაო დღის",
            "10 (ათი) სამუშაო დღის",
        )
        assert finding.quote == "10 (ათი) სამუშაო დღის"
        assert "10 working days in the act, 5 working days in the contract" in finding.message_en

    def test_calendar_days_against_working_days_is_an_error(self) -> None:
        act = _act(terms=_ACT_TERMS.replace("სამუშაო", "კალენდარული"))
        finding = _only(_check(act), "AOA.PAYMENT_DEADLINE")
        assert finding.severity == "error"
        assert "counts calendar days in the act" in finding.message_en

    def test_a_deadline_that_does_not_say_which_days_count_is_a_warning(self) -> None:
        act = _act(terms=_ACT_TERMS.replace(" სამუშაო", ""))
        finding = _only(_check(act), "AOA.PAYMENT_DEADLINE")
        assert (finding.severity, finding.actual) == ("warning", "5 (ხუთი) დღის")
        assert (
            "only the contract says the payment deadline counts working days" in finding.message_en
        )

    @pytest.mark.parametrize(
        "lead",
        [
            "საანგარიშო პერიოდში (1 თვის განმავლობაში) გაწეული მომსახურების დარჩენილი თანხა",
            "3 თვის განმავლობაში გაწეული მომსახურების დარჩენილი თანხა",
            "დარჩენილი თანხა ყოველთვიურად, 6 თვის განმავლობაში,",
        ],
    )
    def test_how_long_the_work_ran_is_not_a_payment_deadline(self, lead: str) -> None:
        """ "N თვის განმავლობაში" in the payment sentence says how long the work ran, or over how
        long the fee is paid; the time limit is the five working days, as in the contract."""
        act = ACT.replace("დარჩენილი თანხა გადაიხდება", f"{lead} გადაიხდება")
        assert _check(act).findings == []

    def test_cash_against_a_transfer_is_an_error(self) -> None:
        act = _act(terms="5 (ხუთი) სამუშაო დღის ვადაში, ნაღდი ანგარიშსწორებით")
        finding = _only(_check(act), "AOA.PAYMENT_METHOD")
        assert finding.severity == "error"
        assert (finding.expected, finding.actual) == ("საბანკო ანგარიშზე", "ნაღდი")

    def test_unaghdo_is_not_cash(self) -> None:
        """ "უნაღდო" (non-cash) holds the letters of "ნაღდ" (cash)."""
        act = _act(terms="5 (ხუთი) სამუშაო დღის ვადაში, უნაღდო ანგარიშსწორებით")
        assert _check(act).findings == []

    def test_the_audits_changed_deadline_and_method_are_both_caught(self) -> None:
        """10 days by transfer in the contract, 90 days in cash in the act: nothing was
        reported before."""
        contract = CONTRACT.replace(_CONTRACT_TERMS, "10 დღის ვადაში, უნაღდო ანგარიშსწორებით")
        act = _act(terms="90 დღის ვადაში, ნაღდი ანგარიშსწორებით")
        report = _check(act, contract)
        assert [(f.rule_id, f.severity) for f in report.findings] == [
            ("AOA.PAYMENT_DEADLINE", "error"),
            ("AOA.PAYMENT_METHOD", "error"),
        ]

    def test_the_notice_period_is_not_a_payment_deadline(self) -> None:
        """The contract's five days' notice of termination normalises to the same five days as
        a deadline; with no deadline of its own, the contract has none to compare."""
        contract = CONTRACT.replace(
            "გაფორმების თარიღიდან 5 (ხუთი) სამუშაო\nდღის ვადაში, „შემსრულებლის“", "„შემსრულებლის“"
        )
        report = _check(ACT, contract)
        assert report.findings == []
        assert _skip_reasons(report, "AOA.PAYMENT_*") == [
            "stated in only one of the two documents, so not compared: the payment deadline "
            "(act only)"
        ]

    def test_parts_only_one_document_states_are_named_in_one_skip(self) -> None:
        act = _act(terms="5 (ხუთი) სამუშაო დღის ვადაში")
        report = _check(act, CONTRACT)
        assert report.findings == []
        assert _skip_reasons(report, "AOA.PAYMENT_*") == [
            "stated in only one of the two documents, so not compared: the payment method "
            "(contract only)"
        ]

    def test_a_contract_with_no_payment_terms_is_a_skip(self) -> None:
        report = _check(SHORT_ACT, SHORT_CONTRACT)
        assert _skip_reasons(report, "AOA.PAYMENT_*") == [
            "no payment terms (deadline, method, advance) were found in the contract, so the "
            "act's were not compared"
        ]


class TestPaymentAdvance:
    def test_a_changed_advance_share_is_an_error(self) -> None:
        act = ACT.replace("ღირებულების 30%", "ღირებულების 40%")
        finding = _only(_check(act), "AOA.PAYMENT_ADVANCE")
        assert (finding.severity, finding.expected, finding.actual) == ("error", "30%", "40%")
        assert finding.quote == "40%"

    def test_a_changed_advance_amount_is_an_error(self) -> None:
        contract = CONTRACT.replace(_ADVANCE, "4.2. ავანსი შეადგენს 300 (სამასი) ლარს.\n")
        act = ACT.replace("ავანსი, ღირებულების 30%,", "ავანსი 400 ლარი")
        finding = _only(_check(act, contract), "AOA.PAYMENT_ADVANCE")
        assert (finding.severity, finding.expected, finding.actual) == (
            "error",
            "300.00 GEL",
            "400.00 GEL",
        )

    def test_the_same_advance_amount_is_clean(self) -> None:
        contract = CONTRACT.replace(_ADVANCE, "4.2. ავანსი შეადგენს 300 (სამასი) ლარს.\n")
        act = ACT.replace("ავანსი, ღირებულების 30%,", "ავანსი 300 ლარი")
        assert _check(act, contract).findings == []

    def test_an_advance_the_contract_rules_out_is_an_error(self) -> None:
        contract = CONTRACT.replace(_ADVANCE, "4.2. ავანსი არ გაიცემა.\n")
        finding = _only(_check(ACT, contract), "AOA.PAYMENT_ADVANCE")
        assert finding.severity == "error"
        assert finding.quote == "30%"
        assert "which the contract rules out" in finding.message_en

    def test_an_advance_the_contract_does_not_provide_for_is_a_warning(self) -> None:
        finding = _only(_check(ACT, CONTRACT.replace(_ADVANCE, "")), "AOA.PAYMENT_ADVANCE")
        assert finding.severity == "warning"

    @pytest.mark.parametrize(
        "wording", ["ავანსი არ განხორციელებულა.", "ავანსი: 0 ლარი.", "ავანსი: არა."]
    )
    def test_an_act_that_says_no_advance_was_paid_states_none(self, wording: str) -> None:
        """Under a contract that provides for no advance, an act saying none was paid is right."""
        paid = " ავანსი, ღირებულების 30%,\nგადახდილია 2026 წლის 10 მარტს."
        act = ACT.replace(paid, f" {wording}")
        assert _check(act, CONTRACT.replace(_ADVANCE, "")).findings == []

    def test_an_act_that_does_not_mention_the_advance_is_not_wrong(self) -> None:
        act = ACT.replace(" ავანსი, ღირებულების 30%,\nგადახდილია 2026 წლის 10 მარტს.", "")
        report = _check(act)
        assert report.findings == []
        assert _skip_reasons(report, "AOA.PAYMENT_*") == [
            "stated in only one of the two documents, so not compared: the advance (contract only)"
        ]
