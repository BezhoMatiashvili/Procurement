"""Client rule 1 against an uploaded registry extract and the snapshot: what is the same party,
and what is a mistake. Invented parties (the canonical synthetic act of ``test_aoa_check``)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.registry.snapshot import RegistryEntry, load_snapshot, open_snapshot
from procurement_agent.registry.source import RefusedExtract, RegistrySource
from tests.unit.test_aoa_check import ACT

#: the act's two parties, as an extract of each would state them
ALPHA = RegistryEntry(
    id_code="123456789",
    name="შპს ალფა ჯგუფი",
    address="საქართველო, ქალაქი თბილისი, მთაწმინდის რაიონი, რუსთაველის გამზირი №12",
    directors=("ნინო კაპანაძე",),
    as_of="2026-04-20",
    legal_form="შეზღუდული პასუხისმგებლობის საზოგადოება",
    source="extract",
    positions=("დირექტორი",),
)
BETA = RegistryEntry(
    id_code="987654321",
    name="ბეტა სერვისი",
    address="საქართველო, ქ. ბათუმი, ჭავჭავაძის ქუჩა N5",
    directors=("დავით მელაძე",),
    as_of="2026-04-20",
    legal_form="შეზღუდული პასუხისმგებლობის საზოგადოება",
    source="extract",
)


def _check(*entries: RegistryEntry, act: str = ACT, **kw: object) -> CheckReport:
    return check_aoa(aoa_text=act, registry=RegistrySource.of(None, entries), **kw)  # type: ignore[arg-type]


def _registry(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.rule_id.startswith("AOA.REGISTRY")]


def _skips(report: CheckReport) -> list[str]:
    return [s.reason_en for s in report.skipped if s.rule_id.startswith("AOA.REGISTRY")]


def test_a_correct_act_against_its_parties_extracts_is_clean() -> None:
    """The registry writes the legal form out, the country, the district and "№"; the act
    abbreviates and leaves them out: none of it is a mistake."""
    report = _check(ALPHA, BETA)
    assert _registry(report) == []
    assert _skips(report) == []


def test_another_legal_form_is_an_error() -> None:
    report = _check(replace(ALPHA, name="სს ალფა ჯგუფი", legal_form="სააქციო საზოგადოება"), BETA)
    [f] = _registry(report)
    assert (f.rule_id, f.severity, f.expected) == ("AOA.REGISTRY_NAME", "error", "სს ალფა ჯგუფი")
    assert "the legal form does not match the registry: 'შპს', expected 'სს'" in f.message_en


def test_a_name_slip_is_corrected_in_the_acts_own_form() -> None:
    report = _check(replace(ALPHA, name="ალფა ჯგუფები"), BETA)
    [f] = _registry(report)
    assert f.rule_id == "AOA.REGISTRY_NAME"
    assert f.expected is not None and f.expected.startswith("შპს ")
    assert f.message_en.endswith("(registry extract, 2026-04-20)")


def test_an_address_restyled_is_the_same_and_one_shortened_is_a_warning() -> None:
    longer = replace(BETA, address="საქართველო, ქ. ბათუმი, ჭავჭავაძის ქუჩა N5, სართული 3, ბინა 9")
    [f] = _registry(_check(ALPHA, longer))
    assert (f.rule_id, f.severity) == ("AOA.REGISTRY_ADDRESS", "warning")
    assert f.expected == longer.address


def test_an_address_with_another_number_is_an_error() -> None:
    moved = replace(BETA, address="საქართველო, ქ. ბათუმი, ჭავჭავაძის ქუჩა N15")
    [f] = _registry(_check(ALPHA, moved))
    assert (f.rule_id, f.severity, f.expected) == (
        "AOA.REGISTRY_ADDRESS",
        "error",
        moved.address,
    )


def test_any_authorised_person_signs_in_either_order() -> None:
    two = replace(BETA, directors=("ეკა ლომიძე", "დავით მელაძე"), positions=("დირექტორი", ""))
    assert _registry(_check(ALPHA, two)) == []
    reversed_name = ACT.replace("დირექტორი: დავით მელაძე", "დირექტორი: მელაძე დავით")
    assert _registry(_check(ALPHA, BETA, act=reversed_name)) == []


def test_a_signatory_the_registry_does_not_list_is_corrected_to_its_nearest() -> None:
    report = _check(ALPHA, replace(BETA, directors=("დავით მელიძე",)))
    found = [f for f in _registry(report) if f.rule_id == "AOA.REGISTRY_DIRECTOR"]
    assert found
    assert all(f.severity == "error" for f in found)
    # every form the act writes is corrected in its own case, to the registry's person
    assert {f.expected for f in found} <= {"დავით მელიძე", "დავით მელიძის"}
    assert "(registry: დავით მელიძე)" in found[0].message_en


def test_a_party_being_wound_up_is_a_warning() -> None:
    closing = replace(BETA, status="ლიკვიდაციის პროცესი დაწყებულია 01/08/2026")
    [f] = _registry(_check(ALPHA, closing))
    assert (f.rule_id, f.severity) == ("AOA.REGISTRY_STATUS", "warning")
    assert "ლიკვიდაციის პროცესი დაწყებულია" in f.message_en


def test_an_old_extract_is_noted() -> None:
    old = replace(ALPHA, as_of="2026-01-05")
    [f] = _registry(_check(old, BETA))
    assert (f.rule_id, f.severity) == ("AOA.REGISTRY", "info")
    assert "printed on 2026-01-05, 115 days before the act" in f.message_en


def test_with_extracts_only_a_party_without_one_is_a_skip_not_a_warning() -> None:
    report = _check(ALPHA)
    assert _registry(report) == []
    assert _skips(report) == [
        "შპს ბეტა სერვისი: no registry extract was uploaded for code 987654321, so the party "
        "was not checked against the registry"
    ]


def test_an_extract_of_no_party_and_a_refused_file_are_said() -> None:
    stranger = replace(BETA, id_code="555555555", name="შპს უცხო")
    source = RegistrySource.of(
        None, [ALPHA, BETA, stranger], [RefusedExtract("invoice.pdf", "no registry extract title")]
    )
    report = check_aoa(aoa_text=ACT, registry=source)
    assert (
        "invoice.pdf: the file could not be read as a registry extract (no registry extract title)"
        in _skips(report)
    )
    assert "the uploaded extract (შპს უცხო, 555555555) is of no party of the act" in _skips(report)


def test_the_extract_wins_over_the_snapshot(tmp_path: Path) -> None:
    csv_path = tmp_path / "r.csv"
    csv_path.write_text(
        "id_code,name,address,directors\n987654321,შპს ბეტა სერვისი,ქ. ბათუმი ჭავჭავაძის 99,"
        "სხვა პირი\n123456789,შპს ალფა ჯგუფი,ქ. თბილისი რუსთაველის 12,ნინო კაპანაძე\n",
        encoding="utf-8",
    )
    load_snapshot(csv_path, tmp_path / "r.db", as_of="2026-01-01")
    snapshot = open_snapshot(tmp_path / "r.db")
    report = check_aoa(aoa_text=ACT, registry=RegistrySource.of(snapshot, [BETA]))
    assert _registry(report) == []  # the snapshot's stale BETA row is not consulted
    assert snapshot is not None
    snapshot.close()
