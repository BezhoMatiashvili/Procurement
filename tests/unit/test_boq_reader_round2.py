"""BOQ bills laid out, worded or typed in ways the second red-team round found the reader missed.

Each test is one such way: a number typed as text with three decimals, the invitation to list
extra items standing in a group heading, a quantity taken from a take-off sheet, component
column groups (Materials: Rate | Amount), a Summary row worded another usual way, sections
closed by their own sub-totals under a grand total, columns headed "Rate in GEL", a unit
written in Georgian. Every workbook is synthetic and built in memory; every expected number is
worked out here from the test's own data.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.boq import norm_unit

# --- units ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("t", "ტნ"),
        ("day", "დღე"),
        ("day", "days"),
        ("set", "კომპლ."),
        ("set", "კომპლექტი"),
        ("set", "sets"),
        ("km", "კმ"),
        ("ha", "ჰა"),
        ("l", "ლ"),
        ("l", "litre"),
        ("l", "ლიტრი"),
        ("week", "weeks"),
    ],
)
def test_a_unit_spelled_the_georgian_or_the_plural_way_is_one_unit(a: str, b: str) -> None:
    assert norm_unit(a) == norm_unit(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("ls", "l"),  # a lump sum is not a litre
        ("set", "pcs"),
        ("item", "pcs"),
        ("km", "lm"),
        ("ha", "hr"),
        ("day", "month"),
        ("day", "week"),
        ("l", "lm"),
    ],
)
def test_units_of_different_measures_stay_apart(a: str, b: str) -> None:
    assert norm_unit(a) != norm_unit(b)
