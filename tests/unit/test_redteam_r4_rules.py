"""Defects the fourth red-team round found in the readers the comparison and the act check share
with the extraction (``extraction/rules.py``): a term's day count in its adjective and case
forms, a rate with its words before the % sign, a contract number in lower case, and a payment
deadline written in words only or abbreviated.

Each test names its defect (``samples/redteam/<module>/CONFIRMED_R4.md``); every widened reading
has a near miss beside it. Every document here is invented.
"""

from __future__ import annotations

from procurement_agent.checks.compare import compare, hits_of
from procurement_agent.extraction.rules import (
    find_contract_numbers,
    find_durations,
    find_percentages,
    normalize_fact_value,
)
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion.segmenter import Segment, build
from tests.unit.test_aoa_contract_rules import _ACT_TERMS, _act, _check, _only, _skip_reasons


def segs(texts: list[str]) -> list[Segment]:
    return build([(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(texts)])


def run(left: list[str], right: list[str]):  # type: ignore[no-untyped-def]
    return compare(
        segs(left),
        segs(right),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )


def substantive(report) -> list[tuple[str, str | None, str | None, str]]:  # type: ignore[no-untyped-def]
    return [
        (f.rule_id, f.expected, f.actual, f.severity)
        for f in report.findings
        if f.severity != "info" and f.rule_id != "CMP.FORMATTING"
    ]


def durations(text: str) -> list[tuple[str, str]]:
    return [(h.type.value, str(h.normalized)) for h in find_durations(text)]


def values(text: str) -> list[str]:
    return [str(h.value) for h in find_durations(text)]


HEAD = ["მიწოდების ხელშეკრულება № TK-2026/77", "ქ. ფოთი, 2026 წლის 14 მაისი"]


def doc(*clauses: str) -> list[str]:
    return [*HEAD, *clauses]


# --- COMPARE-R4-02: a day count in its adjective and case forms ---------------------------------


class TestDayCountForms:
    def test_the_hyphen_adjective_is_the_count(self) -> None:
        assert durations("საქონელი მიწოდებული უნდა იქნეს 25-დღიან ვადაში.") == [
            ("delivery_deadline", "P25D")
        ]
        assert durations("საგარანტიო ვადა: 18-თვიანი გარანტია.") == [("warranty_period", "P18M")]
        assert durations("მომსახურება გაიწევა 2-წლიანი პერიოდით.")[0][1] == "P2Y"

    def test_the_basis_is_read_in_every_case(self) -> None:
        assert values("მიწოდება მოხდება 25 კალენდარულ დღეში.") == ["25 calendar days"]
        assert values("ანგარიშსწორება მოხდება 8 კალენდარულ დღეში.") == ["8 calendar days"]
        assert durations("ანგარიშსწორება მოხდება აქტიდან 8 კალენდარულ დღეში.") == [
            ("payment_terms", "P8D")
        ]
        assert values("გადაიხდება 8 სამუშაო დღეში.") == ["8 working days"]

    def test_near_miss_an_ordinal_or_a_range_is_unchanged(self) -> None:
        assert durations("მიწოდება მოხდება 25-ე დღეს.") == []
        assert durations("მიწოდება მოხდება მე-25 დღეს.") == []
        assert values("მიწოდება მოხდება 10-15 დღეში.") == ["15 days"]
        assert durations("2026-წლის ბიუჯეტით.") == []

    def test_a_restyled_deadline_is_formatting(self) -> None:
        before = doc("1. საქონელი მიწოდებული უნდა იქნეს 25 კალენდარული დღის ვადაში.")
        for after in (
            "1. საქონელი მიწოდებული უნდა იქნეს 25-დღიან ვადაში.",
            "1. საქონელი მიწოდებული უნდა იქნეს 25 კალენდარულ დღეში.",
        ):
            report = run(before, doc(after))
            assert not [f for f in substantive(report) if f[0] == "CMP.DURATION"], after

    def test_a_changed_count_in_the_adjective_is_a_term_change(self) -> None:
        report = run(
            doc("1. საქონელი მიწოდებული უნდა იქნეს 25-დღიან ვადაში."),
            doc("1. საქონელი მიწოდებული უნდა იქნეს 35-დღიან ვადაში."),
        )
        assert ("CMP.DURATION", "P25D", "P35D", "error") in substantive(report)

    def test_a_changed_count_of_calendar_days_is_a_payment_change(self) -> None:
        report = run(
            doc("2. ანგარიშსწორება განხორციელდება აქტის გაფორმებიდან 8 კალენდარულ დღეში."),
            doc("2. ანგარიშსწორება განხორციელდება აქტის გაფორმებიდან 12 კალენდარულ დღეში."),
        )
        assert ("CMP.PAYMENT", "P8D calendar", "P12D calendar", "error") in substantive(report)


# --- AOA-R4-10: a payment deadline in words only, or abbreviated ---------------------------------


class TestDeadlineInWordsOrAbbreviated:
    def test_a_count_in_words_only_is_read(self) -> None:
        assert values("გადაიხდება თხუთმეტი სამუშაო დღის ვადაში.") == ["15 working days"]
        assert values("გადაიხდება ოცდაათი კალენდარული დღის ვადაში.") == ["30 calendar days"]
        assert values("გადაიხდება სამ დღეში.") == ["3 days"]
        [hit] = find_durations("გადაიხდება თხუთმეტი სამუშაო დღის ვადაში.")
        assert hit.raw_text == "თხუთმეტი სამუშაო დღის"

    def test_the_abbreviated_basis_is_read(self) -> None:
        assert values("გადაიხდება 15 სამ. დღის ვადაში.") == ["15 working days"]
        assert values("გადაიხდება ათი სამ. დღის ვადაში.") == ["10 working days"]
        assert values("გადაიხდება 15 კალ. დღის ვადაში.") == ["15 calendar days"]

    def test_a_figure_with_its_words_is_one_count(self) -> None:
        assert values("გადაიხდება 15 (თხუთმეტი) სამუშაო დღის ვადაში.") == ["15 working days"]
        assert values("გადაიხდება თხუთმეტი (15) სამუშაო დღის ვადაში.") == ["15 working days"]

    def test_near_miss_an_ordinal_or_words_that_are_no_number(self) -> None:
        assert durations("გადაიხდება მეორე დღეს.") == []
        assert durations("გადაიხდება ბოლო სამუშაო დღეს.") == []
        assert durations("სამი მხარე ხელს აწერს ხელშეკრულებას.") == []

    def test_the_act_s_same_deadline_in_words_only_is_clean(self) -> None:
        report = _check(_act(terms=_ACT_TERMS.replace("5 (ხუთი)", "ხუთი")))
        assert [f for f in report.findings if f.rule_id.startswith("AOA.PAYMENT")] == []
        assert not _skip_reasons(report, "AOA.PAYMENT_*")

    def test_a_different_deadline_in_words_only_is_an_error(self) -> None:
        report = _check(_act(terms=_ACT_TERMS.replace("5 (ხუთი)", "თხუთმეტი")))
        finding = _only(report, "AOA.PAYMENT_DEADLINE")
        assert (finding.severity, finding.actual) == ("error", "თხუთმეტი სამუშაო დღის")
        assert finding.expected == "5 (ხუთი) სამუშაო დღის"

    def test_a_different_abbreviated_deadline_is_an_error(self) -> None:
        report = _check(_act(terms=_ACT_TERMS.replace("5 (ხუთი) სამუშაო", "15 სამ.")))
        finding = _only(report, "AOA.PAYMENT_DEADLINE")
        assert (finding.severity, finding.actual) == ("error", "15 სამ. დღის")


# --- COMPARE-R4-03: a rate with its words before the % sign --------------------------------------


class TestRateWordsBeforeThePercentSign:
    def test_the_figure_is_the_rate(self) -> None:
        for text, rate in (
            ("გარანტია ღირებულების 7 (შვიდი)%-ის ოდენობით.", "7%"),
            ("არ უნდა აღემატებოდეს ღირებულების 15 (თხუთმეტი) %-ს.", "15%"),
        ):
            assert [h.value for h in find_percentages(text)] == [rate], text
        [penalty] = find_percentages(
            "პირგასამტეხლო ღირებულების 0.3 (ნული მთელი სამი) %-ის ოდენობით ყოველ დღეზე."
        )
        assert (penalty.type, penalty.value) == (FactType.penalty, "0.3% per day")

    def test_near_miss_a_bracket_with_digits_or_a_count_before_another_rate(self) -> None:
        assert [h.value for h in find_percentages("ვადა 5 (ხუთი) დღე, ფასდაკლება 10%.")] == ["10%"]
        # a bracket holding a figure is no figure's words: the 5 before it is no rate
        assert [h.value for h in find_percentages("იხ. პუნქტი 5 (3.1)%.")] == ["3.1%"]
        assert find_percentages("ღირებულება 5 (ხუთი) ლარი, (იხ. დანართი).") == []

    def test_restating_the_rate_with_its_words_is_formatting(self) -> None:
        report = run(
            doc("3. გარანტია შეადგენს ღირებულების 7%-ს."),
            doc("3. გარანტია შეადგენს ღირებულების 7 (შვიდი)%-ს."),
        )
        assert not [f for f in substantive(report) if f[0] == "CMP.PERCENT"]

    def test_a_rate_changed_with_its_words_is_a_rate_change(self) -> None:
        report = run(
            doc("3. გარანტია შეადგენს ღირებულების 7 (შვიდი)%-ს."),
            doc("3. გარანტია შეადგენს ღირებულების 9 (ცხრა)%-ს."),
        )
        assert ("CMP.PERCENT", "7%", "9%", "error") in substantive(report)


# --- COMPARE-R4-13: a contract number in lower case ----------------------------------------------


class TestContractNumberCase:
    def test_latin_letters_are_upper_cased(self) -> None:
        [hit] = find_contract_numbers("ხელშეკრულება № tk-2026/77")
        assert (hit.value, hit.normalized) == ("TK-2026/77", "TK-2026/77")
        assert normalize_fact_value(FactType.contract_number, "№ tk-2026/77") == "TK-2026/77"

    def test_near_miss_georgian_letters_stay_as_written(self) -> None:
        assert [h.normalized for h in find_contract_numbers("ხელშეკრულება N ს-45/2026")] == [
            "ს-45/2026"
        ]
        assert [h.normalized for h in find_contract_numbers("ხელშეკრულება N 12/2026-ა")] == [
            "12/2026-ა"
        ]

    def test_a_number_retyped_in_lower_case_is_no_change(self) -> None:
        report = run(["ხელშეკრულება № TK-2026/77"], ["ხელშეკრულება № tk-2026/77"])
        assert not [f for f in substantive(report) if f[0] == "CMP.CONTRACT_NO"]

    def test_a_real_change_in_lower_case_is_shown_in_the_canonical_form(self) -> None:
        report = run(["ხელშეკრულება № tk-2026/77"], ["ხელშეკრულება № tk-2026/78"])
        assert ("CMP.CONTRACT_NO", "TK-2026/77", "TK-2026/78", "error") in substantive(report)
        assert [h.normalized for h in hits_of("ხელშეკრულება № tk-2026/77")] == ["TK-2026/77"]
