"""Red team round 3: dates, periods and whose date is whose, each reproduced with invented text.

AOA-R3-02, AOA-R3-10, COMPARE-R3-02, COMPARE-R3-03 and EXTRACTION-R3-05. Synthetic text only:
every name, code and date here is invented.
"""

from __future__ import annotations

from datetime import date

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.extraction.georgian import render_date_like
from procurement_agent.extraction.rules import find_dates
from tests.unit.test_aoa_check import ACT, CONTRACT

_SIGNED = "აქტი გაფორმდა 2026 წლის 30 აპრილი"


def _act_date_finding(act_date: str) -> tuple[str | None, str | None]:
    act = ACT.replace(_SIGNED, f"აქტი გაფორმდა {act_date}")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.ACT_DATE"]
    assert finding.severity == "error"
    return finding.actual, finding.expected


# --- AOA-R3-10: a four-digit numeric date keeps its year word -----------------------------------


@pytest.mark.parametrize(
    "form",
    ["05.07.2026 წ.", "05.07.2026წ.", "05.07.2026 წელს", "05.07.2026 წლის", "5/7/2026 წელი"],
)
def test_a_numeric_dates_year_word_is_part_of_it(form: str) -> None:
    [hit] = find_dates(f"აქტი გაფორმდა {form}, თბილისში")
    assert (hit.normalized, hit.raw_text) == ("2026-07-05", form)
    assert render_date_like(date(2026, 7, 5), hit.raw_text) == form


@pytest.mark.parametrize(
    ("text", "raw"),
    [
        ("05.07.2026 წინ", "05.07.2026"),  # a word that only starts like the year word
        ("05.07.2026 წლიდან", "05.07.2026"),  # "from the year": a postposition, read after it
        ("05.07.2026 წყალი", "05.07.2026"),
    ],
)
def test_a_word_that_is_no_year_word_stays_out_of_a_numeric_date(text: str, raw: str) -> None:
    [hit] = find_dates(text)
    assert hit.raw_text == raw


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("05.05.2026 წ.", "30.04.2026 წ."),
        ("05.05.2026წ.", "30.04.2026წ."),
        ("05.05.2026 წელს", "30.04.2026 წელს"),
    ],
)
def test_the_act_date_quote_and_correction_keep_the_year_word(written: str, expected: str) -> None:
    assert _act_date_finding(written) == (written, expected)
