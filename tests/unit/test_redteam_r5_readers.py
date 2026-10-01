"""Defects the fifth red-team round found in the party readers the extraction, the comparison and
the act check share: a party's name slipped in its signature block alone, a draft's blank, an
address that runs on into the next label, a hyphenated double surname, and a personal number
labelled with a sign in place of its noun.

Each test names its defect (``samples/redteam/<module>/CONFIRMED_R5.md``), and every widened
reading has a near miss beside it that must still not match. Synthetic text only: every name,
code and address here is invented.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.config import Settings
from procurement_agent.extraction.anchors import find_anchors, find_parties
from procurement_agent.extraction.georgian import lemma
from procurement_agent.extraction.normalize import ID_CODE_RE, is_blank
from procurement_agent.extraction.pipeline import run_extraction
from procurement_agent.extraction.schemas import FactType, Provenance
from procurement_agent.ingestion.chunker import Chunk
from tests.unit.test_aoa_check import ACT, CONTRACT
from tests.unit.test_compare_redteam4 import run

SETTINGS = Settings(ollama_base_url="http://localhost:11434")


def _check(act: str = ACT, contract: str | None = CONTRACT) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract)


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _extract(text: str) -> dict[FactType, list[str]]:
    chunk = Chunk(id="d:0", text=text, body=text, provenance=Provenance(chunk_id="d:0"))
    result = run_extraction(document_id="d", chunks=[chunk], settings=SETTINGS, llm=None)
    out: dict[FactType, list[str]] = {}
    for f in result.facts:
        out.setdefault(f.type, []).append(
            str(f.normalized if f.normalized is not None else f.value)
        )
    return out


def _anchored(text: str, ft: FactType) -> list[str]:
    return [h.value for h in find_anchors(text) if h.type is ft]


# --- AOA-R5-04: a party's name slipped in its signature block alone ------------------------------

_CONTRACTOR_SIGNATURE = "„შემსრულებელი“\nშპს „ბეტა სერვისი“"
_CUSTOMER_SIGNATURE = "„შემკვეთი“\nშპს „ალფა ჯგუფი“"


@pytest.mark.parametrize(
    ("old", "new", "meant"),
    [
        # a letter slipped
        (_CONTRACTOR_SIGNATURE, "„შემსრულებელი“\nშპს „ბეტა სერვისა“", "შპს ბეტა სერვისი"),
        # the final ი dropped
        (_CONTRACTOR_SIGNATURE, "„შემსრულებელი“\nშპს „ბეტა სერვის“", "შპს ბეტა სერვისი"),
        # the customer's own block
        (_CUSTOMER_SIGNATURE, "„შემკვეთი“\nშპს „ალფა ჯგუფა“", "შპს ალფა ჯგუფი"),
    ],
)
def test_a_name_slipped_in_its_signature_block_alone_gets_its_correction(
    old: str, new: str, meant: str
) -> None:
    """The preamble, with the code, names the party right; the signature block under its role
    writes the name one slip away. It is the contract's name misspelt, corrected as the same slip
    in the preamble is - not 'party not found' with no correction."""
    assert old in ACT
    findings = _of(_check(ACT.replace(old, new)), "AOA.PARTY_NAME")
    assert [(f.severity, f.expected) for f in findings] == [("error", meant)]
    assert "not found" not in findings[0].message_en


def test_another_company_in_a_signature_block_still_has_no_correction() -> None:
    """Near miss: a name nothing like either party's is no slip of one; it stays 'party not
    found in the contract', with no name offered in its place."""
    act = ACT.replace(_CONTRACTOR_SIGNATURE, "„შემსრულებელი“\nშპს „ნისლიანი ტყე“")
    findings = _of(_check(act), "AOA.PARTY_NAME")
    assert [(f.severity, f.expected) for f in findings] == [("error", None)]
    assert "not found" in findings[0].message_en


def test_a_name_written_right_in_its_signature_block_stays_clean() -> None:
    assert _of(_check(), "AOA.PARTY_NAME") == []


# --- COMPARE-R5-07: a draft's blank is no value ------------------------------------------------

_BLANK_BLOCK = (
    "___________________ (საიდენტიფიკაციო კოდი: ___________), მისამართი: ___________________, "
    "წარმოდგენილი მისი დირექტორის ________________ სახით (შემდგომში „მიმწოდებელი“)"
)
_FILLED_BLOCK = (
    "შპს „ტყის ბილიკი“ (საიდენტიფიკაციო კოდი: 404717171), მისამართი: ქ. ფოთი, ნავსადგურის ქ. "
    "N8, წარმოდგენილი მისი დირექტორის ლაშა ქობალაძის სახით (შემდგომში „მიმწოდებელი“)"
)


@pytest.mark.parametrize("blank", ["___________", "……………", "...........", "- - - -", "_ _ _ _"])
def test_a_blank_after_the_address_label_is_no_address(blank: str) -> None:
    text = (
        f"შპს „ტყის ბილიკი“ (ს/კ 404717171), მისამართი: {blank}, "
        "წარმოდგენილი მისი დირექტორის ლაშა ქობალაძის სახით"
    )
    assert _anchored(text, FactType.party_address) == []
    assert is_blank(blank)


def test_an_address_with_a_letter_or_a_figure_is_still_read() -> None:
    """Near miss: a short address, or one with a blank left inside it, is text a person wrote."""
    assert not is_blank("N 5")
    assert not is_blank("ქ. ფოთი, ____")
    text = "შპს „ტყის ბილიკი“ (ს/კ 404717171), მისამართი: ქ. ფოთი, ზღვის ქ. N2"
    assert _anchored(text, FactType.party_address) == ["ქ. ფოთი, ზღვის ქ. N2"]


def test_filling_in_a_drafts_blank_address_is_an_added_address() -> None:
    report = run([_BLANK_BLOCK], [_FILLED_BLOCK])
    address = [
        (f.rule_id, f.expected, f.actual, f.severity)
        for f in report.findings
        if f.rule_id == "CMP.PARTY" and "address" in (f.message_en or "").lower()
    ]
    assert address == [("CMP.PARTY", None, "ქ. ფოთი, ნავსადგურის ქ. N8", "error")]


# --- EXTRACTION-R5-06: an address ends before the next label ------------------------------------

_TWO_ADDRESSES = (
    "შპს „ქვის ხიდი“, ს/კ: 405229911, იურიდიული მისამართი: ქ. ჭიათურა, სადგურის ქ. N4, "
    "{qualifier} მისამართი: ქ. თბილისი, ბალახვნის ქ. N17 (შემდგომში „შემსრულებელი“)"
)


@pytest.mark.parametrize(
    "qualifier", ["ფაქტობრივი", "ფაქტიური", "საფოსტო", "ფაქტ.", "კორესპონდენციის"]
)
def test_a_legal_address_ends_before_the_next_addresss_qualifier(qualifier: str) -> None:
    text = _TWO_ADDRESSES.format(qualifier=qualifier)
    assert _anchored(text, FactType.party_address) == [
        "ქ. ჭიათურა, სადგურის ქ. N4",
        "ქ. თბილისი, ბალახვნის ქ. N17",
    ]
    assert find_parties(text)[0].address == "ქ. ჭიათურა, სადგურის ქ. N4"


@pytest.mark.parametrize(
    "requisite",
    [
        "სახაზინო კოდი: 300112233",
        "დღგ-ს გადამხდელის ნომერი: 404112233",
        "ვებგვერდი: www.example.ge",
        "ვებ-გვერდი: example.ge",
        "ტელ: 0431 22 33 44",  # the control: a contact label was always an end
    ],
)
def test_an_address_ends_where_the_next_labelled_requisite_begins(requisite: str) -> None:
    text = (
        f"ა(ა)იპ „მთის სკოლა“, ს/კ: 212009988, მისამართი: ქ. ზუგდიდი, რუსთაველის ქ. N14, "
        f"{requisite} (შემდგომში „შემსყიდველი“)"
    )
    assert _anchored(text, FactType.party_address) == ["ქ. ზუგდიდი, რუსთაველის ქ. N14"]


@pytest.mark.parametrize(
    "address",
    [
        # no colon: a part of the address, however many words
        "ქ. თბილისი, ვაჟა-ფშაველას გამზ. N5, კორპუსი 2, ბინა 5",
        "ქ. რუსთავი, მე-7 მიკრორაიონი, სადარბაზო III",
        # a colon after a part of the address itself, or after a word with a figure
        "ქ. თბილისი, ვაკის რაიონი, კორპუსი: 3, ბინა: 12",
        "ქ. ბათუმი, ზღვის ქ. N2, სართ. 4: ოფისი 9",
    ],
)
def test_an_address_part_is_no_label(address: str) -> None:
    """Near misses: what an address holds - a hyphenated street, a block and a flat, with a
    colon or without one - does not end it."""
    text = f"შპს „ქვის ხიდი“, ს/კ: 405229911, მისამართი: {address} (შემდგომში „შემსრულებელი“)"
    assert _anchored(text, FactType.party_address) == [address]


# --- EXTRACTION-R5-12: a hyphenated double surname is one surname --------------------------------

_DOUBLE_PREAMBLE = (
    "შპს „ქვის ხიდი“, ს/კ: 405229911, მისამართი: ქ. ჭიათურა, სადგურის ქ. N4 (შემდგომში "
    "„შემსრულებელი“), წარმოდგენილი მისი დირექტორის, ქეთევან ხურციძე-ლომაიას სახით, მეორე მხრივ,"
)
_DOUBLE_SIGNATURE = (
    "„შემსრულებელი“: შპს „ქვის ხიდი“, დირექტორი ქეთევან ხურციძე-ლომაია  _____________"
)


def test_a_double_surname_in_the_preamble_is_read_whole() -> None:
    assert find_parties(_DOUBLE_PREAMBLE)[0].directors == ["ქეთევან ხურციძე-ლომაიას"]
    assert _extract(_DOUBLE_PREAMBLE)[FactType.party_director] == ["ქეთევან ხურციძე-ლომაია"]


def test_a_double_surname_in_preamble_and_signature_is_one_signatory() -> None:
    text = f"{_DOUBLE_PREAMBLE}\n1.1. საგანი.\n{_DOUBLE_SIGNATURE}"
    assert _extract(text)[FactType.party_director] == ["ქეთევან ხურციძე-ლომაია"]


@pytest.mark.parametrize(
    "line",
    [
        "შპს „ქვის ხიდი“\nდირექტორი: ქეთევან ხურციძე-ლომაია",
        "შპს „ქვის ხიდი“\nქეთევან ხურციძე-ლომაია - დირექტორი",
        "შპს „ქვის ხიდი“\nდირექტორი ______ /ქეთევან ხურციძე-ლომაია/",
    ],
)
def test_a_double_surname_in_a_signatures_layouts(line: str) -> None:
    assert find_parties(line)[0].directors == ["ქეთევან ხურციძე-ლომაია"]


def test_the_nominative_of_a_double_surname_folds_its_last_part() -> None:
    assert lemma("ქეთევან ხურციძე-ლომაიას") == "ქეთევან ხურციძე-ლომაია"
    assert lemma("გია ბერიშვილი-მაისურაძის") == "გია ბერიშვილი-მაისურაძე"
    # a Latin stem's hyphenated ending is still cut as before
    assert lemma("Green Valley-ს") == "Green Valley"


@pytest.mark.parametrize(
    ("line", "name"),
    [
        # a case ending or a postposition hyphenated to the surname is no second surname
        ("შპს „ქვის ხიდი“\nდირექტორი: ლაშა ქობალაძე-ს", "ლაშა ქობალაძე"),
        ("შპს „ქვის ხიდი“\nდირექტორი: ლაშა ქობალაძე-მა", "ლაშა ქობალაძე"),
        ("შპს „ქვის ხიდი“\nდირექტორი: ლაშა ქობალაძე-სთვის", "ლაშა ქობალაძე"),
        # nor a title joined by a hyphen, nor one after a spaced dash
        ("შპს „ქვის ხიდი“\nლაშა ქობალაძე-დირექტორი", "ლაშა ქობალაძე"),
        ("შპს „ქვის ხიდი“\nდირექტორი: ლაშა ქობალაძე - ხელმოწერა", "ლაშა ქობალაძე"),
    ],
)
def test_a_hyphen_before_an_ending_or_a_title_is_no_double_surname(line: str, name: str) -> None:
    assert find_parties(line)[0].directors == [name]


def test_a_correct_act_with_a_double_surname_is_clean() -> None:
    """The act check compares the whole name: the same double surname in the contract and the
    act is no finding, and a slip in its second part is the director's slip."""
    contract = CONTRACT.replace("დავით მელაძის", "დავით მელაძე-ჯაფარიძის")
    act = ACT.replace(
        "ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“)",
        "ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის დავით "
        "მელაძე-ჯაფარიძის სახით",
    ).replace("დირექტორი: დავით მელაძე", "დირექტორი: დავით მელაძე-ჯაფარიძე")
    report = _check(act, contract)
    assert [f for f in report.findings if f.severity in ("error", "warning")] == []
    slipped = _check(act.replace("მელაძე-ჯაფარიძე\n", "მელაძე-ჯაფარძე\n"), contract)
    director = _of(slipped, "AOA.DIRECTOR")
    assert [(f.severity, f.actual, f.expected) for f in director] == [
        ("error", "დავით მელაძე-ჯაფარძე", "დავით მელაძე-ჯაფარიძე")
    ]


# --- EXTRACTION-R5-16 (policy): "პირადი №" labels a personal number ------------------------------

_PERSON = "ფ/პ ლევან ჩიქოვანი, {label} 01009988776, მისამართი: ქ. სენაკი, მშვიდობის ქ. N3"


@pytest.mark.parametrize(
    "label", ["პირადი ნომერი:", "პირადი №:", "პირადი № ", "პირადი N", "პირადი #"]
)
def test_a_personal_number_behind_its_sign_label_is_read(label: str) -> None:
    text = _PERSON.format(label=label)
    assert _anchored(text, FactType.party_id_code) == ["01009988776"]
    assert find_parties(text)[0].id_code == "01009988776"


@pytest.mark.parametrize(
    "text",
    [
        # no sign: "პირადი" alone is an adjective ("personal")
        "პირადი შემოსავალი 12345678901 ლარი",
        "ფ/პ ლევან ჩიქოვანი, პირადი 01009988776",
        # a sign, but no personal number's eleven digits after it
        "პირადი № 404112233",
        "პირადი № 0100998877",
    ],
)
def test_personal_without_its_sign_or_its_eleven_digits_is_no_label(text: str) -> None:
    assert [m.group(1) for m in ID_CODE_RE.finditer(text)] == []
