"""Comparison accuracy on the mistakes a reviewer actually plants: typos, spelled-out numbers,
reworded labels, renamed parties. Every document here is invented."""

from __future__ import annotations

import pytest

from procurement_agent.checks.compare import as_number, compare
from procurement_agent.extraction.labels import LABELS
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion.segmenter import Segment, build

ACT = [
    "შპს „ნიმუში“ (ს/კ 401000111), დირექტორი ნინო კაპანაძე.",
    "მომსახურება შესრულდა 2026 წლის 1 აგვისტოდან 2026 წლის 31 აგვისტოს ჩათვლით.",
    "მომსახურების საფასური შეადგენს 100 (ასი) ლარს და გადაიხდება 5 (ხუთი) სამუშაო დღეში.",
    "საავტორო და მომიჯნავე უფლებები ეკუთვნის შემკვეთს.",
    "აქტი შედგენილია 2 (ორი) ეგზემპლარად.",
]


def segs(texts: list[str]) -> list[Segment]:
    return build([(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(texts)])


def run(left: list[str], right: list[str]):  # type: ignore[no-untyped-def]
    return compare(
        segs(left),
        segs(right),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )


def found(left: list[str], right: list[str]) -> list[tuple[str, str | None, str | None]]:
    return [(f.rule_id, f.expected, f.actual) for f in run(left, right).findings]


def edit(i: int, old: str, new: str) -> list[str]:
    out = list(ACT)
    assert old in out[i]
    out[i] = out[i].replace(old, new)
    return out


def test_an_identical_copy_gives_nothing() -> None:
    assert found(ACT, list(ACT)) == []


def test_a_misspelled_number_is_a_typo_not_another_amount() -> None:
    # "(ასოი)" used to be paired with the next bracket and read as "ასი (100) → ხუთი (5)";
    # it is the fee's words misspelled - wrong as the fee would be wrong, so an amount error
    assert found(ACT, edit(2, "(ასი)", "(ასოი)")) == [("CMP.AMOUNT_WORDS", "ასი", "ასოი")]


def test_a_changed_number_in_words_is_labelled_by_what_it_counts() -> None:
    [amount] = run(ACT, edit(2, "(ასი)", "(ორასი)")).findings
    assert (amount.rule_id, amount.severity) == ("CMP.AMOUNT_WORDS", "error")
    assert amount.message_en.startswith("Amount in words")
    [days] = run(ACT, edit(2, "5 (ხუთი)", "5 (ოცდაათი)")).findings
    assert days.message_en.startswith("Period in words")
    [copies] = run(ACT, edit(4, "(ორი)", "(სამი)")).findings
    assert (copies.severity, copies.message_en.split(":")[0]) == ("warning", "Number in words")


def test_a_number_written_only_in_words_is_a_value() -> None:
    left = ["გადახდა ხდება ხუთი სამუშაო დღის ვადაში."]
    right = ["გადახდა ხდება ოცდაათი სამუშაო დღის ვადაში."]
    [f] = run(left, right).findings
    assert (f.rule_id, f.severity) == ("CMP.AMOUNT_WORDS", "error")
    assert (f.expected, f.actual) == ("ხუთი (5)", "ოცდაათი (30)")


def test_a_month_typo_and_a_stray_latin_letter_are_spelling() -> None:
    assert found(ACT, edit(1, "აგვისტოდან", "აგისტოდან")) == [
        ("CMP.SPELLING", "აგვისტოდან", "აგისტოდან")
    ]
    assert found(ACT, edit(3, "მომიჯნავე", "sმომიჯნავე")) == [
        ("CMP.SPELLING", "მომიჯნავე", "sმომიჯნავე")
    ]


def test_a_changed_word_in_a_long_paragraph_is_quoted_where_it_changed() -> None:
    """A finding that quoted the whole paragraph, cut at 160 characters, showed two identical
    texts when the change lay further on; it quotes the change and a few words around it."""
    long_ = (
        "მომსახურების საფასური გადაიხდება ამ აქტის გაფორმების თარიღიდან ხუთი სამუშაო დღის "
        "განმავლობაში, შემსრულებლის მიერ წარმოდგენილი ანგარიშფაქტურის საფუძველზე, "
        "შემსრულებლის საბანკო ანგარიშზე უნაღდო ანგარიშსწორების წესით."
    )
    assert len(long_) > 160
    [f] = run([*ACT, long_], [*ACT, long_.replace("ანგარიშზე", "ანაგრიშზე")]).findings
    assert f.expected is not None and f.actual is not None
    assert "ანგარიშზე" in f.expected and "ანაგრიშზე" in f.actual
    assert len(f.expected) < 80 and f.expected.startswith("…") and f.expected.endswith("…")


def test_a_change_inside_a_table_cell_and_its_own_paragraph_is_one_finding() -> None:
    """A cell of two paragraphs reaches the diff whole and as its paragraphs (the parser emits
    both), so one typo in it was two "text changed" findings."""
    cell = "შპს „ალფა“ სამუშაოების შესრულება\nსამუშაოები შესრულდა ხარისხიანად და დროულად"
    line = "სამუშაოები შესრულდა ხარისხიანად და დროულად"
    left, right = [*ACT, cell, line], [*ACT, cell, line]
    right[-2] = right[-2].replace("ხარისხიანად", "ხარისხინად")
    right[-1] = right[-1].replace("ხარისხიანად", "ხარისხინად")
    [f] = run(left, right).findings
    assert f.expected is not None and "ხარისხიანად" in f.expected


def test_a_relabelled_price_is_not_a_change() -> None:
    left = ["ფასი შეადგენს 100 ლარს."]
    right = ["ღირებულება შეადგენს 100 ლარს."]
    assert [f for f in run(left, right).findings if f.severity == "error"] == []


def test_a_renamed_value_is_reported_once() -> None:
    right = edit(0, "ნინო კაპანაძე", "ნინო კაპანია")
    rules = [f.rule_id for f in run(ACT, right).findings]
    assert rules == ["CMP.PARTY"]


def test_a_one_line_document_with_an_edit_is_still_a_version() -> None:
    left = ["საფასური შეადგენს 100 (ასი) ლარს."]
    right = ["საფასური შეადგენს 200 (ორასი) ლარს."]
    report = run(left, right)
    assert report.alignment == "versions"
    # the words changed with the digits and still agree with them: one change, the amount's
    assert [f.rule_id for f in report.findings] == ["CMP.AMOUNT"]


def test_two_unrelated_documents_report_no_line_changes() -> None:
    left = [
        f"ხელშეკრულების პუნქტი {i}: მხარეები თანხმდებიან პირობაზე ნომერი {i}." for i in range(30)
    ]
    right = ["სრულიად სხვა დოკუმენტი.", "მასში სხვა ტექსტია.", "და სხვა მონაცემები."]
    report = run(left, right)
    assert report.alignment == "unrelated"
    assert not [f for f in report.findings if f.rule_id.startswith("CMP.TEXT_")]


def test_a_value_both_documents_carry_is_never_added_or_removed() -> None:
    # the cadastral code moves to another paragraph together with an edit around it
    left = ["პროექტი (ს/კ: 01.02.03.456) მდებარეობს ბათუმში.", "სხვა ტექსტი აქ არის."]
    right = [
        "სხვა ტექსტი აქ არის.",
        "ახალი აღწერა, პროექტი მდებარეობს ბათუმში (ს/კ: 01.02.03.456).",
    ]
    assert not [f for f in run(left, right).findings if f.rule_id == "CMP.CADASTRAL"]


# --- one change is one finding, and layout is not a change -----------------------------------

#: The parties laid out the way the acceptance acts lay them out: each defined with its side, the
#: customer's director named in the genitive in the preamble and in the nominative under the
#: signatures - once in the signature table, once as a line of its own.
PARTIES = [
    "შპს „ლურჯი ფრთა“ საიდენტიფიკაციო კოდი: 401000111; იურიდიული მისამართი: ქ. ბათუმი, "
    "ფიჭვის ქ. N7 (შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის ნინო კაპანაძის სახით და",
    "შპს „ვერცხლის ხიდი“ საიდენტიფიკაციო კოდი: 402000222, იურიდიული მისამართი: ქ. თბილისი, "
    "ცაცხვის ქ. N3 (შემდგომში - „შემსრულებელი“)",
    "მომსახურება შესრულდა სრულად და დროულად.",
    "“შემკვეთი”\nშპს „ლურჯი ფრთა“\nდირექტორი ნინო კაპანაძე",
    "დირექტორი ნინო კაპანაძე",
    "“შემსრულებელი”\nშპს „ვერცხლის ხიდი“\nდირექტორი: ლევან ხარაზი",
]


def substantive(report) -> list:  # type: ignore[no-untyped-def,type-arg]
    """What a reader is asked to look at: everything but formatting and information."""
    return [f for f in report.findings if f.severity != "info" and f.rule_id != "CMP.FORMATTING"]


def labels_of(report) -> set[str]:  # type: ignore[no-untyped-def]
    deltas = [d for h in report.hunks for d in h.deltas] + list(report.field_deltas)
    return {d.label_en for d in deltas}


def test_digits_and_words_changed_together_are_one_amount_change() -> None:
    [f] = substantive(run(ACT, edit(2, "100 (ასი)", "300 (სამასი)")))
    assert f.rule_id == "CMP.AMOUNT"
    assert (as_number(f.expected), as_number(f.actual)) == (100, 300)
    # the same for a period of days: the figure's own rule reports it, once - a payment term,
    # as the sentence pays within it ("გადაიხდება"; COMPARE-04)
    [f] = substantive(run(ACT, edit(2, "5 (ხუთი)", "10 (ათი)")))
    assert (f.rule_id, f.expected, f.actual) == ("CMP.PAYMENT", "P5D working", "P10D working")


def test_digits_or_words_changed_alone_are_still_reported() -> None:
    [f] = substantive(run(ACT, edit(2, "100 (ასი)", "300 (ასი)")))
    assert f.rule_id in ("CMP.AMOUNT", "CMP.AMOUNT_WORDS")
    [f] = substantive(run(ACT, edit(2, "(ასი)", "(სამასი)")))
    assert (f.rule_id, f.expected, f.actual) == ("CMP.AMOUNT_WORDS", "ასი (100)", "სამასი (300)")
    # a count no other rule reads keeps its words finding when digits and words change together
    [f] = substantive(run(ACT, edit(4, "2 (ორი)", "3 (სამი)")))
    assert (f.rule_id, f.expected, f.actual) == ("CMP.AMOUNT_WORDS", "ორი (2)", "სამი (3)")


def test_a_shifted_service_period_is_one_change_of_the_period() -> None:
    shifted = edit(1, "2026 წლის 1 აგვისტოდან 2026", "2027 წლის 1 აგვისტოდან 2027")
    report = run(ACT, shifted)
    [f] = substantive(report)
    assert f.rule_id in ("CMP.DURATION", "CMP.DATE")
    assert (f.expected, f.actual) == ("2026-08-01/2026-08-31", "2027-08-01/2027-08-31")
    assert LABELS[FactType.execution_period][0] in labels_of(report)
    # moving only the end is one change of the same period
    [f] = substantive(run(ACT, edit(1, "31 აგვისტოს", "15 სექტემბრის")))
    assert (f.expected, f.actual) == ("2026-08-01/2026-08-31", "2026-08-01/2026-09-15")


def test_a_range_of_plain_dates_is_one_change_but_two_listed_dates_stay_two() -> None:
    left = ["სამუშაოები შესრულდება 01.03.2026-დან 30.04.2026-მდე."]
    right = ["სამუშაოები შესრულდება 01.03.2027-დან 30.04.2027-მდე."]
    [f] = substantive(run(left, right))
    # two dates written as a range are a period: the time the works are done
    assert (f.rule_id, f.expected, f.actual) == (
        "CMP.DURATION",
        "2026-03-01/2026-04-30",
        "2027-03-01/2027-04-30",
    )
    left = ["ვადები: 2026 წლის 1 მარტი და 2026 წლის 20 ოქტომბერი."]
    right = ["ვადები: 2027 წლის 1 მარტი და 2027 წლის 20 ოქტომბერი."]
    assert len(substantive(run(left, right))) == 2


def test_a_director_renamed_in_every_case_form_is_one_change_in_the_nominative() -> None:
    right = [p.replace("კაპანაძ", "კაპნაძ") for p in PARTIES]
    report = run(PARTIES, right)
    [f] = substantive(report)
    assert (f.rule_id, f.expected, f.actual) == ("CMP.PARTY", "ნინო კაპანაძე", "ნინო კაპნაძე")
    assert LABELS[FactType.party_director][0] in labels_of(report)


def test_swapped_roles_are_one_critical_change_saying_who_holds_which_role() -> None:
    right = list(PARTIES)
    right[0] = right[0].replace("„შემკვეთი“", "„შემსრულებელი“")
    right[1] = right[1].replace("„შემსრულებელი“", "„შემკვეთი“")
    report = run(PARTIES, right)
    [f] = substantive(report)
    assert (f.rule_id, f.severity) == ("CMP.PARTY", "error")
    now = f.actual or ""
    assert now.index("ლურჯი ფრთა") < now.index("შემსრულებელი") < now.index("ვერცხლის ხიდი")
    assert now.index("ვერცხლის ხიდი") < now.rindex("შემკვეთი")
    # the two definitions carry the change themselves: nothing is left for the model to explain
    assert report.unexplained == []


def test_a_side_renamed_with_a_synonym_is_not_a_role_swap() -> None:
    left = [PARTIES[0], PARTIES[1].replace("„შემსრულებელი“", "„მიმწოდებელი“")]
    right = [PARTIES[0], PARTIES[1]]
    assert [f.rule_id for f in substantive(run(left, right))] == ["CMP.TEXT_CHANGED"]


def test_the_same_date_written_in_another_style_is_formatting() -> None:
    left = ["„აქტი“ გაფორმდა 2026 წლის 31 აგვისტო"]
    for style in ("31 აგვისტო, 2026 წ.", "31.08.2026"):
        report = run(left, [f"„აქტი“ გაფორმდა {style}"])
        assert substantive(report) == [], style
    # another date written in another style is still a changed date
    [f] = substantive(run(left, ["„აქტი“ გაფორმდა 30.08.2026"]))
    assert f.rule_id == "CMP.DATE"


SECTIONS = [
    "შესავალი ტექსტი აქ არის.",
    "1",
    "მომსახურება",
    "შემსრულებელმა შეასრულა მომსახურება.",
    "2",
    "საფასური",
    "საფასური შეადგენს 100 (ასი) ლარს.",
    "დასასრული.",
]


def test_a_table_of_numbered_sections_turned_into_paragraphs_is_layout() -> None:
    right = [
        "შესავალი ტექსტი აქ არის.",
        "1. მომსახურება",
        "შემსრულებელმა შეასრულა მომსახურება.",
        "2. საფასური",
        "საფასური შეადგენს 100 (ასი) ლარს.",
        "დასასრული.",
    ]
    report = run(SECTIONS, right)
    assert substantive(report) == []
    # every segment of both sides is still drawn
    assert {r.left.index for r in report.rows if r.left} == set(range(len(SECTIONS)))
    assert {r.right.index for r in report.rows if r.right} == set(range(len(right)))


def test_a_decimal_point_is_never_layout() -> None:
    left = ["შესავალი ტექსტი აქ არის.", "ფასი", "12.5 ლარი", "დასასრული."]
    right = ["შესავალი ტექსტი აქ არის.", "ფასი 125 ლარი", "დასასრული."]
    assert substantive(run(left, right)) != []


def test_a_currency_word_moved_to_the_next_paragraph_is_layout() -> None:
    fee = "საფასური შეადგენს 1 200 (ათას ორასი) ლარს და გადაიხდება 5 (ხუთი) სამუშაო დღის ვადაში."
    cut = fee.index("ლარს")
    left = ["სათაური", fee, "ბოლო სტრიქონი"]
    right = ["სათაური", fee[:cut], fee[cut:], "ბოლო სტრიქონი"]
    assert substantive(run(left, right)) == []


def test_names_without_their_quotes_are_the_same_names() -> None:
    right = [
        p.replace("„ლურჯი ფრთა“", "ლურჯი ფრთა").replace("„ვერცხლის ხიდი“", "ვერცხლის ხიდი")
        for p in PARTIES
    ]
    assert substantive(run(PARTIES, right)) == []


def test_a_director_named_nowhere_any_more_is_still_removed() -> None:
    right = [
        p.replace(" წარმოდგენილი დირექტორის ნინო კაპანაძის სახით და", "").replace(
            "\nდირექტორი ნინო კაპანაძე", ""
        )
        for p in PARTIES
        if p != "დირექტორი ნინო კაპანაძე"
    ]
    report = run(PARTIES, right)
    assert ("CMP.PARTY", "ნინო კაპანაძე", None) in [
        (f.rule_id, f.expected, f.actual) for f in report.findings
    ]


def test_a_removed_paragraph_is_reported_even_when_its_value_is_written_elsewhere() -> None:
    left = ["საფასური შეადგენს 100 ლარს.", "გადახდა ხდება ყოველთვიურად.", "ჯამში 100 ლარი."]
    assert [f.rule_id for f in run(left, left[1:]).findings] == ["CMP.TEXT_REMOVED"]


def test_a_stray_cyrillic_letter_in_a_word_is_one_spelling_finding() -> None:
    cyrillic_o = chr(0x043E)
    right = edit(3, "მომიჯნავე", f"მომი{cyrillic_o}ჯნავე")
    [f] = substantive(run(ACT, right))
    assert (f.rule_id, f.expected) == ("CMP.SPELLING", "მომიჯნავე")


def test_a_stray_letter_next_to_a_value_does_not_change_the_value() -> None:
    # "სამუშაო" (working) with a Latin "a": the payment term is still five working days
    [f] = substantive(run(ACT, edit(2, "სამუშაო", "სამუშaო")))
    assert (f.rule_id, f.expected) == ("CMP.SPELLING", "სამუშაო")
    # a Latin "c" inside the director's label: the director is still named
    right = [p.replace("დირექტორის ნინო", "დირექტორcის ნინო") for p in PARTIES]
    [f] = substantive(run(PARTIES, right))
    assert (f.rule_id, f.expected) == ("CMP.SPELLING", "დირექტორის")


def test_a_number_word_misspelled_into_a_shorter_word_is_reported() -> None:
    assert found(ACT, edit(2, "(ასი)", "(აი)")) == [("CMP.AMOUNT_WORDS", "ასი", "აი")]


def test_a_line_reflowed_around_a_removed_code_is_one_removal() -> None:
    # the code and its label are cut out, and the rest of the line merges with the next one;
    # the fragment that loses the project name is not a paragraph removed
    left = [
        "შესავალი ტექსტი.",
        "შემკვეთის პროექტ „ქარვის კოშკისთვის“ (ს/კ:",
        "01.02.03.456)",
        "ოთახების რემონტი, ფასადის შეღებვა და ეზოს კეთილმოწყობა, კერძოდ:",
        "დასასრული.",
    ]
    right = [
        "შესავალი ტექსტი.",
        "შემკვეთის პროექტ „ქარვის კოშკისთვის“ ოთახების რემონტი, ფასადის შეღებვა და ეზოს "
        "კეთილმოწყობა, კერძოდ:",
        "დასასრული.",
    ]
    assert [(f.rule_id, f.expected, f.actual) for f in substantive(run(left, right))] == [
        ("CMP.CADASTRAL", "01.02.03.456", None)
    ]


def test_a_table_cell_read_once_whole_and_once_per_paragraph_is_not_new_text() -> None:
    fee = "საფასური შეადგენს 1 200 (ათას ორასი) ლარს და გადაიხდება 5 (ხუთი) სამუშაო დღეში."
    cut = fee.index("ლარს")
    left = build([(t, "text", f"¶{i}", None, None) for i, t in enumerate(["სათაური", fee, "ბოლო"])])
    right = build(
        [
            ("სათაური", "text", "¶1", None, None),
            (f"{fee[:cut]}\n{fee[cut:]}", "table_cell", "T1/r1/c2", None, None),
            (fee[:cut], "text", "¶2", None, None),
            (fee[cut:], "text", "¶3", None, None),
            ("ბოლო", "text", "¶4", None, None),
        ]
    )
    report = compare(
        left,
        right,
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    assert substantive(report) == []


def test_a_renamed_project_is_reported_in_the_nominative() -> None:
    left = ["საავტორო უფლებები პროექტ „ქარვის კოშკისთვის“ შექმნილ მასალაზე ეკუთვნის შემკვეთს."]
    right = ["საავტორო უფლებები პროექტ „ქარის წისქვილისთვის“ შექმნილ მასალაზე ეკუთვნის შემკვეთს."]
    [f] = substantive(run(left, right))
    assert (f.rule_id, f.expected, f.actual) == ("CMP.PROJECT", "ქარვის კოშკი", "ქარის წისქვილი")


#: A contract and its act: the contract's term runs from signing with no end, its performance
#: window is six months, and it sets a penalty per day; the act states one month of service.
CONTRACT = [
    "ხელშეკრულება",
    "ქ. ბათუმი, 2026 წლის 2 მარტი",
    PARTIES[0],
    PARTIES[1],
    "2.1. შემსრულებელი ვალდებულია შეასრულოს მომსახურება 2026 წლის 2 მარტიდან "
    "2026 წლის 2 სექტემბრის ჩათვლით.",
    "2.2. შემსრულებელს დაეკისრება ჯარიმა ღირებულების 20% ოდენობით, ამასთან პირგასამტეხლო "
    "ღირებულების 0.1% ოდენობით, ყოველ ვადაგადაცილებულ დღეზე.",
    "3.1. ხელშეკრულება ძალაში შედის მისი ხელმოწერის მომენტიდან და მოქმედებს ვალდებულებების "
    "სრულად შესრულებამდე.",
    "4.1. ღირებულება შეადგენს 900 (ცხრაასი) ლარს.",
]
ITS_ACT = [
    "მიღება-ჩაბარების აქტი",
    PARTIES[0],
    PARTIES[1],
    "„მხარეებმა“ 2026 წლის 2 მარტს გააფორმეს ხელშეკრულება.",
    "მომსახურება შესრულდა 2026 წლის 2 მარტიდან 2026 წლის 31 მარტის ჩათვლით.",
    "საფასური შეადგენს 900 (ცხრაასი) ლარს.",
    "„აქტი“ გაფორმდა 2026 წლის 31 მარტი",
]


def test_a_contract_against_its_act_compares_what_each_states_as_a_whole() -> None:
    report = run(CONTRACT, ITS_ACT)
    fields = {(d.rule_id, d.label_en, d.left, d.right) for d in report.field_deltas}
    assert {
        ("CMP.DURATION", "Execution period", "2026-03-02/2026-09-02", "2026-03-02/2026-03-31"),
        ("CMP.DURATION", "Validity period", "2026-03-02/..", None),
        ("CMP.PERCENT", "Penalty", "0.1% per day", None),
        ("CMP.PERCENT", "Penalty", "20%", None),
    } <= fields
    # each change is reported once: the period not also as a range of dates removed from its
    # line, the penalty once whether its line says "0.1%" or its clause "0.1% per day"
    reported = [(f.rule_id, f.expected, f.actual) for f in report.findings]
    for change in [
        ("CMP.DURATION", "2026-03-02/2026-09-02", "2026-03-02/2026-03-31"),
        ("CMP.DURATION", "2026-03-02/..", None),
    ]:
        assert reported.count(change) == 1, change
    gone = [f for f in report.findings if f.expected == "2026-03-02/2026-09-02" and not f.actual]
    assert gone == []
    rates = [
        f for f in report.findings if f.rule_id == "CMP.PERCENT" and "0.1" in (f.expected or "")
    ]
    assert len(rates) == 1
    # a value only one of two different documents states is what each is for: a warning; the
    # period both state differently is the error
    severity = {(f.rule_id, f.expected, f.actual): f.severity for f in report.findings}
    assert severity[("CMP.DURATION", "2026-03-02/..", None)] == "warning"
    assert rates[0].severity == "warning"
    assert severity[("CMP.DURATION", "2026-03-02/2026-09-02", "2026-03-02/2026-03-31")] == "error"


def test_a_whole_document_reader_that_fails_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from procurement_agent.checks import document_facts

    def broken(text: str) -> list[object]:
        raise RuntimeError("reader failed")

    monkeypatch.setattr(document_facts, "document_facts", broken)
    report = run(CONTRACT, ITS_ACT)
    assert any(s.rule_id == "CMP.FIELDS" for s in report.skipped)
    # the values are still compared, line by line
    assert any(d.rule_id == "CMP.PERCENT" and d.kind == "removed" for d in report.field_deltas)


def test_a_name_written_in_two_alphabets_is_compared_as_written() -> None:
    # three Latin letters are part of the name, not a slip of the keyboard
    left = [
        "შპს „ABCჯგუფი“ საიდენტიფიკაციო კოდი: 401000111 (შემდგომში „შემკვეთი“)",
        "მომსახურება შესრულდა სრულად და დროულად.",
    ]
    right = [left[0].replace("ABC", "XYZ"), left[1]]
    assert ("CMP.PARTY", "შპს ABCჯგუფი", "შპს XYZჯგუფი") in found(left, right)


def test_an_amount_restated_with_its_tetri_is_no_change() -> None:
    """ "100 (ასი) ლარს" and "100.00 (ასი ლარი და 00 თეთრი)" state one amount."""
    from procurement_agent.checks.compare import word_amount_deltas

    assert (
        word_amount_deltas(
            "საფასური შეადგენს 100 (ასი) ლარს.",
            "საფასური შეადგენს 100.00 (ასი ლარი და 00 თეთრი).",
        )
        == []
    )
    [delta] = word_amount_deltas(
        "საფასური შეადგენს 100 (ასი) ლარს.",
        "საფასური შეადგენს 100.50 (ასი ლარი და ორმოცდაათი თეთრი).",
    )
    assert delta.right == "ასი ლარი და ორმოცდაათი თეთრი (100.50)"


def test_a_range_of_plain_dates_named_by_no_keyword_is_the_period() -> None:
    """ "გაწეულია 01.04.2026 - 30.04.2026 პერიოდში": a period in figures that no "შესრულება"
    word names is still the execution period, and a year typed wrong in it is one change."""
    text = "„მომსახურება“ გაწეულია 01.04.{0} - 30.04.{0} პერიოდში."
    report = run([text.format("2026")], [text.format("2025")])
    changed = [f for f in report.findings if f.rule_id in ("CMP.DATE", "CMP.DURATION")]
    assert [(f.rule_id, f.expected, f.actual) for f in changed] == [
        ("CMP.DURATION", "2026-04-01/2026-04-30", "2025-04-01/2025-04-30")
    ]
