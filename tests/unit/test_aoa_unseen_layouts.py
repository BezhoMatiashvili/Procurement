"""Layouts the rules had not seen, found by the blind Test Set 4: an act dated in its heading
line over a requisites table, and a table of labels and their values. Invented text only - not
the test set's own.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.aoa_scope import service_items
from procurement_agent.checks.findings import CheckReport

_TABLE = (
    "ხელშეკრულება: 2026 წლის 2 თებერვლის N 7/2026 ხელშეკრულება;\n"
    "დამკვეთი: შპს „ზღვის ბრიზი“;\n"
    "დამკვეთის საიდენტიფიკაციო კოდი: 404555121;\n"
    "შემსრულებელი: შპს „ქარის წისქვილი“;\n"
    "შემსრულებლის საიდენტიფიკაციო კოდი: 405666232;\n"
    "პროექტის დასახელება: „ნავსადგურის ხედი“;\n"
    "საკადასტრო კოდი: 05.21.07.144;\n"
)
_BODY = (
    "სამუშაოები შესრულდა 2026 წლის 12 მარტს.\n"
    "შესრულებული სამუშაოების ღირებულება შეადგენს 4 300 (ოთხი ათას სამასი) ლარს, დღგ-ს "
    "ჩათვლით.\n"
)


def _check(heading: str) -> CheckReport:
    act = f"მიღება-ჩაბარების აქტი\n{heading}\n{_TABLE}{_BODY}"
    return check_aoa(aoa_text=act, contract_text=None)


_CONTRACT = (
    "ხელშეკრულება N 7/2026\nქ. ბათუმი, 2026 წლის 2 თებერვალი\n"
    "შპს „ზღვის ბრიზი“, საიდენტიფიკაციო კოდი: 404555121 (შემდგომში „დამკვეთი“) და\n"
    "შპს „ქარის წისქვილი“, საიდენტიფიკაციო კოდი: 405666232 (შემდგომში „შემსრულებელი“)\n"
    "4.1. ღირებულება შეადგენს 9 500 (ცხრა ათას ხუთასი) აშშ დოლარს.\n"
    "4.2. ავანსი შეადგენს 1 500 (ათას ხუთასი) აშშ დოლარს.\n"
)
_FEE_IN_PARTS = (
    "მიღება-ჩაბარების აქტი\n"
    "შპს „ზღვის ბრიზი“, საიდენტიფიკაციო კოდი: 404555121 (შემდგომში „დამკვეთი“) და\n"
    "შპს „ქარის წისქვილი“, საიდენტიფიკაციო კოდი: 405666232 (შემდგომში „შემსრულებელი“)\n"
    "სამუშაოები შესრულდა 2026 წლის 12 მარტს.\n"
    "შესრულებული სამუშაოების ღირებულება შეადგენს {fee} აშშ დოლარს.\n"
    "გამოქვითული ავანსი: 1 500 (ათას ხუთასი) აშშ დოლარი; გადასახდელი თანხა: {rest} აშშ "
    "დოლარი.\n"
    "აქტი გაფორმდა 2026 წლის 12 მარტს.\n"
)


class TestAFeePaidInParts:
    def test_a_rest_that_makes_up_the_acts_own_fee_is_that_fee_in_parts(self) -> None:
        """The fee is wrong, and the rest after the advance makes it up: one mistake, the fee's -
        the rest corrected to the contract's price would be wrong (Test Set 4)."""
        act = _FEE_IN_PARTS.format(fee="9 800 (ცხრა ათას რვაასი)", rest="8 300 (რვა ათას სამასი)")
        report = check_aoa(aoa_text=act, contract_text=_CONTRACT)
        fees = [f for f in report.findings if f.rule_id == "AOA.FEE"]
        assert [f.actual for f in fees] == ["9,800.00 USD"]

    def test_a_rest_that_makes_up_nothing_is_compared(self) -> None:
        """Near miss: a right fee, and a rest that does not make it up with the advance."""
        act = _FEE_IN_PARTS.format(fee="9 500 (ცხრა ათას ხუთასი)", rest="8 300 (რვა ათას სამასი)")
        report = check_aoa(aoa_text=act, contract_text=_CONTRACT)
        assert [f.rule_id for f in report.findings if f.rule_id == "AOA.FEE"] == ["AOA.FEE"]


class TestAListOfWorks:
    @pytest.mark.parametrize(
        "lead_in",
        [
            "1.4. სამუშაოების ჩამონათვალი:",
            "შესრულებულია შემდეგი სამუშაოები:",
            "შემსრულებელმა გასწია შემდეგი მომსახურება:",
            "სამუშაოების აღწერა:",
        ],
    )
    def test_a_lead_in_naming_works_or_services_opens_the_list(self, lead_in: str) -> None:
        lines = (lead_in, "- 64 ცალი სანათის მონტაჟი", "- ქსელის გამოცდა", "2. ღირებულება")
        assert [i.text for i in service_items(lines)] == [
            "64 ცალი სანათის მონტაჟი",
            "ქსელის გამოცდა",
        ]

    def test_a_list_of_other_things_is_no_list_of_services(self) -> None:
        """Near miss: a list of documents handed over names no work done."""
        lines = ("დოკუმენტების ჩამონათვალი:", "- საველე ანგარიში", "- ფოტოები")
        assert service_items(lines) == []


class TestAnActDatedInItsHeadingLine:
    def test_the_date_that_ends_the_heading_line_dates_the_act(self) -> None:
        """ "ქ. ბათუმი<TAB>date" over a table whose first row is the contract: the contract word
        on the next line is another field's, not this date's."""
        report = _check("ქ. ბათუმი\t2026 წლის 12 მარტი")
        assert report.summary.get("act_date") == "2026-03-12"
        assert [f for f in report.findings if f.rule_id == "AOA.ACT_DATE"] == []

    def test_a_heading_date_over_the_line_that_names_the_contract(self) -> None:
        """A bilingual heading: the date ends its line, and the next one says "Under Contract
        No. …" - that contract word is the next line's, not the date's."""
        heading = (
            "Batumi / ქ. ბათუმი\t12.03.2026\n"
            "Under Contract No. 7/2026 / 2026 წლის 2 თებერვლის N 7/2026 ხელშეკრულების საფუძველზე"
        )
        assert _check(heading).summary.get("act_date") == "2026-03-12"

    def test_a_heading_date_with_the_contract_on_its_line_is_not_taken(self) -> None:
        """Near miss: the same words on the date's own line leave whose date it is unsaid."""
        heading = "Batumi / ქ. ბათუმი\t12.03.2026 Under Contract No. 7/2026"
        assert _check(heading).summary.get("act_date") is None

    def test_a_heading_date_beside_a_contract_word_is_not_taken(self) -> None:
        """Near miss: the contract named on the date's own line - whose date it is is unsaid."""
        report = _check("ქ. ბათუმი\t2026 წლის 12 მარტი, ხელშეკრულება N 7/2026")
        assert report.summary.get("act_date") is None

    def test_a_requisites_table_names_the_parties_project_and_plot(self) -> None:
        report = _check("ქ. ბათუმი\t2026 წლის 12 მარტი")
        rules = {f.rule_id for f in report.findings}
        assert not rules & {"AOA.ID_CODES", "AOA.PROJECT_PRESENT", "AOA.CADASTRAL_PRESENT"}

    def test_a_side_that_labels_a_name_is_its_role(self) -> None:
        """ "დამკვეთი: შპს „…“", a requisites row: the side is the party's role."""
        summary = _check("ქ. ბათუმი\t2026 წლის 12 მარტი").summary
        assert summary.get("party_1", "").startswith("დამკვეთი: შპს ზღვის ბრიზი")
        assert summary.get("party_2", "").startswith("შემსრულებელი: შპს ქარის წისქვილი")

    def test_a_label_of_another_kind_is_no_role(self) -> None:
        """Near miss: a label that names no side ("მიმღები:") gives the name no role."""
        act = (
            "მიღება-ჩაბარების აქტი\nმიმღები: შპს „ზღვის ბრიზი“, საიდენტიფიკაციო კოდი: 404555121;\n"
            f"{_BODY}"
        )
        summary = check_aoa(aoa_text=act, contract_text=None).summary
        assert summary.get("party_1", "").startswith("?:")
