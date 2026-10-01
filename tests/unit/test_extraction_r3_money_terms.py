"""Extraction defects a red team found and a verifier reproduced (round 3): money, contract
numbers, project names, VAT, payment terms and penalties. One test or more each, with a near
miss beside every widened pattern. Every document here is invented: names, codes and figures
are made up."""

from __future__ import annotations

from procurement_agent.checks.document_facts import document_facts
from procurement_agent.config import Settings
from procurement_agent.extraction.georgian import lemma
from procurement_agent.extraction.pipeline import run_extraction
from procurement_agent.extraction.rules import find_amounts, find_contract_numbers
from procurement_agent.extraction.schemas import FactType, Provenance
from procurement_agent.ingestion.chunker import Chunk

SETTINGS = Settings(ollama_base_url="http://localhost:11434")

KA_HEAD = "ხელშეკრულება N 9/2026\nქ. გორი, 2026 წლის 11 თებერვალი\n"
EN_HEAD = "AGREEMENT No. 9/2026\nGori, 11 February 2026\n"
ACT_HEAD = "მიღება-ჩაბარების აქტი\nქ. გორი, 2026 წლის 20 მაისი\n"


def _result(text: str):  # type: ignore[no-untyped-def]
    chunk = Chunk(id="d:0", text=text, body=text, provenance=Provenance(chunk_id="d:0"))
    return run_extraction(document_id="d", chunks=[chunk], settings=SETTINGS, llm=None)


def _extract(text: str) -> dict[FactType, list[str]]:
    out: dict[FactType, list[str]] = {}
    for f in _result(text).facts:
        out.setdefault(f.type, []).append(
            str(f.normalized if f.normalized is not None else f.value)
        )
    return out


def _conflicts(text: str) -> list[str]:
    return [str(f.normalized) for f in _result(text).facts if f.conflict]


def _amount_types(text: str) -> dict[float, FactType]:
    return {float(h.normalized): h.type for h in find_amounts(text)}  # type: ignore[arg-type]


def _numbers(text: str) -> list[str]:
    return [str(h.normalized) for h in find_contract_numbers(text)]


# --- EXTRACTION-R3-14: a figure, its words, then the magnitude word ---------------------------


def test_a_magnitude_after_the_figure_s_words_multiplies_the_figure() -> None:
    assert _amount_types("ღირებულება შეადგენს 48 (ორმოცდარვა) ათას ლარს.") == {
        48000.0: FactType.total_amount
    }
    assert _amount_types("ღირებულება შეადგენს 2,5 (ორი მთელი ხუთი) მილიონ ლარს.") == {
        2500000.0: FactType.total_amount
    }
    assert _extract(KA_HEAD + "4.1. ღირებულება შეადგენს 48 (ორმოცდარვა) ათას ლარს.")[
        FactType.total_amount
    ] == ["48000.0"]


def test_words_before_no_magnitude_leave_the_figure_as_it_is() -> None:
    # "ათასობით" (thousands of) is no magnitude, and a bracket before the currency is the
    # figure's words only
    assert _amount_types("ღირებულება 7 (შვიდი) ათასობით ლარს") == {}
    assert _amount_types("ღირებულება შეადგენს 48 (ორმოცდარვა) ლარს.") == {
        48.0: FactType.total_amount
    }


# --- EXTRACTION-R3-04: an act names its contract with the number first -------------------------


def test_the_number_before_the_contract_word_is_the_contract_s_number() -> None:
    assert _numbers("მხარეებს შორის 2026 წლის 2 მარტის N 41/2026 ხელშეკრულების საფუძველზე") == [
        "41/2026"
    ]
    assert _numbers("1. №41/2026 ხელშეკრულების თანახმად შესრულდა სამუშაო.") == ["41/2026"]
    assert _numbers("N 41/2026 ხელშეკრულებით გათვალისწინებული") == ["41/2026"]
    act = ACT_HEAD + "1. №41/2026 ხელშეკრულების თანახმად შესრულდა მომსახურება."
    assert _extract(act)[FactType.contract_number] == ["41/2026"]


def test_a_number_before_another_word_is_no_contract_number() -> None:
    assert _numbers("იხილეთ N 5 პუნქტის მოთხოვნები") == []
    assert _numbers("N 17 აქტის საფუძველზე") == []
    assert _numbers("2026 წლის ხელშეკრულება") == []  # no number sign


def test_the_number_after_the_word_is_read_once() -> None:
    assert _numbers("ხელშეკრულება N 41/2026 ხელშეკრულების დანართი") == ["41/2026"]


# --- EXTRACTION-R3-13: a case ending hyphenated inside the quotes ------------------------------


def test_a_hyphenated_ending_on_a_latin_or_figure_stem_is_dropped() -> None:
    assert lemma("„Blue Harbour-სთვის“") == "„Blue Harbour“"
    assert lemma("საბა 4-ისთვის") == "საბა 4"
    assert lemma("ბლოკი N7-ის") == "ბლოკი N7"
    assert lemma("River Park-ში") == "River Park"
    text = KA_HEAD + "1.1. ხელშეკრულების საგანია პროექტ „Blue Harbour-სთვის“ საპროექტო სამუშაოები."
    assert _extract(text)[FactType.project_name] == ["Blue Harbour"]


def test_a_letter_or_a_nominative_after_a_hyphen_stays() -> None:
    assert lemma("საბა-ბ") == "საბა-ბ"
    assert lemma("14/2026-ა") == "14/2026-ა"
    assert lemma("Plaza-ი") == "Plaza-ი"
    assert lemma("ნაძვის ველისთვის") == "ნაძვის ველი"


# --- EXTRACTION-R3-10: other amounts inside the price clause ------------------------------------


def test_an_advance_and_a_guarantee_past_a_turn_are_amounts() -> None:
    text = (
        "4.1. ღირებულება შეადგენს 50 000 ლარს დღგ-ს ჩათვლით, საიდანაც ავანსი შეადგენს 15 000 "
        "ლარს, ხოლო საგარანტიო თანხა - 2 500 ლარს."
    )
    assert _amount_types(text) == {
        50000.0: FactType.total_amount,
        15000.0: FactType.amount,
        2500.0: FactType.amount,
    }
    assert _conflicts(KA_HEAD + text) == []
    english = "4.1. The total price is GEL 50,000, of which the advance is GEL 15,000."
    assert _amount_types(english)[15000.0] is FactType.amount


def test_a_monthly_fee_beside_the_named_total_is_an_amount() -> None:
    text = (
        "4.1. ყოველთვიური ღირებულება შეადგენს 4 000 ლარს, ხოლო ხელშეკრულების საერთო "
        "ღირებულება - 48 000 ლარს."
    )
    assert _amount_types(text) == {4000.0: FactType.amount, 48000.0: FactType.total_amount}
    found = _extract(KA_HEAD + text)
    assert found[FactType.total_amount] == ["48000.0"]
    assert FactType.price not in found


def test_an_advance_word_with_no_turn_leaves_the_price() -> None:
    # "including the advance" qualifies the price: no turn, no second amount
    assert _amount_types("4.1. ღირებულება, ავანსის ჩათვლით, შეადგენს 50 000 ლარს.") == {
        50000.0: FactType.total_amount
    }
    # a monthly fee with no total beside it is left as the rules type it
    assert _amount_types("4.1. ყოველთვიური ღირებულება შეადგენს 4 000 ლარს.") == {
        4000.0: FactType.total_amount
    }


# --- EXTRACTION-R3-06: the price again, before VAT, in a bracket -----------------------------


def test_the_net_figure_in_a_bracket_after_the_price_is_an_amount() -> None:
    text = "4.1. ღირებულება შეადგენს 2 360 ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 2 000 ლარი)."
    assert _amount_types(text) == {2360.0: FactType.total_amount, 2000.0: FactType.amount}
    english = "4.1. The price is GEL 2,360 incl. VAT (excl. VAT GEL 2,000)."
    assert _amount_types(english)[2000.0] is FactType.amount


def test_a_bracketed_price_with_no_price_before_it_is_the_price() -> None:
    assert _amount_types("4.1. ფასი (დღგ-ს ჩათვლით 2 360 ლარი).") == {2360.0: FactType.price}
    # a bracket with no tax in it restates nothing
    assert _amount_types("4.1. ღირებულება შეადგენს 2 360 ლარს (ორი ათას სამას სამოცი ლარი).") == {
        2360.0: FactType.total_amount
    }


def _doc(text: str, ft: FactType) -> list[str]:
    return [f.normalized for f in document_facts(text) if f.type is ft]


# --- EXTRACTION-R3-01: the payment verbs contracts use most ------------------------------------


def test_every_payment_verb_makes_a_payment_clause() -> None:
    for verb_clause in (
        "4.2. დამკვეთი მიმწოდებელს ღირებულებას გადაუხდის აქტიდან 9 სამუშაო დღის განმავლობაში.",
        "4.2. დამკვეთი ვალდებულია გადაუხადოს მიმწოდებელს ღირებულება 9 სამუშაო დღის ვადაში.",
        "4.2. დამკვეთი ღირებულებას მიმწოდებლის ანგარიშზე გადაურიცხავს 9 სამუშაო დღის ვადაში.",
        "4.2. დამკვეთი ღირებულებას აუნაზღაურებს მიმწოდებელს 9 სამუშაო დღის ვადაში.",
        "4.2. დამკვეთი აანაზღაურებს ღირებულებას 9 სამუშაო დღის ვადაში.",
        "4.2. თანხა ჩაირიცხება მიმწოდებლის ანგარიშზე 9 სამუშაო დღის განმავლობაში.",
        "4.2. დამკვეთი იხდის ღირებულებას აქტიდან 9 სამუშაო დღის განმავლობაში.",
    ):
        assert _doc(KA_HEAD + verb_clause, FactType.payment_terms) == ["P9D working"], verb_clause
    english = EN_HEAD + "4.2. The Buyer pays the price within 9 working days after acceptance."
    assert _doc(english, FactType.payment_terms) == ["P9D working"]


def test_a_staged_payment_with_the_indirect_verb_states_the_balance_s_deadline() -> None:
    text = KA_HEAD + (
        "4.2. დამკვეთი მიმწოდებელს გადაუხდის ავანსს 9 000 ლარის ოდენობით, ხოლო დარჩენილ თანხას "
        "- აქტის გაფორმებიდან 9 სამუშაო დღის განმავლობაში."
    )
    assert _doc(text, FactType.payment_terms) == ["P9D working"]


def test_a_payer_a_damages_clause_or_a_transfer_of_goods_pays_no_fee() -> None:
    for clause in (
        KA_HEAD + "4.2. მიმწოდებელი არის დღგ-ს გადამხდელი და მიაწოდებს 9 სამუშაო დღის ვადაში.",
        KA_HEAD + "4.2. მიმწოდებელი ზიანს აუნაზღაურებს დამკვეთს 9 სამუშაო დღის ვადაში.",
        EN_HEAD + "4.2. The Supplier shall transfer the goods within 9 working days.",
    ):
        assert _doc(clause, FactType.payment_terms) == [], clause
    english = EN_HEAD + "4.2. The Supplier shall pay the Buyer's damages within 9 working days."
    assert _doc(english, FactType.payment_terms) == []


def test_damage_mentioned_in_a_payment_clause_leaves_it_a_payment_clause() -> None:
    for clause in (
        KA_HEAD + "4.2. შემკვეთი ღირებულებას გადაიხდის მიწოდებიდან 9 სამუშაო დღის განმავლობაში, "
        "თუ საქონელი არ არის დაზიანებული.",
        KA_HEAD + "4.2. ღირებულება გადაიხდება 9 სამუშაო დღეში, თუ შემკვეთს ზიანი არ მიადგა.",
        EN_HEAD + "4.2. The Buyer shall pay the price within 9 working days after delivery of the "
        "goods free of damage.",
    ):
        assert _doc(clause, FactType.payment_terms) == ["P9D working"], clause


# --- EXTRACTION-R3-09: the balance's deadline after a semicolon --------------------------------


def test_the_balance_after_a_semicolon_states_the_payment_term() -> None:
    text = EN_HEAD + (
        "4.2. An advance of 25% is paid within 3 banking days; the balance within 12 working "
        "days after acceptance."
    )
    assert _doc(text, FactType.payment_terms) == ["P12D working advance 25%"]
    georgian = KA_HEAD + (
        "4.2. ავანსი 25% გადაიხდება 3 სამუშაო დღეში; დარჩენილი თანხა - აქტიდან 12 სამუშაო დღის "
        "ვადაში."
    )
    assert _doc(georgian, FactType.payment_terms) == ["P12D working advance 25%"]


def test_a_sentence_after_a_full_stop_or_about_something_else_does_not_pay() -> None:
    # after a full stop the balance names a new sentence; the remaining obligations are no payment
    text = (
        EN_HEAD + "4.2. The price is paid within 3 banking days; the remaining duties last 12 days."
    )
    assert _doc(text, FactType.payment_terms) == ["P3D working"]


# --- EXTRACTION-R3-06: the price with VAT and its net figure in a bracket ------------------------


def test_the_net_figure_s_bracket_does_not_decide_the_vat_status() -> None:
    text = KA_HEAD + "4.1. ღირებულება შეადგენს 2 360 ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 2 000 ლარი)."
    assert _doc(text, FactType.vat_status) == ["incl"]
    found = _extract(text)
    assert found[FactType.total_amount] == ["2360.0"]
    assert found[FactType.vat_status] == ["incl"]
    assert _conflicts(text) == []
    english = EN_HEAD + "4.1. The price is GEL 2,360 inclusive of VAT (excl. VAT GEL 2,000)."
    assert _doc(english, FactType.vat_status) == ["incl"]


def test_a_price_before_vat_with_no_bracket_is_still_excluded() -> None:
    text = KA_HEAD + "4.1. ღირებულება შეადგენს 2 000 ლარს დღგ-ს გარეშე."
    assert _doc(text, FactType.vat_status) == ["excl"]


# --- EXTRACTION-R3-07: "net" that is no net amount -----------------------------------------------


def test_net_of_vat_is_an_exclusion() -> None:
    assert _doc(EN_HEAD + "4.1. The price is GEL 2,000 net of VAT.", FactType.vat_status) == [
        "excl"
    ]


def test_net_days_or_net_weight_leave_the_stated_status() -> None:
    for tail in (
        "4.2. Payment terms: net 45 days from the invoice date.",
        "4.2. The price is set per kilogram of net weight.",
    ):
        text = EN_HEAD + "4.1. The price is GEL 2,000 inclusive of VAT.\n" + tail
        assert _doc(text, FactType.vat_status) == ["incl"], tail
    georgian = KA_HEAD + (
        "4.1. ღირებულება შეადგენს 2 000 ლარს დღგ-ს ჩათვლით.\n4.2. ფასი განისაზღვრება ნეტო "
        "მასის მიხედვით."
    )
    assert _doc(georgian, FactType.vat_status) == ["incl"]


def test_a_net_fee_is_still_net() -> None:
    assert _doc(KA_HEAD + "4.1. ანაზღაურება შეადგენს 900 ლარს NET.", FactType.vat_status) == ["net"]
    text = KA_HEAD + "4.1. ანაზღაურება შეადგენს 900 ლარს NET (საშემოსავლო გადასახადის გარეშე)."
    assert _doc(text, FactType.vat_status) == ["net"]
    # "net" before the money figure is the net amount itself, no count of days
    for before in ("4.1. ანაზღაურება შეადგენს NET 900 ლარს.", "4.1. ანაზღაურება: ნეტო 900 ლარი."):
        assert _doc(KA_HEAD + before, FactType.vat_status) == ["net"], before


# --- EXTRACTION-R3-08: a non-payer clause under a condition --------------------------------------


def test_a_conditional_non_payer_clause_does_not_override_the_stated_status() -> None:
    text = KA_HEAD + (
        "4.1. ღირებულება შეადგენს 2 000 ლარს დღგ-ს ჩათვლით. იმ შემთხვევაში, თუ მიმწოდებელი არ "
        "არის დღგ-ს გადამხდელი, ღირებულება არ იცვლება."
    )
    assert _doc(text, FactType.vat_status) == ["incl"]
    english = EN_HEAD + (
        "4.1. The price is GEL 2,000 inclusive of VAT. If the Supplier is not registered as a "
        "VAT payer, the price does not change."
    )
    assert _doc(english, FactType.vat_status) == ["incl"]


def test_a_non_payer_stated_outright_is_none() -> None:
    text = KA_HEAD + "4.1. მიმწოდებელი არ არის დღგ-ს გადამხდელი. ღირებულება შეადგენს 2 000 ლარს."
    assert _doc(text, FactType.vat_status) == ["none"]
    # with nothing else stated, the condition is all the document says
    conditional = KA_HEAD + (
        "4.1. ღირებულება შეადგენს 2 000 ლარს. თუ მიმწოდებელი არ არის დღგ-ს გადამხდელი, ფასი არ "
        "იცვლება."
    )
    assert _doc(conditional, FactType.vat_status) == ["none"]


# --- EXTRACTION-R3-12: a penalty's rate is read in its own sentence ------------------------------


def test_a_rate_of_the_next_clause_is_no_penalty() -> None:
    penalty = "5.1. დაგვიანებისთვის მიმწოდებელს ეკისრება პირგასამტეხლო 0.3% ყოველ დღეზე."
    for neighbour in (
        "5.2. მიმწოდებელი წარადგენს საბანკო გარანტიას ღირებულების 4%-ის ოდენობით.",
        "5.2. საგარანტიო თანხის სახით დაკავდება ღირებულების 4%.",
        "5.2. ფასი მოიცავს 18% დღგ-ს.",
    ):
        text = KA_HEAD + penalty + " " + neighbour
        assert _doc(text, FactType.penalty) == ["0.3% per day"], neighbour
        assert _extract(text)[FactType.penalty] == ["0.3% per day"], neighbour


def test_fine_inside_another_word_is_no_penalty_word() -> None:
    text = EN_HEAD + (
        "1.1. Consulting services as defined in Annex 2.\n"
        "4.2. The Buyer shall pay 40% of the price on delivery."
    )
    assert _doc(text, FactType.penalty) == []
    assert FactType.penalty not in _extract(text)


def test_a_penalty_named_by_its_heading_or_as_a_fine_is_kept() -> None:
    heading = KA_HEAD + "5. პირგასამტეხლო 5.1. ვადის გადაცილებისას ეკისრება 0.3% ყოველ დღეზე."
    assert _doc(heading, FactType.penalty) == ["0.3% per day"]
    english = EN_HEAD + "5.1. A fine of 0.3% per day of delay is charged."
    assert _doc(english, FactType.penalty) == ["0.3% per day"]
    late_advance = (
        KA_HEAD + "5.1. ავანსის დაბრუნების დაგვიანებისთვის პირგასამტეხლო 0.3% ყოველ დღეზე."
    )
    assert _doc(late_advance, FactType.penalty) == ["0.3% per day"]
