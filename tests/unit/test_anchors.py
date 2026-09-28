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


def test_a_role_says_where_it_was_read() -> None:
    """The definition "(შემდგომში „…“)" wins over a signature block's bare role word, even one
    read first: a signature block can stand above the other column's name."""
    parties = find_parties(CONTRACT)
    assert [(p.role, p.role_source) for p in parties] == [
        ("შემკვეთი", "defined"),
        ("შემსრულებელი", "defined"),
    ]
    signed_first = "„შემსრულებელი“\nშპს „ალფა ჯგუფი“\nდირექტორი ნინო კაპანაძე\n" + CONTRACT
    alfa = find_parties(signed_first)[0]
    assert (alfa.name, alfa.role, alfa.role_source) == ("შპს ალფა ჯგუფი", "შემკვეთი", "defined")


def test_a_defined_term_that_is_not_a_role_does_not_replace_a_role() -> None:
    """ "(შემდგომში „მომსახურება“)" defines the service, not a party: a party named in the clause
    before it keeps the role its signature block gave it."""
    doc = (
        "„შემკვეთი“\nშპს „ალფა ჯგუფი“, საიდენტიფიკაციო კოდი: 123456789\n"
        "„შემსრულებელი“\nშპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321\n"
        "ხელშეკრულების საგანია შპს „ბეტა სერვისი“-ს მიერ ვებგვერდის დიზაინის შექმნა "
        "(შემდგომში „მომსახურება“).\n"
    )
    assert [(p.role, p.role_source) for p in find_parties(doc)] == [
        ("შემკვეთი", "signature"),
        ("შემსრულებელი", "signature"),
    ]


def test_a_signature_role_is_used_when_nothing_is_defined() -> None:
    doc = "ბოლო გვერდი\n„მიმწოდებელი“\nშპს „ბეტა სერვისი“\nდირექტორი: დავით მელაძე\n"
    [beta] = find_parties(doc)
    assert (beta.role, beta.role_source) == ("მიმწოდებელი", "signature")


def test_a_party_says_where_its_name_code_and_role_were_read() -> None:
    """Offsets into the text as ``find_parties`` normalised it: one flattened line indexes
    directly, which is the text the AOA checker hands it."""
    flat = " ".join(CONTRACT.split())
    beta = find_parties(flat)[1]
    assert beta.span is not None and beta.id_span is not None and beta.role_span is not None
    assert flat[slice(*beta.span)] == "შპს „ბეტა სერვისი“"
    assert flat[slice(*beta.id_span)] == "987654321"
    assert flat[slice(*beta.role_span)] == "შემდგომში „შემსრულებელი“"


_SHARED_CODE = CONTRACT.replace("987654321", "123456789")


def test_one_code_under_two_names_is_split_only_when_asked() -> None:
    """Merging two blocks on a shared code is right for extraction - one company written
    twice - but it hides a code given to the wrong party, so the AOA checker asks to keep
    them apart."""
    merged = find_parties(_SHARED_CODE)
    assert [(p.name, p.id_code) for p in merged] == [("შპს ალფა ჯგუფი", "123456789")]
    split = find_parties(_SHARED_CODE, split_conflicts=True)
    assert [(p.name, p.id_code, p.role) for p in split] == [
        ("შპს ალფა ჯგუფი", "123456789", "შემკვეთი"),
        ("შპს ბეტა სერვისი", "123456789", "შემსრულებელი"),
    ]


def test_splitting_still_merges_one_party_written_twice() -> None:
    """The signature block, with no code of its own, joins its party by name; two blocks with
    one code and one name are one party either way."""
    doc = (
        _SHARED_CODE
        + "„შემსრულებელი“\nშპს „ბეტა სერვისი“\nდირექტორი: დავით მელაძე\n"
        + "შპს „ალფა ჯგუფი“, საიდენტიფიკაციო კოდი: 123456789\n"
    )
    split = find_parties(doc, split_conflicts=True)
    assert [p.name for p in split] == ["შპს ალფა ჯგუფი", "შპს ბეტა სერვისი"]
    assert split[1].directors == ["დავით მელაძის"]


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
    # the account and the bank's code are two facts: a BIC is not an account number
    types = {FactType.bank_account, FactType.bank_code}
    hits = {h.unit: (h.type, h.value) for h in find_anchors(CONTRACT) if h.type in types}
    assert hits["IBAN"] == (FactType.bank_account, "GE29NB0000000101904917")
    assert hits["SWIFT"] == (FactType.bank_code, "AAAAGE22")


def test_cadastral_code_is_not_mistaken_for_an_id_code() -> None:
    hits = find_anchors(CONTRACT)
    assert [h.value for h in hits if h.type == FactType.cadastral_code] == ["01.02.03.456"]
    assert "01.02.03.456" not in [h.value for h in hits if h.type == FactType.party_id_code]


def test_no_parties_in_unrelated_text() -> None:
    assert find_parties("This document contains no Georgian parties at all.") == []
