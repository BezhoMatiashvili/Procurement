"""Defects the third red-team round found in the act check's fee, signatory, period and date
rules.

Each test states the defect's id (``samples/redteam/aoa/CONFIRMED_R3.md``) and the behaviour the
spec asks for, and every widened reading has a near miss beside it that must still not match.
Synthetic text only: the pair is ``test_aoa_check``'s, with every name, code and account invented.
"""

from __future__ import annotations

from procurement_agent.checks.amounts import (
    digits_and_words,
    figure_value,
    money_hits,
    render_figure_like,
)
from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from tests.unit.test_aoa_check import ACT, CONTRACT


def _check(act: str = ACT, contract: str | None = CONTRACT, **kw: object) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract, **kw)  # type: ignore[arg-type]


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _substantive(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.severity in ("error", "warning")]


_FEE_TAX = "საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით."


def _fee(sentence: str, act: str = ACT) -> str:
    """The act with its fee sentence rewritten."""
    assert _FEE_TAX in act
    return act.replace(_FEE_TAX, sentence)


# --- AOA-R3-03: a fee with its tetri after a hyphen ----------------------------------------------

_WORDS = "(ათას ორას ორმოცდაათი)"
_PRICED = CONTRACT.replace("100 (ასი) ლარს", f"1 250 {_WORDS} ლარს")


class TestTetriAfterAHyphen:
    def test_the_hyphen_form_of_the_price_is_the_price(self) -> None:
        for fee in (f"1 250-00 {_WORDS} ლარს", f"1250-00 {_WORDS} ლარს", "1 250-00 ლარს"):
            report = _check(_fee(f"საფასური შეადგენს {fee} გადასახადების ჩათვლით."), _PRICED)
            assert _substantive(report) == [], fee

    def test_a_slip_is_quoted_whole_and_corrected_in_the_hyphen_style(self) -> None:
        act = _fee(f"საფასური შეადგენს 1 520-00 {_WORDS} ლარს გადასახადების ჩათვლით.")
        report = _check(act, _PRICED)
        [fee] = _of(report, "AOA.FEE")
        assert fee.quote == f"1 520-00 {_WORDS} ლარს"
        assert fee.expected == f"1 250-00 {_WORDS} ლარს"
        [words] = _of(report, "AOA.AMOUNT_WORDS")
        assert "1 520" in (words.quote or "")
        assert words.expected == "1 250-00"

    def test_the_figure_reads_with_its_tetri(self) -> None:
        assert figure_value("3 450-00") == 3450.0
        assert figure_value("3 450-50") == 3450.5
        assert [m.value for m in money_hits("შეადგენს 3 450-00 ლარს.")] == [3450.0]
        assert [m.value for m in money_hits("შეადგენს GEL 3 450-25.")] == [3450.25]
        assert render_figure_like(3450.5, "3 540-00") == "3 450-50"

    def test_a_range_is_no_tetri(self) -> None:
        """Near misses: a range whose second figure is larger, three digits after the hyphen, a
        fraction before it, a date's digits, and a range word after it."""
        assert figure_value("10-15") is None
        assert figure_value("3 450.50-00") is None
        assert [m.value for m in money_hits("ღირებულება 10-15 ლარი.")] == [15.0]
        assert [m.value for m in money_hits("ღირებულება 100-150 ლარი.")] == [150.0]
        assert [m.value for m in money_hits("ღირებულება 2026-06-30 ლარი.")] == [30.0]
        assert 90.4 not in [m.value for m in money_hits("ფასი 90-40 ლარამდე.")]

    def test_a_range_keeps_its_words_pair(self) -> None:
        [pair] = digits_and_words("ღირებულება 10-15 (თხუთმეტი) ლარი.")
        assert (pair.digits, pair.digits_raw, pair.spelled) == (15.0, "15", 15.0)


# --- AOA-R3-07: a signatory with an initial and a slip in the surname's last letter -------------

_CONT_SIGN = "დირექტორი: დავით მელაძე"
_CUST_SIGN = "დირექტორი ნინო კაპანაძე"


class TestInitialledSignatory:
    def test_an_initialled_name_written_right_is_clean(self) -> None:
        """The act's only form of the contractor's director, and the customer's beside its full
        genitive in the preamble, each with the first name as its initial."""
        for act in (
            ACT.replace(_CONT_SIGN, "დირექტორი: დ. მელაძე"),
            ACT.replace(_CUST_SIGN, "დირექტორი ნ. კაპანაძე"),
            ACT.replace("დირექტორის ნინო კაპანაძის", "დირექტორის ნ. კაპანაძის"),
        ):
            assert _of(_check(act), "AOA.DIRECTOR") == []

    def test_a_final_vowel_slip_after_an_initial_is_an_error(self) -> None:
        for act, quote, fix in (
            (ACT.replace(_CONT_SIGN, "დირექტორი: დ. მელაძი"), "დ. მელაძი", "დ. მელაძე"),
            (ACT.replace(_CONT_SIGN, "დირექტორი: დ.მელაძი"), "დ.მელაძი", "დ. მელაძე"),
            (ACT.replace(_CUST_SIGN, "დირექტორი ნ. კაპანაძი"), "ნ. კაპანაძი", "ნ. კაპანაძე"),
        ):
            [finding] = _of(_check(act), "AOA.DIRECTOR")
            assert (finding.severity, finding.quote, finding.expected) == ("error", quote, fix)

    def test_another_initial_is_not_the_director(self) -> None:
        act = ACT.replace(_CONT_SIGN, "დირექტორი: ა. მელაძე")
        [finding] = _of(_check(act), "AOA.DIRECTOR")
        assert (finding.quote, finding.expected) == ("ა. მელაძე", "დ. მელაძე")

    def test_an_initial_before_another_word_is_no_form_of_the_director(self) -> None:
        """Near miss: "ნ." before a word that is not the surname (an abbreviation in prose)."""
        act = ACT.replace(
            "4. გაფორმების თარიღი",
            "შენიშვნა: ნ. კატეგორია და დ. მეორე ეტაპი.\n4. გაფორმების თარიღი",
        )
        assert _of(_check(act), "AOA.DIRECTOR") == []


# --- mutation harness: a one-letter slip in the act's signing verb ------------------------------

_SIGNED = "აქტი გაფორმდა 2026 წლის 30 აპრილი"
#: the act with no "გაფორმების თარიღი" heading: its signing verb alone dates it
_UNLABELLED = ACT.replace("4. გაფორმების თარიღი\n", "4. დასკვნითი დებულება\n")


class TestSigningVerbSlip:
    def test_the_date_after_a_slipped_verb_is_the_acts(self) -> None:
        for slip in ("გაორმდა", "გაფორდა", "გაფორმდაა", "გაფრომდა"):
            act = _UNLABELLED.replace(_SIGNED, f"„აქტი“ {slip} 2026 წლის 30 აპრილი")
            report = _check(act)
            assert _of(report, "AOA.ACT_DATE") == [], slip
            assert report.summary["act_date"] == "2026-04-30"

    def test_a_slipped_verb_still_dates_a_wrong_day(self) -> None:
        act = _UNLABELLED.replace(_SIGNED, "აქტი გაორმდა 2026 წლის 20 აპრილი")
        [finding] = _of(_check(act), "AOA.ACT_DATE")
        assert (finding.quote, finding.expected) == ("2026 წლის 20 აპრილი", "2026 წლის 30 აპრილი")

    def test_other_words_are_no_signing(self) -> None:
        """Near misses: another form of the verb (will be signed), another verb, and a slip
        after the contract's name - the contract's date, never the act's."""
        for words in (
            "აქტი გაფორმდება 2026 წლის 30 აპრილი",
            "აქტი გააფორმეს 2026 წლის 30 აპრილი",
            "ხელშეკრულება გაორმდა 2026 წლის 30 აპრილი",
        ):
            report = _check(_UNLABELLED.replace(_SIGNED, words))
            assert report.summary.get("act_date") != "2026-04-30", words


# --- AOA-R3-11: a period no single year puts inside the term ------------------------------------

_PERIOD = "2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით"


class TestPeriodOutsideTheTerm:
    """The contract's term is 2026-03-01..2026-09-01; each period starts before it, and no year
    moved puts it inside."""

    def _finding(self, period: str) -> Finding:
        [finding] = _of(_check(ACT.replace(_PERIOD, period)), "AOA.PERIOD_IN_TERM")
        return finding

    def test_the_whole_period_is_quoted_and_the_term_written_in_its_style(self) -> None:
        f = self._finding("2026 წლის 20 თებერვლიდან 2026 წლის 30 აპრილის ჩათვლით")
        assert f.quote == "2026 წლის 20 თებერვლიდან 2026 წლის 30 აპრილის"
        assert f.expected == "2026 წლის 1 მარტიდან 2026 წლის 1 სექტემბრის"
        assert f.actual == f.quote

    def test_a_numeric_period_is_corrected_in_figures(self) -> None:
        f = self._finding("20.02.2026-დან 30.04.2026-ის ჩათვლით")
        assert (f.quote, f.expected) == ("20.02.2026-დან 30.04.2026", "01.03.2026-დან 01.09.2026")

    def test_a_month_with_no_day_gets_the_term_written_out(self) -> None:
        f = self._finding("2026 წლის თებერვლის თვეში")
        assert f.quote == "2026 წლის თებერვლის"
        assert f.expected == "2026 წლის 1 მარტიდან 2026 წლის 1 სექტემბრის ჩათვლით"

    def test_no_correction_is_iso(self) -> None:
        for period in (
            "2026 წლის 20 თებერვლიდან 2026 წლის 30 აპრილის ჩათვლით",
            "20.02.2026-დან 30.04.2026-ის ჩათვლით",
            "2026 წლის თებერვლის თვეში",
        ):
            assert ".." not in (self._finding(period).expected or "")

    def test_a_year_typed_wrong_is_still_corrected_in_that_year(self) -> None:
        """Near miss: when one year moved puts the period inside, that is the correction."""
        f = self._finding("2025 წლის 1 აპრილიდან 2025 წლის 30 აპრილის ჩათვლით")
        assert f.expected == "2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის"
