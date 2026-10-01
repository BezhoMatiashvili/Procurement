"""Extraction defects the fourth red-team round found and a verifier reproduced: penalties,
warranties, payment terms, money and label lines. One test or more each, with a near miss beside
every widened reading. Every document here is invented: names, codes and figures are made up."""

from __future__ import annotations

from procurement_agent.checks.document_facts import document_facts
from procurement_agent.config import Settings
from procurement_agent.extraction.pipeline import run_extraction
from procurement_agent.extraction.rules import (
    continues_payment,
    find_amounts,
    find_durations,
    find_percentages,
)
from procurement_agent.extraction.schemas import FactType, Provenance
from procurement_agent.ingestion.chunker import Chunk

SETTINGS = Settings(ollama_base_url="http://localhost:11434")

HEAD = (
    "ხელშეკრულება N 31/2026\nქ. ზესტაფონი, 2026 წლის 4 თებერვალი\n"
    "შპს „ლურჯი ტბა“ (შემდგომში „შემკვეთი“) და შპს „მთის ქარი“ (შემდგომში „შემსრულებელი“) "
    "ვაფორმებთ ხელშეკრულებას:\n"
)
EN_HEAD = "SERVICES AGREEMENT No. 31/2026\nZestaponi, 4 February 2026\n"


def _result(text: str):  # type: ignore[no-untyped-def]
    chunk = Chunk(id="d:0", text=text, body=text, provenance=Provenance(chunk_id="d:0"))
    return run_extraction(document_id="d", chunks=[chunk], settings=SETTINGS, llm=None)


def _extract(text: str) -> dict[FactType, list[str]]:
    out: dict[FactType, list[str]] = {}
    for f in _result(text).facts:
        out.setdefault(f.type, []).append(
            str(f.normalized if f.normalized is not None else f.value)
        )
    return out


def _conflicts(text: str) -> list[str]:
    return [str(f.normalized) for f in _result(text).facts if f.conflict]


def _penalties(text: str) -> list[str]:
    return [h.value for h in find_percentages(text) if h.type is FactType.penalty]


# --- EXTRACTION-R4-09: a penalty's period is not read across ", ხოლო" ----------------------------


class TestPenaltyPeriodAcrossATurn:
    SENTENCE = (
        "5.1. ვადის გადაცილებისთვის „შემსრულებელს“ ეკისრება პირგასამტეხლო ღირებულების "
        "0.3%-ის ოდენობით ყოველ ვადაგადაცილებულ დღეზე, ხოლო ხელშეკრულების "
        "შეუსრულებლობისთვის - ჯარიმა ღირებულების 8%-ის ოდენობით."
    )

    def test_the_fine_after_the_turn_has_no_period(self) -> None:
        assert _penalties(self.SENTENCE) == ["0.3% per day", "8%"]
        assert sorted(_extract(HEAD + self.SENTENCE)[FactType.penalty]) == ["0.3% per day", "8%"]

    def test_near_miss_a_period_in_the_rate_s_own_clause_stays(self) -> None:
        assert _penalties("პირგასამტეხლო შეადგენს 0.3%-ს ყოველ დღეზე.") == ["0.3% per day"]
        assert _penalties(
            "ყოველ ვადაგადაცილებულ დღეზე „შემსრულებელს“ ეკისრება პირგასამტეხლო 0.3%."
        ) == ["0.3% per day"]
        assert _penalties(
            "პირგასამტეხლო 0.3% ყოველ დღეზე, ხოლო ყოველ კვირაზე - პირგასამტეხლო 2%."
        ) == ["0.3% per day", "2% per week"]


# --- EXTRACTION-R4-04: a count framed by the warranty is no warranty ----------------------------


WARRANTY = "6.1. სამუშაოებზე საგარანტიო ვადა შეადგენს 18 (თვრამეტი) თვეს.\n"


class TestWarrantyFrames:
    def test_a_remedy_or_a_refund_counted_in_the_warranty_s_frame_is_no_warranty(self) -> None:
        for clause in (
            "6.2. საგარანტიო ვადის განმავლობაში გამოვლენილ ნაკლს „შემსრულებელი“ აღმოფხვრის "
            "12 (თორმეტი) კალენდარული დღის ვადაში.",
            "6.2. საგარანტიო ვადაში გამოვლენილი ხარვეზი აღმოფხვრილ უნდა იქნეს 12 (თორმეტი) "
            "დღის ვადაში.",
            "6.2. საგარანტიო თანხა „შემსრულებელს“ ჩაერიცხება საგარანტიო ვადის დასრულებიდან "
            "20 (ოცი) სამუშაო დღის განმავლობაში.",
            "6.2. საგარანტიო უზრუნველყოფა „შემსრულებელს“ დაუბრუნდება ვალდებულებების "
            "შესრულებიდან 40 (ორმოცი) დღის ვადაში.",
        ):
            facts = _extract(HEAD + WARRANTY + clause)
            assert facts[FactType.warranty_period] == ["P18M"], clause
            assert _conflicts(HEAD + WARRANTY + clause) == [], clause

    def test_the_english_frame_is_no_warranty(self) -> None:
        text = (
            "6.1. The warranty period is 18 (eighteen) months. "
            "6.2. Any defect found during the warranty period shall be remedied within "
            "12 (twelve) days."
        )
        assert [(h.type, h.normalized) for h in find_durations(text)] == [
            (FactType.warranty_period, "P18M"),
            (FactType.duration, "P12D"),
        ]

    def test_near_miss_the_warranty_s_own_length_is_still_read(self) -> None:
        for text in (
            "საგარანტიო ვადა - 18 თვე.",
            "საგარანტიო ვადა შეადგენს 18 თვეს.",
            "The warranty period of 24 months starts on acceptance.",
            "შემსრულებელი იძლევა 18-თვიან გარანტიას.",
        ):
            [hit] = find_durations(text)
            assert hit.type is FactType.warranty_period, text


# --- EXTRACTION-R4-10: an advance named "prepayment", or after its verb ---------------------------


class TestAdvanceShare:
    def test_a_prepayment_is_the_advance(self) -> None:
        text = EN_HEAD + (
            "4.2. The Customer shall make a prepayment of 25% of the Contract value; the balance "
            "shall be paid within 12 working days after acceptance."
        )
        assert _extract(text)[FactType.payment_terms] == ["P12D working advance 25%"]

    def test_the_advance_named_after_its_verb(self) -> None:
        text = HEAD + (
            "4.2. ღირებულების 25% გადაიხდება ავანსის სახით, ხოლო დარჩენილი თანხა - "
            "მიღება-ჩაბარების აქტის გაფორმებიდან 12 (თორმეტი) სამუშაო დღის ვადაში."
        )
        assert _extract(text)[FactType.payment_terms] == ["P12D working advance 25%"]

    def test_near_miss_a_balance_paid_less_the_advance_is_no_advance(self) -> None:
        [hit] = find_percentages("დარჩენილი 75% გადაიხდება ავანსის გამოქვითვით.")
        assert hit.type is not FactType.payment_terms
        [hit] = find_percentages("ღირებულების 75% გადაიხდება ავანსის გადახდიდან 10 დღეში.")
        assert hit.type is not FactType.payment_terms


# --- EXTRACTION-R4-03: a staged payment written as a list ----------------------------------------


class TestStagedPaymentList:
    def test_letter_items_state_the_balance_s_deadline(self) -> None:
        text = HEAD + (
            "4.2. ანგარიშსწორება განხორციელდება შემდეგნაირად:\n"
            "ა) ავანსი - ღირებულების 25% - ხელშეკრულების გაფორმებიდან 4 (ოთხი) სამუშაო დღის "
            "ვადაში;\n"
            "ბ) დარჩენილი თანხა - მიღება-ჩაბარების აქტის გაფორმებიდან 12 (თორმეტი) სამუშაო დღის "
            "ვადაში.\n"
        )
        assert _extract(text)[FactType.payment_terms] == ["P12D working advance 25%"]

    def test_sub_items_state_the_balance_s_deadline(self) -> None:
        text = HEAD + (
            "4.2. ანგარიშსწორება მოხდება ეტაპობრივად:\n"
            "4.2.1. ავანსი - ხელშეკრულების ღირებულების 25% - ხელშეკრულების გაფორმებიდან 4 "
            "(ოთხი) სამუშაო დღის ვადაში;\n"
            "4.2.2. დარჩენილი 75% - მიღება-ჩაბარების აქტის გაფორმებიდან 12 (თორმეტი) სამუშაო "
            "დღის ვადაში.\n"
        )
        assert _extract(text)[FactType.payment_terms] == ["P12D working advance 25%"]

    def test_near_miss_a_sibling_clause_after_a_payment_clause_is_no_payment(self) -> None:
        text = HEAD + (
            "4.2. ანგარიშსწორება მოხდება აქტის გაფორმებიდან 12 (თორმეტი) სამუშაო დღის ვადაში;\n"
            "4.3. შემსრულებელი ვალდებულია შეასრულოს მომსახურება 30 (ოცდაათი) დღის ვადაში.\n"
        )
        assert _extract(text)[FactType.payment_terms] == ["P12D working"]
        facts = document_facts(text)
        assert [f.normalized for f in facts if f.type is FactType.payment_terms] == [
            "P12D working"
        ]

    def test_near_miss_a_clause_under_a_payment_heading_is_no_stage(self) -> None:
        text = HEAD + (
            "4. ანგარიშსწორების წესი:\n"
            "4.1. შემსრულებელი ვალდებულია მიაწოდოს საქონელი 30 (ოცდაათი) დღის ვადაში.\n"
        )
        facts = document_facts(text)
        assert [f for f in facts if f.type is FactType.payment_terms] == []
        assert not continues_payment("წესი: 4.1. მიწოდება", 5, 6)
        assert continues_payment("ეტაპობრივად: 4.2.1. ავანსი", 12, 13)
        assert continues_payment("ვადაში; ბ) დარჩენილი თანხა", 7, 8)
        assert not continues_payment("ვადაში; 4.3. შემსრულებელი", 7, 8)


# --- EXTRACTION-R4-01: the price restated without VAT, past a turn or in the next sentence -------


def _money(text: str) -> dict[float, FactType]:
    return {float(h.normalized): h.type for h in find_amounts(text)}  # type: ignore[arg-type]


def _vat(text: str) -> list[str]:
    return [str(f.normalized) for f in document_facts(text) if f.type is FactType.vat_status]


class TestNetPartOfThePrice:
    def test_the_net_figure_past_a_turn_is_an_amount(self) -> None:
        for text in (
            "4.1. ღირებულება შეადგენს 47 200 ლარს დღგ-ს ჩათვლით, საიდანაც ღირებულება დღგ-ს "
            "გარეშე შეადგენს 40 000 ლარს.",
            "4.1. ღირებულება შეადგენს 47 200 ლარს დღგ-ს ჩათვლით. ღირებულება დღგ-ს გარეშე "
            "შეადგენს 40 000 ლარს.",
            "4.1. The Contract price is GEL 47,200 including VAT, of which the price excluding "
            "VAT is GEL 40,000.",
        ):
            assert _money(text) == {47200.0: FactType.total_amount, 40000.0: FactType.amount}, text
            assert _vat(text) == ["incl"], text
            facts = _extract(HEAD + text)
            assert facts[FactType.total_amount] == ["47200.0"], text
            assert facts[FactType.vat_status] == ["incl"], text

    def test_the_net_part_after_mat_shoris_is_no_exclusion(self) -> None:
        text = (
            "4.1. ღირებულება შეადგენს 47 200 ლარს, მათ შორის: დღგ-ს გარეშე - 40 000 ლარი, "
            "დღგ - 7 200 ლარი."
        )
        assert _money(text) == {
            47200.0: FactType.total_amount,
            40000.0: FactType.amount,
            7200.0: FactType.amount,
        }
        assert "excl" not in _vat(text)

    def test_near_miss_a_net_price_stated_alone_stays_the_price(self) -> None:
        text = "4.1. ღირებულება დღგ-ს გარეშე შეადგენს 40 000 ლარს."
        assert _money(text) == {40000.0: FactType.total_amount}
        assert _vat(text) == ["excl"]

    def test_near_miss_a_total_past_a_turn_is_no_restatement(self) -> None:
        text = (
            "4.1. ავანსის თანხა შეადგენს 5 000 ლარს, ხოლო საერთო ღირებულება დღგ-ს ჩათვლით "
            "47 200 ლარს."
        )
        assert _money(text)[47200.0] is FactType.total_amount

    def test_near_miss_a_new_clause_is_no_restatement(self) -> None:
        text = (
            "4.1. ღირებულება შეადგენს 47 200 ლარს დღგ-ს ჩათვლით. 4.2. მომსახურების ღირებულება "
            "დღგ-ს გარეშე შეადგენს 40 000 ლარს."
        )
        assert _money(text)[40000.0] is FactType.total_amount


# --- EXTRACTION-R4-05: a net / VAT / total breakdown has one total ------------------------------


class TestBreakdown:
    def test_one_sentence(self) -> None:
        text = "3. ღირებულება შეადგენს 4 000 ლარს, დღგ - 720 ლარი, სულ 4 720 ლარი."
        assert _money(text) == {
            4000.0: FactType.amount,
            720.0: FactType.amount,
            4720.0: FactType.total_amount,
        }

    def test_lines_of_their_own(self) -> None:
        text = (
            "3. შესრულებული სამუშაოს ღირებულება:\n"
            "ღირებულება დღგ-ს გარეშე: 4 000.00 ლარი\n"
            "დღგ (18%): 720.00 ლარი\n"
            "სულ დღგ-ს ჩათვლით: 4 720.00 ლარი\n"
        )
        assert _money(text) == {
            4000.0: FactType.amount,
            720.0: FactType.amount,
            4720.0: FactType.total_amount,
        }
        assert _vat(text) == ["incl"]
        assert _extract(HEAD + text)[FactType.total_amount] == ["4720.0"]
        assert _conflicts(HEAD + text) == []

    def test_the_tax_labelled_figure_is_an_amount(self) -> None:
        assert _money("ღირებულება 4 720 ლარი. დღგ (18%): 720 ლარი.")[720.0] is FactType.amount
        assert _money("Fee: GEL 4,720. VAT: GEL 720.")[720.0] is FactType.amount

    def test_near_miss_a_price_labelled_with_its_tax_basis_is_the_price(self) -> None:
        assert _money("Price including VAT: 118 GEL.") == {118.0: FactType.price}
        assert _money("ღირებულება + დღგ: 1 180 ლარი.") == {1180.0: FactType.total_amount}
        assert _money("ღირებულება შეადგენს 4 000 ლარს, დღგ - 720 ლარი.")[4000.0] is (
            FactType.total_amount
        )
