"""Defects the fifth red-team round found in the readers the comparison and the act check share
with the extraction: a fee written words first, a dollar named with its adjective, a surname that
drops a vowel before its ending, a penalty's period in the inessive, an amount's words after its
currency, an advance clause's amount and share, and the price of one item.

Each test names its defect (``samples/redteam/<module>/CONFIRMED_R5.md``); every widened reading
has a near miss beside it. Every document here is invented.
"""

from __future__ import annotations

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.compare import compare, hits_of
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.eval.runner import compare_label
from procurement_agent.extraction.georgian import attested_nominatives, in_case_of, lemma
from procurement_agent.extraction.rules import find_amounts, find_percentages
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion.segmenter import Segment, build
from tests.unit.test_aoa_check import ACT, CONTRACT


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


def substantive(report) -> list[tuple[str, str | None, str | None, str | None, str]]:  # type: ignore[no-untyped-def]
    return [
        (f.rule_id, compare_label(f, report), f.expected, f.actual, f.severity)
        for f in report.findings
        if f.severity != "info" and f.rule_id != "CMP.FORMATTING"
    ]


def _check(act: str = ACT, contract: str | None = CONTRACT) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract)


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _substantive(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.severity in ("error", "warning")]


def _amounts(text: str) -> list[tuple[float, str | None, str]]:
    return [(float(h.normalized), h.currency, h.raw_text) for h in find_amounts(text)]  # type: ignore[arg-type]


# --- AOA-R5-03: a fee written words first, its figure in brackets --------------------------------


class TestFeeWordsFirst:
    def test_the_amount_is_read_whole(self) -> None:
        text = "საფასური შეადგენს სამი ათას ოთხას ორმოცდაათი (3 450) ლარს."
        assert _amounts(text) == [(3450.0, "GEL", "სამი ათას ოთხას ორმოცდაათი (3 450) ლარს")]

    def test_near_miss_a_bracketed_figure_after_other_words(self) -> None:
        assert _amounts("ღირებულება (3 450) ლარი") == []
        assert _amounts("იხილეთ დანართი (2) ლარის კურსით") == []

    def test_a_correct_act_written_words_first_is_clean(self) -> None:
        act = ACT.replace("100 (ასი) ლარს", "ასი (100) ლარს")
        assert _substantive(_check(act)) == []

    def test_a_figure_slip_is_a_fee_error_with_the_correction(self) -> None:
        act = ACT.replace("100 (ასი) ლარს", "ასი (120) ლარს")
        report = _check(act)
        [fee] = _of(report, "AOA.FEE")
        assert fee.severity == "error"
        assert fee.expected == "ასი (100) ლარს"
        [words] = _of(report, "AOA.AMOUNT_WORDS")
        assert (words.actual, words.expected) == ("120", "100")

    def test_both_halves_wrong_corrects_both(self) -> None:
        act = ACT.replace("100 (ასი) ლარს", "ორასი (200) ლარს")
        [fee] = _of(_check(act), "AOA.FEE")
        assert fee.expected == "ასი (100) ლარს"

    def test_a_contract_price_written_words_first_is_the_price(self) -> None:
        contract = CONTRACT.replace("100 (ასი) ლარს", "ასი (100) ლარს")
        act = ACT.replace("100 (ასი) ლარს", "120 (ასი) ლარს")
        report = _check(act, contract)
        assert [f.severity for f in _of(report, "AOA.FEE")] == ["error"]
        assert not [s for s in report.skipped if s.rule_id == "AOA.FEE"]


# --- AOA-R5-10: "ამერიკული დოლარი" is dollars -----------------------------------------------------


class TestAmericanDollar:
    def test_the_adjective_in_its_cases(self) -> None:
        for written in ("1 500 (ათას ხუთასი) ამერიკული დოლარი", "1 500 ამერიკულ დოლარს"):
            assert [(v, c) for v, c, _ in _amounts(f"საფასური შეადგენს {written}.")] == [
                (1500.0, "USD")
            ], written

    def test_near_miss_the_adjective_before_another_noun(self) -> None:
        assert _amounts("მიწოდებულია 1 500 ამერიკული ტონა.") == []

    def test_a_wrong_dollar_fee_is_compared(self) -> None:
        contract = CONTRACT.replace("100 (ასი) ლარს", "100 (ასი) აშშ დოლარს")
        right = ACT.replace("100 (ასი) ლარს", "100 (ასი) ამერიკულ დოლარს")
        assert _of(_check(right, contract), "AOA.FEE") == []
        wrong = ACT.replace("100 (ასი) ლარს", "110 (ასი) ამერიკულ დოლარს")
        [fee] = _of(_check(wrong, contract), "AOA.FEE")
        assert fee.severity == "error"
        assert fee.expected == "100 (ასი) ამერიკულ დოლარს"


# --- AOA-R5-05: a surname that drops a vowel before its ending ------------------------------------


class TestSyncopatedSurname:
    def test_the_nominative_another_form_attests(self) -> None:
        assert lemma("დავით გოგოთლის") == "დავით გოგოთლი"  # alone it cannot be told
        assert attested_nominatives(["დავით გოგოთლის", "დავით გოგოთელი"]) == [
            "დავით გოგოთელი",
            "დავით გოგოთელი",
        ]
        assert attested_nominatives(["ეკა სანაბრის", "ეკა სანაბარი"])[0] == "ეკა სანაბარი"

    def test_near_miss_unrelated_and_unattested_forms_keep_their_lemma(self) -> None:
        assert attested_nominatives(["დავით გოგოთლის"]) == ["დავით გოგოთლი"]
        assert attested_nominatives(["დავით მელაძის", "ნინო კაპანაძე"]) == [
            "დავით მელაძე",
            "ნინო კაპანაძე",
        ]
        # another first name is another person
        assert attested_nominatives(["გელა გოგოთლის", "დავით გოგოთელი"])[0] == "გელა გოგოთლი"

    def test_a_correction_in_a_case_the_contract_writes_is_its_form(self) -> None:
        written = ["დავით გოგოთლის", "დავით გოგოთელი"]
        assert in_case_of("ეკა ხარაბაძის", "დავით გოგოთელი", written=written) == "დავით გოგოთლის"
        assert in_case_of("ეკა ხარაბაძე", "დავით გოგოთლის", written=written) == "დავით გოგოთელი"

    #: the contractor's director written in the genitive in the preamble and in the nominative
    #: at the contract's signature
    CONTRACT_S = (
        CONTRACT.replace("დავით მელაძის", "დავით გოგოთლის")
        + "„შემსრულებელი“\nშპს „ბეტა სერვისი“\nდირექტორი დავით გოგოთელი\n"
    )

    def test_another_signatory_is_corrected_to_the_written_nominative(self) -> None:
        act = ACT.replace("დირექტორი: დავით მელაძე", "დირექტორი: გელა ხარაბაძე")
        found = [(f.actual, f.expected) for f in _of(_check(act, self.CONTRACT_S), "AOA.DIRECTOR")]
        assert found == [("გელა ხარაბაძე", "დავით გოგოთელი")]

    def test_a_slip_is_corrected_to_the_written_nominative(self) -> None:
        act = ACT.replace("დირექტორი: დავით მელაძე", "დირექტორი: დავით გოგოთალი")
        found = [(f.actual, f.expected) for f in _of(_check(act, self.CONTRACT_S), "AOA.DIRECTOR")]
        assert found == [("დავით გოგოთალი", "დავით გოგოთელი")]

    def test_a_genitive_place_keeps_the_contract_s_genitive(self) -> None:
        act = ACT.replace(
            "ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“)",
            "ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის ნინო კაპანაძის სახით",
        ).replace("დირექტორი: დავით მელაძე", "დირექტორი: დავით გოგოთელი")
        found = [(f.actual, f.expected) for f in _of(_check(act, self.CONTRACT_S), "AOA.DIRECTOR")]
        assert found == [("ნინო კაპანაძის", "დავით გოგოთლის")]

    def test_the_extraction_states_the_written_nominative(self) -> None:
        from procurement_agent.config import Settings
        from procurement_agent.extraction.pipeline import run_extraction
        from procurement_agent.extraction.schemas import Provenance
        from procurement_agent.ingestion.chunker import Chunk

        text = self.CONTRACT_S
        chunk = Chunk(id="d:0", text=text, body=text, provenance=Provenance(chunk_id="d:0"))
        result = run_extraction(
            document_id="d",
            chunks=[chunk],
            settings=Settings(ollama_base_url="http://localhost:11434"),
            llm=None,
        )
        directors = [f.value for f in result.facts if f.type is FactType.party_director]
        assert "დავით გოგოთელი" in directors
        assert "დავით გოგოთლი" not in directors


# --- COMPARE-R5-05: a penalty's period in the inessive or as one word -----------------------------


def _penalties(text: str) -> list[str]:
    return [h.value for h in find_percentages(text) if h.type is FactType.penalty]


class TestPenaltyPeriodInessive:
    def test_each_period(self) -> None:
        for said, period in (
            ("0.2% დღეში", "day"),
            ("0.2% კვირაში", "week"),
            ("0.2% თვეში", "month"),
            ("0.2% წელიწადში", "year"),
            ("0.2%-ის ოდენობით დღეში", "day"),
            ("0.2% ყოველდღე", "day"),
            ("0.2% ყოველკვირა", "week"),
        ):
            assert _penalties(f"ეკისრება პირგასამტეხლო ღირებულების {said}.") == [
                f"0.2% per {period}"
            ], said

    def test_near_miss_a_count_between_is_a_length(self) -> None:
        assert _penalties("ეკისრება პირგასამტეხლო ღირებულების 0.2% 10 დღეში.") == ["0.2%"]
        # "ყოველთვის" is "always", no month
        assert _penalties("პირგასამტეხლო ყოველთვის შეადგენს 0.2%-ს.") == ["0.2%"]

    P = "1. ღირებულება შეადგენს 1 000 ლარს."

    def test_a_restyle_is_no_penalty_change(self) -> None:
        report = run(
            [self.P, "2. პირგასამტეხლო 0.2% ყოველ დღეზე."],
            [self.P, "2. პირგასამტეხლო 0.2% დღეში."],
        )
        assert [f for f in substantive(report) if f[0] == "CMP.PERCENT"] == []

    def test_a_period_changed_is_a_penalty_change(self) -> None:
        report = run(
            [self.P, "2. პირგასამტეხლო 0.2% დღეში."], [self.P, "2. პირგასამტეხლო 0.2% თვეში."]
        )
        assert substantive(report) == [
            ("CMP.PERCENT", "Penalty", "0.2% per day", "0.2% per month", "error")
        ]


# --- COMPARE-R5-06: an amount's words after its currency ------------------------------------------


class TestAmountWordsAfterTheCurrency:
    S = "1. ხელშეკრულების საგანია თაროების შესყიდვა."

    def test_a_change_is_one_amount_finding(self) -> None:
        report = run(
            [self.S, "2. ხელშეკრულების ღირებულება შეადგენს 6 400 ლარს (ექვსი ათას ოთხასი ლარი)."],
            [self.S, "2. ხელშეკრულების ღირებულება შეადგენს 7 400 ლარს (შვიდი ათას ოთხასი ლარი)."],
        )
        assert substantive(report) == [
            ("CMP.AMOUNT", "Total amount", "6,400.00 GEL", "7,400.00 GEL", "error")
        ]

    def test_a_restyle_is_no_finding(self) -> None:
        report = run(
            [self.S, "2. ხელშეკრულების ღირებულება შეადგენს 6 400 (ექვსი ათას ოთხასი) ლარს."],
            [self.S, "2. ხელშეკრულების ღირებულება შეადგენს 6 400 ლარს (ექვსი ათას ოთხასი ლარი)."],
        )
        assert substantive(report) == []

    def test_near_miss_a_bracket_that_is_no_figure_s_words(self) -> None:
        report = run(
            [self.S, "2. ღირებულება შეადგენს 6 400 ლარს (ერთი თვის)."],
            [self.S, "2. ღირებულება შეადგენს 6 400 ლარს (ორი თვის)."],
        )
        assert substantive(report) != []


# --- COMPARE-R5-08: an advance clause's amount and share ------------------------------------------


class TestAdvanceClause:
    @staticmethod
    def clause(amount: str, share: int) -> str:
        return (
            f"2. შემსყიდველი გადაიხდის ავანსს {amount} ლარის ოდენობით, ხელშეკრულების "
            f"ღირებულების {share}%."
        )

    def test_the_advance_s_amount_is_no_total_and_its_share_is_the_advance(self) -> None:
        for amount in ("5 520 (ხუთი ათას ხუთას ოცი)", "3 680 (სამი ათას ექვსას ოთხმოცი)", "3 680"):
            hits = {(h.type, str(h.normalized)) for h in hits_of(self.clause(amount, 30))}
            assert (FactType.payment_terms, "advance 30%") in hits, amount
            assert not any(t is FactType.total_amount for t, _ in hits), amount

    def test_a_revised_advance(self) -> None:
        p = "1. ღირებულება შეადგენს 18 400 ლარს."
        report = run(
            [p, self.clause("5 520 (ხუთი ათას ხუთას ოცი)", 30)],
            [p, self.clause("3 680 (სამი ათას ექვსას ოთხმოცი)", 20)],
        )
        assert sorted(substantive(report)) == [
            ("CMP.AMOUNT", "Amount (unclassified)", "5,520.00 GEL", "3,680.00 GEL", "error"),
            ("CMP.PAYMENT", "Payment terms", "advance 30%", "advance 20%", "error"),
        ]

    def test_near_miss_the_advance_only_qualifies_the_price(self) -> None:
        hits = {
            (h.type, str(h.normalized))
            for h in hits_of("ღირებულება, ავანსის ჩათვლით, შეადგენს 36 000 ლარს.")
        }
        assert (FactType.total_amount, "36000.0") in hits

    def test_near_miss_a_comparison_is_no_negation(self) -> None:
        said = (
            "The advance shall be paid no later than 5 days after signing and is 30% of the price."
        )
        assert [h.normalized for h in find_percentages(said)] == ["advance 30%"]

    def test_near_miss_no_advance_and_a_guarantee_s_share(self) -> None:
        said = (
            "4.2. ავანსი არ გაიცემა, ანგარიშსწორება მოხდება ღირებულების 100%-ით მიწოდების შემდეგ."
        )
        assert [h.type for h in find_percentages(said)] == [FactType.percentage]
        said = (
            "4.3. მიმწოდებელი წარმოადგენს საბანკო გარანტიას ავანსის თანხის 100%-ის ოდენობით, "
            "რომელიც მოქმედებს მიწოდებამდე და გაიცემა უპირობოდ."
        )
        assert all(h.type is not FactType.payment_terms for h in find_percentages(said))


# --- COMPARE-R5-09: the price of one item is a price ----------------------------------------------


class TestPriceOfOneItem:
    def test_one_item_s_price_is_a_unit_price(self) -> None:
        for clause in (
            "2.3. ერთი სამუშაო მაგიდის ფასი შეადგენს 250 ლარს.",
            "2.3. 1 (ერთი) ცალი სამუშაო მაგიდის ღირებულება შეადგენს 250 ლარს.",
            "2.3. ერთი სკამის ღირებულება შეადგენს 250 ლარს.",
        ):
            types = [
                h.type for h in hits_of(clause) if h.type in (FactType.price, FactType.total_amount)
            ]
            assert types == [FactType.price], clause

    def test_the_change_is_a_unit_price_change(self) -> None:
        total = "1. საერთო ღირებულება შეადგენს 18 400 ლარს."
        report = run(
            [total, "2. ერთი ცალი სკამის ღირებულება შეადგენს 250 ლარს."],
            [total, "2. ერთი ცალი სკამის ღირებულება შეადგენს 260 ლარს."],
        )
        assert substantive(report) == [
            ("CMP.AMOUNT", "Unit price", "250.00 GEL", "260.00 GEL", "error")
        ]

    def test_near_miss_the_works_and_a_figure_s_own_words(self) -> None:
        for clause in (
            "2.1. სამუშაოების ფასი შეადგენს 21 000 ლარს.",
            "2.1. ხელშეკრულების ღირებულება შეადგენს 21 000 (ოცდაერთი ათასი) ლარს.",
            "2.1. ხელშეკრულების ღირებულება შეადგენს 1 000 (ერთი ათასი) ლარს.",
        ):
            types = [
                h.type for h in hits_of(clause) if h.type in (FactType.price, FactType.total_amount)
            ]
            assert types == [FactType.total_amount], clause
        # "სამუშაო დღე" is a working day, no work's price
        assert [h.type for h in find_percentages("გადახდა 5 სამუშაო დღეში 30%-ით")] != []

    def test_near_miss_a_period_s_price_and_as_per(self) -> None:
        # a count of one before a unit of time prices a period, and "as per" is no unit
        for clause in (
            "4.1. ერთი წლის მომსახურების ღირებულება შეადგენს 12 000 ლარს.",
            "4.1. 1 თვის მომსახურების ღირებულება შეადგენს 1 000 ლარს.",
            "The total value of the Contract as per Annex 1 is GEL 36,000.",
        ):
            assert [h.type for h in find_amounts(clause)] == [FactType.total_amount], clause
