"""Party readers, red team round 4: legal forms in every spelling, where an address ends, a
signatory written before its title or in slashes, and an attorney at the act's signature.

Each test names its defect (``samples/redteam/<module>/CONFIRMED_R4.md``). Synthetic text only:
every name, code and address is invented, and each widened reading has a near miss beside it.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.extraction.anchors import (
    defined_sides,
    find_anchors,
    find_parties,
    plain_party_wording,
)
from procurement_agent.extraction.georgian import legal_form_kind
from procurement_agent.extraction.normalize import legal_form_abbreviation
from procurement_agent.extraction.schemas import FactType
from tests.unit.test_aoa_check import ACT, CONTRACT
from tests.unit.test_compare_redteam3 import CONTRACT as CMP_CONTRACT
from tests.unit.test_compare_redteam3 import edit, labelled, run, table

_PAIR = (
    "{form} „ნისლის ველი“ (ს/კ 204118833) (შემდგომში „შემკვეთი“) და შპს „ქვის ხიდი“ "
    "(ს/კ 405229911) (შემდგომში „შემსრულებელი“) ვაფორმებთ ხელშეკრულებას."
)


def _names(text: str) -> list[str | None]:
    return [p.name for p in find_parties(text)]


# --- AOA-R4-02, COMPARE-R4-05, EXTRACTION-R4-06: one legal-form table for every reader ---------


@pytest.mark.parametrize(
    ("form", "short"),
    [
        ("შ.პ.ს.", "შპს"),
        ("შ. პ. ს.", "შპს"),
        ("შ.პ.ს", "შპს"),
        ("ს.ს.", "სს"),
        ("ა(ა)იპ", "ააიპ"),
        ("ა.ა.ი.პ.", "ააიპ"),
        ("ს.ს.ი.პ.", "სსიპ"),
        ("კს", "კს"),
        ("კ.ს.", "კს"),
        ("სპს", "სპს"),
        ("ს.პ.ს.", "სპს"),
    ],
)
def test_a_dotted_or_bracketed_legal_form_names_a_party(form: str, short: str) -> None:
    text = _PAIR.format(form=form)
    assert _names(text) == [f"{short} ნისლის ველი", "შპს ქვის ხიდი"]
    names = [h.value for h in find_anchors(text) if h.type is FactType.party_name]
    assert names == [f"{short} ნისლის ველი", "შპს ქვის ხიდი"]
    assert legal_form_abbreviation(form) == short


@pytest.mark.parametrize(
    "near_miss",
    [
        "ს.კ. „ნისლის ველი“",  # an ID label, not "ს.ს."
        "ს. „ნისლის ველი“",  # one letter
        "კლასს „ნისლის ველი“",  # a word ending in "სს"
        "ბ.ს. „ნისლის ველი“",  # no form
        "შ.პ.ს. ნისლის ველი",  # no quoted name after it
    ],
)
def test_what_only_looks_like_a_legal_form_names_no_party(near_miss: str) -> None:
    assert find_parties(f"{near_miss} (ს/კ 204118833)") == []


def test_a_dotted_form_after_the_quoted_name_is_read_too() -> None:
    assert _names("„ნისლის ველი“ შ.პ.ს. (ს/კ 204118833)") == ["შპს ნისლის ველი"]


def test_every_spelling_of_a_form_is_one_wording() -> None:
    assert plain_party_wording("ა(ა)იპ „ა“, შ.პ.ს. „ბ“, ს.ს.ი.პ. „გ“, ს.ს. „დ“") == (
        "ააიპ „ა“, შპს „ბ“, სსიპ „გ“, სს „დ“"
    )
    # no quoted name after it: left as written
    assert plain_party_wording("შ.პ.ს. და ს.ს.") == "შ.პ.ს. და ს.ს."


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("შ.პ.ს. „ა“", "company"),
        ("ა(ა)იპ „ა“", "company"),
        ("კ.ს. „ა“", "company"),
        ("შპს. „ა“", "company"),
        ("ს.ს.ი.პ. „ა“", None),  # a public body, as "სსიპ" always was: not "ს.ს." + "ი.პ."
        ("ი/მ ანა ბერიძე", "individual_entrepreneur"),
    ],
)
def test_the_contractor_kind_reads_the_same_table(name: str, kind: str | None) -> None:
    assert legal_form_kind(name) == kind


_AAIP_CONTRACT = CONTRACT.replace("შპს „ალფა ჯგუფი“", "ა(ა)იპ „ალფა ჯგუფი“")


def test_a_bracketed_form_and_its_abbreviation_are_one_party_in_the_act() -> None:
    """AOA-R4-02: the contract's 'ა(ა)იპ' against the act's 'ააიპ' was 'party not found'."""
    act = ACT.replace("შპს „ალფა ჯგუფი“", "ააიპ „ალფა ჯგუფი“")
    assert check_aoa(aoa_text=act, contract_text=_AAIP_CONTRACT).errors == []
    act = ACT.replace("შპს „ალფა ჯგუფი“", "ა(ა)იპ „ალფა ჯგუფი“")
    assert (
        check_aoa(aoa_text=act, contract_text=CONTRACT.replace("შპს „ალფა", "ააიპ „ალფა")).errors
        == []
    )


def test_a_name_slip_behind_a_bracketed_form_is_reported() -> None:
    act = ACT.replace("შპს „ალფა ჯგუფი“", "ა(ა)იპ „ალფა ჯგუფ“")
    found = [
        (f.rule_id, f.actual, f.expected)
        for f in check_aoa(aoa_text=act, contract_text=_AAIP_CONTRACT).errors
    ]
    assert found == [("AOA.PARTY_NAME", "ააიპ ალფა ჯგუფ", "ააიპ ალფა ჯგუფი")]


def test_an_address_slip_behind_a_dotted_form_is_reported() -> None:
    contract = CONTRACT.replace("შპს „ბეტა სერვისი“", "შ.პ.ს. „ბეტა სერვისი“")
    act = ACT.replace("შპს „ბეტა სერვისი“", "შ.პ.ს. „ბეტა სერვისი“").replace(
        "ჭავჭავაძის 5", "ჭავჭავაძის 7"
    )
    found = [f.rule_id for f in check_aoa(aoa_text=act, contract_text=contract).errors]
    assert found == ["AOA.PARTY_ADDRESS"]


def test_a_side_the_contract_defines_and_no_party_read_for_it_is_a_skip_note() -> None:
    """AOA-R4-02: a party written in a form no reader knows was passed over in silence."""
    contract = CONTRACT.replace("შპს „ალფა ჯგუფი“", "კოოპერატივი „ალფა ჯგუფი“")
    act = ACT.replace("შპს „ალფა ჯგუფი“", "კოოპერატივი „ალფა ჯგუფი“")
    report = check_aoa(aoa_text=act, contract_text=contract)
    notes = [s.reason_en for s in report.skipped if s.rule_id == "AOA.PARTY_NAME"]
    assert any("'შემკვეთი'" in n and "could not be read" in n for n in notes)


def test_no_skip_note_when_every_defined_side_has_its_party() -> None:
    report = check_aoa(aoa_text=ACT, contract_text=CONTRACT)
    assert [s for s in report.skipped if s.rule_id == "AOA.PARTY_NAME"] == []


def test_only_a_side_is_a_defined_side() -> None:
    text = _PAIR.format(form="შპს") + " მომსახურება (შემდგომში „მომსახურება“)."
    assert defined_sides(text) == ["შემკვეთი", "შემსრულებელი"]


@pytest.mark.parametrize("form", ["ა(ა)იპ", "შ.პ.ს.", "კს", "სპს"])
def test_a_party_renamed_behind_any_form_is_a_party_change(form: str) -> None:
    """COMPARE-R4-05: the rename was only text changed."""
    left = edit(CMP_CONTRACT, "შპს „ვერცხლის ხეობა“", f"{form} „ვერცხლის ხეობა“")
    right = edit(left, f"{form} „ვერცხლის ხეობა“", f"{form} „ოქროს ხეობა“")
    short = legal_form_abbreviation(form)
    assert labelled(run(left, right)) == [
        ("CMP.PARTY", "Party name", f"{short} ვერცხლის ხეობა", f"{short} ოქროს ხეობა", "error")
    ]


def test_a_form_restyled_alone_is_no_party_change() -> None:
    right = edit(CMP_CONTRACT, "შპს „ვერცხლის ხეობა“", "შ.პ.ს. „ვერცხლის ხეობა“")
    assert labelled(run(CMP_CONTRACT, right)) == []


# --- COMPARE-R4-04: an address ends where the party's representative is named ----------------

_SUPPLIER = (
    "შპს „ქვის ხიდი“ (ს/კ 405229911), მისამართი: ქ. გორი, ლეღვის ქ. N8{rest} "
    "(შემდგომში „მიმწოდებელი“)"
)


@pytest.mark.parametrize(
    "rest",
    [
        ", წარმოდგენილი მისი დირექტორის ლაშა ქავთარაძის სახით",
        ", რომელსაც წარმოადგენს დირექტორი ლაშა ქავთარაძე",
        ", დირექტორი: ლაშა ქავთარაძე",
        ", მისი დირექტორის ლაშა ქავთარაძის სახით",
        "",
    ],
)
def test_an_address_ends_before_the_representative(rest: str) -> None:
    [party] = find_parties(_SUPPLIER.format(rest=rest))
    assert party.address == "ქ. გორი, ლეღვის ქ. N8"


@pytest.mark.parametrize(
    "address",
    ["ქ. გორი, პრეზიდენტის გამზ. N5", "ქ. გორი, რექტორის ქუჩა N5"],
)
def test_a_street_named_after_an_office_stays_in_the_address(address: str) -> None:
    [party] = find_parties(f"შპს „ქვის ხიდი“ (ს/კ 405229911), მისამართი: {address};")
    assert party.address == address


def test_a_director_replaced_after_the_address_is_one_change() -> None:
    left = edit(
        CMP_CONTRACT,
        "ქ. სენაკი, ცაცხვის ქ. N8 (",
        "ქ. სენაკი, ცაცხვის ქ. N8, წარმოდგენილი მისი დირექტორის ლაშა ქავთარაძის სახით (",
    )
    right = edit(left, "ლაშა ქავთარაძის", "გია ქავთარაძის")
    assert [(r, label) for r, label, *_ in labelled(run(left, right))] == [
        ("CMP.PARTY", "Director / signatory")
    ]


# --- COMPARE-R4-08: one address per cell of a requisites row ----------------------------------

_REQUISITES = [
    ["შემსყიდველი", "მიმწოდებელი"],
    ["შპს „ნისლის ველი“", "შპს „ქვის ხიდი“"],
    ["ს/კ 204118833", "ს/კ 405229911"],
    ["მისამართი: ქ. ფოთი, ზღვის ქ. N4", "მისამართი: ქ. გორი, ლეღვის ქ. N8"],
]


def test_another_address_label_ends_an_address() -> None:
    text = "შპს „ნისლის ველი“ მისამართი: ქ. ფოთი, ზღვის ქ. N4 მისამართი: ქ. გორი, ლეღვის ქ. N8"
    [party] = find_parties(text)
    assert party.address == "ქ. ფოთი, ზღვის ქ. N4"


def test_an_address_changed_in_its_cell_is_one_change() -> None:
    right = [list(row) for row in _REQUISITES]
    right[3][1] = "მისამართი: ქ. გორი, ლეღვის ქ. N10"
    found = labelled(run(table(_REQUISITES), table(right)))
    assert found == [
        (
            "CMP.PARTY",
            "Legal address",
            "ქ. გორი, ლეღვის ქ. N8",
            "ქ. გორი, ლეღვის ქ. N10",
            "error",
        )
    ]


# --- AOA-R4-04: a signatory in slashes, or before the head's title ---------------------------

_HEAD = (
    "შპს „ნისლის ველი“ (ს/კ 204118833) (შემდგომში „შემკვეთი“) და შპს „ქვის ხიდი“ "
    "(ს/კ 405229911) (შემდგომში „შემსრულებელი“).\n"
)


@pytest.mark.parametrize(
    "style",
    [
        "დირექტორი ______________ /{n}/",
        "დირექტორი /{n}/",
        "{n} - დირექტორი",
        "{n} – გენერალური დირექტორი",
        "{n} (დირექტორი)",
        "{n} (თავმჯდომარე)",
    ],
)
@pytest.mark.parametrize("flat", [False, True], ids=["lines", "one-line"])
def test_a_signatory_is_read_in_every_signature_layout(style: str, flat: bool) -> None:
    sigs = (
        f"„შემკვეთი“\nშპს „ნისლის ველი“\n{style.format(n='ნანა ხელაძე')}\n"
        f"„შემსრულებელი“\nშპს „ქვის ხიდი“\n{style.format(n='ლაშა ქავთარაძე')}"
    )
    text = _HEAD + sigs
    if flat:
        text = " ".join(text.split())
    assert [p.directors for p in find_parties(text)] == [["ნანა ხელაძე"], ["ლაშა ქავთარაძე"]]


@pytest.mark.parametrize(
    "line",
    [
        "ნანა ხელაძე - დირექტორის მოადგილე",  # a deputy
        "ნანა ხელაძე - ფინანსური დირექტორი",  # a function's director
        "ნანა ხელაძე - პროექტის ხელმძღვანელი",  # another body's officer
        "ხელშეკრულების პირობები - დირექტორი ვალდებულია",  # prose
        "ამის შემდეგ ნანა ხელაძე - დირექტორი",  # the name does not open its line
        "დირექტორი (უფლებამოსილი პირი)",  # a phrase in brackets after the title
        "ხელმძღვანელი (სტრუქტურული ერთეული)",
        "დირექტორი ________ /სახელი გვარი/",  # a blank's caption
        "სახელი გვარი - დირექტორი",
    ],
)
def test_a_name_before_another_title_or_in_prose_is_no_head(line: str) -> None:
    text = f"{_HEAD}„შემკვეთი“\nშპს „ნისლის ველი“\n{line}\n"
    assert [p.directors for p in find_parties(text)] == [[], []]


_ACT_SIGNED = ACT.replace("დირექტორი: დავით მელაძე", "{line}")


@pytest.mark.parametrize("line", ["დირექტორი ______ /{n}/", "{n} - დირექტორი", "{n} (დირექტორი)"])
def test_another_person_at_the_contractor_signature_is_reported(line: str) -> None:
    """AOA-R4-04: an unrelated person, or the customer's director, passed silently. Each layout
    now reports what "დირექტორი: <name>" (the control) reports."""

    def errors(signature: str) -> list[tuple[str, str | None, str | None]]:
        act = _ACT_SIGNED.format(line=signature)
        report = check_aoa(aoa_text=act, contract_text=CONTRACT)
        return [(f.rule_id, f.actual, f.expected) for f in report.errors]

    assert errors(line.format(n="ირაკლი ჯაფარიძე")) == [
        ("AOA.DIRECTOR", "ირაკლი ჯაფარიძე", "დავით მელაძე")
    ]
    copied = errors(line.format(n="ნინო კაპანაძე"))
    assert ("AOA.DIRECTOR", "ნინო კაპანაძე", "დავით მელაძე") in copied
    assert copied == errors("დირექტორი: ნინო კაპანაძე")
    assert errors(line.format(n="დავით მელაძე")) == []


def test_a_slip_before_the_title_is_reported_once() -> None:
    act = _ACT_SIGNED.format(line="დავით მელოძე - დირექტორი")
    found = [
        (f.rule_id, f.actual, f.expected)
        for f in check_aoa(aoa_text=act, contract_text=CONTRACT).errors
    ]
    assert found == [("AOA.DIRECTOR", "დავით მელოძე", "დავით მელაძე")]


# --- AOA-R4-12: an attorney named at the act's signature ---------------------------------------

_BY_PROXY = "წარმოდგენილი მინდობილობით მოქმედი პირის, ლია ხუციშვილის სახით"
_PROXY_CONTRACT = CONTRACT.replace("წარმოდგენილი დირექტორის ნინო კაპანაძის სახით", _BY_PROXY)
_PROXY_ACT = ACT.replace("წარმოდგენილი დირექტორის ნინო კაპანაძის სახით", _BY_PROXY).replace(
    "დირექტორი ნინო კაპანაძე", "{title} {n}"
)


@pytest.mark.parametrize(
    "title", ["მინდობილი პირი:", "მინდობილი პირი", "წარმომადგენელი (მინდობილობით)"]
)
def test_an_attorney_at_the_signature_is_the_party_attorney(title: str) -> None:
    text = f"{_HEAD}„შემკვეთი“\nშპს „ნისლის ველი“\n{title} ლია ხუციშვილი\n"
    assert [p.proxies for p in find_parties(text)] == [["ლია ხუციშვილი"], []]
    act = _PROXY_ACT.format(title=title, n="ლია ხუციშვილი")
    assert check_aoa(aoa_text=act, contract_text=_PROXY_CONTRACT).findings == []


def test_a_slip_in_the_attorney_the_contract_names_is_an_error_with_its_correction() -> None:
    act = _PROXY_ACT.format(title="მინდობილი პირი:", n="ლია ხუციშვილო")
    found = [
        (f.rule_id, f.severity, f.actual, f.expected)
        for f in check_aoa(aoa_text=act, contract_text=_PROXY_CONTRACT).findings
    ]
    assert found == [("AOA.DIRECTOR", "error", "ლია ხუციშვილო", "ლია ხუციშვილი")]


def test_another_attorney_than_the_contract_names_is_the_power_of_attorney_warning() -> None:
    act = _PROXY_ACT.format(title="მინდობილი პირი:", n="ნანა ხუციშვილი")
    found = [
        (f.rule_id, f.severity, f.actual, f.expected)
        for f in check_aoa(aoa_text=act, contract_text=_PROXY_CONTRACT).findings
    ]
    assert found == [("AOA.DIRECTOR", "warning", "ნანა ხუციშვილი", None)]


def test_an_attorney_word_in_prose_names_no_attorney() -> None:
    text = f"{_HEAD}„შემკვეთი“\nშპს „ნისლის ველი“\nმინდობილი პირი ვალდებულია წარმოადგინოს"
    assert [p.proxies for p in find_parties(text)] == [[], []]
