"""The independent BOQ gold writer (scripts/boq_reference.py) follows round 5's rules.

GOLD_SPEC 5 now says, among others: the Georgian "with VAT" is the total incl. VAT; a filled-in
blank line is priced only by its amount; a template quantity typed as text is evaluated for no
bidder; a heading ends a section group; "1." and "1" are one item number; a single-sheet price
list closed by its total incl. VAT is ranked on it; a hidden sheet the Summary does not link is
no section; the usual Georgian unit spellings are synonyms. The reference must write the same
gold, or the scores would measure a disagreement between two readers rather than the product.
Synthetic data only.
"""

from __future__ import annotations

import pytest

from tests.unit.test_boq_reference_round4 import ref


@pytest.mark.parametrize(
    "label",
    ["ჯამი დღგ-ით", "სულ დღგ-თ", "სულ ღირებულება დღგ-ს გათვალისწინებით", "სულ დღგ-ს ჩათვლით"],
)
def test_the_georgian_with_vat_is_the_total_incl_vat(label: str) -> None:
    assert ref.INCL_VAT.search(ref.fold(label))
    assert not ref.EXCL_VAT.search(ref.fold(label))


@pytest.mark.parametrize("label", ["სულ დღგ-ს გამოკლებით", "ჯამი დღგ-ს გარეშე"])
def test_the_georgian_vat_taken_off_is_the_total_excl_vat(label: str) -> None:
    assert ref.EXCL_VAT.search(ref.fold(label))
    assert not ref.INCL_VAT.search(ref.fold(label))


@pytest.mark.parametrize("label", ["დღგ 18%", "დღგ-ს თანხა", "დღგ-თან ერთად"])
def test_a_vat_label_that_is_no_total_is_not_read_as_one(label: str) -> None:
    assert not ref.INCL_VAT.search(ref.fold(label))
