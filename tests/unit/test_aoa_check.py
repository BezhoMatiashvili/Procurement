"""The AOA rule engine, against synthetic Georgian documents.

Each test plants exactly one defect into a known-good pair, mirroring the shape of the defects
the client asks us to find. The first test - that a correct act produces no errors - is the one
that matters most: a checker that fires on a clean document is worse than no checker.
"""

from __future__ import annotations

from datetime import date

import pytest

from procurement_agent.checks.aoa import DocumentView, add_months, check_aoa

CONTRACT = """ხელშეკრულება
საქართველო, თბილისი                    1 მარტი, 2026 წელი
შპს „ალფა ჯგუფი“. საიდენტიფიკაციო კოდი: 123456789; იურიდიული მისამართი: ქ. თბილისი,
რუსთაველის 12 (შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის ნინო კაპანაძის სახით და
შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321, იურიდიული მისამართი: ქ. ბათუმი,
ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის დავით მელაძის სახით
1.1. „შემსრულებელი“ იღებს ვალდებულებას „მზე“ პროექტისთვის (ს/კ: 01.02.03.456) მომსახურების გაწევა.
2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან - 2026 წლის 1 სექტემბრის ჩათვლით.
4.1. ღირებულება შეადგენს 100 (ასი) ლარს, რომელიც მოიცავს გადასახადებს მათ შორის დღგ-ს.
4.3. ანგარიშსწორება: სს „საქართველოს ბანკი“, ბანკის კოდი: AAAAGE22,
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
საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით. ბანკი: სს „საქართველოს ბანკი“;
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


def errors(act: str = ACT, contract: str | None = CONTRACT, **kw: object) -> list[str]:
    report = check_aoa(aoa_text=act, contract_text=contract, **kw)  # type: ignore[arg-type]
    return [f.rule_id for f in report.errors]


def test_a_correct_act_produces_no_errors() -> None:
    """No errors, and no warnings either: the evaluation scores errors only, so a warning on a
    correct act would go unnoticed anywhere but here."""
    assert errors() == []
    report = check_aoa(aoa_text=ACT, contract_text=CONTRACT)
    assert [f.rule_id for f in report.findings if f.severity in ("error", "warning")] == []


def test_amount_in_words_must_match_the_digits() -> None:
    assert "AOA.AMOUNT_WORDS" in errors(ACT.replace("100 (ასი)", "100 (ორასი)"))


def test_a_misspelled_amount_in_words_is_an_error_with_its_correction() -> None:
    report = check_aoa(aoa_text=ACT.replace("100 (ასი)", "100 (ასოი)"), contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.AMOUNT_WORDS"]
    assert (finding.severity, finding.actual, finding.expected) == ("error", "ასოი", "ასი")


def test_unreadable_amount_in_words_is_a_warning_not_an_error() -> None:
    """Words that read as no number and as no misspelling of the figure: the reader may be the
    one at fault, so a person is asked to look."""
    report = check_aoa(aoa_text=ACT.replace("100 (ასი)", "100 (ასი ასი)"), contract_text=CONTRACT)
    assert report.ok
    assert any(f.rule_id == "AOA.AMOUNT_WORDS" and f.severity == "warning" for f in report.findings)


def test_act_must_be_dated_the_last_day_of_the_service_period() -> None:
    assert "AOA.ACT_DATE" in errors(ACT.replace("2026 წლის 30 აპრილი\n", "2026 წლის 5 მაისი\n"))


def test_execution_period_outside_the_contract_term_is_an_error() -> None:
    moved = ACT.replace("2026 წლის 1 აპრილიდან", "2025 წლის 1 აპრილიდან").replace(
        "2026 წლის 30 აპრილის", "2025 წლის 30 აპრილის"
    )
    assert "AOA.PERIOD_IN_TERM" in errors(moved)


def test_a_misspelled_month_is_reported_without_breaking_the_date() -> None:
    report = check_aoa(aoa_text=ACT.replace("1 აპრილიდან", "1 აპრლიდან"), contract_text=CONTRACT)
    assert any(f.rule_id == "AOA.SPELLING" for f in report.findings)
    # the date itself still parses, so the period rules keep working
    assert "AOA.PERIOD_IN_TERM" not in [f.rule_id for f in report.errors]


def test_a_misspelled_month_comes_with_its_correction_anchored_on_the_word() -> None:
    report = check_aoa(aoa_text=ACT.replace("1 აპრილიდან", "1 აპრლიდან"), contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.SPELLING"]
    assert finding.expected == "აპრილიდან"
    assert finding.quote == "აპრლიდან"  # the word itself, not the whole date


def test_a_misspelled_month_keeps_its_postposition_in_the_correction() -> None:
    """Every correct form is listed, "-ისთვის" too, so the correction keeps the act's case."""
    act = ACT.replace("30 აპრილის ჩათვლით", "30 აპრლისთვის")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    [finding] = report.findings
    assert (finding.rule_id, finding.actual, finding.expected) == (
        "AOA.SPELLING",
        "აპრლისთვის",
        "აპრილისთვის",
    )
    assert report.summary["execution_period"] == "2026-04-01..2026-04-30"


def test_a_misspelled_signatory_is_matched_to_the_contract_name() -> None:
    report = check_aoa(aoa_text=ACT.replace("დავით მელაძე", "დავთ მელაძე"), contract_text=CONTRACT)
    found = [f for f in report.errors if f.rule_id == "AOA.DIRECTOR"]
    assert found and "მელაძ" in (found[0].expected or "")
    assert "misspelling" in found[0].message_en


def test_a_different_company_under_the_right_id_code_is_caught() -> None:
    assert "AOA.PARTY_NAME" in errors(ACT.replace("შპს „ბეტა სერვისი“", "შპს „გამა“", 1))


def test_a_party_mismatch_names_the_contract_as_its_source() -> None:
    act = ACT.replace("შპს „ბეტა სერვისი“", "შპს „გამა“", 1)
    [finding] = check_aoa(aoa_text=act, contract_text=CONTRACT).errors
    assert "does not match the contract" in finding.message_en
    assert "ხელშეკრულებას" in finding.message_ka


def test_a_changed_address_is_caught() -> None:
    assert "AOA.PARTY_ADDRESS" in errors(ACT.replace("ჭავჭავაძის 5", "აღმაშენებლის 9"))


def test_a_changed_project_is_caught() -> None:
    assert "AOA.PROJECT_NAME" in errors(ACT.replace("პროექტ „მზე“", "პროექტ „მთვარე“"))


def test_a_changed_account_number_is_caught() -> None:
    changed = ACT.replace("GE29NB0000000101904917", "GE29NB0000000101904911")
    assert "AOA.BANK" in errors(changed)


def test_a_fee_absent_from_the_contract_is_caught() -> None:
    assert "AOA.FEE" in errors(ACT.replace("100 (ასი) ლარს", "700 (შვიდასი) ლარს"))


def test_an_id_code_absent_from_the_contract_is_caught() -> None:
    assert "AOA.ID_CODES" in errors(ACT.replace("987654321", "111222333"))


def test_the_same_mistake_read_twice_is_reported_once() -> None:
    """A clause restated in an annex, or a table cell the parser emits twice, is one mistake."""
    act = ACT.replace("100 (ასი)", "100 (ორასი)") + "დანართი: საფასური 100 (ორასი) ლარი.\n"
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    # the words disagree with the figure and state a fee the contract does not: one each
    assert sorted(f.rule_id for f in report.findings) == ["AOA.AMOUNT_WORDS", "AOA.FEE"]


def test_two_different_mistakes_of_one_rule_are_both_reported() -> None:
    act = ACT.replace("100 (ასი)", "100 (ორასი)") + "დანართი: საფასური 100 (სამასი) ლარი.\n"
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert [f.actual for f in report.findings if f.rule_id == "AOA.AMOUNT_WORDS"] == [
        "ორასი",
        "სამასი",
    ]
    assert [f.actual for f in report.findings if f.rule_id == "AOA.FEE"] == ["ორასი", "სამასი"]


_SIGNED = "4. გაფორმების თარიღი\nაქტი გაფორმდა 2026 წლის 30 აპრილი\n"
_PAYMENT = (
    "4. ანგარიშსწორება\nთანხა გადაიხდება აქტის გაფორმების თარიღიდან 5 სამუშაო დღის ვადაში; "
    "ავანსი გადახდილია 2026 წლის 10 აპრილს.\n"
)


def test_the_act_date_is_not_read_from_an_advance_clause() -> None:
    """A deadline counted "from the act's date" (გაფორმების თარიღიდან) is followed by the
    advance's date. Read as the act's own, it would report a correct act as misdated."""
    report = check_aoa(aoa_text=ACT.replace(_SIGNED, _PAYMENT + _SIGNED), contract_text=CONTRACT)
    assert report.summary["act_date"] == "2026-04-30"
    assert report.findings == []


def test_a_date_in_the_sentence_after_the_label_still_dates_the_act() -> None:
    act = ACT.replace(_SIGNED, "4. გაფორმების თარიღი.\n2026 წლის 30 აპრილი\n")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary["act_date"] == "2026-04-30"
    assert report.findings == []


def test_an_advance_date_never_dates_the_act_even_with_nothing_else_to_go_on() -> None:
    report = check_aoa(aoa_text=ACT.replace(_SIGNED, _PAYMENT), contract_text=CONTRACT)
    assert "act_date" not in report.summary
    [finding] = [f for f in report.findings if f.rule_id == "AOA.ACT_DATE"]
    assert finding.expected is None  # "the act's date could not be found", not a wrong date


def test_a_document_view_keeps_each_line_as_well_as_the_flat_text() -> None:
    view = DocumentView.build("a", "- პირველი  პუნქტი\n\n- მეორე პუნქტი\n")
    assert view.lines == ("- პირველი პუნქტი", "- მეორე პუნქტი")
    assert view.text == "- პირველი პუნქტი - მეორე პუნქტი"


def test_missing_project_and_cadastral_code_are_warnings() -> None:
    report = check_aoa(
        aoa_text=ACT.replace("პროექტ „მზე“ (ს/კ: 01.02.03.456)", "მომსახურების"),
        contract_text=CONTRACT,
    )
    ids = {f.rule_id for f in report.findings}
    assert {"AOA.PROJECT_PRESENT", "AOA.CADASTRAL_PRESENT"} <= ids


_CODE_IN_ACT = "„შემკვეთის“ პროექტ „მზე“ (ს/კ: 01.02.03.456) მომსახურების გაწევა."


def _act_with_code(code: str) -> str:
    """The act, its cadastral code written after a label, with the sentence's full stop."""
    return ACT.replace(
        _CODE_IN_ACT, f"„შემკვეთის“ პროექტ „მზე“ მომსახურების გაწევა, საკადასტრო კოდი: {code}."
    )


class TestCadastral:
    def test_a_code_followed_by_a_full_stop_is_read(self) -> None:
        report = check_aoa(aoa_text=_act_with_code("01.02.03.456"), contract_text=CONTRACT)
        assert report.findings == []  # neither "no code" nor "code differs"

    def test_a_wrong_code_followed_by_a_full_stop_is_caught(self) -> None:
        report = check_aoa(aoa_text=_act_with_code("01.02.03.999"), contract_text=CONTRACT)
        [finding] = report.findings
        assert (finding.rule_id, finding.actual) == ("AOA.CADASTRAL", "01.02.03.999")

    def test_a_unit_code_inside_the_contracts_parcel_is_not_an_error(self) -> None:
        act = ACT.replace("01.02.03.456", "01.02.03.456.001.01.502")
        assert check_aoa(aoa_text=act, contract_text=CONTRACT).findings == []

    def test_a_unit_code_inside_another_parcel_is_an_error(self) -> None:
        act = ACT.replace("01.02.03.456", "01.02.03.457.001.01.502")
        assert "AOA.CADASTRAL" in errors(act)

    def test_a_contract_without_a_code_is_a_skip_not_an_error(self) -> None:
        contract = CONTRACT.replace(" (ს/კ: 01.02.03.456)", "")
        report = check_aoa(
            aoa_text=ACT.replace("01.02.03.456", "01.02.03.999"), contract_text=contract
        )
        assert "AOA.CADASTRAL" not in [f.rule_id for f in report.findings]
        assert "AOA.CADASTRAL" in [s.rule_id for s in report.skipped]

    def test_an_act_without_a_code_gets_the_presence_warning_and_no_skip(self) -> None:
        report = check_aoa(aoa_text=ACT.replace(" (ს/კ: 01.02.03.456)", ""), contract_text=CONTRACT)
        assert [f.rule_id for f in report.findings] == ["AOA.CADASTRAL_PRESENT"]
        assert "AOA.CADASTRAL" not in [s.rule_id for s in report.skipped]


class TestNotApplicable:
    """Rules the client exempts a contractor type from are said to be exempt in the summary -
    not left silent, and not listed as "could not run", which would read as a gap."""

    def test_the_tax_wording_rules_do_not_apply_to_a_company(self) -> None:
        report = check_aoa(aoa_text=ACT, contract_text=CONTRACT, contractor_type="company")
        assert "AOA.TAX_WORDING" not in [f.rule_id for f in report.findings]
        assert "AOA.TAX_WORDING" not in [s.rule_id for s in report.skipped]
        assert report.summary["not_applicable"] == "AOA.TAX_WORDING"

    @pytest.mark.parametrize("contractor_type", ["small_business", "foreign"])
    def test_the_invoice_rule_does_not_apply_to_its_exempt_types(
        self, contractor_type: str
    ) -> None:
        report = check_aoa(aoa_text=ACT, contract_text=CONTRACT, contractor_type=contractor_type)  # type: ignore[arg-type]
        assert report.summary["not_applicable"] == "AOA.FX_INVOICE"

    @pytest.mark.parametrize(
        "contractor_type", [None, "natural_person", "individual_entrepreneur_vat"]
    )
    def test_every_rule_applies_to_the_other_types(self, contractor_type: str | None) -> None:
        report = check_aoa(aoa_text=ACT, contract_text=CONTRACT, contractor_type=contractor_type)  # type: ignore[arg-type]
        assert "not_applicable" not in report.summary


class TestRulesThatCannotRun:
    """A rule that cannot run must say so. Silent no-ops are how a checker looks clean while
    checking nothing."""

    def test_tax_wording_is_skipped_when_the_contractor_type_is_unknown(self) -> None:
        report = check_aoa(aoa_text=ACT, contract_text=CONTRACT)
        assert any(s.rule_id == "AOA.TAX_WORDING" for s in report.skipped)
        assert not any(f.rule_id == "AOA.TAX_WORDING" for f in report.findings)

    def test_registry_rules_are_skipped_without_a_snapshot(self) -> None:
        report = check_aoa(aoa_text=ACT, contract_text=CONTRACT)
        assert any(s.rule_id.startswith("AOA.REGISTRY") for s in report.skipped)

    def test_without_a_contract_the_registry_note_claims_no_contract_comparison(self) -> None:
        [note] = [s for s in check_aoa(aoa_text=ACT).skipped if s.rule_id == "AOA.REGISTRY"]
        assert "contract only" not in note.reason_en
        assert "not verified" in note.reason_en
        assert "მხოლოდ ხელშეკრულებას" not in note.reason_ka

    def test_contract_rules_are_skipped_when_no_contract_is_supplied(self) -> None:
        report = check_aoa(aoa_text=ACT)
        assert any(s.rule_id.startswith("AOA.CONTRACT") for s in report.skipped)
        # intra-document rules still run
        assert report.summary["act_date"] == "2026-04-30"


_FEE_LINE = "საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით."
_PRICE_1000 = CONTRACT.replace("100 (ასი) ლარს", "1 000 (ათასი) ლარს")
_PRICE_11000_USD = CONTRACT.replace("100 (ასი) ლარს", "11 000 (თერთმეტი ათასი) აშშ დოლარს")


def _with_contract_clause(clause: str) -> str:
    """The contract with one more clause after its price."""
    return CONTRACT.replace("4.3.", f"{clause}\n4.3.")


def _fees(act: str, contract: str = CONTRACT) -> list[tuple[str, str | None, str | None, str]]:
    report = check_aoa(aoa_text=act, contract_text=contract)
    return [
        (f.message_en, f.expected, f.actual, f.quote or "")
        for f in report.findings
        if f.rule_id == "AOA.FEE"
    ]


class TestFee:
    """Client rule 10.6, the amount: the act's fee must be the contract's price - not any
    amount the contract happens to state, and in the same currency."""

    @pytest.mark.parametrize(
        ("clause", "role"),
        [
            ("4.2. ავანსი შეადგენს 700 ლარს.", "the advance"),
            ("4.2. პირგასამტეხლო შეადგენს 700 ლარს ყოველ ვადაგადაცილებულ დღეზე.", "the penalty"),
        ],
    )
    def test_a_fee_equal_to_another_contract_amount_is_caught_and_named(
        self, clause: str, role: str
    ) -> None:
        """The fee used to pass when it equalled any amount in the contract - the advance, a
        penalty - and here 700 is neither the price nor the fee."""
        act = ACT.replace("100 (ასი) ლარს", "700 (შვიდასი) ლარს")
        report = check_aoa(aoa_text=act, contract_text=_with_contract_clause(clause))
        [finding] = report.findings
        assert (finding.rule_id, finding.severity) == ("AOA.FEE", "error")
        assert f"it equals {role} in the contract" in finding.message_en
        # the correction is written as the act writes its fee, ready to type over the mark
        assert (finding.expected, finding.actual) == ("100 (ასი) ლარს", "700.00 GEL")
        assert finding.quote == "700 (შვიდასი) ლარს"
        assert "ემთხვევა ხელშეკრულებით განსაზღვრულ" in finding.message_ka

    def test_a_price_sentence_mentioning_vat_stays_the_price(self) -> None:
        contract = CONTRACT.replace(
            "100 (ასი) ლარს, რომელიც მოიცავს გადასახადებს მათ შორის დღგ-ს",
            "100 (ასი) ლარს დღგ-ს ჩათვლით",
        )
        assert _fees(ACT, contract) == []
        # the price was read, not skipped: another fee is still caught against it
        assert _fees(ACT.replace("100 (ასი) ლარს", "700 (შვიდასი) ლარს"), contract)

    def test_the_same_figure_in_another_currency_is_an_error(self) -> None:
        act = ACT.replace("100 (ასი) ლარს", "100 (ასი) აშშ დოლარს")
        [(message, expected, actual, quote)] = _fees(act)
        assert "currency differs" in message and "100.00 GEL" in message
        # the correction in the act's wording, with the contract's currency (red team R4,
        # AOA-R4-11); the contract's price in its machine form stays in the message
        assert (expected, actual, quote) == ("100 (ასი) ლარს", "100.00 USD", "100 (ასი) აშშ დოლარს")

    @pytest.mark.parametrize(
        ("fee_line", "contract"),
        [
            (
                "საფასური შეადგენს 1 000 (ათასი) ლარს გადასახადების ჩათვლით. ავანსი 300 ლარი "
                "გადახდილია. დარჩენილი თანხა 700 ლარი გადაიხდება აქტის გაფორმებიდან 5 დღეში.",
                _PRICE_1000,
            ),
            (
                "საფასური შეადგენს 11 000 (თერთმეტი ათასი) აშშ დოლარს. ავანსი 1 000 აშშ დოლარი "
                "გადახდილია. დარჩენილი თანხა 10 000 აშშ დოლარი.",
                _PRICE_11000_USD,
            ),
            # no remainder line: the rest of the fee goes without saying
            (
                "საფასური შეადგენს 1 000 (ათასი) ლარს გადასახადების ჩათვლით. ავანსი 300 ლარი "
                "გადახდილია.",
                _PRICE_1000,
            ),
            (
                "საფასური შეადგენს 11 000 (თერთმეტი ათასი) აშშ დოლარს. ავანსი 1 000 აშშ დოლარი "
                "გადახდილია 2026 წლის 10 აპრილს.",
                _PRICE_11000_USD,
            ),
            # the fee stated only in parts, which add up to the price
            ("ავანსი 300 ლარი გადახდილია. დარჩენილი თანხა 700 ლარი.", _PRICE_1000),
        ],
    )
    def test_an_advance_line_does_not_trip_the_fee(self, fee_line: str, contract: str) -> None:
        """The advance and the remainder are the fee paid in parts; they used to be reported as
        amounts the contract does not state. An act in dollars also gets its invoice note (rule
        9), which is information, not a finding against the fee."""
        report = check_aoa(aoa_text=ACT.replace(_FEE_LINE, fee_line), contract_text=contract)
        assert [f for f in report.findings if f.rule_id != "AOA.FX_INVOICE"] == []
        assert all(f.severity == "info" for f in report.findings)

    def test_an_advance_and_remainder_that_do_not_add_up_trip_the_fee(self) -> None:
        act = ACT.replace(
            _FEE_LINE,
            "საფასური შეადგენს 1 000 (ათასი) ლარს გადასახადების ჩათვლით. ავანსი 300 ლარი "
            "გადახდილია. დარჩენილი თანხა 800 ლარი.",
        )
        assert [actual for _, _, actual, _ in _fees(act, _PRICE_1000)] == [
            "300.00 GEL",
            "800.00 GEL",
        ]
        # an advance the contract itself states is not the line that is wrong
        contract = _PRICE_1000.replace("4.3.", "4.2. ავანსი შეადგენს 300 ლარს.\n4.3.")
        [(message, _, actual, quote)] = _fees(act, contract)
        assert (actual, quote) == ("800.00 GEL", "800 ლარი")
        assert message.startswith("remainder 800.00 GEL matches no price")

    def test_an_unrelated_extra_amount_still_trips_the_fee(self) -> None:
        [(message, expected, actual, quote)] = _fees(ACT + "დამატებით: 50 ლარი.\n")
        assert message == "fee 50.00 GEL matches no price in the contract (100.00 GEL)"
        # the correction is written as the act writes the marked amount
        assert (expected, actual, quote) == ("100 ლარი", "50.00 GEL", "50 ლარი")

    @pytest.mark.parametrize(
        "breakdown",
        [
            "მათ შორის დღგ 15.25 ლარი",
            "მათ შორის 15.25 ლარი დღგ",
            "საიდანაც დღგ შეადგენს 15.25 ლარს",
            "მათ შორის დღგ-ს თანხა შეადგენს 15.25 ლარს",
        ],
    )
    def test_a_vat_breakdown_is_not_a_fee(self, breakdown: str) -> None:
        """The tax named before its figure or after it: either way the figure is the VAT."""
        act = ACT.replace("გადასახადების ჩათვლით.", f"გადასახადების ჩათვლით, {breakdown}.")
        assert check_aoa(aoa_text=act, contract_text=CONTRACT).findings == []

    @pytest.mark.parametrize(
        "fee_line",
        [
            "დამკვეთი უზრუნველყოფს 100 (ასი) ლარის ანაზღაურებას გადასახადების ჩათვლით.",
            "საფასური შეადგენს 100 (ასი) ლარს დღგ-ს დარიცხვით.",
            "Total with VAT: 100 GEL.",
            "Price including 18% VAT: 100 GEL.",
            "Price before VAT: 100 GEL.",
            "The fee plus VAT is 100 GEL.",
            "საფასური, რომელსაც ემატება დღგ, შეადგენს 100 (ასი) ლარს.",
            "საფასური, რომელსაც ემატება დღგ შეადგენს 100 (ასი) ლარს.",
        ],
    )
    def test_a_fee_worded_another_way_is_still_the_fee(self, fee_line: str) -> None:
        """Each of these fees was read as a guarantee or as the tax, which left the act with no
        fee at all and a false "no fee found in the act"."""
        assert _fees(ACT.replace(_FEE_LINE, fee_line)) == []

    def test_a_contract_without_a_price_is_a_skip_not_an_error(self) -> None:
        price = "შეადგენს 100 (ასი) ლარს, რომელიც მოიცავს გადასახადებს მათ შორის დღგ-ს."
        contract = CONTRACT.replace(price, "განისაზღვრება დანართით.")
        report = check_aoa(aoa_text=ACT, contract_text=contract)
        assert report.findings == []
        assert "AOA.FEE" in [s.rule_id for s in report.skipped]


def _tax(act: str, contractor_type: str) -> list[tuple[str | None, str | None]]:
    """(quote, expected) of each tax wording finding; every one must be an error."""
    report = check_aoa(aoa_text=act, contract_text=CONTRACT, contractor_type=contractor_type)  # type: ignore[arg-type]
    found = [f for f in report.findings if f.rule_id == "AOA.TAX_WORDING"]
    assert all(f.severity == "error" and f.message_ka and f.message_en for f in found)
    return [(f.quote, f.expected) for f in found]


def _worded(wording: str) -> str:
    """The act with its fee's tax wording replaced."""
    return ACT.replace("გადასახადების ჩათვლით", wording)


def _english_fee(sentence: str) -> str:
    """The act with its fee written in English."""
    return ACT.replace(_FEE_LINE, sentence)


def _with_line(act: str, line: str) -> str:
    """The act with one more line before its date section, well away from the parties."""
    return act.replace("4. გაფორმების თარიღი", f"{line}\n4. გაფორმების თარიღი")


#: the act with no tax wording at all
_UNWORDED = ACT.replace(" გადასახადების ჩათვლით", "")


_NP_FIX = "NET (საშემოსავლო და საპენსიო გადასახადების გარეშე)"


class TestTaxWording:
    """Client rules 5-8. A wrong phrase beside the fee is reported on the phrase, with the
    wording to write instead; missing wording is reported on the fee, with the fee rewritten."""

    def test_natural_person_requires_net(self) -> None:
        assert "AOA.TAX_WORDING" in errors(contractor_type="natural_person")

    def test_natural_person_with_net_wording_passes(self) -> None:
        act = ACT.replace("გადასახადების ჩათვლით", "NET ღირებულებით")
        assert "AOA.TAX_WORDING" not in errors(act, contractor_type="natural_person")

    def test_foreign_organisation_must_not_mention_taxes(self) -> None:
        assert "AOA.TAX_WORDING" in errors(contractor_type="foreign")

    def test_small_business_must_mention_taxes(self) -> None:
        assert "AOA.TAX_WORDING" not in errors(contractor_type="small_business")

    # --- wording that contradicts the rule, beside the fee ---------------------------------

    @pytest.mark.parametrize(
        ("act", "contractor_type", "quote", "expected"),
        [
            # gross wording for a natural person, however the tax is named
            (ACT, "natural_person", "გადასახადების ჩათვლით", _NP_FIX),
            (
                _worded("საშემოსავლო გადასახადის ჩათვლით"),
                "natural_person",
                "საშემოსავლო გადასახადის ჩათვლით",
                _NP_FIX,
            ),
            # NET beside "including taxes" contradicts itself
            (
                _worded("NET, გადასახადების ჩათვლით"),
                "natural_person",
                "გადასახადების ჩათვლით",
                _NP_FIX,
            ),
            # "taking the taxes into account" includes them
            (
                _worded("გადასახადების გათვალისწინებით"),
                "natural_person",
                "გადასახადების გათვალისწინებით",
                _NP_FIX,
            ),
            # a VAT payer's fee without VAT
            (
                _worded("დღგ-ს გარეშე"),
                "individual_entrepreneur_vat",
                "დღგ-ს გარეშე",
                "დღგ-ს ჩათვლით",
            ),
            (
                _english_fee("The fee is 100 GEL without VAT."),
                "individual_entrepreneur_vat",
                "without VAT",
                "including VAT",
            ),
            # a small business's settlement without taxes
            (
                _worded("გადასახადების გარეშე"),
                "small_business",
                "გადასახადების გარეშე",
                "გადასახადების ჩათვლით",
            ),
            # a foreign organisation's fee with taxes in it: NET goes there
            (ACT, "foreign", "გადასახადების ჩათვლით", "NET"),
            (_english_fee("The fee is 100 GEL, VAT included."), "foreign", "VAT included", "NET"),
        ],
    )
    def test_wording_that_contradicts_the_rule_is_reported_on_itself(
        self, act: str, contractor_type: str, quote: str, expected: str
    ) -> None:
        """One finding: the contradiction already names the wording the fee needs, so the
        "missing" finding it would otherwise raise is not added on top."""
        assert _tax(act, contractor_type) == [(quote, expected)]

    # --- wording the rule asks for that the act never states -------------------------------

    @pytest.mark.parametrize(
        ("act", "contractor_type", "quote", "expected"),
        [
            # an exclusion naming only the income tax leaves out the pension contribution
            (
                _worded("საშემოსავლო გადასახადის გარეშე"),
                "natural_person",
                "100 (ასი) ლარს",
                f"100 (ასი) ლარს {_NP_FIX}",
            ),
            # "without taxes" alone does not say NET
            (
                _worded("გადასახადების გარეშე"),
                "natural_person",
                "100 (ასი) ლარს",
                f"100 (ასი) ლარს {_NP_FIX}",
            ),
            # "including taxes" does not say VAT
            (
                ACT,
                "individual_entrepreneur_vat",
                "100 (ასი) ლარს",
                "100 (ასი) ლარს დღგ-ს ჩათვლით",
            ),
            (_UNWORDED, "small_business", "100 (ასი) ლარს", "100 (ასი) ლარს გადასახადების ჩათვლით"),
            (_UNWORDED, "foreign", "100 (ასი) ლარს", "100 (ასი) ლარს NET"),
            (_english_fee("The fee is 100 GEL."), "foreign", "100 GEL", "100 GEL NET"),
        ],
    )
    def test_missing_wording_is_reported_on_the_fee_rewritten(
        self, act: str, contractor_type: str, quote: str, expected: str
    ) -> None:
        """Anchored on the fee, and the fix is the fee with the wording added, in the script
        the sentence is written in."""
        assert _tax(act, contractor_type) == [(quote, expected)]

    # --- correct acts, one per contractor type ---------------------------------------------

    @pytest.mark.parametrize(
        ("act", "contractor_type"),
        [
            (_worded(_NP_FIX), "natural_person"),
            (_worded("საშემოსავლო და საპენსიო გადასახადების გარეშე"), "natural_person"),
            (_worded("საშემოსავლო გადასახადისა და საპენსიო შენატანის გარეშე"), "natural_person"),
            (_english_fee("The fee is 100 GEL net of taxes."), "natural_person"),
            (_worded("დღგ-ს ჩათვლით"), "individual_entrepreneur_vat"),
            (_worded("დღგ-ს გათვალისწინებით"), "individual_entrepreneur_vat"),
            (
                _worded("რომელიც მოიცავს გადასახადებს მათ შორის დღგ-ს"),
                "individual_entrepreneur_vat",
            ),
            (_english_fee("The fee is 100 GEL, VAT included."), "individual_entrepreneur_vat"),
            (ACT, "small_business"),
            (_worded("გადასახადების გათვალისწინებით"), "small_business"),
            (
                _worded(
                    "რომელიც მოიცავს საქართველოს კანონმდებლობით გათვალისწინებულ ყველა გადასახადს"
                ),
                "small_business",
            ),
            (_english_fee("The fee is 100 GEL including all taxes."), "small_business"),
            (_worded("NET"), "foreign"),
            (_english_fee("The fee is 100 GEL NET."), "foreign"),
        ],
    )
    def test_a_correctly_worded_act_has_no_findings_at_all(
        self, act: str, contractor_type: str
    ) -> None:
        report = check_aoa(aoa_text=act, contract_text=CONTRACT, contractor_type=contractor_type)  # type: ignore[arg-type]
        assert report.findings == []

    def test_an_act_that_restates_its_total_without_net_stays_clean(self) -> None:
        """NET beside the fee, and a closing total without it: the act has said NET."""
        act = _with_line(_worded(_NP_FIX), "სულ ჯამი: 100 ლარი.")
        report = check_aoa(aoa_text=act, contract_text=CONTRACT, contractor_type="natural_person")
        assert report.findings == []

    @pytest.mark.parametrize("label", ["VAT No.: GB123456789", "VAT-ID: DE123456789"])
    def test_a_foreign_act_with_a_vat_number_label_stays_clean(self, label: str) -> None:
        act = _with_line(_worded("NET"), f"შემსრულებლის {label}.")
        report = check_aoa(aoa_text=act, contract_text=CONTRACT, contractor_type="foreign")
        assert report.findings == []

    def test_wording_in_the_table_cell_after_the_fee_is_the_fees(self) -> None:
        """A table puts the tax wording in the cell next to the figure; flattened, that cell is
        the next sentence."""
        fee_cell = "საფასური შეადგენს 100 (ასი) ლარს.\n"
        clean = ACT.replace(_FEE_LINE, f"{fee_cell}{_NP_FIX}\n")
        report = check_aoa(aoa_text=clean, contract_text=CONTRACT, contractor_type="natural_person")
        assert report.findings == []
        wrong = ACT.replace(_FEE_LINE, f"{fee_cell}გადასახადების ჩათვლით\n")
        assert _tax(wrong, "natural_person") == [("გადასახადების ჩათვლით", _NP_FIX)]

    def test_net_stated_away_from_the_fee_still_counts(self) -> None:
        """Missing wording is judged against the whole act, because an act restates its total
        and not every restatement repeats the wording: NET written anywhere satisfies the rule."""
        act = _with_line(_UNWORDED, "ანაზღაურება გაიცემა NET ღირებულებით.")
        assert _tax(act, "natural_person") == []

    def test_net_in_a_name_or_an_address_does_not_say_the_fee_is_net(self) -> None:
        """ ".NET" (a software platform) and "net.ge" (a domain) used to satisfy the rule."""
        act = _with_line(_UNWORDED, "მომსახურება: ASP.NET აპლიკაცია; ელ-ფოსტა: info@net.ge.")
        assert _tax(act, "natural_person") == [("100 (ასი) ლარს", f"100 (ასი) ლარს {_NP_FIX}")]

    def test_a_contradiction_far_from_every_fee_is_not_the_fees_wording(self) -> None:
        act = _with_line(_worded("NET"), "სხვა ხარჯები ანაზღაურდება გადასახადების ჩათვლით.")
        assert _tax(act, "natural_person") == []

    def test_a_foreign_act_reports_every_distinct_tax_mention_once(self) -> None:
        stray = "მხარეები გადასახადებს იხდიან დამოუკიდებლად."
        act = _with_line(_worded("NET"), f"{stray}\n{stray}\nEach party pays its own taxes.")
        # a mention away from the fee is to be removed; nothing is written in its place
        assert _tax(act, "foreign") == [("გადასახადებს", None), ("taxes", None)]

    def test_a_foreign_act_with_a_stray_mention_and_no_net_gets_both_findings(self) -> None:
        act = _with_line(_UNWORDED, "მხარეები გადასახადებს იხდიან დამოუკიდებლად.")
        assert _tax(act, "foreign") == [
            ("გადასახადებს", None),
            ("100 (ასი) ლარს", "100 (ასი) ლარს NET"),
        ]


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2024, 1, 31), 1, date(2024, 2, 29)),
        (date(2026, 8, 1), 6, date(2027, 2, 1)),
        (date(2026, 12, 15), 1, date(2027, 1, 15)),
    ],
)
def test_add_months_survives_month_ends(start: date, months: int, expected: date) -> None:
    """The prototype raised ValueError on 31 January plus one month."""
    assert add_months(start, months) == expected
