"""The act's parties against the contract's (client rules 10.1 and 10.2).

Which contract party each act party is, whether it stands on the same side - the customer who
pays or the contractor who is paid - whether its identification code is its own, and whether
the contract's customer and contractor are in the act at all. Each test plants one defect into
the known-good pair from ``test_aoa_check``, on the client's side as well as the contractor's.
Synthetic text only.
"""

from __future__ import annotations

import pytest

from procurement_agent.checks.aoa import AoaChecker, DocumentView, check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.extraction.anchors import Party
from procurement_agent.registry.snapshot import RegistryEntry
from tests.unit.test_aoa_check import ACT, CONTRACT

_CLIENT_ROLE = "(შემდგომში „შემკვეთი“)"
_CONTRACTOR_ROLE = "(შემდგომში „შემსრულებელი“)"
#: the act's requisites block and signature block for each party
_CLIENT_BLOCK = (
    "შპს „ალფა ჯგუფი“. საიდენტიფიკაციო კოდი: 123456789; იურიდიული მისამართი: ქ. თბილისი,\n"
    "რუსთაველის 12 (შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის ნინო კაპანაძის სახით და\n"
)
_CONTRACTOR_BLOCK = (
    "შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321, იურიდიული მისამართი: ქ. ბათუმი,\n"
    "ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“)\n"
)
_CLIENT_SIGNATURE = "„შემკვეთი“\nშპს „ალფა ჯგუფი“\nდირექტორი ნინო კაპანაძე\n"
_CONTRACTOR_SIGNATURE = "„შემსრულებელი“\nშპს „ბეტა სერვისი“\nდირექტორი: დავით მელაძე\n"
_PARTY_RULES = ("AOA.PARTY", "AOA.ID_CODE", "AOA.DIRECTOR")


class _Registry:
    """A registry holding exactly the given entries."""

    def __init__(self, *entries: RegistryEntry) -> None:
        self._entries = {e.id_code: e for e in entries}

    def lookup(self, id_code: str) -> RegistryEntry | None:
        return self._entries.get(id_code)


def _swap(text: str, a: str, b: str) -> str:
    return text.replace(a, "\0").replace(b, a).replace("\0", b)


def _check(act: str = ACT, contract: str = CONTRACT) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract)


def _ids(report: CheckReport) -> list[str]:
    return [f.rule_id for f in report.findings]


def _only(report: CheckReport, rule_id: str) -> Finding:
    [finding] = [f for f in report.findings if f.rule_id == rule_id]
    assert finding.message_ka and finding.message_en
    return finding


def _skips(report: CheckReport, rule_id: str) -> list[str]:
    return [s.reason_en for s in report.skipped if s.rule_id == rule_id]


def test_the_fixtures_are_what_the_tests_rewrite() -> None:
    for part in (_CLIENT_BLOCK, _CONTRACTOR_BLOCK, _CLIENT_SIGNATURE, _CONTRACTOR_SIGNATURE):
        assert ACT.count(part) == 1


def test_a_correct_act_has_no_party_finding_and_skips_no_party_rule() -> None:
    report = _check()
    assert [f for f in report.findings if f.rule_id.startswith(_PARTY_RULES)] == []
    assert [s for s in report.skipped if s.rule_id.startswith(_PARTY_RULES)] == []


# --- the role: which side each party is on ---------------------------------------------------


class TestPartyRole:
    @pytest.mark.parametrize("signatures_too", [False, True])
    def test_swapped_roles_are_an_error_on_each_party(self, signatures_too: bool) -> None:
        """The audit's repro: the act's two definitions swapped, with or without the signature
        blocks' role words - the definition is what the act says each party is."""
        act = _swap(ACT, _CLIENT_ROLE, _CONTRACTOR_ROLE)
        if signatures_too:
            act = _swap(act, "„შემკვეთი“\nშპს", "„შემსრულებელი“\nშპს")
        report = _check(act)
        assert _ids(report) == ["AOA.PARTY_ROLE", "AOA.PARTY_ROLE"]
        client, contractor = report.findings
        assert (client.severity, client.expected, client.actual) == (
            "error",
            "შემკვეთი",
            "შემსრულებელი",
        )
        assert "the act defines შპს ალფა ჯგუფი as 'შემსრულებელი' (the contractor)" in (
            client.message_en
        )
        assert "როგორც „შემსრულებელი“" in client.message_ka
        assert client.quote == "შემდგომში „შემსრულებელი“"  # marked on the definition itself
        assert (contractor.expected, contractor.actual) == ("შემსრულებელი", "შემკვეთი")

    def test_swapped_signature_labels_alone_do_not_overrule_the_definitions(self) -> None:
        """A signature block's bare role word can sit above the other column's name; the
        definitions say who is who, and they are right here."""
        act = _swap(ACT, "„შემკვეთი“\nშპს", "„შემსრულებელი“\nშპს")
        assert "AOA.PARTY_ROLE" not in _ids(_check(act))

    def test_sides_are_compared_not_words(self) -> None:
        """ "დამკვეთი" and "შემკვეთი" are one customer; "მიმწოდებელი" (a supplier) is paid, as a
        "შემსრულებელი" is."""
        act = ACT.replace(_CLIENT_ROLE, "(შემდგომში „დამკვეთი“)")
        contract = CONTRACT.replace(_CONTRACTOR_ROLE, "(შემდგომში „მიმწოდებელი“)")
        report = _check(act, contract)
        assert _ids(report) == []
        assert _skips(report, "AOA.PARTY_ROLE") == []

    def test_a_supplier_on_the_customers_side_is_caught(self) -> None:
        contract = CONTRACT.replace(_CONTRACTOR_ROLE, "(შემდგომში „მიმწოდებელი“)")
        act = ACT.replace(_CONTRACTOR_ROLE, "(შემდგომში „დამკვეთი“)")
        finding = _only(_check(act, contract), "AOA.PARTY_ROLE")
        assert (finding.expected, finding.actual) == ("მიმწოდებელი", "დამკვეთი")
        assert "(the customer), the contract as 'მიმწოდებელი' (the contractor)" in (
            finding.message_en
        )

    def test_an_act_that_defines_no_roles_is_skipped_per_party(self) -> None:
        """Only the signature blocks name the roles, and those are not trusted to decide."""
        act = ACT.replace(f" {_CLIENT_ROLE}", "; კომპანია").replace(
            f" {_CONTRACTOR_ROLE}", "; კომპანია"
        )
        report = _check(act)
        assert _ids(report) == []
        assert _skips(report, "AOA.PARTY_ROLE") == [
            "შპს ალფა ჯგუფი: the act does not define this party's role (შემდგომში „…“), so it "
            "was not compared with the contract",
            "შპს ბეტა სერვისი: the act does not define this party's role (შემდგომში „…“), so it "
            "was not compared with the contract",
        ]

    def test_a_contract_that_names_roles_only_by_signature_is_skipped_per_party(self) -> None:
        contract = (
            CONTRACT.replace(f" {_CLIENT_ROLE}", "; კომპანია").replace(
                f" {_CONTRACTOR_ROLE}", "; კომპანია"
            )
            + _CLIENT_SIGNATURE
            + _CONTRACTOR_SIGNATURE
        )
        report = _check(ACT, contract)
        assert "AOA.PARTY_ROLE" not in _ids(report)
        assert _skips(report, "AOA.PARTY_ROLE") == [
            "შპს ალფა ჯგუფი: the contract does not define this party's role (შემდგომში „…“), so "
            "its role in the act was not compared",
            "შპს ბეტა სერვისი: the contract does not define this party's role (შემდგომში „…“), "
            "so its role in the act was not compared",
        ]

    def test_a_contract_with_no_roles_at_all_skips_both_role_rules(self) -> None:
        contract = CONTRACT.replace(f" {_CLIENT_ROLE}", "; კომპანია").replace(
            f" {_CONTRACTOR_ROLE}", "; კომპანია"
        )
        report = _check(ACT, contract)
        assert [f for f in report.findings if f.rule_id.startswith("AOA.PARTY")] == []
        assert _skips(report, "AOA.PARTY_ROLE") == [
            "the contract names no customer and no contractor (such as „შემკვეთი“, "
            "„შემსრულებელი“), so the parties' roles were not compared"
        ]
        assert _skips(report, "AOA.PARTY_MISSING") == [
            "the contract names no customer and no contractor, so whether both are in the act was "
            "not checked"
        ]


# --- the identification code: whose it is ----------------------------------------------------


class TestIdCodeParty:
    def test_the_contractor_written_with_the_clients_code_is_caught(self) -> None:
        """The audit's repro: the two blocks used to merge on the shared code, and nothing was
        reported. The code is the one wrong thing, so it is the one thing reported."""
        report = _check(ACT.replace("987654321", "123456789"))
        assert _ids(report) == ["AOA.ID_CODE_PARTY"]
        finding = report.findings[0]
        assert (finding.severity, finding.expected, finding.actual) == (
            "error",
            "987654321",
            "123456789",
        )
        assert finding.message_en == (
            "identification code belongs to another party: the act gives შპს ბეტა სერვისი the "
            "code 123456789, which is შპს ალფა ჯგუფი's in the contract; შპს ბეტა სერვისი's code "
            "is 987654321"
        )
        # marked from the contractor's name to the code: the code alone is also, rightly,
        # written in the client's block
        assert finding.quote == "შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 123456789"

    def test_the_client_written_with_the_contractors_code_is_not_a_name_error(self) -> None:
        """The mirror case used to come out as PARTY_NAME, PARTY_ADDRESS and DIRECTOR, each with
        the contractor's values as the expected ones."""
        report = _check(ACT.replace("123456789", "987654321"))
        assert _ids(report) == ["AOA.ID_CODE_PARTY"]
        finding = report.findings[0]
        assert (finding.expected, finding.actual) == ("123456789", "987654321")
        assert finding.quote == "შპს „ალფა ჯგუფი“. საიდენტიფიკაციო კოდი: 987654321"

    def test_a_block_with_the_other_partys_name_is_a_name_error_not_a_code_error(self) -> None:
        """The contractor's block - its code, its address - under the client's name: the name
        is what is wrong, and the rest of the block says so."""
        act = ACT.replace("შპს „ბეტა სერვისი“", "შპს „ალფა ჯგუფი“", 1)
        report = _check(act)
        assert _ids(report) == ["AOA.PARTY_NAME"]
        finding = report.findings[0]
        assert (finding.expected, finding.actual) == ("შპს ბეტა სერვისი", "შპს ალფა ჯგუფი")

    @pytest.mark.parametrize(
        ("wrong", "right", "party"),
        [
            ("987654321", "123456789", "შპს ბეტა სერვისი"),
            ("123456789", "987654321", "შპს ალფა ჯგუფი"),
        ],
        ids=["contractor", "client"],
    )
    def test_the_registry_is_not_asked_about_a_code_on_the_wrong_party(
        self, wrong: str, right: str, party: str
    ) -> None:
        """The code's registry entry is the other company's. Compared with it, the party's name,
        address and director would each be "wrong", with the other company's as the value to
        write in. The code is the one mistake, and the skip note says why the rest was not
        looked up."""
        registry = _Registry(
            RegistryEntry(
                "123456789", "შპს „ალფა ჯგუფი“", "ქ. თბილისი, რუსთაველის 12", ("ნინო კაპანაძე",)
            ),
            RegistryEntry(
                "987654321", "შპს „ბეტა სერვისი“", "ქ. ბათუმი, ჭავჭავაძის 5", ("დავით მელაძე",)
            ),
        )
        assert check_aoa(aoa_text=ACT, contract_text=CONTRACT, registry=registry).findings == []
        report = check_aoa(
            aoa_text=ACT.replace(wrong, right), contract_text=CONTRACT, registry=registry
        )
        assert _ids(report) == ["AOA.ID_CODE_PARTY"]
        assert _skips(report, "AOA.REGISTRY") == [
            f"{party}: the act gives this party another party's identification code "
            "(AOA.ID_CODE_PARTY), so its details were not compared with that code's registry entry"
        ]

    def test_a_code_that_is_nobodys_stays_an_id_codes_error(self) -> None:
        report = _check(ACT.replace("123456789", "111222333"))
        assert _ids(report) == ["AOA.ID_CODES"]

    def test_parties_built_by_hand_are_matched_and_reported_without_a_place(self) -> None:
        """A party with no offsets - built by hand, not read from the text - is reported all
        the same, with no place to mark."""
        client = Party(name="შპს ალფა ჯგუფი", id_code="123456789")
        contractor = Party(name="შპს ბეტა სერვისი", id_code="987654321")
        act = DocumentView("a", "", [Party(name=contractor.name, id_code=client.id_code)])
        checker = AoaChecker(aoa=act, contract=DocumentView("c", "", [client, contractor]))
        checker.rule_parties()
        [finding] = checker.findings
        assert (finding.rule_id, finding.expected, finding.span) == (
            "AOA.ID_CODE_PARTY",
            "987654321",
            None,
        )

    def test_one_party_written_twice_in_the_contract_is_no_code_conflict(self) -> None:
        """The contract's signature block spells the contractor's name another way and gives no
        code: two records, one company. An act that uses that spelling matches one record by
        its code and the other by its name - not a code given to the wrong party. The spelling
        differs from the contract's requisites, and that is what is reported, as before."""
        contract = CONTRACT + "„შემსრულებელი“\nშპს „ბეტა-სერვისი“\nდირექტორი დავით მელაძე\n"
        act = ACT.replace("შპს „ბეტა სერვისი“", "შპს „ბეტა-სერვისი“")
        report = _check(act, contract)
        assert _ids(report) == ["AOA.PARTY_NAME"]
        assert report.findings[0].expected == "შპს ბეტა სერვისი"

    def test_the_matching_is_worked_out_once_a_run(self) -> None:
        checker = AoaChecker(
            aoa=DocumentView.build("a", ACT), contract=DocumentView.build("c", CONTRACT)
        )
        first = checker._matched_parties()
        assert checker._matched_parties() is first
        checker.run()
        assert checker._matched_parties() is not first  # a new run works it out afresh
        assert [m.match.name for m in first if m.match] == [
            m.match.name for m in checker._matched_parties() if m.match
        ]


# --- presence: both of the contract's parties are in the act -------------------------------------


class TestPartyMissing:
    def test_a_missing_contractor_is_an_error(self) -> None:
        act = ACT.replace(_CONTRACTOR_BLOCK, "").replace(_CONTRACTOR_SIGNATURE, "")
        report = _check(act)
        assert _ids(report) == ["AOA.PARTY_MISSING"]
        finding = report.findings[0]
        assert (finding.severity, finding.expected, finding.span) == (
            "error",
            "შპს ბეტა სერვისი (987654321)",
            None,
        )
        assert finding.message_en == (
            "a party to the contract is missing from the act: შპს ბეტა სერვისი (the contractor, "
            "identification code 987654321)"
        )
        assert "„შემსრულებელი“" in finding.message_ka

    def test_a_missing_client_is_an_error(self) -> None:
        act = ACT.replace(_CLIENT_BLOCK, "").replace(_CLIENT_SIGNATURE, "")
        finding = _only(_check(act), "AOA.PARTY_MISSING")
        assert finding.expected == "შპს ალფა ჯგუფი (123456789)"
        assert "(the customer, identification code 123456789)" in finding.message_en

    @pytest.mark.parametrize("keep_code", [True, False], ids=["code", "name-only"])
    def test_a_party_written_in_a_form_not_read_as_one_is_skipped(self, keep_code: bool) -> None:
        """No quotes around the name, so no party is read - but its code or its name is in the
        act, so it is not missing: it could not be compared, and the skip note says so."""
        block = _CONTRACTOR_BLOCK.replace("„ბეტა სერვისი“", "ბეტა სერვისი")
        if not keep_code:
            block = block.replace(", საიდენტიფიკაციო კოდი: 987654321", "")
        act = ACT.replace(_CONTRACTOR_BLOCK, block).replace(_CONTRACTOR_SIGNATURE, "")
        report = _check(act)
        assert "AOA.PARTY_MISSING" not in _ids(report)
        assert _skips(report, "AOA.PARTY_MISSING") == [
            "შპს ბეტა სერვისი: the act gives this party's code or name, but not in a form read as "
            "a party (a legal form and a quoted name), so its details were not compared"
        ]

    @pytest.mark.parametrize(
        ("legal_form", "written"),
        [("ი / მ", "ი / მ ნიკა ხარაზი"), ("ინდ. მეწარმე", "ნიკა ხარაზი")],
        ids=["spaced-legal-form", "name-without-legal-form"],
    )
    def test_an_entrepreneur_not_read_as_a_party_is_skipped(
        self, legal_form: str, written: str
    ) -> None:
        """An individual entrepreneur's legal form is not part of the name to look for: "ი / მ"
        written with spaces, or left out, still names the party."""
        contract = CONTRACT.replace("შპს „ბეტა სერვისი“", f"{legal_form} „ნიკა ხარაზი“")
        block = _CONTRACTOR_BLOCK.replace("შპს „ბეტა სერვისი“", written).replace(
            ", საიდენტიფიკაციო კოდი: 987654321", ""
        )
        act = ACT.replace(_CONTRACTOR_BLOCK, block).replace(_CONTRACTOR_SIGNATURE, "")
        report = _check(act, contract)
        assert "AOA.PARTY_MISSING" not in _ids(report)
        [reason] = _skips(report, "AOA.PARTY_MISSING")
        assert reason.startswith(f"{legal_form} ნიკა ხარაზი: the act gives this party's code")

    def test_digits_inside_an_account_number_are_not_the_partys_code(self) -> None:
        """The contractor is gone from the act; an account number that happens to hold its code's
        digits does not bring it back."""
        act = (
            ACT.replace(_CONTRACTOR_BLOCK, "")
            .replace(_CONTRACTOR_SIGNATURE, "")
            .replace("GE29NB0000000101904917", "GE29NB0987654321904917")
        )
        assert "AOA.PARTY_MISSING" in _ids(_check(act))

    def test_a_party_under_its_code_with_another_name_is_not_missing(self) -> None:
        """Same code, other name: the name is wrong (PARTY_NAME), the party is there. Renamed
        everywhere, signature block too, so only the code says who it is."""
        report = _check(ACT.replace("შპს „ბეტა სერვისი“", "შპს „გამა“"))
        assert _ids(report) == ["AOA.PARTY_NAME"]
        assert _skips(report, "AOA.PARTY_MISSING") == []


# --- the client's side is checked like the contractor's (10.2) ----------------------------------


class TestClientSide:
    def test_a_changed_client_address_is_caught(self) -> None:
        act = ACT.replace("რუსთაველის 12", "ვაჟა-ფშაველას 3")
        finding = _only(_check(act), "AOA.PARTY_ADDRESS")
        assert (finding.expected, finding.actual) == (
            "ქ. თბილისი, რუსთაველის 12",
            "ქ. თბილისი, ვაჟა-ფშაველას 3",
        )

    def test_a_changed_client_signatory_is_caught(self) -> None:
        act = ACT.replace("დირექტორი ნინო კაპანაძე", "დირექტორი ლევან ხარაზი")
        finding = _only(_check(act), "AOA.DIRECTOR")
        assert (finding.expected, finding.actual) == ("ნინო კაპანაძის", "ლევან ხარაზი")

    def test_a_changed_client_name_under_its_code_is_caught(self) -> None:
        report = _check(ACT.replace("შპს „ალფა ჯგუფი“", "შპს „ომეგა“", 1))
        assert _ids(report) == ["AOA.PARTY_NAME"]
        assert (report.findings[0].expected, report.findings[0].actual) == (
            "შპს ალფა ჯგუფი",
            "შპს ომეგა",
        )
