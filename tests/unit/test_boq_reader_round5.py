"""BOQ bills the fifth red-team round found the reader got wrong.

Each test is one such way: a header spelled the other usual Georgian ways ("რაოდ-ბა", a word
hyphenated over two lines, "ერთ.ფასი", "საერთო თანხა"), a total incl. VAT worded "დღგ-ით", a
line the template leaves blank that the bidder filled in without a price, a template quantity
typed as text, a heading between a section and items numbered under another, item numbers with
a closing dot, Summary sections numbered I, II, III, a priced placeholder in the merged
workbook's excluded block, a chain rate typed left of the Summary's TOTAL, and the policies the
lead decided for round 5. Every workbook is synthetic and built in memory; every expected number
is worked out here from the test's own data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from procurement_agent.checks.boq import _label_key, template_findings
from tests.unit.test_boq_reader_round4 import _COST, _SHEET, _compare

# --- R5-04, R5-05, R5-12: the header's other usual Georgian spellings ---------------------


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("რაოდ-ბა", "qty"),
        ("რაოდე-\nნობა", "qty"),  # hyphenated across a line break
        ("რაოდენ.", "qty"),
        ("რაოდ-ობა", "qty"),
        ("რ-ბა", "qty"),
        ("რაოდ.", "qty"),
        ("ერთ.ფასი", "rate_total"),  # no space after the abbreviation's dot
        ("ერთ. ღირებულება", "rate_total"),
        ("ერთ.ღირებულება", "rate_total"),
        ("ერთეულის ფასი", "rate_total"),
        ("საერთო თანხა", "amount"),
        ("ღირებულება სულ", "amount"),
        ("თანხა სულ", "amount"),
        ("სულ თანხა", "amount"),
        ("საერთო ღირებულება", "amount"),
        ("U.o.M.", "unit"),  # an abbreviation spelt with dots is still the unit
    ],
)
def test_a_georgian_header_spelled_the_other_usual_ways_is_read(label: str, key: str) -> None:
    assert _label_key(label) == key


@pytest.mark.parametrize(
    "label",
    [
        "რ",  # a bare letter names no column
        "რაოდენობის შენიშვნა",  # a note about the quantity
        "Description -\nUnit",  # a hyphen standing apart joins no word: two columns named
        "ფასი",  # bound only by where it stands
        "ერთეულის",  # a unit word with no price word
        "ღირებულების შენიშვნა",
    ],
)
def test_a_near_miss_header_is_not_read_as_quantity_rate_or_amount(label: str) -> None:
    assert _label_key(label) is None


def test_the_bare_total_word_stays_the_amount_and_the_unit_abbreviation_the_unit() -> None:
    # the near misses above that do name a column name the one they always did
    assert _label_key("სულ") == "amount"
    assert _label_key("ერთ.") == "unit"


@pytest.mark.parametrize(
    "head",
    [
        ("№", "სამუშაოს დასახელება", "ერთ.", "რაოდ-ბა", "ერთ. ფასი", "ღირებულება"),
        ("№", "სამუშაოს დასახელება", "ერთ.", "რაოდე-\nნობა", "ერთ. ფასი", "ღირებულება"),
        ("№", "სამუშაოს დასახელება", "ერთ.", "რაოდენობა", "ერთ.ფასი", "ღირებულება"),
        ("№", "სამუშაოს დასახელება", "ერთ.", "რაოდენობა", "ერთ. ღირებულება", "სულ ღირებულება"),
        ("№", "სამუშაოს დასახელება", "ერთ.", "რაოდენობა", "ერთეულის ფასი", "საერთო თანხა"),
        ("№", "სამუშაოს დასახელება", "ერთ.", "რაოდენობა", "ერთეულის ფასი", "ღირებულება სულ"),
    ],
)
def test_a_bill_headed_so_is_evaluated_at_its_rate_and_its_slip_reported(
    tmp_path: Path, head: tuple[str, ...]
) -> None:
    t, (a, b) = _compare(tmp_path, {"A": (1.0, {"1.4": 2000.0}), "B": (0.9, None)}, head=head)
    assert t.sheets[_SHEET].columns == {
        "no": 1,
        "desc": 2,
        "unit": 3,
        "qty": 4,
        "rate_total": 5,
        "amount": 6,
    }
    rules = [f.rule_id for f in template_findings(t)]
    assert "BOQ.SHEET_UNREADABLE" not in rules and "BOQ.AMOUNTS_NOT_READ" not in rules
    [slip] = [f for f in a.findings if f.rule_id == "BOQ.ARITHMETIC"]
    assert (slip.actual, slip.expected) == ("8,090.00", "6,090.00")
    # evaluated at the rate, never at the slipped amount / quantity
    assert a.evaluated_cost == pytest.approx(_COST)
    assert b.evaluated_cost == pytest.approx(_COST * 0.9)
    assert (a.colour, b.colour) == ("red", "green")
