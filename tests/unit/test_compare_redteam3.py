"""Comparison defects a red team found and a verifier reproduced (round 3), one test each, with
a near miss beside every widened reading.

Every document here is invented: names, codes, accounts and addresses are made up.
"""

from __future__ import annotations

import time

from procurement_agent.checks.compare import compare, hits_of
from procurement_agent.eval.runner import compare_label
from procurement_agent.ingestion.segmenter import Segment, TableCell, build

CONTRACT = [
    "ხელშეკრულება № QT-2026/305 საოფისე ავეჯის მიწოდების შესახებ",
    "ქ. ზუგდიდი, 2026 წლის 9 მარტი",
    "შპს „ვერცხლის ხეობა“, საიდენტიფიკაციო კოდი: 405123987, იურიდიული მისამართი: ქ. ზუგდიდი, "
    "ნაძვის ქ. N12 (შემდგომში „შემსყიდველი“), წარმოდგენილი დირექტორის ეკა ფიფია სახით, და",
    "შპს „ოქროს მინდორი“, საიდენტიფიკაციო კოდი: 405987321, იურიდიული მისამართი: ქ. სენაკი, "
    "ცაცხვის ქ. N8 (შემდგომში „მიმწოდებელი“), წარმოდგენილი დირექტორის დავით ჯანელიძის სახით.",
    "1. ხელშეკრულების საგანი",
    "1.1. მიმწოდებელი ვალდებულია მიაწოდოს შემსყიდველს საოფისე ავეჯი დანართი №1-ის შესაბამისად.",
    "2. ღირებულება და ანგარიშსწორება",
    "2.1. ხელშეკრულების ჯამური ღირებულება შეადგენს 12 300 (თორმეტი ათას სამასი) ლარს, ყველა "
    "გადასახადების ჩათვლით.",
    "2.2. ანგარიშსწორება მოხდება მიღება-ჩაბარების აქტის გაფორმებიდან 15 (თხუთმეტი) სამუშაო დღის "
    "ვადაში.",
    "3. საგარანტიო ვადა",
    "3.1. საქონელზე ვრცელდება 18 (თვრამეტი) თვის საგარანტიო ვადა.",
    "4. პასუხისმგებლობა",
    "4.1. მიწოდების ვადის გადაცილებისთვის მიმწოდებელს ეკისრება პირგასამტეხლო ღირებულების 0.3% "
    "ოდენობით ყოველ ვადაგადაცილებულ დღეზე.",
    "5. დასკვნითი დებულებები",
    "5.1. ხელშეკრულება შედგენილია 2 (ორი) ეგზემპლარად, თანაბარი იურიდიული ძალით.",
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


def labelled(report) -> list[tuple[str, str, str | None, str | None, str]]:  # type: ignore[no-untyped-def]
    return [
        (f.rule_id, compare_label(f, report), f.expected, f.actual, f.severity)
        for f in report.findings
        if f.severity != "info" and f.rule_id != "CMP.FORMATTING"
    ]


def table(rows: list[list[str]], *, before: list[str] | None = None) -> list[Segment]:
    """A Word table as Docling gives it: the first row the header, every other cell under it."""
    records: list[tuple] = [  # type: ignore[type-arg]
        (t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(before or [])
    ]
    header = rows[0]
    for r, row in enumerate(rows, start=1):
        for c, text in enumerate(row, start=1):
            column = header[c - 1] if r > 1 else None
            records.append(
                (text, "table_cell", f"T1/r{r}/c{c}", None, None, TableCell(1, r, c, column))
            )
    return build(records)


# --- COMPARE-R3-01: a penalty's period changed is one change, whatever case ending it takes -----

PENALTY = "ყოველ ვადაგადაცილებულ დღეზე"


def test_a_penalty_period_changed_to_a_week_is_one_percent_change() -> None:
    right = edit(CONTRACT, PENALTY, "ყოველ ვადაგადაცილებულ კვირაზე")
    assert found(run(CONTRACT, right)) == [
        ("CMP.PERCENT", "0.3% per day", "0.3% per week", "error")
    ]


def test_a_penalty_period_changed_to_a_year_is_one_percent_change() -> None:
    right = edit(CONTRACT, PENALTY, "ყოველ ვადაგადაცილებულ წელზე")
    assert found(run(CONTRACT, right)) == [
        ("CMP.PERCENT", "0.3% per day", "0.3% per year", "error")
    ]


def test_a_penalty_rate_and_period_changed_together_are_one_change() -> None:
    week = "0.4% ოდენობით ყოველ ვადაგადაცილებულ კვირაზე"
    right = edit(CONTRACT, "0.3% ოდენობით " + PENALTY, week)
    assert found(run(CONTRACT, right)) == [
        ("CMP.PERCENT", "0.3% per day", "0.4% per week", "error")
    ]


def test_near_miss_a_penalty_period_changed_with_the_party_it_falls_on_is_still_two() -> None:
    # the mask takes the period word whole, and no word after it
    right = edit(CONTRACT, "მიმწოდებელს ეკისრება", "შემსყიდველს ეკისრება")
    right = edit(right, PENALTY, "ყოველ ვადაგადაცილებულ კვირაზე")
    rules = [r[0] for r in found(run(CONTRACT, right))]
    assert sorted(rules) == ["CMP.PERCENT", "CMP.TEXT_CHANGED"]


# --- COMPARE-R3-05: a long block of lightly edited clauses is still two versions ---------------


def obligations(n: int, word: str) -> list[str]:
    return [
        f"8.{i}. მიმწოდებელი {word} შეასრულოს სამუშაო სათანადო ხარისხით, პუნქტის ნომერი {i}."
        for i in range(1, n + 1)
    ]


def test_more_than_fifty_edited_clauses_in_a_row_are_still_two_versions() -> None:
    head = CONTRACT[:6]
    left = head + obligations(55, "ვალდებულია")
    right = head + obligations(55, "ასევე ვალდებულია")
    right.append("8.56. ვადის დარღვევისას ეკისრება ჯარიმა ღირებულების 5%-ის ოდენობით.")
    report = run(left, right)
    assert report.alignment == "versions"
    assert report.similarity > 0.9
    percent = [(f.rule_id, f.actual, f.severity) for f in report.findings if f.actual == "5%"]
    assert percent == [("CMP.PERCENT", "5%", "error")]
    assert len([f for f in report.findings if f.rule_id == "CMP.TEXT_CHANGED"]) == 55


def test_near_miss_two_long_unrelated_blocks_stay_unrelated() -> None:
    left = [f"{i}. ნაწილი პირველი, ტექსტი ალფა ბეტა გამა {i * 7}" for i in range(1, 61)]
    right = [f"სხვა დოკუმენტის აბზაცი {i}: ზღვა, მთა, ტყე და ველი {i * 13}" for i in range(1, 61)]
    report = run(left, right)
    assert report.alignment == "unrelated"


def test_a_long_block_of_edited_clauses_is_paired_quickly() -> None:
    left = obligations(600, "ვალდებულია")
    right = obligations(600, "ასევე ვალდებულია")
    started = time.perf_counter()
    report = run(left, right)
    assert time.perf_counter() - started < 20
    assert report.alignment == "versions"


# --- COMPARE-R3-06: a director replaced in a requisites table, one party per column -------------

REQUISITES = [
    ["შემსყიდველი", "მიმწოდებელი"],
    ["შპს „ვერცხლის ხეობა“", "შპს „ოქროს მინდორი“"],
    ["ს/კ 405123987", "ს/კ 405987321"],
    ["დირექტორი: ეკა ფიფია", "დირექტორი: დავით ჯანელიძე"],
]


def requisites(director: str) -> list[Segment]:
    rows = [list(r) for r in REQUISITES]
    rows[3][1] = f"დირექტორი: {director}"
    return table(rows, before=CONTRACT)


def test_a_director_replaced_in_a_requisites_table_is_a_party_change() -> None:
    report = run(requisites("დავით ჯანელიძე"), requisites("ლუკა ჯანელიძე"))
    assert labelled(report) == [
        ("CMP.PARTY", "Director / signatory", "დავით ჯანელიძე", "ლუკა ჯანელიძე", "error")
    ]


def test_a_director_replaced_under_a_party_in_a_signature_block_is_one_party_change() -> None:
    block = [*CONTRACT, "მიმწოდებელი:", "შპს „ოქროს მინდორი“", "დირექტორი: დავით ჯანელიძე"]
    right = edit(block, "დირექტორი: დავით ჯანელიძე", "დირექტორი: ლუკა ჯანელიძე")
    assert labelled(run(block, right)) == [
        ("CMP.PARTY", "Director / signatory", "დავით ჯანელიძე", "ლუკა ჯანელიძე", "error")
    ]


def test_near_miss_a_clause_after_a_party_line_is_not_read_as_that_partys() -> None:
    long_clause = "ხელშეკრულების შესრულებაზე პასუხისმგებელია დირექტორი: " + "ა" * 90
    block = [*CONTRACT, "შპს „ოქროს მინდორი“", long_clause]
    right = edit(block, "დირექტორი: ", "დირექტორი: ლუკა ")
    # the line is read as it stands, not under the party line before it
    [hunk] = [h for h in run(block, right).hunks if h.kind == "changed"]
    assert "CMP.PARTY" not in hunk.rules


def test_near_miss_a_director_cell_in_a_table_of_no_parties_is_a_text_change() -> None:
    def rows(director: str) -> list[Segment]:
        return table([["პოზიცია", "შენიშვნა"], ["ოფისი", f"დირექტორი: {director}"]])

    report = run(rows("დავით ჯანელიძე"), rows("ლუკა ჯანელიძე"))
    assert [r[0] for r in found(report)] == ["CMP.TEXT_CHANGED"]


# --- COMPARE-R3-07: a count restated in words only is the same term ----------------------------


def test_a_payment_term_restated_in_words_only_is_the_same_term() -> None:
    right = edit(CONTRACT, "15 (თხუთმეტი) სამუშაო", "თხუთმეტი სამუშაო")
    assert found(run(CONTRACT, right)) == []


def test_a_warranty_restated_in_words_only_is_the_same_term() -> None:
    right = edit(CONTRACT, "18 (თვრამეტი) თვის", "თვრამეტი თვის")
    assert found(run(CONTRACT, right)) == []


def test_a_payment_term_changed_into_words_only_is_a_payment_change() -> None:
    right = edit(CONTRACT, "15 (თხუთმეტი) სამუშაო", "ოცი სამუშაო")
    assert labelled(run(CONTRACT, right)) == [
        ("CMP.PAYMENT", "Payment terms", "P15D working", "P20D working", "error")
    ]


def test_near_miss_an_ordinal_or_a_count_of_parties_is_no_duration() -> None:
    assert not [h for h in hits_of("მიწოდება მოხდება მეორე დღეს.") if h.type.value != "other"]
    assert not hits_of("ორი მხარე შეთანხმდა.")


# --- COMPARE-R3-08: a figure and its words changed together in a cell are one change -----------

PRICES = [
    ["№", "დასახელება", "ღირებულება (ლარი)"],
    ["1", "მიწოდება", "2 100 (ორი ათას ასი)"],
    ["2", "მონტაჟი", "6 450 (ექვსი ათას ოთხას ორმოცდაათი)"],
]


def priced(cell: str) -> list[Segment]:
    rows = [list(r) for r in PRICES]
    rows[2][2] = cell
    return table(rows, before=["დანართი №2. ფასების ცხრილი"])


def test_a_figure_and_its_words_raised_together_in_a_cell_are_one_change() -> None:
    report = run(priced(PRICES[2][2]), priced("6 950 (ექვსი ათას ცხრაას ორმოცდაათი)"))
    assert found(report) == [("CMP.AMOUNT", "6 450", "6 950", "error")]


def test_near_miss_words_in_brackets_that_are_no_figure_stay_other_words() -> None:
    left = ["იხილეთ დანართი 2 (ტექნიკური დავალება) და მისი პირობები."]
    right = ["იხილეთ დანართი 2 (სატენდერო დავალება) და მისი პირობები."]
    assert [r[0] for r in found(run(left, right))] == ["CMP.TEXT_CHANGED"]


# --- COMPARE-R3-09: the same value written another way is formatting --------------------------


def test_a_rate_restated_with_its_words_before_the_figure_is_formatting() -> None:
    right = edit(CONTRACT, "0.3% ოდენობით", "ნული მთელი სამი (0.3) პროცენტის ოდენობით")
    assert found(run(CONTRACT, right)) == []


def test_near_miss_a_rate_with_words_that_say_another_rate_is_a_text_change() -> None:
    right = edit(CONTRACT, "0.3% ოდენობით", "ნული მთელი ხუთი (0.3) პროცენტის ოდენობით")
    assert [r[0] for r in found(run(CONTRACT, right))] == ["CMP.TEXT_CHANGED"]


def test_near_miss_a_rate_restated_in_words_beside_another_changed_word_is_a_text_change() -> None:
    right = edit(CONTRACT, "0.3% ოდენობით", "ნული მთელი სამი (0.3) პროცენტის ოდენობით")
    right = edit(right, "მიმწოდებელს ეკისრება", "შემსყიდველს ეკისრება")
    assert [r[0] for r in found(run(CONTRACT, right))] == ["CMP.TEXT_CHANGED"]


def test_a_party_with_its_legal_form_after_the_name_is_formatting() -> None:
    left = ["შპს „ყვითელი ფოთოლი“, საიდენტიფიკაციო კოდი: 402555111"]
    right = ["„ყვითელი ფოთოლი“ შპს, საიდენტიფიკაციო კოდი: 402555111"]
    assert found(run(left, right)) == []


def test_near_miss_a_party_renamed_while_its_form_moved_is_a_party_change() -> None:
    left = ["შპს „ყვითელი ფოთოლი“, საიდენტიფიკაციო კოდი: 402555111"]
    right = ["„მწვანე ფოთოლი“ შპს, საიდენტიფიკაციო კოდი: 402555111"]
    assert [r[0] for r in found(run(left, right))] == ["CMP.PARTY"]


# --- COMPARE-R3-12: a price table's total row is the total amount -----------------------------

BILL = [
    ["№", "დასახელება", "ერთეული", "რაოდენობა", "ერთეულის ფასი (ლარი)", "ჯამი (ლარი)"],
    ["1", "სკამი", "ცალი", "40", "120", "4 800"],
    ["2", "მაგიდა", "ცალი", "10", "350", "3 500"],
    ["", "სულ", "", "", "", "8 300"],
]


def bill(row: int, col: int, text: str) -> list[Segment]:
    rows = [list(r) for r in BILL]
    rows[row][col] = text
    return table(rows, before=["დანართი №1. ხარჯთაღრიცხვა"])


def test_a_total_rows_figure_changed_is_the_total_amount() -> None:
    report = run(bill(3, 5, "8 300"), bill(3, 5, "8 900"))
    assert labelled(report) == [("CMP.AMOUNT", "Total amount", "8 300", "8 900", "error")]


def test_near_miss_an_item_rows_line_total_is_not_the_total_amount() -> None:
    report = run(bill(1, 5, "4 800"), bill(1, 5, "4 850"))
    assert [(r[0], r[1]) for r in labelled(report)] == [("CMP.AMOUNT", "Number")]


# --- COMPARE-R3-03 (compare side): a day range before one month and year is one period --------

WINDOW = "2.3. საქონელი მიწოდებული უნდა იქნეს 01.05.2026 - 31.05.2026 პერიოდში."


def test_a_window_restyled_as_a_day_range_with_figures_is_formatting() -> None:
    right = [*CONTRACT, WINDOW.replace("01.05.2026 - 31.05.2026", "01-31.05.2026")]
    assert found(run([*CONTRACT, WINDOW], right)) == []


def test_a_window_restyled_as_a_day_range_with_the_month_named_is_formatting() -> None:
    right = [*CONTRACT, WINDOW.replace("01.05.2026 - 31.05.2026", "1-31 მაისი, 2026 წ.")]
    assert found(run([*CONTRACT, WINDOW], right)) == []


def test_a_day_ranges_end_changed_is_one_period_change() -> None:
    left = [*CONTRACT, WINDOW.replace("01.05.2026 - 31.05.2026", "01-31.05.2026")]
    right = [*CONTRACT, WINDOW.replace("01.05.2026 - 31.05.2026", "01-30.05.2026")]
    assert labelled(run(left, right)) == [
        (
            "CMP.DURATION",
            "Execution period",
            "2026-05-01/2026-05-31",
            "2026-05-01/2026-05-30",
            "error",
        )
    ]


def test_a_date_read_with_its_year_after_inclusive_is_until_that_day() -> None:
    # COMPARE-R3-02, compare side: the role word stays the date's when the year follows it
    from procurement_agent.checks.compare import _postposition

    assert _postposition("30 ნოემბრის ჩათვლით, 2026 წ.", None) == "until"
    assert _postposition("30 ნოემბრამდე, 2026 წ.", None) == "until"
    assert _postposition("30 ნოემბრის, 2026 წ.", None) == ""


def test_near_miss_two_dates_joined_by_and_stay_two_dates() -> None:
    hits = hits_of("შეხვედრები: 01.05.2026 და 31.05.2026.")
    assert not [h for h in hits if "/" in h.value]


# --- mutation harness: a one-letter slip in the word that carries a value's wording ------------


def test_a_one_letter_slip_in_the_tax_wording_does_not_flip_the_vat_status() -> None:
    right = edit(CONTRACT, "გადასახადების ჩათვლით", "გძდასახადების ჩათვლით")
    rules = [r[0] for r in found(run(CONTRACT, right))]
    assert "CMP.VAT" not in rules
    assert rules in (["CMP.TEXT_CHANGED"], ["CMP.SPELLING"])


def test_a_one_letter_slip_in_the_inclusive_word_does_not_flip_the_vat_status() -> None:
    right = edit(CONTRACT, "გადასახადების ჩათვლით", "გადასახადების ჩათვლთ")
    assert "CMP.VAT" not in [r[0] for r in found(run(CONTRACT, right))]


def test_near_miss_the_tax_wording_really_changed_is_a_vat_change() -> None:
    right = edit(CONTRACT, "გადასახადების ჩათვლით", "გადასახადების გარეშე")
    assert "CMP.VAT" in [r[0] for r in found(run(CONTRACT, right))]


def test_near_miss_a_slip_beside_a_changed_price_leaves_the_price_change() -> None:
    right = edit(CONTRACT, "გადასახადების ჩათვლით", "გძდასახადების ჩათვლით")
    right = edit(right, "12 300 (თორმეტი ათას სამასი)", "12 900 (თორმეტი ათას ცხრაასი)")
    rules = [r[0] for r in found(run(CONTRACT, right))]
    assert "CMP.AMOUNT" in rules


def test_near_miss_a_month_one_letter_away_is_a_date_change() -> None:
    left = ["მიწოდება განხორციელდება 2026 წლის 5 ივნისს."]
    right = ["მიწოდება განხორციელდება 2026 წლის 5 ივლისს."]
    rules = [r for r in found(run(left, right)) if r[0] != "CMP.TEXT_CHANGED"]
    assert rules and all("2026-06-05" in (r[1] or "") for r in rules)


def test_near_miss_a_surname_one_letter_away_is_a_party_change() -> None:
    right = edit(CONTRACT, "დავით ჯანელიძის", "დავით ჯანალიძის")
    assert "CMP.PARTY" in [r[0] for r in found(run(CONTRACT, right))]
