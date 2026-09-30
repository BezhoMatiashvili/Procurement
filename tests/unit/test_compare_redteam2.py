"""Comparison defects a red team found and a verifier reproduced (round 2), one test each, with
a near miss beside every widened reading.

Every document here is invented: names, codes, accounts and addresses are made up.
"""

from __future__ import annotations

from procurement_agent.checks.compare import compare
from procurement_agent.ingestion.segmenter import Segment, build

CONTRACT = [
    "მომსახურების ხელშეკრულება № TR-2026/081",
    "ქ. ბათუმი, 2026 წლის 14 მაისი",
    "შპს „ლურჯი ტბა“, საიდენტიფიკაციო კოდი: 406777888, იურიდიული მისამართი: ქ. ბათუმი, "
    "ზღვის ქ. N7 (შემდგომში „შემსყიდველი“), წარმოდგენილი დირექტორის თამარ ქობალიას სახით, და",
    "შპს „წითელი კლდე“, საიდენტიფიკაციო კოდი: 407999000, იურიდიული მისამართი: ქ. ფოთი, "
    "ნავსადგურის ქ. N3 (შემდგომში „მიმწოდებელი“), წარმოდგენილი დირექტორის ლაშა გოგიძის სახით.",
    "1. ხელშეკრულების საგანი",
    "1.1. მიმწოდებელი ვალდებულია მიაწოდოს შემსყიდველს სასკოლო მერხები.",
    "2. ღირებულება და ანგარიშსწორება",
    "2.1. ხელშეკრულების ჯამური ღირებულება შეადგენს 8 400 (რვა ათას ოთხასი) ლარს დღგ-ს ჩათვლით.",
    "2.2. ანგარიშსწორება მოხდება მიღება-ჩაბარების აქტის გაფორმებიდან 10 (ათი) სამუშაო დღის "
    "ვადაში.",
    "2.3. შემსყიდველი უხდის მიმწოდებელს ავანსს ღირებულების 15%-ის ოდენობით.",
    "2.4. მიმწოდებლის საბანკო რეკვიზიტები: ბანკის კოდი: ZXCVGE33.",
    "3. საგარანტიო ვადა",
    "3.1. საქონელზე ვრცელდება 36 (ოცდათექვსმეტი) თვის საგარანტიო ვადა.",
    "4. პასუხისმგებლობა",
    "4.1. ვადის გადაცილებისთვის მიმწოდებელს ეკისრება პირგასამტეხლო ღირებულების 0.3% ოდენობით "
    "ყოველ ვადაგადაცილებულ დღეზე.",
    "5. მოქმედების ვადა",
    "5.1. ხელშეკრულება ძალაში შედის ხელმოწერის მომენტიდან და მოქმედებს 2026 წლის 30 ნოემბრამდე.",
    "6. დასკვნითი დებულებები",
    "6.1. ხელშეკრულება შედგენილია 2 ეგზემპლარად, თანაბარი იურიდიული ძალით.",
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


def found(report) -> list[tuple[str, str | None, str | None, str]]:  # type: ignore[no-untyped-def]
    return [
        (f.rule_id, f.expected, f.actual, f.severity)
        for f in report.findings
        if f.severity != "info" and f.rule_id != "CMP.FORMATTING"
    ]


# --- COMPARE-R2-10: a BIC and its "XXX" branch form are one code ---------------------------------


def test_a_bic_restated_with_its_xxx_branch_is_no_change() -> None:
    right = edit(CONTRACT, "ZXCVGE33.", "ZXCVGE33XXX.")
    assert found(run(CONTRACT, right)) == []


def test_a_bic_with_another_branch_or_bank_is_still_a_change() -> None:
    branch = found(run(CONTRACT, edit(CONTRACT, "ZXCVGE33.", "ZXCVGE33A01.")))
    assert [(rule, sev) for rule, _b, _a, sev in branch] == [("CMP.BANK", "error")]
    other = found(run(CONTRACT, edit(CONTRACT, "ZXCVGE33.", "ZXCVGE44XXX.")))
    assert [(r, b, a) for r, b, a, _s in other] == [("CMP.BANK", "ZXCVGE33", "ZXCVGE44XXX")]


# --- COMPARE-R2-14: an advance is reported in the canonical payment-terms form ------------------


def test_an_advance_change_is_reported_as_advance_percent() -> None:
    right = edit(CONTRACT, "15%-ის", "25%-ის")
    assert found(run(CONTRACT, right)) == [
        ("CMP.PAYMENT", "advance 15%", "advance 25%", "error")
    ]


def test_an_english_advance_change_is_reported_as_advance_percent() -> None:
    left = ["An advance payment of 10% of the price is paid within 5 days of signing."]
    right = [left[0].replace("10%", "12.5%")]
    assert found(run(left, right)) == [("CMP.PAYMENT", "advance 10%", "advance 12.5%", "error")]


def test_a_payment_percentage_without_advance_words_stays_as_written() -> None:
    left = ["The remaining 70% of the price is paid within 5 days of acceptance."]
    right = [left[0].replace("70%", "60%")]
    assert [(b, a) for _r, b, a, _s in found(run(left, right))] == [("70%", "60%")]


# --- COMPARE-R2-03 / 04: two dates written as a range are a period, never a point in time --------


def test_a_window_near_the_word_contract_is_an_execution_period() -> None:
    clause = (
        "1.2. ხელშეკრულებით გათვალისწინებული მომსახურება გაიწევა 2026 წლის 1 ივნისიდან "
        "2026 წლის 31 აგვისტოს ჩათვლით."
    )
    at = CONTRACT.index("2. ღირებულება და ანგარიშსწორება")
    left = [*CONTRACT[:at], clause, *CONTRACT[at:]]
    right = edit(left, "2026 წლის 31 აგვისტოს", "2026 წლის 30 სექტემბრის")
    report = run(left, right)
    labels = {d.label_en for h in report.hunks for d in h.deltas}
    assert labels == {"Execution period"}
    assert [(r, b, a) for r, b, a, _s in found(report)] == [
        ("CMP.DURATION", "2026-06-01/2026-08-31", "2026-06-01/2026-09-30")
    ]


def test_an_english_window_near_the_word_contract_is_an_execution_period() -> None:
    left = [
        "Contract No QA-7",
        "1.1. Under this contract the Contractor shall provide guarding services "
        "from 1 June 2026 to 31 August 2026.",
    ]
    right = [left[0], left[1].replace("31 August 2026", "30 September 2026")]
    labels = {d.label_en for h in run(left, right).hunks for d in h.deltas}
    assert labels == {"Execution period"}


WINDOW = "2.5. მიწოდება განხორციელდება 2026 წლის 1 ივნისიდან 2026 წლის 20 ივნისის ჩათვლით."
EXTRA = "2.6. მიწოდება ხორციელდება შეკვეთიდან 20 კალენდარული დღის ვადაში."


def with_delivery_window() -> list[str]:
    at = CONTRACT.index("3. საგარანტიო ვადა")
    return [*CONTRACT[:at], WINDOW, EXTRA, *CONTRACT[at:]]


def test_a_delivery_window_moved_is_one_execution_period_change() -> None:
    left = with_delivery_window()
    moved = [t.replace("20 ივნისის", "25 ივნისის") for t in left]
    report = run(left, moved)
    assert [(r, b, a) for r, b, a, _s in found(report)] == [
        ("CMP.DURATION", "2026-06-01/2026-06-20", "2026-06-01/2026-06-25")
    ]
    assert {d.label_en for h in report.hunks for d in h.deltas} == {"Execution period"}


def test_a_delivery_window_deleted_is_one_finding() -> None:
    left = with_delivery_window()
    report = run(left, [t for t in left if t != WINDOW])
    periods = [f for f in report.findings if f.rule_id == "CMP.DURATION"]
    assert [(f.expected, f.actual) for f in periods] == [("2026-06-01/2026-06-20", None)]


def test_a_single_delivery_date_is_still_a_delivery_deadline() -> None:
    left = ["მიწოდება განხორციელდება არაუგვიანეს 2026 წლის 20 ივნისისა."]
    right = [left[0].replace("20 ივნისისა", "25 ივნისისა")]
    labels = {d.label_en for h in run(left, right).hunks for d in h.deltas}
    assert labels == {"Delivery deadline"}


# --- COMPARE-R2-15: an English heading's date restyled to ISO is the same signing date ----------

ENGLISH = [
    "SUPPLY AGREEMENT No. QB-2026/19",
    "Poti, 8 April 2026",
    "1.1. The Supplier shall deliver office chairs to the Buyer.",
    "4.1. This Agreement shall enter into force upon signature and remain valid until "
    "30 November 2026.",
]


def test_an_english_heading_date_restyled_to_iso_is_no_change() -> None:
    right = [ENGLISH[0], "Poti, 2026-04-08", *ENGLISH[2:]]
    assert found(run(ENGLISH, right)) == []


def test_an_english_heading_date_moved_in_iso_is_still_a_change() -> None:
    right = [ENGLISH[0], "Poti, 2026-04-09", *ENGLISH[2:]]
    rules = {r for r, _b, _a, _s in found(run(ENGLISH, right))}
    assert "CMP.DATE" in rules or "CMP.DURATION" in rules


def test_a_preamble_ends_after_an_iso_heading_date_in_flattened_text() -> None:
    from procurement_agent.extraction.anchors import preamble_end

    text = "SUPPLY AGREEMENT No. QB-2026/19 Poti, 2026-04-08 1.1. The Supplier shall deliver."
    assert preamble_end(text) == text.index("1.1. The")
    # a number that is no date before "1.1." does not open the clauses
    assert preamble_end("Room 12 1.1. The Supplier shall deliver.") is None


# --- COMPARE-R2-05: a period stated as a month is that month's first to last day ----------------

ACT = [
    "მიღება-ჩაბარების აქტი",
    "შპს „ლურჯი ტბა“ (საიდენტიფიკაციო კოდი: 406777888) (შემდგომში „შემსყიდველი“) და",
    "შპს „წითელი კლდე“ (საიდენტიფიკაციო კოდი: 407999000) (შემდგომში „მიმწოდებელი“)",
    "„მომსახურება“ შესრულდა 2026 წლის 1 ივლისიდან 2026 წლის 31 ივლისის ჩათვლით.",
    "„მომსახურების“ საფასური შეადგენს 700 (შვიდასი) ლარს გადასახადების ჩათვლით.",
    "„აქტი“ გაფორმდა 2026 წლის 31 ივლისს",
]
PERIOD = "2026 წლის 1 ივლისიდან 2026 წლის 31 ივლისის ჩათვლით"


def test_an_acts_period_restated_as_its_month_is_no_change() -> None:
    assert found(run(ACT, edit(ACT, PERIOD, "2026 წლის ივლისის თვეში"))) == []


def test_an_acts_period_moved_to_another_month_is_one_period_change() -> None:
    assert found(run(ACT, edit(ACT, PERIOD, "2026 წლის აგვისტოს თვეში"))) == [
        ("CMP.DURATION", "2026-07-01/2026-07-31", "2026-08-01/2026-08-31", "error")
    ]


def test_a_month_named_in_a_sentence_about_an_advance_is_no_period() -> None:
    from procurement_agent.checks.compare import hits_of

    hits = hits_of("ავანსი გადაიხდება 2026 წლის ივლისის თვეში.")
    assert not [h for h in hits if h.type.value == "execution_period"]


# --- COMPARE-R2-16 / 17: the same values written another way are formatting --------------------


def test_a_validity_end_restyled_with_its_postposition_is_formatting() -> None:
    right = edit(CONTRACT, "2026 წლის 30 ნოემბრამდე", "30.11.2026-მდე")
    report = run(CONTRACT, right)
    assert found(report) == []
    assert report.counts.cosmetic == 1


def test_an_acts_period_restyled_in_figures_is_formatting() -> None:
    right = edit(ACT, PERIOD, "01.07.2026-დან 31.07.2026-ის ჩათვლით")
    assert found(run(ACT, right)) == []


def test_a_date_restyled_from_until_into_from_is_still_a_change() -> None:
    right = edit(CONTRACT, "2026 წლის 30 ნოემბრამდე", "30.11.2026-დან")
    assert "CMP.TEXT_CHANGED" in [r for r, _b, _a, _s in found(run(CONTRACT, right))]


def test_a_rate_written_with_the_word_percent_is_formatting() -> None:
    right = edit(CONTRACT, "0.3% ოდენობით", "0.3 პროცენტის ოდენობით")
    assert found(run(CONTRACT, right)) == []


def test_the_lari_abbreviated_is_formatting() -> None:
    right = edit(CONTRACT, "ოთხასი) ლარს დღგ-ს", "ოთხასი) ლ. დღგ-ს")
    assert found(run(CONTRACT, right)) == []


def test_tetri_written_as_a_fraction_is_formatting() -> None:
    left = edit(CONTRACT, "8 400 (რვა ათას ოთხასი) ლარს", "8 400 ლარი და 50 თეთრი")
    right = edit(CONTRACT, "8 400 (რვა ათას ოთხასი) ლარს", "8 400.50 ლარი")
    assert found(run(left, right)) == []


def test_another_currency_for_the_same_figure_is_still_a_change() -> None:
    right = edit(CONTRACT, "ოთხასი) ლარს დღგ-ს", "ოთხასი) აშშ დოლარს დღგ-ს")
    assert found(run(CONTRACT, right)) != []


def test_a_rate_changed_with_the_word_percent_is_still_a_change() -> None:
    right = edit(CONTRACT, "0.3% ოდენობით", "0.4 პროცენტის ოდენობით")
    assert [r for r, _b, _a, _s in found(run(CONTRACT, right))] == ["CMP.PERCENT"]


# --- COMPARE-R2-01: words a value change does not account for are a change of their own --------


def test_a_penalty_moved_onto_the_other_party_with_its_rate_is_two_findings() -> None:
    right = edit(CONTRACT, "მიმწოდებელს ეკისრება პირგასამტეხლო ღირებულების 0.3%",
                 "შემსყიდველს ეკისრება პირგასამტეხლო ღირებულების 0.4%")
    report = run(CONTRACT, right)
    assert [(r, s) for r, _b, _a, s in found(report)] == [
        ("CMP.PERCENT", "error"),
        ("CMP.TEXT_CHANGED", "warning"),
    ]
    rate = report.findings[0]
    assert (rate.expected, rate.actual) == ("0.3% per day", "0.4% per day")
    # the canonical rate is written nowhere: the finding is anchored on the line it was read on
    assert rate.span is not None and "შემსყიდველს" in (rate.quote or "")


def test_a_price_raised_and_newly_excluding_a_cost_is_two_findings() -> None:
    right = edit(CONTRACT, "8 400 (რვა ათას ოთხასი) ლარს დღგ-ს ჩათვლით.",
                 "9 400 (ცხრა ათას ოთხასი) ლარს დღგ-ს ჩათვლით, რომელიც არ მოიცავს მონტაჟის ხარჯებს.")
    rules = [(r, s) for r, _b, _a, s in found(run(CONTRACT, right))]
    assert rules == [("CMP.AMOUNT", "error"), ("CMP.TEXT_CHANGED", "warning")]


def test_a_price_clause_removed_while_its_amount_is_restated_is_a_paragraph_removed() -> None:
    left = [*CONTRACT, "7.1. ხელშეკრულების ჯამური ღირებულებაა 8 400 ლარი."]
    right = [t for t in left if not t.startswith("2.1.")]
    rules = [(r, b, s) for r, b, _a, s in found(run(left, right))]
    assert ("CMP.TEXT_REMOVED", "warning") in [(r, s) for r, _b, s in rules]
    assert ("CMP.VAT", "incl", "error") in rules


def test_a_figure_and_its_words_changed_together_are_one_finding() -> None:
    right = edit(CONTRACT, "36 (ოცდათექვსმეტი) თვის", "24 (ოცდაოთხი) თვის")
    assert found(run(CONTRACT, right)) == [("CMP.DURATION", "P36M", "P24M", "error")]


def test_a_value_clause_deleted_whole_is_one_finding() -> None:
    right = [t for t in CONTRACT if not t.startswith("4.1.")]
    assert [(r, b, a) for r, b, a, _s in found(run(CONTRACT, right))] == [
        ("CMP.PERCENT", "0.3% per day", None)
    ]


def test_a_rate_changed_on_a_line_whose_other_words_stay_is_one_finding() -> None:
    right = edit(CONTRACT, "0.3% ოდენობით", "0.5% ოდენობით")
    assert [r for r, _b, _a, _s in found(run(CONTRACT, right))] == ["CMP.PERCENT"]


# --- COMPARE-R2-02: a contract against its act loses no value to a change it never reports ------

#: The act closing CONTRACT: the same two parties, what was delivered and for how much.
ITS_ACT = [
    "მიღება-ჩაბარების აქტი",
    CONTRACT[2],
    CONTRACT[3],
    "„მხარეებმა“ 2026 წლის 14 მაისს გააფორმეს ხელშეკრულება № TR-2026/081.",
    "მიმწოდებელმა მიაწოდა, ხოლო შემსყიდველმა ჩაიბარა სასკოლო მერხები.",
    "მიწოდებული საქონელი დათვალიერდა ადგილზე, მისი ოდენობა და ხარისხი შეესაბამება "
    "ხელშეკრულებით გათვალისწინებულ მოთხოვნებს, ხილული დაზიანება არ აღმოჩენილა.",
    "შემსყიდველს მიმწოდებლის მიმართ პრეტენზია არ გააჩნია, საქონელი მიღებულია სრულად და "
    "დროულად, თანდართული სასაქონლო ზედნადების შესაბამისად.",
    "წინამდებარე დოკუმენტი წარმოადგენს ანგარიშსწორების საფუძველს და ინახება ორივე მხარესთან.",
    "საქონლის ღირებულება შეადგენს 8 400 (რვა ათას ოთხასი) ლარს დღგ-ს ჩათვლით.",
    "აქტი შედგენილია 2026 წლის 20 ივნისს.",
]


def _parties(report) -> list[tuple[str, str | None, str | None]]:  # type: ignore[no-untyped-def]
    return sorted(
        (
            (f.rule_id, f.expected, f.actual)
            for f in report.findings
            if f.rule_id in ("CMP.PARTY", "CMP.ID_CODE") and "address" not in f.message_en
        ),
        key=str,
    )


def test_an_act_naming_other_directors_for_both_parties_lists_them() -> None:
    act = edit(edit(ITS_ACT, "თამარ ქობალიას", "ნინო ბერაძის"), "ლაშა გოგიძის", "დავით ხუციშვილის")
    report = run(CONTRACT, act)
    assert report.alignment == "unrelated"
    assert _parties(report) == sorted(
        [
            ("CMP.PARTY", "თამარ ქობალია", None),
            ("CMP.PARTY", "ლაშა გოგიძე", None),
            ("CMP.PARTY", None, "ნინო ბერაძე"),
            ("CMP.PARTY", None, "დავით ხუციშვილი"),
        ],
        key=str,
    )


def test_an_act_giving_other_id_codes_for_both_parties_lists_them() -> None:
    act = edit(edit(ITS_ACT, "406777888", "401122334"), "407999000", "402233445")
    report = run(CONTRACT, act)
    assert report.alignment == "unrelated"
    ids = [(e, a) for r, e, a in _parties(report) if r == "CMP.ID_CODE"]
    assert sorted(ids, key=str) == sorted(
        [("406777888", None), ("407999000", None), (None, "401122334"), (None, "402233445")],
        key=str,
    )


def test_an_act_naming_the_contract_s_parties_reports_none_of_them() -> None:
    assert _parties(run(CONTRACT, ITS_ACT)) == []


def test_an_act_naming_one_other_director_is_that_director_s_change() -> None:
    act = edit(ITS_ACT, "ლაშა გოგიძის", "დავით ხუციშვილის")
    # a single leftover on each side is still one director's change, the error it is
    assert [(r, e, a, s) for r, e, a, s in found(run(CONTRACT, act)) if r == "CMP.PARTY"] == [
        ("CMP.PARTY", "ლაშა გოგიძე", "დავით ხუციშვილი", "error")
    ]
