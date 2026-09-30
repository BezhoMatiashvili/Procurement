"""Defects the second red-team round found in reading the parties and their requisites.

Each test names its defect (``samples/redteam/<module>/CONFIRMED_R2.md``): AOA-R2-01 (identification
code labels), AOA-R2-02 (a requisites grid), AOA-R2-03 (an account with its currency after it),
AOA-R2-04 (a party written without quotes or with its legal form after the name), AOA-R2-06
(signatories under other titles than director) and EXTRACTION-R2-11 (side-by-side addresses).
Synthetic text only: the pair is ``test_aoa_check``'s, every name, code and account invented.
"""

from __future__ import annotations

import pytest
from docling_core.types.doc.document import DoclingDocument, RichTableCell, TableCell, TableData
from docling_core.types.doc.labels import DocItemLabel, GroupLabel

from procurement_agent.checks.aoa import _labelled_codes, check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.extraction.anchors import _ibans, find_anchors, find_parties
from procurement_agent.extraction.pipeline import chunks_text
from procurement_agent.extraction.rules import find_bank_accounts, find_id_codes
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion.chunker import chunk_docling_document
from tests.unit.test_aoa_check import ACT, CONTRACT


def _check(act: str = ACT, contract: str | None = CONTRACT) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract)


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _substantive(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.severity in ("error", "warning")]


def _skipped(report: CheckReport, rule_id: str) -> list[str]:
    return [s.reason_en for s in report.skipped if s.rule_id == rule_id]


CONTRACTOR_CODE = "საიდენტიფიკაციო კოდი: 987654321"


# --- AOA-R2-01: one label pattern for every reader of an identification code ---------------------

LABELS = [
    "ს.კ. ",
    "ს.კ ",
    "ს/კ N ",
    "ს/კ № ",
    "ს/ნ ",
    "ID: ",
    "ID code: ",
    "საიდ. კოდი: ",
    "საიდ. № ",
    "საიდენტიფიკაციო № ",
    "საიდენტიფიკაციო კოდით ",
    "საიდენტიფიკაციო ნომრით ",
    "პირადი ნომერი: ",
]


class TestIdLabels:
    @pytest.mark.parametrize("label", LABELS)
    def test_every_reader_reads_the_code_behind_the_label(self, label: str) -> None:
        text = f"შპს „ბეტა სერვისი“, {label}987654321, მისამართი: ქ. ბათუმი"
        assert [p.id_code for p in find_parties(text)] == ["987654321"]
        assert [h.value for h in find_id_codes(text)] == ["987654321"]
        assert _labelled_codes(text) == {"987654321"}

    @pytest.mark.parametrize(
        "text",
        [
            "საიდან 987654321 ლარი",  # "from where", no label
            "PAID 987654321",
            "VALID 987654321",
            "(ს/კ: 01.02.03.456)",  # a plot code behind the same abbreviation
            "ბს/კ 987654321",  # the abbreviation is a word of its own
        ],
    )
    def test_a_near_miss_is_no_code(self, text: str) -> None:
        assert find_id_codes(text) == []
        assert _labelled_codes(text) == set()

    @pytest.mark.parametrize("label", ["ს.კ. ", "ID: ", "ს/კ N ", "საიდენტიფიკაციო კოდით "])
    def test_a_slip_in_a_code_so_labelled_is_an_error_with_its_correction(self, label: str) -> None:
        act = ACT.replace(CONTRACTOR_CODE, f"{label}987654312")
        [finding] = _of(_check(act), "AOA.ID_CODES")
        assert (finding.severity, finding.actual, finding.expected) == (
            "error",
            "987654312",
            "987654321",
        )

    @pytest.mark.parametrize("label", ["ს.კ. ", "ID: ", "ს/კ N ", "საიდენტიფიკაციო კოდით "])
    def test_the_contract_so_labelled_leaves_a_correct_act_clean(self, label: str) -> None:
        contract = CONTRACT.replace(CONTRACTOR_CODE, f"{label}987654321")
        assert _substantive(_check(contract=contract)) == []

    def test_no_correction_towards_the_other_party_s_code(self) -> None:
        """The contract states no code for the contractor: the act's is not compared, and never
        corrected to the customer's."""
        contract = CONTRACT.replace(f"{CONTRACTOR_CODE}, ", "")
        report = _check(contract=contract)
        assert _of(report, "AOA.ID_CODES") == []
        assert any("987654321" in r for r in _skipped(report, "AOA.ID_CODES"))

    def test_a_party_not_read_still_has_its_code_compared(self) -> None:
        """The contractor written without quotes is no party to the reader, so its code stands in
        the customer's window; the slip in it is still an error."""
        act = ACT.replace("შპს „ბეტა სერვისი“", "შპს ბეტა სერვისი").replace(
            "987654321", "987654312"
        )
        [finding] = _of(_check(act), "AOA.ID_CODES")
        assert (finding.actual, finding.expected) == ("987654312", "987654321")


# --- AOA-R2-03: an account with its currency written after it ------------------------------------


def _valid_iban(bank: str, account16: str) -> str:
    """A Georgian-shaped IBAN whose ISO 13616 check digits validate."""
    body = f"{bank}{account16}"
    number = "".join(str(int(c, 36)) for c in f"{body}GE00")
    return f"GE{98 - int(number) % 97:02d}{body}"


_IBAN = _valid_iban("NB", "0000000101904917")
_ACT_V = ACT.replace("GE29NB0000000101904917", _IBAN)
_CONTRACT_V = CONTRACT.replace("GE29NB0000000101904917", _IBAN)


class TestIbanCurrencySuffix:
    @pytest.mark.parametrize("suffix", ["GEL", "USD", " GEL"])
    def test_the_suffix_is_no_part_of_the_account(self, suffix: str) -> None:
        text = f"ა/ა: {_IBAN}{suffix}"
        assert [iban for iban, _, _ in _ibans(text)] == [_IBAN]
        assert [h.normalized for h in find_bank_accounts(text)] == [_IBAN]

    def test_the_act_writing_the_suffix_is_the_contract_s_account(self) -> None:
        report = _check(_ACT_V.replace(_IBAN, f"{_IBAN}GEL"), _CONTRACT_V)
        assert _of(report, "AOA.BANK") == []
        assert _of(report, "AOA.IBAN_CHECKSUM") == []

    def test_the_contract_writing_the_suffix_is_the_act_s_account(self) -> None:
        report = _check(_ACT_V, _CONTRACT_V.replace(_IBAN, f"{_IBAN}GEL"))
        assert _of(report, "AOA.BANK") == []

    def test_a_slip_in_a_suffixed_account_is_corrected_to_the_bare_account(self) -> None:
        slip = _IBAN[:-2] + _IBAN[-1] + _IBAN[-2]
        [finding] = _of(_check(_ACT_V.replace(_IBAN, f"{slip}GEL"), _CONTRACT_V), "AOA.BANK")
        assert finding.expected == _IBAN

    def test_another_currency_sub_account_is_a_warning(self) -> None:
        """Both documents name the sub-account and they differ: not the IBAN wrong, but worth
        saying."""
        act = _ACT_V.replace(_IBAN, f"{_IBAN}USD")
        [finding] = _of(_check(act, _CONTRACT_V.replace(_IBAN, f"{_IBAN}GEL")), "AOA.BANK")
        assert (finding.severity, finding.actual, finding.expected) == ("warning", "USD", "GEL")

    @pytest.mark.parametrize("tail", ["0GEL", "GELA"])
    def test_a_longer_account_is_not_cut_to_length(self, tail: str) -> None:
        """A digit too many, or letters that are no currency code, stay part of the account."""
        text = f"ა/ა: {_IBAN}{tail}"
        assert [iban for iban, _, _ in _ibans(text)] == [f"{_IBAN}{tail}"]
        assert _of(_check(_ACT_V.replace(_IBAN, f"{_IBAN}{tail}"), _CONTRACT_V), "AOA.BANK")


# --- AOA-R2-04: a party without quotes, or with its legal form after the name --------------------


class TestPartyWrittenOtherwise:
    def test_the_legal_form_after_a_quoted_name_is_a_party(self) -> None:
        text = "„ბეტა სერვისი“ შპს, ს/კ: 987654321, დირექტორი: დავით მელაძე"
        [party] = find_parties(text)
        assert (party.name, party.id_code, party.directors) == (
            "შპს ბეტა სერვისი",
            "987654321",
            ["დავით მელაძე"],
        )

    def test_a_role_above_a_party_is_no_party(self) -> None:
        text = "„შემსრულებელი“ შპს „ბეტა სერვისი“, ს/კ: 987654321"
        assert [p.name for p in find_parties(text)] == ["შპს ბეტა სერვისი"]

    @pytest.mark.parametrize("written", ["შპს ბეტა სერვისი", "„ბეტა სერვისი“ შპს"])
    def test_the_contractor_s_own_director_is_no_error(self, written: str) -> None:
        act = ACT.replace("შპს „ბეტა სერვისი“", written)
        assert _of(_check(act), "AOA.DIRECTOR") == []

    def test_a_slip_in_an_unread_party_s_director_is_not_corrected_to_the_other_party(
        self,
    ) -> None:
        act = ACT.replace("შპს „ბეტა სერვისი“", "შპს ბეტა სერვისი").replace(
            "დავით მელაძე", "დავით მელაძი"
        )
        report = _check(act)
        assert all(f.expected != "ნინო კაპანაძე" for f in _of(report, "AOA.DIRECTOR"))
        assert _skipped(report, "AOA.DIRECTOR")

    def test_a_slip_in_a_postposed_party_s_director_is_corrected_to_its_own(self) -> None:
        act = ACT.replace("შპს „ბეტა სერვისი“", "„ბეტა სერვისი“ შპს").replace(
            "დავით მელაძე", "დავით მელაძი"
        )
        [finding] = _of(_check(act), "AOA.DIRECTOR")
        assert (finding.actual, finding.expected) == ("დავით მელაძი", "დავით მელაძე")


# --- AOA-R2-06: signatories under other titles ---------------------------------------------------

_PUBLIC_CONTRACT = CONTRACT.replace("შპს „ალფა ჯგუფი“", "სსიპ „ალფა ჯგუფი“").replace(
    "დირექტორის ნინო კაპანაძის", "თავმჯდომარის ნინო კაპანაძის"
)
_PUBLIC_ACT = (
    ACT.replace("შპს „ალფა ჯგუფი“", "სსიპ „ალფა ჯგუფი“")
    .replace("დირექტორის ნინო კაპანაძის", "თავმჯდომარის ნინო კაპანაძის")
    .replace("დირექტორი ნინო კაპანაძე", "თავმჯდომარე ნინო კაპანაძე")
)


class TestSignatoryTitles:
    @pytest.mark.parametrize(
        "title", ["თავმჯდომარე", "ხელმძღვანელი", "რექტორი", "აღმასრულებელი დირექტორი"]
    )
    def test_the_head_under_another_title_is_read(self, title: str) -> None:
        text = f"სსიპ „ალფა ჯგუფი“, ს/კ: 123456789\n{title}: მარინე ნიმუშაძე"
        assert [p.directors for p in find_parties(text)] == [["მარინე ნიმუშაძე"]]

    @pytest.mark.parametrize(
        "clause",
        [
            "ხელმძღვანელი პირი ვალდებულია შეასრულოს ხელშეკრულება.",
            "ხელმძღვანელი ვალდებულია უზრუნველყოს შესრულება.",
            "მმართველი ორგანოს გადაწყვეტილებით",
            "პროექტის ხელმძღვანელი გიორგი ნიმუშაძე ვალდებულია",
            "პრორექტორი გიორგი ნიმუშაძე",
            "ვიცე-პრეზიდენტი გიორგი ნიმუშაძე",
            "თავმჯდომარე უფლებამოსილია ხელი მოაწეროს.",
        ],
    )
    def test_a_body_clause_names_no_signatory(self, clause: str) -> None:
        text = f"შპს „ალფა ჯგუფი“, ს/კ: 123456789. {clause}"
        [party] = find_parties(text)
        assert party.directors == []

    def test_a_chairman_s_slip_is_an_error(self) -> None:
        act = _PUBLIC_ACT.replace("ნინო კაპანაძ", "ნინო კაპინაძ")
        found = {(f.actual, f.expected) for f in _of(_check(act, _PUBLIC_CONTRACT), "AOA.DIRECTOR")}
        assert found == {
            ("ნინო კაპინაძის", "ნინო კაპანაძის"),
            ("ნინო კაპინაძე", "ნინო კაპანაძე"),
        }

    def test_a_correct_chairman_is_clean(self) -> None:
        assert _of(_check(_PUBLIC_ACT, _PUBLIC_CONTRACT), "AOA.DIRECTOR") == []

    def test_a_deputy_signing_in_the_head_s_place_is_a_note_not_an_error(self) -> None:
        act = _PUBLIC_ACT.replace(
            "თავმჯდომარე ნინო კაპანაძე", "თავმჯდომარის მოადგილე: თამარ ნიმუშაძე"
        )
        report = _check(act, _PUBLIC_CONTRACT)
        assert _of(report, "AOA.DIRECTOR") == []
        assert any("deputy" in r for r in _skipped(report, "AOA.DIRECTOR"))


# --- AOA-R2-02 / EXTRACTION-R2-11: a requisites grid, one column per party -----------------------

GRID = [
    ["„შემკვეთი“", "„შემსრულებელი“"],
    ["შპს „ალფა ჯგუფი“", "შპს „ბეტა სერვისი“"],
    ["მისამართი: ქ. თბილისი, რუსთაველის 12", "მისამართი: ქ. ბათუმი, ჭავჭავაძის 5"],
    ["ს/კ: 123456789", "ს/კ: 987654321"],
    ["ა/ა: GE11CC0000000123456789", "ა/ა: GE29NB0000000101904917"],
    ["დირექტორი: ნინო კაპანაძე", "დირექტორი: დავით მელაძე"],
]
#: the act's body above its closing grid, the signature lines left to the grid
_ACT_BODY = ACT[: ACT.index("„შემკვეთი“\nშპს")].splitlines()
#: the contract with both parties' requisites, each in its own block
_BOTH = (
    CONTRACT + "5. მხარეთა რეკვიზიტები\n„შემკვეთი“: შპს „ალფა ჯგუფი“, ს/კ: 123456789, ბანკის კოდი: "
    "CCCCGE22, ა/ა: GE11CC0000000123456789\n„შემსრულებელი“: შპს „ბეტა სერვისი“, ს/კ: 987654321, "
    "ბანკის კოდი: AAAAGE22, ა/ა: GE29NB0000000101904917\n"
)


def _document(lines: list[str], rows: list[list[str]], *, rich: bool = True) -> DoclingDocument:
    """Paragraphs, then a table as Docling's Word reader builds one: the first row a header, and
    each cell a group of paragraphs (``rich``) or plain text."""
    doc = DoclingDocument(name="synthetic")
    for line in lines:
        doc.add_text(label=DocItemLabel.TEXT, text=line)
    table = doc.add_table(data=TableData(num_rows=len(rows), num_cols=len(rows[0])))
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            where = {
                "start_row_offset_idx": r,
                "end_row_offset_idx": r + 1,
                "start_col_offset_idx": c,
                "end_col_offset_idx": c + 1,
                "column_header": r == 0,
            }
            if rich:
                group = doc.add_group(
                    label=GroupLabel.UNSPECIFIED, name=f"rich_cell_group_1_{c}_{r}", parent=table
                )
                for line in text.split("\n"):
                    doc.add_text(label=DocItemLabel.TEXT, text=line, parent=group)
                cell: TableCell = RichTableCell(text=text, ref=group.get_ref(), **where)
            else:
                cell = TableCell(text=text, **where)
            doc.add_table_cell(table_item=table, cell=cell)
    return doc


def _text(lines: list[str], rows: list[list[str]], *, rich: bool = True) -> str:
    return chunks_text(chunk_docling_document(_document(lines, rows, rich=rich), doc_id="t"))


def _grid_act(rows: list[list[str]]) -> str:
    return _text(_ACT_BODY, rows)


def _addresses(text: str) -> list[str]:
    return sorted({h.value for h in find_anchors(text) if h.type == FactType.party_address})


class TestRequisitesGrid:
    def test_a_correct_act_is_clean(self) -> None:
        assert _substantive(_check(_grid_act(GRID), _BOTH)) == []

    def test_a_code_slip_in_the_grid_is_the_one_finding(self) -> None:
        rows = [r[:] for r in GRID]
        rows[3][1] = "ს/კ: 987654312"
        [finding] = _substantive(_check(_grid_act(rows), _BOTH))
        assert (finding.rule_id, finding.actual, finding.expected) == (
            "AOA.ID_CODES",
            "987654312",
            "987654321",
        )

    def test_an_account_slip_in_the_grid_is_the_one_finding(self) -> None:
        rows = [r[:] for r in GRID]
        rows[4][1] = "ა/ა: GE29NB0000000101904971"
        [finding] = _substantive(_check(_grid_act(rows), _BOTH))
        assert (finding.rule_id, finding.expected) == ("AOA.BANK", "GE29NB0000000101904917")

    @pytest.mark.parametrize("rich", [True, False])
    def test_each_column_is_read_as_its_party_s_block(self, rich: bool) -> None:
        parties = find_parties(_text([], GRID, rich=rich))
        assert [(p.name, p.role, p.id_code, p.accounts, p.directors) for p in parties] == [
            (
                "შპს ალფა ჯგუფი",
                "შემკვეთი",
                "123456789",
                ["GE11CC0000000123456789"],
                ["ნინო კაპანაძე"],
            ),
            (
                "შპს ბეტა სერვისი",
                "შემსრულებელი",
                "987654321",
                ["GE29NB0000000101904917"],
                ["დავით მელაძე"],
            ),
        ]

    @pytest.mark.parametrize("below", ["დირექტორი: ნინო კაპანაძე", "ნინო კაპანაძე"])
    def test_an_address_ends_at_its_cell(self, below: str) -> None:
        """The row under the address a director's or a bare name: neither runs into it."""
        rows = [GRID[0], GRID[1], GRID[2], [below, below.replace("ნინო კაპანაძე", "დავით მელაძე")]]
        assert _addresses(_text([], rows)) == [
            "ქ. ბათუმი, ჭავჭავაძის 5",
            "ქ. თბილისი, რუსთაველის 12",
        ]

    def test_a_label_column_labels_each_party_s_cell(self) -> None:
        rows = [
            ["", "„შემკვეთი“", "„შემსრულებელი“"],
            ["დასახელება", "შპს „ალფა ჯგუფი“", "შპს „ბეტა სერვისი“"],
            ["ს/კ", "123456789", "987654321"],
            ["მისამართი", "ქ. თბილისი, რუსთაველის 12", "ქ. ბათუმი, ჭავჭავაძის 5"],
        ]
        text = _text([], rows)
        assert [(p.name, p.id_code) for p in find_parties(text)] == [
            ("შპს ალფა ჯგუფი", "123456789"),
            ("შპს ბეტა სერვისი", "987654321"),
        ]
        assert _addresses(text) == ["ქ. ბათუმი, ჭავჭავაძის 5", "ქ. თბილისი, რუსთაველის 12"]

    def test_a_table_with_a_party_per_row_is_left_as_it_was(self) -> None:
        """One party per row: a role and a name in one row are one party, not two columns."""
        rows = [
            ["მხარე", "დასახელება", "ს/კ"],
            ["„შემკვეთი“", "შპს „ალფა ჯგუფი“", "123456789"],
            ["„შემსრულებელი“", "შპს „ბეტა სერვისი“", "987654321"],
        ]
        assert " = " in _text([], rows)

    def test_a_price_table_is_left_as_it_was(self) -> None:
        rows = [["დასახელება", "ფასი"], ["კაბელი", "100"], ["სადენი", "200"]]
        assert "კაბელი, ფასი = 100" in _text([], rows)


class TestSideBySideAddressesAsTriplets:
    """EXTRACTION-R2-11: the row as Docling writes it, "<left>, <header> = <right>" (a table
    whose columns the chunker cannot tell apart, or text from elsewhere)."""

    def test_the_left_address_ends_at_the_cell_boundary(self) -> None:
        text = (
            "მისამართი: ქ. თბილისი, ნიმუშის ქ. N1, „შემსრულებელი“ = მისამართი: ქ. ქუთაისი, "
            "ტყის ქ. N2. ს/კ: 206334455"
        )
        assert _addresses(text) == ["ქ. თბილისი, ნიმუშის ქ. N1", "ქ. ქუთაისი, ტყის ქ. N2"]

    def test_an_address_with_commas_is_read_whole(self) -> None:
        text = "მისამართი: ქ. თბილისი, ნიმუშის ქ. N5, ბინა 3; ს/კ: 206334455"
        assert _addresses(text) == ["ქ. თბილისი, ნიმუშის ქ. N5, ბინა 3"]
