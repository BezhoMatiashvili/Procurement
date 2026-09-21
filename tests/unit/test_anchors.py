"""Label-anchor extraction. All fixtures are synthetic; real client documents never enter tests."""

from __future__ import annotations

from procurement_agent.extraction.anchors import find_anchors, find_parties
from procurement_agent.extraction.schemas import FactType

CONTRACT = """ხელშეკრულება
საქართველო, თბილისი                    1 მარტი, 2026 წელი
შპს „ალფა ჯგუფი“ შეზღუდული პასუხისმგებლობის საზოგადოება. საიდენტიფიკაციო კოდი: 123456789;
იურიდიული მისამართი: ქ. თბილისი, რუსთაველის 12 (შემდგომში „შემკვეთი“) წარმოდგენილი
დირექტორის ნინო კაპანაძის სახით და
შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321, იურიდიული მისამართი: ქ. ბათუმი,
ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის დავით მელაძის სახით
1.1. „შემსრულებელი“ იღებს ვალდებულებას „მზე“ პროექტისთვის (ს/კ: 01.02.03.456) მომსახურების გაწევა.
4.1. ღირებულება შეადგენს 100 (ასი) ლარს.
4.3. ანგარიშსწორება მოხდება ანგარიშზე: სს „საქართველოს ბანკი“, ბანკის კოდი: AAAAGE22,
ანგარიშის ნომერი: GE29NB0000000101904917.
"""


def test_finds_both_parties_with_their_labelled_fields() -> None:
    parties = find_parties(CONTRACT)
    assert len(parties) == 2
    a, b = parties
    assert a.name == "შპს ალფა ჯგუფი"
    assert a.id_code == "123456789"
    assert a.address == "ქ. თბილისი, რუსთაველის 12"
    assert a.role == "შემკვეთი"
    assert b.id_code == "987654321"
    assert b.role == "შემსრულებელი"


def test_director_is_read_out_of_the_genitive_construction() -> None:
    """'წარმოდგენილი დირექტორის ნინო კაპანაძის სახით' - the trailing role noun must be cut."""
    parties = find_parties(CONTRACT)
    assert parties[0].directors == ["ნინო კაპანაძის"]
    assert parties[1].directors == ["დავით მელაძის"]


def test_the_bank_in_the_payment_details_is_not_a_contracting_party() -> None:
    assert all("ბანკი" not in (p.name or "") for p in find_parties(CONTRACT))


def test_requisites_and_signature_blocks_merge_into_one_party() -> None:
    doc = CONTRACT + "\n„შემსრულებელი“\nშპს „ბეტა სერვისი“\nდირექტორი: დავით მელაძე\n"
    parties = find_parties(doc)
    assert len(parties) == 2
    beta = parties[1]
    # the same person in two grammatical cases is one director, not two
    assert len(beta.directors) == 1


def test_project_name_is_found_before_or_after_the_word_project() -> None:
    after = find_anchors("პროექტ „მზე“ (ს/კ: 01.02.03.456)")
    before = find_anchors("„მზე“ პროექტისთვის")
    assert [h.value for h in after if h.type == FactType.project_name] == ["მზე"]
    assert [h.value for h in before if h.type == FactType.project_name] == ["მზე"]


def test_a_role_word_before_project_is_not_the_project_name() -> None:
    """'„შემკვეთის“ პროექტ „მზე“' names the client, then the project."""
    values = [
        h.value for h in find_anchors("„შემკვეთის“ პროექტ „მზე“") if h.type == FactType.project_name
    ]
    assert values == ["მზე"]


def test_bank_details_carry_their_kind() -> None:
    hits = {h.unit: h.value for h in find_anchors(CONTRACT) if h.type == FactType.bank_account}
    assert hits["IBAN"] == "GE29NB0000000101904917"
    assert hits["SWIFT"] == "AAAAGE22"


def test_cadastral_code_is_not_mistaken_for_an_id_code() -> None:
    hits = find_anchors(CONTRACT)
    assert [h.value for h in hits if h.type == FactType.cadastral_code] == ["01.02.03.456"]
    assert "01.02.03.456" not in [h.value for h in hits if h.type == FactType.party_id_code]


def test_no_parties_in_unrelated_text() -> None:
    assert find_parties("This document contains no Georgian parties at all.") == []
