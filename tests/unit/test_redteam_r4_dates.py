"""Red team round 4: an act's period and its date, and a contract's validity, read by rule.

AOA-R4-03, AOA-R4-05, AOA-R4-07 and COMPARE-R4-01. Synthetic
text only: every name, code and date here is invented.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.compare import compare
from procurement_agent.checks.document_facts import document_facts
from procurement_agent.ingestion.segmenter import Segment, build
from tests.unit.test_aoa_check import ACT, CONTRACT

_HEADING_AND_PERIOD = (
    "2. შესრულების თარიღი\nმომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით."
)
_SIGNED = "4. გაფორმების თარიღი\nაქტი გაფორმდა 2026 წლის 30 აპრილი"


def _act(period: str, signed: str = "2026 წლის 30 აპრილი") -> str:
    """The test act with its period stated by ``period`` alone - no heading names it - and
    dated ``signed``."""
    assert _HEADING_AND_PERIOD in ACT and _SIGNED in ACT
    return ACT.replace(_HEADING_AND_PERIOD, f"2. {period}").replace(
        _SIGNED, f"4. გაფორმების თარიღი\nაქტი გაფორმდა {signed}"
    )


def _noisy(report) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    return [(f.rule_id, f.severity) for f in report.findings if f.severity in ("error", "warning")]


def _facts(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in document_facts(text):
        out.setdefault(f.type.value, []).append(f.normalized)
    return out


# --- AOA-R4-03: the service period by its form, whatever the verb, by one reader ----------------

_WORDINGS = [
    "მომსახურება შესრულებულია 2026 წლის 1 აპრილიდან 30 აპრილის ჩათვლით.",
    "მომსახურება გაწეულ იქნა 2026 წლის 1 აპრილიდან 30 აპრილის ჩათვლით.",
    "მომსახურება განხორციელდა 2026 წლის 1 აპრილიდან 30 აპრილის ჩათვლით.",
    "მომსახურება ჩატარდა 2026 წლის 1 აპრილიდან 30 აპრილის ჩათვლით.",
    "მომსახურება მიეწოდა 2026 წლის 1 აპრილიდან 30 აპრილის ჩათვლით.",
    "მომსახურება შესრულებულ იქნა 01.04.2026 - 30.04.2026.",
    "პერიოდი: 01.04.2026 - 30.04.2026.",
    "გაწევის პერიოდი: 01.04.2026 - 30.04.2026.",
    "მომსახურების გაწევის პერიოდი: 01.04.2026 - 30.04.2026.",
    "მიწოდების პერიოდი: 01.04.2026 - 30.04.2026.",
    # the period before its verb, as Georgian puts it
    "„შემსრულებელმა“ 2026 წლის 1-30 აპრილის პერიოდში გასწია მომსახურება.",
    "„შემსრულებელმა“ 01.04.2026 - 30.04.2026 პერიოდში შეასრულა მომსახურება.",
    # a turn after the period names the contract: the period's end is still its own
    "მომსახურება გაწეულ იქნა 2026 წლის 1 აპრილიდან 30 აპრილის ჩათვლით, ხელშეკრულების "
    "პირობების შესაბამისად.",
]


@pytest.mark.parametrize("period", _WORDINGS)
def test_a_correct_act_reads_its_period_in_every_wording(period: str) -> None:
    act = _act(period)
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary.get("execution_period") == "2026-04-01..2026-04-30"
    assert _noisy(report) == []
    # one reader: the extraction states the period the act check read
    assert _facts(act)["execution_period"] == ["2026-04-01/2026-04-30"]


@pytest.mark.parametrize("period", _WORDINGS)
def test_an_act_dated_before_the_period_ends_is_an_error_in_every_wording(period: str) -> None:
    report = check_aoa(aoa_text=_act(period, "2026 წლის 29 აპრილი"), contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.ACT_DATE"]
    assert (finding.severity, finding.actual, finding.expected) == (
        "error",
        "2026 წლის 29 აპრილი",
        "2026 წლის 30 აპრილი",
    )


@pytest.mark.parametrize(
    ("period", "written", "fixed"),
    [
        (
            "მომსახურება გაწეულ იქნა 2025 წლის 1 აპრილიდან 30 აპრილის ჩათვლით.",
            "2025 წლის 1 აპრილიდან 30 აპრილის",
            "2026 წლის 1 აპრილიდან 30 აპრილის",
        ),
        ("პერიოდი: 01.04.2025 - 30.04.2025.", "01.04.2025 - 30.04.2025", "01.04.2026 - 30.04.2026"),
    ],
)
def test_a_year_slip_in_a_period_read_by_its_form_is_out_of_term(
    period: str, written: str, fixed: str
) -> None:
    report = check_aoa(aoa_text=_act(period), contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.PERIOD_IN_TERM"]
    assert (finding.severity, finding.actual, finding.expected) == ("error", written, fixed)
    assert [f for f in report.findings if f.rule_id == "AOA.ACT_DATE"] == []


@pytest.mark.parametrize(
    "line",
    [
        "საგარანტიო პერიოდი: 01.05.2026 - 30.04.2027.",
        "ხელშეკრულების მოქმედების პერიოდი: 01.03.2026 - 01.09.2026.",
        # the contract's window restated: a duty, not the work done
        "ხელშეკრულების თანახმად, „შემსრულებელი“ ვალდებულია გასწიოს მომსახურება 2026 წლის 1 "
        "მარტიდან 2026 წლის 1 სექტემბრის ჩათვლით.",
        # a verb with no date in its sentence takes none from the next one
        "მომსახურება შესრულებულია სრულად. საფასური გადაიხდება 2026 წლის 10 მაისამდე.",
    ],
)
def test_a_period_that_is_no_service_period_is_not_read_as_one(line: str) -> None:
    act = _act(line)
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert "execution_period" not in report.summary
    assert "execution_period" not in _facts(act)


def test_a_recital_restating_the_contracts_window_leaves_the_period_alone() -> None:
    recital = (
        "ხელშეკრულების თანახმად, „შემსრულებელი“ ვალდებულია გასწიოს მომსახურება 2026 წლის 1 "
        "მარტიდან 2026 წლის 1 სექტემბრის ჩათვლით. "
    )
    for period in (
        "მომსახურება გაწეულ იქნა 01.04.2026 - 30.04.2026.",
        "პერიოდი: 01.04.2026 - 30.04.2026.",
    ):
        report = check_aoa(aoa_text=_act(recital + period), contract_text=CONTRACT)
        assert report.summary.get("execution_period") == "2026-04-01..2026-04-30"
        assert _noisy(report) == []


# --- COMPARE-R4-01: a date that names its contract is never the period's ------------------------

_PARTIES = (
    "შპს „მთის წყარო“, საიდენტიფიკაციო კოდი: 200100200 (შემდგომში „შემკვეთი“), და\n"
    "შპს „ქვის კიბე“, საიდენტიფიკაციო კოდი: 300200300 (შემდგომში „შემსრულებელი“)\n"
)


def _act_naming_its_contract(reference: str, period: str = "2027 წლის 1-30 აპრილის") -> str:
    return (
        "მიღება-ჩაბარების აქტი\n"
        f"{_PARTIES}"
        f"1. 2027 წლის 3 მარტის {reference} ხელშეკრულების საფუძველზე „შემსრულებელმა“ {period} "
        "პერიოდში გასწია მომსახურება.\n"
        "2. საფასური შეადგენს 500 (ხუთასი) ლარს.\n"
        "3. აქტი გაფორმდა 2027 წლის 30 აპრილს.\n"
    )


@pytest.mark.parametrize(
    "reference", ["N 15", "N15", "№ 15", "№15", "# 15", "No. 15", "№ QX-2027/41", "№ 41/2027"]
)
@pytest.mark.parametrize("period", ["2027 წლის 1-30 აპრილის", "01.04.2027 - 30.04.2027"])
def test_the_contracts_date_is_not_the_periods_first_day(reference: str, period: str) -> None:
    facts = _facts(_act_naming_its_contract(reference, period))
    assert facts["execution_period"] == ["2027-04-01/2027-04-30"]
    assert facts["contract_date"] == ["2027-03-03"]
    assert facts["act_date"] == ["2027-04-30"]


def _segs(text: str) -> list[Segment]:
    lines = [line for line in text.split("\n") if line]
    return build([(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(lines)])


def _changes(left: str, right: str) -> list[tuple[str, str | None, str | None]]:
    result = compare(
        _segs(left),
        _segs(right),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    return [(f.rule_id, f.expected, f.actual) for f in result.findings if f.severity == "error"]


def test_an_act_revision_changing_only_the_contracts_date_is_one_finding() -> None:
    act = _act_naming_its_contract("№ QX-2027/41")
    revised = act.replace("2027 წლის 3 მარტის", "2027 წლის 4 მარტის")
    assert _changes(act, revised) == [("CMP.DATE", "2027-03-03", "2027-03-04")]


def test_an_act_revision_moving_the_month_is_one_period_change() -> None:
    act = _act_naming_its_contract("№ QX-2027/41")
    revised = act.replace("2027 წლის 1-30 აპრილის", "2027 წლის 1-31 მაისის")
    assert _changes(act, revised) == [
        ("CMP.DURATION", "2027-04-01/2027-04-30", "2027-05-01/2027-05-31")
    ]


# --- AOA-R4-05: an act dated in its heading, below a title that dates its contract ---------------

_TITLES = [
    "მიღება-ჩაბარების აქტი (2026 წლის 1 მარტის N 7/2026 ხელშეკრულებაზე)\n",
    "მიღება-ჩაბარების აქტი\n2026 წლის 1 მარტის N 7/2026 ხელშეკრულების საფუძველზე\n",
    "მიღება-ჩაბარების აქტი N 6 (01.03.2026 წლის ხელშეკრულება № 7/2026)\n",
]


def _headed(title: str, heading_date: str) -> str:
    act = ACT.replace(_SIGNED + "\n", "")
    return act.replace(
        "მიღება-ჩაბარების აქტი\n", f"{title}ქ. თბილისი                          {heading_date}\n", 1
    )


@pytest.mark.parametrize("title", _TITLES)
def test_an_act_dated_only_in_its_heading_is_read_below_a_title_dating_its_contract(
    title: str,
) -> None:
    report = check_aoa(aoa_text=_headed(title, "30.04.2026"), contract_text=CONTRACT)
    assert report.summary.get("act_date") == "2026-04-30"
    assert _noisy(report) == []
    early = check_aoa(aoa_text=_headed(title, "29.04.2026"), contract_text=CONTRACT)
    [finding] = [f for f in early.findings if f.rule_id == "AOA.ACT_DATE"]
    assert (finding.severity, finding.actual, finding.expected) == (
        "error",
        "29.04.2026",
        "30.04.2026",
    )


def test_a_heading_date_beside_a_contract_no_date_names_is_not_the_acts() -> None:
    """The contract named on its own line, with a date under it: whose date that is, nothing
    says - it is not taken for the act's."""
    act = _headed("მიღება-ჩაბარების აქტი\nხელშეკრულება N 7/2026\n", "30.04.2026")
    assert "act_date" not in check_aoa(aoa_text=act, contract_text=CONTRACT).summary


def test_a_date_past_the_preamble_is_not_the_acts_heading_date() -> None:
    act = (
        "მიღება-ჩაბარების აქტი (2026 წლის 1 მარტის N 7/2026 ხელშეკრულებაზე)\n"
        "1. მომსახურება გაწეულ იქნა 2026 წლის 1 აპრილიდან 30 აპრილის ჩათვლით.\n"
    )
    assert "act_date" not in _facts(act)


# --- AOA-R4-07: the act's date in its other wordings ---------------------------------------------

_DATE_WORDINGS = [
    "„აქტი“ გაფორმებულია {d}.",
    "აქტი გაფორმებულია მხარეთა მიერ {d}.",
    "აქტის გაფორმების თარიღია {d}.",
    "აქტი შედგა {d}.",
    "„აქტს“ ხელი მოეწერა {d}.",
]


@pytest.mark.parametrize("wording", _DATE_WORDINGS)
def test_the_acts_date_is_read_in_every_wording(wording: str) -> None:
    def act(d: str) -> str:
        return ACT.replace(_SIGNED, "4. " + wording.format(d=d))

    report = check_aoa(aoa_text=act("2026 წლის 30 აპრილს"), contract_text=CONTRACT)
    assert report.summary.get("act_date") == "2026-04-30"
    assert _noisy(report) == []
    late = check_aoa(aoa_text=act("2026 წლის 5 მაისს"), contract_text=CONTRACT)
    [finding] = [f for f in late.findings if f.rule_id == "AOA.ACT_DATE"]
    assert (finding.severity, finding.actual, finding.expected) == (
        "error",
        "2026 წლის 5 მაისს",
        "2026 წლის 30 აპრილს",
    )


@pytest.mark.parametrize(
    "line",
    [
        "4. აქტი შედგა ორ ეგზემპლარად. საფასური გადაიხდება 2026 წლის 10 მაისამდე.",
        "4. აქტი შედგება ორი გვერდისგან, საფასური გადაიხდება 2026 წლის 10 მაისამდე.",
        "4. საფასური გადაიხდება აქტის ხელმოწერის თარიღიდან არაუგვიანეს 2026 წლის 10 მაისისა.",
    ],
)
def test_words_about_the_act_that_date_nothing_give_it_no_date(line: str) -> None:
    report = check_aoa(aoa_text=ACT.replace(_SIGNED, line), contract_text=CONTRACT)
    assert "act_date" not in report.summary
