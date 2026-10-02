"""Client rule 10, what the act leaves out and how a name is put right.

The act must carry the contract's party data and bank details; a field it leaves out is a finding
that names the contract's value - a party's identification code (a warning), the account (a
warning), the bank code beside an account (a note: the account names the bank). A director's
name is corrected in the act's own grammar: a contract that declines the first name too
("დავითის მელაძის") does not put that case into the act's nominative. Synthetic text only: the
pair is ``test_aoa_check``'s, with every name, code and account invented.
"""

from __future__ import annotations

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.extraction.georgian import in_case_of
from tests.unit.test_aoa_check import ACT, CONTRACT

_CONTRACTOR_CODE = "საიდენტიფიკაციო კოდი: 987654321, "
_ACCOUNT = "GE29NB0000000101904917"
#: both parties' requisites, each in its own block: the contract states the customer's account too
_REQUISITES = """5. მხარეთა რეკვიზიტები
„შემკვეთი“: შპს „ალფა ჯგუფი“, ს/კ: 123456789, ბანკის კოდი: CCCCGE22, ა/ა: GE11CC0000000123456789
„შემსრულებელი“: შპს „ბეტა სერვისი“, ს/კ: 987654321, ბანკის კოდი: AAAAGE22,
ა/ა: GE29NB0000000101904917
"""


def _check(act: str = ACT, contract: str | None = CONTRACT) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract)


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


class TestOmittedPartyCode:
    def test_a_party_named_without_its_code_is_a_warning_with_the_contracts_code(self) -> None:
        act = ACT.replace(_CONTRACTOR_CODE, "")
        [finding] = _of(_check(act), "AOA.ID_CODES")
        assert (finding.severity, finding.expected) == ("warning", "987654321")
        assert "987654321" in finding.message_ka and "987654321" in finding.message_en
        assert finding.span is not None

    def test_the_code_stated_elsewhere_in_the_act_is_not_reported(self) -> None:
        act = ACT.replace(_CONTRACTOR_CODE, "").replace(
            "დირექტორი: დავით მელაძე", "დირექტორი: დავით მელაძე\nსაიდენტიფიკაციო კოდი: 987654321"
        )
        assert [f.severity for f in _of(_check(act), "AOA.ID_CODES")] == []

    def test_an_act_without_any_code_stays_one_error(self) -> None:
        act = ACT.replace("საიდენტიფიკაციო კოდი: 123456789; ", "").replace(_CONTRACTOR_CODE, "")
        assert [f.severity for f in _of(_check(act), "AOA.ID_CODES")] == ["error"]

    def test_a_party_the_contract_gives_no_code_is_not_reported(self) -> None:
        contract = CONTRACT.replace(_CONTRACTOR_CODE, "")
        act = ACT.replace(_CONTRACTOR_CODE, "")
        assert [f.severity for f in _of(_check(act, contract), "AOA.ID_CODES")] == []


class TestMissingBankDetails:
    def test_an_act_without_the_account_names_the_contractors(self) -> None:
        act = ACT.replace(f"; ა/ა: {_ACCOUNT}", "")
        [finding] = _of(_check(act), "AOA.BANK")
        assert (finding.severity, finding.expected) == ("warning", _ACCOUNT)
        assert _ACCOUNT in finding.message_ka and _ACCOUNT in finding.message_en

    def test_the_customers_account_is_never_the_correction(self) -> None:
        """The contract states both sides' accounts: the act's missing one is the contractor's,
        the side that is paid."""
        act = ACT.replace(f"; ა/ა: {_ACCOUNT}", "")
        [finding] = _of(_check(act, CONTRACT + _REQUISITES), "AOA.BANK")
        assert (finding.severity, finding.expected) == ("warning", _ACCOUNT)

    def test_accounts_that_cannot_be_told_apart_give_no_correction(self) -> None:
        """Near miss: the customer's account in a clause the contractor's block runs over - two
        accounts, no owner to tell them apart, so no account is offered at all."""
        contract = CONTRACT.replace(
            "4.3. ანგარიშსწორება:",
            "4.2. „შემკვეთის“ რეკვიზიტები: სს „ნიმუში ბანკი“, ბანკის კოდი: CCCCGE22,\n"
            "ანგარიშის ნომერი: GE95TB0000000123456789.\n4.3. ანგარიშსწორება:",
        )
        act = ACT.replace(f"; ა/ა: {_ACCOUNT}", "")
        [finding] = _of(_check(act, contract), "AOA.BANK")
        assert (finding.severity, finding.expected) == ("warning", None)

    def test_an_account_without_its_bank_code_is_a_note_naming_the_code(self) -> None:
        act = ACT.replace("ბანკის კოდი: AAAAGE22; ", "")
        [finding] = _of(_check(act), "AOA.BANK")
        assert (finding.severity, finding.expected) == ("info", "AAAAGE22")

    def test_an_act_without_any_bank_details_warns_once(self) -> None:
        act = ACT.replace(f"ბანკის კოდი: AAAAGE22; ა/ა: {_ACCOUNT}", "")
        assert [f.severity for f in _of(_check(act), "AOA.BANK")] == ["warning"]

    def test_the_complete_act_has_no_bank_finding(self) -> None:
        assert _of(_check(), "AOA.BANK") == []


class TestDirectorInTheActsGrammar:
    #: the contract declines the contractor's director's first name as well as the surname
    DECLINED = CONTRACT.replace(
        "დირექტორის დავით მელაძის სახით", "დირექტორის დავითის მელაძის სახით"
    )

    def test_a_surname_slip_is_corrected_in_the_acts_nominative(self) -> None:
        act = ACT.replace("დირექტორი: დავით მელაძე", "დირექტორი: დავით მელიძე")
        found = [(f.actual, f.expected) for f in _of(_check(act, self.DECLINED), "AOA.DIRECTOR")]
        assert found == [("დავით მელიძე", "დავით მელაძე")]

    def test_a_first_name_slip_takes_the_acts_short_form(self) -> None:
        act = ACT.replace("დირექტორი: დავით მელაძე", "დირექტორი: დავიტ მელაძე")
        found = [(f.actual, f.expected) for f in _of(_check(act, self.DECLINED), "AOA.DIRECTOR")]
        assert found == [("დავიტ მელაძე", "დავით მელაძე")]


class TestInCaseOfKeepsTheActsWords:
    def test_a_word_the_act_writes_as_a_form_of_the_right_one_stays(self) -> None:
        assert in_case_of("ნინო კაპანიძე", "ნინოს კაპანაძის") == "ნინო კაპანაძე"
        # a contract that keeps the first name as it is gives it so in every case
        assert in_case_of("ნინოს კაპანიძის", "ნინო კაპანაძე") == "ნინო კაპანაძის"

    def test_a_name_written_right_keeps_its_case_when_the_slip_is_elsewhere(self) -> None:
        assert in_case_of("ნინა კაპანაძე", "ნინოს კაპანაძის") == "ნინო კაპანაძე"

    def test_a_company_names_first_word_is_given_as_the_contract_writes_it(self) -> None:
        """Near miss: a company's name declines in its last word alone, so a slip in its first
        word is corrected to the contract's word as written, never to a case form of it."""
        assert in_case_of("ბეტის სერვისი", "ბეტა სერვისი") == "ბეტა სერვისი"
        assert in_case_of("ალფო ჯგუფის", "ალფა ჯგუფი") == "ალფა ჯგუფის"
        # a fixed genitive before a nominative is the name's own word, not a declined one
        assert in_case_of("საქართველო ბანკი", "საქართველოს ბანკი") == "საქართველოს ბანკი"

    def test_another_name_is_given_as_written(self) -> None:
        assert in_case_of("გელა ხარაბაძე", "დავით მელაძის") == "დავით მელაძე"
