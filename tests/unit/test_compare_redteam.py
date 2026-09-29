"""Comparison defects a red team found and a verifier reproduced (round 1), one test each.

Every document here is invented: names, codes, accounts and addresses are made up.
"""

from __future__ import annotations

from procurement_agent.checks.compare import as_number, compare
from procurement_agent.ingestion.segmenter import Segment, build

CONTRACT = [
    "მომსახურების ხელშეკრულება № KL-2026/310",
    "ქ. ქუთაისი, 2026 წლის 9 აპრილი",
    "შპს „მთის ნიავი“, საიდენტიფიკაციო კოდი: 404111222, იურიდიული მისამართი: ქ. ქუთაისი, "
    "ვაზის ქ. N4 (შემდგომში „შემკვეთი“), წარმოდგენილი დირექტორის ეკა ბერულავას სახით, და",
    "შპს „ქვის ხიდი“, საიდენტიფიკაციო კოდი: 405333444, იურიდიული მისამართი: ქ. ზუგდიდი, "
    "ტყის ქ. N12 (შემდგომში „შემსრულებელი“), წარმოდგენილი დირექტორის გელა მამულაშვილის სახით.",
    "1. ხელშეკრულების საგანი",
    "1.1. შემსრულებელი ვალდებულია გაუწიოს შემკვეთს დასუფთავების მომსახურება.",
    "1.2. მომსახურების დეტალური აღწერა მოცემულია დანართ №4-ში.",
    "2. შესრულების ვადა",
    "2.1. შემსრულებელი ვალდებულია შეასრულოს მომსახურება 2026 წლის 1 მაისიდან 2026 წლის "
    "30 სექტემბრის ჩათვლით.",
    "3. ღირებულება",
    "3.1. მომსახურების ღირებულება შეადგენს 3 600 (სამი ათას ექვსასი) ლარს დღგ-ს ჩათვლით.",
    "3.2. ანგარიშსწორება მოხდება აქტის გაფორმებიდან 5 (ხუთი) სამუშაო დღის ვადაში.",
    "4. პასუხისმგებლობა",
    "4.1. ვადის გადაცილებისთვის შემსრულებელს ეკისრება პირგასამტეხლო ღირებულების 0.2% "
    "ოდენობით ყოველ ვადაგადაცილებულ დღეზე.",
    "5. ხელშეკრულების მოქმედების ვადა",
    "5.1. ხელშეკრულება ძალაში შედის ხელმოწერის მომენტიდან და მოქმედებს 2026 წლის "
    "31 დეკემბრამდე.",
    "6. დასკვნითი დებულებები",
    "6.1. ხელშეკრულება შედგენილია 2 ეგზემპლარად, თანაბარი იურიდიული ძალით.",
    "6.2. დავები წყდება მოლაპარაკების გზით, შეუთანხმებლობის შემთხვევაში - ქუთაისის "
    "საქალაქო სასამართლოში.",
]

ACT = [
    "მიღება-ჩაბარების აქტი",
    "შპს „მთის ნიავი“ (საიდენტიფიკაციო კოდი: 404111222) (შემდგომში „შემკვეთი“) წარმოდგენილი "
    "დირექტორის ეკა ბერულავას სახით და",
    "შპს „ქვის ხიდი“ (საიდენტიფიკაციო კოდი: 405333444) (შემდგომში „შემსრულებელი“) "
    "წარმოდგენილი დირექტორის გელა მამულაშვილის სახით",
    "„მხარეებმა“ 2026 წლის 9 აპრილს გააფორმეს ხელშეკრულება № KL-2026/310.",
    "„მომსახურება“ შესრულდა 2026 წლის 1 ივნისიდან 2026 წლის 30 ივნისის ჩათვლით.",
    "„მომსახურების“ საფასური შეადგენს 720 (შვიდას ოცი) ლარს გადასახადების ჩათვლით. საფასური "
    "გადაიხდება აქტის გაფორმებიდან 5 (ხუთი) სამუშაო დღის ვადაში, ანგარიშზე "
    "GE11ZZ0000000123456789.",
    "„აქტი“ გაფორმდა 2026 წლის 30 ივნისი",
    "აქტი შედგენილია 2 (ორი) იდენტურ ეგზემპლარად.",
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


def edit(doc: list[str], old: str, new: str) -> list[str]:
    where = [i for i, t in enumerate(doc) if old in t]
    assert where, old
    out = list(doc)
    out[where[0]] = out[where[0]].replace(old, new)
    return out


def substantive(report) -> list:  # type: ignore[no-untyped-def,type-arg]
    return [f for f in report.findings if f.severity != "info" and f.rule_id != "CMP.FORMATTING"]


def rules(report) -> list[tuple[str, str | None, str | None, str]]:  # type: ignore[no-untyped-def]
    return [(f.rule_id, f.expected, f.actual, f.severity) for f in substantive(report)]


# --- COMPARE-01: a text change is not "explained" by a fragment of another change -------------


def test_a_text_change_whose_digit_occurs_in_a_changed_date_is_still_reported() -> None:
    right = edit(edit(CONTRACT, "დანართ №4-ში", "დანართ №5-ში"), "9 აპრილი", "9 მაისი")
    found = rules(run(CONTRACT, right))
    assert any(r[0] == "CMP.DATE" for r in found)
    texts = [r for r in found if r[0] == "CMP.TEXT_CHANGED"]
    assert len(texts) == 1 and "№5-ში" in (texts[0][2] or "")


def test_a_text_change_whose_town_occurs_in_a_changed_address_is_still_reported() -> None:
    right = edit(
        edit(CONTRACT, "ქუთაისის საქალაქო", "ზუგდიდის საქალაქო"),
        "ქ. ქუთაისი, ვაზის",
        "ქ. ზუგდიდი, ვაზის",
    )
    found = rules(run(CONTRACT, right))
    assert any(r[0] == "CMP.PARTY" for r in found)
    assert any(r[0] == "CMP.TEXT_CHANGED" and "ზუგდიდის" in (r[2] or "") for r in found)


def test_the_same_word_changed_on_two_unrelated_lines_is_two_findings() -> None:
    left = [
        "პირველი პუნქტი: სამუშაო სრულდება ეტაპობრივად და დროულად.",
        "მეორე პუნქტი, სხვა შინაარსით: სამუშაო სრულდება ეტაპობრივად.",
    ]
    right = [t.replace("ეტაპობრივად", "ერთიანად") for t in left]
    assert [r[0] for r in rules(run(left, right))] == ["CMP.TEXT_CHANGED", "CMP.TEXT_CHANGED"]


def test_a_director_renamed_in_the_preamble_and_the_signature_table_is_one_change() -> None:
    left = [*ACT, "„შემკვეთი“\nშპს „მთის ნიავი“\nდირექტორი ეკა ბერულავა"]
    right = [t.replace("ეკა ბერულავა", "ეკა ბერუაშვილი") for t in left]
    assert [r[0] for r in rules(run(left, right))] == ["CMP.PARTY"]
