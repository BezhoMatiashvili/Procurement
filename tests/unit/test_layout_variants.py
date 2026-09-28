"""Acts and contracts worded or laid out otherwise than the client's template: a period in
figures, an individual entrepreneur or a natural person as a party, a project named after a
dash, "რომლის საგანია", a signatory by power of attorney. Each once went unread. Synthetic text
only."""

from __future__ import annotations

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.aoa_scope import subject_clause
from procurement_agent.extraction.anchors import find_anchors, find_parties
from procurement_agent.extraction.schemas import FactType
from tests.unit.test_aoa_check import ACT, CONTRACT

_PERIOD_IN_WORDS = "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით."
_PERIOD_IN_FIGURES = "„მომსახურება“ გაწეულია 01.04.2026 - 30.04.2026 პერიოდში."


def _errors(act: str, contract: str = CONTRACT) -> list[str]:
    return [f.rule_id for f in check_aoa(aoa_text=act, contract_text=contract).errors]


# --- a period written in figures -----------------------------------------------------------------


def _in_figures(period: str) -> str:
    """The period in figures under a "reporting period" heading, with no "შესრულება" word."""
    return ACT.replace(_PERIOD_IN_WORDS, period).replace(
        "2. შესრულების თარიღი", "2. საანგარიშო პერიოდი"
    )


def test_a_period_in_figures_after_the_services_rendered_is_the_period() -> None:
    act = _in_figures(_PERIOD_IN_FIGURES)
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary["execution_period"] == "2026-04-01..2026-04-30"
    assert report.errors == []


def test_a_period_in_figures_out_of_the_term_is_reported() -> None:
    act = _in_figures(_PERIOD_IN_FIGURES.replace("2026", "2025"))
    assert "AOA.PERIOD_IN_TERM" in _errors(act)


# --- an individual entrepreneur or a natural person as a party ----------------------------------

_IE = "ი/მ გიორგი ნიკოლაძე (დღგ-ს გადამხდელი), საიდენტიფიკაციო კოდი: 01001001001"


def _with_ie(text: str) -> str:
    return text.replace("შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321", _IE).replace(
        "შპს „ბეტა სერვისი“", "ი/მ გიორგი ნიკოლაძე"
    )


def test_an_individual_entrepreneur_is_a_party() -> None:
    [_, ie] = find_parties(_with_ie(ACT))
    assert (ie.name, ie.id_code) == ("ი/მ გიორგი ნიკოლაძე", "01001001001")


def test_a_natural_person_is_a_party() -> None:
    text = "ფ/პ ანა ბერიძე, პირადი ნომერი: 01002002002, მისამართი: ქ. თბილისი, ვაჟა-ფშაველას 5"
    [person] = find_parties(text)
    assert (person.name, person.id_code) == ("ფ/პ ანა ბერიძე", "01002002002")


def test_an_individual_entrepreneurs_changed_code_is_reported() -> None:
    contract, act = _with_ie(CONTRACT), _with_ie(ACT)
    assert _errors(act, contract) == []
    assert "AOA.ID_CODES" in _errors(act.replace("01001001001", "01001001010"), contract)


# --- a project named after a dash ----------------------------------------------------------------


def test_a_project_named_after_a_dash() -> None:
    text = "რომლის საგანია „შემკვეთის“ პროექტის - „ზღვის რეზიდენსი“ (ს/კ: 01.02.03.456) ფარგლებში"
    names = [h.value for h in find_anchors(text) if h.type == FactType.project_name]
    assert names == ["ზღვის რეზიდენსი"]


# --- the subject introduced by "რომლის საგანია" --------------------------------------------------


def test_whose_subject_is_opens_the_subject() -> None:
    text = (
        "ვინაიდან მხარეებმა გააფორმეს ხელშეკრულება, რომლის საგანია „შემკვეთის“ პროექტის - "
        "„ზღვის რეზიდენსი“ (ს/კ: 01.02.03.456) ფარგლებში ვებგვერდის მხარდაჭერის მომსახურების "
        "გაწევა (შემდგომში „მომსახურება“), მხარეები აფორმებენ აქტს"
    )
    part = subject_clause(text)
    assert part is not None
    assert "ვებგვერდის მხარდაჭერის მომსახურების გაწევა" in part.text


# --- a signatory by power of attorney -----------------------------------------------------------

_PROXY = ACT.replace(
    "წარმოდგენილი დირექტორის ნინო კაპანაძის სახით",
    "წარმოდგენილი მინდობილობით მოქმედი პირის, ლევან გელაშვილის სახით",
).replace("დირექტორი ნინო კაპანაძე", "მინდობილობით: ლევან გელაშვილი")


def test_a_signatory_by_power_of_attorney_is_read() -> None:
    [client, _] = find_parties(_PROXY)
    assert client.proxies == ["ლევან გელაშვილის"]


def test_a_signatory_by_power_of_attorney_is_a_warning_to_check_the_authority() -> None:
    report = check_aoa(aoa_text=_PROXY, contract_text=CONTRACT)
    found = [
        (f.severity, f.actual, f.expected) for f in report.findings if f.rule_id == "AOA.DIRECTOR"
    ]
    assert found == [
        ("warning", "ლევან გელაშვილის", None),
        ("warning", "ლევან გელაშვილი", None),
    ]
    assert report.errors == []


def test_an_act_dated_the_day_its_period_ends_keeps_the_period_whole() -> None:
    """The act's date, repeated under the period's heading, is the period's end date too: set
    aside as the act's, it must not take the period's end with it."""
    act = _in_figures(
        "„აქტი“ გაფორმდა 30.04.2026. „მომსახურება“ გაწეულია 01.04.2026 - 30.04.2026 პერიოდში."
    )
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary["execution_period"] == "2026-04-01..2026-04-30"
    assert report.errors == []


def test_the_acts_date_repeated_beside_every_row_is_never_the_periods() -> None:
    """A table whose first row the parser took for its header: its "„აქტი“ გაფორმდა <date>"
    is repeated beside every row, the period's row among them."""
    header = "„აქტი“ გაფორმდა 30.04.2026."
    act = _in_figures(
        f"2, {header} = „მომსახურება“ გაწეულია 01.04.2025 - 30.04.2025 პერიოდში.. "
        f"3, {header} = „შემსრულებელმა“ გასწია მომსახურება.."
    )
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary["execution_period"] == "2025-04-01..2025-04-30"
    assert "AOA.PERIOD_IN_TERM" in [f.rule_id for f in report.errors]


# --- a contract's performance window and validity, worded otherwise -------------------------------


def test_the_services_to_be_rendered_between_two_dates_is_the_performance_window() -> None:
    from datetime import date

    from procurement_agent.checks.contract_terms import read_term

    for clause, window in (
        (
            "2.1. შემსრულებელი ვალდებულია გასწიოს მომსახურება 2026 წლის 1 ოქტომბრიდან "
            "2027 წლის 31 მარტის ჩათვლით (6 თვე).",
            (date(2026, 10, 1), date(2027, 3, 31)),
        ),
        (
            "2.1. „მომსახურება“ გაიწევა 01.09.2026 - 31.12.2026 (4 თვე).",
            (date(2026, 9, 1), date(2026, 12, 31)),
        ),
    ):
        term = read_term(clause)
        assert term.performance is not None
        assert (term.performance.start, term.performance.end) == window


def test_in_force_upon_signing_runs_from_the_signing() -> None:
    from datetime import date

    from procurement_agent.checks.contract_terms import read_term

    text = (
        "ხელშეკრულება\nქ. თბილისი 01.09.2026\n"
        "შპს „ალფა ჯგუფი“, საიდენტიფიკაციო კოდი: 123456789 (შემდგომში „შემკვეთი“)\n"
        "4.1. ხელშეკრულება ძალაში შედის ხელმოწერისთანავე და შედგენილია ორ ეგზემპლარად."
    )
    term = read_term(text)
    assert term.validity is not None
    assert (term.validity.start, term.validity.end) == (date(2026, 9, 1), None)
