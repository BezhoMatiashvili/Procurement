"""Extraction defects the fifth red-team round found and a verifier reproduced: a price table's
closing rows, the VAT status in other word orders and its attachment, a labelled contract number,
a payment deadline's day kind, the contract's sum in other wordings, a fixed fine beside a rate,
the payable remainder of an act, a subject that opens with a figure, and the tax listed among a
price's parts.

Each test names its defect (``samples/redteam/extraction/CONFIRMED_R5.md``); every widened
reading has a near miss beside it. Every document here is invented: names, codes and figures are
made up.
"""

from __future__ import annotations

from procurement_agent.config import Settings
from procurement_agent.extraction.pipeline import run_extraction
from procurement_agent.extraction.rules import (
    find_amounts,
    find_contract_numbers,
    find_header_amounts,
)
from procurement_agent.extraction.schemas import FactType, Provenance
from procurement_agent.ingestion.chunker import Chunk

SETTINGS = Settings(ollama_base_url="http://localhost:11434")

HEAD = (
    "ხელშეკრულება N 52/2026\nქ. ოზურგეთი, 2026 წლის 9 მარტი\n"
    "შპს „ნისლიანი ხეობა“ (შემდგომში „შემსყიდველი“) და შპს „ქვის ხიდი“ (შემდგომში "
    "„მიმწოდებელი“) ვაფორმებთ ხელშეკრულებას:\n"
)
ACT_HEAD = "მიღება-ჩაბარების აქტი\nქ. ოზურგეთი 2026 წლის 6 აპრილი\n"


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


def _money(text: str) -> list[tuple[str, float]]:
    """Every money figure the rules read, a price table's cells under a currency header too."""
    return [
        (h.type.value, float(h.normalized))  # type: ignore[arg-type]
        for h in [*find_amounts(text), *find_header_amounts(text)]
        if h.currency
    ]


# --- EXTRACTION-R5-04: a price table closed by net / VAT / total rows ----------------------------

#: A specification table as Docling writes it: rows of "<key>, <column> = <cell>", the closing
#: rows with no key at all.
_ROWS = (
    "1, დასახელება = ხის თარო. 1, ერთეული = ცალი. 1, რაოდენობა = 10. 1, ერთეულის ფასი (ლარი) = "
    "300.00. 1, ჯამი (ლარი) = 3 000.00. 2, დასახელება = ფარდა. 2, ერთეული = ცალი. 2, რაოდენობა = "
    "20. 2, ერთეულის ფასი (ლარი) = 154.00. 2, ჯამი (ლარი) = 3 080.00. "
)


def _closing(label: str, figure: str) -> str:
    return (
        f", დასახელება = {label}. , ერთეული = . , რაოდენობა = . , ერთეულის ფასი (ლარი) = . "
        f", ჯამი (ლარი) = {figure}. "
    )


def _table(*closing: tuple[str, str]) -> str:
    return (_ROWS + "".join(_closing(label, figure) for label, figure in closing)).rstrip(". ")


class TestClosingRowsOfAPriceTable:
    def test_net_vat_total_rows_give_one_total(self) -> None:
        table = _table(
            ("ჯამი დღგ-ს გარეშე", "5 152.54"), ("დღგ (18%)", "927.46"), ("სულ ჯამი", "6 080.00")
        )
        assert ("total_amount", 6080.0) in _money(table)
        assert ("amount", 5152.54) in _money(table)
        assert ("amount", 927.46) in _money(table)
        text = HEAD + "1.2. სპეციფიკაცია:\n" + table + "\n4.1. ღირებულება 6 080 ლარი დღგ-ს ჩათვლით."
        assert _extract(text)[FactType.total_amount] == ["6080.0"]
        assert _conflicts(text) == []

    def test_sum_vat_sum_with_vat_rows_give_one_total(self) -> None:
        table = _table(("სულ", "5 152.54"), ("დღგ", "927.46"), ("სულ დღგ-ს ჩათვლით", "6 080.00"))
        found = _money(table)
        assert [v for t, v in found if t == "total_amount"] == [6080.0]
        assert ("amount", 5152.54) in found and ("amount", 927.46) in found

    def test_near_miss_a_single_net_row_stays_the_total(self) -> None:
        # the only closing row names the net sum: in an excl contract it is the total
        found = _money(_table(("ჯამი დღგ-ს გარეშე", "6 080.00")))
        assert [v for t, v in found if t == "total_amount"] == [6080.0]

    def test_near_miss_a_single_total_row_and_its_lines(self) -> None:
        found = _money(_table(("სულ", "6 080.00")))
        assert [v for t, v in found if t == "total_amount"] == [6080.0]
        assert ("amount", 3000.0) in found and ("amount", 3080.0) in found


# --- EXTRACTION-R5-05: a VAT non-payer in other word orders ---------------------------------------


class TestVatNonPayerWordOrders:
    PRICE = "4.1. ღირებულება შეადგენს 2 500 ლარს. "

    def test_every_word_order_is_none(self) -> None:
        for said in (
            "„მიმწოდებელი“ დღგ-ს გადამხდელი არ არის.",
            "„მიმწოდებელი“ არ არის რეგისტრირებული დღგ-ს გადამხდელად.",
            "„მიმწოდებელი“ დღგ-ის გადამხდელად რეგისტრირებული არ არის.",
            "„მიმწოდებელი“ არ არის დღგ-ს გადამხდელი.",
        ):
            assert _extract(HEAD + self.PRICE + said)[FactType.vat_status] == ["none"], said

    def test_near_miss_a_payer_and_a_label_say_nothing(self) -> None:
        for said in (
            "„მიმწოდებელი“ არის დღგ-ს გადამხდელი.",
            "„მიმწოდებელი“ რეგისტრირებულია დღგ-ს გადამხდელად.",
            "დღგ-ს გადამხდელის ნომერი: 404551122.",
        ):
            assert FactType.vat_status not in _extract(HEAD + self.PRICE + said), said

    def test_near_miss_a_negation_of_another_verb_across_a_comma(self) -> None:
        said = "„მიმწოდებელი“ დღგ-ს გადამხდელია, ვალდებულება არ არის შესრულებული."
        assert FactType.vat_status not in _extract(HEAD + self.PRICE + said)


# --- EXTRACTION-R5-07: a contract number behind a label -------------------------------------------


class TestContractNumberBehindALabel:
    def test_label_forms_are_read(self) -> None:
        for line in (
            "ხელშეკრულების №: 52/2026",
            "ხელშეკრულების ნომერი: 52/2026",
            "ხელშეკრულების ნომერი: 52/2026;",
            "Contract No.: 52/2026",
            "Contract number: 52/2026",
        ):
            assert [h.value for h in find_contract_numbers(line)] == ["52/2026"], line

    def test_the_label_line_in_a_document(self) -> None:
        text = "ხელშეკრულება\nხელშეკრულების №: 52/2026\n1.1. საგანია თაროების შესყიდვა."
        assert _extract(text)[FactType.contract_number] == ["52/2026"]

    def test_near_miss_another_document_s_number_behind_a_label(self) -> None:
        # whether another document's number is a contract number is undecided (R3-16): the label
        # form is read after the contract's own word only
        assert find_contract_numbers("აქტის №: 12/2026") == []
        assert find_contract_numbers("ინვოისის ნომერი: 115") == []
        assert find_contract_numbers("ხელშეკრულების ნომერი: იხ. დანართი") == []


# --- EXTRACTION-R5-08: a payment deadline keeps its day kind --------------------------------------


class TestPaymentDeadlineKeepsItsDays:
    def test_label_line_with_the_count_in_the_nominative(self) -> None:
        text = HEAD + (
            "4.2. ანგარიშსწორების ვადა: მიღება-ჩაბარების აქტის გაფორმებიდან 7 (შვიდი) სამუშაო დღე."
        )
        assert _extract(text)[FactType.payment_terms] == ["P7D working"]

    def test_key_terms_line(self) -> None:
        text = HEAD + "ანგარიშსწორების ვადა: აქტის გაფორმებიდან 7 კალენდარული დღე;\n"
        assert _extract(text)[FactType.payment_terms] == ["P7D calendar"]

    def test_near_miss_no_day_kind_stays_bare(self) -> None:
        text = HEAD + "4.2. ანგარიშსწორების ვადა: აქტის გაფორმებიდან 7 დღე."
        assert _extract(text)[FactType.payment_terms] == ["P7D"]


# --- EXTRACTION-R5-09: wording about another figure is not the price's VAT status ----------------


class TestVatStatusIsThePrices:
    PRICE = "4.1. ხელშეკრულების ღირებულება შეადგენს 2 360 ლარს დღგ-ს ჩათვლით. "

    def test_an_advance_s_basis_in_the_next_clause(self) -> None:
        text = HEAD + self.PRICE + "4.2. ავანსი დღგ-ს გარეშე შეადგენს 600 ლარს."
        assert _extract(text)[FactType.vat_status] == ["incl"]

    def test_unit_prices_in_an_annex(self) -> None:
        text = HEAD + self.PRICE + "4.2. ერთეულის ფასები დღგ-ს გარეშე მოცემულია დანართ N2-ში."
        assert _extract(text)[FactType.vat_status] == ["incl"]

    def test_near_miss_the_next_sentence_decides_when_the_price_s_says_nothing(self) -> None:
        text = HEAD + "4.1. ღირებულება შეადგენს 2 000 ლარს. 4.2. ფასს დაემატება დღგ."
        assert _extract(text)[FactType.vat_status] == ["excl"]


# --- EXTRACTION-R5-10: the contract's sum in other wordings ---------------------------------------


class TestContractSumWordings:
    def test_wordings_are_the_total(self) -> None:
        for clause in (
            "4.1. ხელშეკრულების საერთო თანხა შეადგენს 2 360 ლარს დღგ-ს ჩათვლით.",
            "4.1. ხელშეკრულების თანხა შეადგენს 2 360 ლარს დღგ-ს ჩათვლით.",
            "4.1. ხელშეკრულების ღირებულება შეფასებულია 2 360 ლარად დღგ-ს ჩათვლით.",
        ):
            found = _extract(HEAD + clause)
            assert found[FactType.total_amount] == ["2360.0"], clause
            assert FactType.price not in found, clause

    def test_near_miss_an_advance_s_sum_and_a_unit_price(self) -> None:
        assert _money("ავანსის თანხა შეადგენს 600 ლარს.") == [("amount", 600.0)]
        assert _money("ერთეულის ფასი შეადგენს 45 ლარს.") == [("price", 45.0)]
        assert _money("ფასი: 45 ლარი") == [("price", 45.0)]
        # the base of a share is no label of the figure
        assert _money("ხელშეკრულების თანხის 10%, ანუ 236 ლარი.") == [("amount", 236.0)]


# --- EXTRACTION-R5-11: a fixed fine beside a daily rate -------------------------------------------


class TestFixedFineBesideARate:
    RATE = (
        "5.1. ვადის გადაცილებისას „მიმწოდებელს“ ეკისრება პირგასამტეხლო ღირებულების 0.2%-ის "
        "ოდენობით ყოველ ვადაგადაცილებულ დღეზე. "
    )

    def test_the_fine_survives_beside_the_rate(self) -> None:
        text = (
            HEAD
            + self.RATE
            + "5.2. უხარისხო მიწოდებისას დაეკისრება ჯარიმა 700 (შვიდასი) ლარის ოდენობით."
        )
        assert sorted(_extract(text)[FactType.penalty]) == ["0.2% per day", "700.0"]

    def test_near_miss_a_money_cap_is_no_second_penalty(self) -> None:
        text = HEAD + (
            "5.1. ვადის გადაცილებისას ეკისრება პირგასამტეხლო ღირებულების 0.2% ყოველ დღეზე, მაგრამ "
            "არაუმეტეს 1 000 ლარისა."
        )
        assert _extract(text)[FactType.penalty] == ["0.2% per day"]

    def test_near_miss_a_rate_restated_as_money_is_no_second_penalty(self) -> None:
        text = HEAD + "5.1. ეკისრება პირგასამტეხლო ღირებულების 5%-ის, 118 ლარის ოდენობით."
        assert _extract(text)[FactType.penalty] == ["5%"]


# --- EXTRACTION-R5-14: an act's payable remainder after ", ხოლო" ---------------------------------


class TestPayableRemainderPastATurn:
    def test_the_remainder_is_an_amount(self) -> None:
        text = ACT_HEAD + (
            "3. შესრულებული სამუშაოს ღირებულება შეადგენს 4 000 ლარს დღგ-ს ჩათვლით, საიდანაც "
            "დაქვითული ავანსი შეადგენს 1 200 ლარს, ხოლო ასანაზღაურებელი თანხა - 2 800 ლარს."
        )
        assert _extract(text)[FactType.total_amount] == ["4000.0"]
        assert _conflicts(text) == []

    def test_near_miss_no_turn_keeps_the_price_whole(self) -> None:
        text = "ღირებულება, ავანსის ჩათვლით, შეადგენს 4 000 ლარს."
        assert _money(text) == [("total_amount", 4000.0)]


# --- EXTRACTION-R5-15: a subject that opens with a figure keeps it --------------------------------


class TestSubjectKeepsItsFigure:
    def test_a_quantity_and_a_year_stay(self) -> None:
        for subject in (
            "15 ცალი ხის თაროს და 30 ცალი ფარდის შესყიდვა",
            "2026 წლის გაზაფხულის სეზონისთვის საწვავის შესყიდვა",
        ):
            text = HEAD + f"1.1. ხელშეკრულების საგანია {subject}."
            assert _extract(text)[FactType.subject] == [subject]

    def test_near_miss_the_clause_number_still_goes(self) -> None:
        text = HEAD + "1. ხელშეკრულების საგანი\n1.1. ფარდების შესყიდვა და მონტაჟი."
        assert _extract(text)[FactType.subject] == ["ფარდების შესყიდვა და მონტაჟი"]


# --- EXTRACTION-R5-17: the tax listed among the price's parts -------------------------------------


class TestTaxListedAmongTheParts:
    def test_net_then_vat_in_the_list_is_incl(self) -> None:
        text = HEAD + (
            "4.1. ღირებულება შეადგენს 1 180 ლარს, მათ შორის: დღგ-ს გარეშე - 1 000 ლარი, "
            "დღგ - 180 ლარი."
        )
        found = _extract(text)
        assert found[FactType.vat_status] == ["incl"]
        assert found[FactType.total_amount] == ["1180.0"]

    def test_near_miss_the_tax_added_is_excl(self) -> None:
        text = HEAD + "4.1. ღირებულება შეადგენს 1 000 ლარს, რასაც დაემატება დღგ - 180 ლარი."
        assert _extract(text)[FactType.vat_status] == ["excl"]
