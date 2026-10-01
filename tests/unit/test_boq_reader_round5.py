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
    BoqLine,
    BoqWorkbook,
    _label_key,
    _plays,
    _position_groups,
    compare_all,
    read_boq,
    template_findings,
)
from tests.unit.test_boq_reader_round4 import (
    _COST,
    _HEAD,
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


# --- R5-01, R5-06, R5-07: a blank line filled in without a price, a quantity typed as text ---

#: (item, description, unit, quantity, rate, amount) - a cell left None stays empty; a number
#: amount is written as the row's own product with that cached value, text as it is typed; a
#: line with no unit is a heading, its description alone written
_Line = tuple[str, str, str, object, object, object]


def _cells(lines: tuple[_Line, ...], *, template: bool = False) -> tuple[Cells, float]:
    cells: Cells = {(1, c): h for c, h in enumerate(_HEAD, 1)}
    total = 0.0
    for r, (item, desc, unit, qty, rate, amount) in enumerate(lines, 2):
        if not unit:
            cells.update({(r, 2): desc, **({(r, 1): item} if item else {})})
            continue
        cells.update({(r, 1): item, (r, 2): desc, (r, 3): unit})
        for c, value in ((4, qty), (5, rate)):
            if value is not None:
                cells[(r, c)] = value
        if template or isinstance(amount, float):
            value = 0.0 if template else amount
            cells[(r, 6)] = Fx(f"=D{r}*E{r}", value)
            total += value  # type: ignore[operator]
        elif amount is not None:
            cells[(r, 6)] = amount
    last = len(lines) + 1
    cells[(last + 1, 2)] = "Sub-total"
    cells[(last + 1, 6)] = Fx(f"=SUM(F2:F{last})", total)
    return cells, total


def _evaluate(
    tmp_path: Path, template: tuple[_Line, ...], bids: dict[str, tuple[_Line, ...]]
) -> tuple[BoqWorkbook, list[BidComparison]]:
    books = []
    for name, lines in {"T": template, **bids}.items():
        bill, total = _cells(lines, template=name == "T")
        closing = len(lines) + 2
        path = _save(
            tmp_path / f"{name}.xlsx", {"SUMMARY": _summary(total, closing=closing), _SHEET: bill}
        )
        books.append(read_boq(path, name=name))
    return books[0], compare_all(books[0], books[1:])


def _status(comp: BidComparison, item: str) -> tuple[str, float, bool]:
    [p] = [p for p in comp.positions if p.item_no == item]
    return p.status, p.cost, p.in_common_scope


_DIG = ("1.1", "Trench excavation", "m3", 10.0, None, None)
_SUPERVISION = ("1.2", "Site supervision, months as proposed", "month", None, None, None)


def test_a_blank_line_filled_in_with_a_quantity_but_no_price_is_not_priced(tmp_path: Path) -> None:
    _, (a, b) = _evaluate(
        tmp_path,
        (_DIG, _SUPERVISION),
        {
            "A": (
                ("1.1", "Trench excavation", "m3", 10.0, 110.0, 1100.0),
                (*_SUPERVISION[:3], 6.0, None, None),
            ),
            "B": (
                ("1.1", "Trench excavation", "m3", 10.0, 100.0, 1000.0),
                (*_SUPERVISION[:3], 6.0, 3000.0, 18000.0),
            ),
        },
    )
    assert _status(a, "1.2") == ("unpriced", 0.0, False)
    assert _status(b, "1.2") == ("filled_blank", 18000.0, False)
    assert a.common_scope_positions == 1
    assert a.common_scope_cost == pytest.approx(1100.0)
    assert b.common_scope_cost == pytest.approx(1000.0)
    assert (a.colour, b.colour) == ("red", "green")
    # listed as filled in, but said to carry no price - and the bid is not complete on it
    [filled] = a.filled_blank
    assert (filled.bid_qty, filled.amount, filled.priced) == (6.0, 0.0, False)
    [note] = [f for f in a.findings if f.rule_id == "BOQ.FILLED_BLANK"]
    assert "no price" in note.message_en and "own amount" not in note.message_en
    assert [t.item_no for t in a.unpriced_positions] == ["1.2"]
    assert not a.complete and b.complete


def test_a_blank_line_filled_in_and_priced_is_evaluated_at_its_amount(tmp_path: Path) -> None:
    # near miss: an amount, or a quantity and a rate, is a price
    _, (a, b) = _evaluate(
        tmp_path,
        (_DIG, _SUPERVISION),
        {
            "A": (
                ("1.1", "Trench excavation", "m3", 10.0, 110.0, 1100.0),
                (*_SUPERVISION[:3], None, None, 9000.0),
            ),
            "B": (
                ("1.1", "Trench excavation", "m3", 10.0, 100.0, 1000.0),
                (*_SUPERVISION[:3], 6.0, 2500.0, None),
            ),
        },
    )
    assert _status(a, "1.2") == ("filled_blank", 9000.0, True)
    assert _status(b, "1.2") == ("filled_blank", 15000.0, True)
    assert a.complete and b.complete
    assert all(d.priced for d in (*a.filled_blank, *b.filled_blank))


_PLASTER = ("1.2", "Cement plaster to walls", "m2", None, None, None)


def test_text_in_the_rate_or_amount_of_a_filled_in_blank_line_leaves_it_unpriced(
    tmp_path: Path,
) -> None:
    dig = ("1.1", "Trench excavation", "m3", 10.0)
    _, (a, b, c) = _evaluate(
        tmp_path,
        (_DIG, _PLASTER),
        {
            "A": ((*dig, 100.0, 1000.0), (*_PLASTER[:3], 420.0, "incl.", 4095.0)),
            "B": ((*dig, 100.0, 1000.0), (*_PLASTER[:3], 420.0, 9.75, "n/a")),
            "C": ((*dig, 95.0, 950.0), (*_PLASTER[:3], 420.0, 9.25, 3885.0)),
        },
    )
    assert _status(a, "1.2") == ("non_numeric", 0.0, False)
    assert _status(b, "1.2") == ("non_numeric", 0.0, False)
    assert _status(c, "1.2") == ("filled_blank", 3885.0, False)
    assert (a.evaluated_cost, b.evaluated_cost) == (pytest.approx(1000.0), pytest.approx(1000.0))
    assert c.evaluated_cost == pytest.approx(950.0 + 3885.0)
    assert a.common_scope_positions == 1
    assert (a.colour, b.colour, c.colour) == ("red", "red", "green")
    # the bidder's own typed amount is what is listed, never one rebuilt beside text
    assert [(d.amount, d.priced) for d in a.filled_blank] == [(4095.0, False)]
    assert [(d.amount, d.priced) for d in b.filled_blank] == [(0.0, False)]
    for comp in (a, b):
        assert "BOQ.NON_NUMERIC" in _rules(comp)
        assert not comp.complete


def test_a_template_quantity_typed_as_text_is_evaluated_for_no_bidder(tmp_path: Path) -> None:
    rebar = ("1.2", "Reinforcement B500", "t")
    t, (a, b) = _evaluate(
        tmp_path,
        (_DIG, (*rebar, "2.4 t", None, None)),
        {
            "A": (
                ("1.1", "Trench excavation", "m3", 10.0, 110.0, 1100.0),
                (*rebar, "2.4 t", None, 3480.0),
            ),
            "B": (
                ("1.1", "Trench excavation", "m3", 10.0, 100.0, 1000.0),
                (*rebar, 3.0, 1450.0, 4350.0),
            ),
        },
    )
    for comp in (a, b):
        assert _status(comp, "1.2") == ("non_numeric", 0.0, False)
        assert not comp.filled_blank and not comp.unpriced_positions
    assert (a.evaluated_cost, b.evaluated_cost) == (pytest.approx(1100.0), pytest.approx(1000.0))
    assert (a.colour, b.colour) == ("red", "green")
    [note] = [f for f in template_findings(t) if f.rule_id == "BOQ.NON_NUMERIC"]
    assert "not evaluated for any bidder" in note.message_en


def test_a_template_quantity_typed_as_a_number_in_text_is_evaluated(tmp_path: Path) -> None:
    # near miss: text that reads one way is the number
    rebar = ("1.2", "Reinforcement B500", "t")
    t, (a, b) = _evaluate(
        tmp_path,
        (_DIG, (*rebar, "2.5", None, None)),
        {
            "A": (
                ("1.1", "Trench excavation", "m3", 10.0, 110.0, 1100.0),
                (*rebar, "2.5", 1500.0, 3750.0),
            ),
            "B": (
                ("1.1", "Trench excavation", "m3", 10.0, 100.0, 1000.0),
                (*rebar, "2.5", 1400.0, 3500.0),
            ),
        },
    )
    assert _status(a, "1.2") == ("priced", 3750.0, True)
    assert _status(b, "1.2") == ("priced", 3500.0, True)
    assert "BOQ.NON_NUMERIC" not in [f.rule_id for f in template_findings(t)]


# --- R5-02: a heading between ends a section group ---------------------------------------------


def _line(row: int, item: str, desc: str, kind: str = "position") -> BoqLine:
    return BoqLine(_SHEET, row, item, desc, "m3" if kind == "position" else None, 1.0, kind=kind)


@pytest.mark.parametrize("heading_item", ["", "2", "B"])
def test_a_heading_numbered_otherwise_ends_the_section_group(heading_item: str) -> None:
    lines = [
        _line(3, "A1", "Mobilisation"),
        _line(4, "A2", "Insurance"),
        _line(5, heading_item, "2. EARTHWORKS", "heading"),
        _line(6, "2.1", "Excavation"),
        _line(7, "2.2", "Backfilling"),
    ]
    groups = _position_groups(lines)
    assert groups[4] == "A2"
    assert (groups[6], groups[7]) == ("r6", "r7")


def test_a_heading_numbered_under_the_section_keeps_the_group() -> None:
    # near miss: the section's own inner heading ("9.1") and its items stay one group
    lines = [
        _line(3, "A9", "Temporary works"),
        _line(4, "9.1", "Hoarding", "heading"),
        _line(5, "9.1.1", "Hoarding panels"),
        _line(6, "9.2", "Site office"),
    ]
    groups = _position_groups(lines)
    assert [groups[r] for r in (3, 5, 6)] == ["A9", "A9", "A9"]


def test_an_item_unpriced_under_its_own_heading_is_listed_and_out_of_the_common_scope(
    tmp_path: Path,
) -> None:
    head = (
        ("A1", "Mobilisation", "LS", 1.0),
        ("A2", "Insurance", "LS", 1.0),
    )
    earth = (("2.1", "Excavation", "m3", 300.0), ("2.2", "Backfilling", "m3", 180.0))
    last = ("2.3", "Disposal of surplus", "m3", 120.0)

    def lines(rates: tuple[float, ...], unpriced_last: bool) -> tuple[_Line, ...]:
        out: list[_Line] = []
        for (item, desc, unit, qty), rate in zip((*head, *earth), rates, strict=True):
            out.append((item, desc, unit, qty, rate, qty * rate))
        out.insert(2, ("", "2. EARTHWORKS", "", None, None, None))
        tail = (None, None) if unpriced_last else (15.0, last[3] * 15.0)
        out.append((*last, *tail))
        return tuple(out)

    template = tuple((*x[:4], None, None) for x in lines((1, 1, 1, 1), True))
    _, (x, y) = _evaluate(
        tmp_path,
        template,
        {"X": lines((3500, 1200, 12.6, 9.975), True), "Y": lines((3500, 1200, 12.0, 9.5), False)},
    )
    assert [t.item_no for t in x.unpriced_positions] == ["2.3"]
    assert not x.complete
    assert _status(x, "2.3") == ("unpriced", 0.0, False)
    assert x.common_scope_positions == 4
    assert x.common_scope_cost == pytest.approx(3500 + 1200 + 300 * 12.6 + 180 * 9.975)
    assert y.common_scope_cost == pytest.approx(3500 + 1200 + 300 * 12.0 + 180 * 9.5)
    assert (x.colour, y.colour) == ("red", "green")
