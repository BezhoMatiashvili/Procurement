"""Extraction on what the documents state as a whole, and on the labels that used to over-match.
Every document here is invented."""

from __future__ import annotations

from procurement_agent.checks.document_facts import document_facts
from procurement_agent.extraction.anchors import find_anchors, find_parties
from procurement_agent.extraction.rules import find_contract_numbers
from procurement_agent.extraction.schemas import FactType
from tests.unit.test_aoa_check import ACT, CONTRACT


def _facts(text: str) -> dict[FactType, list[str]]:
    out: dict[FactType, list[str]] = {}
    for f in document_facts(text):
        out.setdefault(f.type, []).append(f.normalized)
    return out


def test_an_acts_period_is_one_range_and_its_own_date_is_the_act_date() -> None:
    facts = _facts(ACT)
    assert facts[FactType.execution_period] == ["2026-04-01/2026-04-30"]
    assert facts[FactType.act_date] == ["2026-04-30"]
    assert FactType.contract_date not in facts


def test_a_contracts_term_signing_and_payment() -> None:
    facts = _facts(CONTRACT)
    assert facts[FactType.contract_date] == ["2026-03-01"]
    assert facts[FactType.execution_period] == ["2026-03-01/2026-09-01"]
    assert facts[FactType.vat_status] == ["incl"]


def test_payment_terms_are_a_duration_with_the_days_that_count() -> None:
    act = ACT.replace(
        "გადასახადების ჩათვლით.",
        "გადასახადების ჩათვლით. საფასური გადაიხდება 5 (ხუთი) სამუშაო დღის განმავლობაში.",
    )
    assert _facts(act)[FactType.payment_terms] == ["P5D working"]


def test_a_penalty_per_day_says_so() -> None:
    contract = CONTRACT + (
        "5.1. „შემსრულებელს“ დაეკისრება პირგასამტეხლო ღირებულების 0.5% ოდენობით, ყოველ "
        "ვადაგადაცილებულ დღეზე.\n"
    )
    assert _facts(contract)[FactType.penalty] == ["0.5% per day"]


def test_a_swift_transfer_names_no_bank_code() -> None:
    codes = [
        h.value for h in find_anchors("Payment by SWIFT transfer.") if h.type is FactType.bank_code
    ]
    assert codes == []
    labelled = find_anchors("ბანკის კოდი: NBGEGE22; SWIFT: AAAGE22")
    assert [h.value for h in labelled if h.type is FactType.bank_code] == ["NBGEGE22", "AAAGE22"]


def test_the_director_label_must_end_its_word() -> None:
    party = "შპს „ნიმუში“, საიდენტიფიკაციო კოდი: 123456789, "
    assert find_parties(party + "დირექტორთან შეთანხმებით")[0].directors == []
    assert find_parties(party + "დირექტორის მოადგილე ნინო კაპანაძე")[0].directors == []


def test_a_contact_number_is_not_a_contract_number() -> None:
    text = "Contact #5551234; კონტაქტი N 599123456; ხელშეკრულება № PR-2026/017"
    assert [h.value for h in find_contract_numbers(text)] == ["PR-2026/017"]
