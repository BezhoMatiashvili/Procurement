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

import io
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

import tests.boq_factory as factory
from procurement_agent.checks.boq import (
    BidComparison,
    BoqLine,
    BoqWorkbook,
    _item_key,
    _label_key,
    _plays,
    _position_groups,
    compare_all,
    norm_unit,
    read_boq,
    reconcile_summary,
    template_findings,
    write_merged_workbook,
)
from tests.unit.test_boq_reader_round4 import (
    _COST,
    _HEAD,
    _LINES,
    _MARKUP,
    _SHEET,
    _bill,
    _compare,
    _summary,
)
from tests.unit.test_boq_reader_variants import Cells, Fx, _rules, _save, _summary_book
from tests.unit.test_boq_reader_variants import _row as _srow

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


# --- R5-08: item numbers written with a closing dot ------------------------------------------


@pytest.mark.parametrize("dotted", [False, True])
def test_item_numbers_written_with_a_closing_dot_are_the_same_positions(
    tmp_path: Path, dotted: bool
) -> None:
    def lines(rates: tuple[float, ...] | None, dot: str) -> tuple[_Line, ...]:
        out: list[_Line] = []
        for (item, desc, unit, qty, _), rate in zip(_LINES, rates or (0.0,) * 5, strict=True):
            item_no = item.split(".")[1] if not dotted else item
            amount = qty * rate if rates else None
            out.append((item_no + dot, desc, unit, qty, rate if rates else None, amount))
        return tuple(out)

    rates = tuple(r for *_, r in _LINES)
    _, (a, b) = _evaluate(
        tmp_path,
        lines(None, ""),
        {"A": lines(rates, "."), "B": lines(tuple(r * 0.9 for r in rates), "")},
    )
    assert not a.added and not a.removed
    assert a.evaluated_cost == pytest.approx(_COST)
    assert a.common_scope_positions == 5
    assert (a.colour, b.colour) == ("red", "green")


@pytest.mark.parametrize(
    ("template", "bid", "same"),
    [
        ("1", "1.", True),
        ("1.1", "1.1.", True),
        ("A1", "A1.", True),
        ("1.1", "1.10", False),  # a digit is never folded
        ("1.1", "1.1.1", False),
        ("1", "11", False),
    ],
)
def test_only_a_closing_dot_is_folded_from_an_item_number(
    template: str, bid: str, same: bool
) -> None:
    assert (_item_key(template) == _item_key(bid)) is same


# --- R5-09: Summary sections numbered with Roman numerals --------------------------------------


@pytest.mark.parametrize(
    "items", [("I", "II", "III"), ("I.", "II.", "III."), ("IV", "V", "VI"), ("VIII", "IX", "X")]
)
def test_total_cost_is_the_sum_of_sections_numbered_in_roman(items: tuple[str, ...]) -> None:
    rows = [_srow(r, item, f"Chapter {r}", 100.0 * r) for r, item in enumerate(items, 2)]
    faithful = _summary_book([*rows, _srow(5, "", "TOTAL COST", 900.0)])
    assert reconcile_summary(faithful) == []
    wrong = _summary_book([*rows, _srow(5, "", "TOTAL COST", 950.0)])
    [gap] = reconcile_summary(wrong)
    assert "rows 2, 3, 4" in gap.message_en and gap.expected == "900.00"


def test_roman_chapters_do_not_add_their_numbered_sub_sections_twice() -> None:
    book = _summary_book(
        [
            _srow(2, "I", "EARTHWORKS", 300.0),
            _srow(3, "1", "Excavation", 200.0),
            _srow(4, "2", "Backfill", 100.0),
            _srow(5, "II", "CONCRETE", 700.0),
            _srow(6, "", "TOTAL COST", 1000.0),
        ]
    )
    assert reconcile_summary(book) == []


def test_lettered_sections_that_include_roman_shaped_letters_stay_letters() -> None:
    # near miss: A, B, C, D - "C" and "D" are Roman-shaped, but no item is a numeral of more
    # than one character, and "A" and "B" are no numerals: the sections are lettered
    rows = [_srow(r, item, f"Section {item}", 100.0) for r, item in enumerate("ABCD", 2)]
    assert reconcile_summary(_summary_book([*rows, _srow(6, "", "TOTAL COST", 400.0)])) == []
    # and a lone "I" among lettered sections is one of them, not a chapter of its own
    rows = [_srow(r, item, f"Section {item}", 100.0) for r, item in enumerate("GHI", 2)]
    assert reconcile_summary(_summary_book([*rows, _srow(5, "", "TOTAL COST", 300.0)])) == []


# --- R5-10: a priced placeholder in the merged workbook's excluded block ----------------------


def test_the_excluded_block_lists_a_priced_placeholder(tmp_path: Path) -> None:
    other = ("1.6", "Other works to be listed here by the tenderer", "LS")
    template = (*((*x[:4], None, None) for x in _LINES), (*other, None, None, None))
    priced = tuple((*x[:4], x[4], x[3] * x[4]) for x in _LINES)
    bids = {
        "P": (*priced, (*other, 2.0, 900.0, 1800.0)),
        "Q": (*priced[:3], *priced[4:], (*other, None, None, None)),  # 1.4 removed
    }
    books = []
    for name, lines in {"T": template, **bids}.items():
        bill, total = _cells(lines, template=name == "T")
        summary = _summary(total, closing=len(lines) + 2)
        books.append(
            read_boq(
                _save(tmp_path / f"{name}.xlsx", {"SUMMARY": summary, _SHEET: bill}), name=name
            )
        )
    comps = compare_all(books[0], books[1:])
    assert [b.item_no for b in comps[0].priced_optional] == ["1.6"]
    out = write_merged_workbook(books[0], books[1:], comps, tmp_path / "merged.xlsx")
    ws = load_workbook(out)["SUMMARY"]
    rows = [[c.value for c in row] for row in ws.iter_rows(max_col=6)]
    start = next(
        i for i, r in enumerate(rows) if str(r[0]).startswith("Excluded from the analysis")
    )
    excluded = {(r[1], r[4]): r[0] for r in rows[start + 2 :] if r[1]}
    assert excluded == {
        ("P", "1.6"): "EXCLUDED: the bidder's own additional items",
        ("Q", "1.4"): "EXCLUDED: removed by the bidder",
    }


# --- R5-11: a chain rate typed in the column left of the Summary's TOTAL -----------------------


def _rate_left_summary(cost: float, rate_head: str, overheads: float) -> Cells:
    """Item | Description | <rate_head> | Total: labels carry no rate, the rates are typed in
    column C; the overheads stated as given."""
    pr = cost * 0.05
    excl = cost + overheads + pr
    vat = excl * 0.18
    cells: Cells = {(1, 1): "Item", (1, 2): "Description", (1, 3): rate_head, (1, 4): "Total"}
    rows = (
        ("1", "WORKS", None, Fx(f"='{_SHEET}'!F{len(_LINES) + 2}", cost)),
        ("", "TOTAL COST", None, Fx("=D2", cost)),
        ("", "Overheads", 0.10, overheads),
        ("", "Profit", 0.05, Fx("=D3*C5", pr)),
        ("", "TOTAL PRICE EXCL. VAT", None, Fx("=D3+D4+D5", excl)),
        ("", "VAT", 0.18, Fx("=D6*C7", vat)),
        ("", "TOTAL PRICE INCLUDING VAT", None, Fx("=D6+D7", excl + vat)),
    )
    for r, (item, label, rate, value) in enumerate(rows, 2):
        cells.update({(r, 2): label, (r, 4): value})
        if item:
            cells[(r, 1)] = item
        if rate is not None:
            cells[(r, 3)] = rate
    return cells


@pytest.mark.parametrize("rate_head", ["%", "Rate", "Rate, %", "პროცენტი"])
def test_a_rate_typed_left_of_the_summary_total_is_held(tmp_path: Path, rate_head: str) -> None:
    bill, total = _bill(1.0)
    books = [
        read_boq(_save(tmp_path / f"{n}.xlsx", {"SUMMARY": s, _SHEET: bill}), name=n)
        for n, s in (
            ("A", _rate_left_summary(total, rate_head, total * 0.12)),
            ("B", _rate_left_summary(total, rate_head, total * 0.10)),
        )
    ]
    a, b = (reconcile_summary(book) for book in books)
    [gap] = a
    assert "row 4 (Overheads)" in gap.message_en and "x 0.1" in gap.message_en
    assert b == []


def test_a_fraction_left_of_the_summary_total_under_another_header_is_no_rate(
    tmp_path: Path,
) -> None:
    # near miss: column C headed "Materials" holds an amount that happens to be below 1
    bill, total = _bill(1.0)
    summary = _rate_left_summary(total, "Materials", total * 0.12)
    path = _save(tmp_path / "A.xlsx", {"SUMMARY": summary, _SHEET: bill})
    rows = read_boq(path, name="A").summary_rows
    assert all(r.rate is None for r in rows)


# --- R5-18 (the lead's policy): the usual Georgian unit spellings are one unit -----------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("კაც/სთ", "კაც.სთ"),
        ("კაც/სთ", "კაც-სთ"),
        ("კაც/სთ", "კაც სთ"),
        ("კაც/სთ", "man-hours"),
        ("კაც/დღე", "კაც-დღე"),
        ("მანქ/სთ", "მანქ.სთ"),
        ("შტ", "ც"),
        ("შტ.", "ცალი"),
        ("შტ", "pcs"),
        ("kW", "კვტ"),
        ("kWh", "კვტ/სთ"),
        ("month", "მთვ"),
        ("m", "გრ.მ"),
        ("გრ. მ.", "მ"),
    ],
)
def test_the_usual_georgian_unit_spellings_are_one_unit(a: str, b: str) -> None:
    assert norm_unit(a) == norm_unit(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("კაც/სთ", "სთ"),  # a person's hour is not an hour of time
        ("კაც/სთ", "hr"),
        ("კაც/სთ", "კაც/დღე"),
        ("კაც/სთ", "მანქ/სთ"),  # nor a machine's
        ("კვტ", "კვტ/სთ"),  # a kilowatt is not a kilowatt-hour
        ("შტ", "კომპლ."),  # a piece is not a set
        ("შტ", "LS"),
        ("მთვ", "დღე"),
    ],
)
def test_units_the_georgian_spellings_do_not_merge(a: str, b: str) -> None:
    assert norm_unit(a) != norm_unit(b)


# --- R5-17 (the lead's policy): a hidden template sheet the Summary does not link ------------


def _save_hidden(path: Path, sheets: dict[str, Cells], hidden: set[str]) -> Path:
    """``_save`` with the sheets named in ``hidden`` hidden, cached values kept."""
    wb = Workbook()
    default = wb.active
    assert default is not None
    wb.remove(default)
    cache: dict[tuple[int, str], object] = {}
    for index, (name, cells) in enumerate(sheets.items()):
        ws = wb.create_sheet(name)
        if name in hidden:
            ws.sheet_state = "hidden"
        for (r, c), v in cells.items():
            cell = ws.cell(r, c, v.text if isinstance(v, Fx) else v)
            if isinstance(v, Fx):
                cache[(index, cell.coordinate)] = v.value
    buf = io.BytesIO()
    wb.save(buf)
    path.write_bytes(factory._with_cached_values(buf.getvalue(), cache))
    return path


_DRAFT = "1. WORKS (old)"


def _hidden_draft_compare(
    tmp_path: Path, *, draft_priced_by: str = "", linked: bool = False, hidden: bool = True
) -> tuple[BoqWorkbook, list[BidComparison]]:
    books = []
    for name, factor in (("T", None), ("A", 1.0), ("B", 0.9)):
        bill, total = _bill(factor)
        draft, draft_total = _bill(1.0 if name == draft_priced_by else None)
        summary = _summary(total)
        if linked:
            summary[(9, 1)] = "2"
            summary[(9, 2)] = "OLD WORKS"
            summary[(9, 3)] = Fx(f"='{_DRAFT}'!F{len(_LINES) + 2}", draft_total)
        sheets = {"SUMMARY": summary, _SHEET: bill, _DRAFT: draft}
        path = _save_hidden(tmp_path / f"{name}.xlsx", sheets, {_DRAFT} if hidden else set())
        books.append(read_boq(path, name=name))
    return books[0], compare_all(books[0], books[1:])


def test_a_hidden_draft_sheet_the_summary_does_not_link_is_no_section(tmp_path: Path) -> None:
    t, (a, b) = _hidden_draft_compare(tmp_path)
    assert list(t.sheets) == [_SHEET] and t.hidden_sheets == [_DRAFT]
    [note] = [f for f in template_findings(t) if f.actual == _DRAFT]
    assert (note.rule_id, note.severity) == ("BOQ.SHEET_SKIPPED", "info")
    for comp in (a, b):
        assert "BOQ.SECTION_UNPRICED" not in _rules(comp)
        assert comp.complete and not comp.unpriced_sheets
    assert (a.colour, b.colour) == ("red", "green")


def test_a_hidden_draft_priced_by_a_bidder_is_shown_never_evaluated(tmp_path: Path) -> None:
    _, (a, b) = _hidden_draft_compare(tmp_path, draft_priced_by="A")
    assert len(a.priced_headings) == len(_LINES) and not b.priced_headings
    assert a.evaluated_cost == pytest.approx(_COST)
    # the draft adds into neither the stated nor the sheets' total: nothing to break down
    assert a.difference == {}


def test_a_section_the_bidder_hid_is_still_that_section(tmp_path: Path) -> None:
    # the template's bill is visible; bidder A hides it and types its Summary as values, so
    # nothing links the hidden sheet - it is still the section, compared and evaluated
    books = []
    for name, factor in (("T", None), ("A", 1.0), ("B", 0.9)):
        bill, total = _bill(factor)
        summary = _summary(total)
        if name == "A":
            summary = {k: v.value if isinstance(v, Fx) else v for k, v in summary.items()}
        path = _save_hidden(
            tmp_path / f"{name}.xlsx",
            {"SUMMARY": summary, _SHEET: bill},
            {_SHEET} if name == "A" else set(),
        )
        books.append(read_boq(path, name=name))
    assert books[1].hidden_sheets == [_SHEET]
    a, b = compare_all(books[0], books[1:])
    assert "BOQ.SHEET_UNREADABLE" not in _rules(a)
    assert a.evaluated_cost == pytest.approx(_COST)
    assert a.common_scope_positions == len(_LINES)
    assert (a.colour, b.colour) == ("red", "green")


@pytest.mark.parametrize(("linked", "hidden"), [(True, True), (False, False)])
def test_a_linked_or_visible_second_bill_is_still_a_section(
    tmp_path: Path, linked: bool, hidden: bool
) -> None:
    # near misses: the Summary links the hidden sheet, or the sheet is not hidden
    t, (a, _) = _hidden_draft_compare(tmp_path, linked=linked, hidden=hidden)
    assert _DRAFT in t.sheets and not t.hidden_sheets
    assert "BOQ.SECTION_UNPRICED" in _rules(a)


# --- R5-16 (the lead's policy): a contingency between the bill's Total and the carried total --


def _contingency_bill(
    factor: float | None, label: str, *, total_range: str = "F2:F6", between: object = None
) -> tuple[Cells, float]:
    """The bill, then "<label>" summing the positions, "Contingency 5%" (``=F7*E8``) - or the
    ``between`` row given - and "TOTAL CARRIED TO SUMMARY" (``=F7+F8``), which the Summary
    links."""
    cells, total = _bill(factor, close=label)
    last = int(total_range.split(":")[1][1:])
    stated = total - sum(
        q * r * (factor or 0.0) for r_, (_, _, _, q, r) in enumerate(_LINES, 2) if r_ > last
    )
    cells[(7, 6)] = Fx(f"=SUM({total_range})", stated)
    cells[(8, 2)] = "Contingency 5%"
    cells[(8, 5)] = 0.05
    cells[(8, 6)] = Fx("=F7*E8", stated * 0.05)
    if between is not None:
        cells[(8, 2)] = "Provisional sum"
        cells[(8, 5)] = None
        cells[(8, 6)] = between
    carried = stated + (between if isinstance(between, float) else stated * 0.05)
    cells[(9, 2)] = "TOTAL CARRIED TO SUMMARY"
    cells[(9, 6)] = Fx("=F7+F8", carried)
    return cells, carried


def _contingency_compare(tmp_path: Path, label: str, **kw: object) -> list[BidComparison]:
    books = []
    for name, factor in (("T", None), ("A", 1.0), ("B", 0.9)):
        bill, carried = _contingency_bill(factor, label, **kw)  # type: ignore[arg-type]
        summary = _summary(carried, closing=9)
        path = _save(tmp_path / f"{name}.xlsx", {"SUMMARY": summary, _SHEET: bill})
        books.append(read_boq(path, name=name))
    return compare_all(books[0], books[1:])


@pytest.mark.parametrize("label", ["Total", "ჯამი", "Sub-total", "Works total"])
def test_a_contingency_between_the_total_and_the_carried_total_is_part_of_the_chain(
    tmp_path: Path, label: str
) -> None:
    a, b = _contingency_compare(tmp_path, label)
    for comp in (a, b):
        assert "BOQ.RECONCILE" not in _rules(comp)
    assert (a.colour, b.colour) == ("red", "green")


def test_a_bill_total_one_row_short_is_still_reported(tmp_path: Path) -> None:
    # near miss: chosen by how the rows are built, the Total still answers for its own range
    a, _ = _contingency_compare(tmp_path, "Total", total_range="F2:F5")
    [gap] = [f for f in a.findings if f.rule_id == "BOQ.RECONCILE"]
    assert "row 7" in gap.message_en
    assert gap.actual == "24,005.00" and gap.expected == "16,755.00"


def test_a_row_between_that_is_no_rate_of_the_total_leaves_the_carried_total_closing(
    tmp_path: Path,
) -> None:
    # near miss: a typed provisional sum between them is no rate of the Total - the carried
    # total closes the sheet as before, and the positions do not add up to it
    a, _ = _contingency_compare(tmp_path, "Total", between=1000.0)
    [gap] = [f for f in a.findings if f.rule_id == "BOQ.RECONCILE"]
    assert "row 9" in gap.message_en


# --- R5-13 (the lead's policy): a price list closed by its total incl. VAT alone --------------


def _price_list_compare(
    tmp_path: Path, label: str, *, scale: str = "", after: tuple[str, str] | None = None
) -> list[BidComparison]:
    """One sheet, no Summary: the bill closed by "<label>" (``=SUM(F2:F6)<scale>``), and an
    ``after`` row (label, formula) under it if given."""
    books = []
    for name, factor in (("T", None), ("A", 1.0), ("B", 0.9)):
        bill, total = _bill(factor, close=label)
        if scale:
            bill[(7, 6)] = Fx(f"=SUM(F2:F6){scale}", total * 1.18)
        if after is not None:
            bill[(8, 2)] = after[0]
            bill[(8, 6)] = Fx(after[1], total * 0.18 / 1.18)
        books.append(read_boq(_save(tmp_path / f"{name}.xlsx", {"1. GOODS": bill}), name=name))
    return compare_all(books[0], books[1:])


@pytest.mark.parametrize(
    "label", ["სულ ჯამი (დღგ-ს ჩათვლით)", "Total incl. VAT", "ჯამი დღგ-ით", "TOTAL WITH VAT"]
)
def test_a_price_list_closed_by_its_total_incl_vat_is_ranked_on_it(
    tmp_path: Path, label: str
) -> None:
    a, b = _price_list_compare(tmp_path, label)
    for comp in (a, b):
        assert comp.totals_from == "bill"
        assert comp.markup_factor == pytest.approx(1.0)
        assert not {"BOQ.RECONCILE", "BOQ.NOT_RANKED"} & set(_rules(comp))
    assert a.stated_incl_vat == pytest.approx(_COST)
    assert a.common_scope_total == pytest.approx(_COST)
    assert b.common_scope_total == pytest.approx(_COST * 0.9)
    assert (a.colour, b.colour) == ("red", "green")


@pytest.mark.parametrize(
    ("label", "kw"),
    [
        ("Total", {}),  # no row says incl. VAT
        ("Total incl. VAT", {"scale": "*1.18"}),  # the tax added onto a pre-VAT sum
        ("Total incl. VAT", {"after": ("of which VAT 18%", "=F7*18/118")}),  # not the only row
    ],
)
def test_a_price_list_not_closed_by_its_total_incl_vat_alone_is_not_ranked_so(
    tmp_path: Path, label: str, kw: dict[str, object]
) -> None:
    a, b = _price_list_compare(tmp_path, label, **kw)  # type: ignore[arg-type]
    for comp in (a, b):
        assert comp.markup_factor is None and comp.colour is None


def test_a_bid_closing_on_incl_vat_alone_against_a_template_chain_is_not_ranked(
    tmp_path: Path,
) -> None:
    # near miss: the template closes Total, VAT 18%, Total incl. VAT; read at markup 1.0, a bid
    # collapsing that into one "Total incl. VAT" over the same pre-VAT prices would come out
    # 18% cheaper than it is
    def chain(factor: float | None) -> Cells:
        bill, total = _bill(factor, close="Total")
        bill[(8, 2)], bill[(8, 6)] = "VAT 18%", Fx("=F7*0.18", total * 0.18)
        bill[(9, 2)], bill[(9, 6)] = "Total incl. VAT", Fx("=F7+F8", total * 1.18)
        return bill

    sheets = {
        "T": chain(None),
        "A": _bill(1.0, close="Total incl. VAT")[0],
        "B": chain(0.95),
    }
    books = [
        read_boq(_save(tmp_path / f"{n}.xlsx", {"1. GOODS": cells}), name=n)
        for n, cells in sheets.items()
    ]
    a, b = compare_all(books[0], books[1:])
    assert a.markup_factor is None and a.colour is None
    assert b.markup_factor == pytest.approx(1.18)
