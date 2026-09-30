"""Values the rules read the way documents actually write them: counts in words first, the lari
in every case and abbreviated, tetri in figures, a percent in words, a penalty's period, a
reference number with spaced separators, line totals in a price table, deadlines stated with a
verb, VAT wordings, a subject clause without a defined term, and project names already in the
nominative. Every document here is invented."""

from __future__ import annotations

import pytest

from procurement_agent.config import Settings
from procurement_agent.extraction.pipeline import run_extraction
from procurement_agent.extraction.rules import (
    find_amounts,
    find_contract_numbers,
    find_durations,
    find_percentages,
    normalize_fact_value,
    penalty_period,
)
from procurement_agent.extraction.schemas import FactType, Provenance
from procurement_agent.ingestion.chunker import Chunk

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


def _amounts(text: str) -> list[tuple[float, str | None, FactType]]:
    return [(float(h.normalized), h.currency, h.type) for h in find_amounts(text)]  # type: ignore[arg-type]


# --- counts written in words first (EXTRACTION-01) ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "normalized", "unit"),
    [
        ("თარიღიდან ათი (10) სამუშაო დღის განმავლობაში", "P10D", "day"),
        ("საგარანტიო ვადა შეადგენს ოცდაოთხი (24) თვეს", "P24M", "month"),
        ("ოცდაათი (30) კალენდარული დღის ვადაში", "P30D", "day"),
        ("within ten (10) banking days", "P10D", "day"),
        ("a warranty of 24 (twenty-four) months", "P24M", "month"),
        ("შვიდი ( 7 ) დღე", "P7D", "day"),
    ],
)
def test_a_count_in_words_before_its_figure_is_a_duration(
    text: str, normalized: str, unit: str
) -> None:
    hits = find_durations(text)
    assert [(h.normalized, h.unit) for h in hits] == [(normalized, unit)]


def test_a_numbered_list_item_is_no_duration() -> None:
    assert find_durations("ვალდებულია: 1) დღის განმავლობაში შეატყობინოს") == []


def test_words_first_counts_reach_the_extracted_terms() -> None:
    facts = _extract(
        "4.2. ღირებულება გადაიხდება აქტის გაფორმებიდან ათი (10) სამუშაო დღის განმავლობაში.\n"
        "6.1. საგარანტიო ვადა შეადგენს ოცდაოთხი (24) თვეს.\n"
        "7.1. საქონლის მიწოდება განხორციელდება ოცდაათი (30) კალენდარული დღის ვადაში."
    )
    assert facts[FactType.payment_terms] == ["P10D working"]
    assert facts[FactType.warranty_period] == ["P24M"]
    assert facts[FactType.delivery_deadline] == ["P30D"]


# --- the lari in every case, abbreviated, in English (AOA-04, EXTRACTION-15, COMPARE-21) ----------


@pytest.mark.parametrize(
    "text",
    [
        "საფასური განისაზღვრა 3 180 (სამი ათას ას ოთხმოცი) ლარით",
        "საფასური განისაზღვრა 3 180 (სამი ათას ას ოთხმოცი) ლარად",
        "ღირებულება შეადგენს 3 180 (სამი ათას ას ოთხმოცი) ქართული ლარი",
        "ღირებულება შეადგენს 3 180 (სამი ათას ას ოთხმოცი) ქართულ ლარს",
        "The total amount is 3,180.00 Lari (three thousand one hundred eighty)",
        "The total amount is 3,180.00 (three thousand one hundred eighty) lari",
        "ღირებულება შეადგენს 3 180 ლ. (სამი ათას ას ოთხმოცი ლარი)",
        "სულ ღირებულება: 3 180 ლ.",
    ],
)
def test_the_lari_is_read_in_every_form_a_contract_writes_it(text: str) -> None:
    assert [(v, c) for v, c, _ in _amounts(text)] == [(3180.0, "GEL")]


def test_the_lari_abbreviation_needs_money_around_it() -> None:
    # "ლ." is also a litre: without a word about money nearby it is no amount
    assert find_amounts("ავზის მოცულობა 20 ლ. არის") == []


def test_a_dollar_amount_in_every_case_is_still_dollars() -> None:
    assert [(v, c) for v, c, _ in _amounts("საფასური 250 აშშ დოლარით")] == [(250.0, "USD")]


# --- tetri in figures (COMPARE-22) ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("ღირებულება შეადგენს 4 150 ლარი და 35 თეთრი დღგ-ს ჩათვლით.", 4150.35),
        ("ღირებულება შეადგენს 4 150 ლარს და 5 თეთრს.", 4150.05),
        ("ღირებულება შეადგენს 4 150 ლარი 35 თეთრი.", 4150.35),
        ("The price is 4,150 lari and 35 tetri.", 4150.35),
        ("ღირებულება შეადგენს 4 150.35 ლარს.", 4150.35),
    ],
)
def test_tetri_in_figures_are_the_amounts_fraction(text: str, value: float) -> None:
    got = _amounts(text)
    assert len(got) == 1
    assert got[0][0] == pytest.approx(value)
    assert got[0][1] == "GEL"


def test_tetri_after_dollars_or_after_a_fraction_are_not_added() -> None:
    assert [v for v, _, _ in _amounts("ღირებულება 4 150.20 ლარი და 35 თეთრი")] == [4150.2]
    assert [v for v, _, _ in _amounts("საფასური 100 აშშ დოლარი და 35 თეთრი")] == [100.0]


# --- a percent in words (COMPARE-15) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("პირგასამტეხლო ღირებულების 0.3 პროცენტის ოდენობით", 0.3),
        ("პირგასამტეხლო ღირებულების 0.3 (ნული მთელი სამი) პროცენტის ოდენობით", 0.3),
        ("a penalty of 0.2 percent of the price", 0.2),
        ("a penalty of 0.2 per cent of the price", 0.2),
        ("a penalty of 0.2% of the price", 0.2),
    ],
)
def test_a_percentage_is_read_with_the_word_as_with_the_sign(text: str, value: float) -> None:
    hits = find_percentages(text)
    assert [h.normalized for h in hits] == [value]
    assert hits[0].type == FactType.penalty


def test_a_percentage_adjective_is_no_percentage() -> None:
    assert find_percentages("10 პროცენტული პუნქტით") == []


# --- a penalty's period (EXTRACTION-19, COMPARE-16) -----------------------------------------------


@pytest.mark.parametrize(
    ("text", "period"),
    [
        ("პირგასამტეხლო 0.5%-ის ოდენობით ყოველ ვადაგადაცილებულ დღეზე.", "day"),
        ("პირგასამტეხლო 0.5%-ის ოდენობით ყოველ ვადაგადაცილებულ კვირაზე.", "week"),
        ("პირგასამტეხლო 0.5%-ის ოდენობით ყოველი თვის დაგვიანებისთვის.", "month"),
        ("a penalty of 0.2% per calendar day of delay.", "day"),
        ("a penalty of 0.2% for each week of delay.", "week"),
        ("For each day of delay the Contractor pays a penalty of 0.2% of the price.", "day"),
        ("For every month of delay the Contractor pays 1% of the price.", "month"),
        ("პირგასამტეხლო 0.5%-ის ოდენობით.", None),
        ("If the works are late by more than a month, a penalty of 5% applies.", None),
        # a period in the next sentence is not this rate's
        ("პირგასამტეხლო 0.5%. ყოველ დღეზე ანგარიში იგზავნება.", None),
    ],
)
def test_a_penalty_period_is_read_after_the_rate_or_before_it(
    text: str, period: str | None
) -> None:
    hit = find_percentages(text)[0]
    assert penalty_period(text, hit.start, hit.end) == period


@pytest.mark.parametrize(
    ("clause", "penalty"),
    [
        ("ყოველ ვადაგადაცილებულ დღეზე", "0.5% per day"),
        ("ყოველ ვადაგადაცილებულ კვირაზე", "0.5% per week"),
    ],
)
def test_a_penalty_counted_per_period_keeps_its_period(clause: str, penalty: str) -> None:
    facts = _extract(
        "5.1. ვადის გადაცილების შემთხვევაში „შემსრულებელს“ ეკისრება პირგასამტეხლო "
        f"ხელშეკრულების ღირებულების 0.5%-ის ოდენობით {clause}."
    )
    assert facts[FactType.penalty] == [penalty]


def test_a_penalty_rate_in_words_is_a_penalty() -> None:
    facts = _extract(
        "5.1. „შემსრულებელს“ ეკისრება პირგასამტეხლო ღირებულების 0.3 პროცენტის ოდენობით "
        "ყოველ ვადაგადაცილებულ დღეზე."
    )
    assert facts[FactType.penalty] == ["0.3% per day"]


# --- VAT (EXTRACTION-13) --------------------------------------------------------------------------


def test_a_vat_rate_is_never_a_vat_status_fact() -> None:
    facts = _extract("ღირებულება შეადგენს 900 ლარს, დღგ 18%.")
    assert "18.0" not in facts.get(FactType.vat_status, [])
    assert facts[FactType.percentage] == ["18.0"]


@pytest.mark.parametrize(
    ("sentence", "status"),
    [
        ("ღირებულება შეადგენს 900 (ცხრაასი) ლარს და შეიცავს დღგ-ს.", "incl"),
        ("ღირებულება შეადგენს 900 (ცხრაასი) ლარს. ღირებულებაში შედის დღგ.", "incl"),
        ("„მომსახურების“ დღგ-იანი ღირებულება შეადგენს 900 (ცხრაასი) ლარს.", "incl"),
        ("ღირებულება შეადგენს 900 (ცხრაასი) ლარს + 18% დღგ.", "excl"),
        ("ღირებულება შეადგენს 900 (ცხრაასი) ლარს და არ შეიცავს დღგ-ს.", "excl"),
        ("The price is 900.00 GEL. The price includes VAT.", "incl"),
        ("The price is 900.00 GEL, VAT inclusive.", "incl"),
        ("The price is 900.00 GEL. The price excludes VAT.", "excl"),
        ("The price is 900.00 GEL, VAT exclusive.", "excl"),
    ],
)
def test_vat_wordings(sentence: str, status: str) -> None:
    assert _extract(f"4.1. {sentence}")[FactType.vat_status] == [status]


# --- line totals in a price table (EXTRACTION-11) -------------------------------------------------

_PRICE_TABLE = (
    "4.1. ღირებულება შეადგენს 3 900 (სამი ათას ცხრაასი) ლარს და განისაზღვრება ცხრილით:\n"
    "1, დასახელება = კედლის შეღებვა. 1, ერთეული = მ². 1, რაოდენობა = 100. "
    "1, ერთეულის ფასი = 30.00 ლარი. 1, ჯამი = 3 000.00 ლარი. "
    "2, დასახელება = ფანჯრის მონტაჟი. 2, ერთეული = ცალი. 2, რაოდენობა = 3. "
    "2, ერთეულის ფასი = 300.00 ლარი. 2, ჯამი = 900.00 ლარი. "
    ", დასახელება = სულ. , ერთეული = . , რაოდენობა = . , ერთეულის ფასი = . "
    ", ჯამი = 3 900.00 ლარი"
)


def test_a_line_total_is_not_the_documents_total() -> None:
    by_value = {v: t for v, _, t in _amounts(_PRICE_TABLE)}
    assert by_value[3000.0] == FactType.amount
    assert by_value[900.0] == FactType.amount
    assert by_value[30.0] == FactType.price
    assert by_value[300.0] == FactType.price
    assert by_value[3900.0] == FactType.total_amount


def test_a_price_table_leaves_one_total_and_no_conflict() -> None:
    facts = _extract(_PRICE_TABLE)
    assert facts[FactType.total_amount] == ["3900.0"]


def test_a_key_value_table_row_named_by_a_total_word_is_a_total() -> None:
    text = "ხელშეკრულების ღირებულება, მნიშვნელობა = 5 200 (ხუთი ათას ორასი) ლარი"
    assert [t for _, _, t in _amounts(text)] == [FactType.total_amount]


# --- a reference number with spaced separators (EXTRACTION-20) ------------------------------------


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("ხელშეკრულება N 27 / 2026", "27/2026"),
        ("ხელშეკრულება N PR-2026 / 017", "PR-2026/017"),
        ("ხელშეკრულება N 27/2026", "27/2026"),
        # a bare space between two groups is where the reference ends
        ("ხელშეკრულება N 27 2026 წლის", "27"),
    ],
)
def test_a_reference_number_with_spaces_around_its_separators(text: str, number: str) -> None:
    hits = find_contract_numbers(text)
    assert [h.normalized for h in hits] == [number]


def test_a_spaced_reference_from_another_source_keys_the_same() -> None:
    assert normalize_fact_value(FactType.contract_number, "N 27 / 2026") == "27/2026"


# --- deadlines stated with a verb (EXTRACTION-17) -------------------------------------------------


def test_a_performance_length_stated_with_a_verb_is_the_execution_period() -> None:
    facts = _extract(
        "2.1. „შემსრულებელი“ ვალდებულია შეასრულოს „მომსახურება“ ხელშეკრულების "
        "გაფორმებიდან 60 (სამოცი) კალენდარული დღის ვადაში."
    )
    assert facts[FactType.execution_period] == ["P60D"]


def test_a_delivery_date_stated_with_a_verb_is_the_delivery_deadline() -> None:
    facts = _extract(
        "ქ. თბილისი, 2026 წლის 4 მარტი\n"
        "2.1. „მიმწოდებელი“ ვალდებულია მიაწოდოს „საქონელი“ 2026 წლის 20 აპრილამდე."
    )
    assert facts[FactType.delivery_deadline] == ["2026-04-20"]


# --- a subject clause without a defined term (EXTRACTION-16) --------------------------------------


@pytest.mark.parametrize(
    "clause",
    [
        "1.1. ხელშეკრულების საგანია სახურავის შეკეთების სამუშაოების შესრულება.\n"
        "1.2. სხვა პირობები.",
        "1.1. ხელშეკრულების საგანს წარმოადგენს სახურავის შეკეთების სამუშაოების შესრულება "
        "„შემკვეთის“ ოფისში.\n1.2. სხვა პირობები.",
    ],
)
def test_a_subject_clause_ends_with_its_sentence(clause: str) -> None:
    subject = _extract(clause)[FactType.subject]
    assert len(subject) == 1
    assert subject[0].startswith("სახურავის შეკეთების სამუშაოების შესრულება")
    assert "სხვა პირობები" not in subject[0]


# --- project names already in the nominative (EXTRACTION-09, EXTRACTION-18) -----------------------


@pytest.mark.parametrize(
    "name",
    ["მთის პრემიუმ", "ზღვის ფორუმ", "ოუკვუდ", "ტყის ბონუს", "ზღვის ექსპრეს", "მთის პანორამა"],
)
def test_a_project_name_in_apposition_is_kept_as_quoted(name: str) -> None:
    facts = _extract(f"1.1. მომსახურება ხორციელდება პროექტის „{name}“ ფარგლებში.")
    assert facts[FactType.project_name] == [name]


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("მომსახურება პროექტ „მზის ქალაქისთვის“.", "მზის ქალაქი"),
        ("„მზის ქალაქის“ პროექტის ფარგლებში.", "მზის ქალაქი"),
        ("პროექტის „მზის ქალაქის“ ფარგლებში.", "მზის ქალაქი"),
        ("„ზღვის ექსპრეს“ პროექტის ფარგლებში.", "ზღვის ექსპრეს"),
        # a postposition on the quoted attribute is always an oblique case
        ("„მზის ქალაქისთვის“ პროექტის ფარგლებში.", "მზის ქალაქი"),
    ],
)
def test_a_project_name_in_an_oblique_case_is_given_in_the_nominative(text: str, name: str) -> None:
    assert _extract(f"1.1. {text}")[FactType.project_name] == [name]
