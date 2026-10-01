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

from procurement_agent.checks.boq import (
    BidComparison,
    _label_key,
    _plays,
    compare_all,
    read_boq,
    template_findings,
)
from tests.unit.test_boq_reader_round4 import (
    _COST,
    _MARKUP,
    _SHEET,
    _bill,
    _compare,
    _summary,
)
from tests.unit.test_boq_reader_variants import Cells, Fx, _rules, _save

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


# --- R5-03: the total incl. VAT worded "with VAT" the Georgian way ----------------------------


@pytest.mark.parametrize(
    "label",
    [
        "ჯამი დღგ-ით",
        "სულ დღგ-ით",
        "სულ დღგით",
        "საერთო ღირებულება დღგ-თ",
        "სულ ღირებულება დღგ-ს გათვალისწინებით",
        "სულ დღგ-ს შეტანით",
        "ჯამი დამატებული ღირებულების გადასახადით",
        "სულ ჯამი დღგ-ს ჩათვლით",
    ],
)
def test_the_georgian_with_vat_is_the_total_incl_vat_never_the_vat_row(label: str) -> None:
    assert _plays("incl", label)
    assert not _plays("vat", label)
    assert not _plays("excl", label)


@pytest.mark.parametrize("label", ["სულ დღგ-ს გამოკლებით", "ჯამი დღგ-ს გარეშე"])
def test_the_georgian_vat_taken_off_is_the_total_excl_vat(label: str) -> None:
    assert _plays("excl", label)
    assert not _plays("vat", label) and not _plays("incl", label)


@pytest.mark.parametrize(
    "label",
    [
        "დღგ 18%",  # the VAT row
        "დღგ-ს თანხა",  # the VAT's amount
        "დღგ-თან ერთად გადასახდელი ავანსი",  # another word the tax's name begins
        "დღგ-ის გადამხდელი",  # a VAT payer
    ],
)
def test_a_vat_label_that_is_no_total_incl_vat_is_not_read_as_one(label: str) -> None:
    assert not _plays("incl", label)


def _ka_summary(cost: float, vat_label: str, incl_label: str, *, incl_off: float = 0.0) -> Cells:
    """The round-4 Summary with its VAT and incl.-VAT rows labelled as given, the incl. total
    ``incl_off`` off what it adds up to."""
    cells = _summary(cost)
    cells[(7, 2)] = vat_label
    cells[(8, 2)] = incl_label
    if incl_off:
        cells[(8, 3)] = Fx("=C6+C7+1", cost * 1.15 * 1.18 + incl_off)
    return cells


def _ka_compare(
    tmp_path: Path, vat_label: str, incl_label: str, *, incl_off: float = 0.0
) -> list[BidComparison]:
    books = []
    for name, factor in (("T", None), ("A", 1.0), ("B", 0.9)):
        bill, total = _bill(factor)
        off = incl_off if name == "A" else 0.0
        summary = _ka_summary(total, vat_label, incl_label, incl_off=off)
        path = _save(tmp_path / f"{name}.xlsx", {"SUMMARY": summary, _SHEET: bill})
        books.append(read_boq(path, name=name))
    return compare_all(books[0], books[1:])


@pytest.mark.parametrize(
    ("vat_label", "incl_label"),
    [
        ("დღგ 18%", "ჯამი დღგ-ით"),
        ("დღგ 18%", "სულ დღგ-ით"),
        ("დღგ 18%", "სულ ღირებულება დღგ-ს გათვალისწინებით"),
        ("დღგ 18%", "სულ ჯამი დღგ-ს ჩათვლით"),
        ("გადასახადი 18%", "ჯამი დღგ-ით"),  # a tax row the chain does not name as the VAT
    ],
)
def test_a_summary_closing_with_the_georgian_with_vat_is_ranked(
    tmp_path: Path, vat_label: str, incl_label: str
) -> None:
    a, b = _ka_compare(tmp_path, vat_label, incl_label)
    for comp in (a, b):
        assert comp.markup_factor == pytest.approx(_MARKUP)
        assert not {"BOQ.NOT_RANKED", "BOQ.RECONCILE"} & set(_rules(comp))
    assert (a.colour, b.colour) == ("red", "green")


def test_a_wrong_total_with_vat_after_an_unnamed_tax_row_is_still_reported(
    tmp_path: Path,
) -> None:
    a, b = _ka_compare(tmp_path, "გადასახადი 18%", "ჯამი დღგ-ით", incl_off=1.0)
    [gap] = [f for f in a.findings if f.rule_id == "BOQ.RECONCILE"]
    assert "row 8" in gap.message_en
    assert "BOQ.RECONCILE" not in _rules(b)
