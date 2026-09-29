"""Defects the first red-team round found in the act check, one planted mistake per test.

Each test states the defect's id (``samples/redteam/aoa/CONFIRMED_R1.md``) and the behaviour the
spec asks for. Synthetic text only: the pair is ``test_aoa_check``'s, with every name, code and
account invented.
"""

from __future__ import annotations

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from tests.unit.test_aoa_check import ACT, CONTRACT


def _check(act: str = ACT, contract: str | None = CONTRACT, **kw: object) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract, **kw)  # type: ignore[arg-type]


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _substantive(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.severity in ("error", "warning")]


# --- AOA-02: a BIC and its XXX branch form are one bank ------------------------------------------


class TestBicBranch:
    def test_the_act_writing_the_branch_form_is_the_same_bank(self) -> None:
        act = ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAAAGE22XXX;")
        assert _substantive(_check(act)) == []

    def test_the_contract_writing_the_branch_form_is_the_same_bank(self) -> None:
        contract = CONTRACT.replace("ბანკის კოდი: AAAAGE22,", "ბანკის კოდი: AAAAGE22XXX,")
        assert _substantive(_check(contract=contract)) == []

    def test_another_branch_is_still_another_code(self) -> None:
        act = ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAAAGE22B01;")
        [finding] = _of(_check(act), "AOA.BANK")
        assert (finding.actual, finding.expected) == ("AAAAGE22B01", "AAAAGE22")

    def test_the_branch_form_pairs_with_its_account(self) -> None:
        """With two banks in the contract the codes are also checked as pairs with their accounts,
        and the branch form must pair too."""
        both = CONTRACT.replace(
            "ანგარიშის ნომერი: GE29NB0000000101904917.",
            "ანგარიშის ნომერი: GE29NB0000000101904917.\n4.4. „შემკვეთის“ ანგარიში: სს „ნიმუშ "
            "ბანკი“, ბანკის კოდი: CCCCGE22, ანგარიშის ნომერი: GE11CC0000000123456789.",
        )
        act = ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAAAGE22XXX;")
        assert _substantive(_check(act, both)) == []


# --- AOA-08: a code's correction is the one contract value it stands for --------------------------

#: The contract with both parties' requisites, each in its own block.
_REQUISITES = """5. მხარეთა რეკვიზიტები
„შემკვეთი“: შპს „ალფა ჯგუფი“, ს/კ: 123456789, ბანკის კოდი: CCCCGE22, ა/ა: GE11CC0000000123456789
„შემსრულებელი“: შპს „ბეტა სერვისი“, ს/კ: 987654321, ბანკის კოდი: AAAAGE22,
ა/ა: GE29NB0000000101904917
"""
_BOTH_BANKS = CONTRACT + _REQUISITES


class TestNearestCorrection:
    def test_a_correct_act_under_both_requisites_is_clean(self) -> None:
        assert _substantive(_check(contract=_BOTH_BANKS)) == []

    def test_an_account_with_two_digits_swapped_is_corrected_to_its_own_account(self) -> None:
        act = ACT.replace("GE29NB0000000101904917", "GE29NB0000000101904971")
        [finding] = _of(_check(act, _BOTH_BANKS), "AOA.BANK")
        assert (finding.actual, finding.expected) == (
            "GE29NB0000000101904971",
            "GE29NB0000000101904917",
        )

    def test_a_bank_code_slip_is_corrected_to_the_nearest_code(self) -> None:
        act = ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAABGE22;")
        [finding] = _of(_check(act, _BOTH_BANKS), "AOA.BANK")
        assert (finding.actual, finding.expected) == ("AAABGE22", "AAAAGE22")

    def test_a_tie_goes_to_the_contractor_s_own_account(self) -> None:
        """One digit from both accounts: the act's account is the contractor's to be paid on."""
        contract = _BOTH_BANKS.replace("GE11CC0000000123456789", "GE29NB0000000101904918")
        act = ACT.replace("GE29NB0000000101904917", "GE29NB0000000101904919")
        [finding] = _of(_check(act, contract), "AOA.BANK")
        assert finding.expected == "GE29NB0000000101904917"

    def test_an_unrelated_account_is_corrected_to_the_contractor_s(self) -> None:
        act = ACT.replace("GE29NB0000000101904917", "GE77ZZ9999999999999999")
        [finding] = _of(_check(act, _BOTH_BANKS), "AOA.BANK")
        assert finding.expected == "GE29NB0000000101904917"

    def test_a_plot_code_with_digits_swapped_is_corrected_to_the_nearest_plot(self) -> None:
        contract = CONTRACT.replace("(ს/კ: 01.02.03.456)", "(ს/კ: 01.02.03.456 და 01.02.03.457)")
        act = ACT.replace("(ს/კ: 01.02.03.456)", "(ს/კ: 01.02.03.475)")
        [finding] = _of(_check(act, contract), "AOA.CADASTRAL")
        assert (finding.actual, finding.expected) == ("01.02.03.475", "01.02.03.457")

    def test_a_plot_code_as_near_to_two_plots_has_no_correction(self) -> None:
        """No single plot is meant: the message lists them, the correction is left blank."""
        contract = CONTRACT.replace("(ს/კ: 01.02.03.456)", "(ს/კ: 01.02.03.456 და 01.02.03.457)")
        act = ACT.replace("(ს/კ: 01.02.03.456)", "(ს/კ: 01.02.03.458)")
        [finding] = _of(_check(act, contract), "AOA.CADASTRAL")
        assert finding.expected is None
        assert "01.02.03.456" in finding.message_en and "01.02.03.457" in finding.message_en
