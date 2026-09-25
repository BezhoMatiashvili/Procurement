from __future__ import annotations

import pytest

from procurement_agent.extraction.rules import (
    find_amounts,
    find_bank_accounts,
    find_cadastral_codes,
    find_contract_numbers,
    find_dates,
    find_durations,
    find_id_codes,
    find_percentages,
    month_ka_correction,
    nearest_keyword_group,
    parse_number,
    run_rules,
)
from procurement_agent.extraction.schemas import FactType


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,250.50", 1250.5),
        ("1.250,50", 1250.5),
        ("1 250 000", 1_250_000.0),
        ("1 250 000,75", 1_250_000.75),
        ("12500", 12500.0),
        ("12,500", 12500.0),
        ("0.5", 0.5),
        ("abc", None),
    ],
)
def test_parse_number(raw: str, expected: float | None) -> None:
    assert parse_number(raw) == expected


def test_amounts_georgian() -> None:
    text = "ხელშეკრულების საერთო ღირებულება შეადგენს 125 400,00 ლარს დღგ-ს ჩათვლით."
    hits = find_amounts(text)
    assert len(hits) == 1
    h = hits[0]
    assert h.normalized == 125400.0
    assert h.currency == "GEL"
    assert h.type == FactType.total_amount
    assert text[h.start : h.end] == h.raw_text


def test_amounts_with_spelled_out_number_in_parentheses() -> None:
    text = (
        "საერთო ღირებულება შეადგენს 48 750,00 (ორმოცდარვა ათას შვიდას ორმოცდაათი) ლარს "
        "დღგ-ს ჩათვლით"
    )
    hits = find_amounts(text)
    assert [(h.normalized, h.currency, h.type) for h in hits] == [
        (48750.0, "GEL", FactType.total_amount)
    ]


def test_amounts_english_symbol_before() -> None:
    hits = find_amounts("Unit price: $1,250.00 per item; total USD 25,000.00")
    values = sorted((h.normalized, h.currency) for h in hits)
    assert values == [(1250.0, "USD"), (25000.0, "USD")]
    types = {h.normalized: h.type for h in hits}
    assert types[1250.0] == FactType.price
    assert types[25000.0] == FactType.total_amount


def test_amounts_penalty_context() -> None:
    hits = find_amounts("პირგასამტეხლო შეადგენს 500 ₾ ყოველ ვადაგადაცილებულ დღეზე")
    assert hits[0].type == FactType.penalty
    assert hits[0].currency == "GEL"


def test_amounts_no_currency_ignored() -> None:
    assert find_amounts("see page 12 of 40") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100 აშშ დოლარი", (100.0, "USD")),
        ("100 აშშ დოლარის", (100.0, "USD")),
        ("100 აშშ დოლარს", (100.0, "USD")),
        ("100 დოლარით", (100.0, "USD")),
        ("100 დოლარად", (100.0, "USD")),
        ("100 ა.შ.შ. დოლარი", (100.0, "USD")),
        ("11 000 (თერთმეტი ათასი) აშშ დოლარს", (11000.0, "USD")),
        ("100 US dollars", (100.0, "USD")),
        ("US$1,000", (1000.0, "USD")),
        ("100 ევროს", (100.0, "EUR")),
        ("100 ევროთი", (100.0, "EUR")),
        ("100 euros", (100.0, "EUR")),
        ("100 ფუნტი სტერლინგი", (100.0, "GBP")),
        ("100 pounds sterling", (100.0, "GBP")),
    ],
)
def test_amounts_in_every_case_a_currency_name_takes(
    text: str, expected: tuple[float, str]
) -> None:
    assert [(h.normalized, h.currency) for h in find_amounts(text)] == [expected]


def test_a_dollar_amount_is_never_read_as_lari() -> None:
    """ "დოლარ" (dollar) contains "ლარ" (lari), so the lari name must be tested last."""
    assert [(h.normalized, h.currency) for h in find_amounts("100 დოლარს")] == [(100.0, "USD")]


def test_the_year_after_a_dollar_amount_is_not_an_amount() -> None:
    text = "ავანსი 1 000 აშშ დოლარს 2026 წლის 10 სექტემბერს"
    assert [(h.normalized, h.currency) for h in find_amounts(text)] == [(1000.0, "USD")]


@pytest.mark.parametrize(
    "text",
    [
        # the lari pattern found "ლარს" inside "დოლარს" and read the year as 2,026 lari
        "აშშ დოლარს 2026 წლის",
        # before a figure only a code or a sign is a currency, never a name
        "თანხა დოლარს 100",
        # "ლარ" inside an ordinary word
        "იდენტიურ ეგზემპლარად",
        # a weight: sterling is money only as "pound sterling"
        "100 pounds",
    ],
)
def test_no_amount_where_a_currency_name_is_not_written_after_a_figure(text: str) -> None:
    assert find_amounts(text) == []


@pytest.mark.parametrize(
    ("text", "iso"),
    [
        ("ხელშეკრულება გაფორმდა 2026 წლის 15 მარტს", "2026-03-15"),
        ("თარიღი: 15 მარტი, 2026 წ.", "2026-03-15"),
        ("dated 15 March 2026", "2026-03-15"),
        ("dated March 15, 2026", "2026-03-15"),
        ("Date: 15.03.2026", "2026-03-15"),
        ("Date: 15/03/2026", "2026-03-15"),
        ("Date: 2026-03-15", "2026-03-15"),
        ("3rd of Sept 2026 is written as 3 Sep 2026", "2026-09-03"),
        ("OCR noise: 2026. წლის 31 აგვისტოს ჩათვლით", "2026-08-31"),
    ],
)
def test_dates(text: str, iso: str) -> None:
    hits = find_dates(text)
    assert hits, text
    assert hits[0].normalized == iso
    assert text[hits[0].start : hits[0].end] == hits[0].raw_text


def test_dates_invalid_ignored() -> None:
    assert find_dates("31.02.2026 and 99/99/2026 and 15.03.1850") == []


def test_dates_classified_by_context() -> None:
    hits = find_dates("მიწოდების ვადა: 30.06.2026. ხელშეკრულება ძალაშია 31.12.2026-მდე.")
    assert [h.type for h in hits] == [FactType.delivery_deadline, FactType.validity_period]


def test_id_codes() -> None:
    text = 'შპს "ალფა" ს/კ 204567890, დირექტორი ... ID code: 01001234567'
    hits = find_id_codes(text)
    assert [h.value for h in hits] == ["204567890", "01001234567"]
    assert all(h.type == FactType.party_id_code for h in hits)


def test_id_code_tolerates_pdf_spacing() -> None:
    # docling/OCR often splits punctuation: "ს / კ 405123456"
    assert [h.value for h in find_id_codes("შპს ალფა ( ს / კ 405123456 )")] == ["405123456"]


def test_id_code_needs_marker() -> None:
    assert find_id_codes("phone 555123456 ordered 204567890 items") == []


def test_percentages() -> None:
    hits = find_percentages("ავანსი 30% წინასწარ; დღგ 18%; ჯარიმა 0.1% დღეში; humidity 250%")
    assert [(h.normalized, h.type) for h in hits] == [
        (30.0, FactType.payment_terms),
        (18.0, FactType.vat_status),
        (0.1, FactType.penalty),
    ]


def test_durations_georgian() -> None:
    text = "საგარანტიო ვადა შეადგენს 24 თვეს. მიწოდება 30 კალენდარული დღის განმავლობაში."
    hits = find_durations(text)
    assert [(h.value, h.type) for h in hits] == [
        ("24 months", FactType.warranty_period),
        ("30 calendar days", FactType.delivery_deadline),
    ]
    assert hits[0].normalized == "P24M"
    assert hits[1].normalized == "P30D"


def test_durations_ignore_georgian_year_phrases() -> None:
    hits = find_durations("მიწოდება არაუგვიანეს 2026 წლის 30 აპრილისა; ვადა 2 წელი.")
    assert [(h.value, h.unit) for h in hits] == [("2 years", "year")]


def test_durations_english() -> None:
    hits = find_durations("Payment within 10 (ten) business days after invoice. Warranty: 2 years.")
    assert [(h.value, h.type) for h in hits] == [
        ("10 business days", FactType.payment_terms),
        ("2 years", FactType.warranty_period),
    ]


def test_contract_numbers() -> None:
    hits = find_contract_numbers("ხელშეკრულება № PR-2026/017 და Contract No. C-44")
    assert [h.value for h in hits] == ["PR-2026/017", "C-44"]


def test_keyword_context_survives_inline_run_newlines() -> None:
    # Docling serialises bold runs on their own line
    hits = find_amounts(
        "ხელშეკრულების საერთო ღირებულება შეადგენს\n48 750,00\n(ორმოცდარვა ათას) ლარს"
    )
    assert [(h.normalized, h.type) for h in hits] == [(48750.0, FactType.total_amount)]


def test_georgian_keyword_matches_inside_prefixed_word() -> None:
    """Georgian builds words with prefixes, so the keyword "ფას" has to match inside
    "საფასური". The longer keyword wins the classification: in an acceptance act the
    საფასური *is* the total, and labelling it a unit price made it invisible to the
    contract comparison."""
    hits = find_amounts("“მომსახურების” საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით.")
    assert [(h.normalized, h.type) for h in hits] == [(100.0, FactType.total_amount)]


def test_a_unit_price_is_still_a_unit_price() -> None:
    hits = find_amounts("ერთეულის ფასი შეადგენს 12,50 ლარს")
    assert [(h.normalized, h.type) for h in hits] == [(12.5, FactType.price)]


def _around(text: str, needle: str) -> tuple[int, int]:
    start = text.index(needle)
    return start, start + len(needle)


def test_nearest_keyword_group_takes_any_labels() -> None:
    """The classifier's weighing, lifted out so a check can give an amount its own role."""
    text = "ავანსი შეადგენს 300 ლარს, ხოლო ღირებულება შეადგენს 1000 ლარს."
    groups = {"advance": ("ავანს",), "price": ("ღირებულებ",)}
    assert nearest_keyword_group(text, *_around(text, "300"), groups) == "advance"
    assert nearest_keyword_group(text, *_around(text, "1000"), groups) == "price"


def test_nearest_keyword_group_stays_inside_the_sentence() -> None:
    text = "ავანსი არ გაიცემა. თანხა შეადგენს 100 ლარს."
    assert nearest_keyword_group(text, *_around(text, "100"), {"advance": ("ავანს",)}) is None


def test_nearest_keyword_group_breaks_a_tie_by_group_order() -> None:
    text = "თანხა 100"
    span = _around(text, "100")
    assert nearest_keyword_group(text, *span, {"a": ("თანხა",), "b": ("თანხა",)}) == "a"
    assert nearest_keyword_group(text, *span, {"b": ("თანხა",), "a": ("თანხა",)}) == "b"


def test_dates_do_not_double_count_overlapping_matches() -> None:
    hits = find_dates("შესრულდა 2026 წლის 1 აგვისტოდან 2026 წლის 31 აგვისტოს ჩათვლით")
    assert [h.normalized for h in hits] == ["2026-08-01", "2026-08-31"]
    assert all(h.type == FactType.execution_period for h in hits)


def test_act_date_keyword() -> None:
    hits = find_dates("გაფორმების თარიღი: „აქტი“ გაფორმდა 2026 წლის 31 აგვისტო")
    assert [(h.normalized, h.type) for h in hits] == [("2026-08-31", FactType.contract_date)]


def test_bank_accounts() -> None:
    text = "ბანკი: სს „საქართველოს ბანკი“; ბანკის კოდი: AAAAGE22; ა/ა: GE00XX0000000000000001"
    hits = find_bank_accounts(text)
    assert {(h.unit, h.value) for h in hits} == {
        ("SWIFT", "AAAAGE22"),
        ("IBAN", "GE00XX0000000000000001"),
    }


def test_cadastral_codes() -> None:
    hits = find_cadastral_codes("პროექტისთვის (ს/კ: 01.02.03.456) და ნაკვეთი 01.10.14.006.021")
    assert [h.value for h in hits] == ["01.02.03.456", "01.10.14.006.021"]
    assert find_cadastral_codes("version 1.2.3.456") == []


@pytest.mark.parametrize(
    ("text", "codes"),
    [
        # the full stop that ends the sentence is not part of the code, and does not hide it
        ("საკადასტრო კოდი: 01.02.03.456.", ["01.02.03.456"]),
        ("კოდი 01.10.14.006.021. შემდეგი", ["01.10.14.006.021"]),
        # six and seven groups: a building or a unit inside the parcel, read whole
        (
            "ბინა 01.19.21.004.024.01 და 01.19.21.004.024.01.513.",
            ["01.19.21.004.024.01", "01.19.21.004.024.01.513"],
        ),
        # a group too long, a date, or more digits after a dot: no code
        ("01.02.03.4567", []),
        ("01.02.2026", []),
        ("01.02.03.456.7", []),
    ],
)
def test_cadastral_codes_of_four_to_seven_groups(text: str, codes: list[str]) -> None:
    assert [h.value for h in find_cadastral_codes(text)] == codes


@pytest.mark.parametrize(
    ("word", "correction"),
    [
        ("აგვუისტას", "აგვისტოს"),
        ("აგისტოდან", "აგვისტოდან"),
        ("აპრლიდან", "აპრილიდან"),
        ("სეტემბრის", "სექტემბრის"),
        ("თებრვლის", "თებერვლის"),
        ("ნოებმერი", "ნოემბერი"),
        ("დეკმბრიდან", "დეკემბრიდან"),
    ],
)
def test_a_misspelled_month_is_corrected_in_the_case_it_was_written(
    word: str, correction: str
) -> None:
    assert month_ka_correction(word) == correction


def test_a_word_that_is_no_month_has_no_correction() -> None:
    assert month_ka_correction("ხელშეკრულება") is None


@pytest.mark.parametrize("word", ["აგისტოსთვის", "სექტემრისთვის", "აგისტოშივე"])
def test_a_month_with_an_ending_the_forms_do_not_list_gets_no_correction(word: str) -> None:
    """The nearest listed form would drop the ending or change the case ("აგვისტოთი" for
    "აგისტოსთვის"): a wrong correction is worse than none."""
    assert month_ka_correction(word) is None


def test_run_rules_sorted_and_spans_valid() -> None:
    text = (
        "Contract No. 7/2026 dated 01.02.2026. Total: 10,000 EUR. VAT 18%. "
        "Delivery within 45 calendar days. ს/კ 404404404."
    )
    hits = run_rules(text)
    assert hits == sorted(hits, key=lambda h: (h.start, h.end))
    for h in hits:
        assert text[h.start : h.end] == h.raw_text
    assert {h.type for h in hits} >= {
        FactType.contract_number,
        FactType.contract_date,
        FactType.total_amount,
        FactType.vat_status,
        FactType.delivery_deadline,
        FactType.party_id_code,
    }


def test_offer_and_invoice_numbers_are_found_not_only_contracts() -> None:
    """`contract_number` is documented as the contract/offer/tender reference; the pattern
    used to know only about contracts, so every offer number was missed."""
    assert [h.value for h in find_contract_numbers("Commercial Offer № OFF-2026/031")] == [
        "OFF-2026/031"
    ]
    assert [h.value for h in find_contract_numbers("კომერციული წინადადება №KP-11/2026")] == [
        "KP-11/2026"
    ]
    assert [h.value for h in find_contract_numbers("Invoice #INV-2026-88")] == ["INV-2026-88"]


def test_a_bare_number_sign_is_not_a_reference_number() -> None:
    """In a table "№ 5" is a row number far more often than a reference."""
    assert find_contract_numbers("სულ ჯამი № 5") == []
