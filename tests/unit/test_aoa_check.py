"""The AOA rule engine, against synthetic Georgian documents.

Each test plants exactly one defect into a known-good pair, mirroring the shape of the defects
the client asks us to find. The first test - that a correct act produces no errors - is the one
that matters most: a checker that fires on a clean document is worse than no checker.
"""

from __future__ import annotations

from datetime import date

import pytest

from procurement_agent.checks.aoa import add_months, check_aoa

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
    assert errors() == []


def test_amount_in_words_must_match_the_digits() -> None:
    assert "AOA.AMOUNT_WORDS" in errors(ACT.replace("100 (ასი)", "100 (ორასი)"))


def test_unreadable_amount_in_words_is_a_warning_not_an_error() -> None:
    report = check_aoa(aoa_text=ACT.replace("100 (ასი)", "100 (ასოი)"), contract_text=CONTRACT)
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


def test_a_misspelled_signatory_is_matched_to_the_contract_name() -> None:
    report = check_aoa(aoa_text=ACT.replace("დავით მელაძე", "დავთ მელაძე"), contract_text=CONTRACT)
    found = [f for f in report.errors if f.rule_id == "AOA.DIRECTOR"]
    assert found and "მელაძ" in (found[0].expected or "")
    assert "misspelling" in found[0].message_en


def test_a_different_company_under_the_right_id_code_is_caught() -> None:
    assert "AOA.PARTY_NAME" in errors(ACT.replace("შპს „ბეტა სერვისი“", "შპს „გამა“", 1))


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


def test_missing_project_and_cadastral_code_are_warnings() -> None:
    report = check_aoa(
        aoa_text=ACT.replace("პროექტ „მზე“ (ს/კ: 01.02.03.456)", "მომსახურების"),
        contract_text=CONTRACT,
    )
    ids = {f.rule_id for f in report.findings}
    assert {"AOA.PROJECT_PRESENT", "AOA.CADASTRAL_PRESENT"} <= ids


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

    def test_contract_rules_are_skipped_when_no_contract_is_supplied(self) -> None:
        report = check_aoa(aoa_text=ACT)
        assert any(s.rule_id.startswith("AOA.CONTRACT") for s in report.skipped)
        # intra-document rules still run
        assert report.summary["act_date"] == "2026-04-30"


class TestTaxWording:
    def test_natural_person_requires_net(self) -> None:
        assert "AOA.TAX_WORDING" in errors(contractor_type="natural_person")

    def test_natural_person_with_net_wording_passes(self) -> None:
        act = ACT.replace("გადასახადების ჩათვლით", "NET ღირებულებით")
        assert "AOA.TAX_WORDING" not in errors(act, contractor_type="natural_person")

    def test_foreign_organisation_must_not_mention_taxes(self) -> None:
        assert "AOA.TAX_WORDING" in errors(contractor_type="foreign")

    def test_small_business_must_mention_taxes(self) -> None:
        assert "AOA.TAX_WORDING" not in errors(contractor_type="small_business")


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
