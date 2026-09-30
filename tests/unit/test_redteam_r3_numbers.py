"""EXTRACTION-R3-11: a figure's thousands separated by a thin space, a narrow no-break space, a
figure space or an apostrophe. The amount reader knew only the space and the no-break space, so
"36 000" with a thin space was read from its last group: a total of 0. Invented figures only.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.amounts import digits_and_words
from procurement_agent.extraction.normalize import parse_number
from procurement_agent.extraction.rules import find_amounts
from procurement_agent.extraction.schemas import FactType
from tests.unit.test_extraction_redteam_r2 import _extract

_SEPARATORS = {
    "thin space": "\u2009",
    "narrow no-break space": "\u202f",
    "figure space": "\u2007",
    "apostrophe": "'",
    "right single quote": "\u2019",
    "no-break space (control)": "\u00a0",
}
_CLAUSE = "4.1. მომსახურების ღირებულება შეადგენს {} ლარს გადასახადების ჩათვლით."


@pytest.mark.parametrize("sep", _SEPARATORS.values(), ids=_SEPARATORS.keys())
def test_a_grouped_total_is_read_whole(sep: str) -> None:
    assert _extract(_CLAUSE.format(f"48{sep}500"))[FactType.total_amount] == ["48500.0"]
    assert _extract(_CLAUSE.format(f"48{sep}500.00"))[FactType.total_amount] == ["48500.0"]
    assert _extract(_CLAUSE.format(f"1{sep}048{sep}500,50"))[FactType.total_amount] == ["1048500.5"]


@pytest.mark.parametrize("sep", _SEPARATORS.values(), ids=_SEPARATORS.keys())
def test_parse_number_drops_every_group_separator(sep: str) -> None:
    assert parse_number(f"48{sep}500") == 48500.0
    assert parse_number(f"1{sep}048{sep}500.25") == 1048500.25


@pytest.mark.parametrize("sep", ["\u2009", "'"])
def test_a_figure_never_starts_inside_another(sep: str) -> None:
    """The near miss: a malformed group ("48'50") is no amount, never its last group read as one."""
    values = [h.normalized for h in find_amounts(_CLAUSE.format(f"48{sep}50"))]
    assert 50.0 not in values
    assert 0.0 not in values


def test_a_quoted_figure_is_one_figure() -> None:
    """Apostrophes around a figure are quotes, not a group separator."""
    values = [h.normalized for h in find_amounts("ფასი '500' ლარი, ანუ 500 ლარი.")]
    assert values == [500.0]


@pytest.mark.parametrize("sep", ["\u00a0", "\u2009"])
def test_a_count_before_a_price_is_still_cut_off(sep: str) -> None:
    """After a quantity keyword "5 100 ლარი" is a count and a price; a thin space between them
    reads as the no-break space does."""
    [hit] = find_amounts(f"რაოდენობა 5{sep}100 ლარი")
    assert hit.normalized == 100.0


def test_the_act_check_reads_an_apostrophe_grouped_fee_with_its_words() -> None:
    [pair] = digits_and_words("საფასური შეადგენს 3'450 (სამი ათას ოთხას ორმოცდაათი) ლარს.")
    assert pair.digits == 3450.0
