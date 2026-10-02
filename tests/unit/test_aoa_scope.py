"""The act's subject and services against the contract's (client rules 10.3 and 10.6), rules only.

The pair here is shaped like the client's template: a contract with a subject clause, a list of
obligations and a list of what is to be handed over each month; an act that restates the subject
and lists the services in an annex. It extends the pair in ``test_aoa_contract_rules``, so every
other rule runs on it too. Each test plants one change. Synthetic text only.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.aoa import DocumentView, check_aoa
from procurement_agent.checks.aoa_scope import (
    ScopePart,
    align,
    scope_part,
    service_items,
    subject_clause,
)
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.extraction.normalize import normalize_confusables
from tests.unit.test_aoa_check import ACT as SHORT_ACT
from tests.unit.test_aoa_check import CONTRACT as SHORT_CONTRACT
from tests.unit.test_aoa_contract_rules import ACT as FULL_ACT
from tests.unit.test_aoa_contract_rules import CONTRACT as FULL_CONTRACT

_SUBJECT = "ვებგვერდის დიზაინისა და შემუშავების მომსახურების გაწევა"
CONTRACT = FULL_CONTRACT.replace(
    "1.1. „შემსრულებელი“ იღებს ვალდებულებას „მზე“ პროექტისთვის (ს/კ: 01.02.03.456) მომსახურების "
    "გაწევა.\n",
    "მუხლი I. ხელშეკრულების საგანი\n"
    "1.1. „შემსრულებელი“ იღებს ვალდებულებას „მზე“ პროექტისთვის (ს/კ: 01.02.03.456) "
    f"{_SUBJECT} (შემდგომში „მომსახურება“).\n"
    "1.2. „შემსრულებელი“ ვალდებულია:\n"
    "- შეასრულოს „მომსახურება“ სრული მოცულობით და ჯეროვნად.\n"
    "- გამოასწოროს ნებისმიერი ხარვეზი საკუთარი ხარჯით.\n"
    "1.3. „შემსრულებელმა“ საანგარიშო თვეში „შემკვეთს“ უნდა ჩააბაროს:\n"
    "- მთავარი გვერდის დიზაინი GEO/ENG\n"
    "- 4 სერვისის გვერდის აღწერა GEO/ENG\n"
    "- საკონტაქტო ფორმის აწყობა\n"
    "რის შესახებაც მხარეები აფორმებენ მიღება-ჩაბარების აქტს.\n",
)
_ANNEX = (
    "დანართი #1\n"
    "მომსახურების აღწერა:\n"
    "1. „შემსრულებლის“ მიერ „მზე“ პროექტისთვის შესრულებული მომსახურება, კერძოდ:\n"
    "- მთავარი გვერდის დიზაინი GEO/ENG\n"
    "- 4 სერვისის გვერდის აღწერა GEO/ENG\n"
    "- საკონტაქტო ფორმის აწყობა\n"
)
ACT = (
    FULL_ACT.replace(
        "„შემკვეთის“ პროექტ „მზე“ (ს/კ: 01.02.03.456) მომსახურების გაწევა.\n",
        "„მხარეებმა“ გააფორმეს ხელშეკრულება, რომლის საგანსაც წარმოადგენს „შემკვეთის“ პროექტ "
        f"„მზე“ (ს/კ: 01.02.03.456) {_SUBJECT} (შემდგომში „მომსახურება“).\n",
    )
    + _ANNEX
)


def _check(act: str = ACT, contract: str = CONTRACT) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract)


def _scope(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.rule_id in ("AOA.SUBJECT", "AOA.SERVICES")]


def _only(report: CheckReport, rule_id: str) -> Finding:
    [finding] = [f for f in report.findings if f.rule_id == rule_id]
    assert finding.message_ka and finding.message_en
    return finding


def _review(report: CheckReport, rule_id: str) -> Finding:
    """The one finding of ``rule_id``, which must be a note for a person to read - and the only
    thing on an otherwise correct act."""
    finding = _only(report, rule_id)
    assert (finding.severity, finding.needs_review) == ("info", True)
    assert (finding.expected, finding.actual) == (None, None)  # no "should be" to write
    assert [f.rule_id for f in report.findings if f.severity != "info"] == []
    return finding


def test_the_fixture_rewrites_took() -> None:
    assert "ხელშეკრულების საგანი" in CONTRACT and "საგანსაც წარმოადგენს" in ACT


def test_a_correct_act_restating_subject_and_services_has_no_findings() -> None:
    """Both rules run here and neither fires - no error, no warning, not even a note - and
    nothing is skipped but the three inputs this pair does not supply."""
    report = _check()
    assert report.findings == []
    assert [s.rule_id for s in report.skipped] == [
        "AOA.SPELLING",
        "AOA.REGISTRY",
    ]


def test_the_subject_restated_in_other_case_forms_still_matches() -> None:
    act = ACT.replace(_SUBJECT, "ვებგვერდების დიზაინსა და შემუშავებაზე მომსახურების გაწევა")
    assert _scope(_check(act)) == []


@pytest.mark.parametrize(
    ("right", "wrong"),
    [
        ("- 4 სერვისის გვერდის აღწერა GEO/ENG\n", "- 4 სევრისის გვერდის აღწერა GEO/ENG\n"),
        (f"{_SUBJECT} (შემდგომში", f"{_SUBJECT.replace('შემუშავების', 'შემუშავეის')} (შემდგომში"),
    ],
)
def test_a_typo_in_a_service_or_the_subject_is_the_spelling_rules_alone(
    right: str, wrong: str
) -> None:
    """A misspelled word in a listed service or in the subject is AOA.SPELLING's finding; read
    with its correction, the part is the contract's, so no note of another service follows."""
    from tests.unit.test_aoa_spelling import Lexicon

    act = ACT.replace(right, wrong)
    assert act != ACT
    typo = next(w for w in wrong.split() if w not in right.split())
    speller = Lexicon(
        words={"სერვისის", "შემუშავების"}, unknown={"სევრისის", "შემუშავეის"}
    ).reading(act, CONTRACT)
    report = check_aoa(aoa_text=act, contract_text=CONTRACT, speller=speller)
    assert [(f.rule_id, f.actual) for f in report.findings] == [("AOA.SPELLING", typo)]


def test_the_act_may_list_fewer_services_than_the_contract() -> None:
    """An act covers a month of the work: a service it does not list is not reported."""
    act = ACT.replace("- 4 სერვისის გვერდის აღწერა GEO/ENG\n- საკონტაქტო ფორმის აწყობა\n", "")
    assert _scope(_check(act)) == []


# --- numbers are evidence ----------------------------------------------------------------------


def test_a_changed_quantity_in_a_service_is_an_error() -> None:
    report = _check(ACT.replace("- 4 სერვისის", "- 5 სერვისის"))
    finding = _only(report, "AOA.SERVICES")
    assert (finding.severity, finding.needs_review) == ("error", False)
    assert finding.expected == "4 სერვისის გვერდის აღწერა GEO/ENG"
    assert finding.actual == finding.quote == "5 სერვისის გვერდის აღწერა GEO/ENG"
    assert "a number in a listed service differs from the contract" in finding.message_en


def test_a_changed_number_in_the_subject_is_an_error() -> None:
    contract = CONTRACT.replace(_SUBJECT, f"3 ენაზე {_SUBJECT}")
    act = ACT.replace(_SUBJECT, f"2 ენაზე {_SUBJECT}")
    finding = _only(_check(act, contract), "AOA.SUBJECT")
    assert (finding.severity, finding.expected, finding.actual) == ("error", "3", "2")
    assert finding.message_en == (
        "a number in the subject differs from the contract: 2 in the act, 3 in the contract"
    )
    assert finding.quote is not None and finding.quote.endswith(f"2 ენაზე {_SUBJECT}")


def test_the_act_restating_a_duplicated_list_reports_a_change_once() -> None:
    """The parser can emit a table's text twice; the list counts once, and so does its error."""
    changed = ACT.replace("- 4 სერვისის", "- 5 სერვისის")
    report = _check(changed + _ANNEX.replace("- 4 სერვისის", "- 5 სერვისის"))
    assert [f.rule_id for f in report.findings] == ["AOA.SERVICES"]


@pytest.mark.parametrize(
    "item",
    [
        "- 4 სერვისის გვერდის აღწერა GEO/ENG (აგვისტო 2026)",
        "- 4 სერვისის გვერდის აღწერა GEO/ENG, 2026 წელი",
        "- 4 სერვისის გვერდის აღწერა GEO/ENG (08/2026)",
        "- 4 სერვისის გვერდის აღწერა GEO/ENG - 100%",
        "- სერვისის გვერდის აღწერა GEO/ENG (აგვისტო 2026)",
    ],
    ids=["month-and-year", "year", "month/year", "percent", "count-left-out-period-added"],
)
def test_a_number_the_act_only_adds_is_never_an_error(item: str) -> None:
    """The period, a year, a share done: numbers the contract's item does not state, and none
    of them in place of the count it does. Nothing the contract says differs."""
    report = _check(ACT.replace("- 4 სერვისის გვერდის აღწერა GEO/ENG", item))
    assert [f for f in report.findings if f.severity != "info"] == []


def test_a_changed_count_is_an_error_with_the_period_added_too() -> None:
    item = "- 5 სერვისის გვერდის აღწერა GEO/ENG (აგვისტო 2026)"
    report = _check(ACT.replace("- 4 სერვისის გვერდის აღწერა GEO/ENG", item))
    finding = _only(report, "AOA.SERVICES")
    assert (finding.severity, finding.expected) == ("error", "4 სერვისის გვერდის აღწერა GEO/ENG")


def test_the_period_in_the_subject_is_not_a_changed_number() -> None:
    contract = CONTRACT.replace(_SUBJECT, f"3 ენაზე {_SUBJECT}")
    act = ACT.replace(_SUBJECT, f"3 ენაზე {_SUBJECT} 2026 წლის აგვისტოში")
    assert [f for f in _check(act, contract).findings if f.severity != "info"] == []


# --- anything else is for a person to read -------------------------------------------------------


def test_a_paraphrased_subject_is_only_a_note_for_review() -> None:
    finding = _review(_check(ACT.replace(_SUBJECT, "საიტის ვიზუალის შექმნა")), "AOA.SUBJECT")
    assert finding.quote == "„შემკვეთის“ პროექტ „მზე“ (ს/კ: 01.02.03.456) საიტის ვიზუალის შექმნა"
    assert "the act words the subject differently from the contract - check it" in (
        finding.message_en
    )
    assert "გადაამოწმეთ" in finding.message_ka


def test_a_changed_subject_with_no_number_is_a_note_not_an_error() -> None:
    """The audit's repro: construction works for web design. The rules see other words, not
    evidence that the service is another one, so a person decides."""
    act = ACT.replace(_SUBJECT, "სამშენებლო სამუშაოების შესრულება")
    _review(_check(act), "AOA.SUBJECT")


def test_an_extra_service_is_a_note_naming_the_closest_one() -> None:
    """The audit's repro: an act that adds a service the contract never listed."""
    act = ACT.replace(
        "- საკონტაქტო ფორმის აწყობა\n",
        "- საკონტაქტო ფორმის აწყობა\n- 10 სერვისის გვერდის შექმნა და ლოგოს დიზაინი\n",
    )
    finding = _review(_check(act), "AOA.SERVICES")
    assert finding.quote == "10 სერვისის გვერდის შექმნა და ლოგოს დიზაინი"
    assert "the closest is 'მთავარი გვერდის დიზაინი GEO/ENG'" in finding.message_en


#: the pair's list, in the contract and in the act's annex
_LIST = (
    "- მთავარი გვერდის დიზაინი GEO/ENG\n"
    "- 4 სერვისის გვერდის აღწერა GEO/ENG\n"
    "- საკონტაქტო ფორმის აწყობა\n"
)
#: a list whose items share every word but the one that says what each is about
_PAGES = (
    "- მუზეუმის სერვისის გვერდის აღწერა GEO/ENG\n- ბიბლიოთეკის სერვისის გვერდის აღწერა GEO/ENG\n"
)


@pytest.mark.parametrize(
    ("listed", "extra"),
    [
        (_LIST, "მთავარი გვერდის ანიმაცია GEO/ENG"),
        (_PAGES, "ზოოპარკის სერვისის გვერდის აღწერა GEO/ENG"),
        (_PAGES, "ზოოპარკის სერვისის გვერდის აღწერა"),
    ],
    ids=["one-word-apart", "template-shaped", "template-shaped-no-languages"],
)
def test_an_extra_service_in_the_lists_own_shape_is_a_note(listed: str, extra: str) -> None:
    """Items of one list share most of their words - "… სერვისის გვერდის აღწერა GEO/ENG". Most
    is not all: the word that differs is the one that names the service, so a service the
    contract never listed must not pass as one it did."""
    contract = CONTRACT.replace(_LIST, listed)
    assert _scope(_check(ACT.replace(_LIST, listed), contract)) == []
    finding = _review(_check(ACT.replace(_LIST, f"{listed}- {extra}\n"), contract), "AOA.SERVICES")
    assert finding.quote == extra


def test_a_number_only_the_act_states_is_a_note() -> None:
    """The same words, and a count the contract's item does not give: not a count that
    differs, so not an error."""
    act = ACT.replace("- საკონტაქტო ფორმის აწყობა", "- 3 საკონტაქტო ფორმის აწყობა")
    finding = _review(_check(act), "AOA.SERVICES")
    assert "states a number the contract's does not" in finding.message_en


@pytest.mark.parametrize(
    "item",
    ["- Main page design GEO/ENG", "- 5 service page descriptions GEO/ENG"],
    ids=["latin", "latin-with-a-number"],
)
def test_a_service_in_another_language_is_never_an_error(item: str) -> None:
    """Latin against Georgian shares no words: that is a question for a person, not a number
    that differs, even when the act's item has a number."""
    act = ACT.replace("- მთავარი გვერდის დიზაინი GEO/ENG", item)
    _review(_check(act), "AOA.SERVICES")


def test_the_contracts_obligations_are_not_its_services() -> None:
    """The list under "ვალდებულია:" says what the contractor must do; an act listing one of
    those as a service delivered is not restating the contract's services."""
    items = service_items(DocumentView.build("c", CONTRACT).lines)
    assert [i.text for i in items] == [
        "მთავარი გვერდის დიზაინი GEO/ENG",
        "4 სერვისის გვერდის აღწერა GEO/ENG",
        "საკონტაქტო ფორმის აწყობა",
    ]
    act = ACT.replace(
        "- საკონტაქტო ფორმის აწყობა\n",
        "- საკონტაქტო ფორმის აწყობა\n- გამოასწოროს ნებისმიერი ხარვეზი საკუთარი ხარჯით.\n",
    )
    _review(_check(act), "AOA.SERVICES")


# --- what cannot be compared says so -----------------------------------------------------------


def test_a_pair_that_states_neither_skips_both_rules() -> None:
    """The short pair has no subject clause and no list: both rules say they could not run."""
    report = check_aoa(aoa_text=SHORT_ACT, contract_text=SHORT_CONTRACT)
    assert _scope(report) == []
    reasons = {s.rule_id: s.reason_en for s in report.skipped}
    assert reasons["AOA.SUBJECT"].startswith("no subject clause")
    assert reasons["AOA.SERVICES"].startswith("no list of services")


def test_an_act_that_does_not_restate_them_skips_both_rules() -> None:
    report = _check(FULL_ACT, CONTRACT)
    assert _scope(report) == []
    reasons = {s.rule_id: s.reason_en for s in report.skipped}
    assert reasons["AOA.SUBJECT"].startswith("the act does not restate the contract's subject")
    assert reasons["AOA.SERVICES"].startswith("the act lists no services")


def test_a_subject_naming_only_the_project_is_skipped() -> None:
    act = ACT.replace(f" {_SUBJECT} (შემდგომში", " (შემდგომში")
    report = _check(act)
    assert _scope(report) == []
    assert [s.reason_en for s in report.skipped if s.rule_id == "AOA.SUBJECT"] == [
        "the act's subject names only the project and the plot, which are compared on their own"
    ]


# --- the readers ---------------------------------------------------------------------------------


class TestSubjectClause:
    def test_the_contracts_heading_and_the_acts_restatement_are_both_read(self) -> None:
        contract = subject_clause(normalize_confusables(CONTRACT))
        act = subject_clause(normalize_confusables(ACT))
        assert contract is not None and act is not None
        # the project, the plot and the parties are left to their own rules
        assert act.stems == ("ვებგვერდ", "დიზაინ", "შემუშავ", "მომსახურ", "გაწევა")
        assert set(act.stems) <= set(contract.stems)
        assert act.numbers == () and contract.numbers == ()

    def test_a_word_that_merely_starts_like_subject_opens_nothing(self) -> None:
        text = "საგანგებო წარმოადგენს ვებგვერდის დიზაინს (შემდგომში „მომსახურება“)"
        assert subject_clause(text) is None

    def test_a_clause_that_never_reaches_its_defined_term_is_not_a_subject(self) -> None:
        text = "ხელშეკრულების საგანი " + "სიტყვა " * 120 + "(შემდგომში „მომსახურება“)"
        assert subject_clause(text) is None

    def test_the_works_and_the_goods_end_a_subject_too(self) -> None:
        for term in ("სამუშაო", "სამუშაოები", "საქონელი"):
            found = subject_clause(f"საგანს წარმოადგენს სახურავის შეკეთება (შემდგომში „{term}“)")
            assert found is not None and found.text == "სახურავის შეკეთება"


class TestServiceItems:
    def test_each_bullet_form_under_a_lead_in_is_an_item(self) -> None:
        lines = (
            "სამუშაო მოიცავს:",
            "ა) ფასადის შეღებვა",
            "• სახურავის შეკეთება",
            "1) ღობის მონტაჟი",
            "ეს სია აქ მთავრდება.",
            "- ეს უკვე სიაში აღარ არის",
        )
        assert [i.text for i in service_items(lines)] == [
            "ფასადის შეღებვა",
            "სახურავის შეკეთება",
            "ღობის მონტაჟი",
        ]

    def test_a_lead_in_with_text_after_its_colon_opens_no_list(self) -> None:
        assert service_items(("კერძოდ: ფასადის შეღებვა", "- სახურავის შეკეთება")) == []

    def test_an_item_written_twice_counts_once(self) -> None:
        lines = ("კერძოდ:", "- ფასადის შეღებვა", "კერძოდ:", "- ფასადის  შეღებვა")
        assert [i.text for i in service_items(lines)] == ["ფასადის შეღებვა"]


class TestNumbersAndAlignment:
    def test_numbers_are_values_and_dates_are_not_numbers(self) -> None:
        part = scope_part("1 000 (ათასი) ლარის ღირებულების 3 გვერდი, 2026 წლის 1 მარტამდე")
        assert part.numbers == ("1000", "3")
        assert "ათას" not in part.stems  # a number's words only repeat it

    def test_a_clause_number_and_a_cadastral_code_are_not_the_items_numbers(self) -> None:
        part = scope_part("1.1. ნაკვეთის (ს/კ: 01.02.03.456) შემოღობვა, 01.02.03.457")
        assert part.numbers == ()

    @pytest.mark.parametrize(
        "period", ["აგვისტო 2026", "2026 წელი", "2026 წლის", "2026 წ.", "08/2026", "August 2026"]
    )
    def test_a_year_named_as_one_is_not_a_number(self, period: str) -> None:
        """``find_dates`` reads only a day; a month and year, or a year called one, is a period
        too, not a quantity."""
        assert scope_part(f"4 გვერდის აღწერა, {period}").numbers == ("4",)

    def test_a_number_like_a_year_is_still_a_number(self) -> None:
        assert scope_part("2026 ლარის ღირებულების 3 გვერდი").numbers == ("2026", "3")

    def test_the_contracts_item_that_states_the_acts_number_wins_a_tie(self) -> None:
        contract = [scope_part("4 გვერდის აღწერა"), scope_part("5 გვერდის აღწერა")]
        found = align(scope_part("5 გვერდის აღწერა"), contract)
        assert (found.verdict, found.best) == ("match", contract[1])

    def test_a_number_the_act_adds_does_not_pick_the_item_it_would_contradict(self) -> None:
        """The act's "5" is the second item's; the "100" it adds is no one's, and must not turn
        the first item's "4" into a changed count."""
        contract = [scope_part("4 გვერდის აღწერა"), scope_part("5 გვერდის აღწერა")]
        found = align(scope_part("5 გვერდის აღწერა - 100%"), contract)
        assert (found.verdict, found.best, found.differ) == ("review", contract[1], ("100",))

    def test_an_item_with_no_words_to_compare_matches_its_twin(self) -> None:
        """A quoted name, the same on both sides, leaves no content words; it is the same item
        all the same."""
        assert align(scope_part("„მზე“"), [scope_part("„მზე“")]).verdict == "match"

    @pytest.mark.parametrize(
        ("act", "contract", "verdict"),
        [
            ("გვერდის აღწერა", "4 გვერდის აღწერა", "match"),  # the act need not restate it
            ("5 გვერდის აღწერა", "4 გვერდის აღწერა", "number"),
            ("5 გვერდის აღწერა", "გვერდის აღწერა", "review"),  # only the act states one
            ("4 გვერდის აღწერა - 100%", "4 გვერდის აღწერა", "review"),  # ... beside the same 4
            ("მთავარი გვერდის ტექსტი GEO/ENG", "მთავარი გვერდის დიზაინი GEO/ENG", "review"),
            ("5 მთავარი გვერდის ტექსტი GEO/ENG", "4 მთავარი გვერდის დიზაინი GEO/ENG", "number"),
            ("ლოგოს დიზაინი", "გვერდის აღწერა", "review"),
            ("4", "4 გვერდის აღწერა", "review"),  # no words to go by
        ],
    )
    def test_the_verdict(self, act: str, contract: str, verdict: str) -> None:
        assert align(scope_part(act), [scope_part(contract)]).verdict == verdict

    def test_nothing_to_align_with_is_for_review(self) -> None:
        found = align(scope_part("გვერდის აღწერა"), [])
        assert (found.verdict, found.best, found.share) == ("review", None, 0.0)

    def test_a_part_keeps_the_documents_own_words(self) -> None:
        part = scope_part("„მზე“ (ს/კ: 01.02.03.456) გვერდის  აღწერა", (3, 9))
        assert part == ScopePart(
            "„მზე“ (ს/კ: 01.02.03.456) გვერდის აღწერა", ("გვერდ", "აღწერა"), (), (3, 9)
        )
