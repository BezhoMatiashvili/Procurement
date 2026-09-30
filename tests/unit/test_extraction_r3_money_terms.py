"""Extraction defects a red team found and a verifier reproduced (round 3): money, contract
numbers, project names, VAT, payment terms and penalties. One test or more each, with a near
miss beside every widened pattern. Every document here is invented: names, codes and figures
are made up."""

from __future__ import annotations

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
