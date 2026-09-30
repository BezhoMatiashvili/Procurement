"""Comparing two documents on the values the rules now read as written: a penalty rate in words
and its period, the lari abbreviated, tetri in figures, and a project named by a noun in "-ა".
Every document here is invented."""

from __future__ import annotations

from procurement_agent.checks.compare import compare
from procurement_agent.ingestion.segmenter import Segment, build

CONTRACT = [
    "მომსახურების ხელშეკრულება № KL-2026/041",
    "ქ. თბილისი, 2026 წლის 9 მარტი",
    "1.1. შემსრულებელი ვალდებულია გაუწიოს შემკვეთს დასუფთავების მომსახურება "
    "პროექტ „მთის ვერანდისთვის“.",
    "2.1. შემსრულებელი ვალდებულია შეასრულოს მომსახურება 2026 წლის 1 აპრილიდან 2026 წლის "
    "30 ივნისის ჩათვლით.",
    "3.1. მომსახურების ღირებულება შეადგენს 2 700 (ორი ათას შვიდასი) ლარს დღგ-ს ჩათვლით.",
    "4.1. ვადის გადაცილებისთვის შემსრულებელს ეკისრება პირგასამტეხლო ღირებულების 0.2% "
    "ოდენობით ყოველ ვადაგადაცილებულ დღეზე.",
    "5.1. ხელშეკრულება შედგენილია 2 (ორი) ეგზემპლარად.",
]


def _segs(texts: list[str]) -> list[Segment]:
    return build([(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(texts)])


def _findings(left: list[str], right: list[str]) -> list[tuple[str, str | None, str | None]]:
    report = compare(
        _segs(left),
        _segs(right),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    return [
        (f.rule_id, f.expected, f.actual)
        for f in report.findings
        if f.severity == "error" and f.rule_id != "CMP.FORMATTING"
    ]


def _edit(old: str, new: str) -> list[str]:
    out = list(CONTRACT)
    where = [i for i, t in enumerate(out) if old in t]
    assert len(where) == 1, old
    out[where[0]] = out[where[0]].replace(old, new)
    return out


def test_a_rate_restated_in_words_is_no_change() -> None:
    assert _findings(CONTRACT, _edit("0.2% ", "0.2 პროცენტის ")) == []


def test_a_rate_changed_in_words_is_a_penalty_change_with_its_period() -> None:
    left = _edit("0.2% ", "0.2 პროცენტის ")
    right = _edit("0.2% ", "0.4 პროცენტის ")
    assert _findings(left, right) == [("CMP.PERCENT", "0.2% per day", "0.4% per day")]


def test_a_penalty_period_changed_alone_is_one_penalty_change() -> None:
    right = _edit("ვადაგადაცილებულ დღეზე", "ვადაგადაცილებულ კვირაზე")
    assert _findings(CONTRACT, right) == [("CMP.PERCENT", "0.2% per day", "0.2% per week")]


def test_a_penalty_rate_changed_between_versions_is_reported_canonically() -> None:
    right = _edit("0.2% ", "0.3% ")
    assert _findings(CONTRACT, right) == [("CMP.PERCENT", "0.2% per day", "0.3% per day")]


def test_the_lari_abbreviated_is_the_same_amount() -> None:
    assert _findings(CONTRACT, _edit("ლარს დღგ-ს", "ლ. დღგ-ს")) == []


def test_an_amount_changed_and_written_with_the_abbreviation_keeps_its_new_value() -> None:
    right = _edit("2 700 (ორი ათას შვიდასი) ლარს", "2 900 (ორი ათას ცხრაასი) ლ.")
    rules = [(rule, before, after) for rule, before, after in _findings(CONTRACT, right)]
    assert ("CMP.AMOUNT", "2,700.00 GEL", "2,900.00 GEL") in rules
    assert not any(after is None for rule, _, after in rules if rule == "CMP.AMOUNT")


def test_tetri_in_figures_are_part_of_the_amount() -> None:
    left = _edit("2 700 (ორი ათას შვიდასი) ლარს", "2 700 ლარი და 40 თეთრი")
    assert _findings(left, _edit("2 700 (ორი ათას შვიდასი) ლარს", "2 700.40 ლარი")) == []
    changed = _findings(left, _edit("2 700 (ორი ათას შვიდასი) ლარს", "2 700 ლარი და 80 თეთრი"))
    assert changed == [("CMP.AMOUNT", "2,700.40 GEL", "2,700.80 GEL")]


def test_one_project_in_two_case_forms_is_no_change() -> None:
    act = [
        "მიღება-ჩაბარების აქტი",
        "1. დასუფთავების მომსახურება გაწეულია პროექტი „მთის ვერანდა“ ფარგლებში.",
    ]
    report = compare(
        _segs(CONTRACT),
        _segs(act),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    assert not [f for f in report.findings if f.rule_id == "CMP.PROJECT"]
