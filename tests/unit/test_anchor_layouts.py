"""The label anchors on layouts and spellings the first template did not use.

Each test holds one way a real document may write a field that the anchors used to misread:
an IBAN in groups or broken over a line, quotes typed as two commas, a signatory after the bank
lines, a two-column signature table as Docling serialises it, a signatory written with an
initial or an honorific, a legal form written out, a grouped identification code, a body clause
that mentions a director, an address followed by the party's code, a project name behind a
label. Every name, code and account here is invented.
"""

from __future__ import annotations

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.compare import compare
from procurement_agent.checks.findings import CheckReport
from procurement_agent.extraction.anchors import find_anchors, find_parties
from procurement_agent.extraction.normalize import (
    QUOTE_CHARS,
    fold_comma_quotes_mapped,
    iban_is_valid,
    names_match,
    normalize_confusables,
    normalize_layout,
    strip_quotes,
)
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion.segmenter import Segment, build

# --- an invented contract and its correct act ------------------------------------------------

CUST, CUST_ID, CUST_DIR, CUST_DIR_GEN = "მთვარის ბაღი", "219876543", "თამთა ლეჟავა", "თამთა ლეჟავას"
CONT, CONT_ID, CONT_DIR, CONT_DIR_GEN = "ქვიშის საათი", "318765432", "გურამ ჩხეტია", "გურამ ჩხეტიას"
IBAN = "GE11TB0000000298765431"
#: shaped like an anonymised account: the check digits do not validate
IBAN_ANON = "GE00TB0000000298765431"
BIC = "NSLBGE22"


def _grouped(code: str) -> str:
    return " ".join(code[i : i + 4] for i in range(0, len(code), 4))


def _bank(account: str = IBAN) -> str:
    return f"ბანკი: სს „ნისლის ბანკი“; ბანკის კოდი: {BIC}; ა/ა: {account}"


def contract(*, account: str = IBAN, quote: tuple[str, str] = ("„", "“")) -> str:
    o, c = quote
    return f"""ხელშეკრულება
ქ. თბილისი                                   1 მარტი, 2026 წელი
შპს {o}{CUST}{c}, ს/კ: {CUST_ID}, იურიდიული მისამართი: ქ. თბილისი, ალვის ქ. N8 (შემდგომში \
{o}შემკვეთი{c}), წარმოდგენილი დირექტორის, {CUST_DIR_GEN} სახით, და
შპს {o}{CONT}{c}, ს/კ: {CONT_ID}, იურიდიული მისამართი: ქ. რუსთავი, მშვიდობის ქ. N4 (შემდგომში \
{o}შემსრულებელი{c}), წარმოდგენილი დირექტორის, {CONT_DIR_GEN} სახით,
1.1. ხელშეკრულების საგანია {o}შემკვეთის{c} პროექტ {o}ტყის პირისთვის{c} (საკადასტრო კოდი: \
01.14.22.333) სარემონტო სამუშაოების შესრულება.
2.1. {o}შემსრულებელი{c} ვალდებულია შეასრულოს სამუშაო 2026 წლის 1 მარტიდან 2026 წლის 1 სექტემბრის \
ჩათვლით.
4.1. ღირებულება შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით.
4.3. ანგარიშსწორება მოხდება {o}შემსრულებლის{c} საბანკო ანგარიშზე: {_bank(account)}
მუხლი 5. მხარეთა ხელმოწერები
{o}შემკვეთი{c}: შპს {o}{CUST}{c}, დირექტორი {CUST_DIR}
{o}შემსრულებელი{c}: შპს {o}{CONT}{c}, დირექტორი {CONT_DIR}
"""


def act(
    *,
    bank_line: str | None = None,
    signatures: str | None = None,
    quote: tuple[str, str] = ("„", "“"),
    cont_dir_gen: str = CONT_DIR_GEN,
    cust_name: str = f"შპს „{CUST}“",
) -> str:
    o, c = quote
    bank_line = _bank() if bank_line is None else bank_line
    if signatures is None:
        signatures = (
            f"{o}შემკვეთი{c}\nშპს {o}{CUST}{c}\nდირექტორი: {CUST_DIR}\n"
            f"{o}შემსრულებელი{c}\nშპს {o}{CONT}{c}\nდირექტორი: {CONT_DIR}"
        )
    cust_name = cust_name.replace("„", o).replace("“", c)
    return f"""მიღება-ჩაბარების აქტი
{cust_name}, ს/კ: {CUST_ID}, იურიდიული მისამართი: ქ. თბილისი, ალვის ქ. N8 (შემდგომში \
{o}შემკვეთი{c}), წარმოდგენილი დირექტორის, {CUST_DIR_GEN} სახით, და
შპს {o}{CONT}{c}, ს/კ: {CONT_ID}, იურიდიული მისამართი: ქ. რუსთავი, მშვიდობის ქ. N4 (შემდგომში \
{o}შემსრულებელი{c}), წარმოდგენილი დირექტორის, {cont_dir_gen} სახით,
{o}შემკვეთის{c} პროექტ {o}ტყის პირისთვის{c} (საკადასტრო კოდი: 01.14.22.333) სარემონტო სამუშაოების \
შესრულება.
2. სამუშაო შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით.
3. საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით.
{bank_line}
4. აქტი გაფორმდა 2026 წლის 30 აპრილი
{signatures}
"""


def _check(act_text: str, contract_text: str | None = None) -> CheckReport:
    return check_aoa(aoa_text=act_text, contract_text=contract_text or contract())


def _substantive(report: CheckReport) -> list[tuple[str, str | None, str | None]]:
    return [
        (f.rule_id, f.actual, f.expected)
        for f in report.findings
        if f.severity in ("error", "warning")
    ]


def _errors(report: CheckReport, rule_id: str) -> list[tuple[str | None, str | None]]:
    return [
        (f.actual, f.expected)
        for f in report.findings
        if f.rule_id == rule_id and f.severity == "error"
    ]


def _values(text: str, ft: FactType) -> list[str]:
    return [h.value for h in find_anchors(text) if h.type == ft]


def test_the_invented_pair_is_clean() -> None:
    assert iban_is_valid(IBAN)
    assert not iban_is_valid(IBAN_ANON)
    assert _substantive(_check(act())) == []


# --- IBAN: its end is where the country's length says (AOA-01, AOA-21) ------------------------


def test_a_grouped_iban_with_a_failing_checksum_is_still_an_account() -> None:
    typo = IBAN[:-1] + "2"
    assert _values(f"ა/ა: {_grouped(typo)}", FactType.bank_account) == [typo]


def test_a_grouped_iban_ends_at_the_country_length_not_at_the_next_word() -> None:
    assert _values(f"ა/ა: {_grouped(IBAN)} BANK", FactType.bank_account) == [IBAN]
    assert _values(f"ა/ა: {_grouped(IBAN_ANON)} ABCD", FactType.bank_account) == [IBAN_ANON]


def test_a_grouped_iban_typo_is_a_wrong_account_with_its_correction() -> None:
    typo = IBAN[:-1] + "2"
    report = _check(act(bank_line=_bank(_grouped(typo))))
    assert _errors(report, "AOA.BANK") == [(typo, IBAN)]
    # the comment goes on the account as the act prints it, not on a compact form it lacks
    quotes = {f.quote for f in report.findings if f.rule_id in ("AOA.BANK", "AOA.IBAN_CHECKSUM")}
    assert quotes == {_grouped(typo)}


def test_an_account_broken_over_a_line_is_quoted_as_written() -> None:
    typo = IBAN[:-1] + "2"
    head, tail = typo[:16], typo[16:]
    report = _check(act(bank_line=_bank(f"{head}\n{tail}")))
    [bank] = [f for f in report.findings if f.rule_id == "AOA.BANK"]
    assert (bank.actual, bank.expected) == (typo, IBAN)
    assert bank.quote is not None
    assert bank.quote.replace(" ", "").replace("\n", "") == typo


def test_grouped_anonymised_accounts_that_agree_are_no_finding() -> None:
    report = _check(act(bank_line=_bank(_grouped(IBAN_ANON))), contract(account=IBAN_ANON))
    assert _substantive(report) == []


def test_an_iban_broken_over_a_line_is_read_whole() -> None:
    head, tail = IBAN[:16], IBAN[16:]
    assert _values(f"ა/ა: {head}\n{tail}\nდირექტორი", FactType.bank_account) == [IBAN]
    assert _substantive(_check(act(bank_line=_bank(f"{head}\n{tail}")))) == []


def test_a_short_iban_does_not_swallow_the_next_clause_number() -> None:
    short = IBAN[:-1]  # a digit dropped: 21 characters
    got = _values(f"ა/ა: {short}\n4. აქტი გაფორმდა", FactType.bank_account)
    assert got == [short]


# --- quotes typed as two commas, or as a low single quote (AOA-03) -----------------------------


def test_every_quote_style_is_a_quote() -> None:
    assert "‚" in QUOTE_CHARS
    assert strip_quotes("‚ტყის პირი‘") == "ტყის პირი"
    # two commas before a letter open a quote; two commas before a space are a typo
    # two commas before a letter open a quote; before a space they are a typo
    assert normalize_confusables("X ,,ტყის პირი“") == "X „ტყის პირი“"
    assert normalize_confusables("ფასი,, რომელიც") == "ფასი,, რომელიც"
    # the letters as written keep them, and an offset into the folded text maps back
    assert normalize_layout("X ,,ტყის პირი“") == "X ,,ტყის პირი“"
    folded, back = fold_comma_quotes_mapped("X ,,ტყის“ და ,,ზღვა“")
    assert folded == "X „ტყის“ და „ზღვა“"
    assert len(back) == len(folded) + 1
    assert "X ,,ტყის“ და ,,ზღვა“"[back[2] : back[len(folded)]] == ",,ტყის“ და ,,ზღვა“"


def test_a_finding_in_a_two_comma_act_quotes_the_commas_as_written() -> None:
    swapped = (
        act(quote=(",,", "“"))
        .replace("(შემდგომში ,,შემკვეთი“)", "(შემდგომში ,,TMP“)")
        .replace("(შემდგომში ,,შემსრულებელი“)", "(შემდგომში ,,შემკვეთი“)")
        .replace("(შემდგომში ,,TMP“)", "(შემდგომში ,,შემსრულებელი“)")
    )
    roles = [f for f in _check(swapped).findings if f.rule_id == "AOA.PARTY_ROLE"]
    assert roles
    for f in roles:
        assert f.quote is not None
        assert f.quote in swapped, f.quote


def test_parties_quoted_with_two_commas_are_read() -> None:
    parties = find_parties(act(quote=(",,", "“")))
    assert [(p.name, p.id_code, p.role) for p in parties] == [
        (f"შპს {CUST}", CUST_ID, "შემკვეთი"),
        (f"შპს {CONT}", CONT_ID, "შემსრულებელი"),
    ]


def test_an_act_in_another_quote_style_is_clean_and_its_slips_are_found() -> None:
    for quote in ((",,", "“"), ("‚", "‘")):
        assert _substantive(_check(act(quote=quote))) == [], quote
        slip = _check(act(quote=quote, cont_dir_gen="გურამ ჩხეტიძის"))
        assert ("გურამ ჩხეტიძის", "გურამ ჩხეტიას") in _errors(slip, "AOA.DIRECTOR"), quote


# --- a signatory after the bank lines, a bank as a party (AOA-06) ------------------------------

_REQUISITES_SIGNATURE = (
    "„შემკვეთი“\nშპს „{cust}“\nდირექტორი: {cust_dir}\n"
    "„შემსრულებელი“\nშპს „{cont}“\nს/კ: {cont_id}\n" + _bank() + "\nდირექტორი: {cont_dir}"
)


def test_a_director_listed_after_the_bank_lines_belongs_to_the_party() -> None:
    sig = _REQUISITES_SIGNATURE.format(
        cust=CUST, cust_dir=CUST_DIR, cont=CONT, cont_id=CONT_ID, cont_dir="გურამ ჩხეტიძე"
    )
    [_, contractor] = find_parties(act(signatures=sig))
    assert any(names_match(d, "გურამ ჩხეტიძე") for d in contractor.directors)
    report = _check(act(signatures=sig))
    assert _errors(report, "AOA.DIRECTOR") == [("გურამ ჩხეტიძე", "გურამ ჩხეტია")]


def test_a_correct_director_after_the_bank_lines_is_clean() -> None:
    sig = _REQUISITES_SIGNATURE.format(
        cust=CUST, cust_dir=CUST_DIR, cont=CONT, cont_id=CONT_ID, cont_dir=CONT_DIR
    )
    assert _substantive(_check(act(signatures=sig))) == []


def test_a_bank_that_is_a_party_keeps_its_signatory() -> None:
    bank_party = "სს „ფერადი ხიდი ბანკი“"
    pair_contract = contract().replace(f"შპს „{CUST}“", bank_party)
    good = act(cust_name=bank_party).replace(f"შპს „{CUST}“", bank_party)
    assert _substantive(_check(good, pair_contract)) == []
    slip = good.replace(f"დირექტორი: {CUST_DIR}", "დირექტორი: თამთა ლეჟაძე")
    assert _errors(_check(slip, pair_contract), "AOA.DIRECTOR") == [
        ("თამთა ლეჟაძე", "თამთა ლეჟავა")
    ]


# --- a two-column signature table, serialised "row, header = cell" (AOA-07) --------------------


def _grid_roles_first(cont_dir: str = CONT_DIR) -> str:
    """Docling's serialisation of a four-row, two-column table whose first row holds the roles:
    the corner cell is dropped, and each row reads "<left cell>, <header> = <right cell>"."""
    return (
        f"შპს „{CUST}“, „შემსრულებელი“ = შპს „{CONT}“. "
        f"დირექტორი: {CUST_DIR}, „შემსრულებელი“ = დირექტორი: {cont_dir}. "
        "ხელმოწერა: ________, „შემსრულებელი“ = ხელმოწერა: ________"
    )


def _grid_names_first(cust_dir: str = CUST_DIR) -> str:
    """The same table with the company names as its first row: the customer's name is the
    dropped corner cell."""
    return (
        f"დირექტორი: {cust_dir}, შპს „{CONT}“ = დირექტორი: {CONT_DIR}. "
        f"ხელმოწერა: ________, შპს „{CONT}“ = ხელმოწერა: ________"
    )


def test_each_column_of_a_signature_grid_keeps_its_own_director() -> None:
    customer, contractor = find_parties(act(signatures=_grid_roles_first()))
    assert [d for d in customer.directors if names_match(d, CONT_DIR)] == []
    assert [d for d in contractor.directors if names_match(d, CUST_DIR)] == []
    customer, contractor = find_parties(act(signatures=_grid_names_first()))
    assert [d for d in contractor.directors if names_match(d, CUST_DIR)] == []


def test_a_correct_act_with_a_signature_grid_is_clean() -> None:
    assert _substantive(_check(act(signatures=_grid_roles_first()))) == []
    assert _substantive(_check(act(signatures=_grid_names_first()))) == []


def test_a_slip_in_a_signature_grid_is_reported_on_its_own_column() -> None:
    report = _check(act(signatures=_grid_roles_first("გურამ ჩხეტიძე")))
    assert _errors(report, "AOA.DIRECTOR") == [("გურამ ჩხეტიძე", "გურამ ჩხეტია")]
    report = _check(act(signatures=_grid_names_first("თამთა ლეჟაძე")))
    assert _errors(report, "AOA.DIRECTOR") == [("თამთა ლეჟაძე", "თამთა ლეჟავა")]


# --- a signatory written with an initial (AOA-22) -----------------------------------------------


def test_an_initial_matches_the_full_first_name_it_starts() -> None:
    assert names_match("გ. ჩხეტია", "გურამ ჩხეტიას")
    assert not names_match("ლ. ჩხეტია", "გურამ ჩხეტია")
    assert not names_match("გ. ჩხეტიძე", "გურამ ჩხეტია")


def test_a_signatory_with_an_initial_is_read_and_compared() -> None:
    def signed(name: str) -> str:
        return act().replace(f"დირექტორი: {CONT_DIR}", f"დირექტორი: {name}")

    assert _substantive(_check(signed("გ. ჩხეტია"))) == []
    assert _errors(_check(signed("გ. ჩხეტიძე")), "AOA.DIRECTOR") == [("გ. ჩხეტიძე", "გ. ჩხეტია")]


# --- document comparison: the same party written another way (COMPARE-19, -20, -29) ----------

_PREAMBLE = (
    f"შპს „{CONT}“, საიდენტიფიკაციო კოდი: {CONT_ID} (შემდგომში „შემსრულებელი“), "
    f"წარმოდგენილი დირექტორის {CONT_DIR_GEN} სახით."
)


def _segs(texts: list[str]) -> list[Segment]:
    return build([(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(texts)])


def _compared(right: str) -> list[tuple[str, str | None, str | None]]:
    report = compare(
        _segs([_PREAMBLE, "მომსახურება შესრულდა."]),
        _segs([right, "მომსახურება შესრულდა."]),
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    return [
        (f.rule_id, f.expected, f.actual)
        for f in report.findings
        if f.severity != "info" and f.rule_id != "CMP.FORMATTING"
    ]


def test_an_honorific_before_the_director_is_not_part_of_the_name() -> None:
    for honorific in ("ბატონ", "ბატონი", "ქალბატონ", "ბ-ნ", "ქ-ნი"):
        right = _PREAMBLE.replace("დირექტორის ", f"დირექტორის {honorific} ")
        [party] = find_parties(right)
        assert party.directors == [CONT_DIR_GEN], honorific
        assert _compared(right) == [], honorific


def test_a_legal_form_written_out_is_the_same_party() -> None:
    for written, short in (
        ("შეზღუდული პასუხისმგებლობის საზოგადოება", "შპს"),
        ("სააქციო საზოგადოება", "სს"),
    ):
        text = _PREAMBLE.replace("შპს", written)
        [party] = find_parties(text)
        assert party.name == f"{short} {CONT}"
        assert _values(text, FactType.party_name) == [f"{short} {CONT}"]
    right = _PREAMBLE.replace("შპს", "შეზღუდული პასუხისმგებლობის საზოგადოება")
    assert _compared(right) == []


def test_an_identification_code_printed_in_groups_is_the_same_code() -> None:
    grouped = f"{CONT_ID[:3]} {CONT_ID[3:6]} {CONT_ID[6:]}"
    right = _PREAMBLE.replace(CONT_ID, grouped)
    assert _values(right, FactType.party_id_code) == [CONT_ID]
    [party] = find_parties(right)
    assert party.id_code == CONT_ID
    assert _compared(right) == []


# --- extraction: no invented signatory, the address alone, the project behind a label ---------

_BODY_CLAUSES = (
    "3.2. სამუშაოს კონტროლს ახორციელებს „შემკვეთის“ დირექტორი ან მის მიერ უფლებამოსილი პირი.",
    "3.3. ცვლილება ძალაშია „შემსრულებლის“ დირექტორის ან მისი უფლებამოსილი პირის ხელმოწერით.",
    "3.4. „შემსრულებლის“ დირექტორი პასუხს აგებს სამუშაოს ხარისხზე.",
    "3.5. მხარეთა წარმომადგენლები მოქმედებენ მინდობილობით განსაზღვრული უფლებამოსილების ფარგლებში.",
    "3.6. „შემსრულებლის“ დირექტორი პასუხს აგებს.",
    # wordings of other hands: a predicate and a noun with a surname's ending, and a colon
    # before a noun phrase
    "3.7. „შემსრულებლის“ დირექტორი უფლებამოსილია ხელი მოაწეროს დოკუმენტს.",
    "3.8. „შემკვეთის“ დირექტორი: ხელმოწერის უფლება აქვს მხოლოდ მას.",
    "3.9. „შემკვეთის“ დირექტორი პირადად ხელმძღვანელობს სამუშაოს.",
)


def _directors(text: str) -> list[str]:
    return _values(text, FactType.party_director)


def test_a_body_clause_that_mentions_a_director_names_no_one() -> None:
    base = contract()
    expected = _directors(base)
    assert expected == [CUST_DIR_GEN, CONT_DIR_GEN]
    for clause in _BODY_CLAUSES:
        # after the preamble, and after the bank lines, whose window runs over the body
        for text in (
            base.replace("2.1.", f"{clause}\n2.1."),
            base.replace("მუხლი 5.", f"{clause}\nმუხლი 5."),
        ):
            assert _directors(text) == expected, clause


def test_a_director_written_once_without_a_colon_is_still_read() -> None:
    text = (
        "შპს „ირმის ნახტომი“, ს/კ: 219876543\n"
        "4.1. ანგარიშსწორება: ბანკი: სს „ნისლის ბანკი“, ა/ა: " + IBAN + "\n"
        "დირექტორი ოთარ მესხი\n"
    )
    assert _directors(text) == ["ოთარ მესხი"]


def test_an_address_ends_where_the_code_or_the_account_begins() -> None:
    address = "ქ. რუსთავი, მშვიდობის ქ. N4"
    for text in (
        f"შპს „{CONT}“, იურიდიული მისამართი: {address}, ს/კ: {CONT_ID} (შემდგომში „შემსრულებელი“)",
        f"შპს „{CONT}“, იურიდიული მისამართი: {address}, საიდენტიფიკაციო კოდი: {CONT_ID}",
        f"შპს „{CONT}“\nმისამართი: {address}\nს/კ {CONT_ID}\nა/ა {IBAN}",
        f"შპს „{CONT}“\nმისამართი: {address}\n{CONT_ID}\n{IBAN}",
        f"შპს „{CONT}“, მისამართი: {address} ა/ა {IBAN}",
    ):
        assert set(_values(text, FactType.party_address)) == {address}, text


def test_a_project_name_is_read_behind_a_label_or_with_a_case_ending_after_its_quote() -> None:
    for text in (
        "პროექტის დასახელება: „ტყის პირი“, საკადასტრო კოდი: 01.14.22.333.",
        "პროექტის სახელწოდება „ტყის პირი“.",
        "სამუშაოები „ტყის პირი“-ს პროექტის ფარგლებში.",
    ):
        assert _values(text, FactType.project_name) == ["ტყის პირი"], text
