"""Dates as Georgian documents write them, and the periods and document facts read from them.

Synthetic text only: every name, code and date here is invented.
"""

from __future__ import annotations

from datetime import date

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.compare import compare
from procurement_agent.checks.document_facts import document_facts, is_act
from procurement_agent.config import Settings
from procurement_agent.eval.runner import compare_label
from procurement_agent.extraction.anchors import preamble_end
from procurement_agent.extraction.georgian import (
    MONTH_FORMS,
    month_case,
    month_form,
    month_typo,
    render_date_like,
)
from procurement_agent.extraction.pipeline import run_extraction
from procurement_agent.extraction.rules import find_dates
from procurement_agent.extraction.schemas import FactType, Provenance
from procurement_agent.ingestion.chunker import Chunk, estimate_tokens
from procurement_agent.ingestion.segmenter import Segment
from procurement_agent.ingestion.segmenter import build as build_segments
from tests.unit.test_aoa_check import ACT, CONTRACT

# --- the day of a date: quoted, ordinal ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "iso", "raw"),
    [
        ("„12“ მარტი, 2027 წ.", "2027-03-12", "„12“ მარტი, 2027 წ."),
        ("ქ. ფოთი, „07“ ოქტომბერი 2027 წელი", "2027-10-07", "„07“ ოქტომბერი 2027 წელი"),
        (",,12'' მარტი, 2027 წ.", "2027-03-12", ",,12'' მარტი, 2027 წ."),
        ("2027 წლის „3“ მაისი", "2027-05-03", "2027 წლის „3“ მაისი"),
    ],
)
def test_a_quoted_day_is_a_date(text: str, iso: str, raw: str) -> None:
    [hit] = find_dates(text)
    assert (hit.normalized, hit.raw_text) == (iso, raw)


def test_a_quoted_name_before_a_date_is_not_part_of_it() -> None:
    [hit] = find_dates("„დამკვეთი“ 14 მარტი 2027")
    assert (hit.normalized, hit.raw_text) == ("2027-03-14", "14 მარტი 2027")


def test_an_unclosed_quote_is_left_out_of_the_date() -> None:
    [hit] = find_dates("„14 მარტი 2027")
    assert hit.raw_text == "14 მარტი 2027"


@pytest.mark.parametrize(
    ("text", "iso"),
    [
        ("2027 წლის 1-ლი აპრილი", "2027-04-01"),
        ("2027 წლის 1-ლი აპრილიდან", "2027-04-01"),
        ("1-ლი აპრილი, 2027 წ.", "2027-04-01"),
        ("2027 წლის 1 - ლი აპრილი", "2027-04-01"),
    ],
)
def test_an_ordinal_day_is_a_date(text: str, iso: str) -> None:
    assert [h.normalized for h in find_dates(text)] == [iso]


def test_both_ends_of_a_range_from_an_ordinal_day_are_read() -> None:
    text = "შეასრულოს სამუშაო 2027 წლის 1-ლი აპრილიდან 2027 წლის 31 ივლისის ჩათვლით."
    assert [h.normalized for h in find_dates(text)] == ["2027-04-01", "2027-07-31"]


def test_a_two_digit_year_is_read_only_where_the_word_for_year_follows_it() -> None:
    assert [h.normalized for h in find_dates("თბილისი, 14.03.27 წ.")] == ["2027-03-14"]
    assert [h.normalized for h in find_dates("14.03.27 წლის ხელშეკრულება")] == ["2027-03-14"]
    assert find_dates("ოთახი 14.03.27, სართული 2") == []
    assert find_dates("კოდი 01.14.03.27 წ.") == []


# --- a month's dative ------------------------------------------------------------------------


def test_the_vowel_stem_month_has_a_dative() -> None:
    assert month_form(8, "dat") == "აგვისტოს"
    assert month_form(8, "gen") == "აგვისტოს"
    assert month_form(8, "nom") == "აგვისტო"
    assert month_form(9, "dat") == "სექტემბერს"
    assert month_form(9, "gen") == "სექტემბრის"
    # the table the typo reader ranks by is unchanged: "აგვისტოს" is still a genitive there
    assert MONTH_FORMS["აგვისტოს"] == (8, "gen")
    assert month_typo("აგვისტოს") is None


@pytest.mark.parametrize(
    ("word", "following", "case"),
    [
        ("აგვისტოს", "", "dat"),
        ("აგვისტოს", ".", "dat"),
        ("აგვისტოს", " გაფორმდა", "dat"),
        ("აგვისტოს", " ჩათვლით", "gen"),
        ("აგვისტოს", ", ჩათვლით", "gen"),
        ("აგვისტოს", " მდგომარეობით", "gen"),
        ("აგვისტოს", " აქტით", "gen"),
        ("აგვისტოსვე", "", "dat"),
        ("სექტემბრის", "", "gen"),
        ("სექტემბერს", " ჩათვლით", "dat"),
        ("აგვისტო", "", "nom"),
    ],
)
def test_the_word_after_a_date_settles_the_case_of_its_month(
    word: str, following: str, case: str
) -> None:
    assert month_case(word, following=following) == case


@pytest.mark.parametrize(
    ("example", "following", "day", "expected"),
    [
        # an act signed on a day is dated in the dative, and so is its correction
        ("2027 წლის 31 აგვისტოს", "", date(2027, 9, 30), "2027 წლის 30 სექტემბერს"),
        ("2027 წლის 5 სექტემბერს", "", date(2027, 8, 31), "2027 წლის 31 აგვისტოს"),
        ("2027 წლის 5 აგვისტოს", ".", date(2027, 7, 31), "2027 წლის 31 ივლისს"),
        # ... and a date a postposition follows is in the genitive
        ("2027 წლის 5 აგვისტოს", " ჩათვლით", date(2027, 7, 31), "2027 წლის 31 ივლისის"),
        ("2027 წლის 5 სექტემბრის", "", date(2027, 8, 31), "2027 წლის 31 აგვისტოს"),
        # a quoted day keeps its quotes, a zero-padded one its zero
        ("„05“ ივლისი, 2027 წ.", "", date(2027, 6, 30), "„30“ ივნისი, 2027 წ."),
        ("„15“ ივლისი, 2027 წ.", "", date(2027, 7, 3), "„3“ ივლისი, 2027 წ."),
        ("„05“ ივლისი, 2027 წ.", "", date(2027, 7, 3), "„03“ ივლისი, 2027 წ."),
        ("05 ივლისი 2027", "", date(2027, 6, 30), "30 ივნისი 2027"),
        ("2027 წლის 1-ლი ივლისი", "", date(2027, 6, 30), "2027 წლის 30 ივნისი"),
    ],
)
def test_a_correction_is_written_in_the_examples_style_and_case(
    example: str, following: str, day: date, expected: str
) -> None:
    assert render_date_like(day, example, following=following) == expected


# --- the act's date, corrected in the act's own form -----------------------------------------

_SIGNED = "აქტი გაფორმდა 2026 წლის 30 აპრილი"


def _act_date_correction(act_date: str) -> str | None:
    act = ACT.replace(_SIGNED, f"აქტი გაფორმდა {act_date}.")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.ACT_DATE"]
    assert finding.severity == "error"
    return finding.expected


def test_an_act_dated_in_the_dative_is_corrected_in_the_dative() -> None:
    assert _act_date_correction("2026 წლის 5 მაისს") == "2026 წლის 30 აპრილს"


def test_an_act_dated_with_a_quoted_day_is_read_and_corrected_in_that_style() -> None:
    act = ACT.replace(_SIGNED, "აქტი გაფორმდა „30“ აპრილი, 2026 წ.")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    assert report.summary["act_date"] == "2026-04-30"
    assert [f for f in report.findings if f.severity in ("error", "warning")] == []
    assert _act_date_correction("„05“ მაისი, 2026 წ.") == "„30“ აპრილი, 2026 წ."


# --- what a document states as a whole -------------------------------------------------------

_PARTIES = (
    "შპს „მთის წყარო“, საიდენტიფიკაციო კოდი: 200100200 (შემდგომში „შემკვეთი“), და\n"
    "შპს „ქვის კიბე“, საიდენტიფიკაციო კოდი: 300200300 (შემდგომში „შემსრულებელი“)\n"
)


def _facts(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in document_facts(text):
        out.setdefault(f.type.value, []).append(f.normalized)
    return out


def _act(period_clause: str, *, above: str = "") -> str:
    return (
        f"{above}მიღება-ჩაბარების აქტი\n"
        f"{_PARTIES}"
        "1. მხარეებმა 2027 წლის 3 მარტს გააფორმეს ხელშეკრულება მომსახურების გაწევაზე.\n"
        f"2. {period_clause}\n"
        "3. საფასური შეადგენს 500 (ხუთასი) ლარს.\n"
        "4. აქტი გაფორმდა 2027 წლის 30 აპრილს.\n"
    )


@pytest.mark.parametrize(
    "clause",
    [
        "მომსახურება შესრულდა 2027 წლის 1 აპრილიდან 2027 წლის 30 აპრილის ჩათვლით.",
        "მომსახურება შესრულებულია 2027 წლის 1 აპრილიდან 2027 წლის 30 აპრილის ჩათვლით.",
        "მომსახურება გაწეულ იქნა 2027 წლის 1 აპრილიდან 2027 წლის 30 აპრილის ჩათვლით.",
        "სამუშაოები განხორციელდა 2027 წლის 1 აპრილიდან 2027 წლის 30 აპრილამდე.",
        "სამუშაოები ჩატარდა 2027 წლის 1 აპრილიდან 30 აპრილის ჩათვლით.",
        "მომსახურება შესრულდა 2027 წლის 1 აპრილიდან 30 აპრილის ჩათვლით.",
        "მომსახურების გაწევის პერიოდი: 01.04.2027 - 30.04.2027.",
        "მომსახურება შესრულდა 2027 წლის 1-ლი აპრილიდან 2027 წლის 30 აპრილის ჩათვლით.",
        "მომსახურება შესრულდა 2027 წლის აპრილის თვეში.",
    ],
)
def test_an_acts_service_period_is_one_range_however_it_is_worded(clause: str) -> None:
    facts = _facts(_act(clause))
    assert facts["execution_period"] == ["2027-04-01/2027-04-30"]
    assert facts["act_date"] == ["2027-04-30"]
    assert facts["contract_date"] == ["2027-03-03"]


def test_an_advance_paid_over_a_range_is_no_service_period() -> None:
    clause = "ავანსი გადახდილია 2027 წლის 1 მარტიდან 2027 წლის 10 მარტის ჩათვლით."
    assert "execution_period" not in _facts(_act(clause))


_LETTERHEAD = (
    "შპს „ქვის კიბე“\n"
    "იურიდიული მისამართი: ქ. გორი, მშვიდობის ქ. N7; ფაქტობრივი მისამართი: ქ. გორი, "
    "მშვიდობის ქ. N7, მესამე სართული, ოფისი 12\n"
    "ტელ: +995 370 00 00 00; ელ-ფოსტა: office@example.test; ვებგვერდი: www.example.test\n"
    "ბანკი: სს „სანიმუშო ბანკი“; ბანკის კოდი: XXXXGE22; ანგარიში: GE00XX0000000000000000\n"
    "____________________________________________________________\n"
)


def test_an_act_on_a_letterhead_is_an_act() -> None:
    text = _act("მომსახურება შესრულდა 2027 წლის 1 აპრილიდან 2027 წლის 30 აპრილის ჩათვლით.")
    headed = _LETTERHEAD + text
    assert headed.index("მიღება-ჩაბარების") > 300
    assert is_act(headed)
    facts = _facts(headed)
    assert facts["execution_period"] == ["2027-04-01/2027-04-30"]
    assert facts["contract_date"] == ["2027-03-03"]
    assert facts["act_date"] == ["2027-04-30"]


def test_a_contract_that_mentions_the_act_below_its_first_clause_is_no_act() -> None:
    contract = (
        _LETTERHEAD
        + "მომსახურების ხელშეკრულება N 7\n"
        + _PARTIES
        + "1. მიღება-ჩაბარების აქტი ფორმდება თვის ბოლოს.\n"
    )
    assert not is_act(contract)
    body_first = _LETTERHEAD + "1. მომსახურება.\nმიღება-ჩაბარების აქტი\n"
    assert not is_act(body_first)


def _contract(preamble: str, *clauses: str, parties: str = _PARTIES) -> str:
    return "მომსახურების ხელშეკრულება N 7\n" + preamble + parties + "\n".join(clauses) + "\n"


_IN_FORCE = "3.1. ხელშეკრულება ძალაში შედის ხელმოწერის მომენტიდან."


def test_a_contract_signed_on_an_ordinal_day_is_dated_and_in_force_from_it() -> None:
    facts = _facts(_contract("ქ. თბილისი, 2027 წლის 1-ლი მარტი\n", _IN_FORCE))
    assert facts["contract_date"] == ["2027-03-01"]
    assert facts["validity_period"] == ["2027-03-01/.."]


def test_a_contract_between_persons_is_dated_by_its_preamble() -> None:
    people = (
        "ფ/პ თამარ გელაშვილი, პირადი ნომერი: 01010101010, და\n"
        "ი/მ ლევან ხარაძე, პირადი ნომერი: 02020202020\n"
    )
    facts = _facts(_contract("ქ. თბილისი, 2027 წლის 3 მარტი\n", _IN_FORCE, parties=people))
    assert facts["contract_date"] == ["2027-03-03"]
    assert facts["validity_period"] == ["2027-03-03/.."]


def test_a_preamble_ends_at_the_first_numbered_clause_when_no_party_is_known() -> None:
    text = "SERVICES AGREEMENT No. 7\nBatumi, 3 March 2027\n1. The Contractor shall clean."
    assert preamble_end(text) == text.index("1. The")
    facts = _facts(text)
    assert facts["contract_date"] == ["2027-03-03"]


def test_a_performance_window_whose_first_day_cannot_be_read_does_not_start_at_signing() -> None:
    clause = "2.1. შეასრულოს მომსახურება 2027 წლის 1ლ აპრილიდან 2027 წლის 30 სექტემბრის ჩათვლით."
    facts = _facts(_contract("ქ. თბილისი, 2027 წლის 3 მარტი\n", clause))
    assert facts["execution_period"] == ["../2027-09-30"]


def test_a_performance_window_with_only_an_end_still_runs_from_signing() -> None:
    clause = "2.1. შეასრულოს მომსახურება 2027 წლის 30 სექტემბრამდე."
    facts = _facts(_contract("ქ. თბილისი, 2027 წლის 3 მარტი\n", clause))
    assert facts["execution_period"] == ["2027-03-03/2027-09-30"]


# --- the header's dates in the extraction ----------------------------------------------------

_SETTINGS = Settings(ollama_base_url="http://localhost:11434", llm_chunk_token_budget=600)


def _chunk(cid: str, text: str) -> Chunk:
    return Chunk(
        id=cid,
        text=text,
        body=text,
        provenance=Provenance(page=1, chunk_id=cid),
        token_estimate=estimate_tokens(text),
    )


def _contract_dates(*texts: str) -> list[str]:
    chunks = [_chunk(f"c{i}", t) for i, t in enumerate(texts)]
    res = run_extraction(document_id="d", chunks=chunks, settings=_SETTINGS, llm=None)
    return sorted(str(f.normalized) for f in res.facts if f.type is FactType.contract_date)


def test_a_short_contracts_clause_dates_are_no_header_dates() -> None:
    text = (
        _PARTIES + "2.1. „შემსრულებელი“ ვალდებულია შეასრულოს სამუშაო 2027 წლის 15 მარტიდან "
        "2027 წლის 30 ივნისის ჩათვლით.\n"
        "ხელშეკრულება ხელმოწერილია 2027 წლის 3 მარტს.\n"
    )
    assert _contract_dates(text) == ["2027-03-03"]


def test_a_date_above_the_parties_is_the_header_date() -> None:
    text = "ქ. თბილისი, 2027 წლის 3 მარტი\n" + _PARTIES + "2.1. ფასი 2027 წლის 9 მაისი.\n"
    assert _contract_dates(text) == ["2027-03-03"]


# --- a contract's own date, compared ---------------------------------------------------------


def _segments(lines: list[str]) -> list[Segment]:
    return build_segments([(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(lines)])


def _date_changes(left: list[str], right: list[str]) -> list[tuple[str | None, str | None, str]]:
    report = compare(
        _segments(left),
        _segments(right),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    return [
        (f.expected, f.actual, str(compare_label(f, report)))
        for f in report.findings
        if f.rule_id == "CMP.DATE"
    ]


_HEADED = [
    "მომსახურების ხელშეკრულება N 7",
    "ქ. ქუთაისი, 2027 წლის 4 თებერვალი",
    *_PARTIES.splitlines(),
    "1. „შემსრულებელი“ ასუფთავებს ოფისს.",
    "2. ხელშეკრულება ძალაში შედის ხელმოწერის მომენტიდან.",
]


def test_a_contracts_heading_date_changed_is_its_contract_date() -> None:
    right = [line.replace("4 თებერვალი", "11 თებერვალი") for line in _HEADED]
    assert _date_changes(_HEADED, right) == [("2027-02-04", "2027-02-11", "Contract date")]


def test_an_english_heading_date_changed_is_the_contract_date() -> None:
    left = ["CLEANING AGREEMENT No. 7", "Kutaisi, 4 February 2027", "1. The Contractor cleans."]
    right = [line.replace("4 February", "11 February") for line in left]
    assert _date_changes(left, right) == [("2027-02-04", "2027-02-11", "Contract date")]
