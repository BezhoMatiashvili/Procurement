"""Business-registry snapshot loading and lookup. Synthetic data only."""

from __future__ import annotations

from pathlib import Path

import pytest

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.registry.snapshot import (
    RegistryEntry,
    RegistryFormatError,
    load_snapshot,
    open_snapshot,
)

CSV = """id_code,name,address,directors,as_of
123456789,შპს „ალფა ჯგუფი“,"ქ. თბილისი, რუსთაველის 12",ნინო კაპანაძე,2026-09-01
987654321,შპს „ბეტა სერვისი“,"ქ. ბათუმი, ჭავჭავაძის 5",დავით მელაძე;ანა წერეთელი,2026-09-01
"""


@pytest.fixture
def snapshot(tmp_path: Path):
    csv_path = tmp_path / "registry.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    db = tmp_path / "registry.db"
    assert load_snapshot(csv_path, db, as_of="2026-09-01") == 2
    with open_snapshot(db) as snap:
        yield snap


def test_lookup_returns_the_company(snapshot) -> None:
    entry = snapshot.lookup("123456789")
    assert entry.name == "შპს „ალფა ჯგუფი“"
    assert entry.directors == ("ნინო კაპანაძე",)
    assert entry.as_of == "2026-09-01"


def test_multiple_directors_are_split(snapshot) -> None:
    assert snapshot.lookup("987654321").directors == ("დავით მელაძე", "ანა წერეთელი")


def test_unknown_code_returns_none(snapshot) -> None:
    assert snapshot.lookup("000000000") is None


def test_snapshot_reports_its_age_and_size(snapshot) -> None:
    assert snapshot.as_of == "2026-09-01"
    assert snapshot.count == 2


def test_a_missing_column_fails_loudly(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("company,address\nx,y\n", encoding="utf-8")
    with pytest.raises(RegistryFormatError, match="id_code"):
        load_snapshot(bad, tmp_path / "r.db")


def test_no_snapshot_is_a_degraded_mode_not_a_crash(tmp_path: Path) -> None:
    assert open_snapshot(tmp_path / "absent.db") is None


def test_a_partial_import_never_replaces_a_good_snapshot(tmp_path: Path) -> None:
    csv_path = tmp_path / "r.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    db = tmp_path / "registry.db"
    load_snapshot(csv_path, db)
    broken = tmp_path / "broken.csv"
    broken.write_text("company\nx\n", encoding="utf-8")
    with pytest.raises(RegistryFormatError):
        load_snapshot(broken, db)
    with open_snapshot(db) as snap:
        assert snap.count == 2


class TestRegistryRules:
    ACT = """მიღება-ჩაბარების აქტი
შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321, იურიდიული მისამართი: ქ. ბათუმი,
ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“)
დირექტორი: დავით მელაძე
"""

    def test_a_matching_party_produces_no_registry_findings(self, snapshot) -> None:
        """Nothing to report and nothing left unchecked: no finding, and no skip note either."""
        report = check_aoa(aoa_text=self.ACT, registry=snapshot)
        assert not [f for f in report.findings if f.rule_id.startswith("AOA.REGISTRY")]
        assert not [s for s in report.skipped if s.rule_id.startswith("AOA.REGISTRY")]

    def test_a_name_that_differs_from_the_registry_is_caught(self, snapshot) -> None:
        act = self.ACT.replace("შპს „ბეტა სერვისი“", "შპს „ბეტა“")
        report = check_aoa(aoa_text=act, registry=snapshot)
        assert "AOA.REGISTRY_NAME" in {f.rule_id for f in report.findings}

    def test_a_registry_mismatch_names_the_registry_as_its_source(self, snapshot) -> None:
        """It used to say "does not match the contract", although the registry said otherwise."""
        act = self.ACT.replace("შპს „ბეტა სერვისი“", "შპს „ბეტა“").replace(
            "ჭავჭავაძის 5", "აღმაშენებლის 9"
        )
        found = check_aoa(aoa_text=act, registry=snapshot).errors
        assert {f.rule_id for f in found} == {"AOA.REGISTRY_NAME", "AOA.REGISTRY_ADDRESS"}
        for f in found:
            assert "does not match the registry" in f.message_en
            assert "contract" not in f.message_en
            assert "რეესტრის მონაცემებს" in f.message_ka

    def test_a_signatory_not_in_the_registry_is_caught(self, snapshot) -> None:
        act = self.ACT.replace("დავით მელაძე", "ზურაბ ქართველი")
        report = check_aoa(aoa_text=act, registry=snapshot)
        assert "AOA.REGISTRY_DIRECTOR" in {f.rule_id for f in report.errors}

    def test_an_unknown_id_code_is_a_warning(self, snapshot) -> None:
        act = self.ACT.replace("987654321", "111111111")
        report = check_aoa(aoa_text=act, registry=snapshot)
        assert "AOA.REGISTRY" in {f.rule_id for f in report.findings}


class _Registry:
    """A registry holding exactly the given entries."""

    def __init__(self, *entries: RegistryEntry) -> None:
        self._entries = {e.id_code: e for e in entries}

    def lookup(self, id_code: str) -> RegistryEntry | None:
        return self._entries.get(id_code)


_BETA = RegistryEntry(
    id_code="987654321",
    name="შპს „ბეტა სერვისი“",
    address="ქ. ბათუმი, ჭავჭავაძის 5",
    directors=("დავით მელაძე",),
)


def _registry_notes(act: str, registry: _Registry) -> list[tuple[str, str]]:
    report = check_aoa(aoa_text=act, registry=registry)
    assert not [f for f in report.findings if f.rule_id.startswith("AOA.REGISTRY")]
    return [(s.rule_id, s.reason_en) for s in report.skipped if s.rule_id.startswith("AOA.REG")]


class TestRegistryRulesThatCannotRun:
    """Every party and field the registry could not be asked about is named in a skip note.
    Each of these used to pass silently."""

    ACT = TestRegistryRules.ACT

    def test_the_matching_party_leaves_no_note(self) -> None:
        assert _registry_notes(self.ACT, _Registry(_BETA)) == []

    def test_an_act_with_no_party_says_nothing_was_checked(self) -> None:
        [(rule, why)] = _registry_notes("მიღება-ჩაბარების აქტი\nმომსახურება შესრულდა.", _Registry())
        assert rule == "AOA.REGISTRY"
        assert "no party" in why

    def test_each_party_without_an_id_code_is_named(self) -> None:
        act = "შპს „გამა“ (შემდგომში „შემკვეთი“)\nშპს „დელტა“ (შემდგომში „მყიდველი“)\n" + self.ACT
        notes = _registry_notes(act, _Registry(_BETA))
        assert [rule for rule, _ in notes] == ["AOA.REGISTRY", "AOA.REGISTRY"]
        assert "შპს გამა" in notes[0][1]
        assert "შპს დელტა" in notes[1][1]

    def test_a_registry_entry_without_an_address_or_a_director_is_named(self) -> None:
        bare = RegistryEntry(id_code="987654321", name="შპს „ბეტა სერვისი“")
        notes = _registry_notes(self.ACT, _Registry(bare))
        assert [rule for rule, _ in notes] == ["AOA.REGISTRY_ADDRESS", "AOA.REGISTRY_DIRECTOR"]
        assert all(
            "registry snapshot has no" in why or "names no director" in why for _, why in notes
        )

    def test_an_act_party_without_an_address_or_a_signatory_is_named(self) -> None:
        act = "შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321 (შემდგომში „შემსრულებელი“)\n"
        notes = _registry_notes(act, _Registry(_BETA))
        assert notes == [
            (
                "AOA.REGISTRY_ADDRESS",
                "შპს ბეტა სერვისი: the act states no legal address, so none was compared with "
                "the registry",
            ),
            (
                "AOA.REGISTRY_DIRECTOR",
                "შპს ბეტა სერვისი: the act names no signatory (director), so none was compared "
                "with the registry",
            ),
        ]
