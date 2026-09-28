"""What the model proposes is checked against the shape its type must have, and against the codes
the document's own labels already name. The model is opt-in; when it is used, a cadastral code
filed as a contract number or a bank block filed as payment terms must not reach the report.
Synthetic values only."""

from __future__ import annotations

from procurement_agent.extraction.llm_extractor import plausible
from procurement_agent.extraction.merger import merge
from procurement_agent.extraction.schemas import ExtractedFact, FactType, Provenance


def test_a_contract_number_is_not_a_cadastral_code_an_account_or_a_date() -> None:
    assert plausible(FactType.contract_number, "N 12/2026")
    assert plausible(FactType.contract_number, "PR-2026/017")
    for value in ("01.02.03.456", "GE29NB0000000101904917", "2026-03-01", "01.03.2026"):
        assert not plausible(FactType.contract_number, value), value


def test_payment_terms_name_a_time_or_a_share_not_a_bank() -> None:
    assert plausible(FactType.payment_terms, "აქტის გაფორმებიდან 5 სამუშაო დღის ვადაში")
    assert plausible(FactType.payment_terms, "30% ავანსი, 70% მიღების შემდეგ")
    assert plausible(FactType.payment_terms, "ხუთი სამუშაო დღის ვადაში")
    assert not plausible(
        FactType.payment_terms, "სს „ნიმუშის ბანკი“, ბანკის კოდი: AAAAGE22, ა/ა: GE29NB00001019"
    )
    assert not plausible(FactType.payment_terms, "უნაღდო ანგარიშსწორებით")


def test_periods_amounts_and_quantities_carry_what_they_measure() -> None:
    assert plausible(FactType.validity_period, "2026 წლის 1 მარტიდან 2027 წლის 1 მარტამდე")
    assert plausible(FactType.validity_period, "უვადოდ")  # without a term is a term
    assert plausible(FactType.warranty_period, "24 თვე")
    assert not plausible(FactType.warranty_period, "მიმწოდებლის პასუხისმგებლობით")
    assert plausible(FactType.total_amount, "2 400 ლარი")
    assert not plausible(FactType.total_amount, "ღირებულება ხელშეკრულების მიხედვით")
    assert plausible(FactType.quantity, "4 ცალი")
    assert not plausible(FactType.quantity, "სერვისის გვერდის აღწერა GEO/ENG")


def _fact(ft: FactType, value: str, source: str) -> ExtractedFact:
    return ExtractedFact(
        type=ft,
        label=ft.value,
        value=value,
        raw_text=value,
        provenance=Provenance(page=1),
        source=source,  # type: ignore[arg-type]
        confidence=0.7,
    )


def test_a_value_the_labels_already_name_as_another_code_is_not_taken_from_the_model() -> None:
    anchors = [_fact(FactType.cadastral_code, "01.02.03.456", "rules")]
    llm = [
        _fact(FactType.contract_number, "01.02.03.456", "llm"),
        _fact(FactType.project_name, "ზღვის რეზიდენსი", "llm"),
    ]
    merged = merge([], llm, [FactType.contract_number, FactType.project_name], anchor_facts=anchors)
    assert [(f.type, f.value) for f in merged if f.source == "llm"] == [
        (FactType.project_name, "ზღვის რეზიდენსი")
    ]
