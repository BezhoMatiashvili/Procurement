"""Extraction defects a red team found and a verifier reproduced (round 2), one test or more each,
with a near miss beside every widened pattern. Every document here is invented: names, codes and
figures are made up."""

from __future__ import annotations

import pytest

from procurement_agent.checks.compare import compare
from procurement_agent.checks.contract_terms import tax_phrases
from procurement_agent.checks.document_facts import document_facts
from procurement_agent.config import Settings
from procurement_agent.extraction.merger import merge, rule_hits_to_facts
from procurement_agent.extraction.pipeline import run_extraction
from procurement_agent.extraction.rules import (
    VAT_RATE_NOTE,
    RuleHit,
    find_amounts,
    find_contract_numbers,
    find_durations,
    find_percentages,
    is_rate_cap,
    percent_of,
)
from procurement_agent.extraction.schemas import ExtractedFact, FactType, Provenance
from procurement_agent.ingestion.chunker import Chunk
from procurement_agent.ingestion.segmenter import Segment, build

SETTINGS = Settings(ollama_base_url="http://localhost:11434")


def _extract(text: str) -> dict[FactType, list[str]]:
    chunk = Chunk(id="d:0", text=text, body=text, provenance=Provenance(chunk_id="d:0"))
    result = run_extraction(document_id="d", chunks=[chunk], settings=SETTINGS, llm=None)
    out: dict[FactType, list[str]] = {}
    for f in result.facts:
        out.setdefault(f.type, []).append(
            str(f.normalized if f.normalized is not None else f.value)
        )
    return out


def _conflicts(text: str) -> list[str]:
    chunk = Chunk(id="d:0", text=text, body=text, provenance=Provenance(chunk_id="d:0"))
    result = run_extraction(document_id="d", chunks=[chunk], settings=SETTINGS, llm=None)
    return [str(f.normalized) for f in result.facts if f.conflict]


def _amount_types(text: str) -> dict[float, FactType]:
    return {float(h.normalized): h.type for h in find_amounts(text)}  # type: ignore[arg-type]


def _facts(text: str, ft: FactType) -> list[str]:
    return [f.normalized for f in document_facts(text) if f.type is ft]


# --- EXTRACTION-R2-04: a lump-sum price table's lines are not the total ---

_LUMP_SUM_TABLE = (
    "4.1. სამუშაოს ღირებულება შეადგენს 1 700 (ათას შვიდასი) ლარს და განისაზღვრება ცხრილით:\n"
    "1, დასახელება = კარების შეცვლა. 1, ჯამი (ლარი) = 1 100.00 ლარი. "
    "2, დასახელება = იატაკის დაგება. 2, ჯამი (ლარი) = 600.00 ლარი. "
    ", დასახელება = სულ. , ჯამი (ლარი) = 1 700.00 ლარი"
)


def test_a_lump_sum_table_line_is_no_total() -> None:
    types = _amount_types(_LUMP_SUM_TABLE)
    assert types[1100.0] is FactType.amount
    assert types[600.0] is FactType.amount
    assert _extract(_LUMP_SUM_TABLE)[FactType.total_amount] == ["1700.0"]
    assert _conflicts(_LUMP_SUM_TABLE) == []


def test_a_lump_sum_line_keyed_by_its_item_number_is_no_total() -> None:
    text = "1.1, დასახელება = ღობის შეკეთება. 1.1, ჯამი = 450.00 ლარი"
    assert _amount_types(text) == {450.0: FactType.amount}


def test_a_one_row_table_under_a_total_column_is_still_the_total() -> None:
    # the near miss: no total row and no item number, so the row is the document's figure
    text = "დასახელება, ჯამი = 2 300.00 ლარი"
    assert _amount_types(text) == {2300.0: FactType.total_amount}


# --- EXTRACTION-R2-05 / COMPARE-R2-11: a penalty's cap is no penalty ---


@pytest.mark.parametrize(
    "clause",
    [
        "5.1. დაგვიანებისთვის „მიმწოდებელს“ ეკისრება პირგასამტეხლო 0.2%-ის ოდენობით ყოველ "
        "ვადაგადაცილებულ დღეზე, მაგრამ არაუმეტეს ხელშეკრულების ღირებულების 15%-ისა.",
        "5.1. დაგვიანებისთვის ეკისრება პირგასამტეხლო 0.2%-ის ოდენობით ყოველ დღეზე, "
        "არა უმეტეს ღირებულების 15%-ისა.",
        "5.1. For each day of delay the Supplier shall pay a penalty of 0.2% of the Contract "
        "price, not exceeding 15% of the Contract price.",
        "5.1. A penalty of 0.2% per day of delay applies, up to a maximum of 15% of the price.",
    ],
)
def test_a_penalty_cap_is_no_second_penalty(clause: str) -> None:
    assert _facts(clause, FactType.penalty) == ["0.2% per day"]
    assert _extract(clause)[FactType.penalty] == ["0.2% per day"]


def test_a_cap_word_before_a_count_leaves_the_rate_a_penalty() -> None:
    # the near miss: "not more than 5 days" caps the days, and the rate after it is the penalty
    clause = (
        "5.1. თუ დაგვიანება არაუმეტეს 5 დღისაა, პირგასამტეხლო შეადგენს 0.1%-ს ყოველ "
        "ვადაგადაცილებულ დღეზე."
    )
    assert _facts(clause, FactType.penalty) == ["0.1% per day"]
    [rate] = find_percentages(clause)
    assert not is_rate_cap(clause, rate.start, rate.end)


@pytest.mark.parametrize(
    "clause",
    [
        # the near miss: a cap word before a rate that carries its own period leaves it a rate
        "5.1. A penalty of up to 0.2% per day of delay applies.",
        "5.1. პირგასამტეხლო არაუმეტეს 0.2%-ისა ყოველ ვადაგადაცილებულ დღეზე.",
    ],
)
def test_a_capped_rate_with_its_own_period_is_still_the_penalty(clause: str) -> None:
    assert _facts(clause, FactType.penalty) == ["0.2% per day"]


@pytest.mark.parametrize(
    ("text", "ft", "value"),
    [
        # a rate in brackets is still a rate: the words-first form must not cost it
        (
            "5.1. პირგასამტეხლო (0.2%) ყოველ ვადაგადაცილებულ დღეზე.",
            FactType.penalty,
            "0.2% per day",
        ),
        ("4.2. ავანსი (30%).", FactType.payment_terms, "advance 30%"),
        ("4.1. ღირებულება 100 ლარი დღგ-ს (18%) ჩათვლით.", FactType.percentage, 18.0),
    ],
)
def test_a_rate_in_brackets_is_read(text: str, ft: FactType, value: object) -> None:
    [hit] = find_percentages(text)
    assert (hit.type, hit.normalized) == (ft, value)


def test_a_cap_changed_is_one_finding() -> None:
    base = [
        "ხელშეკრულება № TR-2026/77",
        "1.1. საგანი: სკამები.",
        "5.1. პირგასამტეხლო ღირებულების 0.1% ოდენობით ყოველ ვადაგადაცილებულ დღეზე.",
    ]
    capped = [*base[:2], base[2][:-1] + ", მაგრამ არაუმეტეს ღირებულების 10%-ისა."]
    found = _cmp(base, capped)
    assert [(r, b, a) for r, _, b, a, _ in found] == [("CMP.PERCENT", None, "10%")]
    raised = [*base[:2], capped[2].replace("10%", "20%")]
    found = _cmp(capped, raised)
    assert [(r, b, a) for r, _, b, a, _ in found] == [("CMP.PERCENT", "10%", "20%")]


# --- EXTRACTION-R2-06: an amount stated as a share of the value is not the total ---


@pytest.mark.parametrize(
    ("text", "share"),
    [
        (
            "4.1. ღირებულება შეადგენს 2 000 ლარს.\n7.1. საგარანტიო უზრუნველყოფა ღირებულების "
            "5%-ის, 100 (ასი) ლარის ოდენობით.",
            100.0,
        ),
        ("4.1. ღირებულება შეადგენს 2 000 ლარს, მათ შორის დღგ 18% - 305.08 ლარი.", 305.08),
        ("4.1. ღირებულება შეადგენს 2 000 ლარს, საიდანაც დღგ - 305.08 ლარი.", 305.08),
        (
            "4.1. The total Contract value is GEL 2,000.00. The performance security is 5% of "
            "the Contract value, GEL 100.00.",
            100.0,
        ),
    ],
)
def test_a_share_of_the_value_is_no_total(text: str, share: float) -> None:
    assert _amount_types(text)[share] is FactType.amount
    assert _extract(text)[FactType.total_amount] == ["2000.0"]
    assert _conflicts(text) == []


def test_a_share_of_the_price_is_no_price() -> None:
    text = "4.1. The Contract price is GEL 2,000.00. The security is 5% of the price, GEL 100.00."
    assert _amount_types(text) == {2000.0: FactType.price, 100.0: FactType.amount}


@pytest.mark.parametrize(
    ("text", "value", "ft"),
    [
        ("ღირებულება (18% დღგ-ს ჩათვლით) შეადგენს 1 000 ლარს.", 1000.0, FactType.total_amount),
        ("ღირებულება დღგ-ს ჩათვლით: 1 000 ლარი.", 1000.0, FactType.total_amount),
        ("Price including VAT: 118.00 GEL.", 118.0, FactType.price),
        ("ღირებულება შეადგენს 1 000 ლარს, ანუ 18% დღგ-ს ჩათვლით.", 1000.0, FactType.total_amount),
    ],
)
def test_a_price_beside_a_rate_is_still_the_price(text: str, value: float, ft: FactType) -> None:
    assert _amount_types(text)[value] is ft


# --- EXTRACTION-R2-07 / COMPARE-R2-07: an exemption from VAT is vat_status none ---


@pytest.mark.parametrize(
    "sentence",
    [
        "ღირებულება შეადგენს 900 ლარს. „შემსრულებელი“ არ არის დღგ-ს გადამხდელი.",
        "ღირებულება შეადგენს 900 ლარს და არ იბეგრება დღგ-ით.",
        "ღირებულება შეადგენს 900 ლარს და გათავისუფლებულია დღგ-სგან.",
        "ღირებულება შეადგენს 900 ლარს, დღგ-ით დაუბეგრავი.",
        "ღირებულება შეადგენს 900 ლარს, დღგ-სგან გათავისუფლებული.",
        "ღირებულება შეადგენს 900 ლარს და არ ექვემდებარება დღგ-ით დაბეგვრას.",
        "The price is GEL 900.00. The Supplier is not registered as a VAT payer.",
        "The price is GEL 900.00. The Services are exempt from VAT.",
        "The price is GEL 900.00 and is not subject to VAT.",
    ],
)
def test_an_exemption_from_vat_is_status_none(sentence: str) -> None:
    text = f"4.1. {sentence}"
    assert _facts(text, FactType.vat_status) == ["none"]
    assert _extract(text)[FactType.vat_status] == ["none"]


@pytest.mark.parametrize(
    "sentence",
    [
        # a VAT payer states nothing about the price
        "4.1. ღირებულება შეადგენს 900 ლარს. „შემსრულებელი“ არის დღგ-ს გადამხდელი.",
        # a tax number is a label
        "4.1. ღირებულება შეადგენს 900 ლარს. დღგ-ს გადამხდელის ნომერი: 123456789.",
        # released from liability, not from tax
        "4.1. ღირებულება შეადგენს 900 ლარს. მხარე გათავისუფლებულია პასუხისმგებლობისგან.",
    ],
)
def test_near_misses_of_an_exemption_are_no_status(sentence: str) -> None:
    assert _facts(sentence, FactType.vat_status) == []


def test_an_exemption_keeps_its_tax_word_a_mention() -> None:
    # the act check's rule for a foreign organisation offends on any tax word: an exemption
    # still names the tax
    kinds = {p.kind for p in tax_phrases("The Services are exempt from VAT.")}
    assert kinds == {"exempt", "mention"}


# --- EXTRACTION-R2-08: VAT added with a verb, a rate inside the phrase ---


@pytest.mark.parametrize(
    ("sentence", "status"),
    [
        ("ღირებულება შეადგენს 900 ლარს, რასაც დაემატება დღგ.", "excl"),
        ("ღირებულება შეადგენს 900 ლარს, დღგ-ს დამატებით.", "excl"),
        ("ღირებულება შეადგენს 900 ლარს, რასაც ემატება 18% დღგ.", "excl"),
        ("The price is GEL 900.00, inclusive of 18% VAT.", "incl"),
        ("The price is GEL 900.00. VAT 18% is included.", "incl"),
        ("The price is GEL 900.00 plus 18% VAT.", "excl"),
        ("The price is GEL 900.00, exclusive of 18% VAT.", "excl"),
        ("ღირებულება შეადგენს 900 ლარს დღგ-ს (18%) ჩათვლით.", "incl"),
    ],
)
def test_vat_added_with_a_verb_or_with_its_rate(sentence: str, status: str) -> None:
    text = f"4.1. {sentence}"
    assert _facts(text, FactType.vat_status) == [status]


@pytest.mark.parametrize(
    "sentence",
    [
        # an annex is added, not a tax
        "4.1. ღირებულება შეადგენს 900 ლარს. ხელშეკრულებას დაემატება დანართი.",
        # "additional" is not "in addition"
        "4.1. ღირებულება შეადგენს 900 ლარს. დღგ-ს დამატებითი ანგარიში იხსნება.",
        # a plus sign between two prices adds no tax
        "4.1. The price is GEL 900.00 plus GEL 18.00 delivery.",
    ],
)
def test_near_misses_of_an_added_tax_are_no_status(sentence: str) -> None:
    assert _facts(sentence, FactType.vat_status) == []


# --- COMPARE-R2-06: a rate beside დღგ is never a vat_status ---


def test_a_vat_rate_is_a_percentage_in_every_reader() -> None:
    [hit] = find_percentages("3.5. ფასს ემატება 18% დღგ.")
    assert hit.type is FactType.percentage


@pytest.mark.parametrize(
    ("text", "is_rate"),
    [
        ("ფასს ემატება 18%-იანი დღგ.", True),
        ("ღირებულება დღგ-ს (18%) ჩათვლით.", True),
        ("The price is inclusive of VAT at 18%.", True),
        # the near miss: a discount near the tax word is no rate of the tax
        ("ღირებულება დღგ-ს ჩათვლით, ფასდაკლება 5%.", False),
    ],
)
def test_only_a_rate_beside_the_tax_is_its_rate(text: str, is_rate: bool) -> None:
    [hit] = find_percentages(text)
    assert hit.type is FactType.percentage
    assert (hit.note == VAT_RATE_NOTE) is is_rate


def test_a_vat_rate_changed_is_a_percentage_change() -> None:
    base = ["ხელშეკრულება № TR-2026/77", "1.1. საგანი: სკამები."]
    found = _cmp([*base, "3.5. ფასს ემატება 18% დღგ."], [*base, "3.5. ფასს ემატება 20% დღგ."])
    assert [(r, b, a) for r, _, b, a, _ in found] == [("CMP.PERCENT", "18%", "20%")]


def test_inclusion_restated_with_its_rate_is_no_vat_change() -> None:
    base = ["ხელშეკრულება № TR-2026/77", "1.1. საგანი: სკამები."]
    left = [*base, "3.1. ღირებულება 100 ლარი დღგ-ს ჩათვლით."]
    right = [*base, "3.1. ღირებულება 100 ლარი დღგ-ს (18%) ჩათვლით."]
    # the rate is part of the tax wording: restated beside it, it is no value change of its own
    assert _cmp(left, right) == []
    plus = [*base, "3.1. ღირებულება 100 ლარი + 18% დღგ."]
    found = _cmp(left, plus)
    assert [(r, b, a, s) for r, _, b, a, s in found] == [("CMP.VAT", "incl", "excl", "error")]


def test_a_percentage_added_beside_no_tax_word_is_still_reported() -> None:
    # the near miss: only the VAT's rate is part of the tax wording
    base = ["ხელშეკრულება № TR-2026/77", "1.1. საგანი: სკამები."]
    left = [*base, "3.1. ღირებულება 100 ლარი დღგ-ს ჩათვლით."]
    right = [*base, "3.1. ღირებულება 100 ლარი დღგ-ს ჩათვლით, ფასდაკლება 5%."]
    found = _cmp(left, right)
    assert [(r, b, a) for r, _, b, a, _ in found] == [("CMP.PERCENT", None, "5%")]


def test_an_exempt_contract_against_an_inclusive_one_is_a_vat_error() -> None:
    base = ["ხელშეკრულება № TR-2026/77", "1.1. საგანი: სკამები."]
    left = [*base, "3.1. ღირებულება 100 ლარი დღგ-ს ჩათვლით."]
    right = [*base, "3.1. ღირებულება 100 ლარი, დღგ-ით დაუბეგრავი."]
    vat = [f for f in _cmp(left, right) if f[0] == "CMP.VAT"]
    assert [(b, a, s) for _, _, b, a, s in vat] == [("incl", "none", "error")]


# --- EXTRACTION-R2-09 / COMPARE-R2-14: the advance share in payment_terms ---


_STAGED_KA = (
    "4.2. ანგარიშსწორება მოხდება ეტაპობრივად: ავანსის სახით ღირებულების 30%-ს ხელშეკრულების "
    "გაფორმებიდან 5 (ხუთი) სამუშაო დღეში, ხოლო დარჩენილ 70%-ს - მიღება-ჩაბარების აქტის "
    "გაფორმებიდან 10 (ათი) სამუშაო დღის განმავლობაში."
)
_STAGED_EN = (
    "4.2. The Client shall pay an advance of 30% of the price within 5 banking days of signing, "
    "and the remaining 70% within 10 working days of the acceptance act."
)


@pytest.mark.parametrize("text", [_STAGED_KA, _STAGED_EN])
def test_a_staged_payment_states_the_balance_deadline_and_the_advance(text: str) -> None:
    assert _facts(text, FactType.payment_terms) == ["P10D working advance 30%"]
    assert _extract(text)[FactType.payment_terms] == ["P10D working advance 30%"]


@pytest.mark.parametrize(
    "text",
    [
        "4.2. „შემკვეთი“ გადაუხდის ავანსს ღირებულების 30%-ის ოდენობით.",
        "4.2. An advance payment of 30% of the price is paid.",
        "4.2. ავანსი: 30%.",
    ],
)
def test_an_advance_with_no_deadline_is_advance_n_percent(text: str) -> None:
    assert _extract(text)[FactType.payment_terms] == ["advance 30%"]
    [hit] = [h for h in find_percentages(text) if h.type is FactType.payment_terms]
    assert hit.normalized == "advance 30%"


def test_a_remainder_share_is_no_advance() -> None:
    # "the remaining 70%" is paid after the advance; it is not the advance, nor a payment term
    hits = {percent_of(h): h for h in find_percentages(_STAGED_KA)}
    assert hits[30.0].normalized == "advance 30%"
    assert hits[70.0].type is FactType.percentage


def test_a_payment_share_without_an_advance_is_no_payment_term() -> None:
    text = "4.2. ღირებულების 50%-ის გადახდა მოხდება მიწოდებისთანავე."
    [hit] = find_percentages(text)
    assert hit.type is FactType.percentage


def test_a_bare_number_is_never_a_payment_term_fact() -> None:
    prov = Provenance(chunk_id="d:0")
    llm = [
        ExtractedFact(
            type=FactType.payment_terms,
            label="Payment terms",
            value="30%",
            normalized=30.0,
            raw_text="30%",
            provenance=prov,
            source="llm",
            confidence=0.5,
        )
    ]
    merged = merge([], llm, [FactType.payment_terms])
    assert [(f.type, f.normalized) for f in merged] == [(FactType.percentage, 30.0)]
    chunk = Chunk(id="d:0", text="x", body="x", provenance=prov)
    hit = RuleHit(FactType.payment_terms, "10 days", "10 დღე", 0, 5, normalized="P10D", unit="day")
    [fact] = rule_hits_to_facts({"d:0": [hit]}, [chunk])
    assert fact.type is FactType.payment_terms


def test_an_advance_changed_is_reported_in_canonical_form() -> None:
    base = ["ხელშეკრულება № TR-2026/77", "1.1. საგანი: სკამები."]
    left = [*base, "3.3. შემსყიდველი უხდის ავანსს ღირებულების 20%-ის ოდენობით."]
    right = [*base, "3.3. შემსყიდველი უხდის ავანსს ღირებულების 30%-ის ოდენობით."]
    found = _cmp(left, right)
    assert [(r, b, a) for r, _, b, a, _ in found] == [("CMP.PAYMENT", "advance 20%", "advance 30%")]
    english = [*base, "3.3. An advance payment of 10% of the price is paid."]
    found = _cmp(english, [*base, english[2].replace("10%", "15%")])
    assert [(r, b, a) for r, _, b, a, _ in found] == [("CMP.PAYMENT", "advance 10%", "advance 15%")]


# --- EXTRACTION-R2-10: a percentage written in words first ---


@pytest.mark.parametrize(
    "clause",
    [
        "5.1. პირგასამტეხლო ღირებულების ნული მთელი ორი (0.2) პროცენტის ოდენობით ყოველ "
        "ვადაგადაცილებულ დღეზე.",
        "5.1. A penalty of zero point two (0.2) percent for each day of delay.",
        "5.1. პირგასამტეხლო (0.2)% ყოველ ვადაგადაცილებულ დღეზე.",
    ],
)
def test_a_percentage_in_words_first_is_read(clause: str) -> None:
    assert _facts(clause, FactType.penalty) == ["0.2% per day"]
    [hit] = find_percentages(clause)
    assert percent_of(hit) == 0.2


def test_a_bracketed_figure_before_a_percent_adjective_is_no_percentage() -> None:
    assert find_percentages("მუხლი (5) პროცენტული განაკვეთის შესახებ") == []
    assert find_percentages("ქვეპუნქტი 1) პროცენტის დარიცხვა") == []


# --- EXTRACTION-R2-12: a contract number with a Georgian letter ---


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("ხელშეკრულება N 31/2026-ა", "31/2026-ა"),
        ("ხელშეკრულება N 31/2026-ბ გაფორმდა", "31/2026-ბ"),
        ("ხელშეკრულება N ს-58/2026", "ს-58/2026"),
        ("ხელშეკრულება N 31/2026-B", "31/2026-B"),
        # a case ending on the number is no part of it
        ("ხელშეკრულება N 31/2026-ის საფუძველზე", "31/2026"),
        ("ხელშეკრულება N 31/2026-ს შესაბამისად", "31/2026"),
        ("ხელშეკრულება N 31/2026-ით", "31/2026"),
        ("ხელშეკრულება N 31/2026-ში", "31/2026"),
        ("ხელშეკრულება N 31/2026-ი", "31/2026"),
    ],
)
def test_a_contract_number_with_a_georgian_letter(text: str, number: str) -> None:
    assert [h.normalized for h in find_contract_numbers(text)] == [number]


def test_a_georgian_word_after_the_marker_is_no_number() -> None:
    assert find_contract_numbers("ხელშეკრულება N ს-ის შესახებ") == []


# --- EXTRACTION-R2-13: an amount whose magnitude is a word ---


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("4.1. ღირებულება შეადგენს 36 ათას ლარს გადასახადების ჩათვლით.", 36000.0),
        ("4.1. ღირებულება შეადგენს 2.5 მლნ ლარს.", 2500000.0),
        ("4.1. ღირებულება შეადგენს 2,5 მილიონ ლარს.", 2500000.0),
        ("4.1. The total Contract value is 2.5 million GEL.", 2500000.0),
        ("4.1. The total Contract value is GEL 2.5 million.", 2500000.0),
    ],
)
def test_an_amount_with_a_magnitude_word(text: str, value: float) -> None:
    assert _extract(text)[FactType.total_amount] == [str(value)]


@pytest.mark.parametrize(
    "text",
    [
        # thousands of pieces, not money
        "4.1. მიეწოდება 5 ათასი ცალი.",
        # "thousands of" is no magnitude of a figure
        "4.1. 5 ათასობით ლარი.",
        # metres, not millions
        "4.1. ფასი: 5 m GEL",
    ],
)
def test_near_misses_of_a_magnitude_word(text: str) -> None:
    values = [float(h.normalized) for h in find_amounts(text)]  # type: ignore[arg-type]
    assert all(v < 1000 for v in values)


# --- EXTRACTION-R2-14: a deadline counted from delivery is no delivery deadline ---


@pytest.mark.parametrize(
    "text",
    [
        "2.1. საქონლის მიწოდება განხორციელდება ხელშეკრულების გაფორმებიდან 30 (ოცდაათი) "
        "კალენდარული დღის ვადაში, ხოლო მონტაჟი - მიწოდებიდან 10 (ათი) დღის ვადაში.",
        "2.1. მიწოდება განხორციელდება 30 (ოცდაათი) დღის ვადაში, ხოლო მონტაჟი - მიწოდებიდან "
        "10 (ათი) დღის ვადაში.",
        "2.1. The goods shall be delivered within 30 days. Installation shall be completed "
        "within 10 days after delivery.",
    ],
)
def test_a_count_from_delivery_is_no_delivery_deadline(text: str) -> None:
    by_value = {h.normalized: h.type for h in find_durations(text)}
    assert by_value["P30D"] is FactType.delivery_deadline
    assert by_value["P10D"] is not FactType.delivery_deadline


def test_a_delivery_deadline_named_before_its_count_is_still_one() -> None:
    # the near miss: "მიწოდების ვადა" names the deadline; only "from delivery" does not
    text = "2.1. მიწოდების ვადა: ხელშეკრულების გაფორმებიდან 20 დღე."
    assert [h.type for h in find_durations(text)] == [FactType.delivery_deadline]


# --- comparison helpers ---


def _segs(texts: list[str]) -> list[Segment]:
    return build([(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(texts)])


def _cmp(left: list[str], right: list[str]) -> list[tuple[str, str, str | None, str | None, str]]:
    """(rule, message, before, after, severity) of every value finding. Whether a line's text
    change is reported beside it is the comparison's own policy, not what these tests pin."""
    report = compare(
        _segs(left),
        _segs(right),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    return [
        (f.rule_id, f.message_en, f.expected, f.actual, f.severity)
        for f in report.findings
        if f.severity != "info" and f.rule_id not in ("CMP.FORMATTING", "CMP.TEXT_CHANGED")
    ]
