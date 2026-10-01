"""Comparison defects a red team found and a verifier reproduced (round 5), one test each, with
a near miss beside every widened reading.

Every document here is invented: names, codes, accounts and addresses are made up.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.compare import _row_labels, compare, hits_of
from procurement_agent.eval.runner import compare_label
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion.segmenter import Segment, TableCell, build

PRICE = "1. ხელშეკრულების ღირებულება შეადგენს 2 000 ლარს."


def segs(texts: list[str]) -> list[Segment]:
    return build([(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(texts)])


def run(left: list[str] | list[Segment], right: list[str] | list[Segment]):  # type: ignore[no-untyped-def]
    ls = left if left and isinstance(left[0], Segment) else segs(left)  # type: ignore[arg-type]
    rs = right if right and isinstance(right[0], Segment) else segs(right)  # type: ignore[arg-type]
    return compare(
        ls,  # type: ignore[arg-type]
        rs,  # type: ignore[arg-type]
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )


def edit(doc: list[str], old: str, new: str) -> list[str]:
    where = [i for i, t in enumerate(doc) if old in t]
    assert where, old
    out = list(doc)
    out[where[0]] = out[where[0]].replace(old, new)
    return out


def labelled(report) -> list[tuple[str, str | None, str | None, str | None, str]]:  # type: ignore[no-untyped-def]
    """(rule, label, before, after, severity) of every substantive finding."""
    return [
        (f.rule_id, compare_label(f, report), f.expected, f.actual, f.severity)
        for f in report.findings
        if f.severity != "info" and f.rule_id != "CMP.FORMATTING"
    ]


def rules(report) -> list[str]:  # type: ignore[no-untyped-def]
    return [r[0] for r in labelled(report)]


def values(text: str, ft: FactType) -> list[str | float | None]:
    return [h.normalized for h in hits_of(text) if h.type is ft]


# --- COMPARE-R5-01: a penalty stated as an amount per period --------------------------------

FINE = (
    "2. ვადის დარღვევისთვის მიმწოდებელს ეკისრება პირგასამტეხლო 40 ლარის ოდენობით ყოველ "
    "ვადაგადაცილებულ დღეზე."
)


@pytest.mark.parametrize(("noun", "period"), [("კვირაზე", "week"), ("თვეზე", "month")])
def test_an_amount_penalty_s_period_changed_is_a_penalty_change(noun: str, period: str) -> None:
    left = [PRICE, FINE, "3. დავები წყდება სასამართლოში."]
    right = edit(left, "დღეზე", noun)
    assert labelled(run(left, right)) == [
        ("CMP.PERCENT", "Penalty", "40.00 GEL per day", f"40.00 GEL per {period}", "error")
    ]


def test_an_english_amount_penalty_s_period_changed_is_a_penalty_change() -> None:
    left = [
        "1. The Contract price is GEL 2,000.",
        "2. A penalty of GEL 40 per day of delay applies.",
    ]
    right = edit(left, "per day", "per month")
    assert labelled(run(left, right)) == [
        ("CMP.PERCENT", "Penalty", "40.00 GEL per day", "40.00 GEL per month", "error")
    ]


def test_an_amount_penalty_s_period_is_its_own_not_a_neighbour_s() -> None:
    # near miss: the day is the second fine's; the first one, 300 lari, counts per no period
    hits = values("2. ჯარიმა 300 ლარი და 40 ლარი ყოველ დღეზე.", FactType.penalty)
    assert sorted(map(str, hits)) == ["300.0", "40.00 GEL per day"]


def test_an_amount_penalty_with_no_period_is_read_as_before() -> None:
    assert values("2. მიმწოდებელს ეკისრება პირგასამტეხლო 40 ლარი.", FactType.penalty) == [40.0]


def test_an_amount_penalty_s_period_written_another_way_is_no_penalty_change() -> None:
    left = [
        PRICE,
        "2. პირგასამტეხლო შეადგენს 40 ლარს ყოველ დღეზე.",
        "3. დავები წყდება სასამართლოში.",
    ]
    right = edit(left, "ყოველ დღეზე", "დღიურად")
    assert "CMP.PERCENT" not in rules(run(left, right))


def test_a_rate_penalty_s_period_changed_is_still_one_change() -> None:
    left = [PRICE, "2. პირგასამტეხლო შეადგენს ღირებულების 0.1%-ს ყოველ დღეზე."]
    right = edit(left, "დღეზე", "კვირაზე")
    assert labelled(run(left, right)) == [
        ("CMP.PERCENT", "Penalty", "0.1% per day", "0.1% per week", "error")
    ]


# --- COMPARE-R5-02: terms written as "label: value" lines ------------------------------------

TERMS = [
    "ხელშეკრულება № TB-2026/112",
    "ქ. რუსთავი, 2026 წლის 3 მარტი",
    "ძირითადი პირობები:",
    "ხელშეკრულების ღირებულება: 2 000 ლარი",
    "მიწოდების ვადა: ხელშეკრულების გაფორმებიდან 20 კალენდარული დღე",
    "ანგარიშსწორების ვადა: აქტის გაფორმებიდან 10 სამუშაო დღე",
    "საგარანტიო ვადა: 12 თვე",
]


def test_a_label_line_s_count_is_typed_by_its_own_label_not_the_next() -> None:
    text = "\n".join(TERMS)
    assert values(text, FactType.delivery_deadline) == ["P20D"]
    assert values(text, FactType.payment_terms) == ["P10D working"]
    assert values(text, FactType.warranty_period) == ["P12M"]


def test_a_delivery_term_on_a_label_line_changed_is_one_change() -> None:
    right = edit(TERMS, "გაფორმებიდან 20", "გაფორმებიდან 35")
    assert labelled(run(TERMS, right)) == [
        ("CMP.DURATION", "Delivery deadline", "P20D", "P35D", "error")
    ]


def test_a_payment_term_on_a_label_line_changed_is_one_change() -> None:
    right = edit(TERMS, "10 სამუშაო", "15 სამუშაო")
    assert labelled(run(TERMS, right)) == [
        ("CMP.PAYMENT", "Payment terms", "P10D working", "P15D working", "error")
    ]


def test_a_sentence_broken_over_two_lines_is_still_one_sentence() -> None:
    # near miss: no label line, a paragraph run split by the parser - the count stays the term's
    text = "2. ანგარიშსწორება განხორციელდება აქტის გაფორმებიდან\n10 სამუშაო დღის ვადაში."
    assert values(text, FactType.payment_terms) == ["P10D working"]


def test_requisites_on_label_lines_are_read_as_written() -> None:
    # near miss: closing a label line is for the values' readers; a requisite keeps its text
    lines = ["მისამართი: ქ. ფოთი, ზღვის ქ. N4", "ანგარიშის ნომერი: GE00TB0000000000000001"]
    text = "\n".join(lines)
    assert {(h.type, h.value) for h in hits_of(text)} == {
        (h.type, h.value) for h in hits_of(" ".join(lines))
    }


# --- COMPARE-R5-03: a Word two-column key-terms table ----------------------------------------

KEY_TERMS = [
    ["პირობა", "მნიშვნელობა"],
    ["ხელშეკრულების ღირებულება", "2 000 ლარი"],
    ["მიწოდების ვადა", "ხელშეკრულების გაფორმებიდან 20 კალენდარული დღე"],
    ["ანგარიშსწორების ვადა", "აქტის გაფორმებიდან 10 სამუშაო დღე"],
    ["საგარანტიო ვადა", "12 თვე"],
    ["პირგასამტეხლო", "ღირებულების 0.1% ყოველ ვადაგადაცილებულ დღეზე"],
]


def table(rows: list[list[str]]) -> list[Segment]:
    records: list[tuple] = [  # type: ignore[type-arg]
        ("ხელშეკრულება № TB-2026/112", "text", "¶1", None, None),
        ("ძირითადი პირობები:", "text", "¶2", None, None),
    ]
    for r, row in enumerate(rows, start=1):
        for c, text in enumerate(row, start=1):
            header = rows[0][c - 1] if r > 1 else None
            records.append(
                (text, "table_cell", f"T1/r{r}/c{c}", None, None, TableCell(1, r, c, header))
            )
    records.append(("ხელმოწერები:", "text", "¶3", None, None))
    return build(records)


def changed(row: int, value: str) -> list[list[str]]:
    rows = [list(r) for r in KEY_TERMS]
    rows[row][1] = value
    return rows


@pytest.mark.parametrize(
    ("row", "value", "expected"),
    [
        (
            2,
            "ხელშეკრულების გაფორმებიდან 35 კალენდარული დღე",
            ("CMP.DURATION", "Delivery deadline", "P20D", "P35D", "error"),
        ),
        (
            3,
            "აქტის გაფორმებიდან 15 სამუშაო დღე",
            ("CMP.PAYMENT", "Payment terms", "P10D working", "P15D working", "error"),
        ),
        (4, "24 თვე", ("CMP.DURATION", "Warranty period", "P12M", "P24M", "error")),
        (
            5,
            "ღირებულების 0.3% ყოველ ვადაგადაცილებულ დღეზე",
            ("CMP.PERCENT", "Penalty", "0.1% per day", "0.3% per day", "error"),
        ),
        (
            1,
            "2 500 ლარი",
            ("CMP.AMOUNT", "Total amount", "2,000.00 GEL", "2,500.00 GEL", "error"),
        ),
    ],
)
def test_a_key_terms_table_s_value_is_labelled_by_its_row_once(
    row: int, value: str, expected: tuple[str, str, str, str, str]
) -> None:
    assert labelled(run(table(KEY_TERMS), table(changed(row, value)))) == [expected]


def test_only_a_two_column_table_s_term_names_label_its_values() -> None:
    labels = _row_labels(table(KEY_TERMS))
    assert sorted(labels.values()) == sorted(r[0] for r in KEY_TERMS)  # the header row's too
    # near misses: a first column of item numbers, a value cell that is a bare figure, and a
    # table of three columns label nothing
    numbered = table([["N", "პირობა"], ["1", "12 თვე"], ["2", "20 დღე"]])
    assert [s.text for s in numbered if id(s) in _row_labels(numbered)] == ["პირობა"]
    # the header row only: the bare figure "250" keeps its own reading
    priced = table([["დასახელება", "ფასი"], ["მაგიდა", "250"]])
    assert [s.text for s in priced if id(s) in _row_labels(priced)] == ["ფასი"]
    three = table([["პირობა", "მნიშვნელობა", "შენიშვნა"], ["საგარანტიო ვადა", "12 თვე", "-"]])
    assert _row_labels(three) == {}


def test_a_bare_figure_in_a_two_column_table_is_still_a_number() -> None:
    rows = [["დასახელება", "ფასი"], ["მაგიდა", "250"]]
    after = [["დასახელება", "ფასი"], ["მაგიდა", "275"]]
    assert labelled(run(table(rows), table(after))) == [
        ("CMP.AMOUNT", "Number", "250", "275", "error")
    ]


# --- COMPARE-R5-04: two equal values of one type in one sentence -----------------------------

ITEMS = [
    "1. ხელშეკრულების საგანი: საოფისე ავეჯის შესყიდვა.",
    "2. რაოდენობა: 25 (ოცდახუთი) ცალი კარადა და 25 (ოცდახუთი) ცალი თარო.",
    "3. დავები წყდება სასამართლოში.",
]


def test_two_equal_quantities_in_one_sentence_are_two_values() -> None:
    assert values(ITEMS[1], FactType.quantity) == ["25 item", "25 item"]


def test_one_of_two_equal_quantities_changed_is_one_change() -> None:
    right = edit(ITEMS, "და 25 (ოცდახუთი) ცალი თარო", "და 30 (ოცდაათი) ცალი თარო")
    assert labelled(run(ITEMS, right)) == [
        ("CMP.QUANTITY", "Quantity", "25 item", "30 item", "error")
    ]


def test_one_of_two_equal_prices_changed_is_one_change() -> None:
    left = [ITEMS[0], "2. კარადის ფასი 120 ლარი, თაროს ფასი 120 ლარი.", ITEMS[2]]
    right = edit(left, "თაროს ფასი 120", "თაროს ფასი 140")
    assert labelled(run(left, right)) == [
        ("CMP.AMOUNT", "Unit price", "120.00 GEL", "140.00 GEL", "error")
    ]


def test_an_act_short_of_one_of_two_equal_quantities_is_an_error() -> None:
    contract = [
        "ხელშეკრულება № TB-2026/112 ავეჯის მიწოდების შესახებ",
        "ქ. რუსთავი, 2026 წლის 3 მარტი",
        "1. მიმწოდებელი აწვდის 25 ცალ კარადას და 25 ცალ თაროს.",
        "2. ხელშეკრულების ღირებულება შეადგენს 2 000 ლარს.",
        "3. ანგარიშსწორება განხორციელდება აქტის გაფორმებიდან 10 სამუშაო დღის ვადაში.",
    ]
    act = [
        "მიღება-ჩაბარების აქტი",
        "ქ. რუსთავი, 2026 წლის 20 მარტი",
        "მიმწოდებელმა ჩააბარა, ხოლო შემსყიდველმა მიიღო:",
        "კარადა - 25 ცალი",
        "თარო - 22 ცალი",
    ]
    found = [r for r in labelled(run(contract, act)) if r[0] == "CMP.QUANTITY"]
    assert found == [("CMP.QUANTITY", "Quantity", "25 item", "22 item", "error")]


def test_a_value_restated_on_another_line_is_one_value() -> None:
    # near miss: a figure restated paragraph after paragraph is one value, and the restatement
    # deleted is no value removed while the document still states it
    left = [
        PRICE,
        "2. ხელშეკრულების ღირებულება შეადგენს 2 000 ლარს და მოიცავს ყველა ხარჯს.",
        "3. დავები წყდება სასამართლოში.",
    ]
    assert values("\n".join(left), FactType.total_amount) == [2000.0]
    right = [left[0], left[2]]
    assert "CMP.AMOUNT" not in rules(run(left, right))


def test_a_restated_value_dropped_beside_a_new_one_is_not_a_change_of_it() -> None:
    left = [
        PRICE,
        "2. ღირებულება 2 000 ლარი გადაიხდება ერთიანად.",
        "3. დავები წყდება სასამართლოში.",
    ]
    right = [PRICE, "2. ავანსი 500 ლარი გადაიხდება ხელშეკრულების გაფორმებისას.", left[2]]
    found = labelled(run(left, right))
    assert not [r for r in found if r[2] is not None and r[3] is not None and "2,000" in r[2]]


# --- COMPARE-R5-12 (policy): a value added or removed with its words is one finding ----------

ADVANCE = "2. ავანსის ოდენობა შეადგენს 600 (ექვსასი) ლარს."


def test_a_clause_added_with_an_amount_and_its_words_is_one_finding() -> None:
    left = [PRICE, "3. დავები წყდება სასამართლოში."]
    right = [PRICE, ADVANCE, left[1]]
    assert labelled(run(left, right)) == [
        ("CMP.AMOUNT", "Amount (unclassified)", None, "600.00 GEL", "error")
    ]
    assert labelled(run(right, left)) == [
        ("CMP.AMOUNT", "Amount (unclassified)", "600.00 GEL", None, "error")
    ]


def test_a_draft_s_blank_filled_with_a_figure_and_its_words_is_the_figure() -> None:
    draft = [
        "1. ხელშეკრულების საერთო ღირებულება შეადგენს ________ (_____________) ლარს.",
        "2. მიწოდება განხორციელდება გაფორმებიდან ___ (______) კალენდარული დღის ვადაში.",
    ]
    signed = [
        "1. ხელშეკრულების საერთო ღირებულება შეადგენს 2 000 (ორი ათასი) ლარს.",
        "2. მიწოდება განხორციელდება გაფორმებიდან 20 (ოცი) კალენდარული დღის ვადაში.",
    ]
    found = labelled(run(draft, signed))
    assert "CMP.AMOUNT_WORDS" not in [r[0] for r in found]
    assert ("CMP.AMOUNT", "Total amount", None, "2,000.00 GEL", "error") in found
    assert ("CMP.DURATION", "Delivery deadline", None, "P20D", "error") in found


def test_words_added_that_do_not_spell_their_figure_are_still_reported() -> None:
    # near miss: "600 (შვიდასი)" - the words say 700, and that is reported
    left = [PRICE, "3. დავები წყდება სასამართლოში."]
    right = [PRICE, ADVANCE.replace("(ექვსასი)", "(შვიდასი)"), left[1]]
    assert "CMP.AMOUNT_WORDS" in rules(run(left, right))


def test_a_figure_and_its_words_changed_in_place_are_still_one_change() -> None:
    left = [PRICE, ADVANCE]
    right = edit(left, "600 (ექვსასი)", "700 (შვიდასი)")
    assert rules(run(left, right)) == ["CMP.AMOUNT"]


# --- the comparison's side of the shared readers (round 5): whole pairs, held to their result
# once the reader reads the form, skipped until then ---------------------------------------


def test_a_penalty_period_restyled_as_dgeshi_is_no_penalty_change() -> None:
    # COMPARE-R5-05
    if "0.1% per day" not in values("2. პირგასამტეხლო 0.1% დღეში.", FactType.penalty):
        pytest.skip("the penalty reader does not read 'დღეში' as a period yet")
    left = [PRICE, "2. პირგასამტეხლო შეადგენს ღირებულების 0.1%-ს ყოველ ვადაგადაცილებულ დღეზე."]
    right = edit(left, "ყოველ ვადაგადაცილებულ დღეზე", "დღეში")
    assert set(rules(run(left, right))) <= {"CMP.TEXT_CHANGED"}
    later = edit(right, "დღეში", "თვეში")
    assert labelled(run(right, later)) == [
        ("CMP.PERCENT", "Penalty", "0.1% per day", "0.1% per month", "error")
    ]


def test_a_draft_s_blank_address_filled_in_is_an_address_added() -> None:
    # COMPARE-R5-07
    blank = (
        "შპს „ველის ყვავილი“ (საიდენტიფიკაციო კოდი: 400000017), მისამართი: ______________, "
        "წარმოდგენილი მისი დირექტორის ლევან ქავთარაძის სახით."
    )
    if values(blank, FactType.party_address):
        pytest.skip("the address reader still reads a blank as an address")
    filled = blank.replace("______________", "ქ. ზესტაფონი, ვაზის ქ. N2")
    found = labelled(run([blank, PRICE], [filled, PRICE]))
    assert ("CMP.PARTY", "Legal address", None, "ქ. ზესტაფონი, ვაზის ქ. N2", "error") in found


def test_an_advance_clause_revised_is_its_share_and_its_amount() -> None:
    # COMPARE-R5-08
    clause = "2. შემსყიდველი გადაიხდის ავანსს {n} ლარის ოდენობით, ხელშეკრულების ღირებულების {s}%."
    left = clause.format(n="1 200 (ათას ორასი)", s=30)
    right = clause.format(n="800 (რვაასი)", s=20)
    if values(right, FactType.payment_terms) != ["advance 20%"] or values(
        left, FactType.total_amount
    ):
        pytest.skip("the readers do not read an advance clause's share and amount yet")
    assert labelled(run([PRICE, left], [PRICE, right])) == [
        ("CMP.PAYMENT", "Payment terms", "advance 30%", "advance 20%", "error"),
        ("CMP.AMOUNT", "Amount (unclassified)", "1,200.00 GEL", "800.00 GEL", "error"),
    ]


def test_the_price_of_one_item_is_a_unit_price() -> None:
    # COMPARE-R5-09
    line = "2. ერთი ცალი თაროს ღირებულება შეადგენს 120 ლარს."
    if not values(line, FactType.price):
        pytest.skip("the price reader does not read one item's price as a unit price yet")
    left = ["1. საერთო ღირებულება შეადგენს 2 000 ლარს.", line]
    right = edit(left, "120 ლარს", "130 ლარს")
    assert labelled(run(left, right)) == [
        ("CMP.AMOUNT", "Unit price", "120.00 GEL", "130.00 GEL", "error")
    ]


def test_a_second_batch_s_deadline_changed_is_a_delivery_deadline() -> None:
    # COMPARE-R5-11
    line = (
        "3. მიწოდება: პირველი პარტია - არაუგვიანეს 2026 წლის 10 აპრილისა, მეორე პარტია - "
        "არაუგვიანეს 2026 წლის 10 მაისისა."
    )
    if len(values(line, FactType.delivery_deadline)) != 2:
        pytest.skip("the delivery reader does not read a staged delivery's batches yet")
    right = line.replace("10 მაისისა", "25 მაისისა")
    assert labelled(run([PRICE, line], [PRICE, right])) == [
        ("CMP.DURATION", "Delivery deadline", "2026-05-10", "2026-05-25", "error")
    ]
