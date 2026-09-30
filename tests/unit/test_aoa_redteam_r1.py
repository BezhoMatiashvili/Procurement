"""Defects the first red-team round found in the act check, one planted mistake per test.

Each test states the defect's id (``samples/redteam/aoa/CONFIRMED_R1.md``) and the behaviour the
spec asks for. Synthetic text only: the pair is ``test_aoa_check``'s, with every name, code and
account invented.
"""

from __future__ import annotations

import re

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


# --- AOA-16: "VAT included" in the other words acts use ------------------------------------------

_FEE_TAX = "საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით."


def _vat_worded(wording: str) -> CheckReport:
    act = ACT.replace(_FEE_TAX, f"საფასური შეადგენს 100 (ასი) ლარს{wording}.")
    return _check(act, contractor_type="individual_entrepreneur_vat")


class TestVatIncludedWordings:
    def test_vat_included_in_other_words_is_the_wording_asked_for(self) -> None:
        for wording in (
            " (თანხა შეიცავს დღგ-ს)",
            ", თანხაში შედის დღგ",
            " (ფასში შედის დღგ)",
            ", ჩათვლით დღგ",
            " (დღგ შედის ღირებულებაში)",
        ):
            assert _of(_vat_worded(wording), "AOA.TAX_WORDING") == [], wording

    def test_vat_left_out_in_those_words_is_an_error(self) -> None:
        for wording in (" (თანხა არ შეიცავს დღგ-ს)", ", თანხაში არ შედის დღგ"):
            [finding] = _of(_vat_worded(wording), "AOA.TAX_WORDING")
            assert finding.severity == "error", wording

    def test_entering_into_force_includes_nothing(self) -> None:
        from procurement_agent.checks.contract_terms import tax_phrases

        text = "ხელშეკრულება ძალაში შედის დღგ-ს გადამხდელად რეგისტრაციის დღიდან"
        assert [p.kind for p in tax_phrases(text) if p.kind == "inclusion"] == []
        text = "მომსახურება გაწეულია 2026 წლის 30 აპრილის ჩათვლით დღგ-ს გადამხდელის მიერ"
        assert [p.kind for p in tax_phrases(text) if p.kind == "inclusion"] == []


# --- AOA-17: the fee stated with its VAT broken out ----------------------------------------------


class TestVatBreakdown:
    def test_net_plus_vat_making_the_price_is_the_price(self) -> None:
        for fee in (
            "საფასური დღგ-ს გარეშე შეადგენს 84.75 ლარს, დღგ - 15.25 ლარს, სულ დღგ-ს ჩათვლით "
            "100 (ასი) ლარს.",
            "საფასური დღგ-ს გარეშე შეადგენს 84.75 ლარს. დღგ შეადგენს 15.25 ლარს. სულ დღგ-ს "
            "ჩათვლით საფასური შეადგენს 100 (ასი) ლარს.",
            "საფასური დღგ-ს გარეშე შეადგენს 84.75 ლარს, დღგ - 15.25 ლარს.",
        ):
            report = _check(ACT.replace(_FEE_TAX, fee))
            assert _of(report, "AOA.FEE") == [], fee
            assert _substantive(report) == [], fee

    def test_the_breakdown_is_the_vat_entrepreneur_s_wording(self) -> None:
        fee = (
            "საფასური დღგ-ს გარეშე შეადგენს 84.75 ლარს, დღგ - 15.25 ლარს, სულ დღგ-ს ჩათვლით "
            "100 (ასი) ლარს."
        )
        report = _check(ACT.replace(_FEE_TAX, fee), contractor_type="individual_entrepreneur_vat")
        assert _substantive(report) == []

    def test_parts_that_do_not_add_up_are_still_compared(self) -> None:
        """The fee and the net are right (100 at 18% is 84.75 before VAT): the VAT amount is the
        slip, and the finding is on it (red team R3, AOA-R3-08)."""
        fee = (
            "საფასური დღგ-ს გარეშე შეადგენს 84.75 ლარს, დღგ - 15.00 ლარს, სულ დღგ-ს ჩათვლით "
            "100 (ასი) ლარს."
        )
        [finding] = _of(_check(ACT.replace(_FEE_TAX, fee)), "AOA.FEE")
        assert (finding.actual, finding.expected) == ("15.00 GEL", "15.25 ლარს")


# --- AOA-19: the fee's correction keeps the currency and tetri words in the brackets -------------


class TestFeeCorrectionWords:
    def test_currency_and_tetri_words_are_kept(self) -> None:
        act = ACT.replace("100 (ასი) ლარს", "700.00 (შვიდასი ლარი და 00 თეთრი)")
        [finding] = _of(_check(act), "AOA.FEE")
        assert finding.expected == "100.00 (ასი ლარი და 00 თეთრი)"

    def test_the_currency_word_alone_is_kept(self) -> None:
        act = ACT.replace("100 (ასი) ლარს", "700 (შვიდასი ლარი)")
        [finding] = _of(_check(act), "AOA.FEE")
        assert finding.expected == "100 (ასი ლარი)"

    def test_the_words_with_tetri_follow_the_correct_amount(self) -> None:
        from procurement_agent.checks.amounts import words_like

        assert words_like(100.5, "შვიდასი ლარი და 00 თეთრი") == "ასი ლარი და 50 თეთრი"
        assert words_like(100, "შვიდასი ლარი და ოცი თეთრი") == "ასი ლარი და ნული თეთრი"
        assert words_like(100.2, "შვიდასი ლარი და ოცი თეთრი") == "ასი ლარი და ოცი თეთრი"
        assert words_like(100.5, "შვიდასი") == "ასი ლარი და 50 თეთრი"
        assert words_like(100, "შვიდასი აშშ დოლარი") == "ასი აშშ დოლარი"


# --- AOA-09: a name copied from the other party is marked in this party's places only ------------

#: The act with the contractor's director named in its preamble too.
_TWO_DIRECTORS = ACT.replace(
    "ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“)",
    "ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის დავით მელაძის სახით",
)
#: The contractor's director written as the customer's, in the preamble and the signature.
_COPIED = _TWO_DIRECTORS.replace("დავით მელაძის", "ნინო კაპანაძის").replace(
    "დავით მელაძე", "ნინო კაპანაძე"
)


def _annotated(tmp_path: object, act: str, findings: list[Finding], *, text: bool) -> list[str]:
    """The paragraphs of the act (one per line) that carry a comment, as annotation leaves
    them, and the words each comment covers."""
    from pathlib import Path

    from docx import Document

    from procurement_agent.eval.score import comment_anchors
    from procurement_agent.report.docx_annotate import annotate_docx

    folder = Path(str(tmp_path))
    doc = Document()
    for line in act.splitlines():
        doc.add_paragraph(line)
    src = folder / "act.docx"
    doc.save(src)
    out = folder / "annotated.docx"
    annotate_docx(src, findings, out, text=act if text else None)
    return sorted(comment_anchors(out).values())


class TestCopiedDirector:
    def test_the_pair_with_both_directors_is_clean(self) -> None:
        assert _substantive(_check(_TWO_DIRECTORS)) == []

    def test_each_wrong_form_is_one_finding_at_the_contractor_s_place(self) -> None:
        report = _check(_COPIED)
        found = sorted((f.quote, f.expected) for f in _of(report, "AOA.DIRECTOR"))
        assert found == [
            ("ნინო კაპანაძე", "დავით მელაძე"),
            ("ნინო კაპანაძის", "დავით მელაძის"),
        ]
        contractor_from = _COPIED.index("შპს „ბეტა სერვისი“")
        for f in _of(report, "AOA.DIRECTOR"):
            assert f.span is not None and f.span[0] > contractor_from, f.quote

    def test_only_the_contractor_s_places_are_marked(self, tmp_path: object) -> None:
        findings = _of(_check(_COPIED), "AOA.DIRECTOR")
        for text in (False, True):
            marked = _annotated(tmp_path, _COPIED, findings, text=text)
            assert marked == ["ნინო კაპანაძე", "ნინო კაპანაძის"], text

    def test_a_misspelt_name_is_still_marked_everywhere_it_stands(self, tmp_path: object) -> None:
        """A slip that is nobody's name is wrong at every place: the preamble and signature."""
        act = _TWO_DIRECTORS.replace("დავით მელაძე", "დავით მელაშვილი").replace(
            "დავით მელაძის", "დავით მელაშვილის"
        )
        signed = "დირექტორი: დავით მელაშვილი"
        act = act.replace(signed, f"{signed}\nდავით მელაშვილი")
        findings = _of(_check(act), "AOA.DIRECTOR")
        assert all(f.places is None for f in findings)
        marked = _annotated(tmp_path, act, findings, text=False)
        assert marked == ["დავით მელაშვილი", "დავით მელაშვილი", "დავით მელაშვილის"]


# --- AOA-15: an act the rules cannot read says so, rather than calling its codes missing ---------

_ENGLISH_ACT = """ACCEPTANCE ACT
Northwind Ltd, identification code 123456789 (the "Customer"), and Blue Harbour Ltd,
identification code 987654321 (the "Contractor"), under the Agreement for the Customer's project
"Hill Garden" (cadastral code 01.02.03.456), state the following:
1. The Contractor has rendered the Services under the Agreement.
2. The fee is GEL 100.00 including VAT, paid to SWIFT: AAAAGE22; IBAN: GE29NB0000000101904917.
3. This Act was signed on 30 April 2026.
"""


class TestActNotInGeorgian:
    def test_no_codes_error_on_an_act_in_english(self) -> None:
        report = _check(_ENGLISH_ACT)
        assert _of(report, "AOA.ID_CODES") == []
        [note] = [s for s in report.skipped if s.rule_id == "AOA.ID_CODES"]
        assert "Georgian" in note.reason_en

    def test_what_the_rules_cannot_read_is_a_note_not_a_finding(self) -> None:
        """No finding at all on the act: the project and the period it states are not missing,
        they are written in words the rules do not read, and each rule says so."""
        report = _check(_ENGLISH_ACT)
        assert _substantive(report) == []
        skipped = {s.rule_id for s in report.skipped}
        assert {"AOA.ID_CODES", "AOA.PROJECT_PRESENT", "AOA.PERIOD"} <= skipped

    def test_a_bilingual_act_is_read_as_georgian(self) -> None:
        """Each line followed by its English rendering, longer than the Georgian: the rules read
        the Georgian half, and a project missing from it is still a warning."""
        english = "The same clause in English: the parties, the services, the fee and bank details"
        act = ACT.replace("პროექტ „მზე“ (ს/კ: 01.02.03.456)", "მომსახურების")
        bilingual = "\n".join(f"{line}\n{english}" for line in act.splitlines())
        report = _check(bilingual)
        assert [f.severity for f in _of(report, "AOA.PROJECT_PRESENT")] == ["warning"]
        assert all(s.rule_id != "AOA.PROJECT_PRESENT" for s in report.skipped)

    def test_a_georgian_act_without_a_period_still_warns(self) -> None:
        act = ACT.replace(
            "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით.\n", ""
        )
        assert [f.severity for f in _of(_check(act), "AOA.PERIOD")] == ["warning"]

    def test_a_georgian_act_without_codes_is_still_an_error(self) -> None:
        act = ACT.replace("საიდენტიფიკაციო კოდი: 123456789; ", "").replace(
            "საიდენტიფიკაციო კოდი: 987654321, ", ""
        )
        [finding] = _of(_check(act), "AOA.ID_CODES")
        assert finding.severity == "error"
        assert all(s.rule_id != "AOA.ID_CODES" for s in _check(act).skipped)


# --- the messages print values as text, never as Python lists ------------------------------------

_PYTHON_SYNTAX = re.compile(r"\['|'\]|\{'|frozenset\(|set\(\)")


class TestMessagesArePlainText:
    def test_values_compared_with_several_are_written_as_text(self) -> None:
        contract = _BOTH_BANKS.replace("(ს/კ: 01.02.03.456)", "(ს/კ: 01.02.03.456 და 01.02.03.457)")
        acts = (
            ACT.replace("GE29NB0000000101904917", "GE29NB0000000101904971"),
            ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAABGE22;"),
            ACT.replace("(ს/კ: 01.02.03.456)", "(ს/კ: 01.02.03.475)"),
            ACT.replace("GE29NB0000000101904917", "GE11CC0000000123456789"),
        )
        for act in acts:
            report = _check(act, contract)
            assert _substantive(report), act
            for f in report.findings:
                for message in (f.message_ka, f.message_en):
                    assert not _PYTHON_SYNTAX.search(message), message
            for s in report.skipped:
                for reason in (s.reason_ka, s.reason_en):
                    assert not _PYTHON_SYNTAX.search(reason), reason

    def test_the_bank_message_names_the_act_s_value_and_the_contract_s(self) -> None:
        act = ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAABGE22;")
        [finding] = _of(_check(act, _BOTH_BANKS), "AOA.BANK")
        assert finding.message_en == "bank code differs: AAABGE22 vs contract AAAAGE22, CCCCGE22"
