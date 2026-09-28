"""The act written in other words or another layout than the client's template: each case once
raised an error on a correct act (a false alarm), or read the wrong date. Synthetic text only."""

from __future__ import annotations

from procurement_agent.checks.aoa import check_aoa
from tests.unit.test_aoa_check import ACT, CONTRACT


def _findings(act: str) -> list[tuple[str, str]]:
    return [
        (f.rule_id, f.severity) for f in check_aoa(aoa_text=act, contract_text=CONTRACT).findings
    ]


def test_the_contracts_signing_named_in_the_act_is_not_the_acts_date() -> None:
    act = ACT.replace(
        "2. შესრულების თარიღი",
        "ხელშეკრულება გაფორმდა 2026 წლის 1 მარტს.\n2. შესრულების თარიღი",
    )
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary["act_date"] == "2026-04-30"
    assert [f for f in report.findings if f.severity == "error"] == []


def test_the_acts_own_date_does_not_stretch_the_period() -> None:
    act = ACT.replace(
        "2026 წლის 30 აპრილის ჩათვლით.",
        "2026 წლის 30 აპრილის ჩათვლით. აქტი გაფორმდა 2026 წლის 30 აპრილს.",
    )
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary["execution_period"] == "2026-04-01..2026-04-30"
    assert [f for f in report.findings if f.severity == "error"] == []


def test_a_contract_named_after_its_date_is_not_part_of_the_period() -> None:
    act = ACT.replace(
        "2. შესრულების თარიღი",
        "ვალდებულებები შესრულდა 2026 წლის 1 მარტის ხელშეკრულების შესაბამისად.\n"
        "2. შესრულების თარიღი",
    )
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary["execution_period"] == "2026-04-01..2026-04-30"


def test_an_act_dated_only_in_its_heading_is_dated() -> None:
    act = ACT.replace("მიღება-ჩაბარების აქტი", "მიღება-ჩაბარების აქტი\nქ. თბილისი, 30.04.2026")
    act = act.replace("4. გაფორმების თარიღი\nაქტი გაფორმდა 2026 წლის 30 აპრილი\n", "")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary.get("act_date") == "2026-04-30"
    assert "AOA.ACT_DATE" not in [f.rule_id for f in report.findings]


def test_an_item_number_before_the_fee_is_not_part_of_it() -> None:
    assert _findings(ACT.replace("100 (ასი) ლარს", "1. 100 (ასი) ლარს")) == []


def test_tetri_are_read_with_the_fee() -> None:
    assert _findings(ACT.replace("100 (ასი) ლარს", "100 (ასი) ლარი და 00 თეთრი")) == []


def test_an_equivalent_in_another_currency_is_not_a_second_fee() -> None:
    act = ACT.replace("100 (ასი) ლარს", "100 (ასი) ლარს (ეკვივალენტი 37 აშშ დოლარი)")
    assert _findings(act) == []


def test_a_bracket_that_is_no_number_is_left_alone() -> None:
    act = ACT.replace(
        "მომსახურების გაწევა.", "მომსახურების გაწევა, დანართი 1 (ტექნიკური დავალება)."
    )
    assert _findings(act) == []


def test_a_contract_whose_parties_cannot_be_read_is_skipped_not_failed() -> None:
    contract = CONTRACT.replace("„ალფა ჯგუფი“", "ალფა ჯგუფი").replace(
        "„ბეტა სერვისი“", "ბეტა სერვისი"
    )
    report = check_aoa(aoa_text=ACT, contract_text=contract)
    assert "AOA.PARTY_NAME" not in [f.rule_id for f in report.findings]


def test_net_inside_a_company_name_is_not_net_wording() -> None:
    from procurement_agent.checks.contract_terms import tax_phrases

    assert [p.kind for p in tax_phrases("შპს „ნეტ ჯგუფი“ იღებს ვალდებულებას")] == []
    assert [p.kind for p in tax_phrases("ანაზღაურება NET თანხით")] == ["net"]
