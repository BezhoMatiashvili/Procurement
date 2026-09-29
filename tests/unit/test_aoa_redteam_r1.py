"""Defects the first red-team round found in the act check, one planted mistake per test.

Each test states the defect's id (``samples/redteam/aoa/CONFIRMED_R1.md``) and the behaviour the
spec asks for. Synthetic text only: the pair is ``test_aoa_check``'s, with every name, code and
account invented.
"""

from __future__ import annotations

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from tests.unit.test_aoa_check import ACT, CONTRACT


def _check(act: str = ACT, contract: str | None = CONTRACT, **kw: object) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract, **kw)  # type: ignore[arg-type]


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _substantive(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.severity in ("error", "warning")]


# --- AOA-02: a BIC and its XXX branch form are one bank ------------------------------------------


class TestBicBranch:
    def test_the_act_writing_the_branch_form_is_the_same_bank(self) -> None:
        act = ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAAAGE22XXX;")
        assert _substantive(_check(act)) == []

    def test_the_contract_writing_the_branch_form_is_the_same_bank(self) -> None:
        contract = CONTRACT.replace("ბანკის კოდი: AAAAGE22,", "ბანკის კოდი: AAAAGE22XXX,")
        assert _substantive(_check(contract=contract)) == []

    def test_another_branch_is_still_another_code(self) -> None:
        act = ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAAAGE22B01;")
        [finding] = _of(_check(act), "AOA.BANK")
        assert (finding.actual, finding.expected) == ("AAAAGE22B01", "AAAAGE22")

    def test_the_branch_form_pairs_with_its_account(self) -> None:
        """With two banks in the contract the codes are also checked as pairs with their accounts,
        and the branch form must pair too."""
        both = CONTRACT.replace(
            "ანგარიშის ნომერი: GE29NB0000000101904917.",
            "ანგარიშის ნომერი: GE29NB0000000101904917.\n4.4. „შემკვეთის“ ანგარიში: სს „ნიმუშ "
            "ბანკი“, ბანკის კოდი: CCCCGE22, ანგარიშის ნომერი: GE11CC0000000123456789.",
        )
        act = ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAAAGE22XXX;")
        assert _substantive(_check(act, both)) == []


# --- AOA-08: a code's correction is the one contract value it stands for -------------------------

#: The contract with both parties' requisites, each in its own block.
_REQUISITES = """5. მხარეთა რეკვიზიტები
„შემკვეთი“: შპს „ალფა ჯგუფი“, ს/კ: 123456789, ბანკის კოდი: CCCCGE22, ა/ა: GE11CC0000000123456789
„შემსრულებელი“: შპს „ბეტა სერვისი“, ს/კ: 987654321, ბანკის კოდი: AAAAGE22,
ა/ა: GE29NB0000000101904917
"""
_BOTH_BANKS = CONTRACT + _REQUISITES


class TestNearestCorrection:
    def test_a_correct_act_under_both_requisites_is_clean(self) -> None:
        assert _substantive(_check(contract=_BOTH_BANKS)) == []

    def test_an_account_with_two_digits_swapped_is_corrected_to_its_own_account(self) -> None:
        act = ACT.replace("GE29NB0000000101904917", "GE29NB0000000101904971")
        [finding] = _of(_check(act, _BOTH_BANKS), "AOA.BANK")
        assert (finding.actual, finding.expected) == (
            "GE29NB0000000101904971",
            "GE29NB0000000101904917",
        )

    def test_a_bank_code_slip_is_corrected_to_the_nearest_code(self) -> None:
        act = ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAABGE22;")
        [finding] = _of(_check(act, _BOTH_BANKS), "AOA.BANK")
        assert (finding.actual, finding.expected) == ("AAABGE22", "AAAAGE22")

    def test_a_tie_goes_to_the_contractor_s_own_account(self) -> None:
        """One digit from both accounts: the act's account is the contractor's to be paid on."""
        contract = _BOTH_BANKS.replace("GE11CC0000000123456789", "GE29NB0000000101904918")
        act = ACT.replace("GE29NB0000000101904917", "GE29NB0000000101904919")
        [finding] = _of(_check(act, contract), "AOA.BANK")
        assert finding.expected == "GE29NB0000000101904917"

    def test_an_unrelated_account_is_corrected_to_the_contractor_s(self) -> None:
        act = ACT.replace("GE29NB0000000101904917", "GE77ZZ9999999999999999")
        [finding] = _of(_check(act, _BOTH_BANKS), "AOA.BANK")
        assert finding.expected == "GE29NB0000000101904917"

    def test_a_plot_code_with_digits_swapped_is_corrected_to_the_nearest_plot(self) -> None:
        contract = CONTRACT.replace("(ს/კ: 01.02.03.456)", "(ს/კ: 01.02.03.456 და 01.02.03.457)")
        act = ACT.replace("(ს/კ: 01.02.03.456)", "(ს/კ: 01.02.03.475)")
        [finding] = _of(_check(act, contract), "AOA.CADASTRAL")
        assert (finding.actual, finding.expected) == ("01.02.03.475", "01.02.03.457")

    def test_a_plot_code_as_near_to_two_plots_has_no_correction(self) -> None:
        """No single plot is meant: the message lists them, the correction is left blank."""
        contract = CONTRACT.replace("(ს/კ: 01.02.03.456)", "(ს/კ: 01.02.03.456 და 01.02.03.457)")
        act = ACT.replace("(ს/კ: 01.02.03.456)", "(ს/კ: 01.02.03.458)")
        [finding] = _of(_check(act, contract), "AOA.CADASTRAL")
        assert finding.expected is None
        assert "01.02.03.456" in finding.message_en and "01.02.03.457" in finding.message_en


# --- AOA-13: a director misspelt in the final vowel ----------------------------------------------


class TestDirectorFinalVowel:
    def test_a_signature_ending_in_the_wrong_vowel_is_an_error(self) -> None:
        act = ACT.replace("დირექტორი: დავით მელაძე", "დირექტორი: დავით მელაძი")
        [finding] = _of(_check(act), "AOA.DIRECTOR")
        assert (finding.severity, finding.actual, finding.expected) == (
            "error",
            "დავით მელაძი",
            "დავით მელაძე",
        )

    def test_the_slip_is_found_beside_the_same_name_written_right(self) -> None:
        """The preamble's genitive is right and the signature's nominative is not: the two are
        read as one director, and the slip must not hide behind the right form."""
        act = ACT.replace("დირექტორი ნინო კაპანაძე", "დირექტორი ნინო კაპანაძი")
        [finding] = _of(_check(act), "AOA.DIRECTOR")
        assert (finding.actual, finding.expected) == ("ნინო კაპანაძი", "ნინო კაპანაძე")

    def test_a_shvili_surname_ending_in_e_is_an_error(self) -> None:
        contract = CONTRACT.replace("დავით მელაძის", "დავით ცქვიტინაშვილის")
        act = ACT.replace("დირექტორი: დავით მელაძე", "დირექტორი: დავით ცქვიტინაშვილე")
        [finding] = _of(_check(act, contract), "AOA.DIRECTOR")
        assert (finding.actual, finding.expected) == (
            "დავით ცქვიტინაშვილე",
            "დავით ცქვიტინაშვილი",
        )

    def test_every_case_form_of_the_contract_s_name_is_the_same_person(self) -> None:
        from procurement_agent.checks.aoa import _same_person
        from procurement_agent.extraction.georgian import CASES, inflect

        for name in ("დავით მელაძე", "ნინო ცქვიტინაშვილი", "ლია ქობულია", "გელა ჯაფარიძე"):
            for written in {inflect(name, c) for c in CASES}:
                assert _same_person(written, inflect(name, "gen")), (written, name)
        assert _same_person("ნოდარი მელაძე", "ნოდარ მელაძის")  # the first name's two nominatives
        assert not _same_person("დავით მელაძი", "დავით მელაძის")
        assert not _same_person("ნინო ცქვიტინაშვილე", "ნინო ცქვიტინაშვილის")
        assert not _same_person("დავით მელაძე", "გიორგი მელაძე")


# --- AOA-11: the act dated in other words --------------------------------------------------------

_DATED = "4. გაფორმების თარიღი\nაქტი გაფორმდა 2026 წლის 30 აპრილი"


def _dated(wording: str, day: str) -> str:
    """The act with its date line written as ``wording`` (``{}`` for the date)."""
    return ACT.replace(_DATED, "4. " + wording.format(day))


_ACT_DATE_WORDINGS = (
    "აქტი შედგენილია {}.",
    "„აქტი“ შედგენილია {}.",
    "აქტის შედგენის თარიღი: {}.",
    "ხელმოწერის თარიღი: {}.",
    "აქტი ხელმოწერილია {}.",
)


class TestActDateWordings:
    def test_a_correct_act_is_clean_in_every_wording(self) -> None:
        for wording in _ACT_DATE_WORDINGS:
            report = _check(_dated(wording, "2026 წლის 30 აპრილს"))
            assert _substantive(report) == [], wording
            assert report.summary["act_date"] == "2026-04-30", wording

    def test_a_late_act_is_an_error_in_every_wording(self) -> None:
        for wording in _ACT_DATE_WORDINGS:
            [finding] = _of(_check(_dated(wording, "2026 წლის 5 მაისს")), "AOA.ACT_DATE")
            assert (finding.severity, finding.actual, finding.expected) == (
                "error",
                "2026 წლის 5 მაისს",
                "2026 წლის 30 აპრილს",
            ), wording

    def test_copies_boilerplate_is_not_the_act_s_date(self) -> None:
        """ "Drawn up in two copies" dates nothing, and the date after it is not the act's."""
        act = ACT.replace(
            _DATED,
            "4. აქტი შედგენილია ორ ეგზემპლარად.\nავანსი გადახდილია 2026 წლის 10 მაისს.\n"
            "5. აქტი გაფორმდა 2026 წლის 30 აპრილი",
        )
        report = _check(act)
        assert report.summary["act_date"] == "2026-04-30"
        assert _of(report, "AOA.ACT_DATE") == []

    def test_a_deadline_counted_from_the_signing_is_not_the_act_s_date(self) -> None:
        act = _dated("აქტი ხელმოწერილია {}.", "2026 წლის 30 აპრილს").replace(
            "3. საფასური",
            "3. საფასური გადაიხდება აქტის ხელმოწერის თარიღიდან 2026 წლის 20 მაისამდე.",
        )
        assert _check(act).summary["act_date"] == "2026-04-30"


# --- AOA-12: the service period stated as a month ------------------------------------------------

_PERIOD = "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით."


def _period(line: str, dated: str | None = None) -> str:
    act = ACT.replace(_PERIOD, line)
    if dated is not None:
        act = act.replace("აქტი გაფორმდა 2026 წლის 30 აპრილი", f"აქტი გაფორმდა {dated}")
    return act


_MONTH_PERIODS = (
    "მომსახურება შესრულდა 2026 წლის აპრილის თვეში.",
    "საანგარიშო პერიოდი: 2026 წლის აპრილი.",
    "მომსახურება გაწეულია 2026 წლის აპრილში.",
    "საანგარიშო პერიოდი: აპრილი 2026.",
)


class TestPeriodByMonth:
    def test_a_correct_act_is_clean(self) -> None:
        for line in _MONTH_PERIODS:
            report = _check(_period(line))
            assert _substantive(report) == [], line
            assert report.summary["execution_period"] == "2026-04-01..2026-04-30", line

    def test_a_late_act_is_dated_the_month_s_last_day(self) -> None:
        for line in _MONTH_PERIODS:
            report = _check(_period(line, dated="2026 წლის 15 მაისი"))
            [finding] = _of(report, "AOA.ACT_DATE")
            assert (finding.actual, finding.expected) == (
                "2026 წლის 15 მაისი",
                "2026 წლის 30 აპრილი",
            ), line

    def test_a_month_out_of_the_term_is_corrected_to_the_term_s_year(self) -> None:
        report = _check(_period("მომსახურება შესრულდა 2025 წლის აპრილის თვეში."))
        [finding] = _of(report, "AOA.PERIOD_IN_TERM")
        assert finding.expected == "2026 წლის აპრილის"
        assert _of(report, "AOA.ACT_DATE") == []

    def test_a_contract_s_month_named_in_the_line_is_no_period(self) -> None:
        line = "მომსახურება შესრულდა 2026 წლის მარტის ხელშეკრულების შესაბამისად."
        report = _check(_period(line))
        assert "execution_period" not in report.summary


# --- AOA-20: a period across the New Year with the end year not moved on -------------------------

_NEW_YEAR_CONTRACT = CONTRACT.replace(
    "2026 წლის 1 მარტიდან - 2026 წლის 1 სექტემბრის ჩათვლით",
    "2026 წლის 1 სექტემბრიდან - 2027 წლის 1 მარტის ჩათვლით",
)


def _winter(period: str, dated: str = "2027 წლის 31 იანვარი") -> str:
    return _period(f"მომსახურება შესრულდა {period} ჩათვლით.", dated=dated)


class TestNewYearPeriod:
    def test_the_right_years_are_clean(self) -> None:
        act = _winter("2026 წლის 15 დეკემბრიდან 2027 წლის 14 იანვრის")
        assert _substantive(_check(act, _NEW_YEAR_CONTRACT)) == []

    def test_a_stale_end_year_is_the_period_s_mistake_not_the_act_date_s(self) -> None:
        report = _check(
            _winter("2026 წლის 15 დეკემბრიდან 2026 წლის 14 იანვრის"), _NEW_YEAR_CONTRACT
        )
        assert _of(report, "AOA.ACT_DATE") == []
        [finding] = _of(report, "AOA.PERIOD_IN_TERM")
        assert finding.severity == "error"
        assert finding.actual == "2026 წლის 15 დეკემბრიდან 2026 წლის 14 იანვრის"
        assert finding.expected == "2026 წლის 15 დეკემბრიდან 2027 წლის 14 იანვრის"
        assert report.summary["execution_period"] == "2026-12-15..2027-01-14"

    def test_a_start_year_run_ahead_is_put_back(self) -> None:
        report = _check(
            _winter("2027 წლის 15 დეკემბრიდან 2027 წლის 14 იანვრის"), _NEW_YEAR_CONTRACT
        )
        assert _of(report, "AOA.ACT_DATE") == []
        [finding] = _of(report, "AOA.PERIOD_IN_TERM")
        assert finding.expected == "2026 წლის 15 დეკემბრიდან 2027 წლის 14 იანვრის"

    def test_the_act_dated_off_the_repaired_period_is_still_an_act_date_error(self) -> None:
        report = _check(
            _winter(
                "2026 წლის 15 დეკემბრიდან 2026 წლის 14 იანვრის", dated="2027 წლის 20 თებერვალი"
            ),
            _NEW_YEAR_CONTRACT,
        )
        [finding] = _of(report, "AOA.ACT_DATE")
        assert finding.expected == "2027 წლის 31 იანვარი"
