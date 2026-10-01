"""Comparison defects a red team found and a verifier reproduced (round 4), one test each, with
a near miss beside every widened reading.

Every document here is invented: names, codes, accounts and addresses are made up.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.compare import compare, hits_of
from procurement_agent.eval.runner import compare_label
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion.segmenter import Segment, TableCell, build

CONTRACT = [
    "ხელშეკრულება № KL-2026/044 სარეცხი საშუალებების მიწოდების შესახებ",
    "ქ. ოზურგეთი, 2026 წლის 14 აპრილი",
    "შპს „ზღვის ნიავი“ (საიდენტიფიკაციო კოდი: 404555123), მისამართი: ქ. ოზურგეთი, ლიმონის ქ. "
    "N3 (შემდგომში „შემსყიდველი“), და",
    "შპს „მთის წყარო“ (საიდენტიფიკაციო კოდი: 404666321), მისამართი: ქ. ლანჩხუთი, მუხის ქ. N11 "
    "(შემდგომში „მიმწოდებელი“)",
    "1. ხელშეკრულების საგანი",
    "1.1. მიმწოდებელი ვალდებულია მიაწოდოს შემსყიდველს სარეცხი საშუალებები.",
    "2. ღირებულება და ანგარიშსწორება",
    "2.1. ხელშეკრულების საერთო ღირებულება შეადგენს 18 400 (თვრამეტი ათას ოთხასი) ლარს.",
    "2.2. ფასი მოიცავს ტრანსპორტირების ხარჯებს და დღგ-ს.",
    "2.3. ანგარიშსწორება განხორციელდება აქტის გაფორმებიდან 10 (ათი) სამუშაო დღის ვადაში.",
    "3. გარანტია და პასუხისმგებლობა",
    "3.1. მიმწოდებელი წარმოადგენს ღირებულების 5%-ის ოდენობის საბანკო გარანტიას.",
    "3.2. ვადის გადაცილებისთვის მიმწოდებელს ეკისრება პირგასამტეხლო ღირებულების 0.1%-ის "
    "ოდენობით ყოველ ვადაგადაცილებულ დღეზე.",
    "3.3. შემსყიდველი გადაიხდის 30%-იან ავანსს ხელშეკრულების გაფორმებიდან 5 სამუშაო დღეში.",
    "4. დასკვნითი დებულებები",
    "4.1. ხელშეკრულება შედგენილია 2 (ორი) იდენტურ ეგზემპლარად.",
]


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


def found(report) -> list[tuple[str, str | None, str | None, str]]:  # type: ignore[no-untyped-def]
    """(rule, before, after, severity) of every substantive finding."""
    return [
        (f.rule_id, f.expected, f.actual, f.severity)
        for f in report.findings
        if f.severity != "info" and f.rule_id != "CMP.FORMATTING"
    ]


def labelled(report) -> list[tuple[str, str | None, str | None, str | None, str]]:  # type: ignore[no-untyped-def]
    return [
        (f.rule_id, compare_label(f, report), f.expected, f.actual, f.severity)
        for f in report.findings
        if f.severity != "info" and f.rule_id != "CMP.FORMATTING"
    ]


def rules(report) -> list[str]:  # type: ignore[no-untyped-def]
    return [rule for rule, *_ in found(report)]


# --- COMPARE-R4-06: a figure's words dropped or added, the figure unchanged, is formatting -----


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("10 (ათი) სამუშაო დღის", "10 სამუშაო დღის"),  # a term's words
        ("18 400 (თვრამეტი ათას ოთხასი) ლარს", "18 400 ლარს"),  # an amount's words
        ("2 (ორი) იდენტურ", "2 იდენტურ"),  # a count's words
    ],
)
def test_a_figure_s_words_dropped_or_added_with_the_figure_unchanged_is_formatting(
    old: str, new: str
) -> None:
    dropped = edit(CONTRACT, old, new)
    assert found(run(CONTRACT, dropped)) == []
    assert found(run(dropped, CONTRACT)) == []


def test_a_figure_s_words_dropped_with_the_figure_changed_is_still_the_term_s_change() -> None:
    right = edit(CONTRACT, "10 (ათი) სამუშაო დღის", "15 სამუშაო დღის")
    assert ("CMP.PAYMENT", "P10D working", "P15D working", "error") in found(run(CONTRACT, right))


def test_a_figure_s_words_changed_beside_it_are_still_reported() -> None:
    right = edit(CONTRACT, "2 (ორი) იდენტურ", "2 (სამი) იდენტურ")
    assert rules(run(CONTRACT, right)) == ["CMP.AMOUNT_WORDS"]


def test_a_figure_s_words_dropped_beside_another_word_change_is_a_text_change() -> None:
    right = edit(CONTRACT, "2 (ორი) იდენტურ ეგზემპლარად", "2 ეგზემპლარად")
    assert rules(run(CONTRACT, right)) == ["CMP.TEXT_CHANGED"]


# --- COMPARE-R4-07: the words inside a penalty's period phrase are compared --------------------


@pytest.mark.parametrize(
    "new",
    [
        "ყოველ ვადაგადაცილებულ სამუშაო დღეზე",
        "ყოველ ვადაგადაცილებულ კალენდარულ დღეზე",
        "ყოველ მეათე ვადაგადაცილებულ დღეზე",
        "ყოველ დაგვიანებულ დღეზე",
    ],
)
def test_a_word_changed_inside_a_penalty_s_period_is_a_text_change(new: str) -> None:
    right = edit(CONTRACT, "ყოველ ვადაგადაცილებულ დღეზე", new)
    assert rules(run(CONTRACT, right)) == ["CMP.TEXT_CHANGED"]


def test_an_english_penalty_s_day_basis_changed_is_a_text_change() -> None:
    left = ["5.1. The Supplier shall pay a penalty of 0.1% of the price for each calendar day."]
    right = [left[0].replace("calendar", "working")]
    assert rules(run(left, right)) == ["CMP.TEXT_CHANGED"]


def test_a_penalty_s_period_changed_alone_is_still_one_penalty_change() -> None:
    right = edit(CONTRACT, "ყოველ ვადაგადაცილებულ დღეზე", "ყოველ ვადაგადაცილებულ კვირაზე")
    assert found(run(CONTRACT, right)) == [
        ("CMP.PERCENT", "0.1% per day", "0.1% per week", "error")
    ]


# --- COMPARE-R4-09, R4-10: what a VAT inclusion's price covers ---------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "ფასი მოიცავს ტრანსპორტირების, მონტაჟის ხარჯებს და დღგ-ს.",
        "ფასი მოიცავს ტრანსპორტირების, მონტაჟის, დაზღვევის და სხვა ხარჯებს, ასევე დღგ-ს.",
    ],
)
def test_a_vat_inclusion_after_a_list_of_costs_is_read(line: str) -> None:
    assert [h.value for h in hits_of(line) if h.type is FactType.vat_status] == ["incl"]


@pytest.mark.parametrize(
    "line",
    [
        # a turn of the sentence: the tax is another clause's
        "ფასი მოიცავს ტრანსპორტირების, მონტაჟის ხარჯებს, ხოლო დღგ ემატება ფასს.",
        # the tax excluded: no inclusion
        "ფასი მოიცავს ტრანსპორტირების, მონტაჟის ხარჯებს და არ მოიცავს დღგ-ს.",
        # another sentence
        "ფასი მოიცავს ტრანსპორტირების, მონტაჟის ხარჯებს. მიმწოდებელი დღგ-ს გადამხდელია.",
    ],
)
def test_a_list_of_costs_does_not_carry_an_inclusion_past_its_clause(line: str) -> None:
    assert "incl" not in [h.value for h in hits_of(line) if h.type is FactType.vat_status]


def test_a_cost_added_to_what_the_price_covers_is_a_text_change_not_vat_removed() -> None:
    right = edit(CONTRACT, "ტრანსპორტირების ხარჯებს", "ტრანსპორტირების, მონტაჟის ხარჯებს")
    assert rules(run(CONTRACT, right)) == ["CMP.TEXT_CHANGED"]


@pytest.mark.parametrize("costs", ["მონტაჟის ხარჯებს", "ყველა ხარჯს"])
def test_a_change_in_what_the_price_covers_is_a_text_change(costs: str) -> None:
    right = edit(CONTRACT, "ტრანსპორტირების ხარჯებს", costs)
    assert rules(run(CONTRACT, right)) == ["CMP.TEXT_CHANGED"]


def test_the_tax_restyled_inside_an_inclusion_is_still_formatting() -> None:
    right = edit(CONTRACT, "ხარჯებს და დღგ-ს", "ხარჯებს და დამატებული ღირებულების გადასახადს")
    assert found(run(CONTRACT, right)) == []


# --- COMPARE-R4-11: a rate's hyphenated case ending is the rate's own --------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("0.1%-ის ოდენობით", "0.1 პროცენტის ოდენობით"),
        ("5%-ის ოდენობის", "5 პროცენტის ოდენობის"),
        ("5%-ის ოდენობის", "5 (ხუთი) პროცენტის ოდენობის"),
        ("5%-ის ოდენობის", "ხუთი (5) პროცენტის ოდენობის"),
        ("30%-იან ავანსს", "30% ავანსს"),
    ],
)
def test_a_rate_s_hyphenated_case_ending_restyled_is_formatting(old: str, new: str) -> None:
    assert found(run(CONTRACT, edit(CONTRACT, old, new))) == []


def test_a_rate_changed_with_its_case_ending_restyled_is_the_rate_s_change() -> None:
    right = edit(CONTRACT, "5%-ის ოდენობის", "7 პროცენტის ოდენობის")
    assert found(run(CONTRACT, right)) == [("CMP.PERCENT", "5%", "7%", "error")]


def test_a_word_after_a_rate_and_a_spaced_dash_is_no_case_ending() -> None:
    left = ["5.1. ჯარიმა: 5% - ერთჯერადად."]
    right = ["5.1. ჯარიმა: 5% - განმეორებით."]
    assert rules(run(left, right)) == ["CMP.TEXT_CHANGED"]


# --- COMPARE-R4-12: a unit cell restyled to the same unit is formatting ------------------------

HEADER = ["N", "დასახელება", "ზომის ერთეული", "რაოდენობა", "ერთეულის ფასი, ლარი"]
ROWS = [
    ["1", "ფარდების რეცხვა", "კვ.მ", "140", "12.00"],
    ["2", "სანაგვე კონტეინერი", "ცალი", "7", "55.00"],
    ["3", "ქვიშის შემოტანა", "კუბ.მ", "4", "70.00"],
]


def table(rows: list[list[str]]) -> list[Segment]:
    records: list[tuple] = [("დანართი N2. ფასების ცხრილი", "text", "¶1", None, None)]  # type: ignore[type-arg]
    for r, row in enumerate([HEADER, *rows], start=1):
        for c, text in enumerate(row, start=1):
            header = HEADER[c - 1] if r > 1 else None
            records.append(
                (text, "table_cell", f"T1/r{r}/c{c}", None, None, TableCell(1, r, c, header))
            )
    return build(records)


def with_unit(row: int, unit: str) -> list[list[str]]:
    rows = [list(r) for r in ROWS]
    rows[row][2] = unit
    return rows


@pytest.mark.parametrize(("row", "unit"), [(0, "მ2"), (1, "ც."), (2, "მ3"), (0, "კვ. მეტრი")])
def test_a_unit_cell_restyled_to_the_same_unit_is_formatting(row: int, unit: str) -> None:
    assert found(run(table(ROWS), table(with_unit(row, unit)))) == []


def test_a_unit_cell_changed_to_another_unit_is_still_the_row_s_quantity() -> None:
    assert found(run(table(ROWS), table(with_unit(0, "ცალი")))) == [
        ("CMP.QUANTITY", "140 m2", "140 item", "error")
    ]


# --- COMPARE-R4-14 (policy): a fee per month or per period is a price, never a second total ----

FEES = [
    "1. ღირებულება",
    "1.1. ხელშეკრულების საერთო ღირებულება შეადგენს 12 000 ლარს.",
    "1.2. ყოველთვიური მომსახურების ღირებულება შეადგენს 1 000 ლარს.",
    "2. დასკვნითი დებულებები",
    "2.1. ხელშეკრულება შედგენილია ორ ეგზემპლარად.",
]


def test_a_monthly_fee_in_its_own_clause_is_a_monthly_fee_not_the_total() -> None:
    right = edit(FEES, "1 000 ლარს", "1 100 ლარს")
    assert labelled(run(FEES, right)) == [
        ("CMP.AMOUNT", "Monthly fee", "1,000.00 GEL", "1,100.00 GEL", "error")
    ]


def test_a_monthly_fee_clause_deleted_is_the_monthly_fee_removed() -> None:
    right = [line for line in FEES if not line.startswith("1.2.")]
    assert labelled(run(FEES, right)) == [
        ("CMP.AMOUNT", "Monthly fee", "1,000.00 GEL", None, "error")
    ]


def test_the_total_beside_a_monthly_fee_is_still_the_total() -> None:
    right = edit(FEES, "12 000 ლარს", "13 000 ლარს")
    assert labelled(run(FEES, right)) == [
        ("CMP.AMOUNT", "Total amount", "12,000.00 GEL", "13,000.00 GEL", "error")
    ]


@pytest.mark.parametrize(
    ("line", "label"),
    [
        ("1.2. მომსახურების საფასური შეადგენს 1 000 ლარს თვეში.", "Monthly fee"),
        ("1.2. The monthly service fee is GEL 1,000.", "Monthly fee"),
        ("1.2. The service fee is GEL 1,000 per month.", "Monthly fee"),
        ("1.2. დღიური მომსახურების ღირებულება შეადგენს 1 000 ლარს.", "Unit price"),
    ],
)
def test_a_fee_per_period_is_a_price_in_every_wording(line: str, label: str) -> None:
    left = [*FEES[:2], line, *FEES[3:]]
    right = edit(
        left, "1,000" if "1,000" in line else "1 000", "1,100" if "1,000" in line else "1 100"
    )
    [(rule, got, *_)] = labelled(run(left, right))
    assert (rule, got) == ("CMP.AMOUNT", label)


@pytest.mark.parametrize(
    "line",
    [
        # the total paid month by month is still the total
        "1.1. ხელშეკრულების ღირებულება შეადგენს 12 000 ლარს და გადაიხდება ყოველთვიურად.",
        # six months' service is a total, not a monthly fee
        "1.1. 6 თვის მომსახურების ღირებულება შეადგენს 12 000 ლარს.",
        "1.1. The total price of 12 months of service is GEL 12,000, paid monthly.",
    ],
)
def test_a_total_paid_monthly_or_for_months_stays_the_total(line: str) -> None:
    left = [FEES[0], line, *FEES[3:]]
    right = edit(
        left, "12 000" if "12 000" in line else "12,000", "13 000" if "12 000" in line else "13,000"
    )
    [(rule, got, *_)] = labelled(run(left, right))
    assert (rule, got) == ("CMP.AMOUNT", "Total amount")


# --- COMPARE-R4-02, R4-03, R4-13: the comparison's side of values the readers read -------------
#
# The readers' side (rules) is fixed apart; the pairs whose reading waits on it are skipped until
# the reader reads them, and then hold the comparison to its whole result.

DELIVERY = [
    "1. მიწოდება",
    "1.1. საქონელი მიწოდებული უნდა იქნეს ხელშეკრულების გაფორმებიდან 20 კალენდარული დღის ვადაში.",
    "2. ანგარიშსწორება",
    "2.1. ანგარიშსწორება განხორციელდება აქტის გაფორმებიდან 10 სამუშაო დღის ვადაში.",
]


def _reads(text: str, ft: FactType) -> bool:
    return any(h.type is ft for h in hits_of(text))


@pytest.mark.parametrize(
    "new", ["20 დღეში", "20 დღის ვადაში", "20 კალენდარულ დღეში", "20-დღიან ვადაში"]
)
def test_a_delivery_term_restyled_is_formatting(new: str) -> None:
    if not _reads(f"საქონელი მიწოდებული უნდა იქნეს {new}.", FactType.delivery_deadline):
        pytest.skip("the duration reader does not read this form yet")
    right = edit(DELIVERY, "20 კალენდარული დღის ვადაში", new)
    assert found(run(DELIVERY, right)) == []


@pytest.mark.parametrize(
    ("old", "new"), [("20-დღიან", "30-დღიან"), ("20 კალენდარულ", "30 კალენდარულ")]
)
def test_a_delivery_term_changed_in_another_form_is_the_term_s_change(old: str, new: str) -> None:
    left = edit(
        DELIVERY, "20 კალენდარული დღის ვადაში", f"{old} {'ვადაში' if '-' in old else 'დღეში'}"
    )
    if not _reads(left[1], FactType.delivery_deadline):
        pytest.skip("the duration reader does not read this form yet")
    assert found(run(left, edit(left, old, new))) == [("CMP.DURATION", "P20D", "P30D", "error")]


def test_a_delivery_term_that_drops_its_working_days_is_still_a_change() -> None:
    left = edit(DELIVERY, "20 კალენდარული დღის ვადაში", "20 სამუშაო დღის ვადაში")
    right = edit(DELIVERY, "20 კალენდარული დღის ვადაში", "20 დღეში")
    assert rules(run(left, right)) == ["CMP.TEXT_CHANGED"]


def test_a_term_s_word_changed_beside_its_count_is_still_a_text_change() -> None:
    right = edit(DELIVERY, "20 კალენდარული დღის ვადაში", "20 კალენდარული დღის შემდეგ")
    assert rules(run(DELIVERY, right)) == ["CMP.TEXT_CHANGED"]


@pytest.mark.parametrize(
    ("old", "new"), [("5%-ის", "5 (ხუთი)%-ის"), ("0.1%-ის", "0.1 (ნული მთელი ერთი) %-ის")]
)
def test_a_rate_s_words_before_its_percent_sign_are_formatting(old: str, new: str) -> None:
    if not [h for h in hits_of(f"ღირებულების {new} ოდენობით") if h.value.endswith("%")]:
        pytest.skip("the percentage reader does not read this form yet")
    assert found(run(CONTRACT, edit(CONTRACT, old, new))) == []


def test_a_rate_written_with_its_words_before_the_sign_changed_is_the_rate_s_change() -> None:
    left = edit(CONTRACT, "5%-ის", "5 (ხუთი)%-ის")
    if not [h for h in hits_of(left[11]) if h.value == "5%"]:
        pytest.skip("the percentage reader does not read this form yet")
    assert found(run(left, edit(left, "5 (ხუთი)%", "7 (შვიდი)%"))) == [
        ("CMP.PERCENT", "5%", "7%", "error")
    ]


def test_a_contract_number_retyped_in_lower_case_is_the_same_number() -> None:
    right = edit(CONTRACT, "KL-2026/044", "kl-2026/044")
    assert found(run(CONTRACT, right)) == []


def test_a_contract_number_changed_in_lower_case_is_reported_upper_case() -> None:
    left = edit(CONTRACT, "KL-2026/044", "kl-2026/044")
    assert found(run(left, edit(left, "kl-2026/044", "kl-2026/045"))) == [
        ("CMP.CONTRACT_NO", "KL-2026/044", "KL-2026/045", "error")
    ]


def test_a_contract_number_s_georgian_letter_keeps_its_case() -> None:
    left = edit(CONTRACT, "KL-2026/044", "ს-12/2026")
    right = edit(left, "ს-12/2026", "ს-13/2026")
    assert found(run(left, right)) == [("CMP.CONTRACT_NO", "ს-12/2026", "ს-13/2026", "error")]


def test_a_monthly_fee_beside_the_total_in_one_sentence_is_the_monthly_fee() -> None:
    left = [
        "1. ღირებულება",
        "1.1. ყოველთვიური ღირებულება შეადგენს 4 000 ლარს, ხოლო ხელშეკრულების საერთო ღირებულება "
        "შეადგენს 48 000 ლარს.",
    ]
    fee = labelled(run(left, edit(left, "4 000", "4 500")))
    total = labelled(run(left, edit(left, "48 000", "54 000")))
    assert [(rule, label) for rule, label, *_ in fee] == [("CMP.AMOUNT", "Monthly fee")]
    assert [(rule, label) for rule, label, *_ in total] == [("CMP.AMOUNT", "Total amount")]


def test_an_advance_paid_monthly_is_no_monthly_fee() -> None:
    left = ["1.1. ყოველთვიური ავანსის თანხა შეადგენს 500 ლარს."]
    [(_rule, label, *_)] = labelled(run(left, edit(left, "500", "600")))
    assert label != "Monthly fee"
