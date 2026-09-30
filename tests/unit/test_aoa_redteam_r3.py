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
