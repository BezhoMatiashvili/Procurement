"""Defects the second red-team round found in the act check's fee, tax, party and period rules.

Each test states the defect's id (``samples/redteam/aoa/CONFIRMED_R2.md``) and the behaviour the
spec asks for, and every widened reading has a near miss beside it that must still not match.
Synthetic text only: the pairs are ``test_aoa_check``'s, ``test_aoa_contract_rules``' and
``test_aoa_scope``'s, with every name, code and account invented.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.amounts import digits_and_words
from procurement_agent.checks.aoa import _fee_like, _nearest_price, check_aoa
from procurement_agent.checks.contract_terms import tax_phrases
from procurement_agent.checks.findings import CheckReport, Finding
from tests.unit.test_aoa_check import ACT, CONTRACT

ContractorType = str | None


def _check(act: str = ACT, contract: str | None = CONTRACT, **kw: object) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract, **kw)  # type: ignore[arg-type]


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _substantive(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.severity in ("error", "warning")]


_FEE_TAX = "საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით."


def _fee(sentence: str, act: str = ACT) -> str:
    """The act with its fee sentence rewritten."""
    assert _FEE_TAX in act
    return act.replace(_FEE_TAX, sentence)


# --- AOA-R2-07: the fee before VAT restated beside the fee incl. VAT -----------------------------

#: The pair with a price of 590 incl. VAT: 500 before VAT at 18%.
_VAT_CONTRACT = CONTRACT.replace("100 (ასი) ლარს", "590 (ხუთას ოთხმოცდაათი) ლარს")
_GROSS = "საფასური შეადგენს 590 (ხუთას ოთხმოცდაათი) ლარს დღგ-ს ჩათვლით"


class TestNetRestatedAtTheVatRate:
    @pytest.mark.parametrize("contractor", [None, "company", "individual_entrepreneur_vat"])
    @pytest.mark.parametrize(
        "tail",
        [
            " (დღგ-ს გარეშე 500.00 ლარი).",
            ", რაც დღგ-ს გარეშე შეადგენს 500 ლარს.",
            " (500.00 ლარი დღგ-ს გარეშე).",
        ],
    )
    def test_the_net_figure_is_no_second_fee(self, contractor: ContractorType, tail: str) -> None:
        report = _check(_fee(_GROSS + tail), _VAT_CONTRACT, contractor_type=contractor)
        assert _of(report, "AOA.FEE") == []
        assert _of(report, "AOA.TAX_WORDING") == []

    def test_a_vat_rate_the_contract_states_is_the_rate(self) -> None:
        contract = CONTRACT.replace(
            "100 (ასი) ლარს, რომელიც მოიცავს გადასახადებს მათ შორის დღგ-ს",
            "600 (ექვსასი) ლარს, რომელიც მოიცავს დღგ-ს (20%)",
        )
        act = _fee("საფასური შეადგენს 600 (ექვსასი) ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 500 ლარი).")
        assert _of(_check(act, contract), "AOA.FEE") == []

    def test_another_rate_nobody_states_is_not_the_rate(self) -> None:
        """600 is 500 with 20% added, but VAT here is 18% and no document says otherwise."""
        contract = _VAT_CONTRACT.replace("590 (ხუთას ოთხმოცდაათი)", "600 (ექვსასი)")
        act = _fee("საფასური შეადგენს 600 (ექვსასი) ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 500 ლარი).")
        [finding] = _of(_check(act, contract), "AOA.FEE")
        assert finding.actual == "500.00 GEL"

    def test_a_net_that_is_not_the_fee_before_vat_is_still_compared(self) -> None:
        report = _check(_fee(_GROSS + " (დღგ-ს გარეშე 490.00 ლარი)."), _VAT_CONTRACT)
        [finding] = _of(report, "AOA.FEE")
        assert finding.actual == "490.00 GEL"

    def test_a_net_the_act_states_alone_is_still_compared(self) -> None:
        """No fee incl. VAT beside it: the act states its fee before VAT, which is not the price."""
        report = _check(_fee("საფასური დღგ-ს გარეშე შეადგენს 500 ლარს."), _VAT_CONTRACT)
        [finding] = _of(report, "AOA.FEE")
        assert finding.actual == "500.00 GEL"

    def test_a_fee_stated_without_vat_is_still_the_vat_entrepreneurs_error(self) -> None:
        act = _fee("საფასური შეადგენს 590 (ხუთას ოთხმოცდაათი) ლარს დღგ-ს გარეშე.")
        report = _check(act, _VAT_CONTRACT, contractor_type="individual_entrepreneur_vat")
        [finding] = _of(report, "AOA.TAX_WORDING")
        assert finding.severity == "error"

    def test_a_stated_vat_amount_decides_the_breakdown(self) -> None:
        """With the VAT amount written out, net + VAT must make the fee (round 1, AOA-17); the
        rate alone does not excuse a VAT amount that is off."""
        act = _fee(
            "საფასური დღგ-ს გარეშე შეადგენს 500 ლარს, დღგ - 80 ლარს, სულ დღგ-ს ჩათვლით 590 "
            "(ხუთას ოთხმოცდაათი) ლარს."
        )
        [finding] = _of(_check(act, _VAT_CONTRACT), "AOA.FEE")
        assert finding.actual == "500.00 GEL"


# --- AOA-R2-08: the fee's correction is the one price it is a slip of ----------------------------

_TOTAL = "4.2. ხელშეკრულების საერთო ღირებულება შეადგენს 1 200 (ათას ორასი) ლარს.\n"
_MONTHLY_AND_TOTAL = CONTRACT.replace("4.3.", f"{_TOTAL}4.3.")


class TestNearestPriceCorrection:
    def test_a_slip_of_the_monthly_price_is_corrected_to_it(self) -> None:
        act = _fee("საფასური შეადგენს 101 (ას ერთი) ლარს გადასახადების ჩათვლით.")
        [finding] = _of(_check(act, _MONTHLY_AND_TOTAL), "AOA.FEE")
        assert finding.expected == "100 (ასი) ლარს"
        assert "1,200.00 GEL" in finding.message_en

    def test_a_slip_of_the_total_is_corrected_to_it(self) -> None:
        act = _fee("საფასური შეადგენს 1 020 (ათას ოცი) ლარს გადასახადების ჩათვლით.")
        [finding] = _of(_check(act, _MONTHLY_AND_TOTAL), "AOA.FEE")
        assert finding.expected == "1 200 (ათას ორასი) ლარს"

    def test_a_fee_near_no_price_has_no_correction(self) -> None:
        """Not a slip of either: the message lists both prices, the correction is left blank."""
        act = _fee("საფასური შეადგენს 5 555 (ხუთი ათას ხუთას ორმოცდათხუთმეტი) ლარს.")
        [finding] = _of(_check(act, _MONTHLY_AND_TOTAL), "AOA.FEE")
        assert finding.expected is None
        assert "100.00 GEL" in finding.message_en and "1,200.00 GEL" in finding.message_en


    def test_two_prices_equally_near_give_no_correction(self) -> None:
        """120 is one digit and a fifth from both 100 and 150: neither is the one meant."""
        assert _nearest_price(120, [100, 150]) is None
        assert _nearest_price(101, [100, 150]) == 100


# --- AOA-R2-09: the words in brackets after the currency are part of the fee ---------------------


class TestFeeWordsAfterTheCurrency:
    @pytest.mark.parametrize(
        ("written", "fixed"),
        [
            ("700.00 ლარს (შვიდასი)", "100.00 ლარს (ასი)"),
            ("700 ლარი (შვიდასი ლარი)", "100 ლარი (ასი ლარი)"),
            ("GEL 700.00 (შვიდასი)", "GEL 100.00 (ასი)"),
            ("₾700 (შვიდასი)", "₾100 (ასი)"),
            ("700 ₾ (შვიდასი)", "100 ₾ (ასი)"),
        ],
    )
    def test_the_quote_and_the_correction_carry_the_words(self, written: str, fixed: str) -> None:
        act = _fee(f"საფასური შეადგენს {written} გადასახადების ჩათვლით.")
        [finding] = _of(_check(act), "AOA.FEE")
        assert (finding.quote, finding.expected) == (written, fixed)

    def test_words_after_the_currency_that_state_another_fee_are_caught(self) -> None:
        act = _fee("საფასური შეადგენს 100.00 ლარს (შვიდასი) გადასახადების ჩათვლით.")
        report = _check(act)
        assert [(f.actual, f.expected) for f in _of(report, "AOA.FEE")] == [("შვიდასი", "ასი")]
        assert [(f.actual, f.expected) for f in _of(report, "AOA.AMOUNT_WORDS")] == [
            ("შვიდასი", "ასი")
        ]

    def test_a_correct_fee_with_its_words_after_the_currency_is_clean(self) -> None:
        act = _fee("საფასური შეადგენს 100.00 ლარს (ასი) გადასახადების ჩათვლით.")
        assert _substantive(_check(act)) == []

    @pytest.mark.parametrize(
        "text",
        [
            "100 ლარი (საფასური)",  # no numeral in the brackets
            "700 დღე (შვიდასი)",  # a count of days is no currency
            "100 ლარი, (ასი)",  # a comma ends the amount
        ],
    )
    def test_no_pair_is_read_across_anything_but_a_currency(self, text: str) -> None:
        assert [p for p in digits_and_words(text) if p.numeral] == []


# --- AOA-R2-11: VAT-included wordings with the tax's other spellings ------------------------------


class TestVatTokenSpellings:
    @pytest.mark.parametrize("token", ["დღგ-ს", "დ.ღ.გ.-ს", "დ.ღ.გ-ს", "დღგ-ისა", "დ.ღ.გ."])
    @pytest.mark.parametrize("reversed_order", [False, True])
    def test_every_spelling_in_either_order_says_vat_is_included(
        self, token: str, *, reversed_order: bool
    ) -> None:
        wording = f"ჩათვლით {token}" if reversed_order else f"{token} ჩათვლით"
        act = _fee(f"საფასური შეადგენს 100 (ასი) ლარს {wording}.")
        report = _check(act, contractor_type="individual_entrepreneur_vat")
        assert _of(report, "AOA.TAX_WORDING") == [], wording
        [phrase] = [p for p in tax_phrases(act) if p.kind == "inclusion"]
        assert "vat" in phrase.names

    @pytest.mark.parametrize("token", ["დ.ღ.გ.-ს", "დ.ღ.გ-ს"])
    def test_the_dotted_spelling_excluded_is_the_vat_entrepreneurs_error(self, token: str) -> None:
        act = _fee(f"საფასური შეადგენს 100 (ასი) ლარს {token} გარეშე.")
        [finding] = _of(
            _check(act, contractor_type="individual_entrepreneur_vat"), "AOA.TAX_WORDING"
        )
        assert finding.severity == "error"

    def test_including_after_a_date_still_closes_the_date(self) -> None:
        text = "მომსახურება გაწეულია 2026 წლის 30 აპრილის ჩათვლით დ.ღ.გ-ს გადამხდელის მიერ"
        assert [p for p in tax_phrases(text) if p.kind == "inclusion"] == []

    def test_a_dotted_abbreviation_that_is_not_vat_is_not_vat(self) -> None:
        assert [p for p in tax_phrases("100 ლარს დ.ღ ჩათვლით") if "vat" in p.names] == []

    @pytest.mark.parametrize("text", ["100 ლარს ადღგ-ს ჩათვლით", "100 ლარს ჩათვლით ადღგ-ს"])
    def test_the_token_inside_another_word_is_not_vat(self, text: str) -> None:
        assert [p for p in tax_phrases(text) if "vat" in p.names] == []

    def test_a_dotted_vat_number_is_a_label(self) -> None:
        assert tax_phrases("დ.ღ.გ-ს გადამხდელის ნომერი: 123456789") == []


# --- AOA-R2-12: a company name with its final vowel dropped --------------------------------------


class TestCompanyNameFinalVowel:
    def test_the_name_without_its_final_vowel_is_an_error(self) -> None:
        act = ACT.replace("შპს „ბეტა სერვისი“", "შპს „ბეტა სერვის“")
        [finding] = _of(_check(act), "AOA.PARTY_NAME")
        assert (finding.severity, finding.actual, finding.expected) == (
            "error",
            "შპს ბეტა სერვის",
            "შპს ბეტა სერვისი",
        )

    def test_the_stem_before_an_attached_ending_is_the_name(self) -> None:
        """ "„ბეტა სერვის“-ის" is the genitive written after the quote: the stem is right there."""
        act = ACT.replace(
            "შპს „ბეტა სერვისი“, საიდენტიფიკაციო",
            "შპს „ბეტა სერვის“-ის წარმომადგენელი, საიდენტიფიკაციო",
        )
        assert _substantive(_check(act)) == []

    def test_the_stem_before_a_hyphenated_ending_without_quotes_is_the_name(self) -> None:
        act = ACT.replace(
            "„შემსრულებელი“\nშპს „ბეტა სერვისი“",
            "„შემსრულებელი“ (ბეტა სერვის-ის სახელით)\nშპს „ბეტა სერვისი“",
        )
        assert act != ACT
        assert _of(_check(act), "AOA.PARTY_NAME") == []

    def test_the_name_in_a_case_form_inside_the_quotes_is_the_name(self) -> None:
        act = ACT.replace(
            "შპს „ბეტა სერვისი“, საიდენტიფიკაციო", "შპს „ბეტა სერვისის“ საიდენტიფიკაციო"
        )
        assert _of(_check(act), "AOA.PARTY_NAME") == []


# --- AOA-R2-13: a code printed in groups is quoted as printed -------------------------------------


class TestGroupedIdCodeQuote:
    def test_the_finding_quotes_the_grouped_code(self) -> None:
        act = ACT.replace("კოდი: 987654321", "კოდი: 987 654 312")
        [finding] = _of(_check(act), "AOA.ID_CODES")
        assert (finding.actual, finding.expected, finding.quote) == (
            "987654312",
            "987654321",
            "987 654 312",
        )

    def test_the_digits_inside_a_longer_number_are_not_the_code(self) -> None:
        """The slip is written grouped once; the same nine digits inside an account are not it."""
        act = ACT.replace("კოდი: 987654321", "კოდი: 987 654 312").replace(
            "ა/ა: GE29NB0000000101904917", "ა/ა: GE29NB0000000101904917 (987654312001)"
        )
        [finding] = _of(_check(act), "AOA.ID_CODES")
        assert finding.quote == "987 654 312"


# --- AOA-R2-14: a year slip in one end of a period that runs forward -----------------------------


class TestPeriodEndpointYear:
    from tests.unit.test_aoa_contract_rules import ACT as FULL_ACT
    from tests.unit.test_aoa_contract_rules import CONTRACT as FULL_CONTRACT

    _PERIOD = "2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით"

    def _report(self, period: str) -> CheckReport:
        assert self._PERIOD in self.FULL_ACT
        return _check(self.FULL_ACT.replace(self._PERIOD, period), self.FULL_CONTRACT)

    def test_a_start_year_slip_is_the_period_with_that_year_put_right(self) -> None:
        report = self._report("2025 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით")
        [finding] = _of(report, "AOA.PERIOD_IN_TERM")
        assert (finding.quote, finding.expected) == (
            "2025 წლის 1 აპრილიდან 2026 წლის 30 აპრილის",
            "2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის",
        )

    def test_an_end_outside_the_term_by_months_keeps_the_term_as_the_reference(self) -> None:
        """No one year puts it right: the term is reported, as before."""
        report = self._report("2026 წლის 1 აპრილიდან 2026 წლის 30 ოქტომბრის ჩათვლით")
        [finding] = _of(report, "AOA.PERIOD_IN_TERM")
        assert finding.expected == "2026-03-01..2026-09-01"


# --- AOA-R2-15: tetri written after the lari ------------------------------------------------------


class TestTetriCorrection:
    def test_tetri_the_price_does_not_have_are_dropped(self) -> None:
        act = _fee("საფასური შეადგენს 100 ლარი და 50 თეთრი გადასახადების ჩათვლით.")
        [finding] = _of(_check(act), "AOA.FEE")
        assert finding.actual == "100.50 GEL"
        assert finding.expected == "100 ლარი"

    def test_the_tetri_are_written_from_the_price(self) -> None:
        assert _fee_like(100.75, "700 ლარი და 50 თეთრი") == "100 ლარი და 75 თეთრი"
        assert _fee_like(100.05, "700 ლარი და 50 თეთრი") == "100 ლარი და 05 თეთრი"
        assert _fee_like(100.0, "700 ლარი და 50 თეთრი") == "100 ლარი"

    def test_no_error_offers_its_own_words_as_the_correction(self) -> None:
        act = _fee("საფასური შეადგენს 100 ლარი და 50 თეთრი გადასახადების ჩათვლით.")
        for f in _check(act).findings:
            assert f.expected is None or f.quote is None or f.expected != f.quote, f


# --- AOA-R2-16: the contract decides which half of a count is wrong ------------------------------


class TestCountDigitsAgainstTheContract:
    from tests.unit.test_aoa_scope import ACT as SCOPE_ACT
    from tests.unit.test_aoa_scope import CONTRACT as SCOPE_CONTRACT

    _ITEM = "- 4 სერვისის გვერდის აღწერა GEO/ENG"

    def _pair(self, act_count: str) -> tuple[str, str]:
        assert self._ITEM in self.SCOPE_ACT and self._ITEM in self.SCOPE_CONTRACT
        contract = self.SCOPE_CONTRACT.replace(
            self._ITEM, "- 4 (ოთხი) სერვისის გვერდის აღწერა GEO/ENG"
        )
        act = self.SCOPE_ACT.replace(self._ITEM, f"- {act_count} სერვისის გვერდის აღწერა GEO/ENG")
        return act, contract

    def test_digits_changed_are_corrected_on_the_digits(self) -> None:
        act, contract = self._pair("6 (ოთხი)")
        report = _check(act, contract)
        [words] = _of(report, "AOA.AMOUNT_WORDS")
        assert (words.actual, words.expected) == ("6", "4")
        [service] = _of(report, "AOA.SERVICES")
        assert service.expected is not None and service.expected.startswith("4 (ოთხი)")

    def test_words_changed_are_corrected_on_the_words(self) -> None:
        act, contract = self._pair("4 (ექვსი)")
        [words] = _of(_check(act, contract), "AOA.AMOUNT_WORDS")
        assert (words.actual, words.expected) == ("ექვსი", "ოთხი")

    def test_a_count_the_contract_item_writes_in_digits_only_decides_too(self) -> None:
        """The contract's list says "4 სერვისის …" with no words: its item still states 4, so
        the act's "6 (ოთხი)" is wrong in its digits, as AOA.SERVICES says."""
        act = self.SCOPE_ACT.replace(self._ITEM, "- 6 (ოთხი) სერვისის გვერდის აღწერა GEO/ENG")
        report = _check(act, self.SCOPE_CONTRACT)
        [words] = _of(report, "AOA.AMOUNT_WORDS")
        assert (words.actual, words.expected) == ("6", "4")
        [service] = _of(report, "AOA.SERVICES")
        assert service.expected == "4 სერვისის გვერდის აღწერა GEO/ENG"

    def test_digits_the_contract_item_states_keep_the_words_wrong(self) -> None:
        act = self.SCOPE_ACT.replace(self._ITEM, "- 4 (ექვსი) სერვისის გვერდის აღწერა GEO/ENG")
        [words] = _of(_check(act, self.SCOPE_CONTRACT), "AOA.AMOUNT_WORDS")
        assert (words.actual, words.expected) == ("ექვსი", "ოთხი")

    def test_a_bare_contract_number_outside_the_item_decides_nothing(self) -> None:
        """Near miss: the pair is in no listed service, so the "4" the contract's list writes in
        digits only is not its counterpart; the figure is taken as written."""
        act = self.SCOPE_ACT + "2. ეგზემპლარების რაოდენობა: 6 (ოთხი).\n"
        [words] = _of(_check(act, self.SCOPE_CONTRACT), "AOA.AMOUNT_WORDS")
        assert (words.actual, words.expected) == ("ოთხი", "ექვსი")

    def test_without_a_contract_the_figure_is_taken_as_written(self) -> None:
        act, _ = self._pair("6 (ოთხი)")
        [words] = _of(_check(act, None), "AOA.AMOUNT_WORDS")
        assert (words.actual, words.expected) == ("ოთხი", "ექვსი")
