"""Comparison accuracy on the mistakes a reviewer actually plants: typos, spelled-out numbers,
reworded labels, renamed parties. Every document here is invented."""

from __future__ import annotations

from procurement_agent.checks.compare import compare
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
    # "(ასოი)" used to be paired with the next bracket and read as "ასი (100) → ხუთი (5)"
    assert found(ACT, edit(2, "(ასი)", "(ასოი)")) == [("CMP.SPELLING", "ასი", "ასოი")]


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
    assert {f.rule_id for f in report.findings} >= {"CMP.AMOUNT_WORDS"}


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
